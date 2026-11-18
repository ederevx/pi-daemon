"""PTY output as text a reader can use: escapes dropped, tails held.

A terminal's byte stream draws as well as prints, and a model reads only
what prints. Nothing here keeps a screen: the reader is handed the text one
chunk yielded, plus how many bytes it consumed, so a sequence or a UTF-8
character split across two chunks is finished by the next read rather than
torn.
"""

BEL = 0x07
ESC = 0x1B
ST = 0x5C
# A CSI ends at its final byte; CUP and HVP move the cursor, which a
# line-oriented reader sees as a line break.
CSI_CURSOR = (0x48, 0x66)                 # 'H', 'f'
CPI = (0x28, 0x29, 0x2A, 0x2B)            # charset designators
STRING_INTRODUCERS = (0x50, 0x58, 0x5E, 0x5F)   # DCS, SOS, PM, APC
CSI_OPEN = 0x5B
OSC_OPEN = 0x5D


class TerminalText:
    """It renders one run's bytes as lines and leaves an unfinished tail."""

    def render(self, data):
        """Return `(text, consumed)`; an incomplete tail stays unconsumed."""
        data = bytes(data)
        out = bytearray()
        # (input offset, output length) after every token, so a split
        # character can be given back to exactly its first byte.
        marks = []
        index = 0
        while index < len(data):
            byte = data[index]
            if byte == ESC:
                step = self._escape(data, index)
                if step is None:
                    break
                length, newline = step
                if newline:
                    out += b"\n"
                index += length
            elif byte == 0x0D:
                index += 2 if data[index + 1:index + 2] == b"\n" else 1
                out += b"\n"
            elif byte in (0x0A, 0x09):
                out += data[index:index + 1]
                index += 1
            elif byte < 0x20 or byte == 0x7F:
                index += 1
            else:
                out += data[index:index + 1]
                index += 1
            marks.append((index, len(out)))
        cut = _complete_prefix(out)
        consumed = index if cut == len(out) else _consumed(marks, cut)
        return _tidy(out[:cut].decode("utf-8", "replace")), consumed

    @staticmethod
    def _escape(data, index):
        """`(length, newline)` for the escape at `index`, or None if torn."""
        if index + 1 >= len(data):
            return None
        kind = data[index + 1]
        if kind == CSI_OPEN:
            position = index + 2
            while position < len(data):
                byte = data[position]
                if 0x40 <= byte <= 0x7E:
                    return position - index + 1, byte in CSI_CURSOR
                position += 1
            return None
        if kind == OSC_OPEN or kind in STRING_INTRODUCERS:
            return _string_end(data, index + 2, index)
        if kind in CPI or kind == 0x23:       # '(x', '#x' are three bytes
            return (3, False) if index + 2 < len(data) else None
        return 2, False


def _string_end(data, position, index):
    """A string sequence ends at BEL or an escaped `\\`; torn is None."""
    while position < len(data):
        byte = data[position]
        if byte == BEL:
            return position - index + 1, False
        if byte == ESC:
            if position + 1 >= len(data):
                return None
            if data[position + 1] == ST:
                return position - index + 2, False
        position += 1
    return None


def _complete_prefix(data):
    """How much of `data` decodes without ending inside a character."""
    cut = len(data)
    start = cut
    while start > 0 and cut - start < 3 and 0x80 <= data[start - 1] <= 0xBF:
        start -= 1
    if start == 0:
        return cut
    lead = data[start - 1]
    if lead < 0xC0:
        return cut
    needed = 1 if lead < 0xE0 else (2 if lead < 0xF0 else 3)
    return start - 1 if cut - (start - 1) <= needed else cut


def _consumed(marks, cut):
    """The input offset that produced output byte `cut`."""
    offset = length = 0
    for offset, length in marks:
        if length >= cut:
            break
    return offset - (length - cut)


def _tidy(text):
    """Trailing blanks and runs of blank lines carry nothing a reader uses."""
    if not text:
        return ""
    tidied = "\n".join(line.rstrip(" ") for line in text.split("\n"))
    while "\n\n\n" in tidied:
        tidied = tidied.replace("\n\n\n", "\n\n")
    return tidied
