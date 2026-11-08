"""Single-instance ownership of one runtime root, held for the process."""

import os
import uuid


class InstanceCoordinator:
    """One lock file, one generation; a probe never grants ownership."""

    def __init__(self, path):
        self._path = path
        self._handle = None
        self._instance = None

    def instance(self):
        return self._instance

    def acquire(self):
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        handle = open(self._path, "a+b")
        try:
            self._lock(handle)
        except OSError:
            handle.close()
            raise RuntimeError("another instance owns this runtime root")
        instance = uuid.uuid4().hex
        handle.seek(0)
        handle.truncate()
        handle.write(instance.encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
        self._handle = handle
        self._instance = instance
        return instance

    def _lock(self, handle):
        if os.name == "nt":
            import msvcrt

            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release(self):
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            handle.close()
        self._instance = None
