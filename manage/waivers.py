"""a beat a card did not walk, written down.

darkrun's `IterationResult::Skip` exists because the alternative is silence: a worker that
never ran was indistinguishable from one not yet reached, so a unit could jump the middle of
its own pipeline and still be marked complete. critter.town has the same silence in one
place. review is a column, and an internal service may move a card out of it, so a card on a
reviewed crew can reach done with nobody having looked and nothing on the record saying so.

a waiver is the record. it names the beat, says why in a sentence, and is written against the
card, so the move that would otherwise be silent is a decision someone made. the reason is
required and has a floor, because "n/a" is not a waiver, it is the silence with a word in it.
"""

from __future__ import annotations

from typing import Any

from core.db import Store, new_id, now
from core.errors import NotFound, ValidationError

BEATS = ("review",)
MIN_REASON = 20


def check(beat: str, reason: str) -> str:
    """the reasons a waiver is refused, all of them about the writing and none about the
    judgement: whether review was worth skipping is not a validator's call."""
    beat = (beat or "").strip()
    if beat not in BEATS:
        raise ValidationError(f"a waiver names one of {list(BEATS)}", beat=beat or None)
    text = (reason or "").strip()
    if len(text) < MIN_REASON:
        raise ValidationError(
            f"a waiver says why, in at least {MIN_REASON} characters: an unrecorded skip is "
            "indistinguishable from a beat that never ran",
            reason="reason_too_thin", chars=len(text))
    return text


class Waiver:
    """the rows, and the one question the board asks: was this beat waived?"""

    def __init__(self, store: Store):
        self.store = store

    def grant(self, task: dict, beat: str, reason: str, by: str = "") -> dict:
        text = check(beat, reason)
        existing = self.for_task(task["id"], beat=beat)
        if existing:
            # a second waiver for one beat is a second opinion about a decision already
            # made; the first one stands and the reason is not overwritten
            raise ValidationError("that beat is already waived on this card",
                                  beat=beat, waiver=existing[0]["id"])
        waiver_id = new_id("waive")
        with self.store.tx() as c:
            c.execute(
                """insert into waivers (id, team_id, task_id, beat, reason, waived_by,
                       created_date) values (?,?,?,?,?,?,?)""",
                (waiver_id, task["team_id"], task["id"], beat.strip(), text,
                 (by or "owner").strip(), now()),
            )
        return self.get(waiver_id)

    def get(self, waiver_id: str) -> dict:
        row = self.store.one("select * from waivers where id = ?", (waiver_id,))
        if row is None:
            raise NotFound(f"no waiver {waiver_id}")
        return dict(row)

    def for_task(self, task_id: str, beat: str = "") -> list[dict]:
        if beat:
            rows = self.store.query(
                "select * from waivers where task_id = ? and beat = ? order by created_date",
                (task_id, beat))
        else:
            rows = self.store.query(
                "select * from waivers where task_id = ? order by created_date", (task_id,))
        return [dict(row) for row in rows]

    def has(self, task_id: str, beat: str) -> bool:
        return bool(self.for_task(task_id, beat=beat))

    def for_crew(self, team_id: str, limit: int = 50) -> list[dict]:
        rows = self.store.query(
            """select * from waivers where team_id = ?
               order by created_date desc limit ?""", (team_id, limit))
        return [dict(row) for row in rows]


def may_leave_review(task: dict, actor: str, internal: set[str], waiver: Any) -> None:
    """the rule the board applies, kept here so it can be read without the board.

    a person signing is a signature. an agent that is not the assignee is a signature, and
    the board refuses the assignee before this is reached. what is left is the crew's own
    services, and those may only close a reviewed card if someone wrote down why not.
    """
    actor = (actor or "").strip()
    if actor == "owner":
        return
    if actor not in internal and not actor.startswith("guardrail:"):
        return
    if waiver is not None and waiver.has(task["id"], "review"):
        return
    raise ValidationError(
        "review is a beat: a reviewed card leaves review with a reviewer, the owner, or a "
        "written waiver",
        task_id=task["id"], actor=actor, reason="review_not_signed")
