"""an eval: the same input against every candidate, scored the same way, recorded.

the property worth holding is that a matrix is decided before it is spent, and that a
report can be read after the code that produced it has moved on. both are tested here,
along with the refusal that matters most: a scorer that cannot decide is an error, not
a row of zeros that looks like a result.
"""

from __future__ import annotations

import unittest

from base import AppCase

from agents.runtime import DeterministicModel
from core.errors import ValidationError
from work.evals import matrix, output_of, preflight, score


class ScorerTests(unittest.TestCase):
    """the vocabulary, and what each one refuses."""

    def test_exact_matches_the_whole_answer(self):
        self.assertEqual(score({"kind": "exact", "expect": "forty two"}, "forty two")[0], 1.0)
        self.assertEqual(score({"kind": "exact", "expect": "forty two"}, "about forty two")[0], 0.0)

    def test_contains_is_case_insensitive(self):
        self.assertEqual(score({"kind": "contains", "expect": "TICKET"}, "closed ticket 12")[0], 1.0)
        self.assertEqual(score({"kind": "contains", "expect": "ticket"}, "closed")[0], 0.0)

    def test_regex_matches_across_lines(self):
        self.assertEqual(score({"kind": "regex", "expect": r"^done: \w+$"}, "done: yes")[0], 1.0)
        self.assertEqual(score({"kind": "regex", "expect": r"^done: \w+$"}, "not done")[0], 0.0)

    def test_an_unknown_scorer_is_refused(self):
        with self.assertRaises(ValidationError) as caught:
            score({"kind": "vibes", "expect": "x"}, "x")
        self.assertIn("scorer kind", str(caught.exception))

    def test_a_scorer_without_an_expectation_cannot_decide(self):
        with self.assertRaises(ValidationError) as caught:
            score({"kind": "contains"}, "anything")
        self.assertIn("expect", str(caught.exception))

    def test_a_pattern_that_does_not_compile_is_a_configuration_error(self):
        with self.assertRaises(ValidationError) as caught:
            score({"kind": "regex", "expect": "([unclosed"}, "x")
        self.assertIn("compile", str(caught.exception))

    def test_every_verdict_carries_a_reason(self):
        points, detail = score({"kind": "contains", "expect": "nope"}, "yes")
        self.assertEqual(points, 0.0)
        self.assertIn("nope", detail)


class MatrixTests(unittest.TestCase):
    def test_the_grid_is_every_pair_in_declaration_order(self):
        pairs = matrix([{"id": "s1"}, {"id": "s2"}], [{"id": "a"}, {"id": "b"}])
        self.assertEqual([(s["id"], c["id"]) for s, c in pairs],
                         [("s1", "a"), ("s1", "b"), ("s2", "a"), ("s2", "b")])

    def test_a_duplicate_id_is_refused(self):
        with self.assertRaises(ValidationError):
            matrix([{"id": "s1"}, {"id": "s1"}], [{"id": "a"}])

    def test_an_empty_id_is_refused(self):
        with self.assertRaises(ValidationError):
            matrix([{"id": "  "}], [{"id": "a"}])

    def test_an_empty_grid_is_refused(self):
        with self.assertRaises(ValidationError):
            matrix([], [{"id": "a"}])
        with self.assertRaises(ValidationError):
            matrix([{"id": "s"}], [])

    def test_preflight_reads_the_scorer_before_anything_runs(self):
        with self.assertRaises(ValidationError):
            preflight([{"id": "s", "input": "x"}], [{"id": "a"}], {"kind": "nonsense"})

    def test_a_scenario_without_an_input_is_refused(self):
        with self.assertRaises(ValidationError) as caught:
            preflight([{"id": "s", "input": "  "}], [{"id": "a"}],
                      {"kind": "contains", "expect": "x"})
        self.assertIn("input", str(caught.exception))

    def test_the_output_is_the_message_before_the_thought(self):
        self.assertEqual(output_of({"message": {"body": "hello"}, "thought": "hm"}), "hello")
        self.assertEqual(output_of({"message": {"body": " "}, "thought": "hm"}), "hm")
        self.assertEqual(output_of({"commands": []}), "")


class EvalServiceTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew()

    def grid(self, **over) -> dict:
        body = {
            "scenarios": [{"id": "s1", "input": "say hi"}, {"id": "s2", "input": "say bye"}],
            "candidates": [{"id": "terse", "config": {"system_prompt": "be terse"}},
                           {"id": "warm", "config": {"system_prompt": "be warm"}}],
            "scorer": {"kind": "contains", "expect": "hi"},
            "name": "greeting",
        }
        body.update(over)
        return self.app.evals.create(self.team["id"], body["scenarios"], body["candidates"],
                                     body["scorer"], name=body["name"])

    def test_a_grid_is_created_as_a_draft(self):
        run = self.grid()
        self.assertEqual(run["status"], "draft")
        self.assertEqual(len(run["scenarios"]), 2)
        self.assertEqual(run["scorer"]["kind"], "contains")

    def test_an_invalid_grid_is_refused_and_nothing_is_written(self):
        with self.assertRaises(ValidationError):
            self.grid(scorer={"kind": "contains"})
        self.assertEqual(self.app.evals.list(self.team["id"]), [])

    def test_running_records_one_row_per_cell(self):
        run = self.grid()
        self.app.evals.model = DeterministicModel(script=[
            {"thought": "hi", "message": {"body": "hi there"}},
            {"thought": "hi again"},
            {"thought": "bye"},
            {"thought": "goodbye"},
        ])
        done = self.app.evals.run(run["id"])
        self.assertEqual(done["status"], "done")
        self.assertEqual(len(done["cases"]), 4)
        self.assertEqual([cell["score"] for cell in done["cases"]], [1.0, 1.0, 0.0, 0.0])

    def test_the_summary_means_each_candidate_and_names_the_winner(self):
        run = self.grid()
        self.app.evals.model = DeterministicModel(script=[
            {"thought": "hi"},
            {"thought": "hi"},
            {"thought": "no"},
            {"thought": "no"},
        ])
        summary = self.app.evals.run(run["id"])["summary"]
        means = {row["candidate_id"]: row["mean"] for row in summary["candidates"]}
        self.assertEqual(means, {"terse": 0.5, "warm": 0.5})
        # a tie goes to the candidate declared first, never to the alphabet
        self.assertEqual(summary["best"], "terse")

    def test_a_rerun_replaces_the_cells_instead_of_appending(self):
        run = self.grid()
        self.app.evals.model = DeterministicModel(script=[{"thought": "hi"}])
        self.app.evals.run(run["id"])
        again = self.app.evals.run(run["id"])
        self.assertEqual(len(again["cases"]), 4)

    def test_running_without_a_model_says_so(self):
        run = self.grid()
        self.app.evals.model = None
        with self.assertRaises(ValidationError) as caught:
            self.app.evals.run(run["id"])
        self.assertIn("no model", str(caught.exception))

    def test_an_eval_belongs_to_its_crew(self):
        self.grid()
        other = self.crew("other")
        self.assertEqual(self.app.evals.list(other["id"]), [])

    def test_a_missing_eval_is_not_found(self):
        from core.errors import NotFound

        with self.assertRaises(NotFound):
            self.app.evals.get("eval_nope")
