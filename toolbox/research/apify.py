"""apify: a dataset rather than a search.

an actor run is asynchronous by design, so this client never pretends otherwise: `run` starts
one and returns the run, `wait` polls until it stops or the deadline passes, and `items` reads
the dataset. a caller that only wants "collect this list" gets one method that does all three
and reports which stage it reached, because "the run is still going" and "the run produced
nothing" are different answers and only one of them is a result.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from core.errors import ProviderFailure, ProviderNotConfigured

APIFY_BASE = "https://api.apify.com/v2"
TERMINAL = ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT")


@dataclass
class ApifyClient:
    token: str = ""
    user_id: str = ""
    base_url: str = APIFY_BASE
    timeout: float = 60.0
    transport: httpx.BaseTransport | None = None
    # a test cannot sleep, so the poll interval is a field
    poll_seconds: float = 2.0

    def configured(self) -> bool:
        return bool(self.token)

    def status(self) -> dict:
        return {"kind": "apify", "called": True, "base_url": self.base_url,
                "configured": self.configured(), "user_id": self.user_id,
                "needs": "CRITTERTOWN_APIFY_TOKEN",
                "calls": ["run", "wait", "items", "collect", "me"]}

    # ---- transport -----------------------------------------------------
    def _request(self, method: str, path: str, what: str, **kwargs) -> dict:
        if not self.configured():
            raise ProviderNotConfigured("apify has no api token", provider="apify",
                                        hint="set CRITTERTOWN_APIFY_TOKEN, or stop calling it")
        params = dict(kwargs.pop("params", {}) or {})
        params["token"] = self.token
        try:
            with httpx.Client(base_url=self.base_url, timeout=self.timeout,
                              transport=self.transport) as session:
                response = session.request(method, path, params=params, **kwargs)
        except httpx.HTTPError as exc:
            raise ProviderFailure(f"apify {what} did not answer: {exc}", provider="apify") from exc
        if response.status_code >= 400:
            raise ProviderFailure(f"apify {what} returned {response.status_code}",
                                  provider="apify", status=response.status_code,
                                  body=response.text[:400])
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderFailure(f"apify {what} returned non-json", body=response.text[:400]) from exc
        return payload if isinstance(payload, dict) else {"data": payload}

    # ---- the calls -----------------------------------------------------
    def me(self) -> dict:
        return self._request("GET", "/users/me", "whoami").get("data") or {}

    def run(self, actor_id: str, run_input: dict | None = None) -> dict:
        actor = str(actor_id).replace("/", "~")
        payload = self._request("POST", f"/acts/{actor}/runs", f"run of {actor_id}",
                                json=run_input or {})
        return payload.get("data") or {}

    def run_status(self, run_id: str) -> dict:
        return self._request("GET", f"/actor-runs/{run_id}", f"status of {run_id}").get("data") or {}

    def wait(self, run_id: str, deadline_seconds: float = 120.0) -> dict:
        """poll until the run stops, and say so rather than hanging."""
        waited = 0.0
        while True:
            run = self.run_status(run_id)
            if run.get("status") in TERMINAL:
                return run
            if waited >= deadline_seconds:
                return {**run, "status": run.get("status") or "RUNNING", "timed_out": True}
            time.sleep(self.poll_seconds)
            waited += self.poll_seconds

    def items(self, dataset_id: str, limit: int = 100) -> list[dict]:
        payload = self._request("GET", f"/datasets/{dataset_id}/items", f"items of {dataset_id}",
                                params={"limit": max(1, int(limit)), "format": "json"})
        # the dataset endpoint answers with a bare array, which the shared request helper
        # wraps so the rest of the client can assume an object
        if isinstance(payload, list):
            rows = payload
        else:
            rows = payload.get("data") if isinstance(payload.get("data"), list) else payload.get("items")
        return [row for row in (rows or []) if isinstance(row, dict)]

    def collect(self, actor_id: str, run_input: dict | None = None, limit: int = 100,
                deadline_seconds: float = 120.0) -> dict:
        """start, wait, read. one call for the common case, and it names the stage it reached."""
        run = self.run(actor_id, run_input)
        run_id = str(run.get("id") or "")
        if not run_id:
            raise ProviderFailure("apify did not return a run id", provider="apify", body=run)
        settled = self.wait(run_id, deadline_seconds=deadline_seconds)
        if settled.get("timed_out"):
            return {"stage": "running", "run_id": run_id, "status": settled.get("status"),
                    "items": []}
        if settled.get("status") != "SUCCEEDED":
            return {"stage": "failed", "run_id": run_id, "status": settled.get("status"),
                    "items": []}
        dataset_id = str(settled.get("defaultDatasetId") or "")
        if not dataset_id:
            return {"stage": "no_dataset", "run_id": run_id, "status": "SUCCEEDED", "items": []}
        rows = self.items(dataset_id, limit=limit)
        return {"stage": "done", "run_id": run_id, "status": "SUCCEEDED",
                "dataset_id": dataset_id, "items": rows}


def make_apify(token: str = "", user_id: str = "", base_url: str = APIFY_BASE, transport=None,
               timeout: float = 60.0, poll_seconds: float = 2.0) -> ApifyClient:
    return ApifyClient(token=token, user_id=user_id, base_url=base_url, transport=transport,
                       timeout=timeout, poll_seconds=poll_seconds)
