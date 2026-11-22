"""the door a foreign agent knocks on.

an external worker is hired by one call: it beats, and the answer is either a card it now
holds or a reason there is nothing for it. the card comes back with the lease, the evidence
scheme the card will be closed with, and the capability it was addressed by, because a worker
that has to guess what "done" means will guess.

two refusals are the design. `not_external` keeps the route from becoming a second way to
claim work for a local agent, since a local agent is dispatched by the tick and its custody is
the runner's. and a frozen or burned seat is refused before anything is looked at, because a
worker that is not allowed to work should not be told what the work is.
"""

from __future__ import annotations

import hmac

from core.db import Store, loads, now
from core.errors import NotAllowed, NotFound, ValidationError

from agents import external
from work.dispatch import capability_of, kind_of
from work.evidence import KIND_EVIDENCE

LEASE_SECONDS = 15 * 60.0


def eligible(tasks: list[dict], agent: dict, leased: set[str],
             registered: set[str] | tuple[str, ...] = tuple(KIND_EVIDENCE)) -> list[dict]:
    """the cards this worker could be handed, highest priority first.

    a card is addressed by capability: its declared slice, else a required skill. a worker
    holds it if it can serve that capability, which is the same rule dispatch applies to a
    seat, applied to one worker instead of a pool.
    """
    known = set(registered)
    skills = [str(s) for s in (agent.get("skills") or [])]
    role = str(agent.get("role") or "")
    out = []
    for task in tasks:
        if task["id"] in leased:
            continue
        kind = kind_of(task)
        if kind and kind not in known:
            # a card whose kind has no evidence rule can never be closed, so handing it
            # out manufactures work that cannot finish
            continue
        wanted = capability_of(task)
        if wanted and wanted not in skills and wanted != role:
            continue
        out.append(task)
    return sorted(out, key=lambda task: (-int(task.get("priority") or 0), task["id"]))


class HeartbeatService:
    def __init__(self, store: Store, crews=None, custody=None, strikes=None):
        self.store = store
        self.crews = crews
        self.custody = custody
        self.strikes = strikes

    # ---- reads ---------------------------------------------------------
    def agent(self, agent_id: str) -> dict:
        row = self.store.one("select * from agents where id = ?", (agent_id,))
        if not row:
            raise NotFound(f"agent {agent_id} not found")
        return {"id": row["id"], "team_id": row["team_id"], "name": row["name"],
                "role": row["role"], "kind": row["kind"], "skills": loads(row["skills"], []),
                "hosting": row["hosting"] if "hosting" in row.keys() else "local",
                "heartbeat_at": (row["heartbeat_at"] if "heartbeat_at" in row.keys() else 0) or 0,
                "seat_state": row["seat_state"] if "seat_state" in row.keys() else "live",
                "agent_key": row["agent_key"] if "agent_key" in row.keys() else ""}

    def state(self, agent_id: str) -> dict:
        agent = self.agent(agent_id)
        described = external.describe(agent)
        held = self.store.one(
            "select id, title from tasks where lease_holder = ? and lease_expires > ? limit 1",
            (agent_id, now()))
        return {**described, "name": agent["name"], "role": agent["role"],
                "kind": agent["kind"], "seat_state": agent["seat_state"],
                "last_heartbeat": agent["heartbeat_at"],
                "holding": ({"id": held["id"], "title": held["title"]} if held else None)}

    # ---- the beat ------------------------------------------------------
    def beat(self, agent_id: str, key: str = "", ttl: float = LEASE_SECONDS) -> dict:
        agent = self.agent(agent_id)
        if not external.is_external(agent):
            raise NotAllowed("this agent is run by the platform; it is dispatched, not polled",
                             agent_id=agent_id, why="not_external")
        if not key or not hmac.compare_digest(str(key), str(agent["agent_key"] or "")):
            raise NotAllowed("a heartbeat carries the agent's own key", agent_id=agent_id,
                             why="key_mismatch")
        if self.strikes is not None:
            standing = self.strikes.check(agent_id)
            if not standing["allowed"]:
                raise NotAllowed(f"the seat is {standing['standing']['state']}",
                                 agent_id=agent_id, why="seat_not_live",
                                 standing=standing["standing"])
        ts = now()
        with self.store.tx() as c:
            c.execute("update agents set heartbeat_at = ?, updated_date = ? where id = ?",
                      (ts, ts, agent_id))
        held = self.store.one(
            "select * from tasks where lease_holder = ? and lease_expires > ? limit 1",
            (agent_id, ts))
        if held:
            # beating again while holding is a renew, not a second card: a worker that has
            # not reported on the first one is not ready for the next
            return {"ok": True, "why": "already_holding", "task": self._card(held, agent),
                    "lease": {"task_id": held["id"], "holder": agent_id,
                              "expires": held["lease_expires"]}}
        tasks = [{"id": row["id"], "priority": int(row["priority"] or 0),
                  "payload": loads(row["payload"], {}) or {}}
                 for row in self.store.query(
                     """select id, priority, payload from tasks
                        where team_id = ? and column_name = 'ready'
                        order by priority desc, id""", (agent["team_id"],))]
        leased = {row["id"] for row in self.store.query(
            "select id from tasks where team_id = ? and lease_holder != ''", (agent["team_id"],))}
        candidates = eligible(tasks, agent, leased)
        if not candidates:
            return {"ok": False, "why": "nothing_ready", "at": ts}
        task = candidates[0]
        if self.custody is None:
            raise ValidationError("a heartbeat that claims needs custody")
        lease = self.custody.claim(task["id"], agent_id, ttl=ttl)
        row = self.store.one("select * from tasks where id = ?", (task["id"],))
        return {"ok": True, "why": "", "task": self._card(row, agent), "lease": lease, "at": ts}

    # ---- the card as the worker sees it ---------------------------------
    def _card(self, row, agent: dict) -> dict:
        payload = loads(row["payload"], {}) or {}
        kind = str(payload.get("kind") or "")
        return {
            "id": row["id"],
            "title": row["title"],
            "column": row["column_name"],
            "priority": int(row["priority"] or 0),
            "payload": payload,
            "capability": capability_of({"payload": payload}),
            "kind": kind,
            # the scheme the card will be closed with, so "done" is not a guess
            "evidence": list(KIND_EVIDENCE.get(kind, ())) if kind else [],
            "lease_expires": row["lease_expires"] if "lease_expires" in row.keys() else None,
            "attempts": int(row["attempts"] or 0) if "attempts" in row.keys() else 0,
            "worker": {"id": agent["id"], "name": agent["name"], "role": agent["role"]},
        }
