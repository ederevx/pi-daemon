"""The one failure type the tasks domain raises: a published error name."""


class TaskError(Exception):
    """A published failure name, raised where the domain decides it."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code
