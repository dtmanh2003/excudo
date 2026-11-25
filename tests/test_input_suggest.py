"""予測候補はコマンドだけに限定し、通常の入力を補完しない。"""
import pytest
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document

from excudo.input_suggest import CommandAutoSuggest


@pytest.mark.parametrize("text,expected", [
    ("e", "xit"), ("ex", "it"), ("exi", "t"), ("E", "xit"),
    ("q", "uit"), ("exit", None), ("quit", None), ("", None),
    ("こんにちは", None), ("え", None), ("ｅ", None), ("/", None),
    ("/ex", None), ("example", None), ("e ", None), ("e\n", None),
])
def test_only_known_ascii_commands_are_suggested(text, expected):
    result = CommandAutoSuggest().get_suggestion(Buffer(), Document(text))
    assert (result.text if result else None) == expected


def test_no_suggestion_while_editing_inside_text_or_selecting():
    buffer = Buffer(document=Document("ex", cursor_position=1))
    assert CommandAutoSuggest().get_suggestion(buffer, buffer.document) is None
    buffer.cursor_position = 2
    buffer.start_selection()
    assert CommandAutoSuggest().get_suggestion(buffer, buffer.document) is None
