"""回答表示用Markdown。表の長いセルは省略せず折り返す。"""
from rich import box
from rich.markdown import Markdown, TableElement
from rich.table import Table


class _WrappingTable(TableElement):
    def __rich_console__(self, console, options):
        table = Table(box=box.SIMPLE, pad_edge=False, style="markdown.table.border",
                      show_edge=True, collapse_padding=True)
        if self.header is not None and self.header.row is not None:
            for column in self.header.row.cells:
                heading = column.content.copy()
                heading.stylize("markdown.table.header")
                table.add_column(heading, overflow="fold", no_wrap=False)
        if self.body is not None:
            for row in self.body.rows:
                cells = []
                for element in row.cells:
                    cell = element.content.copy()
                    cell.overflow = "fold"
                    cell.no_wrap = False
                    cells.append(cell)
                table.add_row(*cells)
        yield table


class ReplyMarkdown(Markdown):
    elements = {**Markdown.elements, "table_open": _WrappingTable}

    @property
    def has_table(self):
        return any(token.type == "table_open" for token in self.parsed)
