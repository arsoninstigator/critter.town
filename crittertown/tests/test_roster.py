"""who is on the work, and what each seat is told.

the property worth holding is that a seat's slices come from its role, that a crew may
narrow them and never widen them, and that a roster nobody can fill says so instead of
quietly handing the work to whoever was nearest.
"""

from __future__ import annotations

import unittest

from base import AppCase  # noqa: E402  (puts the repo root on the path)
from work import roster  # noqa: E402
from work.roster import ROLE_SLICES, form, seats_for, slices_for  # noqa: E402

SIGNALS = {"question": "known", "shape": "one", "parts": "coupled",
           "check": "judgement", "stakes": "reversible", "seats": 2}


def signals(**over):
    return {**SIGNALS, **over}


class SeatTests(unittest.TestCase):
    def test_a_shape_answers_with_the_roles_it_needs(self):
        self.assertEqual(seats_for("solo"), ("doer",))
        self.assertEqual(seats_for("harness"), ("doer", "reviewer"))
        self.assertEqual(seats_for("research"), ("researcher", "synthesizer"))
        self.assertEqual(seats_for("orchestration", 2), ("doer", "doer", "integrator"))

    def test_an_unknown_shape_is_refused(self):
        with self.assertRaises(Exception):
            seats_for("committee")

    def test_lanes_are_capped_so_one_card_cannot_become_a_hundred(self):
        self.assertEqual(len(seats_for("orchestration", 99)), 9)

    def test_a_role_is_entitled_to_its_own_slices(self):
        self.assertEqual(slices_for("reviewer")["granted"], ["task", "board", "failures"])

    def test_a_reviewer_is_never_entitled_to_the_doers_lessons(self):
        self.assertNotIn("lessons", ROLE_SLICES["reviewer"])
        self.assertNotIn("inbox", ROLE_SLICES["reviewer"])

    def test_a_crew_may_narrow_and_the_loss_is_named(self):
        narrowed = slices_for("doer", ["task", "intent"])
        self.assertEqual(narrowed["granted"], ["intent", "task"])
        self.assertIn("knowledge", narrowed["withheld"])

    def test_an_unknown_role_is_refused(self):
        with self.assertRaises(Exception):
            slices_for("intern")


class FormTests(unittest.TestCase):
    def test_a_coupled_judgement_task_gets_one_seat(self):
        out = form(signals())
        self.assertEqual(out["shape"], "solo")
        self.assertEqual([s["role"] for s in out["seats"]], ["doer"])
        self.assertTrue(out["decided"])
        self.assertFalse(out["staffed"], "a plan with no crew is decided but not staffed")

    def test_an_open_question_gets_a_researcher_and_a_synthesizer(self):
        out = form(signals(question="open"))
        self.assertEqual(out["shape"], "research")
        self.assertEqual([s["role"] for s in out["seats"]], ["researcher", "synthesizer"])

    def test_independent_parts_get_one_doer_each_and_an_integrator(self):
        out = form(signals(parts="independent", check="programmatic"), parts=3)
        self.assertEqual(out["shape"], "orchestration")
        self.assertEqual([s["role"] for s in out["seats"]],
                         ["doer", "doer", "doer", "integrator"])

    def test_irreversible_work_carries_a_gate(self):
        out = form(signals(stakes="irreversible"))
        self.assertTrue(out["gate"])

    def test_incomplete_signals_return_no_seats_and_name_the_gap(self):
        out = form({"shape": "one"})
        self.assertEqual(out["seats"], [])
        self.assertIn("missing:question", out["problems"])
        self.assertFalse(out["decided"])

    def test_a_seat_nobody_fills_is_a_problem_not_a_silent_borrow(self):
        crew = [{"id": "a1", "name": "wren", "role": "doer"}]
        out = form(signals(question="open"), crew)
        self.assertIn("no_agent_for_role:synthesizer", out["gaps"])
        self.assertTrue(out["decided"])
        self.assertFalse(out["staffed"])

    def test_a_role_beats_a_keyword_and_a_keyword_beats_an_accident(self):
        crew = [
            {"id": "a1", "name": "first", "role": "writer", "skills": ["reviewer"]},
            {"id": "a2", "name": "second", "role": "reviewer"},
        ]
        out = form(signals(check="programmatic"), crew)
        seats = {s["role"]: s["agent"] for s in out["seats"]}
        self.assertEqual(seats["reviewer"], "second")

    def test_one_agent_is_not_a_crew_for_a_gated_shape(self):
        crew = [{"id": "a1", "name": "wren", "role": "doer"}]
        out = form(signals(check="programmatic"), crew)
        self.assertIn("a_review_seat_needs_a_second_agent", out["problems"])
        self.assertFalse(out["decided"])

    def test_the_same_signals_answer_the_same_way(self):
        self.assertEqual(form(signals()), form(signals()))


class SliceCapTests(AppCase):
    def test_a_reviewer_seat_cannot_be_handed_the_doers_lessons(self):
        team = self.crew("capped")
        agent = self.agent(team["id"], "sun")
        card = self.kanban.create_task(team["id"], "check it", column="ready")
        snapshot = self.app.context.compose(agent["id"], task_id=card["id"], role="reviewer")
        names = [s["name"] for s in snapshot["slices"]]
        self.assertIn("task", names)
        self.assertNotIn("lessons", names)
        self.assertNotIn("inbox", names)

    def test_an_unknown_role_refuses_rather_than_composing_everything(self):
        team = self.crew("capped")
        agent = self.agent(team["id"], "sun")
        card = self.kanban.create_task(team["id"], "check it", column="ready")
        with self.assertRaises(ValueError):
            self.app.context.compose(agent["id"], task_id=card["id"], role="intern")
