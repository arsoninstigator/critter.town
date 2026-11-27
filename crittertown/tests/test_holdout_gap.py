"""the gap between what a crew could read and what it could not.

the failure this exists for: an agent that can read its acceptance examples passes them, so
the number goes up while the work does not. the tell is not the sealed rate on its own, it is
the distance between the examples the crew could see and the scenarios it could not. a crew
that clears every example and half the sealed set did not do the work, it did the examples.

grading is partial on purpose. three of five sealed scenarios is worse than five and better
than none, and rounding that to "failed" throws away the only number worth comparing next
week. what acceptance adds is that the sealed rate has a floor and the gap has a ceiling, so
passing the visible half accepts nothing on its own.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from base import AppCase  # noqa: E402
from core.errors import ValidationError  # noqa: E402
from work import holdout as hold  # noqa: E402


def cells(*scores: float) -> list[dict]:
    return [{"scenario": f"s{i}", "score": s} for i, s in enumerate(scores)]


class PartialTests(unittest.TestCase):
    def test_a_clean_crew_is_honest_and_accepted(self):
        found = hold.partial(cells(1, 1, 1), cells(1, 1, 1, 1))
        self.assertEqual(found["verdict"], "honest")
        self.assertTrue(found["accepted"])
        self.assertEqual(found["gap"], 0.0)
        self.assertEqual(found["holdout"]["rate"], 1.0)

    def test_fitting_the_examples_is_named(self):
        # every visible example right, half the sealed set right: the work fitted the examples
        found = hold.partial(cells(1, 1, 1, 1), cells(1, 0, 1, 0))
        self.assertEqual(found["verdict"], "fitted")
        self.assertFalse(found["accepted"])
        self.assertIn("fitted_to_the_examples", found["reasons"])
        self.assertEqual(found["gap"], 0.5)

    def test_doing_better_on_what_it_never_saw_is_not_a_failure(self):
        found = hold.partial(cells(0, 1, 0, 1), cells(1, 1, 1, 1))
        self.assertEqual(found["gap"], -0.5)
        self.assertEqual(found["verdict"], "honest")
        self.assertTrue(found["accepted"])

    def test_a_half_answer_is_a_miss(self):
        found = hold.partial(cells(1, 1), cells(0.5, 0.5, 0.5, 0.5))
        self.assertEqual(found["holdout"]["passed"], 0)
        self.assertEqual(found["holdout"]["rate"], 0.0)

    def test_three_of_five_is_a_number_not_a_fail(self):
        found = hold.partial(cells(1, 1, 1), cells(1, 1, 1, 0, 0))
        self.assertEqual(found["holdout"]["passed"], 3)
        self.assertEqual(found["holdout"]["rate"], 0.6)
        self.assertIn("holdout_below_floor", found["reasons"])
        self.assertFalse(found["accepted"])

    def test_a_rate_over_two_rows_is_a_coin_toss(self):
        found = hold.partial(cells(1, 1), cells(1, 1))
        self.assertEqual(found["verdict"], "thin")
        self.assertFalse(found["accepted"])
        self.assertIn("too_few_scenarios", found["reasons"])

    def test_no_visible_examples_means_no_gap(self):
        found = hold.partial([], cells(1, 1, 1, 1))
        self.assertIsNone(found["gap"])
        self.assertEqual(found["verdict"], "thin")
        self.assertIn("no_public_examples", found["reasons"])
        # the sealed half still decides: a crew with no examples is unmeasured, not failed
        self.assertNotIn("holdout_below_floor", found["reasons"])

    def test_no_sealed_scenarios_is_thin_not_perfect(self):
        found = hold.partial(cells(1, 1), [])
        self.assertIsNone(found["holdout"]["rate"])
        self.assertEqual(found["verdict"], "thin")
        self.assertFalse(found["accepted"])

    def test_the_floors_travel_with_the_answer(self):
        found = hold.partial(cells(1), cells(1, 1, 1, 1), min_holdout_rate=1.0)
        self.assertEqual(found["floors"]["min_holdout_rate"], 1.0)
        self.assertTrue(found["rules"])
        self.assertIn("gap is the public rate minus the sealed rate", " ".join(found["rules"]))


class FixtureModel:
    """answers from the question, and gets the sealed half right only if told to."""

    def __init__(self, sealed_right: bool = True) -> None:
        self.sealed_right = sealed_right

    def act(self, system_prompt: str, prompt: str) -> dict:
        if "tripod" in prompt:
            answer = "three"
        elif "hello" in prompt:
            answer = "welcome aboard"
        elif "harbour" in prompt:
            answer = "high water" if self.sealed_right else "no idea"
        else:
            answer = "no idea"
        return {"thought": "", "commands": [], "done": True, "message": {"body": answer}}


PUBLIC = [
    {"name": "greets", "input": "say hello to the new crew member", "expect": "welcome aboard"},
    {"name": "counts", "input": "how many legs does a tripod have", "expect": "three"},
]

SEALED = [
    {"name": "tide", "input": "what does the harbour board at high water say",
     "expect": "high water"},
    {"name": "tide2", "input": "what does the harbour board at low water say",
     "expect": "high water"},
    {"name": "tide3", "input": "read the harbour board for me", "expect": "high water"},
    {"name": "tide4", "input": "harbour board, again", "expect": "high water"},
]


class GapServiceTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew()
        self.app.crews.add_agent(self.team["id"], "wren", role="doer")

    def test_the_visible_examples_are_stored_and_readable(self):
        self.app.holdout.set_public(self.team["id"], PUBLIC)
        self.assertEqual([e["name"] for e in self.app.holdout.public(self.team["id"])],
                         ["greets", "counts"])
        self.app.holdout.set_public(self.team["id"], PUBLIC[:1])
        self.assertEqual(len(self.app.holdout.public(self.team["id"])), 1)

    def test_an_example_without_an_expect_is_refused(self):
        with self.assertRaises(ValidationError) as caught:
            self.app.holdout.set_public(self.team["id"], [{"name": "x", "input": "y"}])
        self.assertEqual(caught.exception.details.get("reason"), "example_incomplete")

    def test_an_unknown_scorer_is_refused(self):
        with self.assertRaises(ValidationError) as caught:
            self.app.holdout.set_public(self.team["id"], [
                {"name": "x", "input": "y", "expect": "z", "scorer": {"kind": "vibes"}}])
        self.assertEqual(caught.exception.details.get("reason"), "scorer_unknown")

    def test_a_fitted_crew_is_refused_and_recorded(self):
        self.app.holdout.set_public(self.team["id"], PUBLIC)
        self.app.holdout.seal(self.team["id"], SEALED)
        found = self.app.holdout.gap(self.team["id"], FixtureModel(sealed_right=False))
        self.assertEqual(found["public"]["rate"], 1.0)
        self.assertEqual(found["holdout"]["rate"], 0.0)
        self.assertEqual(found["verdict"], "fitted")
        self.assertFalse(found["accepted"])
        self.assertTrue(found["clean"])
        # the run is on record, so next week's number is a comparison
        self.assertEqual([g["id"] for g in self.app.holdout.gaps(self.team["id"])],
                         [found["id"]])

    def test_the_example_set_is_fingerprinted_into_the_record(self):
        # a gap against different examples is a different measurement, and the record says so
        self.app.holdout.set_public(self.team["id"], PUBLIC)
        self.app.holdout.seal(self.team["id"], SEALED)
        first = self.app.holdout.gap(self.team["id"], FixtureModel())
        self.assertTrue(first["public_digest"])
        self.app.holdout.set_public(self.team["id"], PUBLIC[:1])
        second = self.app.holdout.gap(self.team["id"], FixtureModel())
        self.assertNotEqual(first["public_digest"], second["public_digest"])
        self.assertEqual(len({g["public_digest"] for g in self.app.holdout.gaps(self.team["id"])}),
                         2)

    def test_a_crew_that_does_the_work_is_accepted(self):
        self.app.holdout.set_public(self.team["id"], PUBLIC)
        self.app.holdout.seal(self.team["id"], SEALED)
        found = self.app.holdout.gap(self.team["id"], FixtureModel())
        self.assertEqual(found["verdict"], "honest")
        self.assertTrue(found["accepted"])
        self.assertEqual(self.app.holdout.acceptance(self.team["id"])["accepted"], True)

    def test_acceptance_before_any_measurement_says_unmeasured(self):
        found = self.app.holdout.acceptance(self.team["id"])
        self.assertFalse(found["accepted"])
        self.assertEqual(found["verdict"], "unmeasured")
        self.assertEqual(found["reasons"], ["no_gap_run"])

    def test_a_tampered_seal_cannot_be_accepted(self):
        self.app.holdout.set_public(self.team["id"], PUBLIC)
        self.app.holdout.seal(self.team["id"], SEALED)
        found = self.app.holdout.gap(self.team["id"], FixtureModel())
        self.assertTrue(found["accepted"])
        # somebody edits a sealed row after the seal, so the run that follows is tainted
        with self.app.store.tx() as c:
            c.execute("update holdout_scenarios set expect = 'low water' where team_id = ?"
                      " and name = 'tide'", (self.team["id"],))
        again = self.app.holdout.gap(self.team["id"], FixtureModel())
        self.assertFalse(again["clean"])
        self.assertFalse(self.app.holdout.acceptance(self.team["id"])["accepted"])
        self.assertIn("tainted_run", self.app.holdout.acceptance(self.team["id"])["reasons"])

    def test_a_gap_without_a_seal_is_refused(self):
        self.app.holdout.set_public(self.team["id"], PUBLIC)
        with self.assertRaises(ValidationError) as caught:
            self.app.holdout.gap(self.team["id"], FixtureModel())
        self.assertEqual(caught.exception.details.get("reason"), "no_scenarios")

    def test_a_gap_without_a_model_is_refused(self):
        self.app.holdout.seal(self.team["id"], SEALED)
        with self.assertRaises(ValidationError) as caught:
            self.app.holdout.gap(self.team["id"], None)
        self.assertEqual(caught.exception.details.get("reason"), "no_model")

    def test_the_routes_answer(self):
        from fastapi.testclient import TestClient
        from api.routes import create_api
        client = TestClient(create_api(self.app))
        base = f"/crews/{self.team['id']}/holdout"
        self.assertEqual(client.get(f"{base}/public").json()["examples"], [])
        wrote = client.post(f"{base}/public", json={"examples": PUBLIC}).json()
        self.assertEqual(wrote["examples"], 2)
        client.post(f"{base}", json={"scenarios": SEALED})
        self.assertEqual(client.get(f"{base}/acceptance").json()["verdict"], "unmeasured")
        self.assertEqual(client.get(f"{base}/gap").json()["gaps"], [])
