"""a mandate says how a seat works, and a card says what the work is.

band's dark factory disqualifies an entry whose seat mandates name the problem's answer.
the same failure is quieter here: an agent whose instructions carry the card in front of it
reads well and works once. these tests hold the check, and hold the thing that makes it
usable, which is that it never stops a dispatch over a word.

the vocabulary is derived, not listed: the terms this card uses that no other card, no
knowledge doc and no context entry uses. that is what "specific to this card" means, and it
is the part that had to change from band's version, where the specs generate the list.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from base import AppCase  # noqa: E402
from core.errors import NotFound, ValidationError  # noqa: E402
from manage import mandate as mandate_module  # noqa: E402

CARD = ("POST /reservations/{ref}/cancel with idempotency_key; "
        "a failed cancel answers booking_uncertain")


class ShapeTests(unittest.TestCase):
    def test_the_three_shapes_are_found(self):
        tokens = mandate_module.tokens_in(
            "call /reservations/{id}/cancel with idempotency_key and booking-submit")
        kinds = {kind for kind, _ in tokens}
        self.assertEqual(kinds, {"endpoint", "identifier", "test id"})
        self.assertIn("idempotency_key", [term for _, term in tokens])

    def test_prose_and_numbers_are_not_evidence(self):
        # the check reports a line a person can look at and agree with, so a word that
        # needs a judgement call cannot be what it looks for
        self.assertEqual(mandate_module.tokens_in("be careful, take 3 turns, say ok"), [])

    def test_a_slash_joins_two_words_rather_than_starting_a_path(self):
        tokens = mandate_module.tokens_in("handle import/export carefully")
        self.assertNotIn("endpoint", {kind for kind, _ in tokens})


class DistinctiveTests(unittest.TestCase):
    def test_a_term_only_this_card_uses_is_distinctive(self):
        terms = mandate_module.distinctive(CARD, ["email the client about due_date"])
        self.assertIn("idempotency_key", terms)
        self.assertNotIn("due_date", terms)

    def test_a_term_the_crew_already_knows_is_not_distinctive(self):
        # a knowledge doc is read by every seat, so a term in one is not this card's
        terms = mandate_module.distinctive(CARD, ["our policy: every write carries idempotency_key"])
        self.assertNotIn("idempotency_key", terms)


class LintTests(unittest.TestCase):
    def test_a_finding_names_the_line_and_the_term(self):
        text = "how this seat works\nuse /reservations/{ref}/cancel and idempotency_key"
        findings = mandate_module.lint(text, {"/reservations/{ref}/cancel", "idempotency_key"})
        self.assertEqual([f["line"] for f in findings], [2, 2])
        self.assertIn("idempotency_key", [f["term"] for f in findings])

    def test_one_term_twice_on_a_line_is_one_finding(self):
        findings = mandate_module.lint("idempotency_key and idempotency_key", {"idempotency_key"})
        self.assertEqual(len(findings), 1)

    def test_a_mandate_naming_nothing_is_clean(self):
        self.assertEqual(mandate_module.lint("read the card, hand back evidence", {"x_y"}), [])


class CheckTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("studio")

    def card(self, title: str, description: str, column: str = "ready") -> dict:
        return self.app.kanban.create_task(self.team["id"], title,
                                           description=description, column=column)

    def test_a_card_with_no_neighbours_is_thin_and_only_endpoints_are_named(self):
        card = self.card("cancel a booking", CARD)
        vocabulary = self.app.mandate.vocabulary(self.team["id"], card["id"])
        self.assertFalse(vocabulary["thick"])
        seat = self.app.crews.add_agent(self.team["id"], "script", role="doer",
                                       system_prompt="use /reservations/{ref}/cancel")
        checked = self.app.mandate.check(self.team["id"], seat["id"], card["id"])
        self.assertEqual([f["kind"] for f in checked["findings"]], ["endpoint"])

    def test_a_mandate_written_for_the_card_is_flagged(self):
        card = self.card("cancel a booking", CARD)
        self.card("chase an invoice", "email the client about invoice_total", "backlog")
        seat = self.app.crews.add_agent(
            self.team["id"], "script", role="doer",
            system_prompt="cancel bookings with idempotency_key")
        checked = self.app.mandate.check(self.team["id"], seat["id"], card["id"])
        self.assertFalse(checked["ok"])
        self.assertEqual([f["term"] for f in checked["findings"]], ["idempotency_key"])

    def test_a_generic_mandate_passes(self):
        card = self.card("cancel a booking", CARD)
        self.card("chase an invoice", "email the client about invoice_total", "backlog")
        seat = self.app.crews.add_agent(
            self.team["id"], "wren", role="doer",
            system_prompt="take the card in front of you, read its brief, hand back evidence.")
        self.assertTrue(self.app.mandate.check(self.team["id"], seat["id"], card["id"])["ok"])

    def test_a_seat_from_another_crew_is_not_found(self):
        card = self.card("cancel a booking", CARD)
        other = self.crew("other")
        seat = self.app.crews.add_agent(other["id"], "wren", role="doer")
        with self.assertRaises(NotFound):
            self.app.mandate.check(self.team["id"], seat["id"], card["id"])

    def test_a_card_from_another_crew_is_refused(self):
        other = self.crew("other")
        foreign = self.app.kanban.create_task(other["id"], "elsewhere",
                                              description="nothing in common", column="ready")
        seat = self.app.crews.add_agent(self.team["id"], "wren", role="doer")
        with self.assertRaises(ValidationError) as caught:
            self.app.mandate.check(self.team["id"], seat["id"], foreign["id"])
        self.assertEqual(caught.exception.details.get("reason"), "wrong_crew")


class DispatchTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("studio")
        self.card = self.app.kanban.create_task(
            self.team["id"], "cancel a booking", description=CARD, column="ready")
        self.app.kanban.create_task(self.team["id"], "chase an invoice",
                                    description="email about invoice_total", column="backlog")

    def test_a_dispatch_reports_the_finding_and_still_runs(self):
        seat = self.app.crews.add_agent(self.team["id"], "script", role="doer",
                                       system_prompt="cancel bookings with idempotency_key")
        self.app.kanban.assign(self.card["id"], seat["id"])
        out = self.app.runner.dispatch(self.card["id"])
        self.assertTrue(out["run_id"])
        self.assertEqual([f["term"] for f in out["mandate_findings"]], ["idempotency_key"])

    def test_the_finding_is_recorded_against_the_card(self):
        seat = self.app.crews.add_agent(self.team["id"], "script", role="doer",
                                       system_prompt="cancel bookings with idempotency_key")
        self.app.kanban.assign(self.card["id"], seat["id"])
        self.app.runner.dispatch(self.card["id"])
        row = self.app.store.one(
            "select * from agent_audit where action = ? and target_id = ?",
            ("mandate.overfit", self.card["id"]))
        self.assertIsNotNone(row)
        self.assertEqual(row["agent_id"], seat["id"])

    def test_a_generic_mandate_records_nothing(self):
        seat = self.app.crews.add_agent(self.team["id"], "wren", role="doer",
                                       system_prompt="read the brief and hand back evidence")
        self.app.kanban.assign(self.card["id"], seat["id"])
        out = self.app.runner.dispatch(self.card["id"])
        self.assertEqual(out["mandate_findings"], [])
        self.assertIsNone(self.app.store.one(
            "select * from agent_audit where action = ?", ("mandate.overfit",)))

    def test_strict_refuses_with_the_reason_and_the_findings(self):
        seat = self.app.crews.add_agent(self.team["id"], "script", role="doer",
                                       system_prompt="cancel bookings with idempotency_key")
        with self.assertRaises(ValidationError) as caught:
            self.app.mandate.for_dispatch(self.app.kanban.get_task(self.card["id"]),
                                          seat, strict=True)
        self.assertEqual(caught.exception.details.get("reason"), "mandate_names_the_card")
        self.assertTrue(caught.exception.details.get("findings"))


class RouteTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        from fastapi.testclient import TestClient
        from api.routes import create_api
        self.client = TestClient(create_api(self.app))
        self.crew = self.client.post("/crews", json={"name": "studio", "mission": "ship"}).json()
        self.card = self.client.post(
            f"/crews/{self.crew['id']}/tasks",
            json={"title": "cancel a booking", "description": CARD, "column": "ready"}).json()
        self.other = self.client.post(
            f"/crews/{self.crew['id']}/tasks",
            json={"title": "chase an invoice", "description": "email about invoice_total",
                  "column": "backlog"}).json()
        self.seat = self.client.post(
            f"/crews/{self.crew['id']}/agents",
            json={"name": "script", "role": "doer",
                  "system_prompt": "cancel bookings with idempotency_key"}).json()

    def test_the_check_route_returns_the_findings(self):
        response = self.client.post(
            f"/crews/{self.crew['id']}/mandate/check",
            json={"agent_id": self.seat["id"], "task_id": self.card["id"]})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["findings"][0]["term"], "idempotency_key")

    def test_a_check_without_a_target_is_a_validation_error(self):
        response = self.client.post(f"/crews/{self.crew['id']}/mandate/check", json={})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["details"]["reason"], "missing_target")

    def test_the_vocabulary_route_says_what_a_mandate_must_not_name(self):
        response = self.client.get(f"/cards/{self.card['id']}/mandate/vocabulary")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("idempotency_key", body["terms"])
        self.assertTrue(body["thick"])
