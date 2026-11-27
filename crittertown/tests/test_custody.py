"""custody: a card in someone's hands is a lease, and a failed attempt costs.

the two failures this exists for are the ones a board cannot see. a worker that dies
while holding a card, and a card that is retried until everyone is tired of it. the
first is an expiry on the row; the second is an attempt count with a ceiling and a
second verb, replan, for the case where the brief was the problem.
"""

from __future__ import annotations

import unittest

from base import AppCase

from core.errors import Conflict, NotFound, ValidationError
from work.custody import MAX_ATTEMPTS


class LeaseTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        team = self.crew()
        self.agent = self.agent_(team["id"], "wren", role="writer")
        self.card = self.app.kanban.create_task(team["id"], "draft", column="ready")

    def agent_(self, team_id, name, **kwargs):
        return self.app.crews.add_agent(team_id, name, **kwargs)

    def test_a_claim_writes_a_holder_and_an_expiry(self):
        lease = self.app.custody.claim(self.card["id"], self.agent["id"])
        self.assertEqual(lease["holder"], self.agent["id"])
        task = self.app.kanban.get_task(self.card["id"])
        self.assertEqual(task["lease"]["holder"], self.agent["id"])
        self.assertGreater(task["lease"]["expires"], task["created_date"])
        # the expiry is also the row's next deadline, so the sweep needs no second table
        self.assertEqual(task["next_action_at"], task["lease"]["expires"])

    def test_a_second_holder_is_refused_by_name(self):
        self.app.custody.claim(self.card["id"], self.agent["id"])
        other = self.app.crews.add_agent(self.card["team_id"], "rus", role="research")
        with self.assertRaises(Conflict) as caught:
            self.app.custody.claim(self.card["id"], other["id"])
        self.assertEqual(caught.exception.details["reason"], "not_holder")
        self.assertEqual(caught.exception.details["holder"], self.agent["id"])

    def test_the_same_holder_claiming_twice_renews_rather_than_resets(self):
        self.app.custody.claim(self.card["id"], self.agent["id"])
        again = self.app.custody.claim(self.card["id"], self.agent["id"])
        self.assertEqual(again["renewals"], 1,
                         "a replayed claim that resets the counters is a replay that "
                         "erases the evidence of spinning")

    def test_renewal_extends_the_lease_and_counts(self):
        first = self.app.custody.claim(self.card["id"], self.agent["id"])
        second = self.app.custody.renew(self.card["id"], self.agent["id"])
        self.assertGreaterEqual(second["expires"], first["expires"])
        self.assertEqual(second["renewals"], 1)

    def test_only_the_holder_may_renew(self):
        self.app.custody.claim(self.card["id"], self.agent["id"])
        other = self.app.crews.add_agent(self.card["team_id"], "rus", role="research")
        with self.assertRaises(Conflict):
            self.app.custody.renew(self.card["id"], other["id"])

    def test_an_expired_lease_makes_the_card_free_again(self):
        self.app.custody.claim(self.card["id"], self.agent["id"], ttl=-1)
        self.assertEqual(self.app.custody.held_by(self.card["id"]), "",
                         "an expired lease is not a holder: that is what makes a dead "
                         "worker recoverable without a reaper")
        rus = self.app.crews.add_agent(self.card["team_id"], "rus", role="research")
        self.assertEqual(self.app.custody.claim(self.card["id"], rus["id"])["holder"], rus["id"])

    def test_a_card_outside_work_is_not_claimable(self):
        parking = self.app.kanban.create_task(self.card["team_id"], "later", column="backlog")
        with self.assertRaises(ValidationError):
            self.app.custody.claim(parking["id"], self.agent["id"])

    def test_finishing_releases_the_lease_and_names_the_run(self):
        self.app.custody.claim(self.card["id"], self.agent["id"])
        self.app.custody.complete(self.card["id"], self.agent["id"], evidence="run:run_1")
        task = self.app.kanban.get_task(self.card["id"])
        self.assertEqual(task["lease"]["holder"], "")
        self.assertEqual(task["next_action_at"], None)
        self.assertEqual(task["evidence_ref"], "run:run_1")

    def test_an_unknown_card_is_a_not_found(self):
        with self.assertRaises(NotFound):
            self.app.custody.claim("task_nope", self.agent["id"])


class AttemptTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        team = self.crew()
        self.agent = self.app.crews.add_agent(team["id"], "wren", role="writer")
        self.card = self.app.kanban.create_task(team["id"], "draft", column="ready")

    def take(self):
        self.app.custody.claim(self.card["id"], self.agent["id"])

    def test_abandoning_spends_one_attempt_and_puts_the_card_back(self):
        self.take()
        outcome = self.app.custody.abandon(self.card["id"], self.agent["id"], "the machine died")
        self.assertEqual(outcome["attempts"], 1)
        self.assertEqual(outcome["column"], "ready")
        task = self.app.kanban.get_task(self.card["id"])
        self.assertEqual(task["attempts"], 1)
        self.assertEqual(task["lease"]["holder"], "")
        self.assertIsNotNone(task["claim_deadline_at"],
                             "a retryable card needs a claim window or it can never be "
                             "reported unclaimable")

    def test_the_last_attempt_blocks_instead_of_retrying_forever(self):
        for _ in range(MAX_ATTEMPTS):
            self.take()
            outcome = self.app.custody.abandon(self.card["id"], self.agent["id"], "died")
        self.assertEqual(outcome["attempts"], MAX_ATTEMPTS)
        self.assertEqual(outcome["column"], "blocked")
        task = self.app.kanban.get_task(self.card["id"])
        self.assertEqual(task["blocked_reason"][:20], "died")
        with self.assertRaises(Conflict) as caught:
            self.app.custody.claim(self.card["id"], self.agent["id"])
        self.assertEqual(caught.exception.details["reason"], "attempts_exhausted")

    def test_a_wrong_brief_blocks_at_once(self):
        self.take()
        outcome = self.app.custody.abandon(
            self.card["id"], self.agent["id"], "the recipient does not exist", terminal=True)
        self.assertEqual(outcome["column"], "blocked")
        self.assertEqual(outcome["attempts"], 1,
                         "a bad instruction is not a failed holder, so it must not spend "
                         "three holders before a person sees it")

    def test_a_non_holder_cannot_give_the_card_back(self):
        self.take()
        rus = self.app.crews.add_agent(self.card["team_id"], "rus", role="research")
        with self.assertRaises(Conflict):
            self.app.custody.abandon(self.card["id"], rus["id"], "not mine")

    def test_replan_rewrites_the_brief_and_returns_the_attempts(self):
        self.take()
        self.app.custody.abandon(self.card["id"], self.agent["id"], "died")
        replanned = self.app.custody.replan(self.card["id"], "draft the post, then the reply")
        self.assertEqual(replanned["attempts"], 0)
        task = self.app.kanban.get_task(self.card["id"])
        self.assertEqual(task["description"], "draft the post, then the reply")
        self.assertEqual(task["attempts"], 0)

    def test_a_replan_needs_a_different_brief(self):
        self.app.custody.replan(self.card["id"], "draft the post, then the reply")
        with self.assertRaises(ValidationError):
            self.app.custody.replan(self.card["id"], "draft the post, then the reply")
        with self.assertRaises(ValidationError):
            self.app.custody.replan(self.card["id"], "   ")

    def test_a_held_card_cannot_be_replanned(self):
        self.take()
        with self.assertRaises(Conflict):
            self.app.custody.replan(self.card["id"], "a different brief entirely")

    def test_an_agent_cannot_replan_itself(self):
        """the counter that catches spinning is not the agent's to reset."""
        with self.assertRaises(ValidationError):
            self.app.custody.replan(self.card["id"], "a different brief", actor=self.agent["id"])


class DispatchUnderLeaseTests(AppCase):
    def test_a_finished_run_leaves_no_holder(self):
        team = self.crew()
        self.agent(team["id"])
        card = self.task(team["id"], "job")
        self.app.runner.dispatch(card["id"])
        task = self.app.kanban.get_task(card["id"])
        self.assertEqual(task["lease"]["holder"], "")
        self.assertTrue(task["evidence_ref"].startswith("run:"))

    def test_a_runtime_failure_spends_an_attempt_and_keeps_the_card(self):
        team = self.crew()
        self.agent(team["id"])
        card = self.task(team["id"], "job")

        def no_machine(*args, **kwargs):
            raise __import__("core.errors", fromlist=["ProviderNotConfigured"]).ProviderNotConfigured(
                "no machine")

        original = self.app.runner._sandbox
        self.app.runner._sandbox = no_machine
        try:
            out = self.app.runner.dispatch(card["id"])
        finally:
            self.app.runner._sandbox = original
        task = self.app.kanban.get_task(card["id"])
        self.assertEqual(out["decision"], "env_blocked")
        self.assertEqual(task["column"], "ready", "a dead machine is a retry, not a funeral")
        # and not the card's fault: an environment that never opened spends no attempt
        self.assertEqual(task["attempts"], 0)
        self.assertEqual(task["env_blocks"], 1)

    def test_a_crashed_holder_is_reclaimable(self):
        """the whole point: no process has to remember the holder existed."""
        team = self.crew()
        agent = self.agent(team["id"])
        card = self.task(team["id"], "job")
        self.app.custody.claim(card["id"], agent["id"], ttl=-1)
        self.app.kanban.move(card["id"], "in_progress", "orchestrator")
        preview = self.app.liveness.preview(team["id"])
        self.assertIn("reclaim_expired", [a["kind"] for a in preview["actions"]])


if __name__ == "__main__":
    unittest.main()
