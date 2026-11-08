"""A handler's answer when the connection itself changes hands."""


class ControlAction:
    """A reply plus what happens to the connection after it is written."""

    def __init__(self, reply=None, upgrade=None, after_send=None, abort=None):
        self.reply = reply
        self.upgrade = upgrade
        self.after_send = after_send
        self.abort = abort
