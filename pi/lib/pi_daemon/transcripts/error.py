class CatalogError(Exception):
    """A conversation question the store answers with a refusal."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code
