"""custody: a card in someone's hands is a lease, not a status.

a dispatched card used to be a column plus a busy flag on the agent. if the worker
died mid-run, the card stayed in progress with nobody holding it, and the only
thing that could tell a working worker from a dead one was a person looking at the
board. that is a gap that grows with the number of cards nobody is watching.

so custody is a lease carried on the card. a holder that stops renewing becomes
visible by comparing the row to the clock, without any process remembering that it
existed. every function is pure over (task, now) and returns the columns to write
or raises a named refusal, because the runner has to handle "no" the same way it
handles "yes".

the second half is what a failed attempt means. "the holder died" and "the brief
was wrong" are different facts about the world and only one of them is worth
retrying. abandon spends an attempt, and replan resets the count because the
previous attempts were spent answering a different question. a brief that is
merely retried burns good holders and then a person.

ported from the reference implementation's custody and liveness modules, with one
deliberate difference: sqlite cannot add a table constraint to an existing table,
so the invariants live in work/liveness.py and in the refusals here instead of in
check constraints on the table. the sweep is what makes them enforceable every
tick, and a test names each one.
"""

from __future__ import annotations

from typing import Any

from core.db import now as clock
from core.errors import Conflict, NotFound, ValidationError

LEASE_SECONDS = 300.0
CLAIM_WINDOW_SECONDS = 900.0
MAX_ATTEMPTS = 3

LEASE_COLUMNS = ("lease_holder", "lease_expires", "lease_renewals")


class Custody:
    """who holds a card, for how long, and what a failed attempt costs."""

    def __init__(self, store: Any):
        self.store = store

    # ---- reading ---------------------------------------------------------
    def task(self, task_id: str) -> dict:
        row = self.store.one("select * from tasks where id = ?", (task_id,))
        if not row:
            raise NotFound(f"task {task_id} not found")
        task = dict(row)
        # the stored column is `column_name`; every rule here reads the board's word
        task["column"] = task.pop("column_name")
        return task

    def held_by(self, task_id: str) -> str:
        """the holder, or an empty string. an expired lease is not a holder: the
        card is free the moment the clock passes the expiry, which is what makes a
        dead worker recoverable without a reaper process."""
        task = self.task(task_id)
        if not task["lease_holder"]:
            return ""
        if (task["lease_expires"] or 0) <= clock():
            return ""
        return task["lease_holder"]

    # ---- the lease -------------------------------------------------------
    def claim(self, task_id: str, agent_id: str, ttl: float = LEASE_SECONDS) -> dict:
        task = self.task(task_id)
        if task["column"] not in {"ready", "in_progress"}:
            raise ValidationError(
                f"a card in '{task['column']}' is not claimable; move it to ready first",
                task_id=task_id)
        if int(task["attempts"] or 0) >= int(task["max_attempts"] or MAX_ATTEMPTS):
            raise Conflict("this card has spent its attempts", task_id=task_id,
                           reason="attempts_exhausted", attempts=task["attempts"])
        holder = self.held_by(task_id)
        if holder and holder != agent_id:
            raise Conflict(f"the card is held by {holder}", task_id=task_id,
                           reason="not_holder", holder=holder)
        if holder == agent_id:
            # the same holder asking twice is a replay, not a race: renew, do not reset
            return self.renew(task_id, agent_id, ttl=ttl)
        ts = clock()
        expires = ts + ttl
        self._write(task_id, {
            "lease_holder": agent_id,
            "lease_expires": expires,
            "lease_renewals": 0,
            "next_action_at": expires,
            "claim_deadline_at": None,
        })
        return {"task_id": task_id, "holder": agent_id, "expires": expires}

    def renew(self, task_id: str, agent_id: str, ttl: float = LEASE_SECONDS) -> dict:
        task = self.task(task_id)
        self._must_hold(task, agent_id)
        ts = clock()
        expires = ts + ttl
        renewals = int(task["lease_renewals"] or 0) + 1
        self._write(task_id, {
            "lease_expires": expires,
            "lease_renewals": renewals,
            "next_action_at": expires,
        })
        return {"task_id": task_id, "holder": agent_id, "expires": expires,
                "renewals": renewals}

    def complete(self, task_id: str, agent_id: str, evidence: str = "") -> dict:
        """give the card back finished. the lease is released here and the column is
        the board's business, because a review gate may sit between this and done."""
        task = self.task(task_id)
        self._must_hold(task, agent_id)
        self._write(task_id, {
            "lease_holder": None,
            "lease_expires": None,
            "lease_renewals": 0,
            "next_action_at": None,
            "claim_deadline_at": None,
            "evidence_ref": evidence or task["evidence_ref"],
        })
        return {"task_id": task_id, "released": True}

    def abandon(self, task_id: str, agent_id: str, reason: str,
                terminal: bool = False) -> dict:
        """give the card back without claiming success.

        terminal is for the case the sweep cannot see: the holder did its job and the
        brief was the problem. retrying a mis-specified instruction spends good
        holders on a question nobody asked, so it blocks the card at once and leaves
        replan as the way forward.
        """
        task = self.task(task_id)
        self._must_hold(task, agent_id)
        attempts = int(task["attempts"] or 0) + 1
        spent = bool(terminal) or attempts >= int(task["max_attempts"] or MAX_ATTEMPTS)
        ts = clock()
        self._write(task_id, {
            "lease_holder": None,
            "lease_expires": None,
            "lease_renewals": 0,
            "attempts": attempts,
            "blocked_reason": (reason or "")[:500] if spent else "",
            "next_action_at": None if spent else ts + CLAIM_WINDOW_SECONDS,
            "claim_deadline_at": None if spent else ts + CLAIM_WINDOW_SECONDS,
        })
        return {
            "task_id": task_id,
            "attempts": attempts,
            "column": "blocked" if spent else "ready",
            "reason": "attempts_exhausted" if spent else "",
        }

    def replan(self, task_id: str, brief: str, actor: str = "owner") -> dict:
        """rewrite the brief and give the card its attempts back.

        only whoever wrote the brief may do this: an agent that can replan itself can
        reset the counter that catches it spinning.
        """
        actor = (actor or "").strip()
        if actor not in {"owner", "system", "orchestrator"}:
            raise ValidationError(
                "a replan is the brief writer's move; use the owner or the platform",
                task_id=task_id, actor=actor)
        task = self.task(task_id)
        if self.held_by(task_id):
            raise Conflict("a held card cannot be replanned; the holder has it",
                           task_id=task_id, reason="held", holder=task["lease_holder"])
        brief = (brief or "").strip()
        if not brief:
            raise ValidationError("a replan needs a new brief", task_id=task_id)
        if brief == (task["description"] or "").strip():
            raise ValidationError("that is the same brief; nothing was replanned",
                                  task_id=task_id)
        ts = clock()
        self._write(task_id, {
            "description": brief,
            "attempts": 0,
            "blocked_reason": "",
            "lease_holder": None,
            "lease_expires": None,
            "lease_renewals": 0,
            "claim_deadline_at": ts + CLAIM_WINDOW_SECONDS,
            "next_action_at": ts + CLAIM_WINDOW_SECONDS,
        })
        return {"task_id": task_id, "attempts": 0, "brief": brief}

    def release(self, task_id: str) -> dict:
        """drop a lease without spending an attempt, for a run that was cancelled.
        the card keeps its attempts, because a release is not a retry."""
        self._write(task_id, {"lease_holder": None, "lease_expires": None,
                              "lease_renewals": 0, "next_action_at": None})
        return {"task_id": task_id, "released": True}

    # ---- internals -------------------------------------------------------
    def _must_hold(self, task: dict, agent_id: str) -> None:
        if not task["lease_holder"]:
            raise Conflict("that card is not leased to anyone", task_id=task["id"],
                           reason="not_leased")
        if task["lease_holder"] != agent_id:
            raise Conflict(f"the card is held by {task['lease_holder']}",
                           task_id=task["id"], reason="not_holder")
        if (task["lease_expires"] or 0) <= clock():
            raise Conflict("the lease expired; the sweep reclaims it, not the holder",
                           task_id=task["id"], reason="lease_expired")

    def _write(self, task_id: str, values: dict) -> None:
        if not values:
            return
        values = {"column_name" if name == "column" else name: value
                  for name, value in values.items()}
        columns = ", ".join(f"{name} = ?" for name in values)
        params = tuple(values.values()) + (clock(), task_id)
        with self.store.tx() as c:
            c.execute(f"update tasks set {columns}, updated_date = ? where id = ?", params)
