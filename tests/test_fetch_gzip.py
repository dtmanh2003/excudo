"""Gzip stays bounded on the wire and during decompression, before HTML parsing."""
import gzip
from random import Random

import pytest

from excudo import url_reader as reader
from tests.test_fetch_url import Response, fake_http
from tests.test_fetch_redirects import redirect_http


@pytest.mark.parametrize('encoding', ['gzip', 'GZip', ' gzip '])
def test_gzip_html_is_read_without_running_scripts_or_navigation(monkeypatch, encoding):
    html='<head><title>日本語</title></head><nav>menu</nav><main><h1>Python &amp; gzip</h1><p>Tiếng Việt</p><script>bad()</script></main>'
    requests,closed=fake_http(monkeypatch,Response(gzip.compress(html.encode()),extra={'Content-Encoding':encoding}))
    result=reader.read_page('https://example.com/')
    assert result['text']=='Python & gzip\n\nTiếng Việt' and result['title']=='日本語'
    assert requests==['/'] and closed==[True]


def test_gzip_plain_text_keeps_charset_indentation_and_character_limit(monkeypatch):
    text='  if True:\n    print("café")\n'
    body=gzip.compress(text.encode('iso-8859-1'))
    fake_http(monkeypatch,Response(body,media='text/plain; charset=iso-8859-1',extra={
        'Content-Encoding':'gzip','Content-Length':str(len(body))}))
    result=reader.read_page('https://example.com/',max_chars=20)
    assert result['text']==text[:20] and result['truncated'] is True


@pytest.mark.parametrize('length', [reader.MAX_BYTES-1,reader.MAX_BYTES,reader.MAX_BYTES+1])
def test_decompressed_byte_limit_is_checked_before_excerpt_and_html_parser(monkeypatch,length):
    body=gzip.compress(b'x'*length)
    requests,closed=fake_http(monkeypatch,Response(body,extra={'Content-Encoding':'gzip'}))
    result=reader.read_page('https://example.com/',max_chars=7)
    assert result['text']=='x'*7 and result['truncated'] is True
    assert result['source_truncated'] is (length>reader.MAX_BYTES)
    assert len(body)<reader.MAX_BYTES and requests==['/'] and closed==[True]


@pytest.mark.parametrize('second_length', [reader.MAX_BYTES//2,reader.MAX_BYTES//2+1])
def test_concatenated_members_share_one_output_limit(monkeypatch,second_length):
    body=gzip.compress(b'a'*(reader.MAX_BYTES//2))+gzip.compress(b'b'*second_length)
    fake_http(monkeypatch,Response(body,media='text/plain',extra={'Content-Encoding':'gzip'}))
    result=reader.read_page('https://example.com/',max_chars=9)
    assert result['text']=='a'*9 and result['truncated']
    assert result['source_truncated'] is (second_length>reader.MAX_BYTES//2)


def test_all_valid_gzip_members_are_read_including_empty_members(monkeypatch):
    body=gzip.compress(b'')+gzip.compress('First 日本語\n'.encode())+gzip.compress(b'Second')+gzip.compress(b'')
    fake_http(monkeypatch,Response(body,media='text/plain',extra={'Content-Encoding':'gzip'}))
    assert reader.read_page('https://example.com/')['text']=='First 日本語\nSecond'


def test_bomb_uses_bounded_decompress_calls_and_never_flushes_unbounded(monkeypatch):
    body=gzip.compress(b'a'*(reader.MAX_BYTES//2))+gzip.compress(b'b'*reader.MAX_BYTES)
    fake_http(monkeypatch,Response(body,extra={'Content-Encoding':'gzip'}))
    factory=reader.zlib.decompressobj
    limits=[]
    class BoundedDecoder:
        def __init__(self,*a,**k):self.decoder=factory(*a,**k)
        def decompress(self,data,max_length=0):
            assert 0<max_length<=reader.MAX_BYTES+1
            limits.append(max_length)
            return self.decoder.decompress(data,max_length)
        def flush(self,*a,**k):pytest.fail('unbounded flush is forbidden')
        def __getattr__(self,name):return getattr(self.decoder,name)
    monkeypatch.setattr(reader.zlib,'decompressobj',BoundedDecoder)
    result=reader.read_page('https://example.com/')
    assert result['text']=='a'*6000 and result['source_truncated'] is True
    assert limits==[reader.MAX_BYTES+1,reader.MAX_BYTES//2+1]


VALID=gzip.compress(b'<main>Complete article</main>',mtime=0)
BAD_CRC=VALID[:-8]+bytes([VALID[-8]^1])+VALID[-7:]


@pytest.mark.parametrize('body', [b'',b'not gzip',VALID[:8],VALID[:-1],BAD_CRC,VALID+b'junk',VALID+gzip.compress(b'partial')[:-2]])
def test_invalid_or_truncated_stream_never_returns_partial_page(monkeypatch,body):
    _,closed=fake_http(monkeypatch,Response(body,extra={'Content-Encoding':'gzip'}))
    result=reader.read_page('https://example.com/')
    assert 'Invalid or incomplete gzip' in result['error'] and 'text' not in result
    assert closed==[True]


def test_valid_empty_gzip_is_reported_as_no_readable_text(monkeypatch):
    fake_http(monkeypatch,Response(gzip.compress(b''),extra={'Content-Encoding':'gzip'}))
    assert 'No readable text' in reader.read_page('https://example.com/')['error']


@pytest.mark.parametrize('declared',[False,True])
def test_compressed_wire_prefix_is_bounded_before_decompression(monkeypatch,declared):
    body=gzip.compress(b'Readable start.\n'+Random(0).randbytes(reader.MAX_BYTES+1),mtime=0)
    assert len(body)>reader.MAX_BYTES
    extra={'Content-Encoding':'gzip'}
    if declared:extra['Content-Length']=str(len(body))
    _,closed=fake_http(monkeypatch,Response(body,media='text/plain',extra=extra))
    original=reader.decompress_gzip
    calls=[]
    def bounded(data,input_truncated=False):
        calls.append((len(data),input_truncated))
        return original(data,input_truncated)
    monkeypatch.setattr(reader,'decompress_gzip',bounded)
    result=reader.read_page('https://example.com/')
    assert calls==[(reader.MAX_BYTES,True)]
    assert result['text'].startswith('Readable start.') and result['source_truncated'] is True
    assert closed==[True]


@pytest.mark.parametrize('encoding',['br','deflate','gzip, br','identity, gzip',''])
def test_unsupported_or_stacked_encoding_is_rejected_before_body_read(monkeypatch,encoding):
    class NoRead(Response):
        def read(self,size):pytest.fail('unsupported encoding must not read body')
    fake_http(monkeypatch,NoRead(VALID,extra={'Content-Encoding':encoding}))
    assert 'Unsupported Content-Encoding' in reader.read_page('https://example.com/')['error']


def test_duplicate_encoding_headers_cannot_bypass_the_supported_encoding_check(monkeypatch):
    response=Response(VALID,extra={'Content-Encoding':'gzip'})
    response.headers['Content-Encoding']='br'
    fake_http(monkeypatch,response)
    assert 'Unsupported Content-Encoding' in reader.read_page('https://example.com/')['error']


def test_gzip_does_not_allow_binary_media(monkeypatch):
    fake_http(monkeypatch,Response(VALID,media='application/pdf',extra={'Content-Encoding':'gzip'}))
    monkeypatch.setattr(reader,'decompress_gzip',lambda *a:pytest.fail('binary body must not be decompressed'))
    assert 'Only HTML or plain text' in reader.read_page('https://example.com/')['error']


def test_gzip_after_redirect_still_returns_original_and_checked_final_url(monkeypatch):
    requests,closed,_=redirect_http(monkeypatch,[
        Response(b'',status=301,extra={'Location':'/final'}),
        Response(VALID,extra={'Content-Encoding':'gzip'})])
    result=reader.read_page('https://example.com/')
    assert result['text']=='Complete article'
    assert result['url']=='https://example.com/' and result['final_url']=='https://example.com/final'
    assert len(requests)==len(closed)==2
