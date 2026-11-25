"""単独HTTPSワーカー。検証済みIPへ接続し、Host/SNI/証明書検証は元のホスト名を使う。"""
from html.parser import HTMLParser
from http.client import HTTPSConnection, HTTPException
import ipaddress
import json
import re
import socket
import ssl
import sys
import zlib
from urllib.parse import parse_qsl, quote, unquote, urljoin, urlsplit, urlunsplit

MAX_BYTES = 512 * 1024
MAX_REDIRECTS = 3
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
SECRET_PARAMETER = re.compile(r"(?:api[_-]?key|(?:access|refresh|auth|id)[_-]?token|token|secret|password|pwd|auth|authorization|credential|signature|cookie|session(?:[_-]?id)?)$", re.I)


def public_address(value):
    try:
        address = ipaddress.ip_address(value)
        return (address.is_global and not address.is_multicast and not address.is_reserved
                and not (isinstance(address, ipaddress.IPv6Address)
                         and (address.ipv4_mapped or address.sixtofour or address.teredo)))
    except ValueError:
        return False


def validate_url(url):
    if (not isinstance(url, str) or not 1 <= len(url) <= 2048
            or any(ord(c) <= 32 or ord(c) == 127 for c in url) or "\\" in url):
        raise ValueError("URL must be a single HTTPS URL, at most 2048 characters, without whitespace or controls.")
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.port not in (None, 443):
        raise ValueError("Only HTTPS on port 443 is supported.")
    decoded = url
    for _ in range(4):
        candidate = urlsplit(decoded)
        if (candidate.username is not None or candidate.password is not None
                or any(SECRET_PARAMETER.search(key) for key, _ in parse_qsl(candidate.query, keep_blank_values=True))):
            raise ValueError("URLs containing credentials or credential parameters are refused.")
        decoded = unquote(decoded)
    host = parts.hostname.rstrip('.').lower().encode('idna').decode('ascii')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        labels = host.split('.')
        if (len(host) > 253 or len(labels) < 2 or host.endswith(('.local', '.localhost', '.internal', '.corp', '.lan'))
                or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)):
            raise ValueError("Only public hostnames are supported.")
    else:
        if '%' in host or not public_address(host):
            raise ValueError("Private, local and special IP addresses are refused.")
    authority = f"[{host}]" if ':' in host else host
    # フラグメントは送信しない。日本語のパスもHTTPのASCII表現へ変換する。
    return urlunsplit(('https', authority, quote(parts.path or '/', safe="/%:@!$&'()*+,;=-._~"),
                       quote(parts.query, safe="=&;%:@!$'()*+,-._~/?:"), ''))


def public_destinations(host):
    records = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not records or any(not public_address(record[4][0]) for record in records):
        raise ValueError("DNS returned a private, local or special destination; no connection was made.")
    destinations = []
    for family, _, _, _, address in records:
        target = (address[0], 443) if family == socket.AF_INET else (address[0], 443, 0, 0)
        entry = (family, target)
        if family in (socket.AF_INET, socket.AF_INET6) and entry not in destinations:
            destinations.append(entry)
    if not destinations:
        raise ValueError("No supported public destination was found.")
    return sorted(destinations, key=lambda item: item[0] != socket.AF_INET)[:4]


class PinnedHTTPS(HTTPSConnection):
    def __init__(self, host, destinations):
        self.destinations = destinations
        self.tls = ssl.create_default_context()
        super().__init__(host, timeout=5, context=self.tls)

    def connect(self):
        for family, target in self.destinations:
            raw = socket.socket(family, socket.SOCK_STREAM)
            raw.settimeout(3)
            try:
                raw.connect(target)  # 数値IPのみに接続。ここでDNSを再解決しない。
                self.sock = self.tls.wrap_socket(raw, server_hostname=self.host)
                return
            except OSError:
                raw.close()
        raise OSError("Could not connect to a validated destination with verified TLS.")


class PageText(HTMLParser):
    SKIP = {'head', 'script', 'style', 'noscript', 'nav', 'footer', 'form', 'svg', 'aside'}
    BLOCK = {'p', 'div', 'br', 'li', 'h1', 'h2', 'h3', 'h4', 'pre', 'tr', 'section', 'article'}
    VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}
    NOISE = {'related-posts', 'related-articles', 'recommended-posts', 'sidebar', 'advertisement',
             'newsletter', 'social-share', 'cookie-banner', 'ads'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.parts, self.main_parts, self.articles = [], [], [], []
        self.title, self.time_values, self.tables = [], [], []
        self.has_main = False

    @property
    def hidden(self):
        return bool(self.stack and self.stack[-1]['hidden'])

    @property
    def pre(self):
        return any(frame['tag'] == 'pre' for frame in self.stack)

    def _append(self, text):
        self.parts.append(text)
        if any(frame['main'] for frame in self.stack):
            self.main_parts.append(text)

    def add(self, text):
        if self.tables and self.tables[-1]['cell'] is not None:
            self.tables[-1]['cell'].append(text)
        else:
            self._append(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        names = set((attrs.get('class') or '').lower().split()) | {(attrs.get('id') or '').lower()}
        # Reactのstreaming SSRは完成したHTMLをhiddenなS:nへ送り、後で本文へ移す。
        # 静的テキストだけを記事候補として読む。移動用scriptや任意のhidden要素は実行・採用しない。
        segment = tag == 'div' and 'hidden' in attrs and bool(re.fullmatch(r'S:\d+', attrs.get('id') or ''))
        hidden = (self.hidden or tag in self.SKIP or bool(names & self.NOISE)
                  or attrs.get('role') in {'navigation', 'complementary', 'contentinfo'}
                  or ('hidden' in attrs and not segment) or attrs.get('aria-hidden') == 'true')
        main = not hidden and (tag in {'main', 'article'} or attrs.get('role') == 'main' or segment)
        self.has_main |= main
        if tag not in self.VOID:
            self.stack.append({'tag': tag, 'hidden': hidden,
                               'main': main, 'segment': segment,
                               'start': len(self.parts)})
        if hidden:
            return
        if tag == 'table':
            self.tables.append({'row': [], 'cell': None, 'header': False, 'header_emitted': False})
        elif tag == 'tr' and self.tables:
            self.tables[-1].update(row=[], header=False)
        elif tag in {'th', 'td'} and self.tables:
            self.tables[-1]['cell'] = []
            self.tables[-1]['header'] |= tag == 'th'
        elif tag == 'time' and attrs.get('datetime') and len(self.time_values) < 12:
            self.time_values.append(attrs['datetime'][:100])
        if attrs.get('data-timezone') and len(self.time_values) < 12:
            self.time_values.append('timezone=' + attrs['data-timezone'][:100])
        if tag in self.BLOCK and tag != 'tr':
            self.add('\n')

    def handle_endtag(self, tag):
        if not self.hidden:
            if self.tables and tag in {'td', 'th'}:
                table = self.tables[-1]
                table['row'].append(' '.join(''.join(table['cell'] or []).split()).replace('|', '\\|'))
                table['cell'] = None
            elif self.tables and tag == 'tr':
                table = self.tables[-1]
                if table['row']:
                    self.add('\n| ' + ' | '.join(table['row']) + ' |\n')
                    if table['header'] and not table['header_emitted']:
                        self.add('| ' + ' | '.join('---' for _ in table['row']) + ' |\n')
                        table['header_emitted'] = True
            elif self.tables and tag == 'table':
                self.tables.pop()
            elif tag in self.BLOCK:
                self.add('\n')
        matching = [index for index, frame in enumerate(self.stack) if frame['tag'] == tag]
        if matching:
            index = matching[-1]
            frame = self.stack[index]
            if (tag == 'article' or frame['segment']) and not frame['hidden']:
                self.articles.append(''.join(self.parts[frame['start']:]))
            del self.stack[index:]

    def handle_data(self, data):
        if self.stack and self.stack[-1]['tag'] == 'title':
            self.title.append(data[:512])
        if not self.hidden:
            self.add(data if self.pre else re.sub(r'\s+', ' ', data))

    def text(self):
        # 空のmainとフッターだけのJSアプリを、記事の取得成功として返さない。
        main = ''.join(self.main_parts) if self.has_main else ''.join(self.parts)
        article = max(self.articles, key=len, default='')
        # 本文が大半を占める記事だけを優先。記事一覧や複数の同格の記事は切り捨てない。
        return article if len(article) >= 300 and len(article) >= len(main) * .75 else main


def decompress_gzip(data, input_truncated=False):
    """Return a bounded prefix and whether unread input/output remains."""
    chunks, total = [], 0
    try:
        while True:
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            chunk = decoder.decompress(data, MAX_BYTES - total + 1)
            total += len(chunk)
            chunks.append(chunk)
            if total > MAX_BYTES:
                return b''.join(chunks)[:MAX_BYTES], True
            if not decoder.eof:
                # Only our explicit wire limit permits an unfinished gzip stream.
                # Small incomplete responses still fail rather than looking complete.
                if input_truncated:
                    return b''.join(chunks), True
                raise ValueError('Invalid or incomplete gzip response; no page text was returned.')
            data = decoder.unused_data
            if not data:
                return b''.join(chunks), input_truncated
    except zlib.error:
        raise ValueError('Invalid or incomplete gzip response; no page text was returned.') from None


def read_page(url, max_chars=6000):
    url = validate_url(url)
    if type(max_chars) is not int or not 1 <= max_chars <= 20000:
        raise ValueError("max_chars must be an integer from 1 to 20000.")
    current, redirects, visited = url, [], {url}
    for hop in range(MAX_REDIRECTS + 1):
        parts = urlsplit(current)
        # Every hop resolves again, rejects mixed/private answers and pins the checked IP.
        connection = PinnedHTTPS(parts.hostname, public_destinations(parts.hostname))
        try:
            path = parts.path + ('?' + parts.query if parts.query else '')
            connection.request('GET', path, headers={'User-Agent': 'Excudo/0.1 URL reader',
                               'Accept': 'text/html, text/plain', 'Accept-Encoding': 'identity', 'Connection': 'close'})
            response = connection.getresponse()
            if response.status in REDIRECT_STATUSES:
                location = response.headers.get('Location')
                # Check before urljoin: URL parsers can silently discard controls.
                if (not isinstance(location, str) or not 1 <= len(location) <= 2048
                        or any(ord(c) <= 32 or ord(c) == 127 for c in location) or "\\" in location):
                    return {'error': 'Redirect refused: missing or invalid Location.'}
                try:
                    target = validate_url(urljoin(current, location))
                except ValueError:
                    return {'error': 'Redirect refused: destination must be public HTTPS without credentials.'}
                if target in visited:
                    return {'error': 'Redirect loop refused.'}
                if hop == MAX_REDIRECTS:
                    return {'error': f'Redirect limit exceeded ({MAX_REDIRECTS}).'}
                visited.add(target)
                redirects.append(target)
                current = target
                continue  # finally closes this response before the next DNS lookup.
            if response.status != 200:
                return {'error': f'HTTP {response.status}; the page was not read. Login/CAPTCHA is not handled.'}
            media = response.headers.get_content_type()
            if media not in {'text/html', 'text/plain'}:
                return {'error': 'Only HTML or plain text is supported; PDF/binary/JavaScript pages are not processed.'}
            encoding = ','.join(response.headers.get_all('Content-Encoding', ['identity'])).strip().lower()
            if encoding not in {'identity', 'gzip'}:
                return {'error': 'Unsupported Content-Encoding; only identity and gzip are supported.'}
            size = response.headers.get('Content-Length')
            if size and not size.isdecimal():
                return {'error': 'Invalid Content-Length.'}
            # One lookahead byte detects a larger response even without Content-Length.
            # Neither parsing nor decompression receives that byte.
            body = response.read(MAX_BYTES + 1)
            source_truncated = len(body) > MAX_BYTES
            body = body[:MAX_BYTES]
            if encoding == 'gzip':
                try:
                    body, source_truncated = decompress_gzip(body, source_truncated)
                except ValueError as error:
                    return {'error': str(error)}
            try:
                text = body.decode(response.headers.get_content_charset() or 'utf-8', errors='replace')
            except (LookupError, ValueError):
                text = body.decode('utf-8', errors='replace')
            details = {}
            if media == 'text/html':
                parser = PageText()
                parser.feed(text)
                text = parser.text()
                details = {'title': ''.join(parser.title)[:512], 'time_values': parser.time_values}
            text = re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', text.replace('\r\n', '\n').replace('\r', '\n'))
            if media == 'text/html':
                text = re.sub(r'(?m)^[ \t]+$', '', text)
                text = re.sub(r'\n{3,}', '\n\n', text).strip('\n')
            if not text.strip():
                if source_truncated:
                    return {'error': 'No readable text in the first 512 KiB; the remaining page was not read. JavaScript is not executed.'}
                return {'error': 'No readable text found. JavaScript is not executed.'}
            return {'url': url, 'final_url': current, 'redirect_urls': redirects,
                    'text': text[:max_chars], 'truncated': len(text) > max_chars,
                    'source_truncated': source_truncated,
                    'media_type': media, **details}
        finally:
            connection.close()


if __name__ == '__main__':
    try:
        result = read_page(**json.loads(sys.stdin.buffer.read(16385)))
    except ValueError as error:
        result = {'error': str(error)}
    except (OSError, HTTPException):
        result = {'error': 'Connection, DNS, timeout or TLS verification failed.'}
    except Exception:
        result = {'error': 'Unsupported page or invalid worker input.'}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=True).encode('ascii'))
