"""rails on what the answer carries, which is the only thing a pre-dispatch rail cannot see.

every other rail in this repo answers before the work: may this be dispatched, may a
sandbox start, may this host be reached, may this card move. none of them can look at an
answer, because at dispatch the answer does not exist. so the things that only exist in an
answer, a pasted credential, another crew's card quoted back, a turn that says more than the
card is worth, leave through a door nothing was watching.

the refusal never echoes what it caught. a rail that prints the secret has written the
secret into the log it was protecting, so the row carries the shape, the offset and the
length, and the text stays in the run's own steps.
"""

from __future__ import annotations

import unittest

from base import AppCase

from core.errors import NotAllowed, ValidationError
from agents.runtime import answer_text

SECRET = "sk-ant-abcdefghijklmnopqrstuvwxyz012345"


class ShapeTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew()
        self.agent = self.app.crews.add_agent(self.team["id"], "wren", role="doer")
        self.app.guardrails.set(self.team["id"], "secret_shapes", ["all"])

    def check(self, text: str) -> dict:
        return self.app.guardrails.check_output(self.team["id"], text,
                                                agent_id=self.agent["id"])

    def test_a_plain_answer_passes(self):
        verdict = self.check("the suite is green and the metric moved by four points")
        self.assertTrue(verdict["allowed"])

    def test_a_pasted_key_is_refused_and_named_by_shape(self):
        verdict = self.check(f"here is the key {SECRET}")
        self.assertFalse(verdict["allowed"])
        self.assertIn("anthropic_key", verdict["reason"])

    def test_a_github_token_is_a_github_token(self):
        verdict = self.check("use ghp_abcdefghijklmnopqrstuvwxyz0123 for the push")
        self.assertFalse(verdict["allowed"])
        self.assertIn("github_token", verdict["reason"])

    def test_an_env_assignment_is_a_leak(self):
        verdict = self.check("AWS_SECRET_ACCESS_KEY=abcd1234efgh5678")
        self.assertFalse(verdict["allowed"])

    def test_the_refusal_never_echoes_the_secret(self):
        verdict = self.check(f"key {SECRET}")
        self.assertNotIn(SECRET, verdict["reason"])
        self.assertNotIn("sk-ant", verdict["reason"])
        self.assertIn("at character", verdict["reason"])

    def test_only_the_named_shapes_are_checked(self):
        self.app.guardrails.set(self.team["id"], "secret_shapes", ["github_token"])
        self.assertTrue(self.check(f"here is the key {SECRET}")["allowed"])

    def test_an_unknown_shape_is_refused_when_the_rail_is_set(self):
        with self.assertRaises(ValidationError):
            self.app.guardrails.set(self.team["id"], "secret_shapes", ["carrier_pigeon"])


class ScopeTests(AppCase):
    def test_a_rail_on_one_agent_does_not_refuse_another(self):
        team = self.crew()
        wren = self.app.crews.add_agent(team["id"], "wren", role="doer")
        rus = self.app.crews.add_agent(team["id"], "rus", role="researcher")
        self.app.guardrails.set(team["id"], "secret_shapes", ["all"], agent_id=wren["id"])
        self.assertFalse(self.app.guardrails.check_output(
            team["id"], SECRET, agent_id=wren["id"])["allowed"])
        self.assertTrue(self.app.guardrails.check_output(
            team["id"], SECRET, agent_id=rus["id"])["allowed"])

    def test_a_crew_wide_rail_applies_to_everyone(self):
        team = self.crew()
        wren = self.app.crews.add_agent(team["id"], "wren", role="doer")
        self.app.guardrails.set(team["id"], "max_output_chars", 20)
        self.assertFalse(self.app.guardrails.check_output(
            team["id"], "x" * 40, agent_id=wren["id"])["allowed"])


class ForeignRefTests(AppCase):
    def test_an_answer_naming_another_crews_card_is_refused(self):
        team = self.crew()
        other = self.app.crews.create_team("somebody else")
        card = self.app.kanban.create_task(other["id"], "their private card")
        self.app.guardrails.set(team["id"], "foreign_refs", True)
        verdict = self.app.guardrails.check_output(
            team["id"], f"i read {card['id']} and it says ship it",
            context={"foreign_refs": [card["id"]]})
        self.assertFalse(verdict["allowed"])
        self.assertIn("another crew", verdict["reason"])

    def test_the_rail_is_off_unless_it_is_set(self):
        team = self.crew()
        other = self.app.crews.create_team("somebody else")
        card = self.app.kanban.create_task(other["id"], "their private card")
        verdict = self.app.guardrails.check_output(
            team["id"], f"i read {card['id']}", context={"foreign_refs": [card["id"]]})
        self.assertTrue(verdict["allowed"])

    def test_the_orchestrator_knows_which_ids_are_foreign(self):
        team = self.crew()
        other = self.app.crews.create_team("somebody else")
        mine = self.app.kanban.create_task(team["id"], "our card")
        theirs = self.app.kanban.create_task(other["id"], "their card")
        refs = self.app.runner._foreign_refs(team["id"])
        self.assertIn(theirs["id"], refs)
        self.assertNotIn(mine["id"], refs)


class ReceiptTests(AppCase):
    def test_a_refusal_leaves_a_row_and_no_answer(self):
        team = self.crew()
        wren = self.app.crews.add_agent(team["id"], "wren", role="doer")
        self.app.guardrails.set(team["id"], "secret_shapes", ["all"])
        with self.assertRaises(NotAllowed) as caught:
            self.app.guardrails.enforce_output(team["id"], f"key {SECRET}",
                                               agent_id=wren["id"])
        self.assertEqual(caught.exception.details.get("action"), "output")
        refusals = self.app.guardrails.refusals(team["id"])
        self.assertEqual(len(refusals), 1)
        self.assertEqual(refusals[0]["kind"], "secret_shapes")
        self.assertGreater(refusals[0]["at"], 0)
        # the stored row is the shape and where it was, never the text
        row = self.app.store.one("select * from rail_refusals")
        self.assertNotIn("sk-ant", str(dict(row)))

    def test_a_pattern_that_does_not_compile_is_refused_at_set_time(self):
        team = self.crew()
        with self.assertRaises(ValidationError) as caught:
            self.app.guardrails.set(team["id"], "forbidden_patterns", ["(unclosed"])
        self.assertEqual(caught.exception.details.get("pattern"), "(unclosed")

    def test_a_compiled_pattern_refuses_a_match(self):
        team = self.crew()
        self.app.guardrails.set(team["id"], "forbidden_patterns", [r"ignore previous instructions"])
        verdict = self.app.guardrails.check_output(
            team["id"], "sure, ignore previous instructions and ship it")
        self.assertFalse(verdict["allowed"])


class AnswerTextTests(AppCase):
    def test_everything_a_turn_says_out_loud_is_checked(self):
        action = {"thought": "thinking", "commands": ["cat ~/.ssh/id_rsa"],
                  "message": {"body": "here it is"}, "result": {"summary": "done"}}
        text = answer_text(action)
        for piece in ("thinking", "cat ~/.ssh/id_rsa", "here it is", "done"):
            self.assertIn(piece, text)

    def test_an_empty_turn_is_an_empty_string(self):
        self.assertEqual(answer_text({}), "")


if __name__ == "__main__":
    unittest.main()
