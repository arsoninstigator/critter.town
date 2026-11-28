"""rooms that follow the org.

four scopes mirror four pieces of the org, plus `plain` for a room somebody opened by hand:

- crew: everyone in the crew. one per crew, the room the whole town of that crew shares.
- department: the department's members, its coordinator as lead.
- bloc: the seats a bloc borrowed, for as long as each grant lives. the room closes with the bloc,
  because a bloc that kept its room after its objective ended would be a department nobody named.
- wing: the wing's holders, all lead. a wing has a veto, so everyone holding it can steer its room.

roles are ours (lead, member) and map onto the relay's own (admin, member), so the relay refuses what
we decided rather than us asking it nicely. the human owner is not a row here: a person reads every
room through the overview, and an observer that is a fake agent would sign as someone it is not.

`plan` is pure: it compares what the org says against what exists and names every change. `apply`
carries a plan out. keeping them apart is what lets a reconcile be read before it runs.
"""
from __future__ import annotations

from typing import Any

from core.db import Store, now
from core.errors import NotAllowed, NotFound, ValidationError

SCOPES = ("crew", "department", "bloc", "wing")
ROLES = ("lead", "member")
# our role -> the relay's. `owner` is kept for the relay's own operator; an agent never holds it, so
# no agent can delete a room or demote the person who runs the relay
RELAY_ROLE = {"lead": "admin", "member": "member"}

# a department's channel is a dept-kind room, the rest are ordinary rooms
KIND_FOR_SCOPE = {"crew": "channel", "department": "dept", "bloc": "channel", "wing": "channel"}
SLUG_PREFIX = {"crew": "crew", "department": "dept", "bloc": "bloc", "wing": "wing"}


def desired_rooms(store: Store, team_id: str, at: float | None = None) -> list[dict]:
    """every room the org implies right now, each with its full roster. read only."""
    moment = now() if at is None else at
    team = store.one("select id, name, slug from teams where id = ?", (team_id,))
    if not team:
        raise NotFound(f"crew {team_id} not found")
    rooms: list[dict] = []

    agents = store.query("select id from agents where team_id = ? order by created_date", (team_id,))
    rooms.append({"scope": "crew", "scope_id": team_id, "name": team["name"],
                  "slug": f"crew-{team['slug']}", "purpose": f"everyone in {team['name']}",
                  "open": True,
                  "roster": {row["id"]: "member" for row in agents}})

    for dept in store.query(
            "select id, name, slug, purpose, coordinator_agent_id from departments "
            "where team_id = ? order by created_date", (team_id,)):
        roster = {row["agent_id"]: "member" for row in store.query(
            "select agent_id from department_members where department_id = ?", (dept["id"],))}
        if dept["coordinator_agent_id"]:
            roster[dept["coordinator_agent_id"]] = "lead"
        rooms.append({"scope": "department", "scope_id": dept["id"], "name": dept["name"],
                      "slug": f"dept-{dept['slug']}", "purpose": dept["purpose"] or "",
                      "open": True, "roster": roster})

    from core.db import loads
    for bloc in store.query(
            "select id, name, objective, state, closes_at from org_blocs where team_id = ? "
            "order by created_date", (team_id,)):
        live = bloc["state"] == "open" and float(bloc["closes_at"]) > moment
        roster = {}
        if live:
            for grant in store.query(
                    """select agent_id from org_bloc_grants where bloc_id = ? and revoked_at is null
                       and expires_at > ?""", (bloc["id"], moment)):
                roster[grant["agent_id"]] = "member"
        rooms.append({"scope": "bloc", "scope_id": bloc["id"], "name": bloc["name"],
                      "slug": f"bloc-{_slug(bloc['name'])}-{bloc['id'][-6:]}",
                      "purpose": bloc["objective"], "open": live, "roster": roster})

    for wing in store.query(
            "select id, name, domain, agent_ids from org_wings where team_id = ? "
            "order by created_date", (team_id,)):
        rooms.append({"scope": "wing", "scope_id": wing["id"], "name": wing["name"],
                      "slug": f"wing-{_slug(wing['domain'])}",
                      "purpose": f"the {wing['domain']} wing",
                      "open": True,
                      "roster": {agent_id: "lead" for agent_id in loads(wing["agent_ids"], [])}})
    return rooms


def _slug(value: str) -> str:
    from agents.crews import slugify
    return slugify(value)


def existing_rooms(store: Store, team_id: str) -> dict[tuple[str, str], dict]:
    out: dict[tuple[str, str], dict] = {}
    for row in store.query(
            "select id, scope, scope_id, state, name, purpose from channels "
            "where team_id = ? and scope != 'plain'", (team_id,)):
        roster = {m["agent_id"]: m["role"] for m in store.query(
            "select agent_id, role from channel_members where channel_id = ?", (row["id"],))}
        out[(row["scope"], row["scope_id"])] = {
            "id": row["id"], "state": row["state"], "name": row["name"],
            "purpose": row["purpose"], "roster": roster}
    return out


def plan(desired: list[dict], existing: dict[tuple[str, str], dict]) -> list[dict]:
    """every change that makes the rooms match the org, in an order that is safe to apply: a room is
    made before anyone joins it, and a room is closed after its last member leaves."""
    steps: list[dict] = []
    for room in desired:
        key = (room["scope"], room["scope_id"])
        have = existing.get(key)
        if have is None:
            if not room["open"]:
                continue  # a bloc that ended before it was ever reconciled needs no room
            steps.append({"op": "create", "scope": room["scope"], "scope_id": room["scope_id"],
                          "name": room["name"], "slug": room["slug"], "purpose": room["purpose"]})
            for agent_id, role in sorted(room["roster"].items()):
                steps.append({"op": "join", "scope": room["scope"], "scope_id": room["scope_id"],
                              "agent_id": agent_id, "role": role})
            continue
        if room["open"] and have["state"] != "open":
            steps.append({"op": "reopen", "channel_id": have["id"], "scope": room["scope"],
                          "scope_id": room["scope_id"]})
        if have["name"] != room["name"] or (have["purpose"] or "") != (room["purpose"] or ""):
            steps.append({"op": "rename", "channel_id": have["id"], "name": room["name"],
                          "purpose": room["purpose"]})
        for agent_id, role in sorted(room["roster"].items()):
            if have["roster"].get(agent_id) != role:
                steps.append({"op": "join", "channel_id": have["id"], "agent_id": agent_id,
                              "role": role, "was": have["roster"].get(agent_id)})
        for agent_id in sorted(set(have["roster"]) - set(room["roster"])):
            steps.append({"op": "leave", "channel_id": have["id"], "agent_id": agent_id,
                          "was": have["roster"][agent_id]})
        if not room["open"] and have["state"] == "open":
            steps.append({"op": "close", "channel_id": have["id"], "scope": room["scope"],
                          "scope_id": room["scope_id"]})
    # a room whose piece of the org is gone (a department deleted, a wing disbanded) closes too
    wanted = {(r["scope"], r["scope_id"]) for r in desired}
    for key, have in sorted(existing.items()):
        if key not in wanted and have["state"] == "open":
            for agent_id in sorted(have["roster"]):
                steps.append({"op": "leave", "channel_id": have["id"], "agent_id": agent_id,
                              "was": have["roster"][agent_id]})
            steps.append({"op": "close", "channel_id": have["id"], "scope": key[0],
                          "scope_id": key[1], "why": "its piece of the org no longer exists"})
    return steps


class Rooms:
    def __init__(self, store: Store, channels: Any):
        self.store = store
        self.channels = channels

    def plan(self, team_id: str, at: float | None = None) -> dict:
        desired = desired_rooms(self.store, team_id, at)
        steps = plan(desired, existing_rooms(self.store, team_id))
        return {"team_id": team_id, "steps": steps, "in_sync": not steps}

    def reconcile(self, team_id: str, at: float | None = None) -> dict:
        """make the rooms match the org. safe to run any number of times: a second run finds nothing
        to do, and the unique index on (crew, scope, scope_id) refuses a second room for one piece."""
        planned = self.plan(team_id, at)
        made: dict[tuple[str, str], str] = {}
        for step in planned["steps"]:
            op = step["op"]
            if op == "create":
                channel = self.channels.create(
                    team_id, step["name"], purpose=step["purpose"],
                    kind=KIND_FOR_SCOPE[step["scope"]], slug=self._free_slug(team_id, step["slug"]))
                with self.store.tx() as c:
                    c.execute("update channels set scope = ?, scope_id = ? where id = ?",
                              (step["scope"], step["scope_id"], channel["id"]))
                made[(step["scope"], step["scope_id"])] = channel["id"]
            elif op == "join":
                channel_id = step.get("channel_id") or made[(step["scope"], step["scope_id"])]
                self.channels.join(channel_id, step["agent_id"], role=step["role"], _org=True)
            elif op == "leave":
                self.channels.leave(step["channel_id"], step["agent_id"], _org=True)
            elif op == "rename":
                self.channels.update(step["channel_id"], name=step["name"], purpose=step["purpose"])
            elif op == "close":
                with self.store.tx() as c:
                    c.execute("update channels set state = 'closed', closed_date = ?, "
                              "updated_date = ? where id = ?", (now(), now(), step["channel_id"]))
            elif op == "reopen":
                with self.store.tx() as c:
                    c.execute("update channels set state = 'open', closed_date = null, "
                              "updated_date = ? where id = ?", (now(), step["channel_id"]))
        return {**planned, "applied": len(planned["steps"])}

    def _free_slug(self, team_id: str, slug: str) -> str:
        """a hand made room may already hold the slug; the org's room takes the next free one rather
        than taking a name somebody chose."""
        candidate, n = slug, 2
        while self.store.one("select id from channels where team_id = ? and slug = ?",
                             (team_id, candidate)):
            candidate, n = f"{slug}-{n}", n + 1
        return candidate

    # ---- the relay's view ---------------------------------------------
    def relay_roster(self, channel_id: str) -> dict:
        """the roster as the relay should hold it: our roles mapped to its roles."""
        channel = self.channels.get(channel_id)
        return {"channel_id": channel_id, "scope": channel.get("scope", "plain"),
                "members": [{"agent_id": m["agent_id"], "role": m["role"],
                             "relay_role": RELAY_ROLE[m["role"]]}
                            for m in channel["members"]]}


# ---- the overview: what a person reads to see the town talk -----------------
def overview(store: Store, team_id: str, recent: int = 5) -> dict:
    """one row per room, enough to see where the work and the talk are without opening any of it.

    a2a rows carry no channel, so a message is attributed to a room by a rule a person can re-run: it
    belongs when its card is on the room's board, or when its sender and its recipient are both
    members. a note with no recipient (one agent to the whole crew) is attributed to no room, because
    guessing would put a private remark in a public one."""
    if not store.one("select id from teams where id = ?", (team_id,)):
        raise NotFound(f"crew {team_id} not found")
    rooms = []
    for ch in store.query(
            "select id, name, slug, kind, scope, scope_id, state, purpose from channels "
            "where team_id = ? order by case scope when 'crew' then 0 when 'department' then 1 "
            "when 'wing' then 2 when 'bloc' then 3 else 4 end, name", (team_id,)):
        members = store.query(
            """select m.agent_id, m.role, a.name from channel_members m
               join agents a on a.id = m.agent_id where m.channel_id = ?
               order by m.role desc, a.name""", (ch["id"],))
        ids = [m["agent_id"] for m in members]
        cards = {row["column_name"]: int(row["n"]) for row in store.query(
            "select column_name, count(*) as n from tasks where channel_id = ? group by column_name",
            (ch["id"],))}
        talk = _a2a_for_room(store, team_id, ch["id"], ids, recent)
        rooms.append({
            "id": ch["id"], "name": ch["name"], "slug": ch["slug"], "scope": ch["scope"],
            "scope_id": ch["scope_id"], "state": ch["state"], "purpose": ch["purpose"],
            "members": [{"agent_id": m["agent_id"], "name": m["name"], "role": m["role"],
                         "relay_role": RELAY_ROLE.get(m["role"], "member")} for m in members],
            "cards": cards, "open_cards": sum(n for col, n in cards.items() if col != "done"),
            "a2a": talk,
            "messages": int(store.one(
                "select count(*) as n from channel_messages where channel_id = ?",
                (ch["id"],))["n"]),
        })
    return {"team_id": team_id, "rooms": rooms,
            "attribution": ("an a2a message belongs to a room when its card is on that room's board "
                            "or when both its sender and its recipient are members; a message to the "
                            "whole crew belongs to no room")}


def _a2a_for_room(store: Store, team_id: str, channel_id: str, member_ids: list[str],
                  recent: int) -> dict:
    clauses = ["(m.task_id in (select id from tasks where channel_id = ?))"]
    args: list[Any] = [team_id, channel_id]
    if len(member_ids) >= 2:
        marks = ",".join("?" for _ in member_ids)
        clauses.append(f"(m.from_agent_id in ({marks}) and m.to_agent_id in ({marks}))")
        args += member_ids + member_ids
    where = " or ".join(clauses)
    rows = store.query(
        f"""select m.id, m.kind, m.status, m.from_agent_id, m.to_agent_id, m.body, m.created_date,
                   fa.name as from_name, ta.name as to_name
            from a2a_messages m
            join agents fa on fa.id = m.from_agent_id
            left join agents ta on ta.id = m.to_agent_id
            where m.team_id = ? and ({where})
            order by m.created_date desc""", tuple(args))
    return {"count": len(rows),
            "pending_review": sum(1 for r in rows if r["status"] == "pending"),
            "recent": [{"id": r["id"], "kind": r["kind"], "status": r["status"],
                        "from": r["from_name"], "to": r["to_name"],
                        "body": (r["body"] or "")[:160], "created_date": r["created_date"]}
                       for r in rows[:recent]]}
