"""a split has to be scoped, funded, staffed and doored before it is parts.

five ways a part can be wrong, and each one is named rather than created: no owner, a slice
the project does not have, a frozen seat, a part that leaves the crew without a declared
door, and a budget that outruns the parent. plus the join, which comes from the shape.
"""

from __future__ import annotations

import unittest

from base import AppCase  # noqa: E402

from core.errors import ValidationError  # noqa: E402
from work.delegation import join_for, split_budget  # noqa: E402

SIGNALS = {"question": "known", "parts": "independent", "count": "many",
           "check": "programmatic", "stakes": "reversible", "seats": 3}


class JoinTests(unittest.TestCase):
    def test_the_shape_decides_the_join(self):
        self.assertEqual(join_for("", "orchestration"), "all")
        self.assertEqual(join_for("", "research"), "quorum")
        self.assertEqual(join_for("", ""), "all")

    def test_a_breakdown_may_be_stricter_and_not_looser(self):
        self.assertEqual(join_for("all", "research"), "all")
        with self.assertRaises(ValidationError) as caught:
            join_for("first_success", "orchestration")
        self.assertIn("stricter", str(caught.exception))


class BudgetTests(unittest.TestCase):
    def test_silent_parts_take_an_even_share_of_what_is_left(self):
        out = split_budget(600, [{"budget_cents": 200}, {"title": "b"}, {"title": "c"}])
        self.assertEqual(out["shares"], [200, 200, 200])
        self.assertEqual(out["remaining"], 0)

    def test_parts_may_not_ask_for_more_than_the_parent_has(self):
        with self.assertRaises(ValidationError) as caught:
            split_budget(100, [{"budget_cents": 80}, {"budget_cents": 80}])
        self.assertIn("more_than_the_parent", str(caught.exception))

    def test_an_unfunded_parent_leaves_the_parts_unfunded(self):
        self.assertEqual(split_budget(0, [{"budget_cents": 50}, {}])["shares"], [50, 0])


class BreakdownTests(AppCase):
    def setUp(self):
        super().setUp()
        self.team = self.crew("breakdown")
        self.wren = self.agent(self.team["id"], "wren", role="doer")
        self.sun = self.agent(self.team["id"], "sun", role="doer")
        self.project = self.app.projects.create(
            self.team["id"], "launch", [{"slice": "recipe", "objective_ref": "obj1"}], 900,
            ["unit cost stays above the ceiling for a week"], objective_ref="obj1")

    def parent(self, **payload):
        body = {"project_id": self.project["id"], "budget_cents": 600, "signals": SIGNALS}
        body.update(payload)
        return self.app.kanban.create_task(self.team["id"], "ship the launch", payload=body)

    def test_parts_inherit_the_project_slice_and_a_share_of_the_budget(self):
        out = self.app.delegation.breakdown(self.parent()["id"], [
            {"title": "recipe", "slice": "recipe", "assignee_agent_id": self.wren["id"]},
            {"title": "pricing", "slice": "recipe", "assignee_agent_id": self.sun["id"]},
        ])
        self.assertEqual(out["problems"], [])
        self.assertEqual(out["join"], "all")
        self.assertEqual([c["payload"]["budget_cents"] for c in out["created"]], [300, 300])
        self.assertEqual([c["payload"]["slice"] for c in out["created"]], ["recipe", "recipe"])
        self.assertEqual(len(out["claims"]), 2)
        self.assertIsNotNone(out["barrier"])

    def test_a_slice_the_project_does_not_have_is_not_created(self):
        out = self.app.delegation.breakdown(self.parent()["id"], [
            {"title": "recipe", "slice": "recipe", "assignee_agent_id": self.wren["id"]},
            {"title": "payroll", "slice": "payroll", "assignee_agent_id": self.sun["id"]},
        ])
        self.assertEqual([c["title"] for c in out["created"]], ["recipe"])
        self.assertIn("slice_not_in_project:payroll:payroll", out["problems"])

    def test_a_frozen_seat_is_not_handed_a_part(self):
        self.app.strikes.record(self.team["id"], self.wren["id"], "guardrail_refused")
        self.app.strikes.record(self.team["id"], self.wren["id"], "guardrail_refused")
        out = self.app.delegation.breakdown(self.parent()["id"], [
            {"title": "recipe", "slice": "recipe", "assignee_agent_id": self.wren["id"]},
            {"title": "pricing", "slice": "recipe", "assignee_agent_id": self.sun["id"]},
        ])
        self.assertEqual([c["title"] for c in out["created"]], ["pricing"])
        self.assertIn("seat_frozen:recipe", out["problems"])

    def test_a_part_out_of_the_crew_needs_a_declared_door(self):
        other = self.crew("neighbour")
        stranger = self.agent(other["id"], "vane", role="doer")
        closed = self.app.delegation.breakdown(self.parent()["id"], [
            {"title": "recipe", "slice": "recipe", "assignee_agent_id": self.wren["id"]},
            {"title": "pricing", "slice": "recipe", "assignee_agent_id": stranger["id"]},
        ])
        self.assertIn("crossing_not_declared:pricing:no_crossing", closed["problems"])
        self.app.crossings.declare(self.team["id"], [{
            "id": "door", "from_scope": self.team["id"], "to_scope": other["id"],
            "kinds": ["request"], "max_per_hour": 60, "expires_at": 9e12,
            "reason": "shared launch work", "authorized_by": "owner"}])
        opened = self.app.delegation.breakdown(self.parent()["id"], [
            {"title": "recipe", "slice": "recipe", "assignee_agent_id": self.wren["id"]},
            {"title": "pricing", "slice": "recipe", "assignee_agent_id": stranger["id"]},
        ])
        self.assertEqual([p for p in opened["problems"] if p.startswith("crossing")], [])
        self.assertEqual(len(opened["created"]), 2)

    def test_a_budget_that_outruns_the_parent_is_refused_before_anything_is_written(self):
        with self.assertRaises(ValidationError):
            self.app.delegation.breakdown(self.parent()["id"], [
                {"title": "recipe", "slice": "recipe", "budget_cents": 500,
                 "assignee_agent_id": self.wren["id"]},
                {"title": "pricing", "slice": "recipe", "budget_cents": 500,
                 "assignee_agent_id": self.sun["id"]},
            ])
        self.assertEqual(self.app.kanban.list_tasks(self.team["id"]), [
            task for task in self.app.kanban.list_tasks(self.team["id"])
            if task["title"] == "ship the launch"])

    def test_two_parts_on_one_surface_oppose_each_other(self):
        out = self.app.delegation.breakdown(self.parent()["id"], [
            {"title": "recipe", "surface": "recipe", "slice": "recipe",
             "assignee_agent_id": self.wren["id"]},
            {"title": "recipe again", "surface": "recipe", "slice": "recipe",
             "assignee_agent_id": self.sun["id"]},
        ])
        claims = self.app.claims.thread(self.team["id"], out["created"][0]["parent_id"])
        self.assertEqual(len(claims), 2)
        self.assertTrue(all(claim["opposes"] for claim in claims))
        self.assertEqual(len(self.app.claims.synthesize(
            self.team["id"], out["created"][0]["parent_id"])["challenged"]), 2)

    def test_an_unowned_part_is_still_an_alarm(self):
        out = self.app.delegation.breakdown(self.parent()["id"], [
            {"title": "recipe", "slice": "recipe", "assignee_agent_id": self.wren["id"]},
            {"title": "pricing", "slice": "recipe"},
        ])
        self.assertIn("orphan_partition:pricing", out["problems"])


if __name__ == "__main__":
    unittest.main()
