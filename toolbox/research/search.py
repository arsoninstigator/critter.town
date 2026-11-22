"""two search engines, one row shape.

a source is only worth citing if the row says where it came from, so every result is
normalized before anything downstream sees it:

    {"provider", "url", "title", "snippet", "score", "published_at", "site"}

`fan_out` asks every configured engine and merges by url. a url two engines both returned is
a stronger candidate than a url one engine ranked first, and the merged row keeps the list of
engines that found it so the caller can weigh agreement instead of trusting a score that two
providers do not mean the same thing by.

no import of the project's own code: this is a toolbox entry, and the same discipline that
keeps an agpl tool at arm's length keeps a search vendor out of the core.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from core.errors import ProviderFailure, ProviderNotConfigured

TAVILY_BASE = "https://api.tavily.com"
QUERIT_BASE = "https://api.querit.ai/v1"


def normalize(provider: str, row: dict) -> dict:
    """one row, whatever the vendor called its fields."""
    url = str(row.get("url") or "").strip()
    return {
        "provider": provider,
        "url": url,
        "title": str(row.get("title") or "").strip(),
        "snippet": str(row.get("snippet") or row.get("content") or "").strip()[:1200],
        "score": _score(row.get("score")),
        "published_at": row.get("published_at") or row.get("published_date") or row.get("page_age"),
        "site": str(row.get("site") or row.get("site_name") or "").strip(),
    }


def _score(value) -> float:
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class TavilySearch:
    api_key: str = ""
    base_url: str = TAVILY_BASE
    timeout: float = 60.0
    transport: httpx.BaseTransport | None = None

    def configured(self) -> bool:
        return bool(self.api_key)

    def status(self) -> dict:
        return {"kind": "tavily", "called": True, "base_url": self.base_url,
                "configured": self.configured(), "needs": "CRITTERTOWN_TAVILY_API_KEY",
                "calls": ["search"]}

    def search(self, query: str, limit: int = 8, depth: str = "basic") -> list[dict]:
        if not query.strip():
            raise ProviderFailure("tavily needs a query", provider="tavily")
        body = {"api_key": self.api_key, "query": query,
                "max_results": max(1, min(int(limit), 20)), "search_depth": depth}
        payload = _post(self, "/search", body, f"search for {query!r}", "tavily")
        rows = payload.get("results") or []
        return [normalize("tavily", row) for row in rows if isinstance(row, dict)]


@dataclass
class QueritSearch:
    api_key: str = ""
    base_url: str = QUERIT_BASE
    timeout: float = 60.0
    transport: httpx.BaseTransport | None = None

    def configured(self) -> bool:
        return bool(self.api_key)

    def status(self) -> dict:
        return {"kind": "querit", "called": True, "base_url": self.base_url,
                "configured": self.configured(), "needs": "CRITTERTOWN_QUERIT_API_KEY",
                "calls": ["search"]}

    def search(self, query: str, limit: int = 8) -> list[dict]:
        if not query.strip():
            raise ProviderFailure("querit needs a query", provider="querit")
        body = {"query": query, "count": max(1, min(int(limit), 50))}
        payload = _post(self, "/search", body, f"search for {query!r}", "querit")
        # querit answers 200 with an error_code inside the body, so a failed search that
        # reads as an empty result is the failure this check exists to prevent
        code = payload.get("error_code")
        if code not in (None, 200, "200"):
            raise ProviderFailure(f"querit refused the search: {payload.get('error_msg') or code}",
                                  provider="querit", code=code)
        rows = ((payload.get("results") or {}).get("result")) or []
        return [normalize("querit", row) for row in rows if isinstance(row, dict)]


def _post(client, path: str, body: dict, what: str, provider: str) -> dict:
    if not client.configured():
        raise ProviderNotConfigured(f"{provider} has no api key", provider=provider,
                                    hint=f"set the {provider} key, or stop calling it")
    headers = {}
    if getattr(client, "api_key", ""):
        headers["authorization"] = f"Bearer {client.api_key}"
    try:
        with httpx.Client(base_url=client.base_url, timeout=client.timeout,
                          transport=client.transport, headers=headers) as session:
            response = session.post(path, json=body)
    except httpx.HTTPError as exc:
        raise ProviderFailure(f"{provider} {what} did not answer: {exc}", provider=provider) from exc
    if response.status_code >= 400:
        raise ProviderFailure(f"{provider} {what} returned {response.status_code}",
                              provider=provider, status=response.status_code,
                              body=response.text[:400])
    try:
        payload = response.json()
    except ValueError as exc:
        raise ProviderFailure(f"{provider} {what} returned non-json", body=response.text[:400]) from exc
    if not isinstance(payload, dict):
        raise ProviderFailure(f"{provider} {what} returned {type(payload).__name__}, not an object")
    return payload


def fan_out(engines: list, query: str, limit: int = 8) -> dict:
    """ask every configured engine, merge by url, and keep who found what.

    an engine that fails is reported rather than dropped: "querit timed out" is a fact about
    the answer, and a merged list that hides it looks like a consensus nobody reached.
    """
    merged: dict[str, dict] = {}
    errors: list[dict] = []
    asked: list[str] = []
    for engine in engines:
        if not engine.configured():
            continue
        asked.append(engine.status()["kind"])
        try:
            rows = engine.search(query, limit=limit)
        except ProviderFailure as exc:
            errors.append({"provider": exc.details.get("provider") or "", "error": exc.message})
            continue
        for row in rows:
            if not row["url"]:
                continue
            held = merged.get(row["url"])
            if held is None:
                merged[row["url"]] = {**row, "engines": [row["provider"]], "agreement": 1}
                continue
            if row["provider"] not in held["engines"]:
                held["engines"].append(row["provider"])
                held["agreement"] = len(held["engines"])
            if row["score"] > held["score"]:
                held["score"] = row["score"]
            if not held["snippet"] and row["snippet"]:
                held["snippet"] = row["snippet"]
    rows = sorted(merged.values(), key=lambda r: (-r["agreement"], -r["score"]))
    return {"query": query, "asked": asked, "errors": errors, "results": rows}


def make_tavily(api_key: str = "", base_url: str = TAVILY_BASE, transport=None,
                timeout: float = 60.0) -> TavilySearch:
    return TavilySearch(api_key=api_key, base_url=base_url, transport=transport, timeout=timeout)


def make_querit(api_key: str = "", base_url: str = QUERIT_BASE, transport=None,
                timeout: float = 60.0) -> QueritSearch:
    return QueritSearch(api_key=api_key, base_url=base_url, transport=transport, timeout=timeout)
