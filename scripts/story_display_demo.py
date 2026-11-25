"""Model-free terminal check of story color and the five-second receiving note."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rich.console import Console
from main import StreamPanel
from excudo.thinking_view import ThinkingTrace


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    console = Console()
    console.print("小話表示デモ（モデル接続なし）。待機の一言を3秒表示した後、本文を12秒間受信します。")
    panel = StreamPanel(console)
    elapsed = [0.0]
    panel.thoughts = ThinkingTrace(clock=lambda: elapsed[0])
    try:
        panel.thinking("表示確認用の思考")
        elapsed[0] = 30.0
        panel._status.refresh()
        time.sleep(3)
        for index in range(12):
            panel.feed(f"本文の受信 {index + 1}/12。")
            time.sleep(1)
    except KeyboardInterrupt:
        panel.cancelling()
    finally:
        panel.close()
    console.print("確認完了：受信開始の一言は5秒で消え、本文の受信は続きます。")


if __name__ == "__main__":
    main()
