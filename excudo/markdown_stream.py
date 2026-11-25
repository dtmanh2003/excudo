"""Append-only Markdown segments for streamed replies.

Only complete lines are inspected, so token boundaries cannot accidentally
close a fence or a table. finish() drains only the not-yet-emitted tail.
"""
import re

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_LIST = re.compile(r"^ {0,3}(?:[-+*]|\d+[.)])[ \t]+")
_QUOTE = re.compile(r"^(?: {0,3}>[ \t]?)+")
_CELL = re.compile(r":?-{3,}:?")


def _opening_fence(line):
    # Fences nested in a quote/list still protect their internal blank lines.
    content = _QUOTE.sub("", line)
    item = _LIST.match(content)
    offset = item.end() if item else 0
    match = _FENCE.match(content[offset:])
    if match and not (match[1][0] == "`" and "`" in match[2]):
        return match[1][0], len(match[1]), offset + 3
    return None


def _cells(line):
    row = line.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", row)]


class MarkdownStream:
    def __init__(self):
        self._partial = []
        self._lines = []
        self._fence = None
        self._table_columns = 0
        self._indented = False
        self._list = False

    @property
    def pending_text(self):
        """Text not yet emitted as a complete Markdown block."""
        return "".join(self._lines) + "".join(self._partial)

    @property
    def pending_kind(self):
        """The structure being buffered, if its kind is already known."""
        partial = "".join(self._partial)
        if self._fence or self._indented or _opening_fence(partial) or partial.startswith(("    ", "\t")):
            return "code"
        return "table" if self._table_columns else None

    @property
    def waiting_for_structure(self):
        """コード・表は確定前にMarkdown整形せず、本文の途中表示も保留する。"""
        partial = "".join(self._partial)
        return bool(self._fence or self._table_columns or self._indented
                    or _opening_fence(partial) or partial.startswith(("    ", "\t")))

    def _drain(self):
        text = "".join(self._lines)
        self._lines = []
        self._fence = None
        self._table_columns = 0
        self._indented = False
        self._list = False
        return [text] if text.strip() else []

    def _line(self, raw):
        line = raw.rstrip("\r\n")
        blank = not line.strip()
        if self._fence:
            self._lines.append(raw)
            marker, length, indentation = self._fence
            closer = _QUOTE.sub("", line)
            pattern = r" {0," + str(indentation) + "}" + re.escape(marker) + "{" + str(length) + r",}[ \t]*"
            if re.fullmatch(pattern, closer):
                return self._drain()
            return []

        emitted = []
        if self._indented:
            if blank or line.startswith(("    ", "\t")):
                self._lines.append(raw)
                return []
            emitted += self._drain()

        fence = _opening_fence(line)
        if self._table_columns:
            if not blank and not fence and (self._table_columns == 1 or re.search(r"(?<!\\)\|", line)):
                self._lines.append(raw)
                return emitted
            emitted += self._drain()

        # Keep loose list items together, preserving numbering and nesting.
        if self._list and not blank and not (_LIST.match(line) or line.startswith(("  ", "\t"))):
            emitted += self._drain()

        if fence:
            # A fence can interrupt a paragraph even without a preceding blank line.
            if not self._list:
                emitted += self._drain()
            self._lines.append(raw)
            self._fence = fence
            return emitted

        if blank:
            if self._list:
                self._lines.append(raw)
            else:
                emitted += self._drain()
            return emitted

        cells = _cells(line)
        if "|" in line and self._lines and all(_CELL.fullmatch(cell) for cell in cells):
            header = self._lines[-1]
            if header.strip() and len(_cells(header)) == len(cells):
                # Preserve the header even when it immediately follows prose.
                self._lines.pop()
                emitted += self._drain()
                self._lines = [header, raw]
                self._table_columns = len(cells)
                return emitted

        if not self._lines:
            self._indented = line.startswith(("    ", "\t"))
            self._list = bool(_LIST.match(line))
        self._lines.append(raw)
        return emitted

    def feed(self, text):
        emitted = []
        parts = text.split("\n")
        for part in parts[:-1]:
            raw = "".join(self._partial) + part + "\n"
            self._partial.clear()
            emitted += self._line(raw)
        if parts[-1]:
            self._partial.append(parts[-1])
        return emitted

    def finish(self):
        emitted = []
        if self._partial:
            raw = "".join(self._partial)
            self._partial.clear()
            emitted += self._line(raw)
        return emitted + self._drain()
