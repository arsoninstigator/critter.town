"""the sweep: what a card looks like when nobody is watching it.

a month of unattended work gathers four kinds of rot, and none of them announce
themselves:

  1. a holder that died, so the card is leased to nobody who exists.
  2. work nobody can take, because it has no deadline and therefore never looks late.
  3. a holder that renews forever without the work moving, which is the commonest
     real failure and the one no health check catches.
  4. a terminal state reached without a reason or without evidence.

the sweep is a pure function of the rows and the clock. it never fixes anything
itself: it returns a list of findings and transitions, and the service applies the
transitions. a finding that repaired itself would hide the bug that produced it.

deliberately not ported yet: the starvation counter. it needs a per tick deferral
budget to mean anything, and this platform does not defer work yet, so a counter
here would be a number that never moves.
"""

from __future__ import annotations

from typing import Any, Iterable

from core.db import now as clock
from work.custody import CLAIM_WINDOW_SECONDS, MAX_ATTEMPTS

LIVE_COLUMNS = {"backlog", "ready", "in_progress", "review"}
STUCK_RENEWAL_LIMIT = 3


def is_terminal(task: dict) -> bool:
    return task["column"] == "done"


def violations(task: dict) -> list[str]:
    """the invariants, as names rather than prose.

    only the columns that hold work are checked. a backlog card and a card in review
    are both waiting on a person, and demanding a deadline of either would fill the
    report with rows that are supposed to sit still.
    """
    found: list[str] = []
    working = task["column"] in {"ready", "in_progress"}
    if task["column"] == "in_progress" and not task["lease_holder"]:
        found.append("leased_without_lease")
    if task["column"] == "in_progress" and task["lease_holder"] and not task["lease_expires"]:
        found.append("leased_without_expiry")
    if working and not task["next_action_at"]:
        found.append("missing_next_action")
    if task["column"] == "ready" and not task["claim_deadline_at"]:
        found.append("ready_without_claim_deadline")
    return found


def sweep(tasks: Iterable[dict], now: float | None = None,
          stuck_renewal_limit: int = STUCK_RENEWAL_LIMIT) -> list[dict]:
    """deterministic order, so the same rows always produce the same report."""
    now = clock() if now is None else now
    actions: list[dict] = []
    for task in sorted(tasks, key=lambda t: t["id"]):
        for rule in violations(task):
            actions.append({"kind": "violation", "task_id": task["id"], "rule": rule})
        if is_terminal(task):
            continue
        attempts = int(task["attempts"] or 0)
        max_attempts = int(task["max_attempts"] or MAX_ATTEMPTS)
        if task["column"] != "blocked" and attempts >= max_attempts:
            actions.append({"kind": "exhausted", "task_id": task["id"],
                            "attempts": attempts})
            continue
        if task["lease_holder"]:
            expires = task["lease_expires"] or 0
            if now > expires:
                actions.append({"kind": "reclaim_expired", "task_id": task["id"],
                                "holder": task["lease_holder"],
                                "next_attempts": attempts + 1})
                continue
            # alive and holding, but not producing
            if int(task["lease_renewals"] or 0) >= stuck_renewal_limit:
                actions.append({"kind": "revoke_spinning", "task_id": task["id"],
                                "holder": task["lease_holder"],
                                "renewals": task["lease_renewals"]})
            continue
        if task["column"] == "ready" and task["claim_deadline_at"]:
            if now > task["claim_deadline_at"]:
                # not a state change: the card stays claimable, because a busy crew
                # should not lose work that a free one could still take
                actions.append({"kind": "alarm_unclaimable", "task_id": task["id"]})
    return actions


def healthy(actions: list[dict]) -> bool:
    return not actions


def apply(task: dict, action: dict, now: float | None = None,
          claim_window: float = CLAIM_WINDOW_SECONDS) -> dict:
    """the columns a transition writes, or an empty dict for a finding."""
    now = clock() if now is None else now
    deadline = now + claim_window
    kind = action["kind"]
    if kind in {"reclaim_expired", "revoke_spinning"}:
        attempts = (action.get("next_attempts") if kind == "reclaim_expired"
                    else int(task["attempts"] or 0) + 1)
        attempts = int(attempts or 0)
        if attempts >= int(task["max_attempts"] or MAX_ATTEMPTS):
            # reclaimed past the cap, so the card blocks in the same step: a dead
            # holder must not leave a row that is neither retryable nor visible
            return {"column_name": "blocked", "lease_holder": None, "lease_expires": None,
                    "lease_renewals": 0, "attempts": attempts,
                    "blocked_reason": "attempts_exhausted", "next_action_at": None,
                    "claim_deadline_at": None}
        return {"column_name": "ready", "lease_holder": None, "lease_expires": None,
                "lease_renewals": 0, "attempts": attempts,
                "claim_deadline_at": deadline, "next_action_at": deadline}
    if kind == "exhausted":
        return {"column_name": "blocked", "lease_holder": None, "lease_expires": None,
                "lease_renewals": 0, "blocked_reason": "attempts_exhausted",
                "next_action_at": None, "claim_deadline_at": None}
    if kind == "alarm_unclaimable":
        return {"claim_deadline_at": deadline, "next_action_at": deadline}
    return {}


class Liveness:
    """the sweep as a service: preview it, or run it and apply the transitions."""

    def __init__(self, store: Any, kanban: Any):
        self.store = store
        self.kanban = kanban

    def cards(self, team_id: str) -> list[dict]:
        """rows as the sweep wants them: one `column` key, because the stored name is
        `column_name` and every rule reads the board's word for it."""
        rows = self.store.query(
            "select * from tasks where team_id = ? and column_name != 'done'", (team_id,))
        cards = []
        for row in rows:
            task = dict(row)
            task["column"] = task.pop("column_name")
            cards.append(task)
        return cards

    def preview(self, team_id: str) -> dict:
        actions = sweep(self.cards(team_id))
        return {
            "actions": actions,
            "healthy": healthy(actions),
            "held": [{"task_id": t["id"], "holder": t["lease_holder"],
                      "expires": t["lease_expires"]}
                     for t in self.cards(team_id) if t["lease_holder"]],
        }

    def run(self, team_id: str) -> dict:
        actions = sweep(self.cards(team_id))
        applied: list[dict] = []
        for action in actions:
            row = dict(self.store.one("select * from tasks where id = ?",
                                      (action["task_id"],)))
            # rules read the board's word for the column, the row stores `column_name`
            row["column"] = row.pop("column_name")
            task = row
            changes = apply(task, action)
            if not changes:
                continue
            column = changes.pop("column_name", "")
            if changes:
                columns = ", ".join(f"{name} = ?" for name in changes)
                with self.store.tx() as c:
                    c.execute(
                        f"update tasks set {columns}, updated_date = ? where id = ?",
                        tuple(changes.values()) + (clock(), task["id"]),
                    )
            if column and column != task["column"]:
                # a column change is a board move, so it is audited and gated like one
                self.kanban.move(task["id"], column, "scheduler",
                                 note=f"sweep: {action['kind']}")
            applied.append({**action, "column": column or task["column"]})
        return {"actions": actions, "applied": applied, "healthy": healthy(actions)}
