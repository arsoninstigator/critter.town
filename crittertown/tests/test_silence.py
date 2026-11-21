"""the watchdog: a card that is held, alive, and writing nothing.

the properties worth holding: the clock starts at the last output rather than at the
lease, so a renewing run that has stopped producing is visible; five minutes is a look
and fifteen is a rescue, because an alarm that acts on a hunch teaches people to mute
it; a person can answer with a snooze, a continue or a dismissal and the answer is a
row; and a recovery card that goes silent does not file another one, because a cascade
is how a watchdog becomes the outage.
"""

from __future__ import annotations

import json
import time
import unittest

from base import AppCase
from fastapi.testclient import TestClient

from api.routes import create_api

from core.errors import NotAllowed, ValidationError
from work import silence as silence_mod
from work.silence import (CRITICAL_SECONDS, REARM_SECONDS, SUSPICION_SECONDS,
                          classify, decision_window, may_decide, recovery_origin,
                          should_fold, silence_age, silence_started_at, suppression)


def card(**over) -> dict:
    base = {"id": "task_a", "team_id": "t", "title": "do the thing",
            "column": "in_progress", "lease_holder": "agent_a",
            "lease_started_at": 1000.0, "last_output_at": None,
            "output_seq": 0, "created_date": 900.0, "payload": {}}
    base.update(over)
    return base


class ClockTests(unittest.TestCase):
    def test_the_clock_prefers_output_over_the_lease_over_the_card(self):
        self.assertEqual(silence_started_at(card(last_output_at=1200.0)), 1200.0)
        self.assertEqual(silence_started_at(card()), 1000.0)
        self.assertEqual(silence_started_at(card(lease_started_at=None)), 900.0)
        self.assertIsNone(silence_started_at(
            card(lease_started_at=None, created_date=None)))

    def test_age_is_measured_from_the_last_output(self):
        self.assertEqual(silence_age(card(last_output_at=1200.0), 1500.0), 300.0)
        self.assertEqual(silence_age(card(), 1000.0), 0.0)
        # a clock that went backwards is not a negative silence
        self.assertEqual(silence_age(card(last_output_at=2000.0), 1500.0), 0.0)


class LevelTests(unittest.TestCase):
    def test_a_card_nobody_holds_is_not_watched(self):
        self.assertEqual(classify(card(column="ready", lease_holder=None), 5000.0),
                         "not_applicable")
        self.assertEqual(classify(card(column="backlog", lease_holder=None), 5000.0),
                         "not_applicable")

    def test_five_minutes_is_a_look_and_fifteen_is_a_rescue(self):
        held = card(lease_started_at=0.0)
        self.assertEqual(classify(held, SUSPICION_SECONDS - 1), "ok")
        self.assertEqual(classify(held, SUSPICION_SECONDS), "suspicious")
        self.assertEqual(classify(held, CRITICAL_SECONDS - 1), "suspicious")
        self.assertEqual(classify(held, CRITICAL_SECONDS), "critical")

    def test_output_resets_the_level(self):
        talking = card(lease_started_at=0.0, last_output_at=CRITICAL_SECONDS + 100)
        self.assertEqual(classify(talking, CRITICAL_SECONDS + 200), "ok")

    def test_a_snooze_quiets_it_and_a_dismissal_retires_it(self):
        held = card(lease_started_at=0.0)
        snoozed = {"decision": "snooze", "snoozed_until": 2000.0}
        self.assertEqual(classify(held, 1900.0, snoozed), "snoozed")
        # once the window ends the clock resumes where it was, not where the snooze was
        self.assertEqual(classify(held, 2100.0, snoozed), "critical")
        dismissed = {"decision": "dismissed_false_positive", "snoozed_until": None}
        self.assertEqual(classify(held, 5000.0, dismissed), "not_applicable")

    def test_thresholds_are_declarable_per_crew(self):
        held = card(lease_started_at=0.0)
        self.assertEqual(classify(held, 60.0, None, suspicion=30.0, critical=120.0),
                         "suspicious")
        self.assertEqual(classify(held, 60.0, None, suspicion=300.0, critical=900.0), "ok")


class SuppressionTests(unittest.TestCase):
    def test_the_reasons_are_resolved_in_a_fixed_order(self):
        held = card(lease_started_at=0.0)
        snoozed = {"decision": "snooze", "snoozed_until": 5000.0}
        self.assertEqual(suppression(held, snoozed, 1000.0), "snoozed")
        dismissed = {"decision": "dismissed_false_positive", "snoozed_until": 5000.0}
        self.assertEqual(suppression(held, dismissed, 1000.0), "dismissed_false_positive")
        recovery = card(payload={"kind": "recovery", "recovery_of": "task_z"})
        self.assertEqual(suppression(recovery, None, 1000.0), "recovery_origin")
        self.assertEqual(suppression(card(column="blocked"), None, 1000.0),
                         "blocked_source")
        self.assertIsNone(suppression(held, None, 1000.0))

    def test_a_recovery_card_is_recognised_from_a_stored_payload_too(self):
        self.assertTrue(recovery_origin(card(payload=json.dumps({"recovery_of": "x"}))))
        self.assertFalse(recovery_origin(card(payload=json.dumps({"slice": "code"}))))
        self.assertFalse(recovery_origin(card(payload="")))

    def test_an_expired_snooze_suppresses_nothing(self):
        held = card(lease_started_at=0.0)
        old = {"decision": "snooze", "snoozed_until": 500.0}
        self.assertIsNone(suppression(held, old, 1000.0))


class DecisionTests(unittest.TestCase):
    def test_a_snooze_must_say_until_when_and_the_time_must_be_ahead(self):
        with self.assertRaises(ValidationError):
            decision_window("snooze", 1000.0)
        with self.assertRaises(ValidationError):
            decision_window("snooze", 1000.0, snoozed_until=500.0)
        self.assertEqual(decision_window("snooze", 1000.0, snoozed_until=2000.0), 2000.0)

    def test_continue_buys_the_rearm_window_and_a_later_time_wins(self):
        self.assertEqual(decision_window("continue", 1000.0), 1000.0 + REARM_SECONDS)
        self.assertEqual(decision_window("continue", 1000.0, snoozed_until=9000.0), 9000.0)
        # a continue asking for a time already past gets the window, not the past
        self.assertEqual(decision_window("continue", 1000.0, snoozed_until=500.0),
                         1000.0 + REARM_SECONDS)

    def test_a_dismissal_has_no_window_because_it_is_not_a_wait(self):
        self.assertIsNone(decision_window("dismissed_false_positive", 1000.0))

    def test_an_unknown_decision_is_refused(self):
        with self.assertRaises(ValidationError):
            decision_window("ignore_forever", 1000.0)

    def test_only_the_board_or_the_assigned_recovery_owner_may_answer(self):
        held = card()
        self.assertEqual(may_decide("owner", held)[0], True)
        self.assertEqual(may_decide("agent_a", held)[0], False)
        recovery = {"id": "task_r", "assignee_agent_id": "agent_b",
                    "watchdog_of": "task_a"}
        self.assertEqual(may_decide("agent_b", held, recovery), (True, ""))
        self.assertEqual(may_decide("agent_c", held, recovery)[1], "not_authorized")
        # the recovery card for a different card is not a licence here
        other = {"id": "task_r", "assignee_agent_id": "agent_b", "watchdog_of": "task_z"}
        self.assertEqual(may_decide("agent_b", held, other)[1], "not_authorized")


class FoldTests(unittest.TestCase):
    def test_a_card_that_finished_after_the_silence_is_late_not_stuck(self):
        self.assertTrue(should_fold(card(column="done"), evidence_after=2000.0))
        self.assertFalse(should_fold(card(column="done"), evidence_after=500.0))
        self.assertFalse(should_fold(card(), evidence_after=2000.0))
        self.assertFalse(should_fold(card(column="done"), evidence_after=None))


class ServiceTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew()
        self.agent = self.agent(self.team["id"])
        self.silence = self.app.silence

    # helpers -----------------------------------------------------------
    def held(self, title: str = "do the thing") -> dict:
        task = self.task(self.team["id"], title)
        self.app.custody.claim(task["id"], self.agent["id"])
        self.app.kanban.move(task["id"], "in_progress", self.agent["id"])
        return task

    def backdate(self, task_id: str, seconds: float) -> None:
        """pretend the clock moved, because a test that sleeps is a test that flakes."""
        at = time.time() - seconds
        with self.app.store.tx() as c:
            c.execute("""update tasks set lease_started_at = ?, last_output_at = NULL
                         where id = ?""", (at, task_id))

    # output ------------------------------------------------------------
    def test_only_the_holder_may_report_output(self):
        task = self.held()
        with self.assertRaises(NotAllowed):
            self.silence.record_output(task["id"], "agent_someone_else")
        out = self.silence.record_output(task["id"], self.agent["id"], size=42)
        self.assertEqual(out["output_seq"], 1)
        self.assertGreater(out["last_output_at"], 0)

    def test_output_moves_the_clock_and_the_sequence_counts_it(self):
        task = self.held()
        self.backdate(task["id"], 20 * 60)
        self.assertEqual(self.silence.watch(task["id"])["level"], "critical")
        self.silence.record_output(task["id"], self.agent["id"])
        seen = self.silence.watch(task["id"])
        self.assertEqual(seen["level"], "ok")
        self.assertEqual(seen["output_seq"], 1)

    def test_a_card_with_no_holder_refuses_output(self):
        task = self.task(self.team["id"], "unclaimed")
        with self.assertRaises(NotAllowed):
            self.silence.record_output(task["id"], self.agent["id"])

    def test_a_claim_starts_a_fresh_silence_clock(self):
        task = self.held()
        self.silence.record_output(task["id"], self.agent["id"])
        self.app.custody.complete(task["id"], self.agent["id"], "run:x")
        row = self.app.store.one("select * from tasks where id = ?", (task["id"],))
        self.assertIsNone(row["lease_started_at"])
        self.app.kanban.move(task["id"], "ready", "owner")
        self.app.custody.claim(task["id"], self.agent["id"])
        row = self.app.store.one("select * from tasks where id = ?", (task["id"],))
        # the previous holder's last output says nothing about this one
        self.assertIsNone(row["last_output_at"])
        self.assertGreater(row["lease_started_at"], 0)

    # watching ----------------------------------------------------------
    def test_the_state_counts_every_level(self):
        self.held("quiet one")
        self.task(self.team["id"], "sitting in ready")
        state = self.silence.state(self.team["id"])
        self.assertEqual(state["counts"].get("ok"), 1)
        self.assertEqual(state["counts"].get("not_applicable"), 1)

    # escalation --------------------------------------------------------
    def test_a_fresh_card_files_nothing(self):
        task = self.held()
        out = self.silence.escalate(self.team["id"])
        self.assertEqual(out["created"], [])
        self.assertEqual(self.silence.watch(task["id"])["level"], "ok")

    def test_five_minutes_is_reported_and_not_rescued(self):
        task = self.held()
        self.backdate(task["id"], SUSPICION_SECONDS + 1)
        out = self.silence.escalate(self.team["id"])
        self.assertEqual(out["created"], [])
        self.assertEqual(out["skipped"], [{"task_id": task["id"],
                                           "why": "suspicious_only"}])

    def test_fifteen_minutes_files_one_recovery_card(self):
        task = self.held()
        self.backdate(task["id"], CRITICAL_SECONDS + 1)
        out = self.silence.escalate(self.team["id"])
        self.assertEqual(len(out["created"]), 1)
        recovery_id = out["created"][0]["recovery_task_id"]
        recovery = self.app.kanban.get_task(recovery_id)
        self.assertEqual(recovery["watchdog_of"], task["id"])
        self.assertEqual(recovery["payload"]["recovery_of"], task["id"])
        self.assertEqual(recovery["column"], "ready")
        # the filed card says what happened, in minutes, because a person reads it
        self.assertIn("has written nothing", recovery["description"])

    def test_a_second_sweep_finds_the_recovery_card_already_open(self):
        task = self.held()
        self.backdate(task["id"], CRITICAL_SECONDS + 1)
        self.silence.escalate(self.team["id"])
        again = self.silence.escalate(self.team["id"])
        self.assertEqual(again["created"], [])
        self.assertEqual(again["existing"], [task["id"]])
        open_recoveries = self.app.store.query(
            "select id from tasks where watchdog_of = ?", (task["id"],))
        self.assertEqual(len(open_recoveries), 1)

    def test_a_recovery_card_that_goes_silent_does_not_cascade(self):
        task = self.held("the original")
        self.backdate(task["id"], CRITICAL_SECONDS + 1)
        recovery_id = self.silence.escalate(self.team["id"])["created"][0]["recovery_task_id"]
        self.app.custody.claim(recovery_id, self.agent["id"])
        self.app.kanban.move(recovery_id, "in_progress", self.agent["id"])
        self.backdate(recovery_id, CRITICAL_SECONDS + 60)
        out = self.silence.escalate(self.team["id"])
        self.assertEqual(out["created"], [])
        self.assertIn({"task_id": recovery_id, "why": "recovery_origin"}, out["skipped"])

    def test_a_snooze_stops_the_escalation_until_it_ends(self):
        task = self.held()
        self.backdate(task["id"], CRITICAL_SECONDS + 1)
        self.silence.decide(task["id"], "snooze", snoozed_until=time.time() + 600)
        out = self.silence.escalate(self.team["id"])
        self.assertEqual(out["created"], [])
        self.assertEqual(out["snoozed"], [task["id"]])
        self.assertEqual(self.silence.watch(task["id"])["level"], "snoozed")

    def test_a_dismissal_is_recorded_and_retires_the_signal(self):
        task = self.held()
        self.backdate(task["id"], CRITICAL_SECONDS + 1)
        self.silence.decide(task["id"], "dismissed_false_positive", reason="slow model")
        self.assertEqual(self.silence.watch(task["id"])["level"], "not_applicable")
        self.assertEqual(self.silence.escalate(self.team["id"])["created"], [])

    def test_a_stranger_cannot_answer_for_a_silent_card(self):
        task = self.held()
        self.backdate(task["id"], CRITICAL_SECONDS + 1)
        with self.assertRaises(NotAllowed):
            self.silence.decide(task["id"], "continue", actor=self.agent["id"])

    def test_the_assigned_recovery_owner_may_answer(self):
        task = self.held()
        self.backdate(task["id"], CRITICAL_SECONDS + 1)
        recovery_id = self.silence.escalate(self.team["id"])["created"][0]["recovery_task_id"]
        self.app.kanban.assign(recovery_id, self.agent["id"])
        out = self.silence.decide(task["id"], "continue", actor=self.agent["id"])
        self.assertGreater(out["snoozed_until"], time.time())

    # the loop ----------------------------------------------------------
    def test_the_sweep_reclaims_a_dead_lease_and_watches_output_in_one_call(self):
        client = TestClient(create_api(self.app))
        dead = self.held("dead holder")
        quiet = self.held("quiet holder")
        with self.app.store.tx() as c:
            c.execute("update tasks set lease_expires = ? where id = ?",
                      (time.time() - 60, dead["id"]))
        self.backdate(quiet["id"], CRITICAL_SECONDS + 1)
        out = client.post(f"/crews/{self.team['id']}/sweep").json()
        self.assertTrue(any(a["kind"] == "reclaim_expired" for a in out["actions"]))
        self.assertEqual(len(out["silence"]["created"]), 1)
        self.assertEqual(out["silence"]["created"][0]["task_id"], quiet["id"])

    # the routes --------------------------------------------------------
    def test_output_and_the_silence_view_are_reachable(self):
        client = TestClient(create_api(self.app))
        task = self.held()
        self.backdate(task["id"], CRITICAL_SECONDS + 1)
        seen = client.get(f"/tasks/{task['id']}/silence").json()
        self.assertEqual(seen["level"], "critical")
        posted = client.post(f"/tasks/{task['id']}/output",
                             json={"agent_id": self.agent["id"], "size": 10})
        self.assertEqual(posted.status_code, 200)
        self.assertEqual(client.get(f"/tasks/{task['id']}/silence").json()["level"], "ok")
        crew = client.get(f"/crews/{self.team['id']}/silence").json()
        self.assertIn("counts", crew)

    def test_a_snooze_can_be_recorded_and_an_unknown_decision_is_refused(self):
        client = TestClient(create_api(self.app))
        task = self.held()
        self.backdate(task["id"], CRITICAL_SECONDS + 1)
        out = client.post(f"/tasks/{task['id']}/silence/decide",
                          json={"decision": "snooze", "reason": "known slow model",
                                "snoozed_until": time.time() + 900})
        self.assertEqual(out.status_code, 200)
        self.assertEqual(out.json()["decision"], "snooze")
        bad = client.post(f"/tasks/{task['id']}/silence/decide",
                          json={"decision": "ignore"})
        # the platform answers a refusal with 422, the same as every other validation
        self.assertEqual(bad.status_code, 422)
