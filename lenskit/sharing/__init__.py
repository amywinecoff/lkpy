"""
Support for sharing and saving models and data structures.
"""

import sys
import os
import pathlib
import warnings
from abc import abstractmethod, ABC
from contextlib import contextmanager
import tempfile
import threading
import logging
import pickle
try:
    import multiprocessing.shared_memory as shm
except ImportError:
    shm = None

import binpickle

_log = logging.getLogger(__name__)

_store_state = threading.local()


def _save_mode():
    return getattr(_store_state, 'mode', 'save')


def _active_stores():
    if not hasattr(_store_state, 'active'):
        _store_state.active = []
    return _store_state.active


@contextmanager
def sharing_mode():
    """
    Context manager to tell models that pickling will be used for cross-process
    sharing, not model persistence.
    """
    old = _save_mode()
    _store_state.mode = 'share'
    try:
        yield
    finally:
        _store_state.mode = old


def in_share_context():
    """
    Query whether sharing mode is active.  If ``True``, we are currently in a
    :func:`sharing_mode` context, which means model pickling will be used for
    cross-process sharing.
    """
    return _save_mode() == 'share'


class PersistedModel(ABC):
    """
    A persisted model for inter-process model sharing.

    These objects can be pickled for transmission to a worker process.

    .. note::
        Subclasses need to override the pickling protocol to implement the
        proper pickling implementation.
    """

    @abstractmethod
    def get(self):
        """
        Get the persisted model, reconstructing it if necessary.
        """
        pass

    @abstractmethod
    def close(self):
        """
        Release the persisted model resources.  Should only be called in the
        parent process (will do nothing in a child process).
        """
        pass


def persist(model):
    """
    Persist a model for cross-process sharing.

    This will return a persiste dmodel that can be used to reconstruct the model
    in a worker process (using :func:`reconstruct`).

    This function automatically selects a model persistence strategy from the
    the following, in order:

    1. If `LK_TEMP_DIR` is set, use :mod:`binpickle` in shareable mode to save
       the object into the LensKit temporary directory.
    2. If :mod:`multiprocessing.shared_memory` is available, use :mod:`pickle`
       to save the model, placing the buffers into shared memory blocks.
    3. Otherwise, use :mod:`binpickle` in shareable mode to save the object
       into the system temporary directory.

    Args:
        model(obj): the model to persist.

    Returns:
        PersistedModel: The persisted object.
    """
    lk_tmp = os.environ.get('LK_TEMP_DIR', None)
    if lk_tmp is not None:
        return persist_binpickle(model, lk_tmp)
    elif shm is not None:
        return persist_shm(model)
    else:
        return persist_binpickle(model)


def persist_binpickle(model, dir=None):
    """
    Persist a model using binpickle.

    Args:
        model: The model to persist.
        dir: The temporary directory for persisting the model object.

    Returns:
        PersistedModel: The persisted object.
    """
    fd, path = tempfile.mkstemp(suffix='.bpk', prefix='lkpy-', dir=dir)
    os.close(fd)
    path = pathlib.Path(path)
    _log.info('persisting %s to %s', model, path)
    with binpickle.BinPickler.mappable(path) as bp, sharing_mode():
        bp.dump(model)
    return BPKPersisted(path)


class BPKPersisted(PersistedModel):
    def __init__(self, path):
        self.path = path
        self.is_owner = True
        self._bpk_file = None
        self._model = None

    def get(self):
        if self._bpk_file is None:
            self._bpk_file = binpickle.BinPickleFile(self.path, direct=True)
            self._model = self._bpk_file.load()
        return self._model

    def close(self, unlink=True):
        if self._bpk_file is not None:
            self._model = None
            try:
                self._bpk_file.close()
            except IOError as e:
                _log.warn('error closing %s: %s', self.path, e)
            self._bpk_file = None

        if self.is_owner and unlink:
            assert self._model is None
            if unlink:
                self.path.unlink()
            self.is_owner = False

    def __getstate__(self):
        d = dict(self.__dict__)
        d['is_owner'] = False
        return d

    def __del___(self):
        self.close(False)


def persist_shm(model, dir=None):
    """
    Persist a model using binpickle.

    Args:
        model: The model to persist.
        dir: The temporary directory for persisting the model object.

    Returns:
        PersistedModel: The persisted object.
    """
    if shm is None:
        raise ImportError('multiprocessing.shared_memory')

    buffers = []
    buf_keys = []

    def buf_cb(buf):
        ba = buf.raw()
        block = shm.SharedMemory(create=True, size=ba.nbytes)
        _log.debug('serializing %d bytes to %s', ba.nbytes, block.name)
        # blit the buffer into shared memory
        block.buf[:ba.nbytes] = ba
        buffers.append(block)
        buf_keys.append((block.name, ba.nbytes))

    with sharing_mode():
        data = pickle.dumps(model, protocol=5, buffer_callback=buf_cb)
        shm_bytes = sum(b.size for b in buffers)
        _log.info('serialized %s to %d pickle bytes with %d buffers of %d bytes',
                  model, len(data), len(buffers), shm_bytes)

    return SHMPersisted(data, buf_keys, buffers)


class SHMPersisted(PersistedModel):
    buffers = []
    _model = None

    def __init__(self, data, buf_specs, buffers):
        self.pickle_data = data
        self.buffer_specs = buf_specs
        self.buffers = buffers
        self.is_owner = True

    def get(self):
        if self._model is None:
            buffers = []
            shm_bufs = []
            for bn, bs in self.buffer_specs:
                # funny business with buffer sizes
                block = shm.SharedMemory(name=bn)
                _log.debug('%s: %d bytes (%d used)', block.name, bs, block.size)
                buffers.append(block.buf[:bs])
                shm_bufs.append(block)

            self.buffers = shm_bufs
            self._model = pickle.loads(self.pickle_data, buffers=buffers)

        return self._model

    def close(self, unlink=True):
        self._model = None
        if self.is_owner:
            for buf in self.buffers:
                buf.close()
                buf.unlink()
            del self.buffers
            self.is_owner = False

    def __getstate__(self):
        return {
            'pickle_data': self.pickle_data,
            'buffer_specs': self.buffer_specs,
            'is_owner': False
        }


def get_store(reuse=True, *, in_process=False):
    """
    Get a model store, using the best available on the current platform.  The
    resulting store should be used as a context manager, as in:

    >>> with get_store() as store:
    ...     pass

    This function uses the following priority list for locating a suitable store:

    1. The currently-active store, if ``reuse=True``
    2. A no-op store, if ``in_process=True``
    3. :class:`SHMModelStore`, if on Python 3.8
    4. :class:`FileModelStore`

    Args:
        reuse(bool):
            If a store is active (with a ``with`` block), use that store instead
            of creating a new one.
        in_process(bool):
            If ``True``, then create a no-op store for use without multiprocessing.

    Returns:
        BaseModelStore: the model store.
    """
    stores = _active_stores()
    if reuse and stores:
        return stores[-1]
    elif in_process:
        return NoopModelStore()
    elif SHMModelStore.ENABLED:
        return SHMModelStore()
    else:
        return FileModelStore()


class BaseModelClient:
    """
    Model store client to get models given keys.  Clients must be able to be cheaply
    pickled and de-pickled to enable worker processes to access them.
    """

    @abstractmethod
    def get_model(self, key):
        """
        Get a model from the  model store.

        Args:
            key: the model key to retrieve.

        Returns:
            SharedObject:
                The model, previously stored with :meth:`BaseModelStore.put_model`,
                wrapped in a :class:`SharedObject` to manage underlying resources.
        """


class SharedObject:
    """
    Wrapper for a shared object that can release it when the object is no longer needed.

    Objects of this type are context managers, that return the *shared object* (not
    themselves) when entered.

    Any other refernces to ``object``, or its contents, **must** be released before
    calling :meth:`release` or exiting the context manager.  Among other things, that
    means that you will need to delete its variable::

        with client.get_model(k) as model:
            # model here is the actual model object wrapped by the SharedObject
            # returned by get_model
            pass       # actually do the things you want to do
            del model  # release model, so the shared object can be closed

    Be careful of stray references to the model object!  Some things we have seen
    causing stray references include:

    * passing the algorithm to a logger (call :func:`str` on it explicitly), at least
      in the test harness

    The default implementation uses :func:`sys.getrefcount` to provide debugging
    support to help catch stray references.

    Attributes:
        object: the underlying shared object.
    """
    _rc = None
    _exiting = False

    def __init__(self, obj):
        self.object = obj

    def release(self):
        """
        Release the shared object.  Automatically called by :meth:`__exit__`, so in
        normal use of a shared object with a ``with`` statement, this method is not
        needed.

        The base class implementation simply deletes the object reference.  Subclasses
        should override this method to handle their own release logic.
        """
        if self.object is not None:
            rc = sys.getrefcount(self.object)
            if self._rc and rc > self._rc:
                # since most backends won't actually crash, track & emit a warning
                _log.debug('reference count to %s increased from %d to %d',
                           self.object, self._rc, rc)
                wm = f'reference count to {self.object} increased, object leak?'
                level = 3 if self._exiting else 2
                warnings.warn(wm, ResourceWarning, stacklevel=level)
            del self.object

    def __enter__(self):
        self._rc = sys.getrefcount(self.object)
        return self.object

    def __exit__(self, *args):
        self._exiting = True
        try:
            self.release()
        finally:
            del self._exiting


class BaseModelStore(BaseModelClient):
    """
    Base class for storing models for access across processes.

    Stores are also context managers that initalize themselves and clean themselves
    up.  As context managers, they are also re-entrant, and register themselves so
    that :func:`create_store` can re-use existing managers.
    """

    _act_count = 0

    @abstractmethod
    def put_model(self, model):
        """
        Store a model in the model store.

        Args:
            model(object): the model to store.

        Returns:
            a key to retrieve the model with :meth:`BaseModelClient.get_model`
        """
        pass

    def put_serialized(self, path, binpickle=False):
        """
        Deserialize a model and load it into the store.

        The base class method unpickles ``path`` and calls :meth:`put_model`.

        Args:
            path(str or pathlib.Path): the path to deserialize
            binpickle: if ``True``, deserialize with :func:`binpickle.load` instead of pickle.
        """
        if binpickle:
            self.put_model(bpk.load(path))
        else:
            with open(path, 'rb') as mf:
                return self.put_model(pickle.load(mf))

    @abstractmethod
    def client(self):
        """
        Get a client for the model store.  Clients are cheap to pass to
        child processes for multiprocessing.

        Returns:
            BaseModelClient: the model client.
        """
        pass

    def init(self):
        "Initialize the store."

    def shutdown(self):
        "Shut down the store"

    def __enter__(self):
        if self._act_count == 0:
            self.init()
        self._act_count = self._act_count + 1
        _active_stores().append(self)
        return self

    def __exit__(self, *args):
        self._act_count = self._act_count - 1
        if self._act_count == 0:
            self.shutdown()
        assert _active_stores()[-1] is self
        _active_stores().pop()
        return None

    def __getstate__(self):
        raise RuntimeError('stores cannot be pickled, do you want to use the client?')


class NoopModelStore(BaseModelStore):
    """
    Model store that does nothing - models are their own keys.  Only useful in
    single-threaded computations.
    """

    def get_model(self, key):
        return SharedObject(key)

    def put_model(self, model):
        return model

    def client(self):
        return self  # since we're only single-threaded, we are the client

    def __str__(self):
        return 'NoopModelStore'


# more imports
from .file import FileModelStore
from .sharedmem import SHMModelStore
