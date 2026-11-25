"""Streaming boundaries must never depend on how the model chunks its text."""
import pytest

from excudo.markdown_stream import MarkdownStream


def test_paragraph_is_available_before_the_next_paragraph_finishes():
    stream = MarkdownStream()
    assert stream.feed("最初の**段落**。\n") == []
    assert stream.feed("\n次の段落の途中") == ["最初の**段落**。\n"]
    assert stream.finish() == ["次の段落の途中"]
    assert stream.finish() == []


@pytest.mark.parametrize("fence,closer", [("```python", "```"), ("~~~~text", "~~~~~"), ("````", "````")])
def test_code_holds_blank_lines_and_emits_only_when_the_fence_is_closed(fence, closer):
    stream = MarkdownStream()
    assert stream.feed(fence + "\nfirst\n\nsecond\n") == []
    # A marker without a line ending could still become an invalid closing line.
    assert stream.feed(closer) == []
    assert stream.feed("\n") == [fence + "\nfirst\n\nsecond\n" + closer + "\n"]
    assert stream.finish() == []


def test_short_fence_and_fence_with_trailing_text_do_not_close_code():
    stream = MarkdownStream()
    text = "````python\n```\n\n```` not a closer\n"
    assert stream.feed(text) == []
    assert stream.feed("````\n") == [text + "````\n"]


def test_four_space_indented_marker_does_not_close_a_top_level_fence():
    stream = MarkdownStream()
    text = "```text\n    ```\n\nstill inside code\n"
    assert stream.feed(text) == []
    assert stream.feed("   ```\n") == [text + "   ```\n"]


def test_fenced_code_in_a_quote_or_list_is_not_split_at_blank_lines():
    for opening, closing in [(" > ```", "> ```"), ("- ```", "  ```")]:
        stream = MarkdownStream()
        code = opening + "\n\ncode\n" + closing + "\n"
        assert stream.feed(code) == [code]


def test_table_holds_header_and_rows_until_a_blank_line():
    stream = MarkdownStream()
    table = "| 列A | 列B |\n| :--- | ---: |\n| 項目 | 値 |\n"
    assert stream.feed(table) == []
    assert stream.feed("\n次の段落") == [table]
    assert stream.finish() == ["次の段落"]


def test_table_ends_on_non_table_line_without_requiring_a_blank_line():
    stream = MarkdownStream()
    table = "a | b\n--- | ---\n1 | 2\n"
    assert stream.feed(table) == []
    assert stream.feed("次の説明\n") == [table]
    assert stream.finish() == ["次の説明\n"]


def test_paragraph_before_table_and_code_is_not_lost():
    stream = MarkdownStream()
    assert stream.feed("説明\na | b\n--- | ---\n") == ["説明\n"]
    assert stream.feed("1 | 2\n```py\n") == ["a | b\n--- | ---\n1 | 2\n"]
    assert stream.feed("print('hello')\n```\n") == ["```py\nprint('hello')\n```\n"]


def test_single_column_table_and_escaped_pipes():
    stream = MarkdownStream()
    table = "| A |\n| --- |\nvalue\\|escaped\n"
    assert stream.feed(table) == []
    assert stream.finish() == [table]


def test_indented_code_and_loose_numbered_list_preserve_internal_blanks():
    stream = MarkdownStream()
    code = "    code\n\n    more code\n"
    assert stream.feed(code) == []
    assert stream.feed("説明\n\n") == [code, "説明\n"]
    items = "1. first\n\n2. second\n\n   continued\n\n"
    assert stream.feed(items) == []
    assert stream.feed("次の段落\n") == [items]
    assert stream.finish() == ["次の段落\n"]


@pytest.mark.parametrize("tail", ["未完の段落", "```py\n途中\n\n", "| a | b |\n| --- | --- |\n| 1 | 2", "last\r"])
def test_finish_drains_an_unterminated_tail_once_and_can_start_a_new_answer(tail):
    stream = MarkdownStream()
    assert stream.feed(tail) == []
    assert stream.finish() == [tail]
    assert stream.finish() == []
    assert stream.feed("新しい回答\n\n") == ["新しい回答\n"]


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_every_two_chunk_split_and_one_character_chunks_produce_the_same_blocks(newline):
    text = newline.join([
        "## 見出し", "", "最初の**段落**。", "", "```python", "print('a|b')", "", "```", "",
        "| A | B |", "| --- | :---: |", "| 値 | `x` |", "", "末尾",
    ])

    def collect(chunks):
        stream = MarkdownStream()
        blocks = []
        for chunk in chunks:
            blocks.extend(stream.feed(chunk))
        return blocks + stream.finish()

    expected = collect([text])
    assert len(expected) == 5
    assert collect(text) == expected
    for position in range(len(text) + 1):
        assert collect([text[:position], text[position:]]) == expected
