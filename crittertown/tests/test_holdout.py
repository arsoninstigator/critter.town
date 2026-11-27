"""the holdout wall: the criteria a crew is measured on, out of the crew's reach.

the failure this exists for does not look like a failure. an agent that can read its own
acceptance criteria passes them, so the metric rises while the work does not, and the only
way to tell the difference is to know the generator could not read the test. this suite is
that knowledge, kept as tests rather than as a promise in a comment.
"""

from __future__ import annotations

import json
import unittest

from base import AppCase

from core.errors import NotAllowed, ValidationError

CRITERIA = [
    {"name": "greets", "input": "say hello to the new crew member", "expect": "welcome aboard"},
    {"name": "counts", "input": "how many legs does a tripod have", "expect": "three"},
]


class EchoModel:
    """a model that answers from the question, so a score can be asserted without pinning
    the order the scenarios happen to come back in."""

    def __init__(self, right: bool = True) -> None:
        self.right = right
        self.prompts: list[str] = []

    def act(self, system_prompt: str, prompt: str) -> dict:
        self.prompts.append(prompt)
        if "tripod" in prompt:
            answer = "three" if self.right else "four"
        elif "hello" in prompt:
            answer = "welcome aboard"
        else:
            answer = "no idea"
        return {"thought": "", "commands": [], "done": True, "message": {"body": answer}}


class SealTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew()
        self.doer = self.app.crews.add_agent(self.team["id"], "wren", role="doer")

    def test_sealing_stores_the_criteria_and_hides_what_they_say(self):
        state = self.app.holdout.seal(self.team["id"], CRITERIA)
        self.assertEqual(state["scenarios"], 2)
        self.assertEqual(sorted(state["names"]), ["counts", "greets"])
        # the state route is what a page reads: it must not carry the answers
        self.assertNotIn("welcome aboard", json.dumps(state))
        self.assertNotIn("tripod", json.dumps(state))

    def test_a_holdout_with_no_scenarios_is_refused(self):
        with self.assertRaises(ValidationError) as caught:
            self.app.holdout.seal(self.team["id"], [])
        self.assertEqual(caught.exception.details.get("reason"), "no_scenarios")

    def test_sealing_twice_is_refused(self):
        self.app.holdout.seal(self.team["id"], CRITERIA)
        with self.assertRaises(ValidationError) as caught:
            self.app.holdout.seal(self.team["id"], [{"input": "x" * 20, "expect": "y"}])
        self.assertEqual(caught.exception.details.get("reason"), "already_sealed")

    def test_a_scenario_without_an_expect_is_refused(self):
        with self.assertRaises(ValidationError) as caught:
            self.app.holdout.seal(self.team["id"], [{"input": "say hello to everyone"}])
        self.assertEqual(caught.exception.details.get("reason"), "bad_scenario")

    def test_a_scorer_that_cannot_decide_is_refused_at_seal_time(self):
        with self.assertRaises(ValidationError):
            self.app.holdout.seal(self.team["id"], [
                {"input": "say hello to everyone", "expect": "hi", "scorer": {"kind": "vibes"}}])

    def test_criteria_already_in_the_prompt_cannot_be_sealed(self):
        # the wall is decorative if the answer is already in the room
        self.app.crews.add_agent(
            self.team["id"], "leaky", role="doer",
            system_prompt="when asked, reply welcome aboard and nothing else")
        with self.assertRaises(ValidationError) as caught:
            self.app.holdout.seal(self.team["id"], CRITERIA)
        self.assertEqual(caught.exception.details.get("reason"), "criteria_already_in_context")
        self.assertEqual(self.app.holdout.state(self.team["id"])["scenarios"], 0)


class ReadTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew()
        self.doer = self.app.crews.add_agent(self.team["id"], "wren", role="doer")
        self.app.holdout.seal(self.team["id"], CRITERIA)

    def test_an_agent_may_not_read_the_criteria(self):
        with self.assertRaises(NotAllowed) as caught:
            self.app.holdout.scenarios(self.team["id"], actor=self.doer["id"])
        self.assertEqual(caught.exception.details.get("reason"), "holdout_is_sealed")

    def test_a_refused_read_is_a_row(self):
        with self.assertRaises(NotAllowed):
            self.app.holdout.scenarios(self.team["id"], actor=self.doer["id"])
        access = self.app.holdout.access(self.team["id"])
        self.assertTrue(access)
        self.assertTrue(access[0]["refused"])
        self.assertEqual(access[0]["actor"], self.doer["id"])

    def test_an_unknown_reader_is_refused_too(self):
        with self.assertRaises(NotAllowed):
            self.app.holdout.scenarios(self.team["id"], actor="curious")
        self.assertTrue(self.app.holdout.access(self.team["id"])[0]["refused"])

    def test_the_owner_may_read_them_and_the_read_is_logged(self):
        cases = self.app.holdout.scenarios(self.team["id"], actor="owner", reason="audit")
        self.assertEqual(len(cases), 2)
        self.assertEqual(sorted(c["expect"] for c in cases), ["three", "welcome aboard"])
        logged = self.app.holdout.access(self.team["id"])
        self.assertTrue(any(a["actor"] == "owner" and not a["refused"] for a in logged))
        self.assertTrue(any(a["reason"] == "audit" for a in logged))


class VerifyTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew()
        self.app.crews.add_agent(self.team["id"], "wren", role="doer")
        self.app.holdout.seal(self.team["id"], CRITERIA)

    def test_a_clean_crew_verifies_and_the_check_actually_composed_something(self):
        verdict = self.app.holdout.verify(self.team["id"])
        self.assertTrue(verdict["ok"])
        self.assertEqual(verdict["scenarios"], 2)
        # a verify that composed nothing would pass for the wrong reason
        self.assertGreater(verdict["checked"], 0)

    def test_criteria_planted_after_the_seal_are_found(self):
        self.app.crews.add_agent(self.team["id"], "planted", role="doer",
                                 system_prompt="answer welcome aboard when greeted")
        verdict = self.app.holdout.verify(self.team["id"])
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["leaks"][0]["source"], "agent_prompt")

    def test_a_skill_carrying_the_answer_is_found(self):
        self.app.skills.create(
            self.team["id"],
            "---\nname: greeting\n"
            "description: use this when greeting somebody, and say welcome aboard\n---\n"
            "always say welcome aboard.")
        verdict = self.app.holdout.verify(self.team["id"])
        self.assertFalse(verdict["ok"])
        self.assertIn("skill", [l["source"] for l in verdict["leaks"]])


class RunTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew()
        self.doer = self.app.crews.add_agent(self.team["id"], "wren", role="doer")
        self.app.holdout.seal(self.team["id"], CRITERIA)

    def test_a_run_scores_the_criteria_and_records_a_clean_metric(self):
        model = EchoModel()
        out = self.app.holdout.run(self.team["id"], model)
        self.assertEqual(out["metric"], 1.0)
        self.assertEqual(out["scenarios"], 2)
        self.assertTrue(out["clean"])
        self.assertEqual(self.app.holdout.state(self.team["id"])["last_metric"], 1.0)

    def test_a_run_is_tainted_when_the_crew_tried_to_look(self):
        with self.assertRaises(NotAllowed):
            self.app.holdout.scenarios(self.team["id"], actor=self.doer["id"])
        out = self.app.holdout.run(self.team["id"], EchoModel())
        # the number is still recorded: a metric with a footnote beats a metric with a lie
        self.assertEqual(out["metric"], 1.0)
        self.assertFalse(out["clean"])
        self.assertEqual(out["access_during"], 1)

    def test_a_run_needs_a_model(self):
        with self.assertRaises(ValidationError) as caught:
            self.app.holdout.run(self.team["id"], None)
        self.assertEqual(caught.exception.details.get("reason"), "no_model")

    def test_a_wrong_answer_lowers_the_metric(self):
        out = self.app.holdout.run(self.team["id"], EchoModel(right=False))
        self.assertEqual(out["metric"], 0.5)

    def test_the_template_does_not_carry_the_criteria(self):
        doc = self.app.templates.export(self.team["id"])
        self.assertNotIn("welcome aboard", json.dumps(doc))
        self.assertNotIn("holdout", json.dumps(doc))

class FingerprintTests(AppCase):
    """a seal nobody can check is a promise. these hold the check.

    the failure: the criteria are rows, and a row can be edited between the claim and the
    gate. the crew still cannot read them, so the wall looks intact, and the metric is now
    measuring something nobody agreed to. band's dark factory submissions register their
    acceptance suite by hash before any work is accepted, for exactly this reason.
    """

    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew()

    def edit(self, name: str, expect: str) -> None:
        with self.app.store.tx() as c:
            c.execute("update holdout_scenarios set expect = ? where team_id = ? and name = ?",
                      (expect, self.team["id"], name))

    def test_a_fresh_seal_is_untouched_and_carries_a_fingerprint(self):
        state = self.app.holdout.seal(self.team["id"], CRITERIA)
        self.assertEqual(len(state["digest"]), 64)
        self.assertEqual(state["tampered"], [])

    def test_identical_criteria_in_two_crews_get_the_same_fingerprint(self):
        # the digest is over what was sealed, not over who sealed it or when
        first = self.app.holdout.seal(self.team["id"], CRITERIA)["digest"]
        other = self.crew("second")
        self.assertEqual(self.app.holdout.seal(other["id"], CRITERIA)["digest"], first)

    def test_the_order_of_the_scenarios_does_not_change_the_fingerprint(self):
        first = self.app.holdout.seal(self.team["id"], CRITERIA)["digest"]
        other = self.crew("second")
        self.assertEqual(self.app.holdout.seal(other["id"], list(reversed(CRITERIA)))["digest"],
                         first)

    def test_an_edited_scenario_is_named(self):
        self.app.holdout.seal(self.team["id"], CRITERIA)
        self.edit(CRITERIA[0]["name"], "something else entirely")
        state = self.app.holdout.state(self.team["id"])
        self.assertEqual(len(state["tampered"]), 1)
        self.assertIn(CRITERIA[0]["name"], state["tampered"][0])

    def test_a_removed_scenario_is_reported_as_a_size_change(self):
        self.app.holdout.seal(self.team["id"], CRITERIA)
        with self.app.store.tx() as c:
            c.execute("delete from holdout_scenarios where team_id = ? and name = ?",
                      (self.team["id"], CRITERIA[0]["name"]))
        self.assertTrue(any("sealed with" in item
                            for item in self.app.holdout.state(self.team["id"])["tampered"]))

    def test_an_untouched_seal_verifies(self):
        self.app.holdout.seal(self.team["id"], CRITERIA)
        self.assertTrue(self.app.holdout.verify(self.team["id"])["ok"])

    def test_a_tampered_seal_fails_the_wall_check(self):
        self.app.holdout.seal(self.team["id"], CRITERIA)
        self.edit(CRITERIA[1]["name"], "not the criteria")
        checked = self.app.holdout.verify(self.team["id"])
        self.assertFalse(checked["ok"])
        self.assertTrue(checked["tampered"])

    def test_a_run_on_a_tampered_set_is_not_clean(self):
        self.app.holdout.seal(self.team["id"], CRITERIA)
        self.edit(CRITERIA[0]["name"], "something else entirely")
        out = self.app.holdout.run(self.team["id"], EchoModel())
        self.assertFalse(out["clean"])
        self.assertTrue(out["tampered"])
        # the number is still recorded: a metric with a footnote beats a metric with a lie
        self.assertIn("metric", out)
