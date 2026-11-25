"""既存の入力編集を保ち、上下線と説明付き候補だけを配置する。"""
import asyncio
import sys

from prompt_toolkit.filters import Condition
from prompt_toolkit.layout.containers import ConditionalContainer, FloatContainer, HSplit, Window, VerticalAlign
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.processors import Processor, Transformation


class _WindowsImeRepaint(Processor):
    """IME確定後の表示ずれを、文字列やカーソルを変えずに描き直す。"""

    def __init__(self, session):
        self.session = session
        self.phase = 0
        self.pending = []
        session.default_buffer.on_text_changed += self.text_changed
        session.app.on_reset += self.cancel
        session.app.after_render += self.after_render

    def cancel(self, _=None):
        for handle in self.pending:
            handle.cancel()
        self.pending.clear()

    def after_render(self, app):
        if app.is_done:
            self.cancel()

    def text_changed(self, buffer):
        self.cancel()
        if not self.session.app.is_running or buffer.text.isascii():
            return
        # IMEが確定文字を描画後に消す場合に備え、入力が落ち着いてから補修する。
        # 常時再描画は変換候補やスクロールを妨げるため、各編集後の2回だけにする。
        loop = asyncio.get_running_loop()
        self.pending = [loop.call_later(delay, self.repaint) for delay in (0.15, 0.4)]

    def repaint(self):
        app = self.session.app
        if app.is_running and not app.is_done:
            self.phase ^= 1
            app.invalidate()

    def apply_transformation(self, transformation_input):
        # invalidateだけでは同じ文字は差分描画で省略される。色を変えないクラスを
        # 交互に付け、入力文字だけを再出力する。全消去やRendererのresetはしない。
        style = f" class:ime-repaint-{self.phase}"
        return Transformation([(fragment[0] + style, *fragment[1:])
                               for fragment in transformation_input.fragments])


def menu_page(session):
    state = session.default_buffer.complete_state
    if not state:
        session._menu_scroll = (None, 0)
        return 0, 0, 0

    # 低い画面でも入力欄を残す。通常は候補8行＋操作案内1行。
    capacity = max(1, min(8, session.app.output.get_size().rows - 5))
    total = len(state.completions)
    selected = state.complete_index if state.complete_index is not None else 0

    # 前回の補完状態とスクロール開始位置を取得
    last_state, last_start = getattr(session, "_menu_scroll", (None, 0))
    if last_state is not state:
        last_start = 0

    # 選択位置に合わせてスクロール枠を追従
    if total <= capacity:
        start = 0
    elif selected < last_start:
        # 上へはみ出た場合: 選択項目が枠の一番上に来るようスクロール
        start = selected
    elif selected >= last_start + capacity:
        # 下へはみ出た場合: 選択項目が枠の一番下に来るようスクロール
        start = selected - capacity + 1
    else:
        # 枠内に収まっている場合: 枠は動かさずカーソルのみ移動
        start = last_start

    # 有効なインデックス範囲内にクランプ
    start = max(0, min(start, max(0, total - capacity)))
    session._menu_scroll = (state, start)

    return start, min(total, start + capacity), selected


def command_rows(session):
    state = session.default_buffer.complete_state
    if not state:
        return []
    start, end, selected = menu_page(session)
    rows = []
    for index in range(start, end):
        completion = state.completions[index]
        style = "class:command.current" if index == selected else "class:command"
        marker = "›" if index == selected else " "
        rows.append((style, f" {marker} {completion.text:<24} {completion.display_meta_text}"))
        rows.append(("", "\n"))
    rows.append(("class:command", f"   {selected + 1}/{len(state.completions)} 件  ·  ↑↓ 選択  ·  Enter/Tab 確定  ·  Esc 閉じる"))
    return rows


def _clear_completion_tail(app):
    """Clear screen-only residue to the right of a selected slash completion."""
    if app.is_done:
        return
    buffer = app.current_buffer
    state = buffer.complete_state
    # The renderer has restored the input cursor here. Never erase a real suffix
    # or repaint the whole screen; completion selection itself owns the text.
    if (state is not None and state.current_completion is not None
            and buffer.text.startswith("/") and buffer.document.is_cursor_at_the_end):
        app.output.erase_end_of_line()
        app.output.flush()


class _CompletionMenuCleanup:
    """Clear the retired menu area below the input rule, preserving the prompt."""

    def __init__(self, session, bottom_rule):
        self.session = session
        self.bottom_rule = bottom_rule
        self.was_visible = False
        session.app.on_reset += self.reset
        session.app.after_render += self.after_render

    def reset(self, _=None):
        self.was_visible = False

    def after_render(self, app):
        visible = bool(self.session.default_buffer.complete_state) and not app.is_done
        closed = self.was_visible and not visible
        self.was_visible = visible
        if not closed or app.is_done:
            return
        screen = app.renderer.last_rendered_screen
        if screen is None:
            return
        rule = screen.visible_windows_to_write_positions.get(self.bottom_rule)
        if rule is None:
            return
        cursor = screen.get_cursor_position(app.layout.current_window)
        first_unused_row = rule.ypos + rule.height
        distance = first_unused_row - cursor.y
        if distance <= 0 or first_unused_row >= app.output.get_size().rows:
            return
        # Only the menu/unused area is below this rule. Avoid newlines (scrolling)
        # and return to the same cursor position without resetting the renderer.
        output = app.output
        output.cursor_backward(cursor.x)
        output.cursor_down(distance)
        output.erase_down()
        output.cursor_up(distance)
        output.cursor_forward(cursor.x)
        output.flush()


def style_input_layout(session):
    session.app.after_render += _clear_completion_tail
    if sys.platform == "win32":
        session.input_processors = [*(session.input_processors or []), _WindowsImeRepaint(session)]
    # PromptSessionの入力・履歴検索・キー操作はそのまま使う。
    # 浮動メニューのみ外し、入力欄の下へ候補を配置して履歴と重ならないようにする。
    for container in list(session.layout.walk()):
        if isinstance(container, FloatContainer):
            container.floats.clear()
    original = session.layout.container
    def input_height():
        size = session.app.output.get_size()
        # Rendererは一度使った高さを保持する。余りの高さを入力領域へ配分せず、
        # 今の文章（端末での折り返しを含む）と検索欄に必要な高さだけを使う。
        page_start, page_end, _ = menu_page(session)
        menu_height = page_end - page_start + 1 if session.default_buffer.complete_state and not session.app.is_done else 0
        available = max(1, size.rows - 2 - menu_height)
        preferred = original.preferred_height(size.columns, available).preferred
        # 長文は入力Window内をスクロールさせる。枠と候補の領域も画面内に残す。
        return Dimension.exact(max(1, min(preferred, available)))

    compact_input = HSplit([original], height=input_height)
    visible = Condition(lambda: bool(session.default_buffer.complete_state) and not session.app.is_done)
    menu = Window(FormattedTextControl(lambda: command_rows(session)),
                  # 高さを明示し、入力Windowへ空間を取られて2行に縮むのを防ぐ。
                  height=lambda: Dimension.exact(menu_page(session)[1] - menu_page(session)[0] + 1),
                  dont_extend_height=True, wrap_lines=False)
    bottom_rule = Window(height=1, char="─", style="class:input-rule")
    session.layout.container = HSplit([
        Window(height=1, char="─", style="class:input-rule"),
        compact_input,
        bottom_rule,
        ConditionalContainer(menu, filter=visible),
    ], align=VerticalAlign.TOP)
    _CompletionMenuCleanup(session, bottom_rule)
