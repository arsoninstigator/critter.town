"""what kind of worker an agent is, as a rule rather than a name.

a crew can already hold agents with roles and skills, and the roster decides which seat
each fills. what was missing is the other direction: an agent that only deals with code
should not be asked to run a research question, and a research agent should not be handed
a repository to refactor. that is not a preference about personality, it is a claim about
which tools and which context make the work possible.

the four kinds of agent work are the four the research names, plus a default:

- `harness`: a coding harness. reads a repository, runs commands, checks a test. the seat roles are
  doer and reviewer, and the slices are the ones a change needs: intent, task, lessons,
  failures, board. no inbox, because a coding card is not a conversation.
- `autoscientist`: an auto-research loop. finds out what is true before deciding what to do, so
  it needs knowledge and the inbox and is allowed to be slow.
- `factory`: a dark factory. one item at a time, many times, cheaply: task, board, lessons.
- `orchestrator`: does not do the work. joins lanes, so it reads task, board, knowledge and
  nothing that would tempt it to do a lane's job.
- `general`: no cap. the honest default for an agent nobody has characterised yet.

a kind caps the roles it may fill and the slices it may read. it never widens them: the
roster's role entitlement is the ceiling, and a kind is a second, narrower ceiling.
"""

from __future__ import annotations

KINDS: dict[str, dict] = {
    "harness": {
        "roles": ("doer", "reviewer"),
        "slices": ("intent", "task", "lessons", "failures", "board"),
        "note": "a harness: a repository, a command, a check",
    },
    "autoscientist": {
        "roles": ("researcher", "synthesizer"),
        "slices": ("intent", "knowledge", "task", "inbox", "lessons"),
        "note": "an auto-research loop: find out before deciding",
    },
    "factory": {
        "roles": ("doer", "sampler"),
        "slices": ("task", "board", "lessons"),
        "note": "one item, many times, cheaply",
    },
    "orchestrator": {
        "roles": ("integrator",),
        "slices": ("task", "board", "knowledge"),
        "note": "joins lanes, does not run one",
    },
    "general": {
        "roles": ("doer", "reviewer", "researcher", "synthesizer", "integrator", "sampler"),
        "slices": ("intent", "knowledge", "lessons", "task", "board", "inbox", "failures", "crew"),
        "note": "nobody has said what this agent is for yet",
    },
}


# the names people actually type, so a crew can say "code" or "research" without the
# roster having to guess which of the four it meant
ALIASES: dict[str, str] = {
    "code": "harness",
    "coding": "harness",
    "research": "autoscientist",
    "dark-factory": "factory",
    "dark_factory": "factory",
    "batch": "factory",
    "orchestration": "orchestrator",
}


def canonical(kind: str) -> str:
    """the kind behind a name, or the name itself when it is already canonical."""
    name = str(kind or "general").strip().lower()
    return ALIASES.get(name, name)


def validate(kind: str) -> str:
    name = canonical(kind)
    if name not in KINDS:
        raise ValueError(f"unknown agent kind '{kind}'; expected one of {sorted(KINDS)}")
    return name


def may_fill(kind: str, role: str) -> tuple[bool, str]:
    """can an agent of this kind take this seat, and if not, why not."""
    kind = canonical(kind)
    spec = KINDS.get(kind or "general")
    if spec is None:
        return False, f"unknown_kind:{kind}"
    if role not in spec["roles"]:
        return False, f"{kind}_cannot_fill:{role}"
    return True, ""


def slices_for(kind: str, role: str, entitled: tuple[str, ...] | list[str]) -> list[str]:
    """the intersection of the role's entitlement and the kind's ceiling, in the kind's
    order. a seat never gets a slice its role does not carry, and never one its kind
    does not read."""
    kind = canonical(kind)
    spec = KINDS.get(kind or "general")
    if spec is None:
        raise ValueError(f"unknown agent kind '{kind}'")
    return [name for name in spec["slices"] if name in entitled]


def describe() -> dict:
    return {kind: {"roles": list(spec["roles"]), "slices": list(spec["slices"]),
                   "note": spec["note"]} for kind, spec in KINDS.items()}
