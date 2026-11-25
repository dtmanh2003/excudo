"""Playwrightでブラウザーを操作するツール（ページを開く・クリック・入力・画面の読み取り）。

Playwrightの同期APIは、作成したスレッドからしか使えない。Harnessはターンごとに別のスレッドから
ツールを呼ぶため、ブラウザーは専用スレッド1つで動かし、各操作はそのスレッドへ渡して結果を待つ。
ブラウザーは毎回新しいプロファイル（Cookie・保存済みログインなし）で起動し、ダウンロードは受け付けない。
依存関係は browser extra（uv sync --extra browser）。ブラウザー本体はPCのEdge・Chromeも使える。
"""

from concurrent.futures import Future, TimeoutError as FutureTimeout
from contextlib import contextmanager
import atexit
import ipaddress
import os
import queue
import socket
import socketserver
import select
import re
import threading
import time
from urllib.parse import parse_qsl, urljoin, urlparse

from .process_control import check_cancelled
from .security import find_internal_info, redact_secrets

NAVIGATION_TIMEOUT_MS = 15_000
ACTION_TIMEOUT_MS = 10_000
# モデルへ返す画面の量の上限。本文が長いページでも会話を埋め尽くさない。
MAX_TEXT_CHARS = 4_000
MAX_ELEMENTS = 60
MAX_FILL_CHARS = 2_000
# EXCUDO_BROWSER_HEADLESS=0 で画面を表示する（動作の確認用）。
HEADLESS = os.getenv("EXCUDO_BROWSER_HEADLESS", "1").lower() not in {"0", "false", "no"}
# 社内ネットワーク（localhost・プライベートIP・社内のホスト名）へのアクセスは既定で拒否する。
# ローカルで動作を確認するときだけ EXCUDO_BROWSER_ALLOW_LOCAL=1 で許可する。
ALLOW_LOCAL = os.getenv("EXCUDO_BROWSER_ALLOW_LOCAL", "0").lower() in {"1", "true", "yes"}
# 社内のホストに使われる名前の末尾。
_INTERNAL_SUFFIXES = (".local", ".localhost", ".internal", ".intranet", ".corp", ".lan", ".home.arpa")
# ネットワークを使わない読み込み（ページ内のデータ）。ブラウザー内部の要求として許可する。
_NO_NETWORK_SCHEMES = {"about", "data", "blob"}
# 使うブラウザー（msedge・chromeなど）。空なら Edge → Chrome → Chromium の順に試す。
BROWSER_CHANNEL = os.getenv("EXCUDO_BROWSER_CHANNEL", "").strip()

INSTALL_HINT = (
    "Browser tools need Playwright: run `uv sync --extra browser`, then install Microsoft Edge or "
    "Google Chrome, or run `uv run playwright install chromium`, and restart Excudo."
)

# 操作できる要素に番号（data-excudo-ref）を振り、画面の状態を1回の呼び出しで返す。
# 番号は読み取るたびに振り直す。入力欄の値はモデルへ返さない（入力済みの個人情報を含みうる）。
# 要素1つの説明（種類・表示名・type・リンク先・無効か）。画面の読み取りと、操作の直前の確認で
# 同じものを使う（承認したときの説明と、操作する要素の説明を同じ方法で比べるため）。
_ELEMENT_JS = """
(el) => {
  const tag = el.tagName.toLowerCase();
  const label = (el.getAttribute('aria-label') || el.innerText || el.getAttribute('placeholder')
    || el.getAttribute('name') || el.getAttribute('title') || el.getAttribute('alt') || '')
    .trim().replace(/\\s+/g, ' ').slice(0, 80);
  return {
    tag, label,
    type: (el.getAttribute('type') || '').toLowerCase(),
    href: tag === 'a' ? String(el.href).slice(0, 200) : '',
    disabled: !!el.disabled,
  };
}
"""

_SNAPSHOT_JS = """
([maxElements, maxText]) => {
  const describe = __ELEMENT_JS__;
  const selector = 'a[href], button, input:not([type=hidden]), textarea, select, '
    + '[role=button], [role=link], [role=checkbox], [role=tab], [contenteditable="true"]';
  document.querySelectorAll('[data-excudo-ref]').forEach(e => e.removeAttribute('data-excudo-ref'));
  const elements = [];
  let more = false;
  for (const el of document.querySelectorAll(selector)) {
    const style = getComputedStyle(el);
    if (!el.getClientRects().length || style.visibility === 'hidden' || style.display === 'none') continue;
    if (elements.length >= maxElements) { more = true; break; }
    const ref = elements.length + 1;
    el.setAttribute('data-excudo-ref', String(ref));
    elements.push({ref, ...describe(el)});
  }
  const text = document.body ? document.body.innerText : '';
  return {title: document.title, url: location.href, text: text.slice(0, maxText + 1), elements, more};
}
""".replace("__ELEMENT_JS__", _ELEMENT_JS.strip())

# 最後に読み取った画面（承認画面で、番号がどの要素かを示すために使う）。
_last_page: dict = {"url": "", "elements": {}}


def _resolve(host: str) -> list[str]:
    """ホスト名のIPアドレス（解決できなければ空）。テストで置き換える。"""
    try:
        return sorted({info[4][0] for info in socket.getaddrinfo(host, None)})
    except (OSError, UnicodeError):
        return []


def _is_internal_address(address: str) -> bool:
    """インターネット上のアドレスでなければ True（プライベート・ループバック・リンクローカルなど）。"""
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return not ip.is_global


# ホスト名ごとの判定結果（同じホストへの要求のたびに名前解決しない）。
_host_verdicts: dict[str, bool] = {}


def blocked_destination(url: str) -> str | None:
    """url へアクセスしてはいけない理由（社内ネットワークなど）。問題なければ None。

    ページを開くときと、ブラウザーが出すすべての要求（リダイレクト・画像・スクリプト・フォーム送信）
    に使う。ホスト名はIPアドレスに解決して確かめる（社内のアドレスを指す外部の名前も拒否する）。
    """
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme in _NO_NETWORK_SCHEMES:
        return None
    if scheme not in {"http", "https", "ws", "wss"}:
        return f"{scheme or 'this kind of'} URLs are not opened"
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return "the URL has no host"
    if ALLOW_LOCAL:
        return None
    if host == "localhost" or host.endswith(_INTERNAL_SUFFIXES):
        return "internal host names (localhost, .local, .corp, ...) are not opened"
    try:
        ipaddress.ip_address(host)
        literal = True
    except ValueError:
        literal = False
    if literal:
        return "internal network addresses are not opened" if _is_internal_address(host) else None
    if "." not in host:
        return "host names without a domain (intranet names) are not opened"
    if host not in _host_verdicts:
        _host_verdicts[host] = any(_is_internal_address(address) for address in _resolve(host))
    return "this host name points to an internal network address" if _host_verdicts[host] else None


# Requests rejected by the browser guard, for user-facing navigation errors.
_blocked_requests: list[str] = []


class _NetworkProxy:
    """Validate and pin destination IPs without issuing requests on the browser's behalf.

    Chromium handles redirects, cookies and form methods normally. Each connection
    passes through this local proxy; DNS is checked at connection time, not cached.
    """

    def __init__(self):
        owner = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                owner._handle(self.request)

        self._closed = threading.Event()
        self._server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"

    def close(self):
        self._closed.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def _handle(self, client):
        upstream = None
        hostname = "unknown host"
        try:
            client.settimeout(10)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = client.recv(4096)
                if not chunk:
                    return
                data += chunk
                if len(data) > 65536:
                    return
            header, tail = data.split(b"\r\n\r\n", 1)
            lines = header.split(b"\r\n")
            method, target, version = lines[0].decode("ascii").split(" ", 2)
            url = "https://" + target if method == "CONNECT" else target
            parsed = urlparse(url)
            hostname = parsed.hostname or hostname
            reason = blocked_destination(url)
            if reason:
                raise ValueError(reason)
            addresses = _resolve(parsed.hostname or "")
            if not addresses or (not ALLOW_LOCAL and any(_is_internal_address(ip) for ip in addresses)):
                raise ValueError("destination is unresolved or points to an internal network address")
            port = parsed.port or (443 if method == "CONNECT" else 80)
            for address in addresses:
                try:
                    upstream = socket.create_connection((address, port), timeout=10)
                    break
                except OSError:
                    continue
            if upstream is None:
                raise OSError("destination could not be reached")
            if method == "CONNECT":
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if tail:
                    upstream.sendall(tail)
            else:
                path = parsed._replace(scheme="", netloc="", fragment="").geturl() or "/"
                # One HTTP request per connection, so later requests cannot change hosts
                # inside an already-approved tunnel. HTTPS tunnels remain host-bound.
                kept = [line for line in lines[1:] if line.split(b":", 1)[0].lower()
                        not in {b"connection", b"proxy-connection", b"proxy-authorization"}]
                request = f"{method} {path} {version}".encode("ascii")
                upstream.sendall(b"\r\n".join([request, *kept, b"Connection: close"]) + b"\r\n\r\n" + tail)
            client.settimeout(None)
            upstream.settimeout(None)
            while not self._closed.is_set():
                readable, _, _ = select.select([client, upstream], [], [], 0.2)
                for source in readable:
                    chunk = source.recv(65536)
                    if not chunk:
                        return
                    (upstream if source is client else client).sendall(chunk)
        except ValueError as error:
            _blocked_requests.append(f"{hostname}: {error}")
            try:
                client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
        except (OSError, UnicodeError):
            pass
        finally:
            if upstream is not None:
                upstream.close()


def _guard_request(route):
    reason = blocked_destination(route.request.url)
    if reason:
        _blocked_requests.append(f"{urlparse(route.request.url).hostname}: {reason}")
        route.abort("blockedbyclient")
    else:
        route.continue_()


def _guard_websocket(websocket) -> None:
    """WebSocket も同じ確認をする（通常の要求とは別に扱われるため）。"""
    if blocked_destination(websocket.url):
        websocket.close()
    else:
        websocket.connect_to_server()


def _launch_browser(playwright):
    """ブラウザーを起動する。EXCUDO_BROWSER_CHANNEL（msedge・chromeなど）の指定があればそれを使う。

    指定がなければPCにインストール済みのMicrosoft Edge、Google Chromeの順に使い（ブラウザー本体の
    追加ダウンロードが不要）、どちらもなければPlaywrightのChromiumを使う。
    """
    options = {"headless": HEADLESS}
    if BROWSER_CHANNEL:
        return playwright.chromium.launch(channel=BROWSER_CHANNEL, **options)
    errors = []
    for channel in ("msedge", "chrome", None):
        try:
            return playwright.chromium.launch(**({"channel": channel} if channel else {}), **options)
        except Exception as error:
            errors.append(error)
    raise errors[0]


@contextmanager
def _launch_playwright():
    """Chromiumを起動し、現在のページを返す関数を渡す。新しいタブが開いたらそちらを操作対象にする。"""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        proxy = _NetworkProxy()
        try:
            # 権限（位置情報・カメラ・通知など）は与えない。Service Worker は要求の確認を
            # 迂回しうるので使わせない。
            context = browser.new_context(accept_downloads=False, permissions=[], service_workers="block",
                                          proxy={"server": proxy.url, "bypass": "<-loopback>"})
            context.route("**/*", _guard_request)
            # すべての要求（リダイレクト・画像・スクリプト・フォーム送信）を確認し、社内宛ては止める。
            if hasattr(context, "route_web_socket"):
                context.route_web_socket("**", _guard_websocket)
            context.set_default_timeout(ACTION_TIMEOUT_MS)
            context.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
            pages = [context.new_page()]
            context.on("page", lambda page: pages.append(page))

            def current_page():
                open_pages = [page for page in pages if not page.is_closed()]
                if not open_pages:
                    open_pages = [context.new_page()]
                pages[:] = open_pages
                return open_pages[-1]

            yield current_page
        finally:
            try:
                browser.close()
            finally:
                proxy.close()


class _BrowserThread:
    """ブラウザーを1つの専用スレッドで動かし、他のスレッドからの操作を順番に実行する。"""

    def __init__(self, launch=_launch_playwright):
        self._launch = launch
        self._jobs: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def run(self, action, timeout: float):
        """action(page) をブラウザーのスレッドで実行し、結果を返す。停止要求があれば待つのをやめる。"""
        future: Future = Future()
        # 登録と起動を同じロックの中で行う。起動に失敗したスレッドが待ち行列を空にした後の操作は、
        # 必ず新しいスレッドが受け取る（取り残されて時間切れにならない）。
        with self._lock:
            self._jobs.put((action, future))
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="excudo-browser", daemon=True)
                self._thread.start()
        deadline = time.monotonic() + timeout
        while True:
            try:
                check_cancelled()
            except RuntimeError:
                future.cancel()  # まだ始まっていなければ実行しない
                raise
            try:
                return future.result(timeout=0.2)
            except FutureTimeout:
                if time.monotonic() > deadline:
                    future.cancel()
                    raise TimeoutError("the browser did not respond in time") from None

    def close(self) -> None:
        with self._lock:
            thread = self._thread
        if thread is not None and thread.is_alive():
            self._jobs.put(None)
            thread.join(timeout=5)

    def _loop(self) -> None:
        try:
            with self._launch() as current_page:
                while True:
                    job = self._jobs.get()
                    if job is None:
                        return
                    action, future = job
                    if not future.set_running_or_notify_cancel():
                        continue
                    try:
                        future.set_result(action(current_page()))
                    except BaseException as error:  # 結果として呼び出し元へ返す
                        future.set_exception(error)
        except Exception as error:
            # 起動できない（Chromium未インストールなど）：待っている操作すべてにエラーを返す。
            # スレッドは終了し、次の操作で起動をやり直す。
            first_line = str(error).strip().splitlines()[0] if str(error).strip() else type(error).__name__
            failure = RuntimeError(f"Could not start the browser ({first_line}). {INSTALL_HINT}")
            with self._lock:
                self._thread = None
                while True:
                    try:
                        job = self._jobs.get_nowait()
                    except queue.Empty:
                        return
                    if job is not None and job[1].set_running_or_notify_cancel():
                        job[1].set_exception(failure)


_browser = _BrowserThread()
atexit.register(lambda: _browser.close())


def _playwright_installed() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


def _run(action, timeout: float = (NAVIGATION_TIMEOUT_MS + ACTION_TIMEOUT_MS) / 1000 + 5) -> str:
    if not _playwright_installed():
        return f"Error: {INSTALL_HINT}"
    return _browser.run(action, timeout)


def _describe(element: dict) -> str:
    kind = element["tag"] + (f"[{element['type']}]" if element.get("type") else "")
    text = f'{kind} "{element["label"]}"' if element.get("label") else kind
    if element.get("href"):
        text += f" -> {element['href']}"
    if element.get("disabled"):
        text += " (disabled)"
    return text


def _snapshot(page) -> str:
    """画面の状態（タイトル・URL・番号付きの操作できる要素・表示中の文章）をモデル向けの文字列にする。"""
    state = page.evaluate(_SNAPSHOT_JS, [MAX_ELEMENTS, MAX_TEXT_CHARS])
    elements = state.get("elements", [])
    _last_page["url"] = state.get("url", "")
    _last_page["elements"] = {element["ref"]: _describe(element) for element in elements}
    lines = [f"Page: {state.get('title', '')}", f"URL: {state.get('url', '')}", ""]
    if elements:
        lines.append("Interactive elements (pass the number as `ref` to browser_click / browser_fill):")
        lines += [f"[{element['ref']}] {_describe(element)}" for element in elements]
        if state.get("more"):
            lines.append("(more elements not shown)")
    else:
        lines.append("No interactive elements found.")
    text = state.get("text", "")
    lines += ["", "Visible text:", text[:MAX_TEXT_CHARS] or "(none)"]
    if len(text) > MAX_TEXT_CHARS:
        lines.append("(text truncated)")
    return "\n".join(lines)


def _settle(page) -> None:
    """クリックや送信の後、ページの読み込みを少し待つ（遷移しない操作では待たずに進む）。"""
    try:
        page.wait_for_load_state("domcontentloaded", timeout=5_000)
    except Exception:
        pass


def _valid_ref(ref) -> int:
    if isinstance(ref, bool) or not isinstance(ref, int) or ref < 1:
        raise ValueError("ref must be a positive element number from the latest page snapshot")
    return ref


def _target(page, ref: int):
    target = page.locator(f'[data-excudo-ref="{ref}"]')
    if target.count() == 0:
        raise ValueError(f"No element [{ref}] on the current page; call browser_read to get the current numbers")
    return target.first


def _approved_target(page, ref: int):
    """The element [ref], only if it is still the one shown when the action was approved.

    The approval describes [ref] from the page read last. A page can change afterwards (scripts,
    another page): the click or the fill must never hit something the user did not see.
    """
    approved = _last_page["elements"].get(ref)
    if approved is None:
        raise ValueError(f"[{ref}] is not in the page read last; call browser_read and use its numbers")
    if page.url != _last_page["url"]:
        raise ValueError("the browser is on another page than the one read last; call browser_read again")
    target = _target(page, ref)
    current = _describe(target.evaluate(_ELEMENT_JS))
    if current != approved:
        raise ValueError(f"the page changed since it was read: [{ref}] was {approved}, it is now {current}; "
                         "call browser_read again")
    return target


# URL のパラメーター名・入力欄の名前で、秘密情報を表すもの。
_SECRET_NAME = re.compile(
    r"pass(word|wd|code)?|secret|token|api[_\- ]?key|apikey|access[_\- ]?key|credential|auth|session|"
    r"\botp\b|one[_\- ]?time|\bpin\b|cvv|cvc|security[_\- ]?code|"
    r"パスワード|暗証|トークン|秘密|認証コード|ワンタイム",
    re.I,
)


def _outbound_problem(text: str) -> str | None:
    """外部へ送ってはいけない内容（鍵・トークン・パスワード、社内のアドレス・パス）が text にあれば理由。

    ページを開く URL と、ページに入力する文字に使う。値そのものは返さない。
    """
    if redact_secrets(text) != text:
        return "it looks like it contains a key, token or password"
    kinds = {finding.split(" ", 1)[0] for finding in find_internal_info(text)}
    if kinds:
        names = {"IP": "an internal IP address", "host": "an internal host name", "path": "a local file path",
                 "credential": "a credential"}
        return "it contains " + ", ".join(names.get(kind, kind) for kind in sorted(kinds))
    return None


def _secret_parameters(url: str) -> list[str]:
    """値のある、秘密情報を表す名前の URL パラメーター（?api_key=... など）。"""
    return [name for name, value in parse_qsl(urlparse(url).query, keep_blank_values=False)
            if value and _SECRET_NAME.search(name)]


def _looks_like_card_number(text: str) -> bool:
    """13〜19桁でLuhnの検査を通る数字（クレジットカード番号の可能性）。"""
    digits = re.sub(r"[ \-]", "", text)
    if not digits.isdigit() or not 13 <= len(digits) <= 19:
        return False
    total = 0
    for index, digit in enumerate(reversed(digits)):
        value = int(digit)
        if index % 2:
            value = value * 2 - 9 if value > 4 else value * 2
        total += value
    return total % 10 == 0


def browser_open(url: str) -> str:
    """http/httpsのページを開き、画面の状態を返す。"""
    url = url.strip() if isinstance(url, str) else ""
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return "Error: only http:// and https:// URLs can be opened"
    if parsed.username or parsed.password:
        return "Error: URLs containing a user name or password are not opened"
    reason = blocked_destination(url)
    if reason:
        return f"Error: {reason} (Excudo's browser does not access the internal network)"
    # The host was checked above; the rest of the URL (path, query, fragment) must not carry secrets.
    problem = _outbound_problem(parsed._replace(scheme="", netloc="").geturl())
    if problem or _secret_parameters(url):
        detail = problem or f"its parameters {', '.join(_secret_parameters(url))} carry secrets"
        return f"Error: the URL is not opened: {detail}. Do not put secrets or internal information into URLs."

    def action(page):
        _blocked_requests.clear()
        try:
            page.goto(url, wait_until="domcontentloaded")
        except Exception as error:
            if _blocked_requests:  # e.g. redirected to an internal address
                return f"Error: the page was not opened: {_blocked_requests[0]}"
            raise error
        return _snapshot(page)

    return _run(action)


def browser_click(ref: int) -> str:
    """最後に読み取った画面の番号の要素をクリックし、クリック後の画面の状態を返す。"""
    ref = _valid_ref(ref)

    def action(page):
        _approved_target(page, ref).click()
        _settle(page)
        return f"Clicked [{ref}].\n\n" + _snapshot(page)

    return _run(action)


def browser_fill(ref: int, text: str, submit: bool = False) -> str:
    """入力欄に文字を入力する（選択欄は表示名で選ぶ）。submit=TrueならEnterで送信する。

    パスワード・カード番号・ファイル選択は入力しない。利用者が自分で入力する。
    """
    ref = _valid_ref(ref)
    if not isinstance(text, str):
        return "Error: text must be a string"
    if len(text) > MAX_FILL_CHARS:
        return f"Error: text is longer than {MAX_FILL_CHARS} characters"
    if _looks_like_card_number(text):
        return "Error: this looks like a payment card number; ask the user to enter it themselves"
    problem = _outbound_problem(text)
    if problem:
        return (f"Error: not typed: {problem}. Pages are outside the company; "
                "do not send secrets or internal information to them.")

    def action(page):
        target = _approved_target(page, ref)
        tag, kind, autocomplete, names = target.evaluate(
            "e => [e.tagName.toLowerCase(), (e.getAttribute('type') || '').toLowerCase(),"
            " (e.getAttribute('autocomplete') || '').toLowerCase(),"
            " [e.getAttribute('name'), e.id, e.getAttribute('placeholder'), e.getAttribute('aria-label'),"
            "  ...Array.from(e.labels || [], l => l.innerText)].filter(Boolean).join(' ')]"
        )
        if kind == "password" or "password" in autocomplete or autocomplete.startswith("cc-"):
            return "Error: refusing to type into a password or payment field; ask the user to enter it themselves"
        if _SECRET_NAME.search(names or "") or autocomplete == "one-time-code":
            return ("Error: refusing to type into a field for a secret (password, token, API key, code); "
                    "ask the user to enter it themselves")
        if kind == "file":
            return "Error: file upload fields are not supported"
        if tag == "select":
            target.select_option(label=text)
        else:
            target.fill(text)
        if submit:
            target.press("Enter")
            _settle(page)
        return f"Filled [{ref}]{' and submitted' if submit else ''}.\n\n" + _snapshot(page)

    return _run(action)


def browser_read() -> str:
    """今開いているページの状態を読み取る（ページの再読み込みはしない）。"""

    def action(page):
        if page.url in {"", "about:blank"}:
            return "No page is open. Use browser_open first."
        return _snapshot(page)

    return _run(action)


def approval_note(tool_name: str, args: dict) -> str | None:
    """承認画面に添える説明：クリック・入力する番号が、最後に読み取った画面のどの要素か。"""
    if tool_name not in {"browser_click", "browser_fill"}:
        return None
    element = _last_page["elements"].get(args.get("ref"))
    page = _last_page["url"] or "（不明）"
    if element is None:
        return f"対象: [{args.get('ref')}]（最後に読み取った画面にない番号です）  ページ: {page}"
    return f"対象: [{args.get('ref')}] {element}  ページ: {page}"
