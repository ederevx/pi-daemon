"""One failure type for the comms domain, carrying a wire code."""


class CommsError(Exception):
    """A domain failure the operations layer projects as an error reply."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code
