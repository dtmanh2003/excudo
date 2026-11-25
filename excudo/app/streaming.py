"""Desktop events using the same complete-block boundaries as the CLI."""
import re

from ..markdown_stream import MarkdownStream


class AnswerStream:
    def __init__(self):
        self._markdown = MarkdownStream()
        self._active = False

    def feed(self, text):
        self._active = True
        blocks = self._markdown.feed(text)
        pending = self._markdown.pending_text
        # A potential table header or partial fence must not flash as prose.
        uncertain = any('|' in line or re.match(r'^\s*[`~]', line)
                        for line in pending.splitlines())
        preview = '' if self._markdown.waiting_for_structure or uncertain else pending
        event = {'type': 'answer_update', 'blocks': blocks, 'preview': preview}
        if pending and not preview:
            kind = self._markdown.pending_kind
            if kind == 'code':
                event['code_preview'] = pending
            elif kind == 'table':
                event['table_preview'] = pending
            if kind is None and pending.lstrip().startswith('|'):
                kind = 'table'
            lines = pending.splitlines()
            count = len(lines)
            if kind == 'table':
                count = max(0, count - 2)  # Header and separator are not data rows.
            elif kind == 'code' and re.match(r'^ {0,3}(?:`{3,}|~{3,})', lines[0]):
                count -= 1  # The opening fence is not a line of code.
            event['waiting'] = {'kind': kind or 'answer', 'lines': count}
        return event

    def finish(self):
        if not self._active:
            return None
        self._active = False
        return {'type': 'answer_update', 'blocks': self._markdown.finish(), 'preview': ''}
