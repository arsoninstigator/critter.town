"""who is on the work, and what each of them is told.

two questions were being answered by accident. the first is who a task gets: dispatch
picked the best-scoring agent, which answers "who is free and close enough" and not "who
does this task need". the second is what each of them reads: the slicer honoured whatever
slices an agent had enabled, so a reviewer could be handed the doer's lessons and a lane
could be handed the whole board.

so formation is a decision table too, over the same signals the topology layer reads, and
it answers in seats: a role, the slices that role is entitled to, and one sentence saying
why the seat exists. it never creates an agent and never edits a profile. it says what the
work needs, and the gap between that and the crew is the thing a person fixes.

one rule is not negotiable: a seat is entitled to the slices its role needs, and a crew
may narrow that and never widen it. the reviewer is the case that matters, because a
reviewer who has read the doer's reasoning is not reviewing, it is agreeing.
"""

from __future__ import annotations

from agents.context import SLICES
from core.errors import ValidationError
from work import topology

# role -> the slices that role is entitled to. absence is a decision: a reviewer is not
# entitled to `lessons` (the doer's own teaching) or `inbox` (other people's requests).
ROLE_SLICES: dict[str, tuple[str, ...]] = {
    "doer": ("intent", "knowledge", "lessons", "task", "failures"),
    "reviewer": ("task", "board", "failures"),
    "integrator": ("task", "board", "knowledge"),
    "researcher": ("intent", "knowledge", "task", "inbox"),
    "synthesizer": ("knowledge", "task", "lessons"),
    "sampler": ("task", "board"),
}

# shape -> the seats it needs, in the order they are filled
SHAPE_SEATS: dict[str, tuple[str, ...]] = {
    "solo": ("doer",),
    "harness": ("doer", "reviewer"),
    "research": ("researcher", "synthesizer"),
    "factory": ("doer", "sampler"),
    "orchestration": ("doer", "integrator"),
}

ROLES = tuple(ROLE_SLICES)


def seats_for(shape: str, parts: int = 1) -> tuple[str, ...]:
    """the roles a shape needs, with the doer repeated once per independent part.

    `parts` is only meaningful for orchestration, where each independent part gets its own
    doer and the integrator joins what they report. capping it here keeps a caller from
    turning one card into a hundred lanes by accident.
    """
    if shape not in SHAPE_SEATS:
        raise ValidationError(f"unknown shape '{shape}'", shapes=sorted(SHAPE_SEATS))
    roles = list(SHAPE_SEATS[shape])
    if shape == "orchestration":
        lanes = max(1, min(int(parts), 8))
        roles = ["doer"] * lanes + ["integrator"]
    return tuple(roles)


def slices_for(role: str, granted: list[str] | None = None) -> dict:
    """what a seat is entitled to, narrowed by what the crew actually enabled.

    `granted` is the agent's own enabled slices. the intersection is what it reads, and
    what falls out is reported rather than dropped: a seat missing a slice it needs is a
    seat that will do the job blind, and that is worth a name.
    """
    if role not in ROLE_SLICES:
        raise ValidationError(f"unknown role '{role}'", roles=list(ROLES))
    entitled = list(ROLE_SLICES[role])
    if granted is None:
        return {"entitled": entitled, "granted": entitled, "withheld": []}
    allowed = [name for name in entitled if name in set(granted)]
    withheld = [name for name in entitled if name not in set(allowed)]
    return {"entitled": entitled, "granted": allowed, "withheld": withheld}


def form(signals: dict, agents: list[dict] | None = None, parts: int = 1) -> dict:
    """the roster for a task: seats, slices, and the reason each seat exists.

    deterministic on purpose, and it fails closed the same way topology does: an
    incomplete signal set returns no seats and names what is missing, because a roster
    invented from a guess is a crew spending a day on the wrong question.
    """
    decision = topology.choose(signals)
    if decision["problems"]:
        return {"seats": [], "because": "signals_incomplete", "problems": decision["problems"],
                "gaps": [], "decided": False, "staffed": False,
                "shape": decision["structure"], "gate": decision["gate"]}
    shape = decision["structure"]
    roles = seats_for(shape, parts)
    crew = list(agents or [])
    seats: list[dict] = []
    problems: list[str] = []
    gaps: list[str] = []
    for index, role in enumerate(roles):
        match = _pick(crew, role, taken=[seat["agent_id"] for seat in seats])
        granted = match.get("context") if match else None
        slices = slices_for(role, granted if isinstance(granted, list) else None)
        if not slices["granted"]:
            problems.append(f"seat_cannot_be_told_anything:{role}")
        seats.append({
            "role": role,
            "index": index,
            "agent_id": (match or {}).get("id", ""),
            "agent": (match or {}).get("name", ""),
            "slices": slices["granted"],
            "withheld": slices["withheld"],
            "why": _why(role, shape, bool(match)),
        })
        if match is None:
            gaps.append(f"no_agent_for_role:{role}")
    if shape in {"harness", "factory"} and len(crew) < 2:
        problems.append("a_review_seat_needs_a_second_agent")
    if shape == "orchestration" and len(crew) < 2:
        problems.append("orchestration_needs_two_agents")
    return {"shape": shape, "because": decision["because"], "seats": seats,
            "gate": decision["gate"], "gate_because": decision["gate_because"],
            "problems": problems, "gaps": gaps,
            # decided is about the shape; staffed is about the crew. a roster can be a
            # correct answer with nobody in it yet, and the two must not be confused
            "decided": not problems, "staffed": not problems and not gaps}


def _pick(agents: list[dict], role: str, taken: list[str]) -> dict | None:
    """the agent for a role: by declared role first, then by skill, then anybody free.

    the order matters, and it is stated here rather than left to a score: a crew's own
    labelling should beat a keyword, and a keyword should beat an accident.
    """
    free = [a for a in agents if a.get("id") not in taken]
    for agent in free:
        if str(agent.get("role", "")).lower() == role:
            return agent
    for agent in free:
        skills = [str(s).lower() for s in (agent.get("skills") or [])]
        if role in skills:
            return agent
    if role in {"doer", "sampler"} and free:
        return free[0]
    return None


def _why(role: str, shape: str, matched: bool) -> str:
    reasons = {
        "doer": f"the {shape} shape needs the work done once, in one place",
        "reviewer": "the gate needs someone who did not do the work",
        "integrator": "the lanes report to one seat that joins them",
        "researcher": "an open question needs someone whose job is finding out",
        "synthesizer": "the findings need one voice that keeps the disagreement",
        "sampler": "a batch is reviewed by sample, not by reading every item",
    }
    why = reasons.get(role, f"{role} for a {shape}")
    return why if matched else why + ", and nobody in the crew fills it"


def check_seat(seat: dict, granted: list[str] | None = None) -> list[str]:
    """invariants a seat has to hold, checked after the fact as well as at formation."""
    problems: list[str] = []
    if seat.get("role") not in ROLE_SLICES:
        problems.append(f"unknown_role:{seat.get('role')}")
        return problems
    entitled = set(ROLE_SLICES[seat["role"]])
    for name in seat.get("slices") or []:
        if name not in SLICES:
            problems.append(f"unknown_slice:{name}")
        elif name not in entitled:
            problems.append(f"slice_not_entitled:{seat['role']}:{name}")
    if granted is not None:
        for name in seat.get("slices") or []:
            if name not in set(granted):
                problems.append(f"slice_not_enabled:{name}")
    if not seat.get("slices"):
        problems.append(f"seat_has_no_slices:{seat.get('role')}")
    return problems
