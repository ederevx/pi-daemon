"""Single-instance ownership of one runtime root, held for the process."""

import os
import uuid

from .error import InstanceHeldError


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
        except (BlockingIOError, PermissionError):
            # flock answers EWOULDBLOCK; msvcrt.locking answers EACCES.
            handle.close()
            raise InstanceHeldError("another instance owns this runtime root")
        except OSError:
            handle.close()
            raise
        instance = uuid.uuid4().hex
        handle.seek(0)
        handle.truncate()
        handle.write(instance.encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
        self._handle = handle
        self._instance = instance
        return instance

    def held(self):
        """A probe: whether another process owns the lock right now.

        It creates nothing and keeps nothing: a missing file is an
        unowned root, and a free lock is released before returning.
        """
        try:
            handle = open(self._path, "rb")
        except FileNotFoundError:
            return False
        try:
            if os.name == "nt":
                if os.fstat(handle.fileno()).st_size == 0:
                    return False
                return not self._probe_windows(handle)
            return not self._probe_posix(handle)
        finally:
            handle.close()

    @staticmethod
    def _probe_posix(handle):
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return True

    @staticmethod
    def _probe_windows(handle):
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return True

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
