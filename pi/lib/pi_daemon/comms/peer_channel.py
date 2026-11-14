"""One agent-bound connection to a peer, for the length of one call.

The delivery worker keeps a connection open for its queue and the tunnel
under it; a call opens its own connection through that tunnel, presents the
calling agent, and closes when the answer arrives.
"""

from .peer_link_error import PeerLinkError

HELD = "comms.wait"
CLOSED = "channel-closed"


class PeerChannel:
    """It owns its connection and the deadline of the call it carries."""

    def __init__(self, config, peer, link, credentials, agent_id, address,
                 owner=None):
        self._config = config
        self._peer = peer
        self._link = link
        self._credentials = credentials
        self._agent_id = agent_id
        self._address = address
        self._owner = owner
        self._methods = ()
        self._limits = {}
        self._closed = False

    def open(self):
        """Authenticate as this agent; a peer that refuses leaves nothing open."""
        try:
            credential = self._credentials.outbound_credential(self._peer)
            greeting = self._link.open(
                self._address, self._peer, credential,
                self._config.comms_peer_request_timeout,
                agent={"agentId": self._agent_id})
        except (PeerLinkError, OSError, ValueError) as failure:
            self.close()
            raise PeerLinkError(str(failure)) from None
        if self._closed:
            # A shutdown that closed this channel while its hello was in
            # flight leaves no connection behind for anyone to use.
            self._link.close()
            raise PeerLinkError(CLOSED)
        self._methods = tuple(greeting.get("methods") or ())
        self._limits = greeting.get("limits") or {}
        return self

    def request(self, method, params, timeout=None):
        """A method the peer did not list in its greeting is not offered."""
        if method not in self._methods:
            raise PeerLinkError("method-unavailable")
        return self._link.request(method, params,
                                  self._deadline(method, params, timeout))

    def interrupt(self):
        """Break a request that is waiting, from another thread."""
        self._link.interrupt()

    def close(self):
        self._closed = True
        self._link.close()
        if self._owner is not None:
            self._owner.forget(self)

    # -- deadlines ---------------------------------------------------------

    def _deadline(self, method, params, timeout):
        """A held wait answers within the limit that node advertises."""
        if timeout is None or method != HELD:
            return timeout
        return max(float(timeout), 0.0) + self._hold(params)

    def _hold(self, params):
        """How long the peer may hold a wait for, in seconds.

        The request is validated by the destination, never here, so a
        negative or absent value costs the deadline nothing.
        """
        limit = self._limits.get("maxWaitMs")
        if isinstance(limit, bool) or not isinstance(limit, (int, float)):
            limit = self._config.comms_wait_max * 1000.0
        limit = max(float(limit), 0.0)
        asked = params.get("timeoutMs")
        if isinstance(asked, bool) or not isinstance(asked, (int, float)):
            asked = limit
        return max(min(float(asked), limit), 0.0) / 1000.0
