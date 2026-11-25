"""固定の検索先だけへ接続するワーカー。検索語・キーは標準入力で受け取る。"""
from html.parser import HTMLParser
from http.client import HTTPException
import json
from pathlib import Path
import re
import sys
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

if __package__:
    from .url_reader import MAX_BYTES, PageText, PinnedHTTPS, public_destinations, validate_url
else:
    # -Iではスクリプトのディレクトリもimport対象外。信頼する同梱コードだけを追加する。
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from url_reader import MAX_BYTES, PageText, PinnedHTTPS, public_destinations, validate_url


class SearchError(ValueError):
    pass


def clean_text(value, limit):
    parser = PageText()
    parser.feed(value[:8000])
    return re.sub(r'[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]', '',
                  ' '.join(''.join(parser.parts).split()))[:limit]


def result_url(value):
    parts = urlsplit(urljoin('https://duckduckgo.com/', value))
    if parts.hostname in {'duckduckgo.com', 'html.duckduckgo.com'} and parts.path == '/l/':
        value = parse_qs(parts.query).get('uddg', [''])[0]
    elif value.startswith('//'):
        value = parts.geturl()
    return validate_url(value)


class DuckResults(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results, self.current, self.capture, self.depth = [], None, None, 0
        self.blocked, self.empty = False, False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get('class', '').split()
        if (attrs.get('id') in {'challenge-form', 'img-form'}
                or 'anomaly.js' in attrs.get('action', '')
                or any(name.startswith('anomaly-modal') for name in classes)):
            self.blocked = True
        if 'no-results' in classes or 'no-results__message' in classes:
            self.empty = True
        if self.capture and tag not in PageText.VOID:
            self.depth += 1
        if tag == 'a' and 'result__a' in classes:
            self.current = {'title': '', 'url': attrs.get('href', ''), 'snippet': ''}
            self.results.append(self.current)
            self.capture, self.depth = 'title', 1
        elif self.current and 'result__snippet' in classes:
            self.capture, self.depth = 'snippet', 1

    def handle_endtag(self, tag):
        if self.capture and tag not in PageText.VOID:
            self.depth -= 1
            if self.depth <= 0:
                self.capture = None

    def handle_data(self, data):
        if self.capture:
            self.current[self.capture] += data


def normalize_results(raw, count, duck=False):
    if not isinstance(raw, list):
        raise SearchError('SEARCH_INVALID_RESPONSE')
    results, seen = [], set()
    for item in raw:
        if not isinstance(item, dict) or not all(isinstance(item.get(k), str) for k in ('title', 'url', 'snippet')):
            raise SearchError('SEARCH_INVALID_RESPONSE')
        try:
            url = result_url(item['url']) if duck else validate_url(item['url'])
        except (ValueError, UnicodeError):
            continue
        title = clean_text(item['title'], 256)
        if title and url not in seen:
            results.append({'title': title, 'url': url, 'snippet': clean_text(item['snippet'], 1000)})
            seen.add(url)
    return {'results': results[:count], 'truncated': len(results) > count}


def search(provider, query, count=5, api_key=''):
    if (provider not in {'duckduckgo', 'brave'} or not isinstance(query, str)
            or not 1 <= len(query.strip()) <= 1000
            or any(ord(c) < 32 or ord(c) == 127 for c in query)
            or type(count) is not int or not 1 <= count <= 20):
        raise SearchError('SEARCH_INVALID_ARGUMENTS')
    headers = {'User-Agent': 'Excudo/0.1 web search', 'Accept-Encoding': 'identity', 'Connection': 'close'}
    body = None
    if provider == 'duckduckgo':
        host, method, path = 'html.duckduckgo.com', 'POST', '/html/'
        body = urlencode({'q': query, 'kl': 'wt-wt'}).encode('utf-8')
        headers.update({'Accept': 'text/html', 'Content-Type': 'application/x-www-form-urlencoded'})
    else:
        if (not isinstance(api_key, str) or not api_key or len(api_key) > 4096
                or any(ord(c) < 33 or ord(c) > 126 for c in api_key)):
            raise SearchError('SEARCH_CONFIGURATION')
        host, method = 'api.search.brave.com', 'GET'
        path = '/res/v1/web/search?' + urlencode({'q': query, 'count': count})
        headers.update({'Accept': 'application/json', 'X-Subscription-Token': api_key})
    try:
        destinations = public_destinations(host)
    except ValueError:
        raise SearchError('SEARCH_DESTINATION_BLOCKED') from None
    connection = PinnedHTTPS(host, destinations)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        if response.status in {202, 403} and provider == 'duckduckgo':
            raise SearchError('SEARCH_BLOCKED')
        if response.status == 429:
            raise SearchError('SEARCH_RATE_LIMIT')
        if response.status in {401, 403} and provider == 'brave':
            raise SearchError('SEARCH_AUTHENTICATION')
        if 300 <= response.status < 400:
            raise SearchError('SEARCH_REDIRECT_REFUSED')
        if response.status != 200:
            raise SearchError('SEARCH_UNAVAILABLE')
        expected = 'text/html' if provider == 'duckduckgo' else 'application/json'
        if (response.headers.get_content_type() != expected
                or response.headers.get('Content-Encoding', 'identity').lower() != 'identity'):
            raise SearchError('SEARCH_INVALID_RESPONSE')
        size = response.headers.get('Content-Length')
        if size and (not size.isdecimal() or int(size) > MAX_BYTES):
            raise SearchError('SEARCH_TOO_LARGE')
        data = response.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise SearchError('SEARCH_TOO_LARGE')
        text = data.decode('utf-8')
        if provider == 'duckduckgo':
            parser = DuckResults()
            parser.feed(text)
            if parser.blocked:
                raise SearchError('SEARCH_BLOCKED')
            if not parser.results and not parser.empty:
                raise SearchError('SEARCH_INVALID_RESPONSE')
            result = normalize_results(parser.results, count, duck=True)
        else:
            payload = json.loads(text)
            web = payload.get('web', {}) if isinstance(payload, dict) else None
            if not isinstance(web, dict) or not isinstance(web.get('results'), list):
                raise SearchError('SEARCH_INVALID_RESPONSE')
            raw = web['results']
            if any(not isinstance(item, dict) for item in raw):
                raise SearchError('SEARCH_INVALID_RESPONSE')
            result = normalize_results([{'title': item.get('title'), 'url': item.get('url'),
                                         'snippet': item.get('description', '')} for item in raw], count)
        return {'provider': provider, **result}
    finally:
        connection.close()


def main():
    try:
        result = search(**json.loads(sys.stdin.buffer.read(32769)))
    except SearchError as error:
        result = {'error': str(error)}
    except (OSError, HTTPException):
        result = {'error': 'SEARCH_NETWORK_ERROR'}
    except Exception:
        result = {'error': 'SEARCH_INVALID_RESPONSE'}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=True).encode('ascii'))


if __name__ == '__main__':
    main()
