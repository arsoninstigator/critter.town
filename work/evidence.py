"""what can prove a card done: the kind of work decides the scheme.

a review gate that accepts a sentence is a gate that accepts a sentence. the first
version of this board could sign off a commit with "looks fine", and nothing
objected, because the note was the evidence. so the kind of work now decides which
kinds of reference can close a card: code wants a commit or a run, financial wants a
receipt from the provider that actually holds the money, research wants a sheet or a
query or a source, support wants a ticket.

a crew may narrow the list for its own kinds (a crew that closes code cards only on a
commit, never on a bare run), and may never widen it past what the kind allows. that
direction is the whole point: the widening is how a gate quietly stops meaning
anything, and it is the direction an agent under pressure would ask for.

the refusal names the scheme it wanted, because "evidence rejected" sends a person
looking in the wrong place.
"""

from __future__ import annotations

import re

# the axis the board was missing: every gate, every evidence scheme and every
# acceptance rule had been written for code and silently applied to a deal.
KINDS = (
    "code", "design", "research", "security", "financial",
    "media", "ops", "accounting", "support", "doc",
)

# a gate is the one scheme that closes a card of any kind, because a recorded human
# decision is a row with who, when and why in it, not a sentence. it is a pointer, and
# the pointer is checkable, which is the whole test for whether evidence counts.
UNIVERSAL = ("gate",)

KIND_EVIDENCE: dict[str, tuple[str, ...]] = {
    "code": ("commit", "run"),
    "design": ("file", "figma", "penpot", "artifact"),
    "research": ("sheet", "sql", "source", "chart"),
    "security": ("scan", "audit"),
    "financial": ("stripe", "payout", "invoice"),
    "media": ("asset", "render"),
    "ops": ("run", "deploy", "ticket"),
    "accounting": ("ledger", "sheet"),
    "support": ("ticket",),
    "doc": ("url", "artifact"),
}

# a reference is a scheme and a payload, never a sentence. `commit:abc1234`,
# `run:run_9`, `invoice:inv_88`. the colon is what makes it checkable.
SCHEME = re.compile(r"^([a-z]+):\S")


def scheme_of(reference: str) -> str:
    """the scheme of a reference, or an empty string when it is prose."""
    match = SCHEME.match((reference or "").strip())
    return match.group(1) if match else ""


def allowed_for(kind: str, narrow: list[str] | None = None) -> tuple[str, ...]:
    """what this crew accepts for this kind: the default, or a narrower list.

    a narrowing that is not a subset is not a narrowing. it is refused rather than
    silently widened, because the widening is the failure this function exists for.
    """
    universal = KIND_EVIDENCE.get(kind or "code", ()) + UNIVERSAL
    if not narrow:
        return universal
    kept = tuple(scheme for scheme in narrow if scheme in universal)
    if len(kept) != len(narrow):
        extra = sorted(set(narrow) - set(universal))
        raise ValueError(f"{kind} cannot accept {extra}: a crew may narrow, never widen")
    return kept or universal


def evidence_fits(kind: str, reference: str,
                  narrow: list[str] | None = None) -> tuple[bool, str]:
    """(ok, reason). the reason is a sentence a person can act on."""
    scheme = scheme_of(reference)
    if not scheme:
        return False, (
            "evidence must be a reference like commit:abc1234, run:run_9 or "
            f"ticket:123, not a sentence ({reference[:40]!r})"
        )
    if kind not in KIND_EVIDENCE:
        return False, f"unknown kind {kind!r}; one of {list(KINDS)}"
    allowed = allowed_for(kind, narrow)
    if scheme not in allowed:
        return False, (
            f"a {kind} card is closed with {' or '.join(s + ':' for s in allowed)}, "
            f"not {scheme}:"
        )
    return True, ""


def expected(kind: str, narrow: list[str] | None = None) -> list[str]:
    """the schemes a card of this kind accepts, for a message or a form."""
    return list(allowed_for(kind, narrow))
