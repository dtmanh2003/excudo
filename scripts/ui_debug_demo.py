"""実モデル・ツールなしでCLIを操作確認する。uv run python scripts/ui_debug_demo.pyで起動。"""

from pathlib import Path
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main as cli


class DemoHarness:
    history = []
    def __init__(self, *_args):
        self.cancelled = threading.Event()

    def cancel(self):
        self.cancelled.set()

    def reset(self):
        self.cancelled.clear()

    def run(self, text, *, thinking=None):
        self.cancelled.clear()
        # UIに見せないイベントと、待機・回答・エラーを模擬する。
        thinking_steps = 0 if thinking is False else (120 if text == "resize" else 8)
        for _ in range(thinking_steps):
            if self.cancelled.wait(0.1):
                yield {"type": "done", "reason": "interrupted"}
                return
            yield {"type": "reasoning_delta", "text": "依頼の要点を整理しています。\n表示と履歴を分ける方法を確認しています。"}
        if text == "error":
            yield {"type": "error", "content": "動作確認用の通信エラー"}
            yield {"type": "done", "reason": "error"}
            return
        yield {"type": "tool_call", "name": "HIDDEN_TOOL", "args": {}, "result": "hidden"}
        answer = ("日本語の回答が途中から流れます。**太字**も確定時に整形します。\n\n"
                  "```python\nprint('hello')\n\nprint('world')\n```\n\n"
                  "| 列A | 列B |\n| --- | --- |\n| 項目 | 値 |\n\n最後の段落です。")
        if text == "layout":
            answer = ("**不変**（イミュータブル）な値と、**変更可能**な値を比較します。\n\n"
                      "- FIRST：リストは変更可能です。\n"
                      "- SECOND：タプルは変更できません。\n"
                      "- THIRD：辞書はキーと値を対応させます。\n\n"
                      "| 項目 | list | tuple | dict | set |\n"
                      "| --- | --- | --- | --- | --- |\n"
                      "| 用途 | 順序付きのデータ | 変更しない並び | キーと値の対応 | 重複しない要素 |\n"
                      "| ハッシュ | 不可 | 中身がすべてハッシュ可能な場合に使用できます（説明末尾A） | 不可 | 不可 |\n"
                      "| 長いセル | " + "x" * 80 + " | " + "y" * 80 + " | 説明末尾B | 説明末尾C |\n\n"
                      "```python\nvalues = [\"**literal**\", \"日本語\"]\nprint(values)\n```\n\n"
                      "LAYOUT_DONE\n")
        for char in answer:
            if self.cancelled.wait(0.02):
                yield {"type": "done", "reason": "interrupted"}
                return
            yield {"type": "answer_delta", "text": char}
        yield {"type": "done", "reason": "completed"}


if __name__ == "__main__":
    cli.Model = lambda: None
    cli.save_session = lambda _session: None
    cli.Harness = DemoHarness
    cli.main()
