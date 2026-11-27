"""the decisions a crew builds on, kept where work can be checked against them.

the failure is quiet: a decision is made in a call, written in a doc, contradicted three weeks
later by work nobody compared it with, and the code still runs. the six shapes it takes are not
about code either, which is why the kinds here are the six kinds of drift and not the parts of a
pull request. a research claim, an operations rule and a pricing rule all live in the same table.

two things are being tested: that a contradiction is computed from a declared subject rather than
guessed from prose, and that an agent's claim is a proposal until a person answers for it.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from base import AppCase  # noqa: E402
from core.errors import NotAllowed, ValidationError  # noqa: E402
from work import ledger as led  # noqa: E402


class PureTests(unittest.TestCase):
    def test_a_subject_is_a_handle_not_a_sentence(self):
        self.assertEqual(led.normalise_subject("  Billing Terms "), "billing.terms")
        self.assertEqual(led.normalise_subject(""), "")

    def test_short_and_common_words_do_not_carry_a_subject(self):
        self.assertEqual(led.terms("the invoice is due and we will not wait"),
                         {"invoice", "wait"})

    def test_two_claims_on_one_subject_disagree(self):
        others = [{"id": "a", "subject": "billing.terms", "text": "terms are 30 days",
                   "status": "active"}]
        self.assertEqual(led.contradicts({"subject": "billing.terms",
                                          "text": "terms are 60 days"}, others), ["a"])

    def test_the_same_claim_restated_is_not_a_contradiction(self):
        others = [{"id": "a", "subject": "billing.terms", "text": "Terms are 30 days.",
                   "status": "active"}]
        self.assertEqual(led.contradicts({"subject": "billing.terms",
                                          "text": "terms are 30 days"}, others), [])

    def test_a_claim_with_no_subject_never_contradicts(self):
        others = [{"id": "a", "subject": "billing.terms", "text": "30 days", "status": "active"}]
        self.assertEqual(led.contradicts({"subject": "", "text": "60 days"}, others), [])

    def test_a_retired_claim_stops_contradicting(self):
        others = [{"id": "a", "subject": "billing.terms", "text": "30 days",
                   "status": "retired"}]
        self.assertEqual(led.contradicts({"subject": "billing.terms", "text": "60 days"},
                                         others), [])

    def test_a_declared_subject_matches_and_says_so(self):
        rows = [{"id": "a", "subject": "billing.terms", "text": "terms are 30 days",
                 "status": "active", "certainty": "settled", "created_date": 1}]
        found = led.relevant(rows, subjects=["billing.terms"], text="anything")
        self.assertEqual(found[0]["matched"], "subject")

    def test_a_short_shared_word_is_a_hint_and_a_long_one_is_a_match(self):
        rows = [{"id": "a", "subject": "", "text": "invoices are due in thirty days",
                 "status": "active", "certainty": "settled", "created_date": 1}]
        # `days` alone is too common to say the card is about this
        self.assertEqual(led.relevant(rows, text="ship it in days"), [])
        # `invoice` carries the subject on its own, and a plural is the same word
        self.assertEqual(len(led.relevant(rows, text="send the invoice")), 1)

    def test_a_contradiction_on_a_declared_subject_blocks(self):
        found = [{"id": "a", "matched": "subject", "certainty": "contradicting"}]
        self.assertEqual(led.verdict(found), "blocked")

    def test_a_contradiction_found_by_word_alone_only_asks(self):
        found = [{"id": "a", "matched": "terms:invoice,due", "certainty": "contradicting"}]
        self.assertEqual(led.verdict(found), "ask")

    def test_a_proposal_asks_and_nothing_asks_nothing(self):
        self.assertEqual(led.verdict([{"matched": "subject", "certainty": "provisional"}]),
                         "ask")
        self.assertEqual(led.verdict([]), "clear")


class LedgerTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew()
        self.doer = self.app.crews.add_agent(self.team["id"], "wren", role="doer")

    def write(self, text: str, **kwargs) -> dict:
        return self.app.ledger.propose(self.team["id"], text, **kwargs)

    def test_a_person_writes_a_settled_claim(self):
        row = self.write("invoices are due in thirty days", drift="logic",
                         subject="billing.terms", source="billing policy",
                         source_kind="doc")
        self.assertEqual(row["certainty"], "settled")
        self.assertEqual(row["decided_by_kind"], "person")
        self.assertEqual(row["subject"], "billing.terms")
        self.assertEqual(row["status"], "active")

    def test_an_agents_claim_is_a_proposal(self):
        row = self.write("invoices are due in sixty days", subject="billing.terms",
                         actor=self.doer["id"], source_kind="agent")
        self.assertEqual(row["certainty"], "provisional")
        self.assertEqual(row["decided_by_kind"], "agent")

    def test_an_agent_cannot_settle_its_own_proposal(self):
        row = self.write("invoices are due in sixty days", subject="billing.terms",
                         actor=self.doer["id"])
        with self.assertRaises(NotAllowed) as caught:
            self.app.ledger.settle(row["id"], actor=self.doer["id"])
        self.assertEqual(caught.exception.details.get("reason"), "agent_cannot_settle")

    def test_a_person_settles_it(self):
        row = self.write("invoices are due in sixty days", subject="billing.terms",
                         actor=self.doer["id"])
        settled = self.app.ledger.settle(row["id"], actor="owner", note="the call on 2 june")
        self.assertEqual(settled["certainty"], "settled")
        self.assertEqual(settled["decided_by_kind"], "person")
        self.assertIn("june", settled["note"])

    def test_two_claims_on_one_subject_are_both_marked(self):
        first = self.write("invoices are due in thirty days", subject="billing.terms")
        second = self.write("invoices are due in sixty days", subject="billing.terms")
        self.assertEqual(second["certainty"], "contradicting")
        self.assertEqual(second["contradicts"], [first["id"]])
        # the other half of the crew has to see it too, or it is a warning nobody reads
        self.assertEqual(self.app.ledger.get(first["id"])["certainty"], "contradicting")
        self.assertEqual(self.app.ledger.get(first["id"])["contradicts"], [second["id"]])
        self.assertEqual([r["id"] for r in self.app.ledger.contradictions(self.team["id"])],
                         [second["id"], first["id"]])

    def test_settling_one_side_names_a_winner_and_leaves_the_loser_marked(self):
        first = self.write("invoices are due in thirty days", subject="billing.terms")
        self.write("invoices are due in sixty days", subject="billing.terms")
        settled = self.app.ledger.settle(first["id"], actor="owner")
        self.assertEqual(settled["certainty"], "settled")
        # the disagreement does not vanish because one side got louder
        self.assertEqual(len(self.app.ledger.contradictions(self.team["id"])), 1)

    def test_retiring_the_loser_clears_the_winner(self):
        first = self.write("invoices are due in thirty days", subject="billing.terms")
        second = self.write("invoices are due in sixty days", subject="billing.terms")
        self.app.ledger.retire(second["id"], reason="the 60 day rule was withdrawn")
        winner = self.app.ledger.get(first["id"])
        self.assertEqual(winner["contradicts"], [])
        self.assertEqual(winner["certainty"], "settled")
        self.assertEqual(self.app.ledger.contradictions(self.team["id"]), [])

    def test_a_supersede_is_one_act(self):
        old = self.write("invoices are due in thirty days", subject="billing.terms")
        new = self.write("invoices are due in sixty days", subject="billing.terms",
                         supersedes=old["id"])
        self.assertEqual(new["certainty"], "settled")
        self.assertEqual(new["contradicts"], [])
        self.assertEqual(new["subject"], "billing.terms")
        self.assertEqual(self.app.ledger.get(old["id"])["status"], "superseded")
        self.assertEqual(self.app.ledger.contradictions(self.team["id"]), [])

    def test_a_statement_needs_text_and_a_known_kind(self):
        with self.assertRaises(ValidationError) as caught:
            self.write("   ")
        self.assertEqual(caught.exception.details.get("reason"), "text_required")
        with self.assertRaises(ValidationError) as caught:
            self.write("something happened", drift="vibes")
        self.assertEqual(caught.exception.details.get("reason"), "unknown_drift")

    def test_the_six_kinds_are_the_six_ways_work_drifts(self):
        self.assertEqual(set(led.DRIFT),
                         {"logic", "compliance", "tooling", "language", "scope", "strategy"})

    def test_statements_are_filtered_by_kind(self):
        self.write("a pricing rule", drift="logic")
        self.write("a retention obligation", drift="compliance")
        found = self.app.ledger.statements(self.team["id"], drift="compliance")
        self.assertEqual([r["text"] for r in found], ["a retention obligation"])

    def test_a_check_records_the_question(self):
        self.write("invoices are due in thirty days", subject="billing.terms")
        found = self.app.ledger.check(self.team["id"], subjects=["billing.terms"],
                                      text="change the invoice terms", actor="owner")
        self.assertEqual(found["verdict"], "clear")
        self.assertEqual(len(found["found"]), 1)
        rows = self.app.store.query("select * from statement_checks where team_id = ?",
                                    (self.team["id"],))
        self.assertEqual(len(rows), 1)

    def test_a_contradiction_on_the_cards_subject_blocks_it(self):
        self.write("invoices are due in thirty days", subject="billing.terms")
        self.write("invoices are due in sixty days", subject="billing.terms")
        task = self.app.kanban.create_task(
            self.team["id"], "move the invoice terms", description="x", column="ready",
            payload={"subjects": ["billing.terms"]})
        found = self.app.ledger.for_card(task["id"])
        self.assertEqual(found["verdict"], "blocked")
        self.assertEqual(len(found["contested"]), 2)
        self.assertIn("settle it first", found["advice"])

    def test_a_card_with_no_declared_subject_is_not_blocked_by_a_word(self):
        self.write("invoices are due in thirty days", subject="billing.terms")
        self.write("invoices are due in sixty days", subject="billing.terms")
        task = self.app.kanban.create_task(
            self.team["id"], "review the invoice workflow", description="the invoice is late",
            column="ready")
        found = self.app.ledger.for_card(task["id"])
        self.assertEqual(found["verdict"], "ask")

    def test_dispatch_records_what_the_ledger_said(self):
        self.write("invoices are due in thirty days", subject="billing.terms")
        self.write("invoices are due in sixty days", subject="billing.terms")
        task = self.app.kanban.create_task(
            self.team["id"], "move the invoice terms", description="x", column="ready",
            payload={"subjects": ["billing.terms"]})
        self.app.kanban.assign(task["id"], self.doer["id"])
        checked = self.app.ledger.for_dispatch(
            self.app.kanban.get_task(task["id"]), self.doer)
        self.assertEqual(checked["verdict"], "blocked")

    def test_strict_dispatch_refuses_a_contradicted_subject(self):
        self.write("invoices are due in thirty days", subject="billing.terms")
        self.write("invoices are due in sixty days", subject="billing.terms")
        task = self.app.kanban.create_task(
            self.team["id"], "move the invoice terms", description="x", column="ready",
            payload={"subjects": ["billing.terms"]})
        self.app.kanban.assign(task["id"], self.doer["id"])
        with self.assertRaises(ValidationError) as caught:
            self.app.ledger.for_dispatch(self.app.kanban.get_task(task["id"]), self.doer,
                                         strict=True)
        self.assertEqual(caught.exception.details.get("reason"), "ledger_contradiction")

    def test_the_routes_answer(self):
        from fastapi.testclient import TestClient
        from api.routes import create_api
        client = TestClient(create_api(self.app))
        base = f"/crews/{self.team['id']}/statements"
        first = client.post(base, json={"text": "invoices are due in thirty days",
                                        "subject": "billing terms",
                                        "drift": "logic"}).json()
        self.assertEqual(first["certainty"], "settled")
        second = client.post(base, json={"text": "invoices are due in sixty days",
                                         "subject": "billing terms"}).json()
        self.assertEqual(second["certainty"], "contradicting")
        self.assertEqual(len(client.get(f"{base}/contradictions").json()["contradictions"]), 2)
        self.assertEqual(len(client.get(base).json()["statements"]), 2)
        checked = client.post(f"{base}/check", json={"subjects": ["billing.terms"],
                                                    "text": "invoice terms"}).json()
        self.assertEqual(checked["verdict"], "blocked")
        self.assertEqual(client.get(f"/statements/{first['id']}").json()["id"], first["id"])
        retired = client.post(f"/statements/{second['id']}/retire",
                              json={"reason": "withdrawn"}).json()
        self.assertEqual(retired["status"], "retired")
