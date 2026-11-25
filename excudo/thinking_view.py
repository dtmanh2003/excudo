"""直前の依頼の思考を画面表示のためだけに保持する。履歴・保存には使用しない。"""
import random
import re
import time
import unicodedata

from rich.cells import get_character_cell_size

from .security import redact_secrets
from .tools import redact
from .waiting_stories import WAITING_STORIES as _WAIT_STORIES, WaitingStory


def display_text(text: str) -> str:
    """Markdownや端末制御として解釈せず、秘密情報を伏せて表示する。"""
    text = redact_secrets(redact(text))
    text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", text)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", text).expandtabs(4)


# 物語の選択履歴はメモリ上の番号だけ。原文・会話履歴・セッションには追加しない。
_previous_wait_story = None


def _choose_wait_story() -> WaitingStory:
    """1依頼に1組を選び、直前と同じ小話は避ける。原文や保存先には触れない。"""
    global _previous_wait_story
    candidates = [index for index in range(len(_WAIT_STORIES)) if index != _previous_wait_story]
    selected = random.choice(candidates)
    _previous_wait_story = selected
    return _WAIT_STORIES[selected]


class ThinkingTrace:
    def __init__(self, clock=None):
        self._clock = clock or time.monotonic
        self.clear()

    def clear(self) -> None:
        self._chunks = []
        self._characters = 0
        self._tail = ""
        self._latest = ""
        self._phase_started = None
        self._first_received = None
        self._seconds = 0.0
        self._incomplete = False
        self._wait_story = None
        self._wait_note_shown = False

    def append(self, text: str) -> None:
        if not text:
            return
        if self._wait_story is None:
            self._wait_story = _choose_wait_story()
        if self._phase_started is None:
            self._phase_started = self._clock()
        if self._first_received is None:
            self._first_received = self._phase_started
        self._chunks.append(text)
        self._characters += len(text)
        lines = (self._tail + text).split("\n")
        self._tail = lines[-1]
        for line in reversed(lines):
            if line.strip():
                self._latest = line
                break

    def finish_phase(self) -> None:
        if self._phase_started is not None:
            self._seconds += max(0.0, self._clock() - self._phase_started)
            self._phase_started = None

    def mark_incomplete(self) -> None:
        self.finish_phase()
        self._incomplete = True

    @property
    def characters(self) -> int:
        return self._characters

    @property
    def seconds(self) -> float:
        started = self._phase_started
        active = max(0.0, self._clock() - started) if started is not None else 0.0
        return self._seconds + active

    @property
    def preview_ready(self) -> bool:
        """受信開始から10秒以上、かつ500文字以上の場合だけ原文をプレビューする。"""
        started = self._first_received
        return started is not None and self._characters >= 500 and self._clock() - started >= 10

    @property
    def text(self) -> str:
        return "".join(self._chunks)

    @property
    def latest_line(self) -> str:
        return display_text(self._latest).replace("\n", " ")

    def preview_tail(self, width: int, *, cjk_console: bool = False) -> str:
        """端末の表示幅に収まる末尾を返す。保持した原文には手を加えない。"""
        if width <= 0:
            return ""
        text = self.latest_line
        def cells(character):
            size = get_character_cell_size(character)
            if cjk_console and size == 1 and unicodedata.east_asian_width(character) == "A":
                return 2
            return size
        sizes = [cells(character) for character in text]
        if sum(sizes) <= width:
            return text
        prefix = "..." if width >= 5 else ""
        available = width - len(prefix)
        used = 0
        start = len(text)
        for index in range(len(text) - 1, -1, -1):
            if used + sizes[index] > available:
                break
            used += sizes[index]
            start = index
        # 結合文字だけが左端に残る場合は、元の基底文字の続きとして表示しない。
        while start < len(text) and unicodedata.combining(text[start]):
            start += 1
        return prefix + text[start:]

    def wait_note(self) -> str | None:
        """30秒ごとに物語を進め、次の更新まで残す。新しいタイマーは作らない。"""
        if self._phase_started is None or self._incomplete:
            return None
        seconds = self.seconds
        interval = int(seconds // 30)
        if interval < 1:
            return None
        story = self._wait_story
        if story is None:
            return None
        self._wait_note_shown = True
        index = interval - 1
        if index < len(story.steps):
            return story.steps[index]
        return story.continuation[(index - len(story.steps)) % len(story.continuation)]

    def receiving_note(self) -> str | None:
        """待機の小話を表示した依頼だけ、同じ物語の締め台詞を返す。"""
        if self._wait_note_shown and not self._incomplete and self._wait_story is not None:
            return self._wait_story.receiving or None
        return None

    def caption(self) -> str:
        state = "途中まで・" if self._incomplete else ""
        return f"思考のメモ（{state}{self.seconds:.1f}秒・{self.characters:,}文字）"

    def summary(self) -> str:
        return f"▸ {self.caption()}  /think で表示"
