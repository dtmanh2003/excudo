import sys
import time
import json
import threading
import re
import os
import queue
import difflib
from pathlib import Path
from typing import Callable
from dataclasses import dataclass
import rich.markup

from rich.console import Console, Group
from rich.text import Text
from rich.table import Table
from rich.rule import Rule
from excudo.markdown_render import ReplyMarkdown as Markdown
from rich.padding import Padding
from rich.constrain import Constrain
from rich.panel import Panel
from rich.align import Align
from excudo.input_layout import style_input_layout
from excudo.input_suggest import CommandAutoSuggest
from prompt_toolkit import PromptSession
from prompt_toolkit.application.current import get_app
from prompt_toolkit.filters import Condition
from prompt_toolkit.styles import Style
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.key_binding import KeyBindings

from excudo import config
from excudo.approval import restore_approval
from excudo.i18n import set_language, t
from excudo.attachments import Attachments, SUFFIXES, split_paths
from excudo.harness import Harness
from excudo.model import Model
from excudo.permissions import ActionRisk, PermissionGate, outbound_findings, patch_header_path
from excudo.security import redact_secrets
from excudo.prompts import build_system_prompt
from excudo.session import Session, save_session
from excudo.tools import REGISTRY, redact, _HUNK_HEADER, _PATCH_INFO_PREFIXES
from excudo.markdown_stream import MarkdownStream
from excudo.stream_preview import ReplyStatus
from excudo.thinking_view import ThinkingTrace, display_text


class SlashCompleter(Completer):
    """/で始まる入力だけ候補を出す。通常の会話には補完を出さない。"""

    def get_completions(self, document, complete_event):
        prefix = document.text_before_cursor
        if not prefix.startswith("/"):
            return
        if any(ch.isspace() for ch in prefix) and not prefix.lower().startswith("/fast "):
            return
        commands = {
            "/help": "コマンドとツールの一覧（/haでも表示）",
            "/tools": "ツール一覧",
            "/new": "新しい会話",
            "/think": "直前の思考を表示（保存しません）",
            "/fast": "次の依頼から思考なし（/fast off で戻す）",
            "/fast off": "高速モードをオフにして通常設定へ戻す",
            "/attach": "画像ファイルを添付",
            "/paste": "クリップボードの画像を添付",
            "/attachments": "送信前の添付画像を確認",
            "/detach": "番号で添付解除（*で全解除）",
            "/receipts": "領収書フォルダーを読み取る（作業フォルダー外は確認）",
            "/excel": "経費申請Excelを指定のフォルダーに保存（作業フォルダー外は確認）",
            "/exit": "終了",
            **{f"/{name}": REGISTRY[name].ui.get("title", "ツールの説明を表示") for name in REGISTRY},
        }
        for command, description in commands.items():
            if command.startswith(prefix.lower()) or (command == "/help" and prefix.lower() == "/ha"):
                yield Completion(command, start_position=-len(prefix), display_meta=description)


def show_slash_command(console: Console, user_input: str) -> bool:
    """UI内の一覧・説明だけ処理する。ツールやAIは呼び出さない。"""
    command = user_input.strip().lower()
    if (not command.startswith("/") or any(ch.isspace() for ch in command)
            or "/" in command[1:]):
        # /home/user/file.pyのようなパスや、/で始まる通常の依頼はAIへ渡す。
        return False
    console.print(Rule(style="#365568", characters="─"))
    if command in {"/help", "/ha", "/tools"}:
        console.print(Text("  コマンド", style="bold cyan"))
        console.print(Text("  /help・/ha  ヘルプ    /tools  ツール一覧    /new  新しい会話    /exit  終了", style="dim"))
        console.print(Text("  /think  直前の思考を表示（履歴・セッションには保存しません）", style="dim"))
        console.print(Text("  /fast   次の依頼から思考なし    /fast off   通常設定へ戻す", style="dim"))
        console.print(Text("  /attach \"画像パス\"  添付    /paste  画像を貼り付け    /attachments  確認    /detach 番号  解除（*で全解除）", style="dim"))
        console.print(Text("  画像パスだけの入力（ドラッグ＆ドロップ）→ Enterでも添付できます。", style="dim"))
        console.print(Text("  /receipts \"フォルダー\"  領収書を読み取る（作業フォルダー外は [y/N] で確認し、読み取りだけ許可）", style="dim"))
        console.print(Text("  /excel \"フォルダー\"     経費申請Excelをそのフォルダーに保存（作業フォルダー外は [y/N] で確認）", style="dim"))
        console.print(Text("\n  キー操作", style="bold cyan"))
        console.print(Text("  Enter    送信\n  Ctrl+J   改行\n  Ctrl+C   処理中の停止要求\n  Esc      補完候補を閉じる（終了しません）", style="dim"))
        console.print(Text("  灰色の候補は → または Tab で確定", style="dim"))
        console.print(Text("  ※実行中のコマンドは、停止要求後も終了まで待つ場合があります。", style="dim"))
        console.print(Text("\n  ツール（選ぶと説明を表示）", style="bold cyan"))
        for name in sorted(REGISTRY):
            title = REGISTRY[name].ui.get("title", "説明を表示")
            console.print(Text(f"  /{name}  —  {title}", style="cyan"))
    elif command[1:] in REGISTRY:
        tool = REGISTRY[command[1:]]
        guide = tool.ui
        title = guide.get("title", tool.name)
        console.print(Text(f"  /{tool.name}  —  {title}", style="bold cyan"))
        console.print(Text(guide.get("description", "このツールの日本語ガイドは未設定です。通常の文章でAIへ依頼できます。")))
        console.print(Text("\n  引数の意味", style="bold cyan"))
        properties = tool.parameters.get("properties", {})
        required = tool.parameters.get("required", [])
        for name, parameter in properties.items():
            label = "必須" if name in required else "任意"
            description = guide.get("args", {}).get(name, "ツールへ渡す値")
            default = f"／既定：{parameter['default']}" if "default" in parameter else ""
            console.print(Text(f"  {name} ({label}{default})  {description}", style="dim"))
        if guide.get("example"):
            console.print(Text("\n  AIへの依頼例", style="bold cyan"))
            console.print(Text(f"  {guide['example']}"))
        console.print(Text("  ※説明の表示のみです。実行は通常の文章でAIへ依頼してください。", style="dim"))
    else:
        console.print(Text("  不明なコマンドです。/help で一覧を確認してください。", style="yellow"))
    console.print()
    return True


def load_logo_pixels() -> list | None:
    """ロゴは任意の装飾。欠損・破損時は文字だけで起動できるようにする。"""
    assets = Path(__file__).resolve().parent / "excudo" / "assets"
    try:
        pixels = json.loads((assets / "logo-pixels.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    if not isinstance(pixels, list) or not pixels or not isinstance(pixels[0], list) or not pixels[0]:
        return None
    width = len(pixels[0])
    if any(not isinstance(row, list) or len(row) != width for row in pixels):
        return None
    if any(color is not None and (not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color))
           for row in pixels for color in row):
        return None
    return pixels


def make_input_session(console: Console) -> PromptSession | None:
    """端末向けの補完・Esc・入力色を設定する。パイプ入力では通常入力を使う。"""
    if not console.is_terminal:
        return None
    bindings = KeyBindings()

    @bindings.add("c-v")
    @bindings.add("c-o")
    def paste_image(event):
        from prompt_toolkit.application import run_in_terminal
        pending = getattr(session, "image_attachments", None)
        if pending is not None:
            run_in_terminal(lambda: handle_image_command(console, "/paste", pending))

    @bindings.add("escape", eager=True)
    def dismiss_completion(event):
        # 誤操作で会話を失わないよう、Escではアプリを終了しない。
        # 補完の選択だけを取り消し、入力中の文章も消さない。
        event.current_buffer.cancel_completion()

    @Condition
    def editing_slash_command():
        buffer = get_app().current_buffer
        return (buffer.document.text_before_cursor.startswith("/")
                and buffer.selection_state is None and not buffer.read_only())

    @bindings.add("backspace", filter=editing_slash_command)
    def delete_and_refresh_completion(event):
        # Deletion clears the menu; refresh the candidates for the remaining prefix.
        buffer = event.current_buffer
        if buffer.delete_before_cursor(count=event.arg) and buffer.document.text_before_cursor.startswith("/"):
            buffer.start_completion(select_first=False)

    @bindings.add("enter")
    def send(event):
        buffer = event.current_buffer
        state = buffer.complete_state
        if state and any(c.text == buffer.text for c in state.completions):
            buffer.validate_and_handle()
        elif state and state.current_completion:
            buffer.apply_completion(state.current_completion)
        elif state and state.completions and not any(c.text == buffer.text for c in state.completions):
            buffer.apply_completion(state.completions[0])
        elif buffer.text.strip():
            buffer.validate_and_handle()

    @bindings.add("c-j")
    def newline(event):
        # Option/Altの設定に依存しない改行。Enterは送信として明示的に割り当てる。
        event.current_buffer.insert_text("\n")

    @Condition
    def command_suggestion_available():
        buffer = get_app().current_buffer
        return CommandAutoSuggest().get_suggestion(buffer, buffer.document) is not None

    # 灰色の候補があるときだけTabを使い、/の補完や通常のTab操作は維持する。
    @bindings.add("tab", filter=command_suggestion_available)
    @bindings.add("right")
    def accept_command_suggestion(event):
        buffer = event.current_buffer
        # 現在の入力から再判定し、古い候補や/補完を誤って採用しない。
        suggestion = CommandAutoSuggest().get_suggestion(buffer, buffer.document)
        if suggestion:
            buffer.insert_text(suggestion.text)
        else:
            buffer.cursor_right()

    session = PromptSession(key_bindings=bindings, completer=SlashCompleter(),
                         auto_suggest=CommandAutoSuggest(),
                         multiline=True, prompt_continuation="    ",
                         reserve_space_for_menu=0,
                         complete_while_typing=True, style=Style.from_dict({
        "": "fg:#e7f2fa",
        "prompt": "fg:#80d4ea bold",
        "auto-suggestion": "fg:#72818c",
        "input-rule": "fg:#4a6864",
        "command": "fg:#a9becd",
        "command.current": "fg:#80d4ea bold",
        "completion-menu.completion": "bg:#173044 fg:#e7f2fa",
        "completion-menu.completion.current": "bg:#365568 fg:#ffffff bold",
        "completion-menu.meta.completion": "bg:#173044 fg:#a9becd",
        "completion-menu.meta.completion.current": "bg:#365568 fg:#ffffff",
    }))
    style_input_layout(session)
    return session


def print_startup_banner(console: Console) -> None:
    """元のビーバーと操作ガイドを枠内に配置し、端末幅に合わせて並べる。"""
    console.print()
    title = Text("EXCUDO", style="bold #9c8765")
    tips = Group(
        Text("使い方", style="bold #9c8765"),
        Text("調べたいこと・変更したいことを、そのまま入力してください。"),
        Text("/ で候補  ·  /help で操作一覧", style="dim"),
        Rule(style="#4a6864"),
        Text("キー操作", style="bold #9c8765"),
        Text("Enter   送信    Ctrl+J   改行", style="dim"),
        Text("Ctrl+C  処理中の停止要求", style="dim"),
        Text("Esc     補完候補を閉じる", style="dim"),
        Text("/new    新しい会話    /exit   終了", style="dim"),
        Rule(style="#4a6864"),
        Text("このセッション", style="bold #9c8765"),
        Text(f"利用可能なツール: {len(REGISTRY)}", style="dim"),
        Text("作業フォルダー", style="dim"),
        Text(str(config.WORKSPACE_ROOT), style="dim", no_wrap=True, overflow="ellipsis"),
    )
    pixels = load_logo_pixels()
    if pixels is None:
        console.print(Panel(Group(title, tips), title="EXCUDO", border_style="#4a6864"))
        print_chat_header(console)
        return
    source_width = len(pixels[0])
    width = min(source_width, max(1, console.width - 6))
    height = max(2, round(len(pixels) * width / source_width))
    height += height % 2
    scaled = [[pixels[min(len(pixels) - 1, y * len(pixels) // height)]
               [min(source_width - 1, x * source_width // width)]
               for x in range(width)] for y in range(height)]
    logo = Text()
    for y in range(0, height, 2):
        for top, bottom in zip(scaled[y], scaled[y + 1]):
            if top and bottom:
                logo.append("▀", style=f"{top} on {bottom}")
            elif top:
                logo.append("▀", style=top)
            elif bottom:
                logo.append("▄", style=bottom)
            else:
                logo.append(" ")
        if y + 2 < height:
            logo.append("\n")
    identity = Group(Align.center(title), Align.center(logo),
                     Align.center(Text("coding assistant", style="dim")))
    if console.width >= 95:
        layout = Table.grid(padding=(0, 2), expand=True)
        layout.add_column(width=42)
        layout.add_column(ratio=1, vertical="top")
        layout.add_row(identity, tips)
        content = layout
    else:
        # 狭い端末では案内をロゴの下へ移す。
        content = Group(identity, Text(""), tips)
    console.print(Panel(content, title="EXCUDO", title_align="left",
                        border_style="#4a6864", padding=(1, 2)))
    print_chat_header(console)


def print_chat_header(console: Console) -> None:
    console.print()
    console.print(Text("  CHAT  ·  / でコマンド候補  ·  /help で操作一覧", style="dim"))
    console.print()


def terminal_width(console: Console) -> int:
    """実端末の幅を優先する。TERM=dumbや固定COLUMNSによる80列判定を避ける。"""
    if console.is_terminal:
        try:
            columns = os.get_terminal_size(console.file.fileno()).columns
            if columns > 0:
                return columns
        except (AttributeError, OSError, ValueError):
            pass
    return console.width


class StreamPanel:
    """回答・思考の最新1行・状態を表示する。思考本文はUIだけで保持する。"""

    def __init__(self, console: Console, *, fast_mode: bool = False):
        self._console = console
        self._fast_mode = fast_mode
        self._started = time.monotonic()
        self._receiving_note_since: float | None = None
        self._received_since: float | None = None
        self._characters = 0
        self._thinking_characters = 0
        self.thoughts = ThinkingTrace()
        self._answer = MarkdownStream()
        self._pending = ""
        self._receiving = False
        self._thinking = False
        self._failed = False
        self._cancel_requested = False
        self._closed = False
        self._progress = ""  # 実行中ツールの途中経過（共通の tool_progress）
        # イベントが来ない通信待ちでも、時刻とアニメーションを更新する。
        # 全画面は使わず、確定した回答は通常の端末履歴に残す。
        self._status = ReplyStatus(console, self._render, self._body_preview, preview=self._thought_preview, note=self._story_note)

    def _thought_preview(self):
        if self._thinking and not self._receiving and not self._cancel_requested and self.thoughts.preview_ready:
            width = max(0, terminal_width(self._console) - 4)
            latest = self.thoughts.preview_tail(width, cjk_console=os.name == "nt")
            return Text("  " + latest, style="dim", no_wrap=True)
        return None

    def _story_note(self):
        """小話を本文・思考原文から分けた一時表示として返す。"""
        if self._closed or self._cancel_requested or self._failed:
            return None
        if self._receiving:
            # Expire from the first body chunk, not the last one. ReplyStatus's
            # existing refresh loop also removes the note during network pauses.
            if (self._receiving_note_since is None
                    or time.monotonic() - self._receiving_note_since >= 5.0):
                return None
            note = self.thoughts.receiving_note()
        elif self._thinking:
            note = self.thoughts.wait_note()
        else:
            note = None
        if not note:
            return None
        spoken_note = note.replace("「", "『").replace("」", "』")
        return Text("  Excudo：「" + spoken_note + "」", style="cyan not dim")

    def _body_preview(self):
        return "" if self._answer.waiting_for_structure else self._pending

    def feed(self, text: str) -> None:
        if not text:
            return
        self.thoughts.finish_phase()
        if self._received_since is None:
            self._received_since = time.monotonic()
        if not self._receiving:
            self._receiving_note_since = time.monotonic()
        self._characters += len(text)
        self._receiving = True
        self._thinking = False
        self._pending += text
        self._print_blocks(self._answer.feed(text))
        # 空行を待たず途中表示する。構造が確定した部分だけ履歴へ移す。
        self._status.refresh()

    def thinking(self, text: str) -> None:
        """受信した思考をUIメモリへ追加し、最新1行と文字数を更新する。"""
        if not text:
            return
        self.thoughts.append(text)
        self._thinking_characters += len(text)
        self._thinking = True
        self._status.refresh()

    def _render(self):
        now = time.monotonic()
        elapsed = now - self._started
        mode_label = "FAST · " if self._fast_mode else ""
        if self._thinking and not self._cancel_requested:
            status = Text(f"  {mode_label}思考中 · {self._thinking_characters:,}文字 · {elapsed:.1f}秒",
                          style="#9c8765", no_wrap=True, overflow="ellipsis")
            return status
        # 総量・完了時刻は不明。左から増えてリセットする処理中アニメーションを描く。
        # いっぱいになっても完了を意味せず、百分率や残り時間は表示しない。
        # コアの送信完了通知はないので、送信と初回応答待ちはまとめたラベルにする。
        color = "bright_cyan" if self._receiving else "#9c8765"
        label = ("停止待ち" if self._cancel_requested else
                 "応答を生成中（本文待ち）" if self._thinking else
                 "↓ 受信中" if self._receiving else self._progress or "↑ 送信・応答待ち")
        status = Text(f"  {mode_label}{label}  ", style=color, no_wrap=True, overflow="ellipsis")
        bar_width = max(4, min(20, terminal_width(self._console) - 48))
        filled = int(elapsed * 5) % (bar_width + 1)
        for i in range(bar_width):
            status.append("━", style=color if i < filled else "bright_black")
        status.append(f"  ·  {elapsed:.1f}秒", style="dim")
        # 回答本文の平均受信ペース。回線速度やモデルのトークン速度ではない。
        # ツール待ちも含む平均なので、残り時間の予測には使わない。
        if self._receiving and self._received_since is not None:
            duration = now - self._received_since
            pace = f"{self._characters / duration:.0f}文字/秒" if duration >= 0.5 else "計測中"
            status.append(f"  ·  {pace}", style="dim")
        return status

    def waiting(self) -> None:
        self.thoughts.finish_phase()
        # ツール前の回答を履歴へ確定して待機表示に戻す。
        self._flush_answer()
        self._receiving = False
        self._thinking = False
        self._status.refresh()

    def tool_progress(self, event: dict) -> None:
        """実行中ツールの途中経過。段階は状態行に、1件ごとの結果（集計行を含む）は履歴に出す。"""
        self.waiting()
        message = event.get("message")
        if message:
            line = re.sub(r"[\x00-\x1f\x7f]", " ", redact_secrets(redact(str(message))))
            self._status.print(Text(f"    {line}", style="dim", no_wrap=True, overflow="ellipsis"))
        else:
            values = {key: event.get(key) if event.get(key) is not None else "" for key in ("current", "total", "name")}
            self._progress = re.sub(r"[\x00-\x1f\x7f]", " ", t("app_tool_" + str(event.get("stage")), **values))
        total, completed = event.get("total"), event.get("completed")
        if isinstance(total, int) and total > 0 and isinstance(completed, int):
            self._progress = self._progress.split(" · ")[0] + " · " + t("app_tool_completed", current=completed, total=total)
        self._status.refresh()

    def tool_call(self, name: str, args: dict) -> None:
        """実行済みのツールを1行で表示する。結果全文・書き込む内容は表示しない。"""
        self._progress = ""
        self.waiting()
        # 通知内容もToolの登録から取得し、新しいツールの追加時にUIの辞書更新を不要にする。
        tool = REGISTRY.get(name)
        guide = tool.ui if tool else {}
        primary = guide.get("primary_arg")
        detail = str(args.get(primary, "")) if primary else ""
        for key, label in guide.get("context_args", {}).items():
            default = tool.parameters.get("properties", {}).get(key, {}).get("default")
            value = args.get(key, default)
            if value is not None:
                detail += f" · {label}={value}"
        summary = f"⏺ {name}" + (f": {detail}" if detail else "")
        if guide.get("action"):
            summary += f" · {guide['action']}"
        # 既知のAPIキーをマスクし、改行・制御文字による表示の乱れを防ぐ。
        # 長い引数は末尾を省略する。実行後の通知であり、承認処理ではない。
        # 設定済みのキーに加え、既知の形式の鍵・トークン・URL内のパスワードも隠す。
        summary = redact_secrets(redact(summary))
        summary = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", summary)
        summary = re.sub(r"[\x00-\x1f\x7f]", " ", summary)
        line = Text(f"  {summary}", style="dim cyan", no_wrap=True, overflow="ellipsis")
        line.truncate(max(1, terminal_width(self._console)), overflow="ellipsis")
        self._status.print(line)
        self._status.refresh()

    def _flush_answer(self) -> None:
        self._print_blocks(self._answer.finish())
        self._pending = ""

    def _print_blocks(self, blocks) -> None:
        # 確定済み段落だけを履歴へ追加する。途中表示は消して二重表示を防ぐ。
        for block in blocks:
            # パーサーは区切りの空行を省くので、単純な文字数ではなく元の位置で消す。
            start = self._pending.index(block)
            self._pending = self._pending[start + len(block):]
            # 横長の端末でも文章を追いやすい幅にし、通知・入力と本文の位置を分ける。
            # 元の文章やコードには改行を挿入せず、表示時だけ折り返す。
            columns = terminal_width(self._console)
            margin = 2 if columns >= 20 else 0
            body = Markdown(block, code_theme="monokai", hyperlinks=False, justify="left")
            # 表は端末幅を使う。確定表示も右端1列を空け、自動折返しと改行の
            # 重なりで箇条書きの項目間に空行が見えることを防ぐ。
            available = max(1, columns - margin * 2 - 1)
            width = available if body.has_table else min(100, available)
            self._status.print(Padding(Constrain(body, width), (0, margin), expand=False))
            self._status.print()

    def error(self, content: str) -> None:
        self.mark_failed()
        self._flush_answer()
        self._status.print(Text(f"  ! {content}", style="red"))

    def warning(self, content: str) -> None:
        self._flush_answer()
        self._status.print(Text(f"  ⚠ {content}", style="yellow"))

    def mark_failed(self) -> None:
        """異常終了を記録する。呼び出し元が内部の表示状態を直接変更しないための窓口。"""
        self._failed = True
        self.thoughts.mark_incomplete()

    def cancelling(self) -> None:
        self.thoughts.mark_incomplete()
        self._cancel_requested = True
        self._receiving = False
        self._status.refresh()

    def pause(self) -> None:
        self.thoughts.finish_phase()
        self._thinking = False
        self._status.pause()

    def resume(self) -> None:
        self._status.resume()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.thoughts.finish_phase()
        self._thinking = False
        self._flush_answer()
        label = "停止" if self._cancel_requested else "終了" if self._failed else "完了"
        self._status.print(Text(f"  {label}  ·  {time.monotonic() - self._started:.1f}秒\n", style="dim"))
        if self.thoughts.characters:
            self._status.print(Text("  " + self.thoughts.summary(), style="dim"))
        self._status.stop()


class ApprovalBridge:
    """PermissionGate の承認確認をワーカースレッドから UI スレッドへ渡す。

    Harness はワーカースレッドで動くため、gate の approval_callback もそこで呼ばれる。
    確認はそのターンのイベントキューで UI スレッドへ送り、端末と Ctrl+C を持つ UI スレッドで
    尋ねて、答えをワーカーへ返す。ターンが中断されたら、答えを待たずに拒否する。
    """

    def __init__(self):
        self.events = None  # the current turn's queue (set by iter_ui_events)
        self.cancel_requested = None

    def request(self, tool_name: str, args: dict, risk: ActionRisk) -> bool:
        events, cancel_requested = self.events, self.cancel_requested
        if events is None:
            return False  # no turn is reading the events: nobody can answer
        reply = {"done": threading.Event(), "allowed": False}
        events.put(("approval", (tool_name, args, risk, reply)))
        while not reply["done"].wait(0.1):
            if cancel_requested is not None and cancel_requested.is_set():
                return False
        return reply["allowed"]


def handle_receipt_folder(console: Console, argument: str, ask=None) -> str | None:
    """/receipts "フォルダー": 領収書の読み取り依頼を作る。作業フォルダー外なら [y/N] で確認し、読み取りだけ許可する。

    許可はこの起動中だけ。結果は作業フォルダーの receipt_data に保存し、選んだフォルダーは変更しない。
    """
    from excudo.tools import workspace_path
    folder = argument.strip().strip('"').strip("'")
    if "process_receipts" not in REGISTRY:
        console.print(Text("  領収書ツールが有効ではありません（keichan extra が必要です）。", style="dim"))
        return None
    if not folder:
        console.print(Text('  例：/receipts "D:\\経費\\10月"', style="dim"))
        return None
    try:
        path = workspace_path(folder)
    except ValueError as error:
        if not str(error).startswith("Access outside the workspace"):
            console.print(Text(f"  ! {error}", style="red"))
            return None
        from excudo.keichan.tools import grant_receipt_folder
        shown = re.sub(r"[\x00-\x1f\x7f]", " ", folder)
        console.print(Text(f"  作業フォルダー外のフォルダーです：{shown}", style="yellow"))
        console.print(Text("  このフォルダーの領収書画像を読み取ります。フォルダー内は変更せず、結果は作業フォルダーに保存します。", style="dim"))
        answer = (ask or (lambda prompt: console.input(Text(prompt, style="bold yellow"))))("  読み取りを許可しますか？ [y/N] ")
        if answer.strip().lower() not in {"y", "yes"}:
            console.print(Text("  許可しませんでした。", style="dim"))
            return None
        try:
            path = grant_receipt_folder(folder)
        except (ValueError, OSError) as refused:
            console.print(Text(f"  ! {refused}", style="red"))
            return None
    if not path.is_dir():
        console.print(Text("  フォルダーが見つかりません。", style="red"))
        return None
    return t("app_receipt_request") + "\n" + t("app_receipt_source", path=str(path))


def handle_export_folder(console: Console, argument: str, ask=None) -> str | None:
    """/excel "フォルダー": 経費申請Excelの保存先を指定する。作業フォルダー外なら [y/N] で確認し、Excelの保存だけ許可する。"""
    from excudo.tools import workspace_path
    folder = argument.strip().strip('"').strip("'")
    if "export_receipts" not in REGISTRY:
        console.print(Text("  領収書ツールが有効ではありません（keichan extra が必要です）。", style="dim"))
        return None
    if not folder:
        console.print(Text('  例：/excel "D:\\経費\\提出用"', style="dim"))
        return None
    try:
        path = workspace_path(folder)
    except ValueError as error:
        if not str(error).startswith("Access outside the workspace"):
            console.print(Text(f"  ! {error}", style="red"))
            return None
        from excudo.keichan.tools import grant_export_folder
        shown = re.sub(r"[\x00-\x1f\x7f]", " ", folder)
        console.print(Text(f"  作業フォルダー外のフォルダーです：{shown}", style="yellow"))
        console.print(Text("  経費申請Excelをこのフォルダーに保存します。同じ名前のファイルは置き換えます。", style="dim"))
        answer = (ask or (lambda prompt: console.input(Text(prompt, style="bold yellow"))))("  保存を許可しますか？ [y/N] ")
        if answer.strip().lower() not in {"y", "yes"}:
            console.print(Text("  許可しませんでした。", style="dim"))
            return None
        try:
            path = grant_export_folder(folder)
        except (ValueError, OSError) as refused:
            console.print(Text(f"  ! {refused}", style="red"))
            return None
    if not path.is_dir():
        console.print(Text("  フォルダーが見つかりません。", style="red"))
        return None
    return "保存済みのデータから経費申請Excelを作成してください。\n" + t("app_export_target", path=str(path))


def iter_ui_events(harness, user_input: str, on_cancel, cancel_requested=None, approvals=None, on_approval=None,
                   *, thinking: bool | None = None):
    """UIスレッドでCtrl+Cを受け取り、既存のcancel()へつなぐ。

    approvals（ApprovalBridge）があれば、ワーカーからの承認確認を on_approval で尋ねる。
    """
    events = queue.Queue(maxsize=256)
    cancel_requested = cancel_requested or threading.Event()
    if approvals is not None:
        approvals.events, approvals.cancel_requested = events, cancel_requested

    def produce():
        from excudo.tool_progress import progress_events
        try:
            # Tool progress (the same common events the desktop shows) goes through the same queue.
            request = progress_events(harness.run(user_input, **({"thinking": thinking} if thinking is not None else {})),
                                      lambda event: events.put(("event", event)))
            for event in request:
                # run開始直後のリセットと中断要求が重なった場合も要求を維持する。
                if cancel_requested.is_set():
                    harness.cancel()
                events.put(("event", event))
        except BaseException as error:
            events.put(("exception", error))
        finally:
            events.put(("end", None))

    worker = threading.Thread(target=produce, daemon=True)
    worker.start()
    finished = False
    try:
        while True:
            try:
                kind, value = events.get(timeout=0.1)
            except KeyboardInterrupt:
                cancel_requested.set()
                on_cancel()
                harness.cancel()
                continue
            except queue.Empty:
                # 停止要求をrun開始時にクリアされても、次の待機周期で再通知する。
                if cancel_requested.is_set():
                    harness.cancel()
                continue
            if kind == "end":
                finished = True
                break
            if kind == "exception":
                raise value
            if kind == "approval":
                tool_name, args, risk, reply = value
                try:
                    allowed = bool(on_approval and on_approval(tool_name, args, risk))
                except KeyboardInterrupt:
                    # Ctrl+C while asking denies the action and stops the turn.
                    allowed = False
                    cancel_requested.set()
                    on_cancel()
                    harness.cancel()
                reply["allowed"] = allowed and not cancel_requested.is_set()
                reply["done"].set()
                continue
            if not cancel_requested.is_set() or value.get("type") == "done":
                yield value
        # コアが終了するまで次の依頼を受け付けず、同じHarnessの並列実行を防ぐ。
        worker.join()
    finally:
        if not finished:
            cancel_requested.set()
            harness.cancel()
        if approvals is not None:
            approvals.events = None


def get_effective_work_dir(args: dict, gate=None) -> Path:
    """
    指定された引数辞書の cwd と基準パスから、実際の作業ディレクトリの絶対パスを返します。
    """
    cwd_str = args.get("cwd")
    base_root = get_base_workspace_root(gate)

    if not cwd_str:
        return base_root

    cwd_path = Path(str(cwd_str))
    if cwd_path.is_absolute():
        return cwd_path.resolve()

    return (base_root / cwd_path).resolve()


def resolve_target_file_path(file_path: str | Path, gate=None) -> Path:
    """
    対象ファイルのパスをワークスペース基準で解決します。
    """
    base_root = get_base_workspace_root(gate)

    if not file_path:
        return base_root

    target_path = Path(file_path)
    if target_path.is_absolute():
        return target_path.resolve()

    return (base_root / target_path).resolve()


def get_base_workspace_root(gate) -> Path:
    return Path(gate.workspace_root).resolve()

def compute_file_diff(old_full_text: str, new_full_text: str) -> Table:
    """元ファイル全体と変更後全体のテキストから正確な差分テーブルを生成する。"""
    table = Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 1))
    table.add_column("#", style="dim", justify="left")
    table.add_column("状態", justify="center")
    table.add_column("内容", justify="left", style="cyan")

    # 改行コードを LF（\n）に統一して比較時のズレをなくす
    old_clean = (old_full_text or "").replace("\r\n", "\n")
    new_clean = (new_full_text or "").replace("\r\n", "\n")

    old_lines = old_clean.splitlines()
    new_lines = new_clean.splitlines()

    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    has_diff = False

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue

        has_diff = True

        # 削除行 (old_lines の正確な物理行番号 i1+1 ~ i2)
        if tag in ("replace", "delete"):
            for idx, line in enumerate(old_lines[i1:i2], start=i1 + 1):
                table.add_row(str(idx), Text("-", style="bold red"), Text(redact_secrets(line), style="red"))

        # 追加行 (new_lines の正確な物理行番号 j1+1 ~ j2)
        if tag in ("replace", "insert"):
            for idx, line in enumerate(new_lines[j1:j2], start=j1 + 1):
                table.add_row(str(idx), Text("+", style="bold green"), Text(redact_secrets(line), style="green"))

    if not has_diff:
        table.add_row("-", "", Text("(変更内容はありません)", style="dim"))

    return table


@dataclass
class PatchDiffResult:
    """apply_patch の差分描画用コンポーネントと属性情報を保持するクラス"""
    group: Group
    summary: str
    is_create: bool = False


def compute_patch_diff(patch: str) -> PatchDiffResult | None:
    """unified diff（apply_patchの引数）からファイルごとの差分表示コンポーネントを生成する。
    
    tools.py の apply_patch と同一の判定基準（_HUNK_HEADER）でパースする
    """
    if not isinstance(patch, str) or not patch.strip():
        return None

    lines = patch.splitlines()
    file_sections = []
    index = 0

    while index < len(lines):
        line = lines[index]
        # ファイルヘッダー以外の不要な行（diff --git など）をスキップ
        if not line.startswith("--- "):
            if not line.strip() or line.startswith(_PATCH_INFO_PREFIXES):
                index += 1
                continue
            return None

        old_name = patch_header_path(line, "a/")
        index += 1
        if index >= len(lines) or not lines[index].startswith("+++ "):
            return None
        new_name = patch_header_path(lines[index], "b/")
        index += 1

        if not new_name:
            return None

        is_create = (old_name is None)

        table = Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 1))
        table.add_column("#", style="dim", justify="left")
        if not is_create:
            table.add_column("状態", justify="center")
        table.add_column("内容", justify="left", style="cyan")

        has_diff = False

        while index < len(lines):
            raw_line = lines[index]
            header = raw_line.strip()

            # 次のファイルヘッダーが来たらこのファイルの解析を終了
            if raw_line.startswith("--- ") or raw_line.startswith("diff --git "):
                break

            match = _HUNK_HEADER.match(header)  
            if not match:
                index += 1
                continue

            old_line = int(match.group(1))
            new_line = int(match.group(3))
            index += 1

            while index < len(lines):
                sub = lines[index]

                # 次の Hunk または 次のファイルヘッダーが来たら内側ループを抜ける
                if _HUNK_HEADER.match(sub.strip()) or sub.startswith("--- ") or sub.startswith("diff --git "):
                    break
                if sub.startswith("\\ No newline at end of file"):
                    index += 1
                    continue

                # 空行（先頭スペースが省略された文脈行）への対応
                if not sub:
                    old_line += 1
                    new_line += 1
                    index += 1
                    continue

                marker = sub[0]
                content = sub[1:]

                if marker in ("-", "+"):
                    has_diff = True
                    is_add = (marker == "+")
                    style = "green" if is_add else "red"
                    line_no = new_line if is_add else old_line

                    # 新規作成時は状態列（+）を出さずに内容を表示
                    if is_create:
                        table.add_row(str(line_no), Text(redact_secrets(content), style="cyan"))
                    else:
                        table.add_row(str(line_no), Text(marker, style=f"bold {style}"), Text(redact_secrets(content), style=style))

                    if is_add:
                        new_line += 1
                    else:
                        old_line += 1
                else:
                    # 文脈行（スペース）または予期しない行は行番号だけインクリメント
                    old_line += 1
                    new_line += 1
                index += 1

        if not has_diff:
            if is_create:
                table.add_row("-", Text("(変更内容はありません)", style="dim"))
            else:
                table.add_row("-", "", Text("(変更内容はありません)", style="dim"))

        safe_path = rich.markup.escape(new_name)

        if is_create:
            heading = Text.assemble(
                (f"📄 {safe_path} ・", "bold"),
                ("新規作成", "bold green"),
                ("(ファイルが存在しないため、新規作成します)\n", "dim")
            )
        else:
            heading = Text(f"📄 {safe_path}\n", style="bold")

        file_sections.append({
            "name": safe_path,
            "is_create": is_create,
            "heading": heading,
            "table": table,
        })

    if not file_sections:
        return None

    elements = []
    for idx, sec in enumerate(file_sections):
        if idx > 0:
            elements.append(Rule(style="#365568"))
        elements.append(sec["heading"])
        elements.append(sec["table"])

    if len(file_sections) == 1:
        sec = file_sections[0]
        single_is_create = sec["is_create"]
        summary = sec["name"]
    else:
        summary = f"{len(file_sections)}ファイル"
        single_is_create = False

    return PatchDiffResult(group=Group(*elements), summary=summary, is_create = single_is_create)


def make_approval_prompt(console: Console, gate: PermissionGate) -> Callable[[str, dict, ActionRisk], bool]:
    """Builds the y/N approval prompt asked on the UI thread (see ApprovalBridge).

    Outbound actions and secret files are framed in red, others in yellow. Only
    "y" / "yes" allows; an empty answer, anything else or EOF denies. Ctrl+C is left
    to the caller (iter_ui_events), which denies and also stops the turn.
    After external content, only the whole word "yes" allows (a reflexive "y" is
    not enough), and internal details that would leave the company are listed.
    """

    def ask(tool_name: str, args: dict, risk: ActionRisk) -> bool:
        # Secrets in the arguments (keys, tokens, passwords in URLs) are shown masked.
        # --- 引数内のシークレット（APIキー等）をマスキングした辞書を作成 ---

        presentation = None
        if risk == ActionRisk.RESTORE:
            try:
                presentation = restore_approval(args)
            except ValueError:
                console.print(Text("復元するファイルを確認できません。操作を中止します。", style="bold red"))
                return False
        effective_work_dir = get_effective_work_dir(args, gate)

        path_str = args.get("path", "")
        old_text = args.get("old_text", None)
        new_text = args.get("new_text") if args.get("new_text") is not None else args.get("content", None)
        patch_text = args.get("patch", None)
        patch_summary = None
        patch_is_create = False

        # パス名やツール名をエスケープ処理
        safe_path = rich.markup.escape(path_str)
        safe_tool = rich.markup.escape(tool_name)

        abs_path = resolve_target_file_path(path_str, gate) if path_str else None

        file_exists = abs_path is not None and abs_path.exists()

        if file_exists and old_text is None:
            try:
                old_text = abs_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                old_text = None

        is_create = bool(path_str and not file_exists)

        if presentation is not None:
            content_renderable = Text(presentation.text)

        elif tool_name == "apply_patch" and patch_text is not None:
            # --- apply_patch 用の複数ファイル差分表示 ---
            patch_result = compute_patch_diff(patch_text)
            if patch_result is not None:
                content_renderable = patch_result.group
                patch_summary = patch_result.summary
                patch_is_create = patch_result.is_create
            else:
                content_renderable = Text(redact_secrets(patch_text))

        elif is_create and new_text is not None:
            # --- 新規作成時 ---
            table = Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 1))
            table.add_column("#", style="dim", justify="left")
            table.add_column("内容", justify="left", style="cyan")

            lines = new_text.splitlines()
            for idx, line in enumerate(lines[:50], start=1):
                table.add_row(str(idx), Text(redact_secrets(line)))
            if not lines:
                table.add_row("1", Text("(空のファイル)", style="dim"))
            elif len(lines) > 50:
                table.add_row("...", Text(f"... 残り {len(lines) - 50} 行", style="dim"))

            notice = Text()
            notice.append(f"📄 {safe_path} ・",style="bold")
            notice.append("新規作成", style="bold green")
            notice.append("(ファイルが存在しないため、新規作成します)\n", style="dim")

            content_renderable = Group(notice, table)

        elif new_text is not None:
            # --- 既存ファイルの変更時（差分表示） ---

            full_old = ""
            if abs_path is not None and abs_path.exists():
                try:
                    full_old = abs_path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    full_old = ""

            # 改行コードをすべて LF (\n) に揃えて比較
            full_old_norm = full_old.replace("\r\n", "\n")
            old_text_norm = old_text.replace("\r\n", "\n") if old_text is not None else None
            new_text_norm = new_text.replace("\r\n", "\n")

            if old_text_norm is not None and old_text_norm in full_old_norm:
                full_new = full_old_norm.replace(old_text_norm, new_text_norm, 1)
            elif old_text_norm is not None:
                # 置き換え対象が存在しない場合でもプロンプト確認を行えるようにフォールバックメッセージを生成
                content_renderable = Text("置き換え対象の文章が見つかりません", style="yellow")
                full_new = None
            else:
                full_new = new_text_norm

            if full_new is not None:
                heading = Text(f"📄 {safe_path}\n", style="bold")
                content_renderable = Group(heading, compute_file_diff(full_old_norm, full_new))

        else:
            # --- bash_exec などのコマンド実行・その他ツール ---

            details_list = []
            for key, value in args.items():
                if key == "purpose":
                    continue  # Purpose is displayed once, after the actual command and warnings.
                masked_val = redact_secrets(redact(str(value))) if isinstance(value, str) else str(value)
                row = Text()
                row.append(f"{rich.markup.escape(str(key))}: ", style="bold cyan")
                # Text(): values such as "[red]" in a command are shown as-is, not as markup.
                row.append(str(masked_val))
                details_list.append(row)

            if details_list:
                content_renderable = Group(*details_list)
            else:
                content_renderable = Text("(引数なし)", style="dim")

        security_warnings = []
        if presentation is not None and presentation.removes_files:
            security_warnings.append(Text("エージェントが作成したファイルを削除します。", style="bold red"))
        if risk == ActionRisk.DELETE:
            security_warnings.append(Text("この会話の間は、保存状態により削除を取り消せる場合があります。", style="bold red"))
        if risk in (ActionRisk.EXTERNAL, ActionRisk.SENSITIVE):
            findings = outbound_findings(args)
            if findings:
                security_warnings.append(Text(f"\n社内情報が外部に送信されます: {', '.join(findings)}", style="bold red"))
        if getattr(gate, "untrusted_content_seen", False):
            security_warnings.append(Text("\nこの会話には外部の内容が含まれています。外部の指示による操作でないか確認してください。", style="bold yellow"))

        info_header = Text(f"作業ディレクトリ: {effective_work_dir}\n", style="dim")

        group_elements = [info_header, Rule(style="#365568"), content_renderable]
        if security_warnings:
            group_elements.extend(security_warnings)

        # Purpose is untrusted, display-only context; retain commands/diffs and warnings first.
        from excudo.approval_view import approval_purpose
        purpose = approval_purpose(args)
        if purpose:
            group_elements.extend([Rule(style="#365568"), Text(display_text(purpose))])

        # Browser tools: which element of the last page snapshot the number refers to.
        from excudo.browser import approval_note as browser_approval_note
        note = browser_approval_note(tool_name, args)
        if note:
            group_elements.append(Text(redact_secrets(note)))

        group_content = Group(*group_elements)
        border = "red" if risk in (ActionRisk.DELETE, ActionRisk.RESTORE, ActionRisk.EXTERNAL, ActionRisk.SENSITIVE) else "yellow"


        if presentation is not None:
            title_text = f"承認が必要です: {presentation.title}"
        elif tool_name == "apply_patch" and patch_summary:
            if patch_is_create:
                title_text = f"承認が必要です: {safe_tool} ({risk.value}) ({patch_summary}) ・新規作成"
            else:
                title_text = f"承認が必要です: {safe_tool} ({risk.value}) ({patch_summary})"
        elif is_create and new_text is not None:
            title_text = f"承認が必要です: {safe_tool} ({risk.value}) ({safe_path}) ・新規作成"
        elif new_text is not None:
            title_text = f"承認が必要です: {safe_tool} ({risk.value}) ({safe_path})"
        else:
            title_text = f"承認が必要です: {safe_tool} ({risk.value})"

        console.print(
            Panel(group_content, title=title_text, border_style=border)
        )

        # リスクレベル(risk)に応じた gate の判定ロジック
        require_full_yes = gate.needs_confirmation(risk)

        if require_full_yes:
            prompt_msg = Text("許可しますか？（外部情報が含まれるため 'yes' と入力してください） [yes/N] ", style="bold red")
        else:
            prompt_msg = Text("許可しますか？ [y/N] ", style="bold")

        try:
            # Text(), not markup: Rich would read "[y/N]" as a style tag and not show it.
            answer = console.input(prompt_msg).strip().lower()
        except EOFError:
            return False

        if require_full_yes:
            return answer == "yes"
        else:
            # Normalize only ordinary approval letters; strict confirmation above stays ASCII yes.
            answer = answer.translate(str.maketrans("ｙｅｓ", "yes"))
            return answer in {"y", "yes"}

    return ask


def handle_image_command(console: Console, user_input: str, pending: Attachments) -> bool | str:
    """Return False for ordinary input, True if consumed, or an attached question."""
    text = user_input.strip()
    if not text:
        return False
    first_line, _, question = text.partition("\n")
    # Only /attach accepts a question on following lines. Parse paths from its first line.
    if first_line.split(maxsplit=1)[0].lower() == "/attach":
        text = first_line
    else:
        question = ""
    pieces = text.split(maxsplit=1)
    command, argument = pieces[0], pieces[1] if len(pieces) > 1 else ""
    command = command.lower()
    recognized = command in {"/attach", "/paste", "/attachments", "/detach"}
    paths = []
    if not recognized:
        try:
            paths = split_paths(text)
        except ValueError:
            return False
        if not paths or not all(Path(path).suffix.lower() in SUFFIXES for path in paths):
            return False
    try:
        if command == "/attach":
            if not argument.strip():
                console.print(Padding(Text("画像のパスを指定してください。", style="yellow"), (0, 0, 0, 2)))
                console.print(Padding(Text('例：/attach "screenshots/photo.jpg"', style="dim"), (0, 0, 0, 2)))
                console.print(Padding(Text("対応形式：PNG・JPEG・WebP", style="dim"), (0, 0, 0, 2)))
                return True
            pending.add_paths(split_paths(argument))
        elif command == "/paste":
            if argument:
                raise ValueError("/pasteは引数なしで使用してください。")
            pending.paste()
        elif command == "/detach":
            if not argument or argument == "*":
                pending.clear()
                console.print(Text("  添付をすべて解除しました。", style="dim"))
                return True
            if argument not in {"1", "2", "3", "4"}:
                raise ValueError("/detach 1〜4 で画像を指定、/detach * で全解除できます。")
            index = int(argument) - 1
            if index >= len(pending.items):
                raise ValueError(f"画像 {argument} はありません。/attachmentsで番号を確認してください。")
            pending.items.pop(index)
            console.print(Text(f"  画像 {argument} の添付を解除しました。", style="dim"))
        elif command == "/attachments":
            if argument:
                raise ValueError("/attachmentsは引数なしで使用してください。")
        else:
            pending.add_paths(paths)
        if question.strip():
            return question.strip()
        if pending.items:
            for index, item in enumerate(pending.items, 1):
                console.print(Text(f"  画像 {index}: {redact_secrets(item.name)}（{len(item.data):,} bytes）", style="cyan"))
            console.print(Text("  次の質問と一緒にモデルへ送信し、会話履歴に保存します。/detachで解除できます。", style="dim"))
        else:
            if command == "/attachments":
                console.print(Padding(Text("まだ画像は添付されていません。", style="dim"), (0, 0, 0, 2)))
                console.print(Padding(Text('画像を添付するなら、/attach "画像パス" で指定できます。', style="dim"), (0, 0, 0, 2)))
                console.print(Padding(Text("コピーした画像は、/paste または Ctrl+O で貼り付けられます。", style="dim"), (0, 0, 0, 2)))
            else:
                console.print(Text("  添付画像はありません。", style="dim"))
    except (OSError, ValueError) as error:
        message = str(error) if isinstance(error, ValueError) else "画像を読めません。作業フォルダー内の通常ファイルを指定してください。"
        console.print(Padding(Text(f"添付できませんでした: {redact_secrets(message)}", style="yellow"), (0, 0, 0, 2)))
    return True


def main() -> None:
    # CLI display language stays Japanese, independently of OS or desktop settings.
    set_language("ja")
    # Windows既定のcp932だと絵文字や記号でUnicodeEncodeErrorになるため、
    # レガシーコンソール描画を無効化してANSIエスケープで出力する。
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
    console = Console(legacy_windows=False)
    print_startup_banner(console)
    # The Harness runs in a worker thread: approvals are asked on this (UI) thread.
    approvals = ApprovalBridge()
    gate = PermissionGate(approval_callback=approvals.request)
    gate.audit_log = gate.workspace_root / ".excudo" / "audit.log"
    harness = Harness(Model(), build_system_prompt(gate.workspace_root), gate)
    ask_approval = make_approval_prompt(console, gate)
    session = Session.new()
    pending_images = Attachments(gate.workspace_root)
    # 入力文字の色はConsole.inputでは制御できないため、入力部分だけprompt_toolkitを使う。
    # 全画面にはせず、端末の通常スクロールと入力編集を維持する。
    input_session = make_input_session(console)
    if input_session is not None:
        input_session.image_attachments = pending_images
    console.print(Text(f"  セッション: {session.id}", style="dim"))
    console.print(Text("  ファイル変更の自動承認: /auto on または /auto off", style="dim"))
    console.print(Text("  画像: Ctrl+O または /paste で貼り付け、/attachでファイルを選択。", style="dim"))

    last_thought = None
    fast_mode = False
    while True:
        try:
            user_input = (
                input_session.prompt([("class:prompt", "  FAST > " if fast_mode else "  > ")])
                if input_session else console.input(Text("  FAST > " if fast_mode else "  > ", style="bold cyan"))
            )
        except (EOFError, KeyboardInterrupt):
            break

        if user_input.strip().lower() in {"exit", "quit", "/exit"}:
            break
        if not user_input.strip():
            continue
        image_result = handle_image_command(console, user_input, pending_images)
        if image_result is True:
            continue
        if isinstance(image_result, str):
            user_input = image_result
        if image_result is False and user_input.strip().lower() in {"/auto on", "/auto off"}:
            gate.auto_approve = user_input.strip().lower() == "/auto on"
            state = "オン（ファイル変更のみ。コマンド実行と外部送信は常に確認）" if gate.auto_approve else "オフ"
            console.print(Text(f"自動承認: {state}\n", style="green"))
            continue
        command = user_input.strip().lower().split()
        if image_result is False and command[0] == "/fast":
            if command in (["/fast"], ["/fast", "on"]):
                fast_mode = True
                console.print(Text("  高速モード: オン（次の依頼から思考なし。承認は従来どおり）", style="cyan"))
            elif command == ["/fast", "off"]:
                fast_mode = False
                console.print(Text("  高速モード: オフ（次の依頼から通常の思考設定）", style="cyan"))
            else:
                console.print(Text("  /fast または /fast off と入力してください。", style="dim"))
            continue
        if image_result is False and command[0] in {"/receipts", "/excel"}:
            handler = handle_receipt_folder if command[0] == "/receipts" else handle_export_folder
            request = handler(console, user_input.strip()[len(command[0]):])
            if request is None:
                continue
            user_input = request
        if image_result is False and user_input.strip().lower() == "/new":
            if last_thought is not None:
                last_thought.clear()
            last_thought = None
            harness.reset()
            pending_images.clear()
            session = Session.new()
            console.print(Text("  新しい会話を開始しました。", style="dim cyan"))
            continue
        if image_result is False and user_input.strip().lower() == "/think":
            if last_thought is None or not last_thought.characters:
                console.print(Text("  表示できる直前の思考はありません。", style="dim"))
            else:
                console.print(Rule(Text(last_thought.caption(), style="dim"), style="dim"))
                console.print(Text(display_text(last_thought.text), style="dim"))
                console.print(Rule(Text("ここまで。回答までの寄り道でした。", style="dim"), style="dim"))
                console.print()
            continue
        if image_result is False and user_input.strip().lower().split(maxsplit=1)[0] == "/think":
            console.print(Text("  /think で直前の思考を表示できます。", style="dim"))
            continue
        if image_result is False and show_slash_command(console, user_input):
            continue

        if not session.history:
            session.title = user_input.strip()[:40]
        # 送信した入力行は端末履歴に残るので、同じ文章を追加表示しない。
        # 細い線で入力と回答を区切る。枠で囲まず、端末の通常履歴に残す。
        if input_session is None:
            console.print(Rule(style="#365568", characters="─"))
        console.print()
        if last_thought is not None:
            last_thought.clear()
        content = pending_images.content(user_input)
        if pending_images.items:
            console.print(Text(f"  画像{len(pending_images.items)}枚を送信します。", style="dim cyan"))
        pending_images.clear()
        stream_panel = StreamPanel(console, fast_mode=fast_mode)
        last_thought = stream_panel.thoughts
        cancel_requested = threading.Event()

        # Asked on this thread while the status line is paused, so it never writes over the prompt.
        def on_approval(tool_name, args, risk, panel=stream_panel):
            panel.pause()
            try:
                return ask_approval(tool_name, args, risk)
            finally:
                panel.resume()

        events = iter_ui_events(harness, content, stream_panel.cancelling, cancel_requested,
                                approvals, on_approval, thinking=False if fast_mode else None)
        try:
            while True:
                try:
                    for event in events:
                        # コアは変更せず、画面に渡すイベントだけ選ぶ。
                        if event["type"] == "reasoning_delta":
                            stream_panel.thinking(event["text"])
                        elif event["type"] == "answer_delta":
                            stream_panel.feed(event["text"])
                        elif event["type"] == "tool_progress":
                            stream_panel.tool_progress(event)
                        elif event["type"] == "tool_call":
                            stream_panel.tool_call(event["name"], event.get("args") or {})
                        elif event["type"] == "error":
                            stream_panel.error(event["content"])
                        elif event["type"] == "warning":
                            stream_panel.warning(event["content"])
                        elif event["type"] == "done" and event.get("reason") not in {None, "completed"}:
                            stream_panel.mark_failed()
                    break
                except KeyboardInterrupt:
                    # 回答描画中に押されたCtrl+Cも同じ停止要求へつなぐ。
                    cancel_requested.set()
                    stream_panel.cancelling()
                    harness.cancel()
        finally:
            # 例外が起きても状態更新スレッドと途中表示は残さない。
            if sys.exc_info()[0] is not None:
                stream_panel.mark_failed()
            try:
                events.close()
            finally:
                stream_panel.close()
        # Save after every turn (SPEC 2.2), metadata included. The Harness runs in a worker
        # thread; this line is reached only after iter_ui_events has run to its end, which joins
        # that thread (Ctrl+C keeps reading the events; an error leaves the loop instead), so the
        # history is complete here.
        session.history = harness.history
        try:
            save_session(session)
        except Exception:
            # Persistence failure must not discard the in-memory conversation.
            console.print(Panel(
                "セッションを保存できませんでした。会話は続けられますが、終了すると未保存の履歴は失われます。",
                title="⚠️ 保存エラー", border_style="yellow",
            ))


if __name__ == "__main__":
    main()
