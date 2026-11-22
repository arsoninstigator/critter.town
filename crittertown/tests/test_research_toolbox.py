"""the research engines, held to the shape the loop needs.

the properties worth holding: every engine answers the same row, so a caller never learns a
vendor's field names; two engines finding the same url is recorded as agreement rather than
collapsed into one score; an engine that fails is reported beside the results instead of
quietly thinning them; and a provider without a key refuses before a request is made.
"""

from __future__ import annotations

import json
import unittest

import httpx

import base  # noqa: F401

from core.errors import ProviderFailure, ProviderNotConfigured
from toolbox.research import apify as apify_mod
from toolbox.research import search as search_mod


def transport(routes: dict, calls: list | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        if calls is not None:
            calls.append({"method": request.method, "path": request.url.path,
                          "body": json.loads(request.content or b"{}"),
                          "query": dict(request.url.params)})
        # the base url carries a path (/v1, /v2), so a route is matched on its suffix
        match = [k for k in routes if k[0] == key[0] and key[1].endswith(k[1])]
        if not match:
            return httpx.Response(404, json={"error": "no such route", "path": key[1]})
        status, body = routes[match[0]]
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler)


class NormalizeTests(unittest.TestCase):
    def test_a_row_has_the_same_fields_whatever_the_vendor_called_them(self):
        row = search_mod.normalize("querit", {"url": "https://x.test/a", "title": "A",
                                              "snippet": "s", "page_age": "2026-01-01"})
        self.assertEqual(row["url"], "https://x.test/a")
        self.assertEqual(row["published_at"], "2026-01-01")
        self.assertEqual(row["score"], 0.0)
        self.assertEqual(row["provider"], "querit")

    def test_a_missing_score_is_zero_and_never_a_string(self):
        self.assertEqual(search_mod.normalize("t", {"url": "u", "score": "n/a"})["score"], 0.0)
        self.assertEqual(search_mod.normalize("t", {"url": "u", "score": "0.5"})["score"], 0.5)


class TavilyTests(unittest.TestCase):
    def test_search_normalizes_and_names_the_key_it_needs(self):
        calls: list = []
        client = search_mod.make_tavily("k", transport=transport({("POST", "/search"): (200, {
            "results": [{"url": "https://x.test/a", "title": "A", "content": "body", "score": 0.9}]})},
            calls))
        rows = client.search("dark factory", limit=3)
        self.assertEqual(rows[0]["snippet"], "body")
        self.assertEqual(calls[0]["body"]["max_results"], 3)
        self.assertIn("CRITTERTOWN_TAVILY_API_KEY", client.status()["needs"])

    def test_no_key_refuses_before_a_request_is_made(self):
        calls: list = []
        client = search_mod.make_tavily("", transport=transport({}, calls))
        with self.assertRaises(ProviderNotConfigured):
            client.search("anything")
        self.assertEqual(calls, [])

    def test_a_500_is_a_provider_failure_not_an_empty_list(self):
        client = search_mod.make_tavily("k", transport=transport({("POST", "/search"): (500, {})}))
        with self.assertRaises(ProviderFailure):
            client.search("x")


class QueritTests(unittest.TestCase):
    def test_the_body_error_code_is_a_refusal(self):
        # querit answers 200 with an error inside the body, so a failed search would read
        # as an empty result if the code were not checked
        client = search_mod.make_querit("k", transport=transport({("POST", "/search"): (200, {
            "error_code": 400, "error_msg": "Missing request body", "results": {}})}))
        with self.assertRaises(ProviderFailure) as caught:
            client.search("x")
        self.assertIn("Missing request body", caught.exception.message)

    def test_a_normal_answer_is_flattened_from_results_result(self):
        client = search_mod.make_querit("k", transport=transport({("POST", "/search"): (200, {
            "error_code": 200, "results": {"result": [
                {"url": "https://y.test/b", "title": "B", "snippet": "s", "site_name": "Y"}]}})}))
        rows = client.search("x")
        self.assertEqual(rows[0]["site"], "Y")
        self.assertEqual(rows[0]["url"], "https://y.test/b")


class FanOutTests(unittest.TestCase):
    def engine(self, kind: str, rows: list[dict], fail: bool = False):
        class Engine:
            def __init__(self) -> None:
                self.kind = kind

            def configured(self) -> bool:
                return True

            def status(self) -> dict:
                return {"kind": kind}

            def search(self, query: str, limit: int = 8) -> list[dict]:
                if fail:
                    raise ProviderFailure(f"{kind} is down", provider=kind)
                return [search_mod.normalize(kind, row) for row in rows]

        return Engine()

    def test_two_engines_finding_one_url_is_agreement(self):
        a = self.engine("tavily", [{"url": "https://x.test/a", "score": 0.4},
                                   {"url": "https://x.test/b", "score": 0.9}])
        b = self.engine("querit", [{"url": "https://x.test/a", "score": 0.1}])
        out = search_mod.fan_out([a, b], "q")
        self.assertEqual(out["asked"], ["tavily", "querit"])
        self.assertEqual(out["results"][0]["url"], "https://x.test/a")
        self.assertEqual(out["results"][0]["agreement"], 2)
        self.assertEqual(out["results"][0]["engines"], ["tavily", "querit"])
        # the one engine that ranked it first does not outrank the one two engines found
        self.assertEqual(out["results"][1]["url"], "https://x.test/b")

    def test_a_failing_engine_is_reported_beside_the_results(self):
        a = self.engine("tavily", [{"url": "https://x.test/a"}])
        b = self.engine("querit", [], fail=True)
        out = search_mod.fan_out([a, b], "q")
        self.assertEqual(len(out["results"]), 1)
        self.assertEqual(out["errors"], [{"provider": "querit", "error": "querit is down"}])

    def test_an_unconfigured_engine_is_not_asked_and_not_reported(self):
        class Missing:
            def configured(self) -> bool:
                return False

            def status(self) -> dict:
                return {"kind": "querit"}

            def search(self, query: str, limit: int = 8):
                raise AssertionError("an unconfigured engine must not be asked")

        out = search_mod.fan_out([self.engine("tavily", [{"url": "https://x.test/a"}]), Missing()], "q")
        self.assertEqual(out["asked"], ["tavily"])
        self.assertEqual(out["errors"], [])

    def test_a_row_with_no_url_is_dropped(self):
        out = search_mod.fan_out([self.engine("tavily", [{"url": ""}, {"url": "https://x.test/a"}])], "q")
        self.assertEqual([r["url"] for r in out["results"]], ["https://x.test/a"])


class ApifyTests(unittest.TestCase):
    def client(self, routes: dict, calls: list | None = None) -> apify_mod.ApifyClient:
        return apify_mod.make_apify("t", "user-1", transport=transport(routes, calls),
                                    poll_seconds=0.0)

    def test_collect_walks_run_then_dataset_and_says_which_stage_it_reached(self):
        routes = {
            ("POST", "/acts/actor~one/runs"): (201, {"data": {"id": "run1", "status": "READY"}}),
            ("GET", "/actor-runs/run1"): (200, {"data": {"id": "run1", "status": "SUCCEEDED",
                                                         "defaultDatasetId": "ds1"}}),
            ("GET", "/datasets/ds1/items"): (200, [{"url": "https://x.test/a"}]),
        }
        out = self.client(routes).collect("actor/one", {"startUrls": []})
        self.assertEqual(out["stage"], "done")
        self.assertEqual(out["items"], [{"url": "https://x.test/a"}])

    def test_a_run_still_going_is_running_and_not_an_empty_result(self):
        routes = {
            ("POST", "/acts/a/runs"): (201, {"data": {"id": "run2", "status": "READY"}}),
            ("GET", "/actor-runs/run2"): (200, {"data": {"id": "run2", "status": "RUNNING"}}),
        }
        out = self.client(routes).collect("a", {}, deadline_seconds=0.0)
        self.assertEqual(out["stage"], "running")
        self.assertEqual(out["items"], [])

    def test_a_failed_run_is_failed_and_carries_no_items(self):
        routes = {
            ("POST", "/acts/a/runs"): (201, {"data": {"id": "run3"}}),
            ("GET", "/actor-runs/run3"): (200, {"data": {"id": "run3", "status": "FAILED"}}),
        }
        out = self.client(routes).collect("a", {})
        self.assertEqual(out["stage"], "failed")
        self.assertEqual(out["status"], "FAILED")

    def test_the_token_is_a_query_parameter_and_the_user_id_is_not(self):
        calls: list = []
        self.client({("GET", "/users/me"): (200, {"data": {"id": "user-1"}})}, calls).me()
        self.assertEqual(calls[0]["query"]["token"], "t")

    def test_no_token_refuses(self):
        with self.assertRaises(ProviderNotConfigured):
            apify_mod.make_apify("", transport=transport({})).me()
