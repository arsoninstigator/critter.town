"""delegation: the handoff, the envelope, and the breakdown that names an orphan.

a card can be handed to another agent, and the handoff is a state machine rather than a
message. state crosses the boundary; a summary does not, which is why an offer carries a
reference and never a paragraph. the receiver decides whether to take the work, and it may
only take it when the precondition it was given actually holds, because "i accepted" is
not the same claim as "the thing i need is there".

three mechanisms, deliberately separate:

1. the offer. who owes the work now, as a row with a reference, a precondition and a
   deadline. expiry is an event, not an absence: a silent drop becomes a row.
2. the envelope. what may cross a crew wall, with a kind, a reference, a hop count and a
   dedupe key, so a relay cannot become a chat app with authority.
3. the breakdown. a parent split into parts with owners, where a part with no owner is an
   alarm rather than a gap, because nobody is coming to pick it up later.
"""

from __future__ import annotations

import re
from typing import Any

from core.db import Store, dumps, loads, new_id, now
from core.errors import NotFound, ValidationError
from work import topology
from work.permeability import resolve

STATES = ("offered", "accepted", "rejected", "expired", "undelivered")
CLOSED = ("accepted", "rejected", "expired", "undelivered")
KINDS = ("finding", "proposal", "request", "signal", "artifact_ref")
DEFAULT_TIMEOUT = 3600.0

# a reference is a scheme and a payload, and the schemes are the ones this platform can
# point at. a handoff that carries prose is a handoff nobody can check.
REFERENCE = re.compile(
    r"^(artifact|repo|db|doc|url|card|run|commit|ticket|invoice|gate|payout|stripe|file)://\S+"
)


def validate(offer: dict) -> list[str]:
    """everything wrong with an offer, in one list, so a refusal can name them all."""
    problems: list[str] = []
    if not str(offer.get("from_agent_id") or "").strip():
        problems.append("a_handoff_needs_a_sender")
    if not str(offer.get("to_agent_id") or "").strip():
        problems.append("a_handoff_needs_a_receiver")
    if offer.get("from_agent_id") and offer.get("from_agent_id") == offer.get("to_agent_id"):
        problems.append("self_handoff")
    if not REFERENCE.match(str(offer.get("payload_ref") or "")):
        problems.append("payload_ref_must_be_a_reference")
    if not str(offer.get("precondition") or "").strip():
        problems.append("precondition_required")
    try:
        if float(offer.get("timeout_seconds") or 0) <= 0:
            problems.append("timeout_required")
    except (TypeError, ValueError):
        problems.append("timeout_required")
    return problems


def is_overdue(offer: dict, at: float | None = None) -> bool:
    if offer.get("state") != "offered":
        return False
    at = now() if at is None else at
    deadline = float(offer.get("offered_at") or 0) + float(offer.get("timeout_seconds") or 0)
    return at > deadline


def transition(offer: dict, event: dict, at: float | None = None) -> dict:
    """the next state, or a refusal. the refusals are the interesting part.

    only the addressee may accept, and it may only accept when the precondition holds.
    a receiver that accepts work it cannot do is not being helpful, it is moving the
    failure somewhere it is harder to see.
    """
    at = now() if at is None else at
    kind = str(event.get("type") or "")
    if offer.get("state") != "offered":
        raise ValidationError(
            f"cannot {kind} a handoff in state {offer.get('state')}",
            handoff=offer.get("id"), state=offer.get("state"))
    if is_overdue(offer, at):
        raise ValidationError("handoff timed out before this event", handoff=offer.get("id"))
    actor = str(event.get("actor") or "")
    if kind == "accept":
        if actor != offer.get("to_agent_id"):
            raise ValidationError("only the addressee may accept", handoff=offer.get("id"))
        if not event.get("precondition_held"):
            raise ValidationError("precondition not satisfied: refuse, do not accept",
                                  precondition=offer.get("precondition"))
        return {**offer, "state": "accepted", "accepted_at": at}
    if kind == "reject":
        if actor != offer.get("to_agent_id"):
            raise ValidationError("only the addressee may reject", handoff=offer.get("id"))
        if not str(event.get("reason") or "").strip():
            raise ValidationError("a_rejection_needs_a_reason", handoff=offer.get("id"))
        return {**offer, "state": "rejected", "reason": str(event["reason"])[:300]}
    if kind == "transport_failed":
        return {**offer, "state": "undelivered", "reason": str(event.get("reason") or "")[:300]}
    raise ValidationError(f"unknown handoff event '{kind}'; expected accept, reject or "
                          "transport_failed")


def expire(offer: dict, at: float | None = None) -> dict:
    return {**offer, "state": "expired"} if is_overdue(offer, at) else offer


def open_overdue(offers: list[dict], at: float | None = None) -> list[dict]:
    return [offer for offer in offers if is_overdue(offer, at)]


def orphan_partitions(partitions: list[str], assignments: dict[str, str]) -> list[str]:
    """a part with no owner. in parallel work this is an alarm, not a gap."""
    return [part for part in partitions if not str(assignments.get(part) or "").strip()]


def validate_envelope(envelope: dict) -> list[str]:
    problems: list[str] = []
    if envelope.get("kind") not in KINDS:
        problems.append(f"kind_must_be_one_of_{'|'.join(KINDS)}")
    if not REFERENCE.match(str(envelope.get("payload_ref") or "")):
        problems.append("payload_ref_must_be_a_reference")
    if not str(envelope.get("authority_basis") or "").strip():
        problems.append("authority_basis_required")
    hops, max_hops = int(envelope.get("hops") or 0), int(envelope.get("max_hops") or 0)
    if max_hops <= 0:
        problems.append("max_hops_required")
    elif hops > max_hops:
        problems.append(f"hop_limit_reached: {hops} of {max_hops}")
    return problems



# what a shape asks of a fan-out. a breakdown may be stricter than its shape and never
# looser: "all" is the strictest join, so asking for first_success where the shape says
# every part matters is how a split quietly becomes a race.
SHAPE_JOIN = {"solo": "all", "harness": "all", "factory": "all", "research": "quorum",
              "orchestration": "all"}
JOIN_STRICTNESS = {"all": 3, "quorum": 2, "first_success": 1}


def join_for(declared: str, shape: str) -> str:
    """the join a breakdown gets: the shape's, unless the caller asked for a stricter one."""
    wanted = SHAPE_JOIN.get(shape or "", "all")
    if not declared:
        return wanted
    if declared not in JOIN_STRICTNESS:
        raise ValidationError("unknown_join", join=declared)
    if JOIN_STRICTNESS[declared] < JOIN_STRICTNESS[wanted]:
        raise ValidationError(
            "a_breakdown_may_be_stricter_than_its_shape_not_looser",
            declared=declared, shape=shape or "unknown", shape_join=wanted)
    return declared


def split_budget(parent_budget: int, parts: list[dict]) -> dict:
    """what each part may spend. a part that declares nothing takes an even share of what
    the parent has left, and the sum is never more than the parent was given."""
    if parent_budget <= 0:
        return {"shares": [int(part.get("budget_cents") or 0) for part in parts], "remaining": 0}
    declared = [int(part.get("budget_cents") or 0) for part in parts]
    spent = sum(declared)
    if spent > parent_budget:
        raise ValidationError("parts_ask_for_more_than_the_parent_was_given",
                              asked=spent, parent=parent_budget)
    silent = [i for i, value in enumerate(declared) if value == 0]
    shares = list(declared)
    if silent:
        share = (parent_budget - spent) // len(silent)
        for i in silent:
            shares[i] = share
    return {"shares": shares, "remaining": parent_budget - sum(shares)}

class DelegationService:
    def __init__(self, store: Store, crews=None, kanban=None, audit=None, convergence=None,
                 projects=None, claims=None, strikes=None, crossings=None):
        self.store = store
        self.crews = crews
        self.kanban = kanban
        self.audit = audit
        self.convergence = convergence
        # a breakdown reads four things beyond the board: which slices the project has,
        # what the parts may spend, whether the seat is frozen, and whether a part leaves
        # the crew at all.
        self.projects = projects
        self.claims = claims
        self.strikes = strikes
        self.crossings = crossings

    # ---- the offer -----------------------------------------------------
    def offer(self, team_id: str, from_agent_id: str, to_agent_id: str, payload_ref: str,
              precondition: str, timeout_seconds: float = DEFAULT_TIMEOUT,
              task_id: str = "", note: str = "",
              acceptance_required: bool = True) -> dict:
        offer = {
            "from_agent_id": from_agent_id, "to_agent_id": to_agent_id,
            "payload_ref": payload_ref, "precondition": precondition,
            "timeout_seconds": timeout_seconds,
        }
        problems = validate(offer)
        if problems:
            raise ValidationError("a handoff that cannot be checked is not an offer",
                                  problems=problems)
        if self.crews is not None:
            for agent_id in (from_agent_id, to_agent_id):
                self.crews.get_agent(agent_id)  # raises when the agent is unknown
        handoff_id = new_id("hand")
        ts = now()
        with self.store.tx() as c:
            c.execute(
                """insert into delegations
                   (id, team_id, task_id, from_agent_id, to_agent_id, payload_ref,
                    precondition, acceptance_required, timeout_seconds, state, note,
                    offered_at, accepted_at, reason, created_date, updated_date)
                   values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (handoff_id, team_id, task_id, from_agent_id, to_agent_id, payload_ref,
                 precondition, 1 if acceptance_required else 0, float(timeout_seconds),
                 "offered", note[:500], ts, None, "", ts, ts),
            )
        return self.get(handoff_id)

    def get(self, handoff_id: str) -> dict:
        row = self.store.one("select * from delegations where id = ?", (handoff_id,))
        if not row:
            raise NotFound(f"handoff {handoff_id} not found")
        return self._shape(dict(row))

    def list(self, team_id: str, state: str | None = None, limit: int = 50) -> list[dict]:
        if state and state not in STATES:
            raise ValidationError(f"state must be one of {list(STATES)}")
        if state:
            rows = self.store.query(
                "select * from delegations where team_id = ? and state = ?"
                " order by created_date desc limit ?", (team_id, state, limit))
        else:
            rows = self.store.query(
                "select * from delegations where team_id = ? order by created_date desc limit ?",
                (team_id, limit))
        return [self._shape(dict(row)) for row in rows]

    def answer(self, handoff_id: str, event: dict, at: float | None = None) -> dict:
        """accept, reject or record a transport failure, then write the row."""
        current = self.get(handoff_id)
        updated = transition(current, event, at)
        with self.store.tx() as c:
            c.execute(
                "update delegations set state = ?, accepted_at = ?, reason = ?, updated_date = ?"
                " where id = ?",
                (updated["state"], updated.get("accepted_at"), updated.get("reason", ""),
                 now(), handoff_id))
        if updated["state"] == "accepted" and current.get("task_id") and self.kanban is not None:
            # taking the work means taking the card: an accepted handoff that leaves the
            # card with the sender is a handoff that moved nothing
            task = self.kanban.get_task(current["task_id"])
            if not task.get("assignee_agent_id"):
                self.kanban.assign(current["task_id"], current["to_agent_id"],
                                   actor=str(event.get("actor") or ""))
        if self.audit is not None:
            self.audit.record(
                team_id=current["team_id"], agent_id=current["to_agent_id"],
                action=f"handoff.{updated['state']}", actor_kind="agent",
                actor_id=str(event.get("actor") or ""), target_kind="handoff",
                target_id=handoff_id, payload={"reason": updated.get("reason", "")})
        return self.get(handoff_id)

    def sweep(self, team_id: str | None = None, at: float | None = None) -> dict:
        """expiry as an event. a handoff nobody answered becomes a row that says so."""
        at = now() if at is None else at
        sql = "select * from delegations where state = 'offered'"
        params: tuple = ()
        if team_id:
            sql += " and team_id = ?"
            params = (team_id,)
        offers = [self._shape(dict(row)) for row in self.store.query(sql, params)]
        overdue = open_overdue(offers, at)
        for offer in overdue:
            with self.store.tx() as c:
                c.execute("update delegations set state = 'expired', updated_date = ?"
                          " where id = ? and state = 'offered'", (now(), offer["id"]))
        return {"expired": [offer["id"] for offer in overdue], "checked": len(offers)}

    # ---- the breakdown -------------------------------------------------
    def breakdown(self, task_id: str, parts: list[dict], join: str = "",
                  quorum: int | None = None, project_id: str = "") -> dict:
        """split a parent into owned parts, and refuse to pretend a part is fine when it is
        not. a part can be wrong in five ways, and each one is named rather than created:
        no owner, a slice the project does not have, a seat that is frozen, a part that
        leaves the crew without a door, and a budget that outruns the parent.
        """
        if self.kanban is None:
            raise ValidationError("a breakdown needs the board")
        parent = self.kanban.get_task(task_id)
        wanted = [part for part in parts if isinstance(part, dict) and part.get("title")]
        if len(wanted) < 2:
            raise ValidationError("a_breakdown_needs_two_parts", parts=len(wanted))
        payload = parent.get("payload") or {}
        problems: list[str] = []

        # the shape decides the join. a caller may tighten it and may not loosen it.
        shape = str(payload.get("structure") or "")
        if not shape and payload.get("signals"):
            shape = str(topology.choose(payload["signals"]).get("structure") or "")
        join = join_for(join, shape)

        # which project is this card part of, and which slices may a part name
        bound = project_id or str(payload.get("project_id") or "")
        project = self.projects.get(bound) if (bound and self.projects is not None) else None
        allowed_slices = {str(s.get("slice")) for s in (project or {}).get("slices") or []}

        # what the parts may spend between them
        parent_budget = int(payload.get("budget_cents") or 0)
        try:
            budget = split_budget(parent_budget, wanted)
        except ValidationError:
            raise
        shares = budget["shares"]

        assignments = {str(part["title"]): str(part.get("assignee_agent_id") or "")
                       for part in wanted}
        orphans = set(orphan_partitions([str(part["title"]) for part in wanted], assignments))
        problems.extend(f"orphan_partition:{name}" for name in orphans)

        # a frozen seat is not offered work, and a part outside the crew needs a declared
        # door before it is a part rather than a leak
        blocked: set[str] = set()
        for part in wanted:
            title = str(part["title"])
            if title in orphans:
                continue
            agent_id = assignments[title]
            if self.strikes is not None:
                standing = self.strikes.check(agent_id)
                if not standing["allowed"]:
                    blocked.add(title)
                    problems.append(f"seat_frozen:{title}")
                    continue
            if self.crews is not None and self.crossings is not None:
                agent = self.crews.get_agent(agent_id)
                if str(agent.get("team_id")) != str(parent["team_id"]):
                    found = resolve(self.crossings.list(parent["team_id"]),
                                    str(parent["team_id"]), str(agent.get("team_id")),
                                    "request", now())
                    if not found["ok"]:
                        blocked.add(title)
                        problems.append(f"crossing_not_declared:{title}:{found['reason']}")

        # a slice the project does not have is the same kind of alarm as an unowned part:
        # it means the split invented work nobody scoped
        if project is not None:
            for part in wanted:
                title = str(part["title"])
                if title in orphans or title in blocked:
                    continue
                named = str(part.get("slice") or "")
                if named not in allowed_slices:
                    blocked.add(title)
                    problems.append(f"slice_not_in_project:{title}:{named or 'none'}")

        created: list[dict] = []
        for index, part in enumerate(wanted):
            title = str(part["title"])
            if title in orphans or title in blocked:
                continue
            part_payload = {"surface": str(part.get("surface") or title)}
            if project is not None:
                part_payload["project_id"] = project["id"]
                part_payload["slice"] = str(part.get("slice"))
            if shares[index]:
                part_payload["budget_cents"] = shares[index]
            created.append(self.kanban.create_task(
                team_id=parent["team_id"], title=title[:200],
                description=str(part.get("description", "")), column="ready",
                priority=int(part.get("priority", parent["priority"])),
                assignee_agent_id=assignments[title], parent_id=task_id,
                payload=part_payload,
            ))

        barrier = None
        if len(created) > 1 and self.convergence is not None:
            lanes = {child["id"]: child["id"] for child in created}
            barrier = self.convergence.open(task_id, lanes, list(lanes), join=join,
                                            quorum=quorum)

        # the split is an assertion, so it is written as one: a plan claim per part, and
        # two parts on one surface oppose each other here rather than colliding later
        claim_ids: list[str] = []
        if self.claims is not None and created:
            by_surface: dict[str, list[str]] = {}
            drafts = []
            for child in created:
                surface = str((child.get("payload") or {}).get("surface", child["title"]))
                cid = new_id("claim")
                by_surface.setdefault(surface, []).append(cid)
                drafts.append({"id": cid, "kind": "plan",
                               "statement": f"{surface} is covered by {child['title']}",
                               "confidence": 0.6, "basis": f"task:{task_id}",
                               "by": str(child.get("assignee_agent_id") or "")})
            for ids in by_surface.values():
                if len(ids) > 1:
                    for cid in ids:
                        next(d for d in drafts if d["id"] == cid)["opposes"] = [
                            other for other in ids if other != cid]
            try:
                posted = self.claims.post(parent["team_id"], task_id,
                                          f"breakdown of {parent['title']}", drafts,
                                          by=str(parent.get("assignee_agent_id") or "owner"))
                claim_ids = posted["claims"]
            except ValidationError as exc:
                # a split whose claims will not hold is a split that cannot explain itself
                problems.append(f"claims_refused:{exc.code}")

        return {"created": created, "orphans": sorted(orphans), "barrier": barrier,
                "join": join, "budget": {"parent": parent_budget, "remaining": budget["remaining"]},
                "claims": claim_ids, "problems": problems}

    # ---- rows ----------------------------------------------------------
    def _shape(self, row: dict) -> dict:
        row["acceptance_required"] = bool(row.get("acceptance_required"))
        row["timeout_seconds"] = float(row.get("timeout_seconds") or 0)
        return row


# ---- the delivery side ---------------------------------------------------
# an envelope is offered and then has to arrive. the queue is where "it was sent" and
# "it was received" stop being the same sentence.

DEFAULT_BACKOFF = {"base": 2.0, "cap": 900.0, "max_attempts": 8}
QUEUE_STATES = ("queued", "leased", "delivered", "dead")


def _hash32(text: str) -> int:
    """fnv-1a, so jitter is the same number on every machine and in every test."""
    value = 0x811C9DC5
    for char in text:
        value ^= ord(char)
        value = (value * 0x01000193) & 0xFFFFFFFF
    return value


def jitter_factor(message_id: str, attempt: int) -> float:
    """deterministic jitter in [0.5, 1.5): retries spread without a random seed."""
    return 0.5 + (_hash32(f"{message_id}:{attempt}") % 1000) / 1000


def backoff_seconds(message_id: str, attempt: int, config: dict | None = None) -> float:
    config = {**DEFAULT_BACKOFF, **(config or {})}
    raw = config["base"] * (2 ** max(0, attempt))
    return round(min(config["cap"], raw) * jitter_factor(message_id, attempt), 3)


def is_visible(message: dict, at: float) -> bool:
    return message["state"] == "queued" and float(message.get("next_attempt_at") or 0) <= at


def lease(message: dict, holder: str, at: float, ttl: float = 60.0) -> dict:
    if message["state"] != "queued":
        raise ValidationError(f"cannot_lease_from:{message['state']}", message=message["id"])
    return {**message, "state": "leased", "holder": holder, "locked_until": at + ttl}


def ack(message: dict, at: float) -> dict:
    """written after the receiver persists, never after it reads."""
    return {**message, "state": "delivered", "acked_at": at, "holder": "",
            "locked_until": None}


def dead(message: dict, reason: str) -> dict:
    return {**message, "state": "dead", "dead_reason": reason, "holder": "",
            "locked_until": None}


def nack(message: dict, at: float, config: dict | None = None) -> dict:
    """retries stop before the ttl: retrying into an expired envelope spends budget to
    deliver something the receiver has to reject on arrival."""
    config = {**DEFAULT_BACKOFF, **(config or {})}
    attempts = int(message.get("attempts", 0)) + 1
    if attempts >= min(int(message.get("max_attempts", config["max_attempts"])),
                       int(config["max_attempts"])):
        return dead(message, "attempts_exhausted")
    next_at = at + backoff_seconds(message["id"], attempts, config)
    if next_at >= float(message.get("expires_at") or float("inf")):
        return dead(message, "ttl_would_expire_before_retry")
    return {**message, "state": "queued", "attempts": attempts, "next_attempt_at": next_at,
            "holder": "", "locked_until": None}


def reclaim(message: dict, at: float, config: dict | None = None) -> dict:
    """the holder died mid-delivery. the lease expires and the message comes back by
    itself, which is the same trick the board uses for a card."""
    if message["state"] != "leased":
        return message
    if message.get("locked_until") and float(message["locked_until"]) > at:
        return message
    return nack({**message, "state": "queued"}, at, config)


def check_route(route: dict, at: float, size: int = 0) -> str:
    """a route answers "" or the reason it cannot carry this. an expired route, a size
    over the limit and a rate already spent are all refusals, not warnings."""
    if float(route.get("expires_at") or float("inf")) <= at:
        return "route_expired"
    if size and int(route.get("max_bytes") or 0) and size > int(route["max_bytes"]):
        return "route_payload_too_large"
    per_hour = int(route.get("max_per_hour") or 0)
    if per_hour and int(route.get("used_this_hour") or 0) >= per_hour:
        return "route_rate_exhausted"
    return ""
