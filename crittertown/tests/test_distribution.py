"""where a card's work went, and what a rejection changed on it.

volume hides both. a crew that closes forty cards and one that closes eight look the same on a
board, and a review that says "not yet" three times looks like diligence until you ask what
changed between the second round and the third. a rejection answered by rewriting a field to the
value it already held is a round trip that cost a cycle and taught nobody anything, and a board
that records only which fields were touched cannot tell that from a real fix.

so the tests here are mostly about the difference between "edited" and "changed", which is why
the board now fingerprints each edited field.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from base import AppCase  # noqa: E402
from core.db import new_id, now  # noqa: E402
from core.errors import NotFound  # noqa: E402
from work import distribution as dist  # noqa: E402


def moved(at: float, frm: str, to: str, actor: str = "owner", note: str = "",
          evidence: str = "") -> dict:
    return {"kind": "moved", "actor": actor, "created_date": at,
            "data": {"from": frm, "to": to, "note": note, "evidence": evidence}}


def edited(at: float, fields: list[str], digests: dict | None = None) -> dict:
    data: dict = {"fields": fields}
    if digests is not None:
        data["digests"] = digests
    return {"kind": "edited", "actor": "owner", "created_date": at, "data": data}


class RoundTests(unittest.TestCase):
    def test_a_card_never_rejected_has_no_rounds(self):
        self.assertEqual(dist.rounds([moved(1, "ready", "in_progress"),
                                      moved(2, "in_progress", "review"),
                                      moved(3, "review", "done", evidence="commit:abc")]), [])

    def test_a_rejection_that_changed_the_text_is_rework(self):
        found = dist.rounds([
            moved(1, "ready", "in_progress"), moved(2, "in_progress", "review"),
            edited(2.5, ["description"], {"description": "aaa"}),
            moved(3, "review", "in_progress", note="the cite was wrong"),
            edited(4, ["description"], {"description": "bbb"}),
            moved(5, "in_progress", "review"),
        ])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["verdict"], "rework")
        self.assertEqual(found[0]["changed"], ["description"])
        self.assertEqual(found[0]["note"], "the cite was wrong")
        self.assertTrue(found[0]["certain"])
        self.assertEqual(found[0]["closed_at"], 5.0)

    def test_a_rejection_answered_with_the_same_text_is_silent(self):
        # the tell: the field was edited and the content did not move
        found = dist.rounds([
            moved(1, "ready", "in_progress"), moved(2, "in_progress", "review"),
            edited(2.5, ["description"], {"description": "aaa"}),
            moved(3, "review", "in_progress", note="try again"),
            edited(4, ["description"], {"description": "aaa"}),
            moved(5, "in_progress", "review"),
        ])
        self.assertEqual(found[0]["verdict"], "silent")
        self.assertEqual(found[0]["changed"], [])
        self.assertEqual(found[0]["edits"], 1)

    def test_a_rejection_answered_by_moving_alone_is_silent(self):
        found = dist.rounds([
            moved(3, "review", "in_progress", note="try again"),
            moved(5, "in_progress", "review"),
        ])
        self.assertEqual(found[0]["verdict"], "silent")
        self.assertEqual(found[0]["changed"], [])

    def test_a_rejection_still_open_is_open_not_silent(self):
        found = dist.rounds([moved(3, "review", "in_progress", note="try again")])
        self.assertEqual(found[0]["verdict"], "open")
        self.assertIsNone(found[0]["closed_at"])

    def test_a_rejection_closed_by_the_gate_keeps_its_evidence(self):
        found = dist.rounds([
            moved(3, "review", "in_progress"),
            moved(5, "review", "done", evidence="commit:abc"),
        ])
        self.assertEqual(found[0]["verdict"], "rework")
        self.assertEqual(found[0]["evidence"], "commit:abc")

    def test_an_edit_with_nothing_to_compare_against_is_unproven(self):
        # an edit whose field had no fingerprint before it cannot be called a change, and
        # calling it one would be the same mistake as calling it a rubber stamp
        found = dist.rounds([
            moved(3, "review", "in_progress"),
            edited(4, ["description"]),
            moved(5, "in_progress", "review"),
        ])
        self.assertEqual(found[0]["changed"], [])
        self.assertEqual(found[0]["assumed"], ["description"])
        self.assertFalse(found[0]["certain"])
        self.assertEqual(found[0]["verdict"], "unproven")

    def test_a_card_born_with_digests_makes_its_first_fix_provable(self):
        born = {"kind": "created", "actor": "system", "created_date": 0.5,
                "data": {"column": "ready", "title": "t",
                         "digests": {"description": "aaa", "title": "ttt"}}}
        found = dist.rounds([
            born, moved(3, "review", "in_progress"),
            edited(4, ["description"], {"description": "bbb"}),
            moved(5, "in_progress", "review"),
        ])
        self.assertEqual(found[0]["verdict"], "rework")
        self.assertEqual(found[0]["changed"], ["description"])
        self.assertTrue(found[0]["certain"])

    def test_an_edit_back_to_the_birth_value_is_silent(self):
        born = {"kind": "created", "actor": "system", "created_date": 0.5,
                "data": {"column": "ready", "title": "t",
                         "digests": {"description": "aaa"}}}
        found = dist.rounds([
            born, moved(3, "review", "in_progress"),
            edited(4, ["description"], {"description": "aaa"}),
            moved(5, "in_progress", "review"),
        ])
        self.assertEqual(found[0]["verdict"], "silent")

    def test_two_rounds_are_two_rows(self):
        found = dist.rounds([
            moved(3, "review", "in_progress"), moved(4, "in_progress", "review"),
            moved(5, "review", "in_progress"), moved(6, "in_progress", "review"),
        ])
        self.assertEqual([r["at"] for r in found], [3.0, 5.0])
        self.assertEqual([r["verdict"] for r in found], ["silent", "silent"])


class ShareTests(unittest.TestCase):
    def test_two_hands_split_the_card(self):
        found = dist.share([{"agent_id": "a", "turns": 8, "spend_cents": 800},
                            {"agent_id": "b", "turns": 2, "spend_cents": 200}])
        self.assertEqual(found["turns"], 10)
        self.assertEqual(found["spend_cents"], 1000)
        self.assertEqual([r["agent_id"] for r in found["agents"]], ["a", "b"])
        self.assertEqual(found["agents"][0]["turn_share"], 0.8)
        self.assertEqual(found["agents"][1]["spend_share"], 0.2)
        self.assertTrue(found["concentrated"])

    def test_one_hand_alone_is_not_concentrated(self):
        # a solo card is not a warning, it is a solo card
        found = dist.share([{"agent_id": "a", "turns": 5, "spend_cents": 500}])
        self.assertEqual(found["top_share"], 1.0)
        self.assertFalse(found["concentrated"])

    def test_spend_with_no_agent_is_unattributed_not_split(self):
        found = dist.share([{"agent_id": "a", "turns": 5, "spend_cents": 100},
                            {"agent_id": "", "turns": 0, "spend_cents": 300}])
        self.assertEqual(found["unattributed_cents"], 300)
        self.assertEqual([r["agent_id"] for r in found["agents"]], ["a"])
        # the share is of what the card cost, and a quarter of it went to a named agent
        self.assertEqual(found["agents"][0]["spend_share"], 0.25)

    def test_no_work_is_no_share(self):
        found = dist.share([])
        self.assertEqual(found["turns"], 0)
        self.assertIsNone(found["top_share"])
        self.assertFalse(found["concentrated"])


class SummaryTests(unittest.TestCase):
    def test_silent_rejections_are_counted_apart(self):
        found = dist.summarise([
            {"rounds": [{"verdict": "silent", "certain": True},
                        {"verdict": "rework", "certain": True},
                        {"verdict": "open", "certain": True}]},
            {"rounds": [{"verdict": "silent", "certain": True},
                        {"verdict": "unproven", "certain": False}]},
        ])
        self.assertEqual(found["rejections"], 5)
        self.assertEqual(found["answered"], 4)
        self.assertEqual(found["silent"], 2)
        # the unproven round is left out of the share rather than counted as a rubber stamp
        self.assertEqual(found["silent_share"], 0.6667)
        self.assertEqual(found["unproven"], 1)
        self.assertEqual(found["open"], 1)
        self.assertEqual(found["uncertain"], 1)

    def test_a_crew_never_rejected_has_no_share(self):
        self.assertIsNone(dist.summarise([{"rounds": []}])["silent_share"])


class CardTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew()
        self.doer = self.app.crews.add_agent(self.team["id"], "wren", role="doer")
        self.other = self.app.crews.add_agent(self.team["id"], "pip", role="reviewer")

    def card(self, title: str = "settle the policy") -> dict:
        task = self.app.kanban.create_task(self.team["id"], title, description="x",
                                          column="ready")
        self.app.kanban.assign(task["id"], self.doer["id"])
        return self.app.kanban.get_task(task["id"])

    def turn(self, task: dict, agent: dict, steps: int = 1) -> None:
        with self.app.store.tx() as c:
            c.execute(
                """insert into runs (id, task_id, agent_id, provider, status, steps,
                       created_date) values (?,?,?,?,?,?,?)""",
                (new_id("run"), task["id"], agent["id"], "local", "done",
                 "[" + ",".join("{}" for _ in range(steps)) + "]", now()))

    def spend(self, task: dict, cents: float, agent: dict | None = None) -> None:
        held = self.app.budget.hold(self.team["id"], cents, task_id=task["id"],
                                    agent_id=(agent or self.doer)["id"])
        self.app.budget.settle(held["id"], spent_cents=cents)

    def review(self, task: dict) -> dict:
        self.app.kanban.move(task["id"], "in_progress", "owner")
        return self.app.kanban.move(task["id"], "review", "owner")

    def test_a_fix_after_a_rejection_is_named_with_what_changed(self):
        task = self.card()
        self.turn(task, self.doer, steps=3)
        self.spend(task, 300)
        self.review(task)
        self.app.kanban.move(task["id"], "in_progress", "owner", note="the cite was wrong")
        self.app.kanban.edit(task["id"], {"description": "the cite, corrected"}, actor="owner")
        self.app.kanban.move(task["id"], "review", "owner")
        report = self.app.distribution.for_card(task["id"])
        self.assertEqual(len(report["rounds"]), 1)
        self.assertEqual(report["rounds"][0]["verdict"], "rework")
        self.assertEqual(report["rounds"][0]["changed"], ["description"])
        self.assertEqual(report["summary"]["silent"], 0)

    def test_a_rejection_that_changed_nothing_is_silent(self):
        task = self.card()
        self.review(task)
        self.app.kanban.move(task["id"], "in_progress", "owner", note="try again")
        # the same description written back, which is an edit that changed no text
        self.app.kanban.edit(task["id"], {"description": "x"}, actor="owner")
        self.app.kanban.move(task["id"], "review", "owner")
        report = self.app.distribution.for_card(task["id"])
        self.assertEqual(report["rounds"][0]["verdict"], "silent")
        self.assertEqual(report["summary"]["silent"], 1)
        self.assertEqual(report["summary"]["silent_share"], 1.0)

    def test_the_work_is_split_by_turns_and_spend(self):
        task = self.card()
        self.turn(task, self.doer, steps=6)
        self.turn(task, self.other, steps=2)
        self.spend(task, 600, agent=self.doer)
        self.spend(task, 200, agent=self.other)
        report = self.app.distribution.for_card(task["id"])
        split = report["distribution"]
        self.assertEqual(split["turns"], 8)
        self.assertEqual(split["spend_cents"], 800)
        self.assertEqual(split["agents"][0]["agent_id"], self.doer["id"])
        self.assertEqual(split["agents"][0]["turn_share"], 0.75)
        self.assertTrue(split["concentrated"])

    def test_rework_cost_is_measured_from_the_rejection(self):
        task = self.card()
        self.spend(task, 100)
        self.review(task)
        self.app.kanban.move(task["id"], "in_progress", "owner", note="the cite was wrong")
        self.turn(task, self.doer, steps=4)
        self.spend(task, 250)
        self.app.kanban.move(task["id"], "review", "owner")
        report = self.app.distribution.for_card(task["id"])
        self.assertEqual(report["distribution"]["spend_cents"], 350)
        self.assertEqual(report["rounds"][0]["rework"]["spend_cents"], 250)
        self.assertEqual(report["rounds"][0]["rework"]["turns"], 4)

    def test_a_card_that_does_not_exist_is_not_a_report(self):
        with self.assertRaises(NotFound):
            self.app.distribution.for_card("tsk_nope")

    def test_the_crew_report_names_the_silent_rejections(self):
        loud = self.card("fix the loud one")
        self.review(loud)
        self.app.kanban.move(loud["id"], "in_progress", "owner", note="wrong")
        self.app.kanban.edit(loud["id"], {"description": "corrected"}, actor="owner")
        self.app.kanban.move(loud["id"], "review", "owner")
        quiet = self.card("fix the quiet one")
        self.review(quiet)
        self.app.kanban.move(quiet["id"], "in_progress", "owner", note="wrong")
        self.app.kanban.move(quiet["id"], "review", "owner")
        report = self.app.distribution.for_crew(self.team["id"])
        self.assertEqual(report["total"]["rejections"], 2)
        self.assertEqual(report["total"]["silent"], 1)
        self.assertEqual([c["task_id"] for c in report["silent_rejections"]], [quiet["id"]])

    def test_the_routes_answer(self):
        from fastapi.testclient import TestClient
        from api.routes import create_api
        task = self.card()
        client = TestClient(create_api(self.app))
        card = client.get(f"/cards/{task['id']}/distribution").json()
        self.assertEqual(card["task_id"], task["id"])
        self.assertEqual(card["rounds"], [])
        crew = client.get(f"/crews/{self.team['id']}/distribution").json()
        self.assertEqual(crew["total"]["cards"], 1)
        self.assertEqual(crew["window_days"], 7.0)
