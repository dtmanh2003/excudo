"""Large pages return a bounded prefix, with source and character limits distinct."""
import gzip
import json
import subprocess

import pytest

from excudo import url_reader as reader, web_fetch as fetch
from tests.test_fetch_url import Response, fake_http


@pytest.mark.parametrize('declared', [False, True])
@pytest.mark.parametrize('length', [reader.MAX_BYTES-1, reader.MAX_BYTES, reader.MAX_BYTES+1])
@pytest.mark.parametrize('media', ['text/html', 'text/plain'])
def test_wire_prefix_handles_size_boundary_without_rejecting_large_pages(monkeypatch, declared, length, media):
    # Visible text ends early, so the byte-limit warning cannot depend on max_chars.
    body=b'Visible prefix.'+b' '*(length-len(b'Visible prefix.'))
    extra={'Content-Length':str(length)} if declared else {}
    _,closed=fake_http(monkeypatch,Response(body,media=media,extra=extra))
    data=reader.read_page('https://example.com/',max_chars=20000)
    assert data['text'].startswith('Visible prefix.')
    assert data['source_truncated'] is (length>reader.MAX_BYTES)
    assert data['truncated'] is (media=='text/plain')
    assert closed==[True]


def test_html_parser_never_receives_the_lookahead_or_unread_tail(monkeypatch):
    prefix=b'<main><p>Prefix</p>'+b' '*(reader.MAX_BYTES-len(b'<main><p>Prefix</p>'))
    fake_http(monkeypatch,Response(prefix+b'NEVER PARSED</main>'))
    original=reader.PageText
    parsed=[]
    class ObservedParser(original):
        def feed(self,text):
            parsed.append(text)
            return super().feed(text)
    monkeypatch.setattr(reader,'PageText',ObservedParser)
    data=reader.read_page('https://example.com/')
    assert data['text'].strip()=='Prefix' and data['source_truncated'] is True
    assert len(parsed)==1 and len(parsed[0])==reader.MAX_BYTES
    assert 'NEVER PARSED' not in parsed[0]


def test_gzip_expansion_is_cut_before_html_parser_and_unread_tail(monkeypatch):
    prefix=b'<main>Prefix'+b' '*(reader.MAX_BYTES-len(b'<main>Prefix'))
    fake_http(monkeypatch,Response(gzip.compress(prefix+b'UNREAD TAIL</main>'),extra={'Content-Encoding':'gzip'}))
    original=reader.PageText
    lengths=[]
    class ObservedParser(original):
        def feed(self,text):
            lengths.append(len(text))
            assert 'UNREAD TAIL' not in text
            return super().feed(text)
    monkeypatch.setattr(reader,'PageText',ObservedParser)
    data=reader.read_page('https://example.com/')
    assert data['text'].strip()=='Prefix' and data['source_truncated'] is True
    assert lengths==[reader.MAX_BYTES]


@pytest.mark.parametrize('source_cut', [False, True])
@pytest.mark.parametrize('text_cut', [False, True])
def test_tool_reports_source_and_character_truncation_independently(monkeypatch, source_cut, text_cut):
    data={'url':'https://example.com/', 'text':'Visible prefix.',
          'source_truncated':source_cut, 'truncated':text_cut}
    monkeypatch.setattr(fetch,'run_command',lambda *a,**k:
                        subprocess.CompletedProcess(a,0,json.dumps(data).encode(),b''))
    result=fetch.fetch_url('https://example.com/')
    assert ('Page read truncated at the 512 KiB limit' in result) is source_cut
    assert ('Content truncated at the requested character limit' in result) is text_cut
    assert 'Visible prefix.' in result
    if source_cut:
        assert result.index('Content may be incomplete')<result.index('Visible prefix.')


@pytest.mark.parametrize('value', [None, 0, 1, 'true', [], {}])
def test_tool_rejects_invalid_source_truncation_metadata(monkeypatch,value):
    data={'url':'https://example.com/', 'text':'Prefix', 'truncated':False, 'source_truncated':value}
    monkeypatch.setattr(fetch,'run_command',lambda *a,**k:
                        subprocess.CompletedProcess(a,0,json.dumps(data).encode(),b''))
    assert fetch.fetch_url('https://example.com/').startswith('Error: Invalid URL reader response')


def test_no_readable_prefix_does_not_claim_the_whole_page_was_checked(monkeypatch):
    fake_http(monkeypatch,Response(b' '*(reader.MAX_BYTES+1)+b'Unread article'))
    data=reader.read_page('https://example.com/')
    assert 'No readable text in the first 512 KiB' in data['error']
    assert 'remaining page was not read' in data['error'] and 'text' not in data
