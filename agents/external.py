"""an agent we do not run.

the field has two ways in for an agent built somewhere else: write an adapter per framework,
or let it poll for work. the second one is cheaper and keeps the audit chain intact, because
the foreign worker never enters this process: it asks for a card, holds it under the same
lease, and reports back through the same routes. paperclip's version of the rule is that if
something can receive a heartbeat it can be hired.

hiring is not the same as trusting, so the boundary is written here as a rule rather than
left to whoever wires the route:

- an external worker may hold work. that is what it was hired for
- it may not review, because a reviewer is asked to judge a card it cannot see inside, and
  the one thing this repo refuses is a judgement with nothing behind it
- it may not integrate, for the same reason: joining lanes is a decision about other
  people's evidence
- everything else it gets is what a local worker gets: a lease that expires, a silence
  watchdog, strikes that freeze the seat, and a budget reservation
"""

from __future__ import annotations

from core.errors import ValidationError

HOSTING = ("local", "external")
# what an external worker may hold. roles are the same words the roster uses
HOLDABLE_ROLES = ("doer", "sampler", "researcher")
JUDGING_ROLES = ("reviewer", "integrator")


def hosting_of(agent: dict) -> str:
    return str((agent or {}).get("hosting") or "local")


def is_external(agent: dict) -> bool:
    return hosting_of(agent) == "external"


def validate(value: str) -> str:
    hosting = str(value or "local").strip().lower()
    if hosting not in HOSTING:
        raise ValidationError(f"hosting must be one of {list(HOSTING)}", hosting=hosting)
    return hosting


def may_hold(agent: dict, role: str) -> dict:
    """whether this worker may be given a seat. a local worker is never asked."""
    if not is_external(agent):
        return {"ok": True, "why": ""}
    if role in HOLDABLE_ROLES:
        return {"ok": True, "why": ""}
    if role in JUDGING_ROLES:
        return {"ok": False, "why": "external_agent_cannot_judge", "role": role}
    return {"ok": False, "why": "external_agent_cannot_hold_role", "role": role}


def may_review(agent: dict) -> dict:
    """the review gate. an external worker may not sign a card off, including its own."""
    if not is_external(agent):
        return {"ok": True, "why": ""}
    return {"ok": False, "why": "external_agent_cannot_review",
            "agent_id": (agent or {}).get("id", "")}


def may_integrate(agent: dict) -> dict:
    if not is_external(agent):
        return {"ok": True, "why": ""}
    return {"ok": False, "why": "external_agent_cannot_integrate",
            "agent_id": (agent or {}).get("id", "")}


def describe(agent: dict) -> dict:
    """what the platform promises a foreign worker, in the worker's own terms."""
    return {
        "agent_id": (agent or {}).get("id", ""),
        "hosting": hosting_of(agent),
        "may_hold": list(HOLDABLE_ROLES) if is_external(agent) else ["any seat its kind allows"],
        "may_not": list(JUDGING_ROLES) if is_external(agent) else [],
        "gets": ["a lease that expires", "a silence watchdog", "strikes that freeze the seat",
                 "a budget reservation"],
        "calls": ["heartbeat", "renew", "output", "complete"],
    }
