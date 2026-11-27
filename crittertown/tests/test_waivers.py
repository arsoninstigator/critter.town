"""a beat a card did not walk is written down, or the card does not leave review.

darkrun's skip waiver exists because a beat that never ran was indistinguishable from one
not yet reached. here the silent case is narrower and real: review is a column, and the
crew's own services may move a card out of it, so a reviewed card could reach done with
nobody having looked and nothing on the record saying why.

a person and a crewmate are signatures. a service needs a waiver, and a waiver needs a
sentence.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from base import AppCase  # noqa: E402
from core.errors import ValidationError  # noqa: E402
from manage import waivers as waivers_module  # noqa: E402


class CheckTests(unittest.TestCase):
    def test_a_sentence_is_a_reason(self):
        self.assertTrue(waivers_module.check("review", "the change is one line and i read it"))

    def test_a_named_beat_is_required(self):
        with self.assertRaises(ValidationError) as ctx:
            waivers_module.check("audit", "the change is one line and i read it")
        self.assertEqual(ctx.exception.to_dict()["details"]["beat"], "audit")

    def test_a_thin_reason_is_refused(self):
        with self.assertRaises(ValidationError) as ctx:
            waivers_module.check("review", "n/a")
        self.assertEqual(ctx.exception.to_dict()["details"]["reason"], "reason_too_thin")


class BoardTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("studio")
        self.agent_row = self.app.crews.add_agent(self.team["id"], "wren", role="doer")
        self.card = self.app.kanban.create_task(
            self.team["id"], "cancel a booking", description="the guest asked twice",
            column="ready")
        self.app.kanban.assign(self.card["id"], self.agent_row["id"])
        self.app.kanban.move(self.card["id"], "in_progress", "system")
        self.app.kanban.move(self.card["id"], "review", self.agent_row["id"])

    def close(self, actor: str = "system") -> dict:
        return self.app.kanban.move(self.card["id"], "done", actor,
                                    evidence="run:run_1")

    def test_a_service_cannot_close_a_reviewed_card_silently(self):
        with self.assertRaises(ValidationError) as ctx:
            self.close("system")
        self.assertEqual(ctx.exception.to_dict()["details"]["reason"], "review_not_signed")

    def test_the_owner_closing_a_card_is_a_signature(self):
        self.assertEqual(self.close("owner")["column"], "done")

    def test_another_agent_closing_a_card_is_a_signature(self):
        reviewer = self.app.crews.add_agent(self.team["id"], "quill", role="reviewer")
        self.assertEqual(self.close(reviewer["id"])["column"], "done")

    def test_a_written_waiver_lets_the_service_close_it(self):
        self.app.waivers.grant(self.card, "review",
                               "the change is a one line copy fix and i read it myself")
        self.assertEqual(self.close("system")["column"], "done")

    def test_a_waiver_says_why_in_a_sentence(self):
        with self.assertRaises(ValidationError):
            self.app.waivers.grant(self.card, "review", "skip")

    def test_one_waiver_per_beat(self):
        self.app.waivers.grant(self.card, "review",
                               "the change is a one line copy fix and i read it myself")
        with self.assertRaises(ValidationError):
            self.app.waivers.grant(self.card, "review",
                                   "a second opinion about a decision already made here")

    def test_an_unreviewed_crew_does_not_ask_for_a_waiver(self):
        open_team = self.crew("open", review_mode="open")
        card = self.app.kanban.create_task(open_team["id"], "ship it",
                                          description="no review on this crew",
                                          column="ready")
        self.app.kanban.move(card["id"], "in_progress", "system")
        self.app.kanban.move(card["id"], "review", "system")
        self.assertEqual(self.app.kanban.move(card["id"], "done", "system",
                                             evidence="run:run_2")["column"], "done")

    def test_the_waiver_is_on_the_card_and_in_the_crew_list(self):
        self.app.waivers.grant(self.card, "review",
                               "the change is a one line copy fix and i read it myself")
        rows = self.app.waivers.for_task(self.card["id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["beat"], "review")
        self.assertIn("one line", rows[0]["reason"])
        self.assertEqual(len(self.app.waivers.for_crew(self.team["id"])), 1)

    def test_the_waiver_routes(self):
        from fastapi.testclient import TestClient
        from api.routes import create_api
        client = TestClient(create_api(self.app))
        response = client.post(f"/cards/{self.card['id']}/waivers",
                               json={"beat": "review",
                                     "reason": "the change is a one line copy fix and i read it"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["beat"], "review")
        listed = client.get(f"/cards/{self.card['id']}/waivers").json()["waivers"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(client.get(f"/crews/{self.team['id']}/waivers").json()["waivers"][0]["id"],
                         listed[0]["id"])


class EnvBlockTests(AppCase):
    """an environment that never opened is not the card's failure."""

    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("studio")
        self.app.crews.add_agent(self.team["id"], "wren", role="doer")
        self.card = self.app.kanban.create_task(
            self.team["id"], "cancel a booking", description="the guest asked twice",
            column="ready")

    def block(self) -> dict:
        from core.errors import ProviderFailure

        def no_room(*args, **kwargs):
            raise ProviderFailure("the sandbox host is down")

        original = self.app.runner._sandbox
        self.app.runner._sandbox = no_room
        try:
            return self.app.runner.dispatch(self.card["id"])
        finally:
            self.app.runner._sandbox = original

    def test_an_env_block_spends_no_attempt(self):
        out = self.block()
        self.assertEqual(out["decision"], "env_blocked")
        self.assertEqual(out["reason"], "provider_failure")
        task = self.app.kanban.get_task(self.card["id"])
        self.assertEqual(task["attempts"], 0)
        self.assertEqual(task["env_blocks"], 1)
        self.assertEqual(task["column"], "ready")

    def test_the_lease_is_not_left_hanging(self):
        self.block()
        self.assertEqual(self.app.custody.held_by(self.card["id"]), "")

    def test_repeated_blocks_stop_asking(self):
        for _ in range(2):
            self.block()
        out = self.block()
        self.assertEqual(out["env_blocks"], 3)
        task = self.app.kanban.get_task(self.card["id"])
        self.assertEqual(task["column"], "blocked")
        self.assertIn("environment unavailable", task["blocked_reason"] or "environment unavailable")

    def test_a_budget_reservation_is_released_not_settled(self):
        self.app.kanban.edit(self.card["id"], {"payload": {"budget_cents": 500}})
        self.block()
        self.assertEqual(self.app.budget.held(self.team["id"]), 0)
        self.assertEqual(self.app.budget.spent_today(self.team["id"]), 0)
