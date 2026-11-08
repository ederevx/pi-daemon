"""The control listener: accepted connections and their lifecycle."""

import socket
import threading

from ..wire import BAD_HANDSHAKE, BAD_REQUEST, error_reply
from .control_action import ControlAction
from .endpoint_file import EndpointFile
from .json_line_stream import JsonLineStream


class ControlEndpoint:
    """Accept, authorize and serve; remove only its own descriptor."""

    def __init__(self, layout, config, handshake, router, codec, instance,
                 token, host="127.0.0.1"):
        self._layout = layout
        self._config = config
        self._handshake = handshake
        self._router = router
        self._codec = codec
        self._instance = instance
        self._token = token
        self._host = host
        self._file = EndpointFile(layout.endpoint_path(), config.file_mode,
                                  config.write_retries,
                                  config.write_retry_delay)
        self._socket = None
        self._thread = None
        self._workers = []
        self._connections = set()
        self._lock = threading.Lock()
        self._stopping = threading.Event()

    def start(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self._host, 0))
        listener.listen(self._config.backlog)
        self._socket = listener
        host, port = listener.getsockname()
        self._file.publish({
            "srv": self._codec.SERVER,
            "proto": self._codec.PROTOCOL,
            "host": host,
            "port": port,
            "token": self._token,
            "instance": self._instance,
            "pid": __import__("os").getpid(),
        })
        self._thread = threading.Thread(target=self._accept,
                                        name="control-accept", daemon=True)
        self._thread.start()

    def _accept(self):
        while not self._stopping.is_set():
            try:
                connection, _ = self._socket.accept()
            except OSError:
                break
            except AttributeError:
                break
            worker = threading.Thread(target=self._serve, args=(connection,),
                                      name="control-worker", daemon=True)
            with self._lock:
                self._connections.add(connection)
                self._workers.append(worker)
            worker.start()

    def _serve(self, connection):
        stream = JsonLineStream(connection, self._config.frame_bytes,
                                self._config.read_chunk_bytes)
        transferred = False
        try:
            try:
                hello = self._codec.decode(stream.read())
            except ValueError:
                raise PermissionError(BAD_HANDSHAKE) from None
            greeting = self._handshake.authorize(hello)
            stream.write(self._codec.encode(greeting))
            while not self._stopping.is_set():
                action = self._answer(self._codec.decode(stream.read()),
                                      connection)
                if isinstance(action, ControlAction):
                    transferred = self._apply(stream, action)
                    if transferred:
                        return
                    continue
                stream.write(self._codec.encode(action))
        except PermissionError as exc:
            self._try_write(stream, self._codec.encode(
                error_reply(str(exc))))
        except (ValueError, ConnectionError, OSError):
            pass
        finally:
            stream.close()
            with self._lock:
                self._connections.discard(connection)

    def _apply(self, stream, action):
        """Write the reply, then hand the socket over or keep serving."""
        if action.reply is not None:
            stream.write(self._codec.encode(action.reply))
        transferred = action.upgrade is not None
        if transferred:
            socket_, initial = stream.release()
            with self._lock:
                self._connections.discard(socket_)
            try:
                action.upgrade(socket_, initial)
            except (OSError, ValueError):
                if action.abort is not None:
                    action.abort()
                self._close(socket_)
        if action.after_send is not None:
            action.after_send()
        return transferred

    def _answer(self, request, connection=None):
        try:
            return self._router.dispatch(request, connection)
        except (LookupError, ValueError, KeyError):
            return error_reply(BAD_REQUEST)

    @staticmethod
    def _close(socket_):
        try:
            socket_.close()
        except OSError:
            pass

    def _try_write(self, stream, message):
        try:
            stream.write(message)
        except OSError:
            pass

    def stop(self):
        self._stopping.set()
        listener, self._socket = self._socket, None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(self._config.stop_grace)
        with self._lock:
            connections = list(self._connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        with self._lock:
            workers = list(self._workers)
        for worker in workers:
            worker.join(self._config.stop_grace)
        self._file.remove_owned(self._instance)
