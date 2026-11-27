"""what a card owes before it moves, and the questions it still has open.

the property under test is that the board asks, and that it asks about something measurable: a
story is reviewable only when its acceptance suite is sealed for the crew and nothing is open on
it, while an operational task owes only the second. the tests also pin the two refusals that make
the mechanism worth having: an unknown kind is not quietly treated as a task, and an agent cannot
waive its own gate.
"""

from __future__ import annotations

import json
import os
from typing import Any
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from base import AppCase  # noqa: E402
from core.db import new_id, now  # noqa: E402
from core.errors import Conflict, NotAllowed, NotFound, ValidationError  # noqa: E402
from work import phases as ph  # noqa: E402


class PureTests(unittest.TestCase):
    def test_a_code_card_owes_its_questions_before_review(self):
        self.assertEqual(ph.required_for("code", "review"), ("no_open_questions",))

    def test_a_code_card_owes_the_run_and_the_seal_before_it_closes(self):
        self.assertEqual(ph.required_for("code", "done"),
                         ("no_open_questions", "succeeded_run", "sealed_suite"))

    def test_a_research_card_owes_only_its_questions(self):
        self.assertEqual(ph.required_for("research", "done"), ("no_open_questions",))

    def test_a_column_nothing_is_declared_for_owes_nothing(self):
        self.assertEqual(ph.required_for("code", "in_progress"), ())

    def test_an_unknown_kind_is_refused_rather_than_downgraded(self):
        with self.assertRaises(ValidationError):
            ph.required_for("epic", "review")

    def test_a_card_with_no_kind_is_a_code_card(self):
        self.assertEqual(ph.kind_of({"payload": {}}), "code")

    def test_the_column_is_the_truth_and_the_payload_is_older(self):
        self.assertEqual(ph.kind_of({"kind": "design", "payload": {"kind": "code"}}), "design")

    def test_a_card_kind_is_read_case_insensitively(self):
        self.assertEqual(ph.kind_of({"kind": " Design "}), "design")

    def test_a_crew_table_replaces_only_the_kinds_it_names(self):
        table = ph._table({"code": {"done": ["succeeded_run"]}})
        self.assertEqual(table["code"]["done"], ("succeeded_run",))
        self.assertEqual(table["research"]["done"], ("no_open_questions",))

    def test_an_unknown_requirement_is_not_silently_met(self):
        self.assertFalse(ph.met("vibes", {}))
        self.assertIn("is not a requirement this board can measure", ph.unmet_reason("vibes", {}))

    def test_an_unmet_requirement_says_what_is_missing(self):
        reason = ph.unmet_reason("sealed_suite", {})
        self.assertIn("acceptance suite", reason)

    def test_an_open_question_counts_itself_in_the_reason(self):
        reason = ph.unmet_reason("no_open_questions", {"open_questions": 2})
        self.assertIn("2 questions", reason)
        self.assertIn("1 question", ph.unmet_reason("no_open_questions", {"open_questions": 1}))


class ServiceTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew()
        self.doer = self.app.crews.add_agent(self.team["id"], "wren", role="doer")

    def enable(self, policy: Any = True) -> None:
        self.app.crews.update_team(self.team["id"], orchestration={"phases": policy})

    def card(self, kind: str = "code", column: str = "ready") -> dict:
        task = self.app.kanban.create_task(self.team["id"], "reset the password",
                                           description="x", column=column, kind=kind)
        self.app.kanban.assign(task["id"], self.doer["id"])
        return self.app.kanban.get_task(task["id"])

    def seal(self, scenarios: int = 2) -> None:
        with self.app.store.tx() as c:
            c.execute(
                """insert into holdout_seals (team_id, digest, scenarios, actor, sealed_date)
                   values (?,?,?,'owner',?)""",
                (self.team["id"], "abc", scenarios, now()))

    def succeeded_run(self, task: dict) -> str:
        run_id = new_id("run")
        with self.app.store.tx() as c:
            c.execute(
                """insert into runs (id, task_id, agent_id, provider, status, steps,
                       created_date) values (?,?,?,?,?,?,?)""",
                (run_id, task["id"], self.doer["id"], "local", "succeeded",
                 json.dumps([]), now()))
        return run_id

    # ---- questions -----------------------------------------------------
    def test_a_question_is_a_record_with_an_asker(self):
        task = self.card("research")
        question = self.app.phases.ask(task["id"], "does the link expire?", "reviewer")
        self.assertEqual(question["status"], "open")
        self.assertEqual(question["asked_by"], "reviewer")
        self.assertEqual(question["task_id"], task["id"])

    def test_a_question_needs_text(self):
        task = self.card("research")
        with self.assertRaises(ValidationError):
            self.app.phases.ask(task["id"], "   ")

    def test_answering_closes_the_question_and_keeps_the_answer(self):
        task = self.card("research")
        question = self.app.phases.ask(task["id"], "does the link expire?", "reviewer")
        answered = self.app.phases.answer(question["id"], "after one hour", "owner")
        self.assertEqual(answered["status"], "answered")
        self.assertEqual(answered["answer"], "after one hour")
        self.assertEqual(answered["answered_by"], "owner")
        self.assertIsNotNone(answered["closed_date"])

    def test_answering_twice_is_a_conflict(self):
        task = self.card("research")
        question = self.app.phases.ask(task["id"], "expiry?", "reviewer")
        self.app.phases.answer(question["id"], "one hour", "owner")
        with self.assertRaises(Conflict):
            self.app.phases.answer(question["id"], "two hours", "owner")

    def test_a_dropped_question_keeps_its_reason(self):
        task = self.card("research")
        question = self.app.phases.ask(task["id"], "expiry?", "reviewer")
        dropped = self.app.phases.drop(question["id"], "the client answered elsewhere", "owner")
        self.assertEqual(dropped["status"], "dropped")
        self.assertIn("the client answered elsewhere", dropped["answer"])

    def test_dropping_needs_a_reason(self):
        task = self.card("research")
        question = self.app.phases.ask(task["id"], "expiry?", "reviewer")
        with self.assertRaises(ValidationError):
            self.app.phases.drop(question["id"], "", "owner")

    def test_a_question_that_does_not_exist_is_not_found(self):
        with self.assertRaises(NotFound):
            self.app.phases.answer("q_nope", "x", "owner")

    def test_an_open_question_blocks_review_of_any_kind(self):
        self.enable()
        task = self.card("research")
        self.app.phases.ask(task["id"], "which channel?", "reviewer")
        self.app.kanban.move(task["id"], "in_progress", actor=self.doer["id"])
        with self.assertRaises(Conflict):
            self.app.kanban.move(task["id"], "review", actor=self.doer["id"])

    def test_answering_it_unblocks_the_same_move(self):
        self.enable()
        task = self.card("research")
        question = self.app.phases.ask(task["id"], "which channel?", "reviewer")
        self.app.phases.answer(question["id"], "email", "owner")
        self.app.kanban.move(task["id"], "in_progress", actor=self.doer["id"])
        moved = self.app.kanban.move(task["id"], "review", actor=self.doer["id"])
        self.assertEqual(moved["column"], "review")

    def test_the_crew_sees_its_open_questions_with_the_card_title(self):
        task = self.card("research")
        self.app.phases.ask(task["id"], "which channel?", "reviewer")
        report = self.app.phases.crew_questions(self.team["id"])
        self.assertEqual(report["count"], 1)
        self.assertEqual(report["questions"][0]["card_title"], "reset the password")

    # ---- enforcement is the crew's decision ----------------------------
    def test_a_crew_that_never_asked_for_checks_is_not_gated(self):
        task = self.card("code")
        self.app.phases.ask(task["id"], "which channel?", "reviewer")
        self.app.kanban.move(task["id"], "in_progress", actor=self.doer["id"])
        moved = self.app.kanban.move(task["id"], "review", actor=self.doer["id"])
        self.assertEqual(moved["column"], "review")

    def test_the_reading_still_answers_for_a_crew_that_did_not_opt_in(self):
        task = self.card("code")
        self.app.phases.ask(task["id"], "which channel?", "reviewer")
        reading = self.app.phases.readiness(task["id"], "review")
        self.assertFalse(reading["enforced"])
        self.assertFalse(reading["ready"])
        self.assertEqual(reading["unmet"][0]["requirement"], "no_open_questions")

    def test_a_crew_may_state_its_own_table(self):
        self.enable({"research": {"done": ["succeeded_run"]}})
        task = self.card("research")
        reading = self.app.phases.readiness(task["id"], "done")
        self.assertEqual(reading["required"], ["succeeded_run"])
        self.assertFalse(reading["ready"])

    # ---- kinds ---------------------------------------------------------
    def test_a_code_card_cannot_close_without_a_sealed_suite(self):
        self.enable()
        task = self.card("code")
        reading = self.app.phases.readiness(task["id"], "done")
        self.assertEqual([u["requirement"] for u in reading["unmet"]],
                         ["succeeded_run", "sealed_suite"])

    def test_a_seal_and_a_run_let_it_close(self):
        self.enable()
        self.seal()
        task = self.card("code")
        self.succeeded_run(task)
        self.assertTrue(self.app.phases.readiness(task["id"], "done")["ready"])

    def test_a_seal_with_no_scenarios_is_not_a_suite(self):
        self.enable()
        self.seal(scenarios=0)
        task = self.card("code")
        self.succeeded_run(task)
        self.assertFalse(self.app.phases.readiness(task["id"], "done")["ready"])

    def test_a_failed_run_does_not_count_as_work(self):
        self.enable()
        self.seal()
        task = self.card("code")
        run_id = self.succeeded_run(task)
        with self.app.store.tx() as c:
            c.execute("update runs set status = 'failed' where id = ?", (run_id,))
        self.assertFalse(self.app.phases.readiness(task["id"], "done")["ready"])

    def test_a_research_card_closes_without_a_run(self):
        self.enable()
        task = self.card("research")
        self.assertTrue(self.app.phases.readiness(task["id"], "done")["ready"])

    def test_the_board_refuses_the_move_the_reading_refused(self):
        self.enable()
        self.seal()
        task = self.card("code")
        self.app.kanban.move(task["id"], "in_progress", actor=self.doer["id"])
        self.app.kanban.move(task["id"], "review", actor=self.doer["id"])
        with self.assertRaises(Conflict) as caught:
            self.app.kanban.move(task["id"], "done", actor="owner", evidence="commit:abc1234")
        self.assertEqual(caught.exception.details["column"], "done")

    def test_the_crew_report_names_the_cards_that_owe_something(self):
        self.enable()
        blocked = self.card("code")
        self.app.phases.ask(blocked["id"], "which channel?", "reviewer")
        self.card("research")
        report = self.app.phases.for_crew(self.team["id"])
        self.assertEqual(report["cards"], 2)
        self.assertTrue(report["enforced"])
        self.assertEqual([c["task_id"] for c in report["blocked"]], [blocked["id"]])
        self.assertEqual(report["open_questions"], 1)

    # ---- waivers -------------------------------------------------------
    def test_a_person_may_waive_a_phase_and_the_card_moves(self):
        self.enable()
        task = self.card("code")
        self.app.phases.ask(task["id"], "which channel?", "reviewer")
        self.app.phases.waive(task["id"], "review", "the client answered on the call", "owner")
        self.app.kanban.move(task["id"], "in_progress", actor=self.doer["id"])
        self.assertEqual(self.app.kanban.move(task["id"], "review",
                                              actor=self.doer["id"])["column"], "review")

    def test_an_agent_may_not_waive_its_own_gate(self):
        self.enable()
        task = self.card("code")
        with self.assertRaises(NotAllowed):
            self.app.phases.waive(task["id"], "review", "trust me", self.doer["id"], "agent")

    def test_a_waiver_needs_a_reason(self):
        task = self.card("code")
        with self.assertRaises(ValidationError):
            self.app.phases.waive(task["id"], "review", "  ", "owner")

    def test_a_waiver_is_per_column(self):
        task = self.card("code")
        self.app.phases.waive(task["id"], "review", "resealing", "owner")
        self.assertEqual([w["column"] for w in self.app.phases.waivers(task["id"])], ["review"])

    def test_the_reading_says_who_waived_and_why(self):
        self.enable()
        task = self.card("code")
        self.app.phases.ask(task["id"], "which channel?", "reviewer")
        self.app.phases.waive(task["id"], "review", "resealing", "anne")
        reading = self.app.phases.readiness(task["id"], "review")
        self.assertTrue(reading["ready"])
        self.assertEqual(reading["waived"]["waived_by"], "anne")
        self.assertEqual(reading["unmet"][0]["requirement"], "no_open_questions")

    # ---- routes --------------------------------------------------------
    def test_the_routes_answer(self):
        from fastapi.testclient import TestClient
        from api.routes import create_api
        self.enable()
        task = self.card("code")
        client = TestClient(create_api(self.app))
        asked = client.post(f"/cards/{task['id']}/questions",
                            json={"text": "which channel?", "asked_by": "reviewer"}).json()
        self.assertEqual(asked["status"], "open")
        listing = client.get(f"/cards/{task['id']}/questions").json()
        self.assertEqual(listing["count"], 1)
        answered = client.post(f"/questions/{asked['id']}/answer",
                               json={"text": "email", "by": "owner"}).json()
        self.assertEqual(answered["status"], "answered")
        reading = client.get(f"/cards/{task['id']}/readiness", params={"to": "review"}).json()
        self.assertEqual(reading["kind"], "code")
        self.assertTrue(reading["enforced"])
        crew = client.get(f"/crews/{self.team['id']}/questions").json()
        self.assertEqual(crew["count"], 0)
        waived = client.post(f"/cards/{task['id']}/phase-waivers",
                             json={"column": "done", "reason": "shipping tonight"}).json()
        self.assertEqual(waived["column"], "done")
