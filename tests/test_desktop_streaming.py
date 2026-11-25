import pytest

from excudo.app.streaming import AnswerStream


@pytest.mark.parametrize('width', [1, 2, 7, 1000])
@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_blocks_finalize_once_with_structured_previews_before_their_boundaries(width, newline):
    table = newline.join(['| A | B |', '| --- | --- |', '| short | a long cell |', ''])
    code = newline.join(['```python', 'print("<script>|safe")', '', '```', ''])
    for block, boundary in [(table, newline), (code[:-len(newline)], newline)]:
        stream = AnswerStream()
        for offset in range(0, len(block), width):
            update = stream.feed(block[offset:offset + width])
            assert update['blocks'] == []
            assert update['preview'] == ''
            if 'print' in block[:offset + width]:
                assert update['code_preview'] == block[:offset + width]
            if block is table and (newline.join(['| A | B |', '| --- | --- |', ''])) in block[:offset + width]:
                assert update['table_preview'] == block[:offset + width]
        update = stream.feed(boundary)
        assert len(update['blocks']) == 1
        assert update['preview'] == ''
        assert stream.finish()['blocks'] == []
        assert stream.finish() is None


def test_live_prose_does_not_reemit_completed_blocks():
    stream = AnswerStream()
    assert stream.feed('Hello')['preview'] == 'Hello'
    assert stream.feed(' world\n\n')['blocks'] == ['Hello world\n']
    update = stream.feed('Next paragraph')
    assert update['blocks'] == []
    assert update['preview'] == 'Next paragraph'
    assert stream.finish()['blocks'] == ['Next paragraph']


@pytest.mark.parametrize('tail', ['```py\nunfinished', '| A |\n| --- |\n| row', 'last paragraph'])
def test_finish_keeps_received_tail_once_and_resets_for_next_tool_round(tail):
    stream = AnswerStream()
    stream.feed(tail)
    assert stream.finish()['blocks'] == [tail]
    assert stream.finish() is None
    assert stream.feed('new answer\n\n')['blocks'] == ['new answer\n']


@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_waiting_counts_received_lines_and_clears_when_each_block_finishes(newline):
    stream = AnswerStream()
    table = ['| A | B |', '| --- | --- |', '| x | y |', '| z | w |']
    for count, line in enumerate(table, 1):
        update = stream.feed(line + newline)
        assert update['waiting'] == {'kind': 'table', 'lines': max(0, count - 2)}
        assert not update['preview'] and not update['blocks']
    update = stream.feed(newline)
    assert len(update['blocks']) == 1 and 'waiting' not in update
    for count, line in enumerate(['```python', 'value = "a|b"', '', 'print(value)'], 1):
        update = stream.feed(line + newline)
        assert update['waiting'] == {'kind': 'code', 'lines': count - 1}
    assert 'waiting' not in stream.feed('```' + newline)
    assert 'waiting' not in stream.finish()


@pytest.mark.parametrize('width', [1, 3, 11])
def test_count_does_not_increase_for_each_chunk_inside_the_same_row(width):
    stream = AnswerStream()
    text = '| header | value |\n| --- | --- |\n| a very long partial row'
    for offset in range(0, len(text), width):
        received = text[:offset + width]
        update = stream.feed(text[offset:offset + width])
        assert update['waiting']['lines'] == max(0, len(received.splitlines()) - 2)
    assert update['waiting'] == {'kind': 'table', 'lines': 1}


def test_ambiguous_pipe_text_has_a_generic_receiving_label_until_confirmed():
    stream = AnswerStream()
    header = stream.feed('a | b')
    assert header['waiting'] == {'kind': 'answer', 'lines': 1}
    assert 'table_preview' not in header
    confirmed = stream.feed('\n--- | ---\n')
    assert confirmed['waiting'] == {'kind': 'table', 'lines': 0}
    assert confirmed['table_preview'] == 'a | b\n--- | ---\n'


@pytest.mark.parametrize('fence', ['```python', '````text', '~~~html'])
def test_code_preview_preserves_symbols_without_reclassifying_pipes_as_a_table(fence):
    stream = AnswerStream()
    code = fence + '\n<script>literal</script> [red] [/x] 日本語 😀 | pipe'
    for character in code:
        update = stream.feed(character)
    assert update['code_preview'] == code
    assert update['waiting']['kind'] == 'code'
    assert stream.finish()['blocks'] == [code]


@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_table_preview_grows_before_completion_and_finish_keeps_the_partial_row(newline):
    stream = AnswerStream()
    text = newline.join(['Name | Value', '--- | ---', r'a\|b | <script>literal</script> 😀', '日本語 | partial'])
    confirmed = newline.join(['Name | Value', '--- | ---', ''])
    received = ''
    for character in text:
        received += character
        update = stream.feed(character)
        assert not update['blocks']
        if received.startswith(confirmed):
            assert update['table_preview'] == received
    assert stream.finish() == {'type': 'answer_update', 'blocks': [text], 'preview': ''}
    assert stream.finish() is None


@pytest.mark.parametrize('newline', ['\n', '\r\n'])
@pytest.mark.parametrize('fence', ['```python', '````text', '~~~html'])
def test_code_count_excludes_opening_fence_but_keeps_blank_and_partial_code_lines(fence, newline):
    stream = AnswerStream()
    assert stream.feed(fence)['waiting']['lines'] == 0
    assert stream.feed(newline)['waiting']['lines'] == 0
    for character in 'value = "日本語 😀"':
        assert stream.feed(character)['waiting']['lines'] == 1
    assert stream.feed(newline + newline)['waiting']['lines'] == 2
    for character in 'print(value)':
        assert stream.feed(character)['waiting']['lines'] == 3
    closing = fence[:len(fence) - len(fence.lstrip('`~'))]
    assert 'waiting' not in stream.feed(newline + closing + newline)


def test_indented_code_count_keeps_its_first_line_even_if_it_contains_backticks():
    stream = AnswerStream()
    assert stream.feed('    ```')['waiting'] == {'kind': 'code', 'lines': 1}
    assert stream.feed('\n    more')['waiting'] == {'kind': 'code', 'lines': 2}
