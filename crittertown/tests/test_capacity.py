"""a crew's model capacity is the allowance its providers give it, spread across the keys it connects.

the properties under test are the ones that make this honest: there is no default budget and a lane
with no stated limit is uncapped, a limit of zero is refused because it reads as unlimited and means the
reverse, measured and estimated tokens are never folded together, a 429 cools a lane for the wait the
provider itself stated, work goes to the lane with the most room, and a run that finds nothing usable
parks without spending an attempt.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from base import AppCase  # noqa: E402
from core.db import now  # noqa: E402
from core.errors import Conflict, NotFound, ValidationError  # noqa: E402
from work import capacity as cap  # noqa: E402


class PureTests(unittest.TestCase):
    def test_durations_read_the_way_providers_write_them(self):
        self.assertEqual(cap.parse_duration("1s"), 1.0)
        self.assertEqual(cap.parse_duration("6m0s"), 360.0)
        self.assertEqual(cap.parse_duration("200ms"), 0.2)
        self.assertEqual(cap.parse_duration("2h"), 7200.0)
        self.assertEqual(cap.parse_duration("1.5s"), 1.5)

    def test_a_duration_that_is_not_one_is_none_not_zero(self):
        for text in ("", "soon", "ms", "5x", "m5"):
            self.assertIsNone(cap.parse_duration(text), text)

    def test_retry_after_in_seconds_wins(self):
        self.assertEqual(cap.reset_in({"Retry-After": "7"}, 0), 7.0)

    def test_the_openai_reset_header_is_read(self):
        self.assertEqual(cap.reset_in({"x-ratelimit-reset-tokens": "6m0s"}, 0), 360.0)

    def test_no_headers_means_no_answer_and_not_a_guess(self):
        self.assertIsNone(cap.reset_in({}, 0))
        self.assertIsNone(cap.reset_in(None, 0))

    def test_a_day_is_a_utc_day(self):
        self.assertEqual(cap.window_start("day", 86400 * 3 + 500), 86400 * 3)

    def test_an_unknown_window_is_refused(self):
        with self.assertRaises(ValidationError):
            cap.window_start("fortnight")

    def test_the_first_present_usage_name_is_read(self):
        self.assertEqual(cap._first_int({"input_tokens": 9}, "prompt_tokens", "input_tokens"), 9)
        self.assertIsNone(cap._first_int({}, "prompt_tokens"))
        self.assertIsNone(cap._first_int({"prompt_tokens": -3}, "prompt_tokens"))


class CapCase(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew()
        self.tid = self.team["id"]
        self.cap = self.app.capacity
        self.env = {"KEY_A": "x", "KEY_B": "y", "KEY_C": "z"}
        self.cap.environ = self.env

    def lane(self, label="a", token_env="KEY_A", model="m1", limits=None, weight=1.0, **kw):
        args = dict(label=label, provider="openai", base_url="https://api.example.com/v1",
                    model=model, token_env=token_env, limits=limits, weight=weight)
        args.update(kw)
        return self.cap.connect(self.tid, **args)


class ConnectTests(CapCase):
    def test_a_lane_with_no_limits_is_uncapped(self):
        lane = self.lane()
        self.assertFalse(lane["capped"])
        self.assertEqual(lane["limits"], {})

    def test_no_default_number_is_supplied(self):
        self.assertEqual(self.lane()["limits"], {})

    def test_a_stated_allowance_is_kept(self):
        lane = self.lane(limits={"tokens": {"day": 2000000, "minute": 90000}})
        self.assertEqual(lane["limits"]["tokens"]["day"], 2000000)
        self.assertTrue(lane["capped"])

    def test_zero_is_refused_because_it_reads_as_unlimited(self):
        with self.assertRaises(ValidationError) as caught:
            self.lane(limits={"tokens": {"day": 0}})
        self.assertIn("zero", str(caught.exception))

    def test_a_negative_limit_is_refused(self):
        with self.assertRaises(ValidationError):
            self.lane(limits={"tokens": {"day": -5}})

    def test_an_unknown_limit_kind_or_window_is_refused(self):
        with self.assertRaises(ValidationError):
            self.lane(limits={"vibes": {"day": 5}})
        with self.assertRaises(ValidationError):
            self.lane(label="b", limits={"tokens": {"fortnight": 5}})

    def test_a_limit_is_a_whole_number(self):
        with self.assertRaises(ValidationError):
            self.lane(limits={"tokens": {"day": "lots"}})

    def test_a_lane_names_the_variable_never_the_key(self):
        with self.assertRaises(ValidationError):
            self.lane(token_env="sk-proj-abc 123")
        with self.assertRaises(ValidationError):
            self.lane(token_env="")

    def test_the_url_is_https_or_loopback(self):
        with self.assertRaises(ValidationError):
            self.lane(base_url="http://example.com/v1")
        self.assertTrue(self.lane(label="local", base_url="http://localhost:11434/v1")["id"])

    def test_a_model_is_required(self):
        with self.assertRaises(ValidationError):
            self.lane(model="")

    def test_a_label_is_unique_per_crew(self):
        self.lane()
        with self.assertRaises(Conflict):
            self.lane()

    def test_weight_must_be_above_zero(self):
        with self.assertRaises(ValidationError):
            self.lane(weight=0)

    def test_an_unknown_crew_is_refused(self):
        with self.assertRaises(NotFound):
            self.cap.connect("nope", "a", "openai", "https://x.com/v1", "m", "KEY_A")

    def test_limits_can_be_changed_and_cleared(self):
        lane = self.lane(limits={"tokens": {"day": 100}})
        self.assertEqual(self.cap.set_limits(lane["id"], {})["limits"], {})


class MeasureTests(CapCase):
    def test_a_usage_block_is_recorded_as_measured(self):
        lane = self.lane()
        got = self.cap.record(lane["id"], None, {"prompt_tokens": 100, "completion_tokens": 40})
        self.assertTrue(got["measured"])
        self.assertEqual(got["tokens"], 140)

    def test_the_input_output_names_are_read_too(self):
        lane = self.lane()
        got = self.cap.record(lane["id"], None, {"input_tokens": 10, "output_tokens": 5})
        self.assertEqual(got["tokens"], 15)

    def test_a_call_with_no_usage_is_unmeasured_and_never_guessed(self):
        lane = self.lane()
        got = self.cap.record(lane["id"], None, None)
        self.assertFalse(got["measured"])
        self.assertEqual(got["tokens"], 0)

    def test_estimated_and_measured_are_kept_apart(self):
        lane = self.lane()
        self.cap.record(lane["id"], None, {"prompt_tokens": 100, "completion_tokens": 0})
        self.cap.record(lane["id"], None, {"prompt_tokens": 50, "completion_tokens": 0},
                        estimated=True)
        used = self.cap.used(lane["id"], "day")
        self.assertEqual(used["measured_tokens"], 100)
        self.assertEqual(used["estimated_tokens"], 50)

    def test_an_estimate_is_never_flagged_as_measured(self):
        lane = self.lane()
        got = self.cap.record(lane["id"], None, {"prompt_tokens": 9, "completion_tokens": 1},
                              estimated=True)
        self.assertFalse(got["measured"])

    def test_usage_is_counted_per_window(self):
        lane = self.lane()
        self.cap.record(lane["id"], None, {"prompt_tokens": 10, "completion_tokens": 0},
                        at=now() - 2 * 86400)
        self.assertEqual(self.cap.used(lane["id"], "day")["measured_tokens"], 0)

    def test_requests_and_refusals_are_counted(self):
        lane = self.lane()
        self.cap.record(lane["id"], None, {"prompt_tokens": 1, "completion_tokens": 1})
        self.cap.record(lane["id"], None, None, status=429)
        used = self.cap.used(lane["id"], "day")
        self.assertEqual(used["requests"], 2)
        self.assertEqual(used["refused"], 1)


class CoolingTests(CapCase):
    def test_a_429_cools_the_lane_for_the_providers_stated_wait(self):
        lane = self.lane()
        moment = now()
        self.cap.record(lane["id"], None, None, headers={"retry-after": "30"}, status=429,
                        at=moment)
        self.assertAlmostEqual(self.cap._lane(lane["id"])["cooling_until"], moment + 30, places=3)

    def test_a_429_with_no_stated_wait_falls_back_and_says_it_is_a_fallback(self):
        lane = self.lane()
        moment = now()
        self.cap.record(lane["id"], None, None, status=429, at=moment)
        self.assertAlmostEqual(self.cap._lane(lane["id"])["cooling_until"],
                               moment + cap.FALLBACK_COOLDOWN, places=3)

    def test_a_cooling_lane_is_not_usable(self):
        lane = self.lane()
        self.cap.record(lane["id"], None, None, headers={"retry-after": "30"}, status=429)
        room = self.cap.room(self.cap._lane(lane["id"]))
        self.assertFalse(room["usable"])
        self.assertEqual(room["why"], "cooling")

    def test_it_is_usable_again_once_the_wait_passes(self):
        lane = self.lane()
        moment = now()
        self.cap.record(lane["id"], None, None, headers={"retry-after": "30"}, status=429,
                        at=moment)
        self.assertTrue(self.cap.room(self.cap._lane(lane["id"]), at=moment + 31)["usable"])

    def test_a_shorter_later_429_never_shortens_an_existing_cooldown(self):
        lane = self.lane()
        moment = now()
        self.cap.record(lane["id"], None, None, headers={"retry-after": "100"}, status=429,
                        at=moment)
        self.cap.record(lane["id"], None, None, headers={"retry-after": "5"}, status=429,
                        at=moment + 1)
        self.assertAlmostEqual(self.cap._lane(lane["id"])["cooling_until"], moment + 100, places=3)


class RoomTests(CapCase):
    def test_an_uncapped_lane_has_no_remaining_figure_and_is_usable(self):
        room = self.cap.room(self.lane())
        self.assertTrue(room["usable"])
        self.assertEqual(room["why"], "uncapped")
        self.assertIsNone(room["remaining"])

    def test_a_capped_lane_reports_what_is_left(self):
        lane = self.lane(limits={"tokens": {"day": 1000}})
        self.cap.record(lane["id"], None, {"prompt_tokens": 300, "completion_tokens": 100})
        room = self.cap.room(self.cap._lane(lane["id"]))
        self.assertEqual(room["remaining"], 600)

    def test_the_tightest_window_decides_and_is_named(self):
        lane = self.lane(limits={"tokens": {"day": 1000000, "minute": 1000}})
        self.cap.record(lane["id"], None, {"prompt_tokens": 900, "completion_tokens": 0})
        room = self.cap.room(self.cap._lane(lane["id"]))
        self.assertEqual(room["binding"]["window"], "minute")

    def test_a_spent_lane_is_unusable(self):
        lane = self.lane(limits={"tokens": {"day": 100}})
        self.cap.record(lane["id"], None, {"prompt_tokens": 100, "completion_tokens": 0})
        room = self.cap.room(self.cap._lane(lane["id"]))
        self.assertFalse(room["usable"])
        self.assertEqual(room["why"], "allowance_spent")

    def test_request_limits_count_calls(self):
        lane = self.lane(limits={"requests": {"minute": 2}})
        self.cap.record(lane["id"], None, {"prompt_tokens": 1, "completion_tokens": 1})
        self.cap.record(lane["id"], None, {"prompt_tokens": 1, "completion_tokens": 1})
        self.assertFalse(self.cap.room(self.cap._lane(lane["id"]))["usable"])

    def test_a_missing_key_makes_a_lane_unusable_without_reading_it(self):
        self.env.pop("KEY_A")
        room = self.cap.room(self.lane())
        self.assertFalse(room["usable"])
        self.assertEqual(room["why"], "key_not_set")

    def test_a_disabled_lane_is_unusable(self):
        lane = self.lane()
        self.cap.set_state(lane["id"], "disabled")
        self.assertEqual(self.cap.room(self.cap._lane(lane["id"]))["why"], "disabled")


class PickTests(CapCase):
    def test_no_lane_connected_says_so(self):
        got = self.cap.pick(self.tid)
        self.assertIsNone(got["lane"])
        self.assertIn("no lane", got["reason"])

    def test_the_lane_with_the_most_room_wins(self):
        a = self.lane("a", "KEY_A", limits={"tokens": {"day": 1000}})
        b = self.lane("b", "KEY_B", limits={"tokens": {"day": 1000}})
        self.cap.record(a["id"], None, {"prompt_tokens": 800, "completion_tokens": 0})
        self.assertEqual(self.cap.pick(self.tid)["lane"]["id"], b["id"])

    def test_work_moves_when_one_key_runs_dry(self):
        a = self.lane("a", "KEY_A", limits={"tokens": {"day": 100}})
        b = self.lane("b", "KEY_B", limits={"tokens": {"day": 100000}})
        self.cap.record(a["id"], None, {"prompt_tokens": 100, "completion_tokens": 0})
        got = self.cap.pick(self.tid)
        self.assertEqual(got["lane"]["id"], b["id"])
        self.assertEqual(got["skipped"][0]["why"], "allowance_spent")

    def test_a_cooling_lane_is_skipped_and_named(self):
        a = self.lane("a", "KEY_A")
        b = self.lane("b", "KEY_B")
        self.cap.record(a["id"], None, None, headers={"retry-after": "60"}, status=429)
        got = self.cap.pick(self.tid)
        self.assertEqual(got["lane"]["id"], b["id"])
        self.assertIn("cooling", [s["why"] for s in got["skipped"]])

    def test_a_weight_breaks_a_tie(self):
        self.lane("a", "KEY_A", weight=1)
        b = self.lane("b", "KEY_B", weight=3)
        self.assertEqual(self.cap.pick(self.tid)["lane"]["id"], b["id"])

    def test_a_model_can_be_asked_for(self):
        self.lane("a", "KEY_A", model="small")
        b = self.lane("b", "KEY_B", model="large")
        self.assertEqual(self.cap.pick(self.tid, model="large")["lane"]["id"], b["id"])

    def test_a_call_too_big_for_what_is_left_is_skipped(self):
        a = self.lane("a", "KEY_A", limits={"tokens": {"day": 1000}})
        self.cap.record(a["id"], None, {"prompt_tokens": 900, "completion_tokens": 0})
        got = self.cap.pick(self.tid, need_tokens=500)
        self.assertIsNone(got["lane"])
        self.assertEqual(got["skipped"][0]["why"], "too_little_left")

    def test_every_lane_dry_returns_every_reason_and_raises_nothing(self):
        a = self.lane("a", "KEY_A", limits={"tokens": {"day": 10}})
        self.lane("b", "KEY_B")
        self.cap.record(a["id"], None, {"prompt_tokens": 10, "completion_tokens": 0})
        self.env.pop("KEY_B")
        got = self.cap.pick(self.tid)
        self.assertIsNone(got["lane"])
        self.assertEqual(sorted(s["why"] for s in got["skipped"]),
                         ["allowance_spent", "key_not_set"])

    def test_an_uncapped_lane_still_gets_picked(self):
        a = self.lane("a", "KEY_A")
        self.assertEqual(self.cap.pick(self.tid)["lane"]["id"], a["id"])


class ReadingTests(CapCase):
    def test_the_reading_says_an_uncapped_lane_is_uncapped(self):
        self.lane()
        reading = self.cap.reading(self.tid)
        self.assertEqual(reading["capped_lanes"], 0)
        self.assertIn("no number", reading["note"])

    def test_the_share_shows_a_lopsided_spread(self):
        a = self.lane("a", "KEY_A")
        b = self.lane("b", "KEY_B")
        self.cap.record(a["id"], None, {"prompt_tokens": 750, "completion_tokens": 0})
        self.cap.record(b["id"], None, {"prompt_tokens": 250, "completion_tokens": 0})
        shares = {s["label"]: s["share"] for s in self.cap.share(self.tid)}
        self.assertEqual(shares, {"a": 0.75, "b": 0.25})

    def test_an_empty_crew_shares_nothing(self):
        self.lane()
        self.assertEqual(self.cap.share(self.tid)[0]["share"], 0.0)


class ModelHookTests(CapCase):
    def test_the_model_reports_usage_and_headers_through_its_hook(self):
        from agents import runtime
        seen = []
        original = runtime.request_json
        runtime.request_json = lambda *a, **k: (
            {"choices": [{"message": {"content": "{}"}}],
             "usage": {"prompt_tokens": 12, "completion_tokens": 3}},
            {"x-ratelimit-remaining-tokens": "900"})
        try:
            model = runtime.OpenAICompatibleModel(
                base_url="https://x.com/v1", api_key="k", model="m", on_call=seen.append)
            model.act("s", "p")
        finally:
            runtime.request_json = original
        self.assertEqual(seen[0]["usage"], {"prompt_tokens": 12, "completion_tokens": 3})
        self.assertEqual(seen[0]["status"], 200)

    def test_a_refusal_reports_the_providers_status_not_our_own(self):
        from agents import runtime
        from core.errors import ProviderFailure
        seen = []

        def boom(*a, **k):
            raise ProviderFailure("limited", status=429, body="", headers={"retry-after": "9"})

        original = runtime.request_json
        runtime.request_json = boom
        try:
            model = runtime.OpenAICompatibleModel(
                base_url="https://x.com/v1", api_key="k", model="m", on_call=seen.append)
            with self.assertRaises(ProviderFailure):
                model.act("s", "p")
        finally:
            runtime.request_json = original
        self.assertEqual(seen[0]["status"], 429)
        self.assertEqual(seen[0]["headers"], {"retry-after": "9"})


class DispatchTests(CapCase):
    def setUp(self) -> None:
        super().setUp()
        self.agent = self.app.crews.add_agent(self.tid, "anne", role="doer")

    def card(self):
        card = self.app.kanban.create_task(self.tid, "do it", description="x", column="ready")
        self.app.kanban.assign(card["id"], self.agent["id"])
        return card

    def test_a_crew_with_no_lane_runs_exactly_as_before(self):
        out = self.app.runner.dispatch(self.card()["id"], max_turns=1)
        self.assertTrue(out["run_id"])
        self.assertNotEqual(out.get("decision"), "env_blocked")

    def test_a_crew_whose_every_lane_is_spent_parks_without_spending_an_attempt(self):
        lane = self.lane(limits={"tokens": {"day": 10}})
        self.cap.record(lane["id"], None, {"prompt_tokens": 10, "completion_tokens": 0})
        card = self.card()
        out = self.app.runner.dispatch(card["id"], max_turns=1)
        self.assertEqual(out["decision"], "env_blocked")
        self.assertEqual(out["reason"], "capacity_exhausted")
        after = self.app.kanban.get_task(card["id"])
        self.assertEqual(int(after.get("attempts") or 0), 0)
        self.assertEqual(after["column"], "ready")

    def test_repeated_capacity_blocks_stop_asking(self):
        lane = self.lane(limits={"tokens": {"day": 10}})
        self.cap.record(lane["id"], None, {"prompt_tokens": 10, "completion_tokens": 0})
        card = self.card()
        for _ in range(6):
            if self.app.kanban.get_task(card["id"])["column"] == "blocked":
                break
            self.app.runner.dispatch(card["id"], max_turns=1)
        after = self.app.kanban.get_task(card["id"])
        self.assertEqual(after["column"], "blocked")
        self.assertEqual(int(after.get("attempts") or 0), 0)
        with self.assertRaises(ValidationError):
            self.app.runner.dispatch(card["id"], max_turns=1)


class RouteTests(CapCase):
    def test_the_routes_answer(self):
        from fastapi.testclient import TestClient
        from api.routes import create_api
        client = TestClient(create_api(self.app))
        made = client.post(f"/crews/{self.tid}/lanes", json={
            "label": "main", "provider": "openai", "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o", "token_env": "KEY_A",
            "limits": {"tokens": {"day": 2000000}}}).json()
        self.assertEqual(made["label"], "main")
        refused = client.post(f"/crews/{self.tid}/lanes", json={
            "label": "bad", "base_url": "https://api.openai.com/v1", "model": "gpt-4o",
            "token_env": "KEY_B", "limits": {"tokens": {"day": 0}}})
        self.assertEqual(refused.status_code, 422)
        reading = client.get(f"/crews/{self.tid}/lanes").json()
        self.assertEqual(reading["capped_lanes"], 1)
        picked = client.get(f"/crews/{self.tid}/lanes/pick").json()
        self.assertEqual(picked["lane"]["id"], made["id"])
        cleared = client.post(f"/lanes/{made['id']}/limits", json={"limits": {}}).json()
        self.assertEqual(cleared["limits"], {})
        self.assertEqual(client.get(f"/crews/{self.tid}/lanes/share").status_code, 200)
