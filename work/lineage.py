"""a card whose parent never landed is not a card that failed.

band's dark factory charges a stage that did not start to every stage above it, so a broken
first step is one failure rather than five. critter.town had the other shape: a parent that
blocks leaves its children sitting in `ready`, each one gets picked up, each one burns an
attempt against a premise that does not exist yet, and the crew's board fills with cards that
look like independent failures.

so the parent chain is read before a card becomes work. a blocked ancestor is inherited: the
child parks in `blocked` naming the ancestor, spends no attempt and takes no lease. an
ancestor waiting on a person defers the child, because the premise is not wrong, it is
unsettled. everything else runs.

the one exception is a lane of a fan-out. a barrier's children are the parent's work rather
than cards waiting on it, so gating them on the parent's column would deadlock every fan-out
that has ever been opened.

the chain is walked with a limit and a visited set. a card cannot be its own ancestor, and a
row that says otherwise is a bug worth naming rather than a hang.
"""

from __future__ import annotations

from typing import Any

from core.errors import ValidationError

ANCESTOR_LIMIT = 12


def chain(task: dict, kanban: Any, limit: int = ANCESTOR_LIMIT) -> list[dict]:
    """the parent chain, nearest first. stops at the first missing or repeated parent.

    a repeated parent is reported by `cycle` rather than raised here: reading a lineage is
    a diagnostic, and a diagnostic that refuses to answer is not one.
    """
    found: list[dict] = []
    seen = {task.get("id")}
    parent_id = task.get("parent_id")
    while parent_id and len(found) < limit:
        if parent_id in seen:
            break
        seen.add(parent_id)
        parent = kanban.get_task(parent_id) if hasattr(kanban, "get_task") else None
        if parent is None:
            break
        found.append(parent)
        parent_id = parent.get("parent_id")
    return found


def cycle(task: dict, kanban: Any, limit: int = ANCESTOR_LIMIT) -> bool:
    """true when the chain revisits a card, which no correct row ever does."""
    seen = {task.get("id")}
    parent_id = task.get("parent_id")
    steps = 0
    while parent_id and steps < limit:
        if parent_id in seen:
            return True
        seen.add(parent_id)
        parent = kanban.get_task(parent_id) if hasattr(kanban, "get_task") else None
        if parent is None:
            return False
        parent_id = parent.get("parent_id")
        steps += 1
    return False


def decide(parents: list[dict]) -> dict:
    """the table, first match wins, nearest ancestor first.

    the immediate parent is what gates a child: if it is waiting on a person, the child
    waits too even when a grandparent is blocked, because the nearer answer is the one that
    will change first. backlog is deliberately not a reason, since a child queued ahead of
    an unscheduled parent is a scheduling mistake and parking the child would hide it.
    """
    for parent in parents:
        column = parent.get("column")
        if column == "blocked":
            return {
                "decision": "inherit_blocked",
                "reason": "ancestor_blocked",
                "ancestor": {
                    "id": parent["id"], "title": parent.get("title") or "",
                    "column": "blocked",
                    "blocked_reason": parent.get("blocked_reason") or "",
                },
            }
        if column == "review":
            return {
                "decision": "defer",
                "reason": "ancestor_in_review",
                "ancestor": {
                    "id": parent["id"], "title": parent.get("title") or "",
                    "column": "review", "blocked_reason": "",
                },
            }
    return {"decision": "run", "reason": "", "ancestor": None}


class Lineage:
    """the chain, and the crew-level view of what has been inherited."""

    def __init__(self, store: Any, kanban: Any, convergence: Any = None):
        self.store = store
        self.kanban = kanban
        # a lane of a fan-out is the parent's work, not a card waiting on it, so the
        # barrier's own children are the one exception to inheritance
        self.convergence = convergence

    def is_lane(self, task: dict) -> bool:
        if self.convergence is None or not task.get("parent_id"):
            return False
        try:
            plan = self.convergence.for_task(task["parent_id"], status="")
        except Exception:
            return False
        if not plan:
            return False
        return task["id"] in (plan.get("partitions") or {})

    def of(self, task: dict) -> dict:
        parents = chain(task, self.kanban)
        verdict = decide(parents)
        if verdict["decision"] != "run" and self.is_lane(task):
            # the parent is waiting on this card, so its column is not a gate on it
            verdict = {"decision": "run", "reason": "", "ancestor": None}
        return {
            "task_id": task["id"],
            "chain": [
                {"id": row["id"], "title": row.get("title") or "",
                 "column": row.get("column") or "",
                 "blocked_reason": row.get("blocked_reason") or ""}
                for row in parents
            ],
            "cycle": cycle(task, self.kanban),
            **verdict,
        }

    def for_dispatch(self, task: dict) -> dict:
        return self.of(task)

    def inherited(self, team_id: str, limit: int = 200) -> dict:
        """cards in the crew that are held back by an ancestor, as one report.

        this is the read a person needs after a card blocks: not the card, the subtree.
        """
        rows = self.store.query(
            """select * from tasks where team_id = ?
               and column_name in ('ready','in_progress','backlog')
               order by created_date limit ?""", (team_id, limit))
        blocked: list[dict] = []
        deferred: list[dict] = []
        for row in rows:
            task = dict(row)
            verdict = self.of(task)
            if verdict["decision"] == "inherit_blocked":
                blocked.append({"task_id": task["id"], "title": task.get("title") or "",
                                "column": task.get("column") or "",
                                "ancestor": verdict["ancestor"]})
            elif verdict["decision"] == "defer":
                deferred.append({"task_id": task["id"], "title": task.get("title") or "",
                                 "column": task.get("column") or "",
                                 "ancestor": verdict["ancestor"]})
        return {"blocked": blocked, "deferred": deferred,
                "checked": len(rows)}


def park(task: dict, verdict: dict) -> str:
    """the note a child carries when it inherits a failure."""
    ancestor = verdict.get("ancestor") or {}
    reason = ancestor.get("blocked_reason") or ""
    note = f"inherited: {ancestor.get('title') or ancestor.get('id')} is blocked"
    if reason:
        note += f" ({reason})"
    return note[:200]


def refuse(task: dict, verdict: dict) -> ValidationError:
    ancestor = verdict.get("ancestor") or {}
    return ValidationError(
        f"'{ancestor.get('title') or ancestor.get('id')}' is blocked; a child of a card that "
        "never landed cannot be judged on its own",
        task_id=task["id"], ancestor=ancestor, reason=verdict["reason"])
