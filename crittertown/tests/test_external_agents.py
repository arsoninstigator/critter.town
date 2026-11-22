"""an agent we do not run.

the two ways to let a foreign agent work here are an adapter per framework, which copies
someone else's code into this process, and a heartbeat, which does not. this suite holds the
second one to its promises: a beat returns a card the worker may hold and nothing else, the
key is the worker's own, a beat while holding renews instead of double dispatching, and the
boundary is a rule rather than a convention. a worker we do not run may hold work and may
never judge it.
"""

from __future__ import annotations

import unittest

from base import AppCase

from core.errors import NotAllowed
from agents import external
from work import roster as roster_module


class HiringTests(AppCase):
    def test_an_external_agent_is_hired_by_the_same_call(self):
        team = self.crew()
        worker = self.app.crews.add_agent(team["id"], "kilo", role="doer", hosting="external")
        self.assertEqual(worker["hosting"], "external")
        self.assertTrue(worker["agent_key"])

    def test_hosting_is_validated(self):
        team = self.crew()
        with self.assertRaises(Exception) as caught:
            self.app.crews.add_agent(team["id"], "kilo", hosting="somewhere")
        self.assertIn("hosting", str(caught.exception))

    def test_a_local_agent_is_local_by_default(self):
        team = self.crew()
        worker = self.app.crews.add_agent(team["id"], "wren", role="doer")
        self.assertEqual(worker["hosting"], "local")


class BoundaryTests(AppCase):
    def test_an_external_agent_may_hold_work(self):
        self.assertTrue(external.may_hold({"hosting": "external"}, "doer")["ok"])
        self.assertTrue(external.may_hold({"hosting": "external"}, "researcher")["ok"])

    def test_an_external_agent_may_not_judge(self):
        self.assertFalse(external.may_hold({"hosting": "external"}, "reviewer")["ok"])
        self.assertFalse(external.may_hold({"hosting": "external"}, "integrator")["ok"])
        self.assertFalse(external.may_review({"hosting": "external", "id": "a"})["ok"])

    def test_a_local_agent_is_never_asked(self):
        self.assertTrue(external.may_review({"hosting": "local"})["ok"])
        self.assertTrue(external.may_hold({"hosting": "local"}, "reviewer")["ok"])

    def test_the_roster_will_not_seat_one_as_a_reviewer(self):
        """the shape asks for a reviewer, the only free agent is a worker we do not run, so
        the seat stays empty and says why rather than being filled by whoever is around."""
        team = self.crew()
        self.app.crews.add_agent(team["id"], "kilo", role="reviewer", hosting="external")
        crew = self.app.crews.list_agents(team["id"])
        # the signals are declared, not guessed: one item of work with parts that can be
        # worked apart is the shape that wants an integrator seat
        roster = roster_module.form(
            {"question": "known", "shape": "one", "parts": "independent",
             "check": "programmatic", "stakes": "reversible", "seats": 2}, crew)
        self.assertEqual(roster["shape"], "orchestration")
        judging = [seat for seat in roster["seats"] if seat["role"] == "integrator"]
        self.assertTrue(judging)
        self.assertEqual(judging[0]["agent_id"], "")
        self.assertIn("no_agent_for_role:integrator", roster["gaps"])
        # and the refusal is about hosting, not about the agent: run by us, it may sit there
        self.assertFalse(external.may_hold({"hosting": "external"}, "reviewer")["ok"])
        self.assertTrue(external.may_hold({"hosting": "local"}, "reviewer")["ok"])

    def test_the_review_gate_refuses_an_external_agent(self):
        team = self.crew()
        worker = self.app.crews.add_agent(team["id"], "kilo", role="doer", hosting="external")
        card = self.app.kanban.create_task(team["id"], "draft", payload={"kind": "code"},
                                          column="ready")
        self.app.kanban.move(card["id"], "in_progress", "owner")
        self.app.kanban.move(card["id"], "review", "owner")
        with self.assertRaises(NotAllowed) as caught:
            self.app.kanban.approve(card["id"], worker["id"], evidence="commit:abc1234")
        self.assertEqual(caught.exception.details.get("why"), "external_agent_cannot_review")


class HeartbeatTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew()
        self.worker = self.app.crews.add_agent(
            self.team["id"], "kilo", role="doer", hosting="external")
        self.card = self.app.kanban.create_task(
            self.team["id"], "do the thing", payload={"kind": "code", "slice": "doer"},
            column="ready")

    def test_a_beat_hands_over_a_card_and_the_scheme_it_closes_with(self):
        out = self.app.heartbeat.beat(self.worker["id"], key=self.worker["agent_key"])
        self.assertTrue(out["ok"])
        self.assertEqual(out["task"]["id"], self.card["id"])
        self.assertEqual(out["task"]["capability"], "doer")
        # "done" is not a guess: the card arrives with the references that close it
        self.assertIn("commit", out["task"]["evidence"])
        self.assertEqual(out["lease"]["holder"], self.worker["id"])
        self.assertEqual(self.app.kanban.get_task(self.card["id"])["lease"]["holder"],
                         self.worker["id"])

    def test_the_heartbeat_is_recorded(self):
        self.app.heartbeat.beat(self.worker["id"], key=self.worker["agent_key"])
        state = self.app.heartbeat.state(self.worker["id"])
        self.assertGreater(state["last_heartbeat"], 0)
        self.assertEqual(state["holding"]["id"], self.card["id"])

    def test_beating_again_while_holding_renews_instead_of_dispatching(self):
        self.app.heartbeat.beat(self.worker["id"], key=self.worker["agent_key"])
        again = self.app.heartbeat.beat(self.worker["id"], key=self.worker["agent_key"])
        self.assertEqual(again["why"], "already_holding")
        self.assertEqual(again["task"]["id"], self.card["id"])

    def test_the_key_must_be_the_agents_own(self):
        with self.assertRaises(NotAllowed) as caught:
            self.app.heartbeat.beat(self.worker["id"], key="not-the-key")
        self.assertEqual(caught.exception.details.get("why"), "key_mismatch")
        with self.assertRaises(NotAllowed):
            self.app.heartbeat.beat(self.worker["id"])
        self.assertEqual(self.app.kanban.get_task(self.card["id"])["lease"]["holder"], "")

    def test_a_local_agent_cannot_poll_for_work(self):
        wren = self.app.crews.add_agent(self.team["id"], "wren", role="doer")
        with self.assertRaises(NotAllowed) as caught:
            self.app.heartbeat.beat(wren["id"], key=wren["agent_key"])
        self.assertEqual(caught.exception.details.get("why"), "not_external")

    def test_a_card_addressed_elsewhere_is_not_offered(self):
        self.app.kanban.create_task(
            self.team["id"], "a design job", payload={"kind": "design", "slice": "designer"},
            column="ready")
        self.app.kanban.move(self.card["id"], "backlog", "owner")
        out = self.app.heartbeat.beat(self.worker["id"], key=self.worker["agent_key"])
        self.assertFalse(out["ok"])
        self.assertEqual(out["why"], "nothing_ready")

    def test_nothing_ready_is_the_quiet_answer(self):
        self.app.kanban.move(self.card["id"], "backlog", "owner")
        out = self.app.heartbeat.beat(self.worker["id"], key=self.worker["agent_key"])
        self.assertFalse(out["ok"])
        self.assertEqual(out["why"], "nothing_ready")

    def test_a_leased_card_is_not_handed_out_twice(self):
        other = self.app.crews.add_agent(
            self.team["id"], "rus", role="doer", hosting="external")
        self.app.heartbeat.beat(self.worker["id"], key=self.worker["agent_key"])
        out = self.app.heartbeat.beat(other["id"], key=other["agent_key"])
        self.assertEqual(out["why"], "nothing_ready")

    def test_a_frozen_seat_is_refused_before_it_sees_the_work(self):
        for _ in range(3):
            self.app.strikes.record(self.team["id"], self.worker["id"], "dispatch",
                                    note="a limit was hit")
        with self.assertRaises(NotAllowed) as caught:
            self.app.heartbeat.beat(self.worker["id"], key=self.worker["agent_key"])
        self.assertEqual(caught.exception.details.get("why"), "seat_not_live")
        # and the refusal is the point: the worker was not told what the work is
        self.assertEqual(self.app.kanban.get_task(self.card["id"])["lease"]["holder"], "")

    def test_the_state_says_what_a_worker_may_do(self):
        state = self.app.heartbeat.state(self.worker["id"])
        self.assertEqual(state["hosting"], "external")
        self.assertIn("doer", state["may_hold"])
        self.assertIn("reviewer", state["may_not"])
        self.assertIn("heartbeat", state["calls"])
        self.assertIsNone(state["holding"])


if __name__ == "__main__":
    unittest.main()
