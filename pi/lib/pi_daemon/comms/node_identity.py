"""This node's durable name, minted once and never invented again."""

import json
import re
import secrets

from ..atomic_state_file import AtomicStateFile
from .error import CommsError

SCHEMA = 1
STORE_CORRUPT = "store-corrupt"
NODE_ID = re.compile(r"[0-9a-f]{32}")


class NodeIdentity:
    """Peers address this service by the name this file holds."""

    def __init__(self, path, config):
        self._file = AtomicStateFile(path, config.file_mode,
                                     config.write_retries,
                                     config.write_retry_delay)
        self._node_id = ""

    def load(self):
        """Mint on a missing file; refuse anything else that is unreadable."""
        text = self._file.read()
        if text is None:
            self._node_id = secrets.token_hex(16)
            self._file.write_dict({"version": SCHEMA,
                                   "nodeId": self._node_id})
            return self
        try:
            data = json.loads(text)
        except ValueError:
            raise CommsError(STORE_CORRUPT) from None
        if not isinstance(data, dict) or data.get("version") != SCHEMA:
            raise CommsError(STORE_CORRUPT)
        node_id = data.get("nodeId")
        if not isinstance(node_id, str) or not NODE_ID.fullmatch(node_id):
            raise CommsError(STORE_CORRUPT)
        self._node_id = node_id
        return self

    def id(self):
        return self._node_id
