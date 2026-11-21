"""self-direction is a dispatcher, not a loop that waits for tickets.

an org that only answers what it is handed is a help desk. this decides what gets worked on
next, and it refuses with a reason when the honest answer is "nothing". the refusals are the
design: `nothing_ready` is the common, quiet answer, and `kind_unregistered` exists because a
task whose kind has no evidence rule can never be marked done, so handing it out manufactures
work that cannot finish.

work is addressed by capability, not by name. a card names the slice it needs and any live
seat holding that slice may pick it up, which is why a seat being idle, paused or dead stops
the holder rather than the work.
"""

from __future__ import annotations

from core.db import Store, loads, now
from core.errors import NotAllowed, ValidationError
from work.evidence import KIND_EVIDENCE

LEASE_MS = 15 * 60 * 1000


def capability_of(task: dict) -> str:
    """what a card needs, from the card itself: its declared slice, else its kind, else its
    first required skill. empty means any live seat may take it."""
    payload = task.get("payload") or {}
    if payload.get("slice"):
        return str(payload["slice"])
    skills = task.get("required_skills") or payload.get("required_skills") or []
    if skills:
        return str(sorted(skills)[0])
    return ""


def kind_of(task: dict) -> str:
    payload = task.get("payload") or {}
    return str(payload.get("kind") or "")


def next_assignment(candidates: list[dict], seats: list[dict], leased: set[str],
                    registered: tuple[str, ...] | set[str] = tuple(KIND_EVIDENCE)) -> dict:
    """the pure decision. highest priority first, then the lowest id, so the order is stable
    and reviewable rather than arbitrary."""
    known = set(registered)
    ready = [task for task in candidates if task["id"] not in leased]
    if not ready:
        return {"ok": False, "why": "nothing_ready"}
    dispatchable = [task for task in ready if not kind_of(task) or kind_of(task) in known]
    if not dispatchable:
        return {"ok": False, "why": "kind_unregistered",
                "kinds": sorted({kind_of(task) for task in ready if kind_of(task)})}
    ordered = sorted(dispatchable, key=lambda task: (-int(task.get("priority") or 0), task["id"]))
    saw_seat = False
    for task in ordered:
        wanted = capability_of(task)
        pool = [seat for seat in seats
                if seat.get("state", "live") == "live" and seat.get("on_call", True)
                and (not wanted or wanted in (seat.get("skills") or [])
                     or wanted == seat.get("role"))]
        if not pool:
            continue
        saw_seat = True
        seat = sorted(pool, key=lambda s: s["id"])[0]
        return {"ok": True, "seat": seat["id"], "task_id": task["id"],
                "capability": wanted, "kind": kind_of(task), "lease_ms": LEASE_MS}
    return {"ok": False, "why": "no_seat_for_slice" if not saw_seat else "all_seats_leased"}


class DispatchService:
    def __init__(self, store: Store, kanban=None, custody=None, seats=None, crews=None):
        self.store = store
        self.kanban = kanban
        self.custody = custody
        self.seats = seats
        self.crews = crews

    def _candidates(self, team_id: str) -> list[dict]:
        rows = self.store.query(
            """select * from tasks where team_id = ? and column_name = 'ready'
               order by priority desc, id""", (team_id,))
        return [{"id": row["id"], "priority": int(row["priority"] or 0),
                 "payload": loads(row["payload"], {}) or {}} for row in rows]

    def _seats(self, team_id: str) -> list[dict]:
        rows = self.store.query(
            """select id, name, role, skills, seat_state from agents
               where team_id = ? and seat_state = 'live'""", (team_id,))
        return [{"id": row["id"], "role": row["role"], "skills": loads(row["skills"], []),
                 "state": row["seat_state"]} for row in rows]

    def next(self, team_id: str, claim: bool = True) -> dict:
        """decide, then hold the card so two ticks cannot hand out the same work."""
        leased = {row["id"] for row in self.store.query(
            "select id from tasks where team_id = ? and lease_holder != ''", (team_id,))}
        outcome = next_assignment(self._candidates(team_id), self._seats(team_id), leased)
        if not outcome["ok"]:
            return outcome
        if not claim:
            return outcome
        if self.custody is None:
            raise ValidationError("a dispatch that claims needs custody")
        # the seat has to be live at the moment of the claim, not a moment earlier
        if self.seats is not None:
            allowed = self.seats.check(outcome["seat"])
            if not allowed["allowed"]:
                raise NotAllowed(f"seat is {allowed['reason']}")
        self.custody.claim(outcome["task_id"], outcome["seat"], ttl=LEASE_MS / 1000.0)
        return {**outcome, "claimed": True, "at": now()}
