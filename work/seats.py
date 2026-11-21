"""a seat has four verbs and no "stop".

stop conflates three situations that need different audits: paused, done and dead. so a seat
is frozen (reversible, keeps its history), thawed (re-validated, not resumed) or burned
(terminal, tombstoned, and never reissued). duplication is the fourth verb, and it narrows:
a child that could do something its parent cannot is an escalation, and escalation is a
human act, never a spawn parameter.

the freeze carries a window because state expires. a seat frozen for a fortnight is
archaeology: the tools moved, the objective may be superseded, and its lease belongs to
somebody else. thawing that is a lie, so the refusal says to spawn against the current
ledger instead. that window is also the honest answer to firing after idleness: an idle seat
is frozen (cheap, reversible), and a seat left frozen past its window is retired by the same
sweep, with the reason naming the window.
"""

from __future__ import annotations

import time

from core.db import Store, dumps, loads, new_id, now
from core.errors import NotFound, ValidationError

MAX_DEPTH = 3
FROZEN_TTL_MS = 14 * 86_400_000
IDLE_FREEZE_DAYS = 0  # off unless a crew declares it


def can_work(seat: dict) -> bool:
    return seat.get("state") == "live"


def _refuse(reason: str, **detail) -> dict:
    return {"ok": False, "reason": reason, "detail": detail}


def duplicate(parent: dict, child: dict, now_ts: float | None = None) -> dict:
    """a child, not a copy: same lineage, strictly narrower, and not from an orphan."""
    if parent.get("orphaned"):
        return _refuse("orphaned_seat_may_not_spawn")
    if not can_work(parent):
        return _refuse("seat_is_burned" if parent.get("state") == "burned" else "seat_is_frozen")
    depth = int(parent.get("depth") or 0) + 1
    if depth > MAX_DEPTH:
        return _refuse("lineage_too_deep", depth=depth, ceiling=MAX_DEPTH)
    parent_skills = set(parent.get("skills") or [])
    child_skills = set(child.get("skills") or parent_skills)
    widened = child_skills - parent_skills
    if widened:
        return _refuse("skill_not_granted", skills=sorted(widened))
    return {"ok": True, "seat": {
        "id": child["id"], "team_id": parent["team_id"], "name": child.get("name", ""),
        "role": child.get("role", parent.get("role", "")), "skills": sorted(child_skills),
        "state": "live", "frozen_until": None, "burned_reason": "", "orphaned": False,
        "lineage": parent.get("lineage") or parent["id"], "depth": depth,
        "parent_id": parent["id"], "created_date": now_ts or now(),
    }}


def freeze(seat: dict, at: float | None = None, ttl_ms: int = FROZEN_TTL_MS, reason: str = "") -> dict:
    """stop working, keep everything worth keeping, release the responsibility."""
    at = now() if at is None else at
    if seat.get("state") == "burned":
        return _refuse("seat_is_burned")
    if seat.get("state") == "frozen":
        return _refuse("already_frozen")
    return {"ok": True, "seat": {**seat, "state": "frozen", "frozen_until": at + ttl_ms / 1000.0,
                                 "frozen_reason": reason}}


def thaw(seat: dict, at: float | None = None) -> dict:
    """resume only if the freeze is still a freeze. older state is archaeology."""
    at = now() if at is None else at
    if seat.get("state") == "burned":
        return _refuse("seat_is_burned")
    if seat.get("state") != "frozen":
        return _refuse("not_frozen")
    until = seat.get("frozen_until")
    if until and at > float(until):
        return _refuse("frozen_window_expired", frozen_until=until,
                       hint="spawn a fresh seat against the current ledger")
    return {"ok": True, "seat": {**seat, "state": "live", "frozen_until": None}}


def burn(seat: dict, reason: str, at: float | None = None) -> dict:
    """terminal, and the only verb that cannot be undone. it needs a reason."""
    at = now() if at is None else at
    if seat.get("state") == "burned":
        return _refuse("seat_is_burned")
    if len((reason or "").strip()) < 3:
        return _refuse("burn_needs_a_reason")
    return {"ok": True, "seat": {**seat, "state": "burned", "frozen_until": None,
                                 "burned_reason": reason.strip(), "burned_at": at}}


def orphan(seat: dict) -> dict:
    """burning a parent orphans its live children: they finish, they do not multiply."""
    return {**seat, "orphaned": True} if seat.get("state") == "live" else seat


def expired(seat: dict, at: float | None = None) -> bool:
    at = now() if at is None else at
    until = seat.get("frozen_until")
    return seat.get("state") == "frozen" and bool(until) and at > float(until)


class SeatService:
    def __init__(self, store: Store, crews=None, kanban=None, audit=None, strikes=None):
        self.store = store
        self.crews = crews
        self.kanban = kanban
        self.audit = audit
        self.strikes = strikes

    def _shape(self, row) -> dict:
        return {"id": row["id"], "team_id": row["team_id"], "name": row["name"],
                "role": row["role"], "skills": loads(row["skills"], []),
                "state": row["seat_state"], "frozen_until": row["frozen_until"],
                "burned_reason": row["burned_reason"], "orphaned": bool(row["orphaned"]),
                "lineage": row["lineage"] or row["id"], "depth": int(row["depth"] or 0),
                "parent_id": row["parent_id"], "created_date": row["created_date"]}

    def get(self, agent_id: str) -> dict:
        row = self.store.one("select * from agents where id = ?", (agent_id,))
        if not row:
            raise NotFound(f"no seat {agent_id}")
        return self._shape(row)

    def _write(self, seat: dict) -> dict:
        with self.store.tx() as c:
            c.execute(
                """update agents set seat_state = ?, frozen_until = ?, burned_reason = ?,
                   orphaned = ?, lineage = ?, depth = ?, updated_date = ? where id = ?""",
                (seat["state"], seat.get("frozen_until"), seat.get("burned_reason", ""),
                 1 if seat.get("orphaned") else 0, seat["lineage"], seat["depth"], now(), seat["id"]))
        return self.get(seat["id"])

    def _release(self, agent_id: str) -> dict:
        """a frozen seat holding a lease is an outage nobody has been told about yet."""
        released = 0
        with self.store.tx() as c:
            rows = c.execute(
                "select id from tasks where lease_holder = ?", (agent_id,)).fetchall()
            for row in rows:
                c.execute(
                    """update tasks set lease_holder = '', lease_expires = null,
                       column_name = case when column_name = 'in_progress' then 'ready' else column_name end,
                       updated_date = ? where id = ?""", (now(), row["id"]))
                released += 1
            c.execute(
                """update delegations set state = 'undelivered', updated_date = ?
                   where to_agent_id = ? and state in ('offered','accepted')""",
                (now(), agent_id))
        return {"leases_released": released}

    def freeze(self, agent_id: str, reason: str = "", ttl_days: float | None = None) -> dict:
        seat = self.get(agent_id)
        outcome = freeze(seat, ttl_ms=int((ttl_days or FROZEN_TTL_MS / 86_400) * 86_400_000),
                         reason=reason)
        if not outcome["ok"]:
            raise ValidationError(outcome["reason"], **outcome.get("detail", {}))
        written = self._write(outcome["seat"])
        return {**written, "released": self._release(agent_id)}

    def thaw(self, agent_id: str) -> dict:
        seat = self.get(agent_id)
        outcome = thaw(seat)
        if not outcome["ok"]:
            raise ValidationError(outcome["reason"], **outcome.get("detail", {}))
        return self._write(outcome["seat"])

    def burn(self, agent_id: str, reason: str) -> dict:
        seat = self.get(agent_id)
        outcome = burn(seat, reason)
        if not outcome["ok"]:
            raise ValidationError(outcome["reason"], **outcome.get("detail", {}))
        written = self._write(outcome["seat"])
        released = self._release(agent_id)
        # the children finish what they hold and never multiply again
        children = self.store.query("select id from agents where parent_id = ?", (agent_id,))
        for child in children:
            self._write(orphan(self.get(child["id"])))
        return {**written, "released": released, "orphaned": len(children)}

    def duplicate(self, parent_id: str, name: str, role: str = "", skills: list[str] | None = None) -> dict:
        parent = self.get(parent_id)
        outcome = duplicate(parent, {"id": new_id("agent"), "name": name,
                                     "role": role or parent["role"], "skills": skills})
        if not outcome["ok"]:
            raise ValidationError(outcome["reason"], **outcome.get("detail", {}))
        child = outcome["seat"]
        ts = now()
        with self.store.tx() as c:
            c.execute(
                """insert into agents (id, team_id, name, role, model, system_prompt, skills, tools,
                   runtime, status, seat_state, frozen_until, burned_reason, orphaned, lineage,
                   depth, parent_id, created_date, updated_date)
                   values (?,?,?,?,'','',?,'[]','{}','idle','live',null,'',0,?,?,?,?,?)""",
                (child["id"], child["team_id"], name, child["role"], dumps(child["skills"]),
                 child["lineage"], child["depth"], parent_id, ts, ts))
        return self.get(child["id"])

    def check(self, agent_id: str) -> dict:
        """the door dispatch reads: live, or the reason it is not."""
        seat = self.get(agent_id)
        if seat["state"] != "live":
            return {"allowed": False, "seat": seat, "reason": seat["state"]}
        return {"allowed": True, "seat": seat}

    # ---- idleness ------------------------------------------------------
    def last_activity(self, agent_id: str) -> float:
        row = self.store.one(
            """select max(e.created_date) as at from task_events e
               join tasks t on t.id = e.task_id where t.assignee_agent_id = ?""", (agent_id,))
        return float((row["at"] if row is not None else 0) or 0.0)

    def sweep_idle(self, team_id: str, at: float | None = None) -> dict:
        """freeze what has gone quiet, retire what stayed frozen past its window.

        the window is the whole point: freezing is reversible and cheap, so it is the right
        answer to a seat that has had nothing for a while. a seat left frozen longer than the
        window is state nobody can honestly resume, so it is burned with a reason that names
        the window rather than quietly thawed.
        """
        at = now() if at is None else at
        team = self.store.one("select * from teams where id = ?", (team_id,))
        if not team:
            raise NotFound(f"no crew {team_id}")
        policy = loads(team["orchestration"], {}) or {}
        idle_days = float(team["idle_freeze_days"] or policy.get("idle_freeze_days") or IDLE_FREEZE_DAYS)
        frozen, retired = [], []
        for agent in self.store.query("select id from agents where team_id = ?", (team_id,)):
            seat = self.get(agent["id"])
            if seat["state"] == "frozen" and expired(seat, at):
                try:
                    self.burn(seat["id"], f"frozen past its {int(FROZEN_TTL_MS / 86_400_000)} day window")
                    retired.append(seat["id"])
                except ValidationError:
                    pass
                continue
            if seat["state"] != "live" or idle_days <= 0:
                continue
            last = self.last_activity(seat["id"])
            if last and at - last >= idle_days * 86_400:
                try:
                    self.freeze(seat["id"], f"idle for {int((at - last) / 86_400)} days",
                                ttl_days=FROZEN_TTL_MS / 86_400)
                    frozen.append(seat["id"])
                except ValidationError:
                    pass
        return {"frozen": frozen, "retired": retired, "idle_freeze_days": idle_days}
