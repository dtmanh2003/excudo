"""確定済みの履歴の下で、生成中の本文と状態を別々の行へ表示する。"""
import threading
import io
import os
import unicodedata
from collections import deque

from rich.console import Console
from .markdown_render import ReplyMarkdown as Markdown
from rich.padding import Padding
from rich.text import Text
from rich.cells import get_character_cell_size
from excudo.console_viewport import at_live_end


def fit_status_line(text: Text, width: int, *, cjk_console: bool) -> Text:
    """CJK版CMDでは曖昧幅の記号を保守的に2列と数え、右端の折り返しを防ぐ。"""
    def cells(character):
        size = get_character_cell_size(character)
        if cjk_console and size == 1 and unicodedata.east_asian_width(character) == "A":
            return 2
        return size
    sizes = [cells(character) for character in text.plain]
    if sum(sizes) <= width:
        return text
    # 省略記号「…」自身もCMDでは2列になるため、末尾は幅が一定のASCIIを使う。
    suffix = "..." if width >= 3 else ""
    available = max(0, width - len(suffix))
    used = end = 0
    for size in sizes:
        if used + size > available:
            break
        used += size
        end += 1
    result = text[:end]
    result.append(suffix)
    return result


def wrap_note_lines(text: Text, width: int, *, cjk_console: bool) -> list[Text]:
    """小話を端末の実際の列数に合わせて折り返す。通常幅では文字を省略しない。"""
    rows = []
    start = used = 0
    for index, character in enumerate(text.plain):
        size = get_character_cell_size(character)
        if cjk_console and size == 1 and unicodedata.east_asian_width(character) == "A":
            size = 2
        if used + size > width and index > start:
            rows.append(text[start:index])
            start, used = index, 0
        if size > width:
            # 1列の端末には全角文字を置けないため、領域外へ折り返さず代替する。
            rows.append(Text("?", style=text.style))
            start, used = index + 1, 0
        else:
            used += size
    if start < len(text):
        rows.append(text[start:])
    return rows


class ReplyStatus:
    """本文領域の先頭へ常に戻り、前回の行数に依存せず一時表示を更新する。"""

    def __init__(self, console, render, pending, *, preview=None, note=None):
        self.console = console
        self.render = render
        self.pending = pending
        self.preview = preview
        self.note = note
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.paused = False
        self.thread = None
        self.deferred = deque()
        self._body_key = None
        self._body_rows = []
        self._painted_rows = []
        self._painted_width = None
        self._painted_height = console.size.height
        if console.is_terminal:
            self.refresh()
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def _run(self):
        while not self.stopped.wait(0.05):
            try:
                self.refresh()
            except (OSError, UnicodeError):
                self.stopped.set()

    def _at_live_end(self):
        # 縦縮小直後は、古い表示の末尾が画面外でもスクロールとは限らない。
        # 先頭が見えていれば再描画し、新しい高さで領域を作り直す。
        # 履歴へスクロールして先頭も画面外なら、従来どおり保留する。
        rows = 0 if self.console.size.height < self._painted_height else len(self._body_rows)
        return at_live_end(self.console, extra_rows=rows)

    def refresh(self):
        with self.lock:
            # 承認の確認中は何も出力しない。確定分は resume() 後にまとめて出す。
            if self.paused:
                return
            # 履歴を読んでいる間は、状態バーの更新も含めて一切出力しない。
            if not self._at_live_end():
                return
            self._drain()
            if not self.console.is_terminal or self.stopped.is_set():
                return
            width = self.console.width
            try:
                width = os.get_terminal_size(self.console.file.fileno()).columns
            except (AttributeError, OSError, ValueError):
                pass
            width = max(1, width - 2)
            frame = self.render().copy()
            frame = fit_status_line(frame, width, cjk_console=os.name == "nt")
            output = io.StringIO()
            renderer = Console(file=output, width=width, height=1, force_terminal=True,
                               color_system=self.console.color_system, legacy_windows=False)
            pending = self.pending()
            available = max(0, self.console.size.height - 2)
            preview = self.preview() if self.preview is not None else None
            note = self.note() if self.note is not None else None
            key = (pending, preview.plain if preview is not None else None,
                   note.plain if note is not None else None, width, available)
            if key != self._body_key:
                rows = []
                note_rows = wrap_note_lines(note, width, cjk_console=os.name == "nt") if note is not None else []
                # 状態1行と画面下端の余裕を確保し、残りを小話と本文で共有する。
                note_limit = max(0, available - (1 if preview is not None and available > 1 else 0))
                if len(note_rows) > note_limit:
                    note_rows = note_rows[:note_limit]
                    if note_rows:
                        note_rows[-1] = fit_status_line(
                            Text(note_rows[-1].plain + "...", style=note.style), width,
                            cjk_console=os.name == "nt")
                limit = min(16, available - len(note_rows))
                if preview is not None and limit:
                    rows.append(fit_status_line(preview, width, cjk_console=os.name == "nt"))
                elif pending.strip() and limit:
                    margin = 2 if width >= 20 else 0
                    body = Padding(Markdown(pending, code_theme="monokai", hyperlinks=False,
                                            justify="left"), (0, margin), expand=False)
                    lines = renderer.render_lines(body, renderer.options.update(width=min(width, 104)), pad=False)
                    for line in lines[-limit:]:
                        row = Text.assemble(*[(segment.text, segment.style or "") for segment in line if not segment.control])
                        # Richのリスト等が付ける行末パディングは文字として描画しない。
                        row.rstrip()
                        rows.append(fit_status_line(row, width, cjk_console=os.name == "nt"))
                rows.extend(note_rows)
                self._body_key, self._body_rows = key, rows
            # 本文は状態とは別の行に描く。段落が確定したらこの領域を消して、
            # 同じ場所へMarkdownを一度だけ履歴として出力する。
            rendered_rows = []
            for row in [*self._body_rows, frame]:
                output.seek(0)
                output.truncate()
                renderer.print(row, end="", soft_wrap=True)
                rendered_rows.append(output.getvalue())
            if (rendered_rows == self._painted_rows and width == self._painted_width
                    and self.console.size.height == self._painted_height):
                return
            previous = self._painted_rows if width == self._painted_width else []
            # 描画中のカーソル移動が文字の点滅に見えないよう、入力再開まで隠す。
            parts = ["\r\x1b[?25l"]
            for index, row in enumerate(rendered_rows):
                # 先に全消去するとCMDで空白のフレームが見える。変わった行だけ
                # 上書きし、短くなった行の末尾のみ消す。本文が同じなら触らない。
                if index == len(rendered_rows) - 1 or index >= len(previous) or previous[index] != row:
                    parts.extend((row, "\x1b[K"))
                if index < len(rendered_rows) - 1:
                    parts.append("\r\n")
            parts.append("\x1b[0J")
            # Liveのように次回「前回の高さ」を使って戻らない。
            # 今回の描画と同じwriteで先頭に復帰し、リサイズ後も先頭から消去する。
            back = f"\x1b[{len(self._body_rows)}A" if self._body_rows else ""
            self.console.file.write("".join(parts) + "\r" + back + "\r")
            self.console.file.flush()
            self._painted_rows, self._painted_width = rendered_rows, width
            self._painted_height = self.console.size.height

    def pause(self):
        # 承認の確認を出す前に、確定分を出して状態行を消す。入力行を上書きしないため。
        with self.lock:
            self._drain()
            self._clear()
            self.console.file.flush()
            self.paused = True

    def resume(self):
        with self.lock:
            self.paused = False
        self.refresh()

    def _clear(self):
        self._painted_rows = []
        self._painted_width = None
        # 古い行数と描画キーをリセットし、出力保留（フリーズ）防止と再開時の確実な再描画を担保
        self._body_rows = []
        self._body_key = None
        if self.console.is_terminal:
            self.console.file.write("\r\x1b[0J")

    def print(self, content=None):
        with self.lock:
            self.deferred.append(content)
            if not self._at_live_end():
                return
            # 古い途中表示を先に更新し、確定部分が履歴と重なったまま残るのを防ぐ。
            self.refresh()

    def _drain(self):
        if self.deferred:
            self._clear()
        while self.deferred:
            content = self.deferred.popleft()
            self.console.print() if content is None else self.console.print(content)

    def stop(self):
        # 次の入力プロンプトもスクロールを戻すので、読み終えるまで待つ。
        # ロックを保持せず待機し、表示更新スレッドとの競合を防ぐ。
        try:
            while not self._at_live_end():
                self.stopped.wait(0.05)
        finally:
            self.stopped.set()
            if self.thread:
                self.thread.join(timeout=1)
            with self.lock:
                self._drain()
                self._clear()
                if self.console.is_terminal:
                    self.console.file.write("\x1b[?25h")
                self.console.file.flush()
