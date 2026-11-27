"""an escalation is a decision put to a person, and it carries the answer it will take without one.

the properties under test are the ones that make it honest: four lines and a deadline are required,
a default that only waits again is refused as a hostage note, a person is never woken for what the org
handles, an agent can never answer, a rung with nobody behind it is skipped and named, and a default
taken after the deadline is recorded as the org acting and never as a person agreeing.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from base import AppCase  # noqa: E402
from core.db import now  # noqa: E402
from core.errors import Conflict, NotFound, ValidationError  # noqa: E402
from org import escalations as es  # noqa: E402


class PureTests(unittest.TestCase):
    def test_an_empty_default_reads_as_one(self):
        for text in ("wait", "ask again", "TBD", "none", "  ", ""):
            self.assertTrue(es.reads_as_empty_default(text), text)

    def test_a_real_default_does_not(self):
        self.assertFalse(es.reads_as_empty_default("refund the order and note it on the card"))

    def test_a_broken_build_is_not_a_wake(self):
        self.assertIn("broken_build", es.not_a_wake("the build is broken again"))

    def test_a_metric_that_has_not_moved_is_not_a_wake(self):
        self.assertIn("metric_not_moved", es.not_a_wake("the metric has not moved yet"))

    def test_a_plain_decision_is_a_wake(self):
        self.assertEqual(es.not_a_wake("we need to refund a customer 4000 dollars"), [])

    def test_the_month_key_is_calendar_month(self):
        self.assertEqual(es.month_key(0), "1970-01")

    def test_the_ladder_starts_at_zero_and_ends_at_a_person(self):
        self.assertEqual(es.LADDER[0]["after_minutes"], 0)
        self.assertEqual(es.LADDER[-1]["rung"], "person")

    def test_the_ladder_is_in_time_order(self):
        times = [step["after_minutes"] for step in es.LADDER]
        self.assertEqual(times, sorted(times))


class EscCase(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew()
        self.tid = self.team["id"]
        crews = self.app.crews
        self.anne = crews.add_agent(self.tid, "anne", role="doer")
        self.ben = crews.add_agent(self.tid, "ben", role="doer")
        self.cy = crews.add_agent(self.tid, "cy", role="reviewer")
        self.dee = crews.add_agent(self.tid, "dee", role="doer")
        self.root = self.app.departments.create(
            self.tid, "ops", coordinator_agent_id=self.cy["id"])
        self.child = self.app.departments.create(
            self.tid, "support", parent_id=self.root["id"],
            coordinator_agent_id=self.ben["id"], members=[self.anne["id"]])

    def raise_one(self, **kw) -> dict:
        args = dict(
            category="money",
            what_happened="a customer was charged twice for the same order",
            already_tried="checked the ledger and confirmed the duplicate",
            decision_needed="refund the second charge of 40 dollars",
            default_action="refund it and note the card",
            default_after_minutes=120)
        args.update(kw)
        return self.app.escalations.raise_(self.anne["id"], **args)


class RaiseTests(EscCase):
    def test_a_complete_escalation_is_open_with_its_four_lines(self):
        e = self.raise_one()
        self.assertEqual(e["state"], "open")
        self.assertEqual(e["default_action"], "refund it and note the card")

    def test_a_category_outside_the_wake_list_is_refused(self):
        with self.assertRaises(ValidationError):
            self.raise_one(category="annoyed")

    def test_each_line_is_required(self):
        for line in ("what_happened", "already_tried", "decision_needed", "default_action"):
            with self.assertRaises(ValidationError, msg=line):
                self.raise_one(**{line: "  "})

    def test_a_default_that_only_waits_is_a_hostage_note(self):
        with self.assertRaises(ValidationError) as caught:
            self.raise_one(default_action="wait")
        self.assertIn("hostage", str(caught.exception))

    def test_a_deadline_is_required(self):
        with self.assertRaises(ValidationError):
            self.raise_one(default_after_minutes=0)

    def test_a_broken_build_is_refused_as_not_worth_a_wake(self):
        with self.assertRaises(ValidationError) as caught:
            self.raise_one(what_happened="the build is broken and tests fail")
        self.assertIn("broken_build", caught.exception.details["reasons"])

    def test_a_card_outside_the_crew_is_refused(self):
        other = self.crew("elsewhere")
        foreign = self.app.kanban.create_task(other["id"], "x", description="y", column="ready")
        with self.assertRaises(NotFound):
            self.raise_one(task_id=foreign["id"])

    def test_the_four_lines_read_in_the_owners_order(self):
        text = self.app.escalations.four_lines(self.raise_one())
        order = [text.index(s) for s in ("what happened", "already tried",
                                         "decision needed", "if nobody answers")]
        self.assertEqual(order, sorted(order))


class ClimbTests(EscCase):
    def test_the_first_rung_fires_at_once(self):
        e = self.raise_one()
        due = self.app.escalations.due_rungs(e)
        self.assertEqual([s["rung"] for s in due], ["next_holder"])

    def test_later_rungs_come_due_with_time(self):
        e = self.raise_one()
        later = e["raised_date"] + 61 * 60
        rungs = [s["rung"] for s in self.app.escalations.due_rungs(e, at=later)]
        self.assertEqual(rungs, ["next_holder", "coordinator", "parent_coordinator", "person"])

    def test_a_rung_with_nobody_behind_it_is_skipped_and_named(self):
        e = self.raise_one()
        result = self.app.escalations.climb(e["id"], at=e["raised_date"] + 1)
        self.assertEqual([s["rung"] for s in result["skipped"]], ["next_holder"])
        self.assertTrue(result["skipped"][0]["skipped"])

    def test_the_coordinator_rung_reaches_the_department_head(self):
        e = self.raise_one()
        result = self.app.escalations.climb(e["id"], at=e["raised_date"] + 11 * 60)
        coordinator = [r for r in result["reached"] if r["rung"] == "coordinator"]
        self.assertEqual(coordinator[0]["target"], self.ben["id"])

    def test_the_parent_rung_reaches_the_parent_head(self):
        e = self.raise_one()
        result = self.app.escalations.climb(e["id"], at=e["raised_date"] + 31 * 60)
        parent = [r for r in result["reached"] if r["rung"] == "parent_coordinator"]
        self.assertEqual(parent[0]["target"], self.cy["id"])

    def test_the_person_rung_is_the_last_and_names_a_person(self):
        e = self.raise_one()
        result = self.app.escalations.climb(e["id"], at=e["raised_date"] + 61 * 60)
        self.assertEqual(result["reached"][-1]["target"], "person")

    def test_climbing_twice_reaches_nobody_twice(self):
        e = self.raise_one()
        moment = e["raised_date"] + 31 * 60
        self.app.escalations.climb(e["id"], at=moment)
        again = self.app.escalations.climb(e["id"], at=moment)
        self.assertEqual(again["reached"], [])
        self.assertEqual(again["skipped"], [])

    def test_a_coordinator_who_raised_it_is_not_asked_to_answer_itself(self):
        e = self.app.escalations.raise_(
            self.ben["id"], "money", "a charge was duplicated", "checked the ledger",
            "refund 40 dollars", "refund it and note the card", 120)
        result = self.app.escalations.climb(e["id"], at=e["raised_date"] + 11 * 60)
        self.assertIn("coordinator", [s["rung"] for s in result["skipped"]])

    def test_the_message_up_the_ladder_is_the_four_lines(self):
        e = self.raise_one()
        self.app.escalations.climb(e["id"], at=e["raised_date"] + 11 * 60)
        row = self.app.store.one(
            "select body, kind from a2a_messages where to_agent_id = ? order by created_date desc",
            (self.ben["id"],))
        self.assertEqual(row["kind"], "blocker")
        self.assertIn("the decision needed", row["body"])

    def test_a_crew_ladder_overrides_the_default(self):
        self.app.crews.update_team(self.tid, orchestration={"escalation": {"ladder": [
            {"rung": "coordinator", "after_minutes": 5}]}})
        steps = self.app.escalations.ladder(self.tid)
        self.assertEqual(steps[0]["rung"], "next_holder")
        self.assertEqual(steps[1]["after_minutes"], 5)

    def test_an_unknown_rung_is_refused(self):
        self.app.crews.update_team(self.tid, orchestration={"escalation": {"ladder": [
            {"rung": "the_pope", "after_minutes": 5}]}})
        with self.assertRaises(ValidationError):
            self.app.escalations.ladder(self.tid)


class SettleTests(EscCase):
    def test_a_person_answers(self):
        e = self.raise_one()
        done = self.app.escalations.answer(e["id"], "anne.owner@example.com", "refund it")
        self.assertEqual(done["state"], "answered")
        self.assertEqual(done["outcome"], "person_decided")

    def test_an_agent_can_never_answer(self):
        e = self.raise_one()
        with self.assertRaises(ValidationError) as caught:
            self.app.escalations.answer(e["id"], self.cy["id"], "refund it")
        self.assertIn("person", str(caught.exception))

    def test_an_answer_says_what_was_decided(self):
        e = self.raise_one()
        with self.assertRaises(ValidationError):
            self.app.escalations.answer(e["id"], "owner", "  ")

    def test_an_answered_escalation_stays_answered(self):
        e = self.raise_one()
        self.app.escalations.answer(e["id"], "owner", "refund it")
        with self.assertRaises(Conflict):
            self.app.escalations.answer(e["id"], "owner", "do not")

    def test_only_the_raiser_withdraws(self):
        e = self.raise_one()
        with self.assertRaises(ValidationError):
            self.app.escalations.withdraw(e["id"], self.ben["id"])
        self.assertEqual(self.app.escalations.withdraw(e["id"], self.anne["id"])["state"],
                         "withdrawn")

    def test_the_human_answer_is_audited_with_the_person_as_actor(self):
        e = self.raise_one()
        self.app.escalations.answer(e["id"], "owner@example.com", "refund it")
        row = self.app.store.one(
            "select actor_kind, actor_id, agent_id from agent_audit where action = ? "
            "and target_id = ?", ("escalation.answered", e["id"]))
        self.assertEqual(row["actor_kind"], "human")
        self.assertEqual(row["actor_id"], "owner@example.com")
        self.assertEqual(row["agent_id"], self.anne["id"])


class DefaultTests(EscCase):
    def test_nothing_defaults_before_its_deadline(self):
        e = self.raise_one(default_after_minutes=120)
        self.assertEqual(self.app.escalations.take_defaults(self.tid, at=e["raised_date"] + 60), [])

    def test_the_default_is_taken_after_the_deadline(self):
        e = self.raise_one(default_after_minutes=120)
        taken = self.app.escalations.take_defaults(self.tid, at=e["raised_date"] + 121 * 60)
        self.assertEqual(taken[0]["state"], "defaulted")
        self.assertEqual(taken[0]["answer"], "refund it and note the card")

    def test_a_default_is_the_org_acting_and_never_a_person_agreeing(self):
        e = self.raise_one(default_after_minutes=1)
        taken = self.app.escalations.take_defaults(self.tid, at=e["raised_date"] + 120)
        self.assertEqual(taken[0]["outcome"], "org_took_default")
        self.assertIsNone(taken[0]["answered_by"])

    def test_the_default_is_audited_as_the_system_not_a_person(self):
        e = self.raise_one(default_after_minutes=1)
        self.app.escalations.take_defaults(self.tid, at=e["raised_date"] + 120)
        row = self.app.store.one(
            "select actor_kind from agent_audit where action = ? and target_id = ?",
            ("escalation.defaulted", e["id"]))
        self.assertEqual(row["actor_kind"], "system")

    def test_an_answered_escalation_is_not_defaulted_over(self):
        e = self.raise_one(default_after_minutes=1)
        self.app.escalations.answer(e["id"], "owner", "do not refund")
        self.assertEqual(self.app.escalations.take_defaults(
            self.tid, at=e["raised_date"] + 120), [])
        self.assertEqual(self.app.escalations._row(e["id"])["outcome"], "person_decided")

    def test_a_withdrawn_escalation_is_not_defaulted(self):
        e = self.raise_one(default_after_minutes=1)
        self.app.escalations.withdraw(e["id"], self.anne["id"])
        self.assertEqual(self.app.escalations.take_defaults(
            self.tid, at=e["raised_date"] + 120), [])

    def test_the_tick_climbs_then_defaults(self):
        e = self.raise_one(default_after_minutes=20)
        out = self.app.escalations.tick(self.tid, at=e["raised_date"] + 21 * 60)
        self.assertTrue(out["climbed"])
        self.assertEqual(len(out["defaulted"]), 1)

    def test_the_app_beat_takes_a_defaulted_escalation(self):
        e = self.raise_one(default_after_minutes=1)
        self.app.store.query  # the beat runs against the real clock, so age the row
        with self.app.store.tx() as c:
            c.execute("update escalations set default_after = ? where id = ?",
                      (now() - 5, e["id"]))
        beat = self.app.tick()
        self.assertEqual(beat["escalations_defaulted"], 1)


class BudgetTests(EscCase):
    def test_a_crew_starts_with_no_budget_because_none_was_chosen(self):
        self.assertIsNone(self.app.escalations.budget(self.tid))

    def test_a_crew_that_never_stated_a_budget_is_never_called_over(self):
        for _ in range(8):
            self.raise_one()
        spend = self.app.escalations.spend(self.tid)
        self.assertEqual(spend["used"], 8)
        self.assertIsNone(spend["budget"])
        self.assertFalse(spend["over"])
        self.assertEqual(spend["finding"], "")

    def test_a_crew_can_set_its_own_budget(self):
        self.app.crews.update_team(self.tid, orchestration={"escalation": {"monthly_budget": 2}})
        self.assertEqual(self.app.escalations.budget(self.tid), 2)

    def test_spend_counts_this_months_escalations(self):
        self.raise_one()
        self.raise_one()
        self.assertEqual(self.app.escalations.spend(self.tid)["used"], 2)

    def test_over_budget_is_a_finding_about_scoping(self):
        self.app.crews.update_team(self.tid, orchestration={"escalation": {"monthly_budget": 1}})
        self.raise_one()
        self.raise_one()
        spend = self.app.escalations.spend(self.tid)
        self.assertTrue(spend["over"])
        self.assertIn("mis-scoped", spend["finding"])

    def test_at_budget_is_not_over(self):
        self.app.crews.update_team(self.tid, orchestration={"escalation": {"monthly_budget": 2}})
        self.raise_one()
        self.raise_one()
        self.assertFalse(self.app.escalations.spend(self.tid)["over"])

    def test_over_budget_never_refuses_a_raise(self):
        self.app.crews.update_team(self.tid, orchestration={"escalation": {"monthly_budget": 0}})
        self.assertEqual(self.raise_one()["state"], "open")

    def test_the_count_of_defaults_is_reported_beside_the_spend(self):
        e = self.raise_one(default_after_minutes=1)
        self.app.escalations.take_defaults(self.tid, at=e["raised_date"] + 120)
        self.assertEqual(self.app.escalations.spend(self.tid)["defaulted"], 1)

    def test_a_past_month_does_not_count_against_this_one(self):
        e = self.raise_one()
        with self.app.store.tx() as c:
            c.execute("update escalations set month = '2020-01' where id = ?", (e["id"],))
        self.assertEqual(self.app.escalations.spend(self.tid)["used"], 0)


class ReadingTests(EscCase):
    def test_the_reading_lists_open_overdue_and_the_ladder(self):
        e = self.raise_one(default_after_minutes=1)
        reading = self.app.escalations.reading(self.tid, at=e["raised_date"] + 120)
        self.assertEqual(len(reading["open"]), 1)
        self.assertEqual(len(reading["overdue"]), 1)
        self.assertEqual(reading["ladder"][0]["rung"], "next_holder")

    def test_listing_by_state_validates_the_state(self):
        with self.assertRaises(ValidationError):
            self.app.escalations.list(self.tid, state="sulking")


class RouteTests(EscCase):
    def test_the_routes_answer(self):
        from fastapi.testclient import TestClient
        from api.routes import create_api
        client = TestClient(create_api(self.app))
        made = client.post(f"/agents/{self.anne['id']}/escalations", json={
            "category": "money", "what_happened": "double charge",
            "already_tried": "checked the ledger", "decision_needed": "refund 40",
            "default_action": "refund and note", "default_after_minutes": 60}).json()
        self.assertEqual(made["state"], "open")
        refused = client.post(f"/agents/{self.anne['id']}/escalations", json={
            "category": "money", "what_happened": "double charge",
            "already_tried": "checked the ledger", "decision_needed": "refund 40",
            "default_action": "wait", "default_after_minutes": 60})
        self.assertEqual(refused.status_code, 422)
        listed = client.get(f"/crews/{self.tid}/escalations").json()
        self.assertEqual(len(listed["escalations"]), 1)
        reading = client.get(f"/crews/{self.tid}/escalations/reading").json()
        self.assertEqual(reading["spend"]["used"], 1)
        by_agent = client.post(f"/escalations/{made['id']}/answer",
                               json={"by": self.cy["id"], "answer": "yes"})
        self.assertEqual(by_agent.status_code, 422)
        answered = client.post(f"/escalations/{made['id']}/answer",
                               json={"by": "owner@example.com", "answer": "refund it"}).json()
        self.assertEqual(answered["outcome"], "person_decided")
