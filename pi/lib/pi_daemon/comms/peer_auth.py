"""Peer credentials: one directional secret each way, never the daemon token.

The receiving side stores a SHA-256 verifier per source node. The sending
side stores only a path to a protected file. No credential is ever copied
into peer state, an answer, or a log line.
"""

import hashlib
import hmac
import os

import pi_platform

CREDENTIAL_BYTES = 4096


class PeerAuthenticator:
    """It compares verifiers; it does not decide which methods exist."""

    def __init__(self, config, peers, reader=None, directory=None):
        self._max_bytes = min(CREDENTIAL_BYTES, config.frame_bytes)
        self._peers = peers
        self._directory = directory
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
        if self._directory is not None and \
                os.path.dirname(path) != self._directory:
            # Only the daemon's own credentials directory: a path an agent
            # could point anywhere would make the token or a key sendable.
            raise ValueError("the credential is not in its directory")
        # Who may read this file is the platform's question: POSIX answers
        # with owner and mode bits, Windows with the access control list.
        problem, checked = pi_platform.private_file_problem(path)
        if problem is not None:
            raise ValueError(problem)
        try:
            descriptor = os.open(path,
                                 os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as failure:
            raise ValueError(f"the credential is unreadable: {failure}") \
                from None
        try:
            info = os.fstat(descriptor)
            if (info.st_dev, info.st_ino) != \
                    (checked.st_dev, checked.st_ino):
                # The path was swapped between the check and the open: the
                # descriptor no longer names the file that was cleared.
                raise ValueError("the credential changed while it was read")
            if info.st_size <= 0 or info.st_size > self._max_bytes:
                raise ValueError("the credential file is out of bounds")
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                descriptor = None
                return handle.read().strip()
        finally:
            if descriptor is not None:
                os.close(descriptor)
