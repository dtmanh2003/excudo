"""One-request API tools using Phase 8's pinned transport and bounded worker."""
import ipaddress
import json
import math
import re
import socket
import subprocess
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import config, web_access as web
from .results import ToolResult, sanitize_text, sanitize_tool_result

API_TOOLS = frozenset({"api_request", "api_check", "inspect_openapi"})
METHODS = ("GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE")
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_PATH = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*(?:\.(?:[A-Za-z_][A-Za-z0-9_-]*|0|[1-9][0-9]*))*\Z")
_SECRET_NAME = re.compile(r"(?i)(?:auth|cookie|token|password|passwd|secret|credential|signature|(?:^|[-_])(?:api[-_]?)?key(?:$|[-_]))")
_FORBIDDEN_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "upgrade", "proxy-authorization", "proxy-connection", "trailer", "te", "accept-encoding", "expect"}
_REQUEST_FIELDS = {"method", "url", "headers", "query", "json_body", "text_body", "timeout", "target_scope"}
_CHECK_FIELDS = {"expected_status", "expected_headers", "body_contains", "json_assertions"}
_DEFAULT_HEADERS = {"user-agent": "Excudo/0.1 API tester", "accept-encoding": "identity", "connection": "close"}


def _error(code, message):
    raise web.WebError(code, message)


def _limits():
    return {**web._limits(), "request": min(config.MAX_API_REQUEST_BYTES, 262144),
            "headers": min(config.MAX_API_HEADER_BYTES, 16384),
            "assertions": min(config.MAX_API_ASSERTIONS, 100),
            "operations": min(config.MAX_OPENAPI_OPERATIONS, 200)}


def _json_shape(value, depth=0, counter=None):
    counter = [0] if counter is None else counter
    counter[0] += 1
    if depth > 24 or counter[0] > 5000:
        _error("JSON_LIMIT_EXCEEDED", "JSON exceeds the depth or item limit.")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                _error("INVALID_ARGUMENTS", "JSON object keys must be strings.")
            _json_shape(item, depth + 1, counter)
    elif isinstance(value, list):
        for item in value:
            _json_shape(item, depth + 1, counter)
    elif value is not None and not isinstance(value, (str, bool, int, float)):
        _error("INVALID_ARGUMENTS", "Unsupported JSON value.")
    elif isinstance(value, float) and not math.isfinite(value):
        _error("INVALID_ARGUMENTS", "Non-finite JSON numbers are unsupported.")


def redact_url(url):
    try:
        parts = urlsplit(url)
        host = parts.hostname or "invalid"
        if ":" in host:
            host = "[" + host + "]"
        if parts.port:
            host += ":" + str(parts.port)
        query = [(key, "[REDACTED]" if _SECRET_NAME.search(key) else value)
                 for key, value in parse_qsl(parts.query, keep_blank_values=True, max_num_fields=100)]
        return sanitize_text(urlunsplit((parts.scheme, host, parts.path, urlencode(query), "")))
    except (ValueError, TypeError):
        return "[REDACTED URL]"


def _secrets(args):
    found = []
    def visit(value, depth=0):
        if depth > 25:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if _SECRET_NAME.search(str(key)) and isinstance(item, (str, int, float)):
                    secret = str(item)
                    found.append(secret)
                    if str(key).lower() in {"authorization", "proxy-authorization"}:
                        found.append(secret.split(" ", 1)[-1])
                    if "cookie" in str(key).lower():
                        parts = secret.split(";")[:1] if str(key).lower() == "set-cookie" else secret.split(";")
                        found.extend(part.partition("=")[2].strip() for part in parts)
                visit(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                visit(item, depth + 1)
    visit(args)
    try:
        for key, value in parse_qsl(urlsplit(args.get("url", "")).query, keep_blank_values=True, max_num_fields=100):
            visit({key: value})
    except (ValueError, TypeError, AttributeError):
        pass
    return sorted({item for item in found if item}, key=len, reverse=True)


def _redact(value, secrets=(), depth=0):
    if depth > 25:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        return {_redact(str(key), secrets, depth + 1): "[REDACTED]" if _SECRET_NAME.search(str(key)) else _redact(item, secrets, depth + 1)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, secrets, depth + 1) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if len(secret) < 4:
                value = re.sub(r"(?<!\w)" + re.escape(secret) + r"(?!\w)", "[REDACTED]", value)
            else:
                value = value.replace(secret, "[REDACTED]")
                value = value.replace(json.dumps(secret, ensure_ascii=False)[1:-1], "[REDACTED]")
        value = re.sub(r"https?://[^\s\"<>]+", lambda match: redact_url(match[0]), value)
        value = re.sub(r"(?im)((?:authorization|proxy-authorization|cookie|set-cookie|x-api-key|api-key)\s*[:=]\s*)[^\r\n]+", r"\1[REDACTED]", value)
        value = re.sub(r'''(?i)((?:["']?)(?:api[-_]?key|token|password|passwd|secret|credential|authorization)["']?\s*[:=]\s*["']?)[^\r\n,;"'}]+''', r"\1[REDACTED]", value)
        return sanitize_text(value)
    return value


def safe_api_args(args):
    """Diagnostics only. The exact original request stays bound to its approval hash."""
    if not isinstance(args, dict):
        return {"request": "[REDACTED]"}
    safe = _redact(args, _secrets(args))
    if "url" in args:
        safe["url"] = _redact(redact_url(args["url"]), _secrets(args))
    if "text_body" in safe:
        safe["text_body"] = "[BODY OMITTED]"
    if "json_assertions" in safe and isinstance(safe["json_assertions"], list):
        for item in safe["json_assertions"]:
            if isinstance(item, dict) and "value" in item:
                item["value"] = "[ASSERTION VALUE OMITTED]"
    if "body_contains" in safe:
        safe["body_contains"] = "[ASSERTION VALUE OMITTED]"
    if len(json.dumps(safe, ensure_ascii=False)) > 8000:
        for field in ("json_body", "headers", "query", "json_assertions", "expected_headers"):
            if field in safe:
                safe[field] = "[DETAILS OMITTED]"
    return safe


def safe_api_arguments(raw):
    try:
        return json.dumps(safe_api_args(json.loads(raw)), ensure_ascii=False)
    except (ValueError, TypeError, RecursionError):
        return '{"request":"[REDACTED]"}'


def _headers(headers, limit, *, expected=False):
    if headers is None:
        return {}
    if not isinstance(headers, dict) or len(headers) > 50:
        _error("INVALID_HEADERS", "Headers must be an object with at most 50 fields.")
    result, size = {}, 0
    for name, value in headers.items():
        if not isinstance(name, str) or len(name) > 128 or not _NAME.fullmatch(name):
            _error("INVALID_HEADERS", "Invalid HTTP header name.")
        name = name.lower()
        if name in result or (not expected and name in _FORBIDDEN_HEADERS):
            _error("INVALID_HEADERS", "Duplicate, proxy or transport-control headers are unsupported.")
        if expected and value is None:
            result[name] = None
            continue
        if not isinstance(value, str) or len(value) > 4096 or re.search(r"[\x00-\x1f\x7f]", value):
            _error("INVALID_HEADERS", "Invalid HTTP header value.")
        try:
            size += len(name.encode("ascii")) + len(value.encode("latin-1")) + 4
        except UnicodeError:
            _error("INVALID_HEADERS", "Header values must be Latin-1 encodable.")
        result[name] = value
    if size > limit:
        _error("REQUEST_TOO_LARGE", "Headers exceed the size limit.")
    return result


def validate_request(args, *, operation="request", limits=None):
    """Pure validation; PermissionGate calls this before confirmation or any DNS."""
    limits = limits or _limits()
    if not isinstance(args, dict) or set(args) - (_REQUEST_FIELDS | (_CHECK_FIELDS if operation == "check" else set())):
        _error("INVALID_ARGUMENTS", "Unsupported API request fields.")
    method = args.get("method", "GET" if operation == "openapi" else None)
    if method not in METHODS:
        _error("INVALID_HTTP_METHOD", "Only GET, HEAD, OPTIONS, POST, PUT, PATCH and DELETE are supported.")
    if operation == "openapi" and (method != "GET" or {"json_body", "text_body"}.intersection(args)):
        _error("INVALID_ARGUMENTS", "OpenAPI inspection sends a GET without a request body.")
    scope = args.get("target_scope", "public")
    url, _, _ = web.validate_url(args.get("url"), target_scope=scope, allow_sensitive_query=True)
    query = args.get("query", {})
    if not isinstance(query, dict) or len(query) > 50:
        _error("INVALID_QUERY", "Query must be an object with at most 50 fields.")
    for key, value in query.items():
        if not isinstance(key, str) or not key or len(key) > 128 or not isinstance(value, str) or len(value) > 2048:
            _error("INVALID_QUERY", "Query names and values must be bounded strings.")
        if re.search(r"[\x00-\x1f\x7f]", key + value):
            _error("INVALID_QUERY", "Control characters in query fields are unsupported.")
    parts = urlsplit(url)
    try:
        original_query = parse_qsl(parts.query, keep_blank_values=True, max_num_fields=50)
    except ValueError:
        _error("INVALID_QUERY", "Too many URL query fields.")
    if len(original_query) + len(query) > 50:
        _error("INVALID_QUERY", "Too many query fields.")
    if query:
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(original_query + list(query.items())), ""))
        url = web.validate_url(url, target_scope=scope, allow_sensitive_query=True)[0]
    headers = _headers(args.get("headers"), limits["headers"])
    body = None
    if "json_body" in args and "text_body" in args:
        _error("INVALID_ARGUMENTS", "Choose exactly one body mode.")
    if "json_body" in args:
        _json_shape(args["json_body"])
        body = json.dumps(args["json_body"], ensure_ascii=False, allow_nan=False).encode("utf-8")
        headers.setdefault("content-type", "application/json")
    elif "text_body" in args:
        if not isinstance(args["text_body"], str):
            _error("INVALID_ARGUMENTS", "Text body must be a string.")
        body = args["text_body"].encode("utf-8")
        headers.setdefault("content-type", "text/plain; charset=utf-8")
    if body is not None and (method in {"GET", "HEAD"} or len(body) > limits["request"]):
        _error("REQUEST_TOO_LARGE" if len(body) > limits["request"] else "INVALID_ARGUMENTS", "GET/HEAD bodies are unsupported; other bodies must fit the request limit.")
    wire_headers = {**_DEFAULT_HEADERS, **headers, "host": urlsplit(url).netloc}
    if body is not None or method in {"POST", "PUT", "PATCH"}:
        wire_headers["content-length"] = str(len(body) if body is not None else 0)
    if sum(len(key) + len(value.encode("latin-1")) + 4 for key, value in wire_headers.items()) > limits["headers"]:
        _error("REQUEST_TOO_LARGE", "Combined request headers exceed the size limit.")
    if operation == "check":
        _validate_assertions(args, limits)
    if len(json.dumps(args, ensure_ascii=False).encode("utf-8")) > 1_000_000:
        _error("REQUEST_TOO_LARGE", "Combined request and assertions exceed the argument limit.")
    return {"method": method, "url": url, "headers": headers, "body": body, "target_scope": scope}


def _validate_assertions(args, limits):
    status = args.get("expected_status", list(range(200, 300)))
    statuses = status if isinstance(status, list) else [status]
    if not statuses or len(statuses) > 100 or any(type(item) is not int or not 100 <= item <= 599 for item in statuses):
        _error("INVALID_ASSERTION", "Status must be an HTTP status or a nonempty list of statuses.")
    headers = _headers(args.get("expected_headers"), limits["headers"], expected=True)
    contains = args.get("body_contains")
    if contains is not None and (not isinstance(contains, str) or not contains or len(contains) > 2048):
        _error("INVALID_ASSERTION", "body_contains must be a nonempty bounded string.")
    assertions = args.get("json_assertions", [])
    if not isinstance(assertions, list) or len(assertions) + len(headers) + 1 + (contains is not None) > limits["assertions"]:
        _error("INVALID_ASSERTION", "Too many assertions or invalid assertion list.")
    for item in assertions:
        if not isinstance(item, dict) or set(item) - {"path", "operator", "value"}:
            _error("INVALID_ASSERTION", "Unsupported assertion fields.")
        path = item.get("path")
        operator = item.get("operator")
        if not isinstance(path, str) or len(path) > 256 or not _PATH.fullmatch(path) or len(path.split(".")) > 24:
            _error("INVALID_ASSERTION", "Use dotted field names and numeric list indices only.")
        if operator not in {"exists", "equals"} or ("value" in item) != (operator == "equals"):
            _error("INVALID_ASSERTION", "Use exists, or equals with one JSON value.")
        if "value" in item:
            _json_shape(item["value"])
            if len(json.dumps(item["value"])) > 4096:
                _error("INVALID_ASSERTION", "Assertion value exceeds its size limit.")


def _resolve(host, port, scope):
    if scope == "public":
        return web._resolve_public(host, port)
    answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not answers or len(answers) > 64:
        _error("NETWORK_TARGET_BLOCKED", "No acceptable loopback address.")
    for family, _, _, _, address in answers:
        ip = ipaddress.ip_address(address[0])
        if family not in {socket.AF_INET, socket.AF_INET6} or not ip.is_loopback or getattr(ip, "ipv4_mapped", None):
            _error("NETWORK_TARGET_BLOCKED", "Loopback DNS returned a non-loopback address.")
    preferred = next((item for item in answers if item[0] == socket.AF_INET), answers[0]) if host == "localhost" else answers[0]
    return preferred[0], preferred[4][0]


def _assert_response(args, status, headers, text, data, json_valid):
    expected = args.get("expected_status", list(range(200, 300)))
    statuses = expected if isinstance(expected, list) else [expected]
    results = [{"kind": "status", "passed": status in statuses}]
    for name, expected in (args.get("expected_headers") or {}).items():
        actual = headers.get(name.lower())
        results.append({"kind": "header", "passed": actual is not None and (expected is None or actual == expected)})
    if args.get("body_contains") is not None:
        results.append({"kind": "body_contains", "passed": args["body_contains"] in text})
    for item in args.get("json_assertions", []):
        value, exists = data, json_valid
        for part in item["path"].split("."):
            if isinstance(value, dict) and part in value:
                value = value[part]
            elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
                value = value[int(part)]
            else:
                exists = False
                break
        passed = exists and (item["operator"] == "exists" or json.dumps(value, sort_keys=True) == json.dumps(item["value"], sort_keys=True))
        results.append({"kind": "json", "passed": passed})
    return [{"index": i, **result} for i, result in enumerate(results)]


def _openapi(data, limit):
    if not isinstance(data, dict) or not (re.fullmatch(r"3\.\d+\.\d+", str(data.get("openapi", ""))) or data.get("swagger") == "2.0") or not isinstance(data.get("paths"), dict):
        _error("OPENAPI_PARSE_ERROR", "Expected an OpenAPI 3.x or Swagger 2.0 JSON document with paths.")
    info = data.get("info", {})
    if not isinstance(info, dict):
        _error("OPENAPI_PARSE_ERROR", "Invalid OpenAPI info object.")
    operations, total = [], 0
    for path, entry in data["paths"].items():
        if not isinstance(path, str) or not path.startswith("/") or not isinstance(entry, dict):
            _error("OPENAPI_PARSE_ERROR", "Invalid OpenAPI path entry.")
        for method, operation in entry.items():
            if method.upper() not in METHODS or not isinstance(operation, dict):
                continue
            total += 1
            if len(operations) >= limit:
                continue
            shared = entry.get("parameters", [])
            own = operation.get("parameters", [])
            request_body = operation.get("requestBody", {})
            responses = operation.get("responses", {})
            if not isinstance(shared, list) or not isinstance(own, list) or not isinstance(request_body, dict) or not isinstance(responses, dict) or not isinstance(request_body.get("content", {}), dict):
                _error("OPENAPI_PARSE_ERROR", "Invalid OpenAPI operation metadata.")
            parameters = shared + own
            operations.append({"path": path[:512], "method": method.upper(),
                               "operation_id": str(operation.get("operationId", ""))[:256],
                               "parameters": [{"name": str(p.get("name", ""))[:128], "in": str(p.get("in", ""))[:32], "required": p.get("required") is True} for p in parameters[:30] if isinstance(p, dict)],
                               "request_body_types": list(request_body.get("content", {}))[:20],
                               "response_codes": list(responses)[:30]})
    return {"title": str(info.get("title", ""))[:256], "version": str(info.get("version", ""))[:128],
            "operations": operations, "operation_count": total, "truncated": total > len(operations),
            "references_resolved": False, "descriptions_omitted": True}


def _bounded_payload(payload, limits, secrets):
    payload = _redact(payload, secrets)
    budget = min(16000, max(256, limits["text"]))
    while len(json.dumps(payload, ensure_ascii=False)) > budget:
        payload["metadata"]["truncated"] = True
        body = payload.get("body", {})
        if body.get("json") is not None:
            body["json"] = None
            body["json_omitted"] = True
        elif body.get("text"):
            body["text"] = body["text"][:len(body["text"]) // 2]
        elif body.get("operations"):
            body["operations"].pop()
            body["truncated"] = True
        elif payload["metadata"].get("headers"):
            payload["metadata"]["headers"].pop(next(reversed(payload["metadata"]["headers"])))
        else:
            # Keep verification state even under an exceptionally small output setting.
            payload["body"] = {"summary": "Response details omitted by output limit."}
            break
    return payload


def _worker_request(operation, args, timeout, limits):
    """Exactly one HTTP exchange; redirects, retries, proxies and cookies are absent."""
    request = validate_request(args, operation=operation, limits=limits)
    deadline = time.monotonic() + timeout
    family, address = web._bounded_resolve(lambda host, port: _resolve(host, port, request["target_scope"]), urlsplit(request["url"]).hostname, urlsplit(request["url"]).port or (443 if request["url"].startswith("https:") else 80), deadline)
    parts = urlsplit(request["url"])
    connection = web._PinnedConnection(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), family, address, parts.scheme == "https", web._remaining(deadline))
    metadata = {"request_method": request["method"], "requested_url": redact_url(request["url"]),
                "final_url": redact_url(request["url"]), "target_scope": request["target_scope"],
                "redirect_count": 0, "request_count": 1, "truncated": False,
                "remote_side_effects_possible": request["method"] in MUTATING_METHODS,
                "workspace_rollback_available": False, "external": True, "untrusted": True}
    secrets = _secrets(args)
    timer = web._start_deadline(connection, deadline)
    response = None
    try:
        headers = {**_DEFAULT_HEADERS, **request["headers"]}
        connection.request(request["method"], parts.path + ("?" + parts.query if parts.query else ""), body=request["body"], headers=headers)
        response = connection.getresponse()
        web._remaining(deadline)
        response_headers = response.getheaders()
        if len(response_headers) > 64 or sum(len(k) + len(v) + 4 for k, v in response_headers) > limits["headers"]:
            _error("RESPONSE_TOO_LARGE", "Response headers exceed the limit.")
        normalized_headers = {}
        for key, value in response_headers:
            name = key.lower()
            normalized_headers[name] = normalized_headers.get(name, "") + (", " if name in normalized_headers else "") + value
        for key, value in response_headers:
            secrets.extend(_secrets({key: value}))
        metadata.update({"status_code": response.status, "headers": normalized_headers,
                         "content_type": (response.getheader("Content-Type") or "").split(";", 1)[0].lower().strip(),
                         "redirect_not_followed": 300 <= response.status < 400})
        if (response.getheader("Content-Encoding") or "identity").lower() != "identity":
            _error("UNSUPPORTED_CONTENT_TYPE", "Compressed responses are unsupported.")
        body = bytearray()
        while request["method"] != "HEAD" and len(body) <= limits["bytes"]:
            web._remaining(deadline)
            if connection.sock is not None:
                connection.sock.settimeout(web._remaining(deadline))
            chunk = response.read1(min(16384, limits["bytes"] + 1 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
        web._remaining(deadline)
        if len(body) > limits["bytes"]:
            metadata["truncated"] = True
            _error("RESPONSE_TOO_LARGE", "Response body exceeds the byte limit; assertions were not evaluated.")
        media = metadata["content_type"]
        textual = not media or media.startswith("text/") or media == "application/json" or media.endswith("+json")
        if body and not textual:
            _error("UNSUPPORTED_CONTENT_TYPE", "Only text and JSON API responses are supported.")
        text = body.decode("utf-8", errors="replace")
        data, valid_json = None, False
        try:
            data = json.loads(text, parse_constant=lambda value: _error("INVALID_JSON_RESPONSE", "Non-finite JSON number."))
            _json_shape(data)
            valid_json = True
        except (ValueError, RecursionError, web.WebError):
            metadata["json_parse_error"] = bool(text)
            data = None
        metadata.update({"response_bytes": len(body), "json_valid": valid_json})
        error = None
        output = {"json": data, "text": text[:limits["text"]] if not valid_json else ""}
        metadata["truncated"] |= len(text) > limits["text"] and not valid_json
        if operation == "check":
            assertions = _assert_response(args, response.status, normalized_headers, text, data, valid_json)
            failed = sum(not item["passed"] for item in assertions)
            metadata.update({"assertions_passed": len(assertions) - failed, "assertions_failed": failed,
                             "assertions": assertions, "verification": failed == 0})
            if failed:
                error = {"code": "ASSERTION_FAILED", "message": "One or more API assertions failed."}
        elif not 200 <= response.status < 300:
            error = {"code": "HTTP_STATUS_ERROR", "message": "The API returned a non-success status; no redirect or retry was attempted."}
        if operation == "openapi" and error is None:
            if not valid_json:
                _error("OPENAPI_PARSE_ERROR", "OpenAPI inspection supports JSON responses only.")
            output = _openapi(data, limits["operations"])
        return _bounded_payload({"body": output, "metadata": metadata, "error": error}, limits, secrets)
    except web.WebError as exc:
        return _bounded_payload({"error": {"code": exc.code, "message": str(exc)}, "metadata": metadata}, limits, secrets)
    except (TimeoutError, socket.timeout):
        return _bounded_payload({"error": {"code": "REQUEST_TIMEOUT", "message": "API request exceeded its deadline; remote side effects may remain."}, "metadata": metadata}, limits, secrets)
    except Exception:
        if connection.deadline_expired or time.monotonic() >= deadline:
            return _bounded_payload({"error": {"code": "REQUEST_TIMEOUT", "message": "API request exceeded its deadline; remote side effects may remain."}, "metadata": metadata}, limits, secrets)
        return _bounded_payload({"error": {"code": "HTTP_TRANSPORT_ERROR", "message": "API transport failed; no automatic retry was attempted."}, "metadata": metadata}, limits, secrets)
    finally:
        timer.cancel()
        if response is not None:
            response.close()
        connection.close()


def _call(operation, args):
    started = time.monotonic()
    method = args.get("method", "GET")
    try:
        limits = _limits()
        validate_request(args, operation=operation, limits=limits)
        timeout = args.get("timeout")
        timeout = config.API_TIMEOUT if timeout is None else timeout
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            _error("INVALID_TIMEOUT", "Timeout must be a positive finite number.")
        timeout = min(timeout, config.API_TIMEOUT, config.MAX_COMMAND_TIMEOUT)
        payload = web._run_worker("api", {"operation": operation, "args": args, "timeout": timeout, "limits": limits}, timeout)
        payload.setdefault("metadata", {})
        payload["metadata"].setdefault("remote_side_effects_possible", method in MUTATING_METHODS)
        payload["metadata"].setdefault("workspace_rollback_available", False)
        payload = _bounded_payload(payload, limits, _secrets(args))
        error = payload.get("error")
        if error and error["code"] == "WEB_TIMEOUT":
            error["code"] = "REQUEST_TIMEOUT"
        elif error and error["code"] in {"WEB_CONNECTION_ERROR", "WEB_REQUEST_FAILED", "WEB_WORKER_FAILED", "WEB_INVALID_RESPONSE"}:
            error["code"] = "HTTP_TRANSPORT_ERROR"
        metadata = payload.get("metadata", {})
        metadata.update({"external": True, "untrusted": True, "elapsed_ms": int((time.monotonic() - started) * 1000)})
        return sanitize_tool_result(ToolResult(not error, stdout=json.dumps(payload.get("body", {}), ensure_ascii=False),
            error=error, exit_code=(1 if error else 0) if operation == "check" else None,
            timed_out=bool(error and error["code"] == "REQUEST_TIMEOUT"), duration_ms=metadata["elapsed_ms"], metadata=metadata))
    except Exception as exc:
        code = "REQUEST_TIMEOUT" if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)) else exc.code if isinstance(exc, web.WebError) else "HTTP_TRANSPORT_ERROR"
        return ToolResult(False, error={"code": code, "message": "API request could not complete; no retry was attempted."},
                          exit_code=1 if operation == "check" else None, timed_out=code == "REQUEST_TIMEOUT",
                          duration_ms=int((time.monotonic() - started) * 1000),
                          metadata={"external": True, "untrusted": True, "remote_side_effects_possible": method in MUTATING_METHODS,
                                    "workspace_rollback_available": False})


def api_request(method, url, **kwargs):
    return _call("request", {"method": method, "url": url, **kwargs})


def api_check(method, url, **kwargs):
    return _call("check", {"method": method, "url": url, **kwargs})


def inspect_openapi(url, **kwargs):
    return _call("openapi", {"method": "GET", "url": url, **kwargs})
