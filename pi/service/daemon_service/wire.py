"""Wire shapes: one JSON object per line, plus the answers every client
can rely on.

A response carries `ok`. A failure carries `error`, a short reason the
client may branch on, and nothing else.
"""

import json

MAX_LINE = 1 << 20


def encode(message):
    return (json.dumps(message, separators=(",", ":")) + "\n").encode()


def decode(line):
    """Decode one request line, or None when it is not an object."""
    try:
        message = json.loads(line)
    except ValueError:
        return None
    return message if isinstance(message, dict) else None


def ok(**fields):
    fields["ok"] = True
    return fields


def fail(error):
    return {"ok": False, "error": error}
