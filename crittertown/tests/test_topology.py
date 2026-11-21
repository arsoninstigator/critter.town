"""which shape the work needs, from declared facts.

the property worth holding is that the same signals answer the same way, that a missing
signal refuses instead of guessing, and that the expensive shape has to be earned rather
than declared.
"""

from __future__ import annotations

import unittest

from base import AppCase

from core.errors import ValidationError
from work import topology

BASE = {"question": "known", "shape": "one", "parts": "coupled",
        "check": "programmatic", "stakes": "reversible", "seats": 1}


def signals(**over) -> dict:
    return {**BASE, **over}


class ChooseTests(unittest.TestCase):
    def test_an_open_question_is_research(self):
        decision = topology.choose(signals(question="open"))
        self.assertEqual(decision["structure"], "research")
        self.assertEqual(decision["because"], "an_open_question_is_discovery_before_it_is_work")

    def test_many_similar_items_are_a_factory(self):
        decision = topology.choose(signals(shape="many"))
        self.assertEqual(decision["structure"], "factory")

    def test_independent_parts_with_a_second_seat_are_orchestration(self):
        decision = topology.choose(signals(parts="independent", seats=2))
        self.assertEqual(decision["structure"], "orchestration")
        self.assertIn("convergence barrier", decision["implements"])

    def test_independent_parts_without_a_second_seat_stay_solo(self):
        decision = topology.choose(signals(parts="independent", seats=1))
        self.assertEqual(decision["structure"], "solo")
        self.assertEqual(decision["because"], "independent_parts_but_no_second_seat")

    def test_coupled_work_with_a_check_is_a_harness(self):
        self.assertEqual(topology.choose(signals())["structure"], "harness")

    def test_coupled_work_judged_by_a_person_is_solo(self):
        decision = topology.choose(signals(check="judgement"))
        self.assertEqual(decision["structure"], "solo")
        self.assertEqual(decision["because"], "coupled_work_does_not_need_a_crew")

    def test_an_open_question_beats_a_batch(self):
        # you cannot batch what you have not defined
        decision = topology.choose(signals(question="open", shape="many"))
        self.assertEqual(decision["structure"], "research")

    def test_irreversible_work_gets_a_gate_whatever_the_shape(self):
        decision = topology.choose(signals(stakes="irreversible"))
        self.assertTrue(decision["gate"])
        self.assertEqual(decision["structure"], "harness")

    def test_a_missing_signal_refuses_instead_of_guessing(self):
        decision = topology.choose({"question": "known", "shape": "one"})
        self.assertEqual(decision["structure"], "")
        self.assertEqual(decision["because"], "signals_incomplete")
        self.assertIn("missing:parts", decision["problems"])
        self.assertIn("missing:seats", decision["problems"])

    def test_an_unknown_value_is_named_rather_than_read_as_the_default(self):
        problems = topology.validate(signals(parts="parallel"))
        self.assertIn("unknown:parts=parallel", problems)

    def test_a_seat_count_of_zero_is_not_a_crew(self):
        self.assertIn("unknown:seats=0", topology.validate(signals(seats=0)))

    def test_every_shape_is_something_this_repo_has(self):
        for shape in topology.SHAPES:
            self.assertTrue(topology.IMPLEMENTED_BY[shape], shape)


class DeclaredStructureTests(unittest.TestCase):
    def test_a_card_that_declares_nothing_is_not_asked_about_its_shape(self):
        result = topology.check_declared({}, {})
        self.assertTrue(result["ok"])

    def test_a_card_may_not_declare_orchestration_its_signals_deny(self):
        with self.assertRaises(ValidationError) as caught:
            topology.check_declared({"structure": "orchestration"}, signals())
        self.assertIn("harness", str(caught.exception))

    def test_a_card_may_declare_a_cheaper_shape_than_its_signals_suggest(self):
        result = topology.check_declared({"structure": "solo"},
                                         signals(parts="independent", seats=2))
        self.assertTrue(result["ok"])
        self.assertEqual(result["chosen"], "orchestration")

    def test_a_structure_that_is_not_a_shape_is_refused(self):
        with self.assertRaises(ValidationError):
            topology.check_declared({"structure": "swarm"}, signals())

    def test_signals_are_read_from_the_payload_and_only_from_a_dict(self):
        self.assertEqual(topology.signals_of({"payload": {"signals": signals()}})["seats"], 1)
        self.assertEqual(topology.signals_of({"payload": {"signals": "many"}}), {})
        self.assertEqual(topology.signals_of({"payload": {}}), {})


class DispatchTests(AppCase):
    def test_a_card_asking_for_orchestration_it_cannot_justify_is_refused(self):
        team = self.crew()
        self.agent(team["id"])
        card = self.kanban.create_task(
            team["id"], "fix one test", column="ready",
            payload={"structure": "orchestration", "signals": BASE})
        with self.assertRaises(ValidationError) as caught:
            self.runner.dispatch(card["id"])
        self.assertIn("declares orchestration", str(caught.exception))

    def test_a_card_with_signals_and_no_declaration_dispatches(self):
        team = self.crew()
        self.agent(team["id"])
        card = self.kanban.create_task(team["id"], "fix one test", column="ready",
                                       payload={"signals": BASE})
        outcome = self.runner.dispatch(card["id"])
        self.assertEqual(outcome["task"]["column"], "review")


if __name__ == "__main__":
    unittest.main()
