"""Deterministic model for Node/Python IPC integration tests, not a production mode."""
from pathlib import Path
import sys
import threading
import json
from types import SimpleNamespace as Obj

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from excudo.ipc import serve


class FixtureModel:
    def __init__(self):
        self.stopped = threading.Event()

    def cancel(self):
        self.stopped.set()

    def reset_cancel(self):
        self.stopped.clear()

    def stream(self, messages, tools, *, thinking=None):
        text = messages[-1].get('content', '') or ''
        if messages[-1]['role'] == 'tool':
            text = '完了'
        elif text == 'approve':
            call = Obj(index=0, id='fixture-call', function=Obj(name='write_file',
                       arguments=json.dumps({'path': 'created.txt', 'content': 'SYNTHETIC'})))
            yield Obj(choices=[Obj(delta=Obj(content=None, reasoning=None, tool_calls=[call]))])
            return
        elif text == 'wait':
            yield Obj(choices=[Obj(delta=Obj(content=None, reasoning='待機中', tool_calls=None))])
            self.stopped.wait(10)
            return
        else:
            text = '確認: ' + text
        for piece in [text[:2], text[2:]]:
            yield Obj(choices=[Obj(delta=Obj(content=piece, reasoning=None, tool_calls=None))])


if __name__ == '__main__':
    sink = sys.stdout
    sink.reconfigure(encoding='utf-8')
    sys.stdout = sys.stderr
    serve(sys.stdin.buffer, sink, model=FixtureModel())
