"""what can prove a card done.

the gate used to accept a sentence. these tests hold the replacement: the kind of work
decides which references count, a crew may narrow that list and never widen it, and the
short way to done walks the same check as the long way.
"""

from __future__ import annotations

import unittest

from base import AppCase

from core.errors import ValidationError
from work.evidence import (
    KIND_EVIDENCE, KINDS, allowed_for, evidence_fits, expected, scheme_of,
)


class SchemeTests(unittest.TestCase):
    def test_a_reference_has_a_scheme(self):
        self.assertEqual(scheme_of("commit:abc1234"), "commit")
        self.assertEqual(scheme_of("run:run_9"), "run")
        self.assertEqual(scheme_of("  ticket:123  "), "ticket")

    def test_a_sentence_has_no_scheme(self):
        self.assertEqual(scheme_of("looks fine to me"), "")
        self.assertEqual(scheme_of("commit:"), "", "a scheme with no payload proves nothing")
        self.assertEqual(scheme_of(""), "")

    def test_every_kind_has_a_list(self):
        for kind in KINDS:
            self.assertIn(kind, KIND_EVIDENCE)
            self.assertTrue(KIND_EVIDENCE[kind])


class FitTests(unittest.TestCase):
    def test_a_commit_closes_code(self):
        self.assertEqual(evidence_fits("code", "commit:abc"), (True, ""))

    def test_a_receipt_closes_a_financial_card(self):
        self.assertTrue(evidence_fits("financial", "invoice:inv_88")[0])

    def test_the_wrong_scheme_is_refused_and_says_what_it_wanted(self):
        ok, reason = evidence_fits("code", "invoice:inv_88")
        self.assertFalse(ok)
        self.assertIn("commit:", reason)
        self.assertIn("run:", reason)

    def test_prose_is_refused_with_a_sentence_a_person_can_act_on(self):
        ok, reason = evidence_fits("code", "shipped it")
        self.assertFalse(ok)
        self.assertIn("must be a reference", reason)

    def test_an_unknown_kind_is_refused(self):
        self.assertFalse(evidence_fits("vibes", "commit:abc")[0])

    def test_a_crew_may_narrow(self):
        self.assertTrue(evidence_fits("code", "commit:abc", narrow=["commit"])[0])
        self.assertFalse(evidence_fits("code", "run:run_9", narrow=["commit"])[0])

    def test_a_crew_may_not_widen(self):
        with self.assertRaises(ValueError):
            allowed_for("code", ["commit", "invoice"])

    def test_expected_is_the_list_a_message_prints(self):
        self.assertEqual(expected("support"), ["ticket", "gate"])

    def test_a_gate_closes_any_kind_because_a_decision_is_a_row(self):
        for kind in KINDS:
            self.assertTrue(evidence_fits(kind, "gate:gate_88")[0], kind)


class GateTests(AppCase):
    def ready_card(self, team_id, kind="code"):
        card = self.app.kanban.create_task(team_id, "job", column="ready", kind=kind)
        self.app.kanban.move(card["id"], "in_progress", "owner")
        return self.app.kanban.move(card["id"], "review", "owner")

    def test_a_card_is_created_with_a_kind(self):
        team = self.crew()
        card = self.app.kanban.create_task(team["id"], "job", kind="research")
        self.assertEqual(card["kind"], "research")
        with self.assertRaises(ValidationError):
            self.app.kanban.create_task(team["id"], "job", kind="vibes")

    def test_a_card_with_no_evidence_cannot_be_approved(self):
        team = self.crew()
        card = self.ready_card(team["id"], kind="code")
        with self.assertRaises(ValidationError) as caught:
            self.app.kanban.approve(card["id"], "owner")
        self.assertIn("commit:", caught.exception.message)
        self.assertEqual(self.app.kanban.get_task(card["id"])["column"], "review",
                         "a refusal must not leave the card in a half approved state")

    def test_the_wrong_reference_cannot_close_a_card(self):
        team = self.crew()
        card = self.ready_card(team["id"], kind="code")
        with self.assertRaises(ValidationError):
            self.app.kanban.approve(card["id"], "owner", evidence="invoice:inv_88")
        self.assertEqual(self.app.kanban.get_task(card["id"])["column"], "review")

    def test_the_right_reference_closes_it_and_is_kept(self):
        team = self.crew()
        card = self.ready_card(team["id"], kind="code")
        done = self.app.kanban.approve(card["id"], "owner", evidence="commit:abc1234")
        self.assertEqual(done["column"], "done")
        self.assertEqual(done["evidence_ref"], "commit:abc1234")

    def test_a_run_closes_a_code_card_because_the_runner_wrote_one(self):
        team = self.crew()
        self.agent(team["id"])
        card = self.task(team["id"], "job")
        self.app.runner.dispatch(card["id"])
        self.assertEqual(self.app.kanban.approve(card["id"], "owner")["column"], "done")

    def test_the_short_way_to_done_is_the_same_gate(self):
        team = self.crew()
        card = self.ready_card(team["id"], kind="financial")
        with self.assertRaises(ValidationError):
            self.app.kanban.move(card["id"], "done", "owner")
        done = self.app.kanban.move(card["id"], "done", "owner", evidence="payout:po_7")
        self.assertEqual(done["column"], "done")
        self.assertEqual(done["evidence_ref"], "payout:po_7")

    def test_a_crew_can_narrow_what_closes_its_cards(self):
        team = self.crew()
        self.app.crews.update_team(team["id"], orchestration={"evidence": {"code": ["commit"]}})
        card = self.ready_card(team["id"], kind="code")
        with self.assertRaises(ValidationError):
            self.app.kanban.approve(card["id"], "owner", evidence="run:run_9")
        self.assertEqual(
            self.app.kanban.approve(card["id"], "owner", evidence="commit:abc")["column"], "done")

    def test_a_narrowing_that_widens_is_refused_rather_than_trusted(self):
        team = self.crew()
        self.app.crews.update_team(team["id"], orchestration={"evidence": {"code": ["commit", "invoice"]}})
        card = self.ready_card(team["id"], kind="code")
        with self.assertRaises(ValidationError):
            self.app.kanban.approve(card["id"], "owner", evidence="commit:abc")


if __name__ == "__main__":
    unittest.main()
