"""the sweep: the four things a board cannot see, found by comparing rows to a clock.

a sweep that fixes what it finds hides the bug that produced it, so the pure half
returns findings and transitions and only the service applies the transitions. these
tests hold that line: the report is deterministic, a violation never repairs itself,
and a reclaimed card past its attempts blocks in the same step.
"""

from __future__ import annotations

import unittest

from base import AppCase

from work import liveness as sweep_mod
from work.liveness import apply, healthy, is_terminal, sweep, violations


def card(**overrides) -> dict:
    """a task row as the sweep sees it: the stored names the query returns."""
    row = {
        "id": "task_a", "column": "ready", "lease_holder": None, "lease_expires": None,
        "lease_renewals": 0, "attempts": 0, "max_attempts": 3,
        "claim_deadline_at": 1_000.0, "next_action_at": 1_000.0,
    }
    row.update(overrides)
    return row


class ViolationTests(unittest.TestCase):
    def test_a_quiet_ready_card_has_nothing_wrong_with_it(self):
        self.assertEqual(violations(card()), [])

    def test_work_in_progress_without_a_holder_is_named(self):
        self.assertEqual(violations(card(column="in_progress")), ["leased_without_lease"])

    def test_a_lease_without_an_expiry_is_named(self):
        found = violations(card(column="in_progress", lease_holder="agent_1"))
        self.assertIn("leased_without_expiry", found)

    def test_a_ready_card_without_a_claim_window_is_named(self):
        self.assertIn("ready_without_claim_deadline",
                      violations(card(claim_deadline_at=None)))

    def test_a_backlog_card_is_not_demanded_a_deadline(self):
        """a parking lot is supposed to sit still, so demanding deadlines there would
        fill the report with rows that are behaving."""
        self.assertEqual(violations(card(column="backlog",
                                         claim_deadline_at=None, next_action_at=None)), [])

    def test_done_is_terminal(self):
        self.assertTrue(is_terminal({"column": "done"}))
        self.assertFalse(is_terminal({"column": "review"}))


class SweepTests(unittest.TestCase):
    def test_an_expired_lease_is_reclaimed_with_an_extra_attempt(self):
        actions = sweep([card(column="in_progress", lease_holder="agent_1",
                              lease_expires=500.0, next_action_at=500.0)], now=1_000.0)
        reclaim = [a for a in actions if a["kind"] == "reclaim_expired"]
        self.assertEqual(len(reclaim), 1)
        self.assertEqual(reclaim[0]["next_attempts"], 1)

    def test_a_lease_still_running_is_left_alone(self):
        actions = sweep([card(column="in_progress", lease_holder="agent_1",
                              lease_expires=2_000.0, next_action_at=2_000.0)], now=1_000.0)
        self.assertEqual(actions, [])

    def test_a_holder_that_renews_without_producing_is_spinning(self):
        actions = sweep([card(column="in_progress", lease_holder="agent_1",
                              lease_expires=2_000.0, lease_renewals=3,
                              next_action_at=2_000.0)], now=1_000.0)
        self.assertEqual([a["kind"] for a in actions], ["revoke_spinning"])

    def test_a_ready_card_past_its_window_is_an_alarm_not_a_state_change(self):
        actions = sweep([card(claim_deadline_at=500.0, next_action_at=500.0)], now=1_000.0)
        self.assertEqual([a["kind"] for a in actions], ["alarm_unclaimable"])

    def test_an_exhausted_card_is_reported_once(self):
        actions = sweep([card(attempts=3, claim_deadline_at=500.0, next_action_at=500.0)],
                        now=1_000.0)
        kinds = [a["kind"] for a in actions]
        self.assertIn("exhausted", kinds)
        self.assertNotIn("alarm_unclaimable", kinds)

    def test_the_report_is_deterministic(self):
        rows = [card(id="task_b"), card(id="task_a", claim_deadline_at=None)]
        self.assertEqual([a["task_id"] for a in sweep(rows, now=1_000.0)],
                         [a["task_id"] for a in sweep(list(reversed(rows)), now=1_000.0)])

    def test_a_done_card_is_not_swept(self):
        self.assertEqual(sweep([card(column="done", claim_deadline_at=None,
                                     next_action_at=None)], now=1_000.0), [])

    def test_healthy_says_the_same_thing_as_the_list(self):
        self.assertTrue(healthy(sweep([card()], now=0.0)))
        self.assertFalse(healthy(sweep([card(claim_deadline_at=None)], now=0.0)))


class ApplyTests(unittest.TestCase):
    def test_reclaiming_puts_the_card_back_in_play_with_a_window(self):
        changes = apply(card(column="in_progress", lease_holder="agent_1"),
                        {"kind": "reclaim_expired", "next_attempts": 1}, now=1_000.0)
        self.assertEqual(changes["column_name"], "ready")
        self.assertEqual(changes["lease_holder"], None)
        self.assertEqual(changes["attempts"], 1)
        self.assertEqual(changes["claim_deadline_at"], 1_000.0 + 900.0)

    def test_reclaiming_past_the_cap_blocks_in_the_same_step(self):
        """a dead holder must not leave a row that is neither retryable nor visible."""
        changes = apply(card(column="in_progress", lease_holder="agent_1", attempts=2),
                        {"kind": "reclaim_expired", "next_attempts": 3}, now=1_000.0)
        self.assertEqual(changes["column_name"], "blocked")
        self.assertEqual(changes["blocked_reason"], "attempts_exhausted")

    def test_a_violation_writes_nothing(self):
        self.assertEqual(apply(card(), {"kind": "violation", "rule": "x"}), {})

    def test_an_alarm_only_re_arms_the_window(self):
        changes = apply(card(), {"kind": "alarm_unclaimable"}, now=1_000.0)
        self.assertEqual(changes, {"claim_deadline_at": 1_900.0, "next_action_at": 1_900.0})


class SweepServiceTests(AppCase):
    def test_a_crashed_holder_is_reclaimed_and_the_card_runs_again(self):
        team = self.crew()
        agent = self.agent(team["id"])
        card = self.task(team["id"], "job")
        self.app.custody.claim(card["id"], agent["id"], ttl=-1)
        self.app.kanban.move(card["id"], "in_progress", "orchestrator")
        result = self.app.liveness.run(team["id"])
        self.assertIn("reclaim_expired", [a["kind"] for a in result["actions"]])
        after = self.app.kanban.get_task(card["id"])
        self.assertEqual(after["column"], "ready")
        self.assertEqual(after["attempts"], 1)
        self.assertEqual(after["lease"]["holder"], "")

    def test_a_clean_board_is_healthy(self):
        team = self.crew()
        self.agent(team["id"])
        self.task(team["id"], "job")
        self.assertTrue(self.app.liveness.preview(team["id"])["healthy"])

    def test_the_preview_is_read_only(self):
        team = self.crew()
        agent = self.agent(team["id"])
        card = self.task(team["id"], "job")
        self.app.custody.claim(card["id"], agent["id"], ttl=-1)
        before = self.app.kanban.get_task(card["id"])
        self.app.liveness.preview(team["id"])
        self.assertEqual(self.app.kanban.get_task(card["id"])["lease"]["holder"],
                         before["lease"]["holder"])

    def test_a_column_change_from_the_sweep_is_audited_like_a_move(self):
        team = self.crew()
        agent = self.agent(team["id"])
        card = self.task(team["id"], "job")
        self.app.custody.claim(card["id"], agent["id"], ttl=-1)
        self.app.kanban.move(card["id"], "in_progress", "orchestrator")
        self.app.liveness.run(team["id"])
        kinds = [e["kind"] for e in self.app.kanban.events(card["id"])]
        self.assertIn("moved", kinds)


if __name__ == "__main__":
    unittest.main()
