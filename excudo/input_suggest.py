"""英数字の終了コマンドだけをローカルで予測表示する。"""
from prompt_toolkit.auto_suggest import AutoSuggest, Suggestion


class CommandAutoSuggest(AutoSuggest):
    def get_suggestion(self, buffer, document):
        text = document.text
        # 日本語・変換中の文字・複数行・/補完は対象外。履歴も参照・保存しない。
        if (not text or not text.isascii() or not text.isalnum()
                or not document.is_cursor_at_the_end or buffer.selection_state
                or buffer.complete_state):
            return None
        for command in ("exit", "quit"):
            if command.startswith(text.lower()) and len(text) < len(command):
                return Suggestion(command[len(text):])
        return None
