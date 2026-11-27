"""a card cannot close against a premise that moved underneath it.

darkrun keeps `input_witnesses` on a unit and a drift sweep compares them. the same hole here
from the other side: a run reads a brief, a few knowledge entries and its parent's result, all
mutable rows, and nothing recorded which version it read. so edit a knowledge entry after the
run and the card still closes as if it had been read.

the pure half hashes and compares; the sweep only decides what to report. nothing reopens a
card on its own, because re-running closed work spends money and reopening one that drifted
cosmetically spends attention.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from base import AppCase  # noqa: E402
from work import witness as witness_module  # noqa: E402


def card(**extra) -> dict:
    return {"id": "task_a", "team_id": "team_a", "title": "cancel a booking",
            "description": "the guest asked twice", "required_skills": [],
            "payload": {"kind": "support"}, **extra}


class DigestTests(unittest.TestCase):
    def test_the_same_content_hashes_the_same(self):
        self.assertEqual(witness_module.digest("x"), witness_module.digest("x"))
        self.assertNotEqual(witness_module.digest("x"), witness_module.digest("y"))

    def test_a_premise_list_names_the_brief_first(self):
        found = witness_module.premises(card())
        self.assertEqual([row["kind"] for row in found], ["brief"])
        self.assertEqual(found[0]["ref"], "task_a")

    def test_an_absent_premise_is_not_a_premise(self):
        found = witness_module.premises(card(), knowledge=[], parent=None)
        self.assertEqual(len(found), 1)

    def test_a_knowledge_entry_and_a_parent_are_each_a_premise(self):
        found = witness_module.premises(
            card(), knowledge=[{"id": "kn_1", "title": "refunds", "body": "always"}],
            parent={"id": "task_p", "title": "parent", "evidence_ref": "run:1"})
        self.assertEqual([row["kind"] for row in found], ["brief", "knowledge", "parent"])

    def test_the_crew_writing_back_during_a_run_is_not_drift(self):
        # a run records spend and result on its own payload; a witness that reported those
        # as drift would report every card that ever ran
        before = witness_module.premises(card())
        after = witness_module.premises(card(payload={"kind": "support", "spent_cents": 40,
                                                     "run_id": "run_1", "result": {"ok": True}}))
        self.assertEqual(before[0]["digest"], after[0]["digest"])

    def test_an_edited_brief_is_a_change(self):
        before = witness_module.premises(card())
        after = witness_module.premises(card(description="the guest asked once"))
        findings = witness_module.compare(before, after)
        self.assertEqual([row["change"] for row in findings], ["changed"])

    def test_a_deleted_premise_is_named_as_removed(self):
        before = witness_module.premises(
            card(), knowledge=[{"id": "kn_1", "title": "refunds", "body": "always"}])
        after = witness_module.premises(card())
        findings = witness_module.compare(before, after)
        self.assertEqual(findings[0]["change"], "removed")
        self.assertEqual(findings[0]["kind"], "knowledge")
        self.assertEqual(findings[0]["now"], "")

    def test_a_premise_that_appeared_later_is_named_as_added(self):
        before = witness_module.premises(card())
        after = witness_module.premises(
            card(), knowledge=[{"id": "kn_2", "title": "refunds", "body": "never"}])
        findings = witness_module.compare(before, after)
        self.assertEqual([row["change"] for row in findings], ["added"])


class DriftTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("studio")
        self.agent_row = self.app.crews.add_agent(self.team["id"], "wren", role="doer")
        self.card = self.app.kanban.create_task(
            self.team["id"], "cancel a booking", description="the guest asked twice",
            column="ready")

    def run_once(self) -> dict:
        return self.app.runner.dispatch(self.card["id"])

    def test_a_run_records_what_it_was_made_from(self):
        out = self.run_once()
        rows = self.app.witness.for_run(out["run_id"])
        self.assertTrue(rows)
        self.assertEqual({row["kind"] for row in rows}, {"brief"})
        self.assertEqual(rows[0]["task_id"], self.card["id"])

    def test_an_untouched_card_does_not_drift(self):
        self.run_once()
        self.assertFalse(self.app.witness.drift(self.card["id"])["drifted"])

    def test_a_card_nobody_ran_is_not_reported_as_drift(self):
        report = self.app.witness.drift(self.card["id"])
        self.assertFalse(report["witnessed"])
        self.assertEqual(report["findings"], [])
        self.assertIn("no run", report["why"])

    def test_editing_the_brief_after_the_run_is_drift(self):
        self.run_once()
        self.app.kanban.edit(self.card["id"], {"description": "the guest asked once"})
        report = self.app.witness.drift(self.card["id"])
        self.assertTrue(report["drifted"])
        self.assertEqual(report["findings"][0]["kind"], "brief")
        self.assertEqual(report["findings"][0]["change"], "changed")

    def test_editing_the_knowledge_the_run_was_told_is_drift(self):
        self.app.knowledge.record(self.team["id"], kind="fact", title="refunds",
                                  body="refunds on a cancelled booking are always approved, no manager needed")
        out = self.run_once()
        kinds = {row["kind"] for row in self.app.witness.for_run(out["run_id"])}
        self.assertIn("knowledge", kinds)
        entry = self.app.knowledge.search(self.team["id"], limit=1)[0]
        self.app.knowledge.update(entry["id"], body="refunds on a cancelled booking need a manager to approve them")
        report = self.app.witness.drift(self.card["id"])
        self.assertTrue(report["drifted"])
        self.assertEqual(report["findings"][0]["kind"], "knowledge")
        self.assertEqual(report["findings"][0]["ref"], entry["id"])

    def test_the_crew_sweep_lists_only_the_cards_that_drifted(self):
        self.run_once()
        other = self.app.kanban.create_task(self.team["id"], "book a table",
                                           description="six people", column="ready")
        self.app.runner.dispatch(other["id"])
        self.app.kanban.edit(self.card["id"], {"description": "the guest asked once"})
        sweep = self.app.witness.sweep(self.team["id"])
        self.assertEqual(sweep["checked"], 2)
        self.assertEqual(sweep["drifted"], 1)
        self.assertEqual(sweep["cards"][0]["task_id"], self.card["id"])

    def test_the_sweep_does_not_move_a_card(self):
        self.run_once()
        self.app.kanban.edit(self.card["id"], {"description": "the guest asked once"})
        before = self.app.kanban.get_task(self.card["id"])
        self.app.witness.sweep(self.team["id"])
        after = self.app.kanban.get_task(self.card["id"])
        self.assertEqual(before["column"], after["column"])

    def test_a_witnessed_card_reports_its_premises_over_the_api(self):
        from fastapi.testclient import TestClient
        from api.routes import create_api
        client = TestClient(create_api(self.app))
        out = self.run_once()
        rows = client.get(f"/cards/{self.card['id']}/premises").json()["premises"]
        self.assertEqual(rows[0]["run_id"], out["run_id"])
        self.app.kanban.edit(self.card["id"], {"description": "the guest asked once"})
        report = client.get(f"/cards/{self.card['id']}/drift").json()
        self.assertTrue(report["drifted"])
        sweep = client.get(f"/crews/{self.team['id']}/drift").json()
        self.assertEqual(sweep["drifted"], 1)
