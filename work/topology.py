"""which shape the work needs, decided from what the card declares.

the failure this exists for is a crew that reaches for orchestration because
orchestration sounds like the serious option. the mindstudio piece names the cost
plainly (a coding harness built like an orchestrator is slow and expensive) and the
autogen paper, which it links, gives the matching rule in its own words: start with a
simple conversation topology, because two agents and a group chat extend with the least
code. autogen also lists the questions nobody has answered yet (how many agents, which
roles, how they interact, what to automate), and those are exactly the questions a
person answers badly at 1am and a rule answers the same way every time.

so this is a decision table over declared facts, not a model call. a model choosing its
own topology is the thing being avoided: it will pick the expensive shape and write a
confident paragraph about why. a rule picks the cheap shape and names the one fact that
made it cheap.

the signals, all declared by whoever opens the card:

  question   known     the inputs and the steps are known
             open      someone has to find out what the question even is
  shape      one       one item of work
             many      many structurally similar items
  parts      coupled   each step needs the step before it
             independent  parts can be worked apart and joined
  check      programmatic  a test, a metric or a schema decides
             judgement     a person decides
  stakes     reversible / irreversible  can this be undone
  seats      how many agents may work at once

the answers are five shapes, each of which is a thing this repo already has:

  research       an open question: one agent, retrieval, knowledge writes, a lesson
  factory        many similar items: a routine or trigger, a workflow, sampling review
  orchestration  independent parts: fan-out spawns, a convergence barrier, one review
  harness        coupled work with a programmatic check: one card, a sandbox, the tests
  solo           everything else: one card, one agent, no spawns

a gate is not a shape, it is an addition: irreversible work gets a human_gate before it
acts, whatever shape it is.
"""

from __future__ import annotations

from typing import Any

from core.errors import ValidationError

SHAPES = ("research", "factory", "orchestration", "harness", "solo")

# what implements each shape here, so the answer is actionable rather than a label
IMPLEMENTED_BY = {
    "research": ["card", "runner", "knowledge", "lesson"],
    "factory": ["routine or trigger", "workflow", "review sampling"],
    "orchestration": ["spawn fan-out", "convergence barrier", "review gate"],
    "harness": ["card", "sandbox", "programmatic check"],
    "solo": ["card", "runner"],
}

SIGNALS: dict[str, tuple[str, ...]] = {
    "question": ("known", "open"),
    "shape": ("one", "many"),
    "parts": ("coupled", "independent"),
    "check": ("programmatic", "judgement"),
    "stakes": ("reversible", "irreversible"),
}


def validate(signals: dict[str, Any]) -> list[str]:
    """the signals that are missing or wrong, named one at a time.

    a missing signal is not a default. guessing `parts: coupled` on work that was
    actually independent costs a crew its parallel lanes, and guessing the other way
    costs a convergence nobody can close.
    """
    problems: list[str] = []
    for name, allowed in SIGNALS.items():
        value = signals.get(name)
        if value is None:
            problems.append(f"missing:{name}")
        elif value not in allowed:
            problems.append(f"unknown:{name}={value}")
    seats = signals.get("seats")
    if seats is None:
        problems.append("missing:seats")
    elif not isinstance(seats, int) or isinstance(seats, bool) or seats < 1:
        problems.append(f"unknown:seats={seats}")
    return problems


def choose(signals: dict[str, Any]) -> dict:
    """the decision, with the one fact that decided it.

    precedence is deliberate and short. an open question beats a batch, because you
    cannot batch what you have not defined. a batch beats a fan-out, because many
    similar items want one shape repeated, not a crew assembled per item.
    """
    problems = validate(signals)
    if problems:
        # fail closed: no shape at all is better than an expensive guess
        return {"structure": "", "because": "signals_incomplete", "problems": problems,
                "implements": [], "gate": False}

    shape = "solo"
    because = "coupled_work_does_not_need_a_crew"

    if signals["question"] == "open":
        shape, because = "research", "an_open_question_is_discovery_before_it_is_work"
    elif signals["shape"] == "many":
        shape, because = "factory", "many_similar_items_want_one_shape_repeated"
    elif signals["parts"] == "independent" and signals["seats"] >= 2:
        shape, because = ("orchestration",
                          "independent_parts_with_a_second_seat_are_worth_joining")
    elif signals["parts"] == "independent":
        # the work could be split, but there is nobody to split it with
        shape, because = "solo", "independent_parts_but_no_second_seat"
    elif signals["check"] == "programmatic":
        shape, because = "harness", "coupled_work_with_a_programmatic_check_runs_itself"

    return {
        "structure": shape,
        "because": because,
        "problems": [],
        "implements": list(IMPLEMENTED_BY[shape]),
        "gate": signals["stakes"] == "irreversible",
        "gate_because": "irreversible_work_gets_a_person_before_it_acts"
        if signals["stakes"] == "irreversible" else "",
    }


def signals_of(task: dict) -> dict:
    """the signals a card declares, under `payload.signals`.

    an empty dict means the card said nothing, and saying nothing is not a failure:
    check_declared only compares when there is something to compare.
    """
    payload = task.get("payload") or {}
    if not isinstance(payload, dict):
        return {}
    signals = payload.get("signals")
    return dict(signals) if isinstance(signals, dict) else {}


def declared_structure(payload: dict | None) -> str:
    """the shape a card says it wants, if it says anything at all."""
    if not isinstance(payload, dict):
        return ""
    value = payload.get("structure")
    return value if isinstance(value, str) else ""


def check_declared(payload: dict | None, signals: dict[str, Any]) -> dict:
    """a card may declare its shape. it may not declare a more expensive one than its
    own signals justify.

    this is the one mismatch worth refusing rather than warning about: orchestration is
    the shape that costs seats, tokens and a convergence, and it is the shape a card
    asks for when the work feels big.
    """
    declared = declared_structure(payload)
    if not declared:
        return {"ok": True, "declared": "", "chosen": ""}
    if declared not in SHAPES:
        raise ValidationError(f"structure must be one of {list(SHAPES)}, not '{declared}'")
    decision = choose(signals)
    if decision["problems"]:
        # nothing to compare against, and refusing on a missing signal would punish the
        # card for a form the caller has not filled in yet
        return {"ok": True, "declared": declared, "chosen": ""}
    if declared == "orchestration" and decision["structure"] != "orchestration":
        raise ValidationError(
            f"this card declares orchestration, but its signals say "
            f"{decision['structure']} ({decision['because']})",
            declared=declared, chosen=decision["structure"], because=decision["because"],
        )
    return {"ok": True, "declared": declared, "chosen": decision["structure"]}
