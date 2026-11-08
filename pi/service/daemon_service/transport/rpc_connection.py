"""The pi-rpc/1 conversation on one accepted socket."""

from .json_line_stream import JsonLineStream
from .rpc_codec import (INTERNAL_ERROR, INVALID_PARAMS, INVALID_REQUEST,
                        METHOD_NOT_FOUND, UNAUTHORIZED)
from .rpc_error import RpcError
from .rpc_writer import RpcWriter

HELLO = "rpc.hello"
CANCEL = "rpc.cancel"
NO_HELLO = "a hello request is required"


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
        writer = RpcWriter(self._connection, self._config, self._codec)
        session = None
        try:
            session = self._greet(stream, writer)
            while not self._stopping.is_set():
                self._dispatch(self._read(stream, writer), session, writer)
        except RpcError as exc:
            request_id = exc.request_id
            if request_id is None:
                request_id = self._hello_id
            self._try_send(writer, self._codec.failure(request_id, exc.code,
                                                       exc.message))
        except (ValueError, ConnectionError, OSError):
            pass
        except Exception:
            self._try_send(writer, self._codec.failure(
                self._hello_id, INTERNAL_ERROR, "the greeting failed"))
        finally:
            if session is not None:
                session.close()
            writer.close()
            stream.close()

    def _read(self, stream, writer):
        """One frame, or a parse refusal that leaves the peer connected."""
        while True:
            try:
                return self._codec.decode(stream.read())
            except RpcError as exc:
                self._try_send(writer, self._codec.failure(None, exc.code,
                                                           exc.message))

    def _greet(self, stream, writer):
        try:
            hello = self._codec.decode(stream.read())
        except RpcError:
            raise RpcError(UNAUTHORIZED, NO_HELLO) from None
        if hello.get("method") != HELLO or \
                self._codec.request_id(hello) is None:
            raise RpcError(UNAUTHORIZED, NO_HELLO,
                           self._codec.request_id(hello))
        self._hello_id = hello["id"]
        session = self._access.bind(hello.get("params"), writer)
        try:
            writer.send(self._codec.result(hello["id"], session.greeting()))
        except (OSError, RpcError):
            session.close()
            raise
        return session

    def _dispatch(self, message, session, writer):
        method = message.get("method")
        if not isinstance(method, str) or not method:
            if "id" in message:
                self._try_send(writer, self._codec.failure(
                    self._codec.request_id(message), INVALID_REQUEST,
                    "a method is required"))
            return
        if "id" not in message:
            if method == CANCEL:
                session.cancel(message.get("params"))
            return
        request_id = self._codec.request_id(message)
        if request_id is None:
            self._try_send(writer, self._codec.failure(
                None, INVALID_REQUEST,
                "a request id must be a string or a number"))
            return
        try:
            reply = session.handle(message)
        except RpcError as exc:
            self._try_send(writer, self._codec.failure(request_id, exc.code,
                                                       exc.message))
            return
        except LookupError:
            self._try_send(writer, self._codec.failure(request_id,
                                                       METHOD_NOT_FOUND,
                                                       method))
            return
        except ValueError:
            self._try_send(writer, self._codec.failure(
                request_id, INVALID_PARAMS, "invalid parameters"))
            return
        except Exception:
            self._try_send(writer, self._codec.failure(
                request_id, INTERNAL_ERROR, "the request failed"))
            return
        if reply is None:
            return
        try:
            writer.send(self._codec.result(request_id, reply))
        except RpcError as exc:
            self._try_send(writer, self._codec.failure(request_id, exc.code,
                                                       exc.message))
        except OSError:
            pass

    @staticmethod
    def _try_send(writer, message):
        try:
            writer.send(message)
        except (OSError, RpcError):
            pass
