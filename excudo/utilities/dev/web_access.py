"""Bounded public-web retrieval. Permission decisions belong to PermissionGate.

Network work runs in this process. DNS waits and HTTP socket lifetimes are
bounded separately so a slow resolver or response cannot hold a call indefinitely.
"""

from datetime import datetime, timezone
from html.parser import HTMLParser
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

from . import config
from .results import ToolResult, sanitize_text, sanitize_tool_result


WEB_TOOLS = frozenset({"web_search", "fetch_url", "search_docs"})
_SECRET = re.compile(
    r"(?i)(?:\b(?:api[_-]?key|password|passwd|access[_-]?token|refresh[_-]?token|"
    r"token|secret|authorization|credential|signature)\b\s*[=:]\s*\S+|"
    r"\bbearer\s+\S+|\bsk-[A-Za-z0-9_-]{12,}|\bgh[pousr]_[A-Za-z0-9]{15,}|"
    r"\bAKIA[A-Z0-9]{16}\b|-----BEGIN .*PRIVATE KEY-----)"
)
_OFFICIAL_DOMAINS = {
    "python": "docs.python.org", "node": "nodejs.org", "nodejs": "nodejs.org",
    "typescript": "typescriptlang.org", "react": "react.dev",
    "rust": "doc.rust-lang.org", "go": "go.dev", "docker": "docs.docker.com",
    "fastapi": "fastapi.tiangolo.com",
}
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_DNS_SLOTS = threading.BoundedSemaphore(4)


class WebError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def outbound_has_secret(value: str) -> bool:
    """Best-effort rejection of recognizable secrets, including URL encoding."""
    if not isinstance(value, str):
        return False
    for _ in range(3):
        if (_SECRET.search(value) or sanitize_text(value) != value
                or re.search(r"https?://[^/\s]*@", value, re.I)):
            return True
        decoded = unquote(value)
        if decoded == value:
            break
        value = decoded
    return False


def safe_web_args(args: dict) -> dict:
    """Avoid echoing rejected secrets in CLI events."""
    if not isinstance(args, dict):
        return {}
    def safe(value):
        if isinstance(value, str):
            return "[REDACTED]" if outbound_has_secret(value) else value
        if isinstance(value, list):
            return [safe(item) for item in value]
        if isinstance(value, dict):
            return {key: safe(item) for key, item in value.items()}
        return value
    return {
        key: safe(value)
        for key, value in args.items()
    }


def _public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    # Exclude transition addresses that may embed or tunnel private IPv4.
    return (address.is_global and not address.is_multicast
            and not address.is_unspecified and not address.is_reserved
            and not getattr(address, "ipv4_mapped", None)
            and not getattr(address, "sixtofour", None)
            and not getattr(address, "teredo", None))


def validate_url(url: str, *, target_scope="public", allow_sensitive_query=False) -> tuple[str, str, int]:
    """Syntax and literal-host validation, without DNS or any other network IO."""
    if (not isinstance(url, str) or not url or len(url) > 4096
            or re.search(r"[\x00-\x20\x7f\\]", url)
            or re.search(r"%(?![0-9a-fA-F]{2})", url)):
        raise WebError("INVALID_URL", "Expected a well-formed public HTTP/HTTPS URL.")
    try:
        parts = urlsplit(url)
        if (parts.scheme not in {"http", "https"} or not parts.hostname
                or parts.username is not None or parts.password is not None):
            raise ValueError
        host = parts.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        port = parts.port if parts.port is not None else (443 if parts.scheme == "https" else 80)
        if not 1 <= port <= 65535 or "%" in host or parts.netloc.endswith(":"):
            raise ValueError
    except (ValueError, UnicodeError):
        raise WebError("INVALID_URL", "Only HTTP/HTTPS URLs without credentials are accepted.") from None
    if target_scope not in {"public", "loopback"}:
        raise WebError("NETWORK_TARGET_BLOCKED", "Unsupported network target scope.")
    if not allow_sensitive_query and outbound_has_secret(url):
        raise WebError("SENSITIVE_DATA_BLOCKED", "The outbound URL contains possible credentials.")
    if target_scope == "loopback":
        try:
            address = None if host == "localhost" else ipaddress.ip_address(host)
            local = address is None or (address.is_loopback and not getattr(address, "ipv4_mapped", None))
        except ValueError:
            local = False
        if not local:
            raise WebError("NETWORK_TARGET_BLOCKED", "Loopback scope allows only localhost, 127.0.0.0/8 and ::1.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not all(_LABEL.fullmatch(label) for label in host.split(".")) or len(host) > 253:
            raise WebError("INVALID_URL", "The URL hostname is invalid.") from None
        if target_scope == "public" and ("." not in host or host.endswith((".localhost", ".local", ".internal", ".home.arpa"))):
            raise WebError("NETWORK_TARGET_BLOCKED", "Local and internal hosts are blocked.")
    else:
        if target_scope == "public" and not _public_address(host):
            raise WebError("NETWORK_TARGET_BLOCKED", "Non-public network addresses are blocked.")
    authority = f"[{host}]" if ":" in host else host
    if parts.port is not None:
        authority += f":{port}"
    normalized = urlunsplit((parts.scheme, authority,
                            quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~"),
                            quote(parts.query, safe="%=&/?@:!$'()*+,;~-._"), ""))
    if len(normalized) > 4096:
        raise WebError("INVALID_URL", "The encoded URL exceeds the length limit.")
    return normalized, host, port


def _resolve_public(host: str, port: int) -> tuple[int, str]:
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise WebError("WEB_CONNECTION_ERROR", "Public hostname could not be resolved.") from None
    if not addresses or len(addresses) > 64:
        raise WebError("NETWORK_TARGET_BLOCKED", "The target has no acceptable public address.")
    for family, _, _, _, address in addresses:
        if family not in {socket.AF_INET, socket.AF_INET6} or not _public_address(address[0]):
            raise WebError("NETWORK_TARGET_BLOCKED", "DNS resolved to a non-public address.")
    return addresses[0][0], addresses[0][4][0]


def _bounded_resolve(resolver, host, port, deadline):
    """A timed-out lookup never proceeds to HTTP; at most four lookups can linger."""
    if not _DNS_SLOTS.acquire(timeout=_remaining(deadline)):
        raise TimeoutError
    finished, result = threading.Event(), []

    def lookup():
        try:
            result.append((True, resolver(host, port)))
        except Exception as exc:
            result.append((False, exc))
        finally:
            _DNS_SLOTS.release()
            finished.set()

    try:
        thread = threading.Thread(target=lookup, name="excudo-dns", daemon=True)
        thread.start()
    except BaseException:
        _DNS_SLOTS.release()
        raise
    if not finished.wait(_remaining(deadline)):
        raise TimeoutError
    _remaining(deadline)
    if not result or not result[0][0]:
        raise result[0][1] if result else WebError("WEB_CONNECTION_ERROR", "DNS lookup failed.")
    return result[0][1]


def _start_deadline(connection, deadline):
    """Interrupt socket IO, including slow headers and Connection: close bodies."""
    connection.deadline_expired = False

    def expire():
        connection.deadline_expired = True
        sock = getattr(connection, "_transport_socket", None) or connection.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    timer = threading.Timer(_remaining(deadline), expire)
    timer.daemon = True
    timer.start()
    return timer


class _PinnedConnection(http.client.HTTPConnection):
    """Connect only to the validated numeric IP; keep hostname for Host and TLS."""
    def __init__(self, host, port, family, address, secure, timeout):
        super().__init__(host, port, timeout=timeout)
        self.family, self.address, self.secure = family, address, secure

    def connect(self):
        sock = socket.socket(self.family, socket.SOCK_STREAM)
        self._transport_socket = sock
        try:
            sock.settimeout(self.timeout)
            sock.connect((self.address, self.port))
            if self.secure:
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)
                self._transport_socket = sock
            if getattr(self, "deadline_expired", False):
                raise TimeoutError
            self.sock = sock
        except BaseException:
            sock.close()
            raise


class _ReadableHTML(HTMLParser):
    _ignored = {"script", "style", "nav", "footer", "template", "noscript", "svg"}
    _void = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
    _blocks = {"p", "div", "section", "article", "main", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self, base_url: str, limit: int):
        super().__init__(convert_charrefs=True)
        self.base_url, self.limit = base_url, limit
        self.parts, self.title_parts = [], []
        self.stack = []
        self.size = 0
        self.truncated = False
        self.in_title = False
        self.links = 0

    def _add(self, value):
        available = self.limit - self.size
        if len(value) > available:
            self.truncated = True
        if available <= 0:
            return
        self.parts.append(value[:max(0, available)])
        self.size += min(len(value), max(0, available))

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        hidden = (tag in self._ignored or "hidden" in attrs
                  or attrs.get("aria-hidden", "").lower() == "true"
                  or bool(re.search(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", attrs.get("style", ""), re.I)))
        ignored = hidden or bool(self.stack and self.stack[-1][1])
        href = None
        if tag == "title" and not ignored:
            self.in_title = True
        if not ignored:
            if tag in self._blocks or tag in {"br", "hr"}:
                self._add("\n")
            if tag == "pre":
                self._add("\n```\n")
            if tag in {"td", "th"}:
                self._add("\t")
            if tag == "a" and self.links < 20:
                try:
                    href = validate_url(urljoin(self.base_url, attrs.get("href", "")))[0]
                    if len(href) > 512:
                        href = None
                except WebError:
                    pass
        if tag not in self._void:
            if len(self.stack) >= 256:
                raise WebError("CONTENT_EXTRACTION_FAILED", "HTML nesting exceeds the extraction limit.")
            self.stack.append((tag, ignored, href))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self._void:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                _, ignored, href = self.stack[index]
                del self.stack[index:]
                if tag == "title":
                    self.in_title = False
                if not ignored:
                    if tag == "a" and href:
                        self._add(f" ({href})")
                        self.links += 1
                    if tag in self._blocks:
                        self._add("\n")
                    if tag == "pre":
                        self._add("\n```\n")
                break

    def handle_data(self, data):
        if self.stack and self.stack[-1][1]:
            return
        if self.in_title:
            if sum(map(len, self.title_parts)) < 256:
                self.title_parts.append(data[:256])
        elif not any(entry[0] == "head" for entry in self.stack):
            self._add(data)

    def result(self):
        return ("".join(self.title_parts).strip()[:256],
                re.sub(r"\n{3,}", "\n\n", "".join(self.parts)).strip())


def _remaining(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError
    return seconds


def _fetch_public(url: str, timeout: float, limits: dict, *,
                  method="GET", data=None, raw=False, allowed_hosts=None) -> dict:
    """Runs inside the bounded worker; every hop gets fresh DNS validation."""
    deadline = time.monotonic() + timeout
    initial = validate_url(url)[0]
    current = initial
    requests = 0
    for redirects in range(limits["redirects"] + 1):
        current, host, port = validate_url(current)
        if allowed_hosts is not None and host not in allowed_hosts:
            raise WebError("NETWORK_TARGET_BLOCKED", "The search provider redirected outside its allowed hosts.")
        family, address = _bounded_resolve(_resolve_public, host, port, deadline)
        parts = urlsplit(current)
        connection = _PinnedConnection(host, port, family, address,
                                       parts.scheme == "https", _remaining(deadline))
        timer = _start_deadline(connection, deadline)
        response = None
        try:
            requests += 1
            headers = {"User-Agent": "Excudo/0.1 public documentation reader",
                       "Accept-Encoding": "identity", "Connection": "close"}
            body_argument = {}
            if data is not None:
                headers["Content-Type"] = "application/x-www-form-urlencoded"
                body_argument["body"] = data
            connection.request(method, parts.path + ("?" + parts.query if parts.query else ""),
                               headers=headers, **body_argument)
            response = connection.getresponse()
            _remaining(deadline)
            if response.status in {301, 302, 303, 307, 308}:
                target = response.getheader("Location")
                if not target:
                    raise WebError("INVALID_URL", "Redirect response has no target URL.")
                current = validate_url(urljoin(current, target))[0]
                if response.status in {301, 302, 303}:
                    method, data = "GET", None
                if redirects == limits["redirects"]:
                    raise WebError("TOO_MANY_REDIRECTS", "The redirect limit was reached.")
                continue
            metadata = {"url": initial, "final_url": current, "domain": host,
                        "status_code": response.status, "request_count": requests}
            if not 200 <= response.status < 300:
                return {"error": {"code": "HTTP_ERROR", "message": f"HTTP status {response.status}; no retry was attempted."}, "metadata": metadata}
            media_type = (response.getheader("Content-Type") or "").split(";", 1)[0].strip().lower()
            if media_type not in {"text/html", "text/plain", "text/markdown", "text/x-rst", "application/json"} and not (media_type.startswith("application/") and media_type.endswith("+json")):
                raise WebError("UNSUPPORTED_CONTENT_TYPE", "Only HTML, plain documentation text, and JSON are supported.")
            if (response.getheader("Content-Encoding") or "identity").lower() != "identity":
                raise WebError("UNSUPPORTED_CONTENT_TYPE", "Compressed response bodies are not accepted.")
            body = bytearray()
            while len(body) <= limits["bytes"]:
                _remaining(deadline)
                if connection.sock is not None:
                    connection.sock.settimeout(_remaining(deadline))
                chunk = response.read1(min(16384, limits["bytes"] + 1 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
            _remaining(deadline)
            truncated = len(body) > limits["bytes"]
            data = bytes(body[:limits["bytes"]])
            charset = response.headers.get_content_charset() or "utf-8"
            if charset.lower().replace("_", "-") not in {"utf-8", "utf8", "ascii", "us-ascii", "iso-8859-1", "latin-1", "windows-1252", "shift-jis", "euc-jp"}:
                charset = "utf-8"
            decoded = data.decode(charset, errors="replace")
            if raw:
                if truncated:
                    raise WebError("WEB_RESPONSE_TOO_LARGE", "Search response exceeds the byte limit.")
                return {"text": decoded, "metadata": metadata}
            title = ""
            if media_type == "text/html":
                parser = _ReadableHTML(current, limits["text"])
                parser.feed(decoded)
                parser.close()
                title, text = parser.result()
                truncated |= parser.truncated
            else:
                text = decoded[:limits["text"]]
                truncated |= len(decoded) > limits["text"]
            metadata.update({"title": title, "content_type": media_type,
                             "truncated": truncated, "response_bytes": len(data)})
            return {"text": text, "metadata": metadata}
        except (OSError, http.client.HTTPException):
            if connection.deadline_expired or time.monotonic() >= deadline:
                raise TimeoutError from None
            raise
        finally:
            timer.cancel()
            if response is not None:
                response.close()
            connection.close()
    raise WebError("TOO_MANY_REDIRECTS", "The redirect limit was reached.")


def _provider_search(query: str, count: int, timeout: float, limits: dict | None = None) -> dict:
    try:
        from ddgs.engines.duckduckgo import Duckduckgo
    except ImportError:
        raise WebError("WEB_SEARCH_NOT_AVAILABLE", "The configured search dependency is unavailable.") from None
    try:
        # Reuse the repository's provider parser, not ddgs' unbounded HTTP client,
        # implicit engine fallback, proxy configuration, or concurrent executor.
        transport_metadata = {}
        class BoundedDuckduckgo(Duckduckgo):
            def __init__(self):
                self.results = []

            def request(self, method, url, **kwargs):
                if method != "POST" or url != "https://html.duckduckgo.com/html/":
                    raise WebError("WEB_SEARCH_NOT_AVAILABLE", "The installed provider interface is unsupported.")
                response = _fetch_public(
                    url, timeout, limits or _limits(), method="POST",
                    data=urlencode(kwargs["data"]).encode("utf-8"), raw=True,
                    allowed_hosts={"html.duckduckgo.com", "duckduckgo.com"},
                )
                if response.get("error"):
                    raise WebError(response["error"]["code"], response["error"]["message"])
                transport_metadata.update(response.get("metadata", {}))
                return response["text"]

        raw = BoundedDuckduckgo().search(query)
        if raw is None:
            raw = []
        if not isinstance(raw, list):
            raise WebError("WEB_SEARCH_INVALID_RESPONSE", "Search provider returned malformed results.")
        results = []
        for item in raw[:count]:
            if not all(isinstance(getattr(item, key, None), str) for key in ("title", "href", "body")):
                raise WebError("WEB_SEARCH_INVALID_RESPONSE", "Search provider returned malformed results.")
            results.append({"title": item.title[:512], "url": item.href[:4096],
                            "snippet": item.body[:1500]})
        return {"results": results, "metadata": {"request_count": transport_metadata.get("request_count", 1)}}
    except WebError:
        raise
    except Exception as exc:
        if "timeout" in type(exc).__name__.lower():
            raise TimeoutError from None
        raise WebError("WEB_SEARCH_FAILED", "Search provider failed or throttled the request; no fallback was attempted.") from None


def _run_worker(operation: str, arguments: dict, timeout: float) -> dict:
    """zun_loc: the bounded request runs in this process (no worker subprocess)."""
    try:
        if operation == "fetch":
            return _fetch_public(**arguments)
        if operation == "search":
            return _provider_search(**arguments)
        if operation == "api":
            from .api_testing import _worker_request
            return _worker_request(**arguments)
        raise WebError("WEB_REQUEST_FAILED", "Unsupported web operation.")
    except TimeoutError:
        return {"error": {"code": "WEB_TIMEOUT", "message": "The web request exceeded its time limit."}}
    except WebError as exc:
        return {"error": {"code": exc.code, "message": str(exc)}}
    except Exception:
        return {"error": {"code": "WEB_REQUEST_FAILED", "message": "The web request could not be completed."}}


def _timeout(value):
    value = config.WEB_TIMEOUT if value is None else value
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise WebError("INVALID_TIMEOUT", "Timeout must be a positive number.")
    return min(float(value), config.WEB_TIMEOUT, config.MAX_COMMAND_TIMEOUT)


def _limits():
    return {"bytes": min(config.MAX_WEB_RESPONSE_BYTES, 8 * 1024 * 1024),
            "text": min(config.MAX_WEB_TEXT_CHARS, config.MAX_STDOUT_BYTES // 4, 12000),
            "redirects": min(config.MAX_WEB_REDIRECTS, 5)}


def _result(payload: dict, started: float, **metadata) -> ToolResult:
    metadata.update(payload.get("metadata", {}))
    metadata.update({"external": True, "untrusted": True,
                     "retrieved_at": datetime.now(timezone.utc).isoformat()})
    error = payload.get("error")
    return sanitize_tool_result(ToolResult(
        success=not bool(error), stdout=payload.get("text", ""), error=error,
        timed_out=bool(error and error.get("code") == "WEB_TIMEOUT"),
        duration_ms=int((time.monotonic() - started) * 1000), metadata=metadata,
    ))


def _failure(exc, started):
    if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
        code, message = "WEB_TIMEOUT", "The web request exceeded its time limit."
    elif isinstance(exc, WebError):
        code, message = exc.code, str(exc)
    else:
        code, message = "WEB_REQUEST_FAILED", "The web request could not be completed."
    return _result({"error": {"code": code, "message": message}}, started)


def fetch_url(url: str, timeout: float | None = None) -> ToolResult:
    started = time.monotonic()
    try:
        url = validate_url(url)[0]
        timeout = _timeout(timeout)
        payload = _run_worker("fetch", {"url": url, "timeout": timeout, "limits": _limits()}, timeout)
        return _result(payload, started)
    except Exception as exc:
        return _failure(exc, started)


def _domains(domains):
    if domains is None:
        return []
    if not isinstance(domains, list) or len(domains) > 5:
        raise WebError("INVALID_ARGUMENTS", "Supply at most five domain names.")
    normalized = []
    for domain in domains:
        if not isinstance(domain, str) or re.search(r"[/@:?#%]", domain):
            raise WebError("INVALID_ARGUMENTS", "Domain filters must be plain public hostnames.")
        normalized.append(validate_url("https://" + domain)[1])
    return list(dict.fromkeys(normalized))


def web_search(query: str, domains: list[str] | None = None,
               max_results: int | None = None, timeout: float | None = None) -> ToolResult:
    started = time.monotonic()
    try:
        if not isinstance(query, str) or not query.strip() or len(query) > 1000:
            raise WebError("INVALID_ARGUMENTS", "Search query must contain 1–1000 characters.")
        if outbound_has_secret(query):
            raise WebError("SENSITIVE_DATA_BLOCKED", "The search query contains possible credentials.")
        allowed = _domains(domains)
        count = config.MAX_WEB_SEARCH_RESULTS if max_results is None else max_results
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise WebError("INVALID_ARGUMENTS", "Result count must be a positive integer.")
        count = min(count, config.MAX_WEB_SEARCH_RESULTS, 10)
        if config.WEB_SEARCH_PROVIDER != "duckduckgo":
            raise WebError("WEB_SEARCH_NOT_AVAILABLE", "No supported search provider is configured.")
        restriction = " (" + " OR ".join("site:" + domain for domain in allowed) + ")" if allowed else ""
        timeout = _timeout(timeout)
        payload = _run_worker("search", {"query": query + restriction, "count": count,
                                        "timeout": timeout, "limits": _limits()}, timeout)
        if payload.get("error"):
            return _result(payload, started, provider="duckduckgo")
        raw = payload.get("results")
        if not isinstance(raw, list):
            raise WebError("WEB_SEARCH_INVALID_RESPONSE", "Search provider returned malformed results.")
        results = []
        for item in raw[:count]:
            if not isinstance(item, dict) or not all(isinstance(item.get(key), str) for key in ("title", "url", "snippet")):
                raise WebError("WEB_SEARCH_INVALID_RESPONSE", "Search provider returned malformed results.")
            try:
                url, domain, _ = validate_url(item["url"])
            except WebError:
                continue
            if allowed and not any(domain == d or domain.endswith("." + d) for d in allowed):
                continue
            results.append({"title": item["title"][:256], "url": url, "domain": domain,
                            "snippet": item["snippet"][:1000]})
        encoded = json.dumps({"results": results}, ensure_ascii=False)
        truncated = len(raw) > count
        # stdout is itself JSON inside ToolResult. Budget the escaped envelope as
        # well, so its final serialization never cuts a search result mid-JSON.
        while len(json.dumps(encoded, ensure_ascii=False)) > _limits()["text"] and results:
            results.pop()
            truncated = True
            encoded = json.dumps({"results": results}, ensure_ascii=False)
        return _result({"text": encoded, "metadata": payload.get("metadata", {})}, started, provider="duckduckgo",
                       domains=allowed, result_count=len(results), truncated=truncated)
    except Exception as exc:
        return _failure(exc, started)


def search_docs(query: str, domains: list[str] | None = None,
                max_results: int | None = None, timeout: float | None = None) -> ToolResult:
    preferred = domains
    if preferred is None and isinstance(query, str):
        words = set(re.findall(r"[a-z]+", query.lower()))
        preferred = list(dict.fromkeys(domain for tech, domain in _OFFICIAL_DOMAINS.items() if tech in words))[:5]
    result = web_search(query, preferred, max_results, timeout)
    result.metadata["documentation_search"] = True
    # A domain preference is a filter, never evidence that arbitrary results are official.
    return result
