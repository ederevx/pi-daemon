"""The one failure a peer tunnel reports: why its ssh child never came up."""


class TunnelError(Exception):
    """A forward that did not bind, with the reason the caller records."""

    def __init__(self, reason):
        super().__init__(str(reason))
        self.reason = str(reason)
