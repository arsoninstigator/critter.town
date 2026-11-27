"""a wake is a reason for a card to be worked, and losing one is the bug.

the case that matters is the second wake: another agent is already running the card, and
before this the answer was `Conflict: not_holder`, so the reason to work was gone and had to
be rediscovered by a person. here a wake either runs the card, joins the run already on it,
waits behind a person, or is refused with a reason on the record.

the decisions are a pure function of the card row, so the table is argued with in the first
class and only written down in the second.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from base import AppCase  # noqa: E402
from core.db import new_id, now  # noqa: E402
from core.errors import NotFound  # noqa: E402
from work import wake as wake_module  # noqa: E402


def card(column: str = "ready", **extra) -> dict:
    return {"id": "task_x", "column": column, **extra}


class DecideTests(unittest.TestCase):
    def test_a_free_card_runs(self):
        self.assertEqual(wake_module.decide(card())["decision"], "run")

    def test_a_card_in_review_waits_for_the_person(self):
        decided = wake_module.decide(card("review"))
        self.assertEqual(decided["decision"], "defer")
        self.assertEqual(decided["reason"], "awaiting_review")

    def test_a_closed_card_is_refused_with_its_column_named(self):
        self.assertEqual(wake_module.decide(card("done"))["reason"], "card_done")
        self.assertEqual(wake_module.decide(card("blocked"))["reason"], "card_blocked")

    def test_a_backlog_card_is_not_woken_by_a_trigger(self):
        self.assertEqual(wake_module.decide(card("backlog"))["reason"], "not_ready")

    def test_an_exhausted_card_is_refused(self):
        decided = wake_module.decide(card(), attempts=3, max_attempts=3)
        self.assertEqual(decided["reason"], "attempts_exhausted")

    def test_a_card_another_agent_holds_is_joined_not_refused(self):
        # this is the line that used to be a conflict and a lost wake
        decided = wake_module.decide(card("in_progress"), holder="agent_a", agent_id="agent_b")
        self.assertEqual(decided["decision"], "coalesce")
        self.assertEqual(decided["reason"], "held_by_another")

    def test_the_holder_asking_again_is_a_replay(self):
        decided = wake_module.decide(card("in_progress"), holder="agent_a", agent_id="agent_a")
        self.assertEqual(decided["decision"], "coalesce")
        self.assertEqual(decided["reason"], "replay")

    def test_a_coalesced_wake_names_the_run_it_joined(self):
        decided = wake_module.decide(card("in_progress"), holder="agent_a", live_run="run_1")
        self.assertEqual(decided["run_id"], "run_1")

    def test_an_expired_holder_is_not_a_holder(self):
        # custody already treats an expired lease as free, so a wake must not defer behind
        # a worker that is gone
        decided = wake_module.decide(card("in_progress"))
        self.assertEqual(decided["decision"], "run")


class QueueTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("studio")
        self.card = self.app.kanban.create_task(self.team["id"], "cancel a booking",
                                               description="nothing specific", column="ready")

    def running_run(self, agent_id: str) -> str:
        run_id = new_id("run")
        with self.app.store.tx() as c:
            c.execute(
                """insert into runs (id, task_id, agent_id, provider, status, steps,
                       started_at, created_date) values (?,?,?,?, 'running', '[]', ?, ?)""",
                (run_id, self.card["id"], agent_id, "fake", now(), now()),
            )
        return run_id

    def test_raising_twice_reuses_the_pending_wake(self):
        first = self.app.wakes.admit(self.card, agent_id="agent_a")
        second = self.app.wakes.admit(self.card, agent_id="agent_a")
        self.assertEqual(first["wake_id"], second["wake_id"])
        self.assertEqual(len(self.app.wakes.for_task(self.card["id"])), 1)

    def test_a_wake_while_held_coalesces_and_names_the_holder(self):
        seat = self.app.crews.add_agent(self.team["id"], "wren", role="doer")
        self.app.custody.claim(self.card["id"], seat["id"])
        run_id = self.running_run(seat["id"])
        wake = self.app.wakes.admit(self.card, agent_id="agent_other")
        self.assertEqual(wake["decision"], "coalesce")
        self.assertEqual(wake["holder"], seat["id"])
        self.assertEqual(wake["run_id"], run_id)

    def test_the_wake_survives_the_run_that_absorbed_it(self):
        seat = self.app.crews.add_agent(self.team["id"], "wren", role="doer")
        self.app.custody.claim(self.card["id"], seat["id"])
        run_id = self.running_run(seat["id"])
        self.app.wakes.admit(self.card, agent_id="agent_other")
        revived = self.app.wakes.settle(run_id, outcome="succeeded")
        self.assertEqual(len(revived), 1)
        self.assertEqual(revived[0]["decision"], "run")
        self.assertEqual(revived[0]["reason"], "run_ended")

    def test_a_delivered_wake_leaves_the_open_list(self):
        wake = self.app.wakes.admit(self.card, agent_id="agent_a")
        self.app.wakes.delivered(wake["wake_id"], "run_9")
        self.assertEqual(self.app.wakes.open(self.team["id"]), [])

    def test_a_deferred_wake_comes_back_when_the_card_leaves_review(self):
        waiting = self.app.kanban.create_task(self.team["id"], "decide on the booking",
                                             description="a person owes this", column="review")
        wake = self.app.wakes.admit(waiting, agent_id="agent_a")
        self.assertEqual(wake["decision"], "defer")
        self.assertEqual(self.app.wakes.revive(self.team["id"]), [])
        self.app.kanban.move(waiting["id"], "in_progress", "owner")
        self.app.kanban.move(waiting["id"], "ready", "owner")
        revived = self.app.wakes.revive(self.team["id"])
        self.assertEqual([row["decision"] for row in revived], ["run"])
        self.assertEqual(revived[0]["reason"], "deferred_reason_cleared")

    def test_a_wake_does_not_outlive_its_card(self):
        wake = self.app.wakes.admit(self.card, agent_id="agent_a")
        with self.app.store.tx() as c:
            c.execute("delete from tasks where id = ?", (self.card["id"],))
        with self.assertRaises(NotFound):
            self.app.wakes.get(wake["wake_id"])


class DispatchTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("studio")
        self.card = self.app.kanban.create_task(self.team["id"], "cancel a booking",
                                               description="nothing specific", column="ready")
        self.holder = self.app.crews.add_agent(self.team["id"], "holder", role="doer")
        self.other = self.app.crews.add_agent(self.team["id"], "other", role="doer")

    def test_a_dispatch_on_a_held_card_returns_a_decision_instead_of_raising(self):
        # the bug: this used to raise Conflict not_holder and the wake was gone
        self.app.kanban.assign(self.card["id"], self.other["id"])
        self.app.custody.claim(self.card["id"], self.holder["id"])
        out = self.app.runner.dispatch(self.card["id"])
        self.assertEqual(out["decision"], "coalesce")
        self.assertEqual(out["reason"], "held_by_another")
        self.assertEqual(out["run_id"], "")
        self.assertEqual(len(self.app.wakes.for_task(self.card["id"])), 1)

    def test_the_held_card_still_runs_once_the_holder_lets_go(self):
        self.app.kanban.assign(self.card["id"], self.other["id"])
        self.app.custody.claim(self.card["id"], self.holder["id"])
        self.assertEqual(self.app.runner.dispatch(self.card["id"])["decision"], "coalesce")
        self.app.custody.release(self.card["id"])
        out = self.app.runner.dispatch(self.card["id"])
        self.assertTrue(out["run_id"])
        self.assertEqual(out["decision"] if "decision" in out else "run", "run")

    def test_a_free_card_dispatches_and_the_wake_is_delivered(self):
        out = self.app.runner.dispatch(self.card["id"])
        self.assertTrue(out["run_id"])
        wakes = self.app.wakes.for_task(self.card["id"])
        self.assertEqual([w["reason"] for w in wakes], ["delivered"])
        self.assertEqual(wakes[0]["run_id"], out["run_id"])

    def test_a_wake_raised_over_the_api_reports_its_decision(self):
        from fastapi.testclient import TestClient
        from api.routes import create_api
        client = TestClient(create_api(self.app))
        self.app.custody.claim(self.card["id"], self.holder["id"])
        response = client.post(f"/crews/{self.team['id']}/wakes",
                               json={"task_id": self.card["id"], "source": "trigger"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["decision"], "coalesce")
        listed = client.get(f"/crews/{self.team['id']}/wakes").json()["wakes"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["source"], "trigger")
