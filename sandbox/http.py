"""tiny json-over-http helper built on urllib so the backend has no third party
http dependency."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from core.errors import ProviderFailure


def request_json(
    method: str,
    url: str,
    *,
    payload: dict | None = None,
    headers: dict | None = None,
    timeout: int = 60,
    with_headers: bool = False,
) -> Any:
    """`with_headers` returns (body, response_headers). it is opt in because a rate limit is
    stated in headers and nowhere else, and every existing caller wants the body alone."""
    data = None
    hdrs = {"accept": "application/json", **{k: v for k, v in (headers or {}).items() if v}}
    if payload is not None:
        data = json.dumps(payload).encode()
        hdrs["content-type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            received = dict(resp.headers.items()) if with_headers else None
            if not body:
                parsed: Any = {}
            else:
                try:
                    parsed = json.loads(body)
                except ValueError:
                    parsed = {"raw": body}
            return (parsed, received) if with_headers else parsed
    except urllib.error.HTTPError as exc:  # pragma: no cover - network
        detail = exc.read().decode("utf-8", "replace")[:2000]
        raise ProviderFailure(
            f"{method} {url} failed with {exc.code}", status=exc.code, body=detail,
            headers=dict(exc.headers.items()) if exc.headers else {}
        ) from exc
    except urllib.error.URLError as exc:  # pragma: no cover - network
        raise ProviderFailure(f"{method} {url} unreachable: {exc.reason}") from exc


def pick(raw: dict, *keys: str, default: Any = "") -> Any:
    """providers rename fields constantly; read the first key that exists."""
    for key in keys:
        if isinstance(raw, dict) and key in raw and raw[key] is not None:
            return raw[key]
    return default
