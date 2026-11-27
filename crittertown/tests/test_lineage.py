"""a card whose parent never landed is not a card that failed.

band's factory charges a stage that did not start to every stage above it. here the failure
went the other way: a parent that blocks leaves its children in `ready`, each one is picked
up, and each one spends an attempt against a premise that does not exist yet.

the table is pure and first match wins: a blocked ancestor is inherited, an ancestor waiting
on a person defers, everything else runs. backlog is deliberately not a reason, because a
child queued ahead of an unscheduled parent is a scheduling mistake and parking it would
hide the mistake.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from base import AppCase  # noqa: E402
from work import lineage as lineage_module  # noqa: E402


def row(card_id: str, column: str, reason: str = "", parent_id: str | None = None) -> dict:
    return {"id": card_id, "title": f"card {card_id}", "column": column,
            "blocked_reason": reason, "parent_id": parent_id}


class TableTests(unittest.TestCase):
    def test_a_blocked_ancestor_is_inherited(self):
        verdict = lineage_module.decide([row("p", "blocked", "attempts_exhausted")])
        self.assertEqual(verdict["decision"], "inherit_blocked")
        self.assertEqual(verdict["ancestor"]["id"], "p")

    def test_an_ancestor_in_review_defers(self):
        verdict = lineage_module.decide([row("p", "review")])
        self.assertEqual(verdict["decision"], "defer")
        self.assertEqual(verdict["reason"], "ancestor_in_review")

    def test_the_nearest_problem_is_the_one_named(self):
        verdict = lineage_module.decide([row("near", "review"), row("far", "blocked")])
        self.assertEqual(verdict["decision"], "defer")
        self.assertEqual(verdict["ancestor"]["id"], "near")

    def test_a_settled_chain_runs(self):
        self.assertEqual(lineage_module.decide([row("p", "done"), row("g", "done")])["decision"],
                         "run")

    def test_a_backlog_parent_is_not_a_reason(self):
        # a child queued ahead of a parent nobody scheduled is a mistake to name, not a
        # child to park
        self.assertEqual(lineage_module.decide([row("p", "backlog")])["decision"], "run")

    def test_no_chain_runs(self):
        self.assertEqual(lineage_module.decide([])["decision"], "run")

    def test_the_note_says_which_ancestor_and_why(self):
        verdict = lineage_module.decide([row("p", "blocked", "attempts_exhausted")])
        note = lineage_module.park({"id": "c"}, verdict)
        self.assertIn("card p", note)
        self.assertIn("attempts_exhausted", note)


class ChainTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("studio")
        self.app.crews.add_agent(self.team["id"], "wren", role="doer")
        self.parent = self.app.kanban.create_task(
            self.team["id"], "settle the refund policy", description="the guest asked twice",
            column="ready")
        self.child = self.app.kanban.create_task(
            self.team["id"], "reply to the guest", description="once the policy is settled",
            column="ready", parent_id=self.parent["id"])
        self.grandchild = self.app.kanban.create_task(
            self.team["id"], "close the ticket", description="once the guest is answered",
            column="ready", parent_id=self.child["id"])

    def test_the_chain_is_nearest_first(self):
        found = lineage_module.chain(self.app.kanban.get_task(self.grandchild["id"]),
                                     self.app.kanban)
        self.assertEqual([c["id"] for c in found], [self.child["id"], self.parent["id"]])

    def test_a_card_without_a_parent_has_no_chain(self):
        self.assertEqual(lineage_module.chain(self.app.kanban.get_task(self.parent["id"]),
                                              self.app.kanban), [])

    def test_a_repeated_parent_is_a_cycle_not_a_hang(self):
        with self.app.store.tx() as c:
            c.execute("update tasks set parent_id = ? where id = ?",
                      (self.grandchild["id"], self.parent["id"]))
        task = self.app.kanban.get_task(self.parent["id"])
        self.assertTrue(lineage_module.cycle(task, self.app.kanban))
        self.assertLessEqual(len(lineage_module.chain(task, self.app.kanban)), 12)


class DispatchTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("studio")
        self.agent_row = self.app.crews.add_agent(self.team["id"], "wren", role="doer")
        self.parent = self.app.kanban.create_task(
            self.team["id"], "settle the refund policy", description="the guest asked twice",
            column="ready")
        self.child = self.app.kanban.create_task(
            self.team["id"], "reply to the guest", description="once the policy is settled",
            column="ready", parent_id=self.parent["id"])
        self.app.kanban.assign(self.child["id"], self.agent_row["id"])

    def block_parent(self) -> None:
        self.app.kanban.move(self.parent["id"], "in_progress", "system")
        self.app.kanban.move(self.parent["id"], "blocked", "system",
                             note="attempts exhausted on the brief")

    def test_a_child_of_a_blocked_card_is_not_dispatched(self):
        self.block_parent()
        out = self.app.runner.dispatch(self.child["id"])
        self.assertEqual(out["decision"], "inherit_blocked")
        self.assertEqual(out["reason"], "ancestor_blocked")
        self.assertEqual(out["ancestor"]["id"], self.parent["id"])
        self.assertEqual(out["run_id"], "")

    def test_the_child_parks_and_spends_no_attempt(self):
        self.block_parent()
        self.app.runner.dispatch(self.child["id"])
        task = self.app.kanban.get_task(self.child["id"])
        self.assertEqual(task["column"], "blocked")
        self.assertEqual(task["attempts"], 0)
        self.assertIn("inherited", task["blocked_reason"] or "")

    def test_the_parent_is_not_blamed_for_the_child(self):
        self.block_parent()
        self.app.runner.dispatch(self.child["id"])
        self.assertEqual(self.app.kanban.get_task(self.parent["id"])["attempts"], 0)

    def test_a_child_of_a_card_in_review_waits_without_moving(self):
        self.app.kanban.move(self.parent["id"], "in_progress", "system")
        self.app.kanban.move(self.parent["id"], "review", "system")
        out = self.app.runner.dispatch(self.child["id"])
        self.assertEqual(out["decision"], "defer")
        self.assertEqual(self.app.kanban.get_task(self.child["id"])["column"], "ready")

    def test_a_child_of_a_finished_card_runs(self):
        self.app.kanban.move(self.parent["id"], "in_progress", "system")
        self.app.kanban.move(self.parent["id"], "review", "system")
        self.app.kanban.move(self.parent["id"], "done", "owner", evidence="run:run_p")
        out = self.app.runner.dispatch(self.child["id"])
        self.assertNotIn(out.get("decision"), {"inherit_blocked", "defer"})
        self.assertTrue(out["run_id"])

    def test_a_lane_of_a_fan_out_is_not_gated_by_its_parent(self):
        # the parent is waiting on these cards, so its column cannot be a reason they wait:
        # gating them would deadlock every barrier
        self.app.convergence.open(self.parent["id"],
                                  {self.child["id"]: self.child["id"],
                                   "south": "south"},
                                  [self.child["id"], "south"])
        self.block_parent()
        verdict = self.app.lineage.of(self.app.kanban.get_task(self.child["id"]))
        self.assertEqual(verdict["decision"], "run")

    def test_the_inheritance_is_an_audit_row(self):
        self.block_parent()
        self.app.runner.dispatch(self.child["id"])
        rows = self.app.store.query(
            "select * from agent_audit where action = 'task.inherited_failure'")
        self.assertEqual(len(rows), 1)

    def test_the_crew_view_lists_the_subtree(self):
        self.block_parent()
        report = self.app.lineage.inherited(self.team["id"])
        self.assertEqual([r["task_id"] for r in report["blocked"]], [self.child["id"]])
        self.assertEqual(report["deferred"], [])

    def test_the_routes_answer(self):
        from fastapi.testclient import TestClient
        from api.routes import create_api
        client = TestClient(create_api(self.app))
        self.block_parent()
        one = client.get(f"/cards/{self.child['id']}/lineage").json()
        self.assertEqual(one["decision"], "inherit_blocked")
        self.assertEqual(one["chain"][0]["id"], self.parent["id"])
        crew = client.get(f"/crews/{self.team['id']}/lineage").json()
        self.assertEqual(len(crew["blocked"]), 1)
