"""A handler's answer when the connection itself changes hands."""


class ControlAction:
    """A reply plus what happens to the connection after it is written.

    `on_close` runs once when the connection closes for any reason, after
    the reply is out. A handing-over client parks by simply holding its
    socket, so the reply is written, the ordinary read loop keeps serving
    it, and the close itself - never a pid probe - is the signal.
    """

    def __init__(self, reply=None, upgrade=None, after_send=None, abort=None,
                 on_close=None):
        self.reply = reply
        self.upgrade = upgrade
        self.after_send = after_send
        self.abort = abort
        self.on_close = on_close
