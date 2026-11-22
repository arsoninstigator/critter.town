"""a limit that nothing counts is a wish. this is the count.

a seat that keeps earning refusals should stop being offered work, and the reason has to be
a row rather than an impression. two strikes freeze a seat; a person unfreezes it by
looking, never by time passing and never by good behaviour. burn has no inverse, because a
seat that is burned is a decision someone made, not a state that wears off.
"""

from __future__ import annotations

from core.db import Store, new_id, now
from core.errors import NotFound, ValidationError

STRIKE_LIMIT = 2
STATES = ("active", "frozen", "burned")


def apply(standing: dict, code: str, tool: str = "", note: str = "") -> dict:
    """one refusal against a seat, and whether it pushed the seat over the line."""
    if standing["state"] == "burned":
        return {**standing, "froze": False, "spared": "burned"}
    if standing["state"] == "frozen":
        return {**standing, "froze": False, "spared": "already_frozen"}
    strikes = int(standing.get("strikes") or 0) + 1
    froze = strikes >= STRIKE_LIMIT
    return {"seat": standing["seat"], "strikes": strikes,
            "state": "frozen" if froze else "active",
            "last_strike": {"code": code, "tool": tool, "note": note, "at": now()},
            "froze": froze, "spared": ""}


def clear(standing: dict) -> dict:
    """strikes are cleared by a person looking, and burn is not a way back from."""
    if standing["state"] == "burned":
        return dict(standing)
    return {"seat": standing["seat"], "strikes": 0, "state": "active", "last_strike": None}


class StrikeService:
    def __init__(self, store: Store, crews=None):
        self.store = store
        self.crews = crews

    def standing(self, agent_id: str) -> dict:
        rows = self.store.query(
            "select * from strikes where agent_id = ? and cleared_by = '' order by created_date desc",
            (agent_id,))
        count = len(rows)
        burned = bool(self.store.one(
            "select 1 as one from strikes where agent_id = ? and code = 'burned'", (agent_id,)))
        state = "burned" if burned else ("frozen" if count >= STRIKE_LIMIT else "active")
        last = rows[0] if rows else None
        return {"seat": agent_id, "strikes": count, "state": state,
                "last_strike": ({"code": last["code"], "tool": last["tool"],
                                 "note": last["note"], "at": last["created_date"]} if last else None)}

    def record(self, team_id: str, agent_id: str, code: str, tool: str = "", note: str = "") -> dict:
        if not code.strip():
            raise ValidationError("a strike needs a code, or it cannot be explained later")
        with self.store.tx() as c:
            c.execute(
                """insert into strikes (id, team_id, agent_id, code, tool, note, cleared_by, created_date)
                   values (?,?,?,?,?,?,'',?)""",
                (new_id("strike"), team_id, agent_id, code, tool, note, now()))
        standing = self.standing(agent_id)
        return {"standing": standing, "froze": standing["state"] != "active"}

    def clear(self, team_id: str, agent_id: str, by: str) -> dict:
        """clearing is a person's act, so it names the person."""
        if not by.strip():
            raise ValidationError("clearing strikes needs a person's name on it")
        with self.store.tx() as c:
            c.execute(
                "update strikes set cleared_by = ? where team_id = ? and agent_id = ? and cleared_by = ''",
                (by, team_id, agent_id))
        return self.standing(agent_id)

    def crew(self, team_id: str) -> dict:
        """every seat in the crew and where it stands, because a freeze nobody can see is
        a freeze that gets discovered by a card not moving."""
        agents = self.store.query(
            "select id, name, role, kind from agents where team_id = ? order by name",
            (team_id,))
        seats = []
        for agent in agents:
            standing = self.standing(agent["id"])
            seats.append({**standing, "name": agent["name"], "role": agent["role"],
                          "kind": agent["kind"]})
        return {"seats": seats,
                "frozen": [s["seat"] for s in seats if s["state"] == "frozen"],
                "burned": [s["seat"] for s in seats if s["state"] == "burned"],
                "limit": STRIKE_LIMIT}

    def check(self, agent_id: str) -> dict:
        """the door: a frozen seat is not offered work."""
        standing = self.standing(agent_id)
        if standing["state"] == "active":
            return {"allowed": True, "standing": standing}
        return {"allowed": False, "standing": standing,
                "reason": f"{standing['state']}: {standing['strikes']} strike(s) on this seat"}
