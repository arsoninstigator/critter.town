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


class DelegationService:
    def __init__(self, store: Store, crews=None, kanban=None, audit=None, convergence=None):
        self.store = store
        self.crews = crews
        self.kanban = kanban
        self.audit = audit
        self.convergence = convergence

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
    def breakdown(self, task_id: str, parts: list[dict], join: str = "all",
                  quorum: int | None = None) -> dict:
        """split a parent into owned parts, and refuse to pretend an unowned part is fine.

        the parts are cards, because a card is what holds custody here. a part with no
        owner is reported rather than created, since a card nobody is assigned to is a
        card that waits for a person to notice it.
        """
        if self.kanban is None:
            raise ValidationError("a breakdown needs the board")
        parent = self.kanban.get_task(task_id)
        wanted = [part for part in parts if isinstance(part, dict) and part.get("title")]
        if len(wanted) < 2:
            raise ValidationError("a_breakdown_needs_two_parts", parts=len(wanted))
        assignments = {str(part["title"]): str(part.get("assignee_agent_id") or "")
                       for part in wanted}
        orphans = orphan_partitions([str(part["title"]) for part in wanted], assignments)
        created: list[dict] = []
        for part in wanted:
            if str(part["title"]) in orphans:
                continue
            created.append(self.kanban.create_task(
                team_id=parent["team_id"], title=str(part["title"])[:200],
                description=str(part.get("description", "")), column="ready",
                priority=int(part.get("priority", parent["priority"])),
                assignee_agent_id=str(part["assignee_agent_id"]), parent_id=task_id,
                payload={"surface": str(part.get("surface") or part["title"])},
            ))
        barrier = None
        if len(created) > 1 and self.convergence is not None:
            lanes = {child["id"]: child["id"] for child in created}
            barrier = self.convergence.open(task_id, lanes, list(lanes), join=join,
                                            quorum=quorum)
        return {"created": created, "orphans": orphans, "barrier": barrier,
                "problems": [f"orphan_partition:{name}" for name in orphans]}

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
