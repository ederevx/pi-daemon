"""Peer credentials: one directional secret each way, never the daemon token.

The receiving side stores a SHA-256 verifier per source node. The sending
side stores only a path to a protected file. No credential is ever copied
into peer state, an answer, or a log line.
"""

import hashlib
import hmac
import os
import stat

CREDENTIAL_BYTES = 4096


class PeerAuthenticator:
    """It compares verifiers; it does not decide which methods exist."""

    def __init__(self, config, peers, reader=None):
        self._max_bytes = min(CREDENTIAL_BYTES, config.frame_bytes)
        self._peers = peers
        self._reader = reader or self._read

    def verify(self, node_id, credential):
        """True only for the verifier configured for this source node."""
        record = self._peers.get(node_id)
        if record is None or not isinstance(credential, str) or \
                not credential:
            return False
        if record["state"] != "active":
            return False
        digest = hashlib.sha256(credential.encode("utf-8")).hexdigest()
        return hmac.compare_digest(digest, record["inboundCredentialSha256"])

    def outbound_credential(self, peer):
        """Read the caller's own credential file, bounded and protected."""
        try:
            credential = self._reader(peer["outboundCredentialFile"])
        except (OSError, ValueError):
            raise ValueError("credential-unreadable") from None
        if not credential:
            raise ValueError("credential-unreadable")
        return credential

    def _read(self, path):
        if not isinstance(path, str) or not os.path.isabs(path):
            raise ValueError("an absolute credential path is required")
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("a regular credential file is required")
        if info.st_size <= 0 or info.st_size > self._max_bytes:
            raise ValueError("the credential file is out of bounds")
        if info.st_mode & 0o077:
            raise ValueError("the credential file is readable by others")
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip()
