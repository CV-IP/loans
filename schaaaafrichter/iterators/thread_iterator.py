import multiprocessing
import threading
import queue
from multiprocessing import sharedctypes

import numpy
import six
import warnings
from chainer.dataset import iterator


class ThreadIterator(iterator.Iterator):

    _last_signal = object()

    def __init__(self, dataset, batch_size, repeat=True, shuffle=True,
                 n_processes=None, n_prefetch=1, shared_mem=None):
        self.dataset = dataset
        self.batch_size = batch_size
        self._repeat = repeat
        self._shuffle = shuffle
        self._prefetch_order = None  # used at the end of each epoch

        self.n_threads = n_processes or multiprocessing.cpu_count()
        self.n_prefetch = max(n_prefetch, 1)
        self._shared_mem_size = shared_mem

        self._finalized = None

        self.reset()

    def __del__(self):
        self.finalize()

    def __next__(self):
        if not self._repeat and self.epoch > 0:
            raise StopIteration

        self._previous_epoch_detail = self.epoch_detail

        self.is_new_epoch = False
        if self._finalized is None:
            self._init()  # start workers
            # load for the first iteration
            for _ in six.moves.range(self.n_prefetch):
                self._invoke_prefetch()

        batch = self._get()
        self._invoke_prefetch()  # prefetch for the next iteration
        return batch

    next = __next__

    @property
    def epoch_detail(self):
        return self.epoch + self.current_position / len(self.dataset)

    @property
    def previous_epoch_detail(self):
        if self._previous_epoch_detail < 0:
            return None
        return self._previous_epoch_detail

    def finalize(self):
        if self._finalized is None or self._finalized.is_set():
            return

        self._finalized.set()
        self._ordered_data_queue.put(self._last_signal)
        self._data_queue.put((-1, -1, -1))
        for _ in self._workers:
            self._index_queue.put((-1, -1, -1))  # termination signal

        for worker in self._workers:
            worker.join()
        self._get_data_loop_thread.join()

    def serialize(self, serializer):
        self.current_position = serializer('current_position',
                                           self.current_position)
        self.epoch = serializer('epoch', self.epoch)
        self.is_new_epoch = serializer('is_new_epoch', self.is_new_epoch)
        try:
            serializer('order', self._order)
        except KeyError:
            serializer('_order', self._order)
        try:
            self._previous_epoch_detail = serializer(
                'previous_epoch_detail', self._previous_epoch_detail)
        except KeyError:
            # guess previous_epoch_detail for older version
            self._previous_epoch_detail = self.epoch + \
                                          (self.current_position - self.batch_size) / len(self.dataset)
            if self.epoch_detail > 0:
                self._previous_epoch_detail = max(
                    self._previous_epoch_detail, 0.)
            else:
                self._previous_epoch_detail = -1.

    def _init(self):
        finalized = threading.Event()
        self._index_queue = queue.Queue()
        self._data_queue = queue.Queue()
        self._ordered_data_queue = queue.Queue()
        self._unused_mem_queue = queue.Queue()
        self._mem_list = []
        self._cnt = 0

        self._workers = []

        if self._shared_mem_size is not None:
            self._init_thread()

        self._get_data_loop_thread = threading.Thread(
            target=_get_data_loop, name="get_data_loop",
            args=(self._data_queue, self._ordered_data_queue,
                  self._mem_list, self._unused_mem_queue,
                  finalized, self._last_signal))
        self._get_data_loop_thread.daemon = True
        self._get_data_loop_thread.start()

        self._finalized = finalized

    def _init_thread(self):
        assert len(self._workers) == 0
        assert self._shared_mem_size is not None
        mem_size = self._shared_mem_size
        for i in six.moves.range(self.batch_size * (self.n_prefetch + 1)):
            self._mem_list.append(sharedctypes.RawArray('b', mem_size))
            self._unused_mem_queue.put(i)

        args = (self.dataset, self._index_queue, self._data_queue,
                self._mem_list)
        for _ in range(self.n_threads):
            worker = threading.Thread(target=_worker, args=args)
            worker.daemon = True
            self._workers.append(worker)
            worker.start()

    def _invoke_prefetch(self):
        n = len(self.dataset)
        i = self._pushed_position
        if i is None:  # first iteration
            i = self.current_position

        order = self._order
        measure_mode = len(self._workers) == 0
        max_size = 0
        for _ in six.moves.range(self.batch_size):
            if i >= n:
                if not self._repeat:
                    break
                i = 0
                if order is not None:
                    # We cannot shuffle the order directly here, since the
                    # iterator may be serialized before the prefetched data are
                    # consumed by the user, in which case an inconsistency
                    # appears.
                    order = order.copy()
                    numpy.random.shuffle(order)
            index = i if order is None else order[i]
            if measure_mode:
                data = self.dataset[index]
                max_size = max(max_size, _measure(data))
                self._data_queue.put((self._cnt, None, data))
                del data
            else:
                self._index_queue.put(
                    (self._cnt, self._unused_mem_queue.get(), index))
            self._cnt += 1
            i += 1

        self._prefetch_order = order  # Temporarily store the shuffled order.
        self._pushed_position = i

        if measure_mode:
            self._shared_mem_size = max_size
            self._init_thread()

    def _get(self):
        n = len(self.dataset)
        i = self.current_position

        batch = []
        for _ in six.moves.range(self.batch_size):
            d = self._ordered_data_queue.get()
            if d is self._last_signal:
                break
            batch.append(d)
            i += 1
            if i >= n:
                self.epoch += 1
                self.is_new_epoch = True
                i = 0
                if not self._repeat:
                    break
        self.current_position = i
        # Eventually overwrite the (possibly shuffled) order.
        self._order = self._prefetch_order
        return batch

    def reset(self):
        if getattr(self, 'current_position', 0) != 0:
            raise NotImplementedError(
                'Reset of MultiProcessIterator in the middle of a epoch is '
                'currently not supported.')
        if getattr(self, 'epoch', 0) != 0 and self._repeat:
            raise NotImplementedError(
                'Reset of repeating MultiProcessIterator is currently not '
                'supported.')
        if getattr(self, '_finalized', None) is not None and \
                self._finalized.is_set():
            raise NotImplementedError(
                'Reset of finalized MultiProcessIterator is currently not '
                'supported.')

        self.current_position = 0
        self.epoch = 0
        self.is_new_epoch = False

        # use -1 instead of None internally.
        self._previous_epoch_detail = -1.

        self._pushed_position = None  # initialized at the first iteration

        if self._shuffle:
            self._order = numpy.random.permutation(len(self.dataset))
        else:
            self._order = None

        if self._finalized is not None:
            for _ in six.moves.range(self.n_prefetch):
                self._invoke_prefetch()


def _get_data_loop(data_queue, ordered_data_queue, mem_list,
                   unused_mem_queue, finalized, last_signal):
    buf = {}
    cnt = 0
    while not finalized.is_set():
        if cnt in buf:
            data = buf.pop(cnt)
        else:
            try:
                c, mem_index, data = data_queue.get(timeout=0.5)
            except six.moves.queue.Empty:
                continue
            if c < 0:
                break
            if mem_index is not None:
                data = _unpack(data, mem_list[mem_index])
                unused_mem_queue.put(mem_index)
            if c != cnt:
                buf[c] = data
                continue
        ordered_data_queue.put(data)
        del data
        cnt += 1
    ordered_data_queue.put(last_signal)


class _PackedNdarray(object):

    def __init__(self, array, mem, offset):
        self.shape = array.shape
        self.dtype = array.dtype
        self.nbytes = array.nbytes
        self.size = array.size
        self.offset = offset
        total = self.offset + self.nbytes
        if total > len(mem):
            raise ValueError(
                'Shared memory size is too small. expect:{}, actual:{}'.format(
                    total, len(mem)))
        target = numpy.frombuffer(mem, self.dtype, self.size, self.offset)
        target[...] = array.ravel()

    def unpack(self, mem):
        ret = numpy.frombuffer(mem, self.dtype, self.size, self.offset)
        ret = ret.reshape(self.shape).copy()
        return ret


def _measure(data):
    expect = 0
    t = type(data)
    if t is tuple or t is list or t is dict:
        for v in data:
            if isinstance(v, numpy.ndarray):
                expect += v.nbytes
    return expect


def _pack(data, mem):
    if len(mem) == 0:
        return data
    t = type(data)
    offset = 0
    over = False
    if t is tuple or t is list:
        ret = []
        for v in data:
            if isinstance(v, numpy.ndarray):
                if v.nbytes + offset > len(mem):
                    over = True
                else:
                    v = _PackedNdarray(v, mem, offset)
                    offset += v.nbytes
            ret.append(v)
        data = t(ret)
    elif t is dict:
        ret = {}
        for k, v in six.iteritems(data):
            if isinstance(v, numpy.ndarray):
                if v.nbytes + offset > len(mem):
                    over = True
                else:
                    v = _PackedNdarray(v, mem, offset)
                    offset += v.nbytes
            ret[k] = v
        data = ret
    if over:
        expect = _measure(data)
        warnings.warn(
            'Shared memory size is too small.\n' +
            'Please set shared_mem option for MultiprocessIterator.\n' +
            'Expect shared memory size: {} bytes.\n'.format(expect) +
            'Actual shared memory size: {} bytes.'.format(len(mem)),
            UserWarning)
    return data


def _unpack(data, mem):
    if len(mem) == 0:
        return data
    t = type(data)
    if t is tuple or t is list:
        ret = []
        for v in data:
            if isinstance(v, _PackedNdarray):
                v = v.unpack(mem)
            ret.append(v)
        data = t(ret)
    elif t is dict:
        ret = {}
        for k, v in six.iteritems(data):
            if isinstance(v, _PackedNdarray):
                v = v.unpack(mem)
            ret[k] = v
        data = ret
    return data


def _worker(dataset, in_queue, out_queue, mem_list):
    while True:
        cnt, mem_index, index = in_queue.get()
        if cnt < 0:
            break
        mem = mem_list[mem_index]
        data = _pack(dataset[index], mem)
        out_queue.put((cnt, mem_index, data))
