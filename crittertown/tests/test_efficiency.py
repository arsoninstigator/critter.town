"""money per correct outcome, not cards closed.

a dark factory is judged on cost per correct outcome against a baseline someone can reproduce.
volume is the number that hides the retries: three attempts and two rejections on one card is
one correct outcome, and counting cards makes the retries vanish.

the pure half does the division and refuses to invent a figure when nothing was correct or when
spend is unknown. the service reads settled reservations, card columns and review moves, and
the comparison is the same measurement over the previous window of equal length.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from base import AppCase  # noqa: E402
from work import efficiency as eff  # noqa: E402


class DivisionTests(unittest.TestCase):
    def test_no_correct_outcome_is_no_figure(self):
        self.assertIsNone(eff.rate(500, 0))
        self.assertEqual(eff.rate(500, 2), 250.0)

    def test_spend_is_divided_by_outcomes_not_cards(self):
        block = eff.summarise([
            {"spend_cents": 300, "correct": True, "rejected": 2},
            {"spend_cents": 300, "correct": False, "rejected": 0},
            {"spend_cents": 200, "correct": True, "rejected": 0},
        ])
        self.assertEqual(block["cards"], 3)
        self.assertEqual(block["correct"], 2)
        self.assertEqual(block["spend_cents"], 800)
        self.assertEqual(block["cost_per_correct"], 400.0)
        self.assertEqual(block["cost_per_card"], 266.67)

    def test_a_rejected_card_keeps_its_spend(self):
        # the retries bought nothing, and the money spent buying nothing is the story
        block = eff.summarise([{"spend_cents": 600, "correct": True, "rejected": 2}])
        self.assertEqual(block["cost_per_correct"], 600.0)
        self.assertEqual(block["rejected"], 2)

    def test_unknown_spend_is_reported_not_zeroed(self):
        block = eff.summarise([{"spend_cents": None, "correct": True, "rejected": 0},
                               {"spend_cents": 100, "correct": True, "rejected": 0}])
        self.assertTrue(block["partial"])
        self.assertEqual(block["correct_with_unknown_spend"], 1)
        self.assertEqual(block["spend_cents"], 100)
        self.assertEqual(block["cost_per_correct"], 50.0)

    def test_an_empty_crew_is_empty_not_free(self):
        block = eff.summarise([])
        self.assertEqual(block["cards"], 0)
        self.assertIsNone(block["cost_per_correct"])
        self.assertFalse(block["partial"])

    def test_a_correct_outcome_is_not_yet_a_durable_one(self):
        block = eff.summarise([{"spend_cents": 200, "correct": True, "durable": False,
                                "rejected": 0}])
        self.assertEqual(block["correct"], 1)
        self.assertEqual(block["durable"], 0)
        self.assertIsNone(block["cost_per_durable"])
        self.assertEqual(block["cost_per_correct"], 200.0)

    def test_a_partial_window_is_not_a_baseline(self):
        current = eff.summarise([{"spend_cents": None, "correct": True, "rejected": 0}])
        earlier = eff.summarise([{"spend_cents": 100, "correct": True, "rejected": 0}])
        found = eff.compare(current, earlier)
        self.assertEqual(found["verdict"], "unknown")
        self.assertIsNotNone(found["delta_cents"])

    def test_cheaper_and_dearer_are_named(self):
        cheap = eff.summarise([{"spend_cents": 100, "correct": True, "rejected": 0}])
        dear = eff.summarise([{"spend_cents": 300, "correct": True, "rejected": 0}])
        self.assertEqual(eff.compare(cheap, dear)["verdict"], "cheaper")
        self.assertEqual(eff.compare(dear, cheap)["verdict"], "dearer")
        self.assertEqual(eff.compare(dear, dear)["verdict"], "level")

    def test_the_rules_travel_with_the_number(self):
        found = eff.compare(eff.summarise([]), eff.summarise([]))
        self.assertTrue(found["reproducible"])
        self.assertIn("settled budget reservations", " ".join(found["rules"]))


class ServiceTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("studio")
        self.doer = self.app.crews.add_agent(self.team["id"], "wren", role="doer")
        self.other = self.app.crews.add_agent(self.team["id"], "holt", role="doer")

    def card(self, title: str, agent: dict | None = None) -> dict:
        task = self.app.kanban.create_task(self.team["id"], title, description="x",
                                          column="ready")
        self.app.kanban.assign(task["id"], (agent or self.doer)["id"])
        return self.app.kanban.get_task(task["id"])

    def spend(self, task: dict, cents: float) -> None:
        held = self.app.budget.hold(self.team["id"], cents, task_id=task["id"],
                                    agent_id=task["assignee_agent_id"] or "")
        self.app.budget.settle(held["id"], spent_cents=cents)

    def close(self, task: dict) -> dict:
        self.app.kanban.move(task["id"], "in_progress", "owner")
        self.app.kanban.move(task["id"], "review", "owner")
        return self.app.kanban.move(task["id"], "done", "owner", evidence="commit:abc")

    def reject(self, task: dict) -> None:
        self.app.kanban.move(task["id"], "in_progress", "owner")
        self.app.kanban.move(task["id"], "review", "owner")
        self.app.kanban.move(task["id"], "in_progress", "owner", note="the cite was wrong")

    def test_one_correct_card_at_its_cost(self):
        task = self.card("settle the policy")
        self.spend(task, 400)
        self.close(task)
        report = self.app.efficiency.for_crew(self.team["id"])
        self.assertEqual(report["total"]["correct"], 1)
        self.assertEqual(report["total"]["cost_per_correct"], 400.0)

    def test_a_rejected_card_costs_its_retries(self):
        task = self.card("settle the policy")
        self.spend(task, 300)
        self.reject(task)
        again = self.app.kanban.get_task(task["id"])
        self.spend(again, 300)
        self.close(again)
        report = self.app.efficiency.for_crew(self.team["id"])
        self.assertEqual(report["total"]["correct"], 1)
        self.assertEqual(report["total"]["rejected"], 1)
        self.assertEqual(report["total"]["cost_per_correct"], 600.0)
        self.assertEqual([r["task_id"] for r in report["retried"]], [task["id"]])

    def test_a_card_that_paid_and_did_not_close_is_named(self):
        task = self.card("chase the invoice")
        self.spend(task, 250)
        report = self.app.efficiency.for_crew(self.team["id"])
        self.assertEqual(report["total"]["correct"], 0)
        self.assertIsNone(report["total"]["cost_per_correct"])
        self.assertEqual([c["task_id"] for c in report["expensive_without_outcome"]],
                         [task["id"]])

    def test_a_card_still_in_review_is_not_an_outcome(self):
        task = self.card("settle the policy")
        self.spend(task, 100)
        self.app.kanban.move(task["id"], "in_progress", "owner")
        self.app.kanban.move(task["id"], "review", "owner")
        report = self.app.efficiency.for_crew(self.team["id"])
        self.assertEqual(report["total"]["correct"], 0)

    def test_agents_are_ranked_by_cost_per_outcome(self):
        cheap = self.card("cheap card")
        self.spend(cheap, 100)
        self.close(cheap)
        dear = self.card("dear card", agent=self.other)
        self.spend(dear, 900)
        self.close(dear)
        report = self.app.efficiency.for_crew(self.team["id"])
        self.assertEqual([a["agent_id"] for a in report["agents"]],
                         [self.doer["id"], self.other["id"]])
        self.assertEqual(report["agents"][0]["cost_per_correct"], 100.0)

    def test_spend_outside_the_window_is_not_counted(self):
        task = self.card("settle the policy")
        self.spend(task, 400)
        self.close(task)
        until = __import__("core.db", fromlist=["now"]).now()
        report = self.app.efficiency.for_crew(self.team["id"], since=until + 10,
                                             until=until + 20)
        self.assertEqual(report["total"]["spend_cents"], 0)

    def test_the_baseline_is_the_previous_window(self):
        task = self.card("settle the policy")
        self.spend(task, 400)
        self.close(task)
        found = self.app.efficiency.compare_windows(self.team["id"])
        self.assertEqual(found["now"]["cost_per_correct"], 400.0)
        self.assertIsNone(found["before"]["cost_per_correct"])
        self.assertEqual(found["verdict"], "unknown")

    def test_a_card_closed_a_moment_ago_is_not_durable_yet(self):
        task = self.card("settle the policy")
        self.spend(task, 400)
        self.close(task)
        report = self.app.efficiency.for_crew(self.team["id"])
        self.assertEqual(report["total"]["correct"], 1)
        self.assertEqual(report["total"]["durable"], 0)
        self.assertEqual(report["durability_days"], 7.0)
        # the same card at a checkpoint it has already passed
        passed = self.app.efficiency.for_crew(self.team["id"], durability_days=0)
        self.assertEqual(passed["total"]["durable"], 1)
        self.assertEqual(passed["total"]["cost_per_durable"], 400.0)

    def test_a_card_that_came_back_is_named_and_not_durable(self):
        task = self.card("settle the policy")
        self.spend(task, 400)
        self.close(task)
        self.app.kanban.move(task["id"], "in_progress", "owner", note="the cite was wrong")
        report = self.app.efficiency.for_crew(self.team["id"], durability_days=0)
        self.assertEqual(report["total"]["durable"], 0)
        self.assertEqual([c["task_id"] for c in report["came_back"]], [task["id"]])
        self.assertEqual(report["total"]["correct"], 0)

    def test_a_card_that_came_back_and_closed_again_is_durable_again(self):
        # durable is a state at the checkpoint, not a clean history: the churn still shows in
        # came_back, and the second close is the one credited
        task = self.card("settle the policy")
        self.spend(task, 400)
        self.close(task)
        self.app.kanban.move(task["id"], "in_progress", "owner", note="the cite was wrong")
        self.close(self.app.kanban.get_task(task["id"]))
        report = self.app.efficiency.for_crew(self.team["id"], durability_days=0)
        self.assertEqual(report["total"]["correct"], 1)
        self.assertEqual(report["total"]["durable"], 1)
        self.assertEqual([c["task_id"] for c in report["came_back"]], [task["id"]])

    def test_one_card_reads_its_own_cost(self):
        task = self.card("settle the policy")
        self.spend(task, 150)
        self.close(task)
        found = self.app.efficiency.for_card(task["id"])
        self.assertTrue(found["correct"])
        self.assertTrue(found["spend_known"])
        self.assertEqual(found["spend_cents"], 150.0)

    def test_a_card_with_no_reservation_says_so(self):
        task = self.card("settle the policy")
        self.close(task)
        found = self.app.efficiency.for_card(task["id"])
        self.assertTrue(found["correct"])
        self.assertFalse(found["spend_known"])
        self.assertIsNone(found["spend_cents"])

    def test_the_routes_answer(self):
        from fastapi.testclient import TestClient
        from api.routes import create_api
        client = TestClient(create_api(self.app))
        task = self.card("settle the policy")
        self.spend(task, 200)
        self.close(task)
        crew = client.get(f"/crews/{self.team['id']}/efficiency").json()
        self.assertEqual(crew["total"]["cost_per_correct"], 200.0)
        self.assertIn("baseline", crew)
        week = client.get(f"/crews/{self.team['id']}/efficiency?days=30&compare=false").json()
        self.assertEqual(week["window_days"], 30.0)
        self.assertNotIn("baseline", week)
        one = client.get(f"/cards/{task['id']}/efficiency").json()
        self.assertEqual(one["spend_cents"], 200.0)
