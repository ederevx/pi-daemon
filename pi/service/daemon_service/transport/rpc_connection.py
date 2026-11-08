"""The pi-rpc/1 conversation on one accepted socket."""

from .json_line_stream import JsonLineStream
from .rpc_codec import (INVALID_PARAMS, INVALID_REQUEST, METHOD_NOT_FOUND)
from .rpc_error import RpcError
from .rpc_writer import RpcWriter

HELLO = "rpc.hello"
CANCEL = "rpc.cancel"


class RpcConnection:
    """Hello, then requests until the peer leaves; it owns no domain state.

    `access.bind` authenticates the hello and returns the bound session;
    everything after that belongs to the session.
    """

    def __init__(self, connection, config, codec, access, stopping):
        self._connection = connection
        self._config = config
        self._codec = codec
        self._access = access
        self._stopping = stopping
        self._hello_id = None

    def serve(self):
        stream = JsonLineStream(self._connection, self._config.frame_bytes,
                                self._config.read_chunk_bytes)
        writer = RpcWriter(self._connection, self._codec)
        session = None
        try:
            session = self._greet(stream, writer)
            while not self._stopping.is_set():
                self._dispatch(self._codec.decode(stream.read()), session,
                               writer)
        except RpcError as exc:
            self._try_send(writer, self._codec.failure(self._hello_id,
                                                       exc.code, exc.message))
        except (ValueError, ConnectionError, OSError):
            pass
        finally:
            if session is not None:
                session.close()
            writer.close()
            stream.close()

    def _greet(self, stream, writer):
        try:
            hello = self._codec.decode(stream.read())
        except ValueError:
            raise RpcError(INVALID_REQUEST, "a hello frame is required")
        if hello.get("method") != HELLO or "id" not in hello:
            raise RpcError(INVALID_REQUEST, "a hello request is required")
        self._hello_id = hello["id"]
        session = self._access.bind(hello.get("params"), writer)
        writer.send(self._codec.result(hello.get("id"), session.greeting()))
        return session

    def _dispatch(self, message, session, writer):
        method = message.get("method")
        if not isinstance(method, str) or not method:
            if "id" in message:
                self._try_send(writer, self._codec.failure(
                    message.get("id"), INVALID_REQUEST, "a method is required"))
            return
        if "id" not in message:
            if method == CANCEL:
                session.cancel(message.get("params"))
            return
        try:
            reply = session.handle(message)
        except RpcError as exc:
            self._try_send(writer, self._codec.failure(message["id"], exc.code,
                                                       exc.message))
            return
        except LookupError:
            self._try_send(writer, self._codec.failure(
                message["id"], METHOD_NOT_FOUND, method))
            return
        except ValueError:
            self._try_send(writer, self._codec.failure(
                message["id"], INVALID_PARAMS, "invalid parameters"))
            return
        if reply is not None:
            self._try_send(writer, self._codec.result(message["id"], reply))

    @staticmethod
    def _try_send(writer, message):
        try:
            writer.send(message)
        except OSError:
            pass
