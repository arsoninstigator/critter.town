"""where a card's work went, and what a rejection actually changed on it.

volume is the number that hides everything. a crew that moves forty cards and a crew that moves
eight can look identical on a board, and a review that says "not yet" three times looks like
diligence until you ask what changed between the second and third round. the dark-factory
submissions that measure this measure two things per card: the distribution of the work across
the people who did it, and the difference a rejection made.

both are answerable from rows already written, with one addition. the board records which fields
an edit touched, but not whether the text moved, so a field rewritten to the value it already
had counts as a change and a rubber stamp is indistinguishable from a fix. `edit` now records a
digest per field, and this module compares digests rather than names. a field named in an older
edit with no digest is reported as assumed, not as changed, because a guess dressed as evidence
is the failure this whole layer exists to catch.

three refusals are deliberate:

- spend is attributed only where a reservation names an agent, and the rest is reported as
  unattributed rather than split evenly, since an even split invents a fact about who did it.
- a rejection still open is reported open, not counted as silent. it has not finished failing.
- a round is only judged certain when every edit in it carried digests. a mixed round says so.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from core.db import Store, loads, now
from core.errors import NotFound

SECONDS_PER_DAY = 86400
DEFAULT_WINDOW_DAYS = 7

# the fields a person writes, which are the fields a rejection is about. the column, the lease
# and the evidence are moved by the board, so a change to them is a move and not an edit.
EDITABLE = ("title", "description", "payload", "required_skills", "priority")

# a crew where one person holds this much of a card's work is a crew where nobody checked it
CONCENTRATED_AT = 0.75


def digest_of(value: Any) -> str:
    """one field's fingerprint, so "changed" can mean the text moved."""
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                           default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def rounds(events: list[dict]) -> list[dict]:
    """every rejection on a card, and the work that followed it.

    a rejection is a move out of review that is not a move to done. the window it opens closes
    at the next move into review or done, or stays open, because a rejection nobody has answered
    yet is not evidence of anything except that it happened.
    """
    found: list[dict] = []
    open_round: dict | None = None
    seen: dict[str, str] = {}
    for event in sorted(events, key=lambda e: e.get("created_date") or 0):
        kind = event.get("kind")
        data = event.get("data") or {}
        at = float(event.get("created_date") or 0)
        if kind == "created":
            # the card's birth is the first thing every later edit is compared against
            for field, fingerprint in (data.get("digests") or {}).items():
                seen.setdefault(str(field), str(fingerprint))
            continue
        if kind == "edited":
            digests = data.get("digests") or {}
            for field in data.get("fields") or []:
                if open_round is None:
                    # before any rejection the edit is just history; the digests still seed
                    # what the card held, so the first round compares against real content
                    if field in digests:
                        seen[field] = str(digests[field])
                    continue
                if field in digests and field in seen:
                    if seen[field] != str(digests[field]):
                        open_round["changed"].append(field)
                    seen[field] = str(digests[field])
                elif field in digests:
                    # no value to compare against, so the edit is named and not proven
                    open_round["assumed"].append(field)
                    seen[field] = str(digests[field])
                elif field not in open_round["assumed"]:
                    # an edit from before the digest was recorded: named, not proven
                    open_round["assumed"].append(field)
                open_round["edits"] += 1
            continue
        if kind == "moved" and data.get("from") == "review" and data.get("to") != "done":
            open_round = {
                "at": at, "by": event.get("actor") or "", "note": str(data.get("note") or ""),
                "changed": [], "assumed": [], "edits": 0, "closed_at": None,
                "closed_by": "", "evidence": "",
            }
            found.append(open_round)
            continue
        if kind == "moved" and open_round is not None and data.get("to") in ("review", "done"):
            open_round["closed_at"] = at
            open_round["closed_by"] = event.get("actor") or ""
            if data.get("to") == "done":
                open_round["evidence"] = str(data.get("evidence") or "")
    for row in found:
        # one field can be touched twice in a round; it is still one field that changed
        row["changed"] = sorted(set(row["changed"]))
        row["assumed"] = sorted(set(row["assumed"]) - set(row["changed"]))
        row["certain"] = not row["assumed"]
        if row["closed_at"] is None:
            row["verdict"] = "open"
        elif row["changed"] or row["evidence"]:
            row["verdict"] = "rework"
        elif row["assumed"]:
            # an edit happened and there was nothing to compare it against: the honest answer
            # is that this round cannot be judged, not that it changed something
            row["verdict"] = "unproven"
        else:
            row["verdict"] = "silent"
    return found


def share(units: list[dict]) -> dict:
    """the split of a card's work across the people who did it.

    units are `{"agent_id", "turns", "spend_cents"}`. turns are the honest measure of work here
    because a run records them; spend is attributed only where a reservation names an agent.
    """
    rows = []
    total_turns = sum(int(u.get("turns") or 0) for u in units)
    total_spend = sum(float(u.get("spend_cents") or 0) for u in units)
    unattributed = 0.0
    for unit in units:
        agent_id = str(unit.get("agent_id") or "")
        spend = float(unit.get("spend_cents") or 0)
        if not agent_id:
            unattributed += spend
            continue
        rows.append({
            "agent_id": agent_id,
            "turns": int(unit.get("turns") or 0),
            "spend_cents": spend,
            "turn_share": round(int(unit.get("turns") or 0) / total_turns, 4)
            if total_turns else None,
            "spend_share": round(spend / total_spend, 4) if total_spend else None,
        })
    rows.sort(key=lambda r: (-r["turns"], r["agent_id"]))
    top = rows[0]["turn_share"] if rows else None
    return {
        "agents": rows,
        "turns": total_turns,
        "spend_cents": round(total_spend, 2),
        "unattributed_cents": round(unattributed, 2),
        # one person holding most of a card is not a failure, but it is the thing to look at
        "concentrated": bool(top is not None and top >= CONCENTRATED_AT and len(rows) > 1),
        "top_share": top,
    }


def summarise(cards: list[dict]) -> dict:
    """what the rounds add up to, and the number that matters: rejections that changed nothing."""
    rejections = [r for c in cards for r in c.get("rounds") or []]
    closed = [r for r in rejections if r["verdict"] != "open"]
    silent = [r for r in closed if r["verdict"] == "silent"]
    # the share is over the rounds that could be judged. an unproven round is not evidence of a
    # rubber stamp, and counting it either way would be inventing a fact about it
    proven = [r for r in closed if r["verdict"] in ("silent", "rework")]
    return {
        "cards": len(cards),
        "rejections": len(rejections),
        "answered": len(closed),
        "reworked": len([r for r in closed if r["verdict"] == "rework"]),
        "silent": len(silent),
        "silent_share": round(len(silent) / len(proven), 4) if proven else None,
        "open": len(rejections) - len(closed),
        "unproven": len([r for r in closed if r["verdict"] == "unproven"]),
        "uncertain": len([r for r in closed if not r.get("certain", True)]),
    }


class DistributionService:
    def __init__(self, store: Store, crews: Any = None, kanban: Any = None):
        self.store = store
        self.crews = crews
        self.kanban = kanban

    # ---- the rows ------------------------------------------------------
    def _task(self, task_id: str) -> dict:
        row = self.store.one(
            "select id, team_id, title, column_name, assignee_agent_id from tasks where id = ?",
            (task_id,))
        if not row:
            raise NotFound(f"card {task_id} not found")
        return {"id": row["id"], "team_id": row["team_id"], "title": row["title"],
                "column": row["column_name"],
                "assignee_agent_id": row["assignee_agent_id"] or ""}

    def _events(self, task_id: str) -> list[dict]:
        return [
            {"kind": r["kind"], "actor": r["actor"], "data": loads(r["data"], {}),
             "created_date": float(r["created_date"])}
            for r in self.store.query(
                "select * from task_events where task_id = ? order by created_date, rowid",
                (task_id,))
        ]

    def _units(self, task_id: str, since: float = 0.0, until: float = 1e18) -> list[dict]:
        """turns and settled spend per agent, for a card or for a window inside it."""
        turns: dict[str, int] = {}
        for row in self.store.query(
                "select agent_id, steps from runs where task_id = ?", (task_id,)):
            agent_id = row["agent_id"] or ""
            steps = loads(row["steps"], [])
            turns[agent_id] = turns.get(agent_id, 0) + (
                len(steps) if isinstance(steps, list) else 0)
        spend: dict[str, float] = {}
        for row in self.store.query(
                """select agent_id, coalesce(sum(coalesce(settled_cents, amount_cents)), 0) as n
                   from budget_reservations
                   where task_id = ? and state = 'settled' and settled_at >= ?
                     and settled_at <= ? group by agent_id""",
                (task_id, float(since), float(until))):
            agent_id = row["agent_id"] or ""
            spend[agent_id] = spend.get(agent_id, 0.0) + float(row["n"] or 0)
        return [{"agent_id": agent_id, "turns": turns.get(agent_id, 0),
                 "spend_cents": spend.get(agent_id, 0.0)}
                for agent_id in sorted(set(turns) | set(spend))]

    # ---- the answers ---------------------------------------------------
    def for_card(self, task_id: str) -> dict:
        task = self._task(task_id)
        events = self._events(task_id)
        found = rounds(events)
        for row in found:
            # what the rework cost, measured from the rejection to the answer, and never past
            # now, since a round that is still open has only spent what it has spent
            end = row["closed_at"] if row["closed_at"] is not None else now()
            row["rework"] = share(self._units(task_id, row["at"], end))
        return {
            "task_id": task["id"],
            "title": task["title"],
            "column": task["column"],
            "assignee_agent_id": task["assignee_agent_id"],
            "distribution": share(self._units(task_id)),
            "rounds": found,
            "summary": summarise([{"rounds": found}]),
        }

    def for_crew(self, team_id: str, since: float | None = None,
                 until: float | None = None, limit: int = 50) -> dict:
        until = now() if until is None else float(until)
        since = (until - DEFAULT_WINDOW_DAYS * SECONDS_PER_DAY) if since is None else float(since)
        if self.crews is not None:
            self.crews.get_team(team_id)
        rows = self.store.query(
            """select id from tasks where team_id = ? and updated_date >= ? and updated_date <= ?
               order by updated_date desc""", (team_id, since, until))
        cards = [self.for_card(row["id"]) for row in rows]
        # the cards a person should open: rejections that changed nothing, and work in one pair
        # of hands on a card somebody else signed off
        silent = [c for c in cards if c["summary"]["silent"]]
        concentrated = [c for c in cards if c["distribution"]["concentrated"]]
        return {
            "team_id": team_id,
            "since": since,
            "until": until,
            "window_days": round((until - since) / SECONDS_PER_DAY, 2),
            "total": summarise(cards),
            "cards": cards[:int(limit)],
            "silent_rejections": sorted(silent, key=lambda c: -c["summary"]["silent"])[:10],
            "concentrated": sorted(concentrated,
                                   key=lambda c: -(c["distribution"]["top_share"] or 0))[:10],
        }
