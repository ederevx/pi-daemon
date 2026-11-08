"""The one failure a peer link reports: why it could not carry a request."""


class PeerLinkError(Exception):
    """A link that could not open, or one that is no longer usable."""

    def __init__(self, reason):
        super().__init__(str(reason))
        self.reason = str(reason)
