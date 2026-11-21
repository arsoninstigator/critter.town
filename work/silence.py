"""a card that is held, alive, and producing nothing.

the sweep already catches three ways a card rots: a dead holder, a card nobody can
take, and a holder that renews forever without the work moving. the last one counts
renewals, which is a slow signal: with a fifteen minute lease, three renewals is
forty five minutes of nothing, and a seat that renews while its model hangs looks
exactly like a seat that is working.

this is the faster signal, and it is about output rather than about the lease. a run
that has written nothing for five minutes is worth a look; fifteen minutes and it is
an outage nobody has been told about. the clock starts at the last output, then at
the moment the lease was taken, then at the card's creation, so a card that has never
produced anything is measured from when it became work.

three rules keep it from becoming noise:

  - it only applies to a card somebody is actually holding, because a card in the
    backlog being quiet is the whole point of a backlog.
  - a person can snooze it, continue it for half an hour, or call it a false
    positive. a machine that can only escalate is a machine people learn to ignore.
  - a recovery card that goes silent does not file another recovery card. cascades
    are how a watchdog becomes the outage.

the clock is a pure function of the row and the time. the escalation is a service,
because filing a card is a board move and board moves are audited.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

from core.db import now as clock
from core.errors import NotAllowed, NotFound, ValidationError

SUSPICION_SECONDS = 5 * 60
CRITICAL_SECONDS = 15 * 60
# "continue" is not "never again": it buys half an hour and then the clock resumes
REARM_SECONDS = 30 * 60

LEVELS = ("not_applicable", "ok", "snoozed", "suspicious", "critical")
DECISIONS = ("snooze", "continue", "dismissed_false_positive")
HOLDING_COLUMNS = {"in_progress"}


def silence_started_at(card: dict) -> float | None:
    """the last output, then when the lease was taken, then when the card was made.

    the order matters: a card claimed ten minutes ago whose holder wrote two minutes
    ago is fine, and measuring from the claim would call it silent.
    """
    for key in ("last_output_at", "lease_started_at", "created_date"):
        value = card.get(key)
        # a stored zero is a real time in a test and a NULL is the unset case in a row
        if value is not None:
            return float(value)
    return None


def silence_age(card: dict, now: float | None = None) -> float | None:
    started = silence_started_at(card)
    if started is None:
        return None
    return max(0.0, (clock() if now is None else now) - started)


def is_held(card: dict) -> bool:
    return (card.get("column") or card.get("column_name")) in HOLDING_COLUMNS \
        and bool(card.get("lease_holder"))


def recovery_origin(card: dict) -> bool:
    """a card filed by this watchdog is not watched by it."""
    payload = _payload(card.get("payload"))
    return bool(payload.get("recovery_of") or payload.get("kind") == "recovery")


def suppression(card: dict, decision: dict | None = None,
                now: float | None = None) -> str | None:
    """why this card is not worth a signal, in the order that resolves it.

    the order is fixed so two callers never disagree: a dismissed card is not
    suspicious at all, a snoozed one is suspicious but quiet, and a recovery card or
    a blocked card is quiet on purpose.
    """
    now = clock() if now is None else now
    if decision:
        if decision.get("decision") == "dismissed_false_positive":
            return "dismissed_false_positive"
        until = float(decision.get("snoozed_until") or 0)
        if until > now:
            return "snoozed"
    if recovery_origin(card):
        return "recovery_origin"
    if (card.get("column") or card.get("column_name")) == "blocked":
        return "blocked_source"
    return None


def classify(card: dict, now: float | None = None, decision: dict | None = None,
             suspicion: float = SUSPICION_SECONDS,
             critical: float = CRITICAL_SECONDS) -> str:
    now = clock() if now is None else now
    if not is_held(card):
        return "not_applicable"
    if decision and decision.get("decision") == "dismissed_false_positive":
        return "not_applicable"
    if decision and float(decision.get("snoozed_until") or 0) > now:
        return "snoozed"
    age = silence_age(card, now) or 0.0
    if age >= critical:
        return "critical"
    if age >= suspicion:
        return "suspicious"
    return "ok"


def decision_window(decision: str, now: float | None = None,
                    snoozed_until: float | None = None,
                    rearm: float = REARM_SECONDS) -> float | None:
    """how long a decision keeps the watchdog quiet.

    a snooze must say until when, because an open ended snooze is a mute with a
    friendly name. a continue buys the rearm window, or the caller's later time if
    they asked for one.
    """
    if decision not in DECISIONS:
        raise ValidationError(f"decision must be one of {list(DECISIONS)}",
                              decision=decision)
    now = clock() if now is None else now
    if decision == "dismissed_false_positive":
        return None
    if decision == "snooze":
        if snoozed_until is None:
            raise ValidationError("a snooze must say until when",
                                  reason="snooze_without_until")
        if float(snoozed_until) <= now:
            raise ValidationError("a snooze must end in the future",
                                  reason="snooze_in_the_past")
        return float(snoozed_until)
    if snoozed_until is not None and float(snoozed_until) > now:
        return float(snoozed_until)
    return now + rearm


def may_decide(actor: str, card: dict, recovery_card: dict | None = None) -> tuple[bool, str]:
    """the board, or the seat that was handed the recovery.

    a stranger answering for a silent run is how a watchdog gets silenced by the
    thing it is watching.
    """
    if actor in {"owner", "human", "board"}:
        return True, ""
    if recovery_card and recovery_card.get("assignee_agent_id") == actor \
            and recovery_card.get("watchdog_of") == card.get("id"):
        return True, ""
    return False, "not_authorized"


def should_fold(card: dict, evidence_after: float | None) -> bool:
    """a card that finished after the silence started is late, not stuck.

    the sweep already leaves done cards alone, so this is the rule stated where the
    watchdog can read it: terminal work with evidence newer than the silence is a
    folded run, and filing a recovery card for it would be filing one for success.
    """
    column = card.get("column") or card.get("column_name")
    if column != "done":
        return False
    started = silence_started_at(card)
    if evidence_after is None or started is None:
        return False
    return float(evidence_after) >= started


def summary(card: dict, now: float | None = None, decision: dict | None = None,
            suspicion: float = SUSPICION_SECONDS,
            critical: float = CRITICAL_SECONDS) -> dict:
    now = clock() if now is None else now
    age = silence_age(card, now)
    until = float(decision.get("snoozed_until") or 0) if decision else 0.0
    return {
        "task_id": card.get("id"),
        "holder": card.get("lease_holder") or "",
        "last_output_at": card.get("last_output_at") or None,
        "output_seq": int(card.get("output_seq") or 0),
        "silence_started_at": silence_started_at(card),
        "silence_age": age,
        "level": classify(card, now, decision, suspicion, critical),
        "suppressed": suppression(card, decision, now),
        "suspicion_seconds": suspicion,
        "critical_seconds": critical,
        "snoozed_until": until or None,
    }


class SilenceService:
    """output as a heartbeat, and a watchdog that asks a person before it acts twice."""

    def __init__(self, store: Any, kanban: Any = None, audit: Any = None,
                 suspicion: float = SUSPICION_SECONDS,
                 critical: float = CRITICAL_SECONDS,
                 rearm: float = REARM_SECONDS):
        self.store = store
        self.kanban = kanban
        self.audit = audit
        self.suspicion = suspicion
        self.critical = critical
        self.rearm = rearm

    # -- rows -----------------------------------------------------------------
    def _card(self, task_id: str) -> dict:
        row = self.store.one("select * from tasks where id = ?", (task_id,))
        if row is None:
            raise NotFound(f"task {task_id} not found")
        card = dict(row)
        card["column"] = card.pop("column_name")
        card["payload"] = _payload(card.get("payload"))
        return card

    def _cards(self, team_id: str) -> list[dict]:
        rows = self.store.query(
            "select * from tasks where team_id = ? and column_name != 'done' order by id",
            (team_id,))
        cards = []
        for row in rows:
            card = dict(row)
            card["column"] = card.pop("column_name")
            card["payload"] = _payload(card.get("payload"))
            cards.append(card)
        return cards

    def _decision(self, task_id: str, now: float | None = None) -> dict | None:
        row = self.store.one(
            """select * from silence_decisions where task_id = ?
               order by created_date desc, rowid desc limit 1""", (task_id,))
        return dict(row) if row is not None else None

    # -- output ---------------------------------------------------------------
    def record_output(self, task_id: str, agent_id: str, stream: str = "stdout",
                      size: int = 0) -> dict:
        """the holder says it produced something. nobody else may say it for them."""
        card = self._card(task_id)
        if not card.get("lease_holder") or card["lease_holder"] != agent_id:
            raise NotAllowed("only the holder of this card may report its output",
                             task_id=task_id, reason="not_the_holder")
        ts = clock()
        with self.store.tx() as c:
            c.execute(
                """update tasks set last_output_at = ?, output_seq = output_seq + 1,
                   updated_date = ? where id = ?""",
                (ts, ts, task_id))
        if self.kanban is not None:
            self.kanban._event(task_id, "output", agent_id,
                               {"stream": stream, "size": int(size)})
        return {"task_id": task_id, "last_output_at": ts,
                "output_seq": int(card.get("output_seq") or 0) + 1}

    # -- watching -------------------------------------------------------------
    def watch(self, task_id: str, now: float | None = None) -> dict:
        card = self._card(task_id)
        return summary(card, now, self._decision(task_id), self.suspicion, self.critical)

    def state(self, team_id: str, now: float | None = None) -> dict:
        now = clock() if now is None else now
        cards = [summary(card, now, self._decision(card["id"]),
                         self.suspicion, self.critical)
                 for card in self._cards(team_id)]
        counts: dict[str, int] = {}
        for card in cards:
            counts[card["level"]] = counts.get(card["level"], 0) + 1
        return {"cards": cards, "counts": counts,
                "suspicion_seconds": self.suspicion,
                "critical_seconds": self.critical}

    def decide(self, task_id: str, decision: str, actor: str = "owner",
               reason: str = "", snoozed_until: float | None = None) -> dict:
        """a person answering the watchdog, recorded as a row rather than as silence."""
        card = self._card(task_id)
        recovery = self._recovery_card(task_id)
        allowed, why = may_decide(actor, card, recovery)
        if not allowed:
            raise NotAllowed("only the board or the assigned recovery owner may "
                             "answer the watchdog", task_id=task_id, reason=why)
        ts = clock()
        until = decision_window(decision, ts, snoozed_until, self.rearm)
        decision_id = _new_id()
        with self.store.tx() as c:
            c.execute(
                """insert into silence_decisions (id, team_id, task_id, decision,
                       snoozed_until, reason, actor, recovery_task_id, created_date,
                       updated_date)
                   values (?,?,?,?,?,?,?,?,?,?)""",
                (decision_id, card["team_id"], task_id, decision, until, reason,
                 actor, (recovery or {}).get("id", ""), ts, ts))
        if self.audit is not None and card.get("lease_holder"):
            # the row hangs off the holder, because that is the seat the answer is about;
            # the actor is named in the row so a human answer is not read as the agent's
            self.audit.record(
                team_id=card["team_id"], agent_id=card["lease_holder"],
                action="silence.decided", actor_kind="human", actor_id=actor,
                target_kind="task", target_id=task_id,
                payload={"decision": decision, "snoozed_until": until,
                         "reason": reason})
        return {"id": decision_id, "task_id": task_id, "decision": decision,
                "snoozed_until": until, "actor": actor}

    def _recovery_card(self, task_id: str) -> dict | None:
        row = self.store.one(
            """select * from tasks where watchdog_of = ? and column_name != 'done'
               order by created_date desc limit 1""", (task_id,))
        return dict(row) if row is not None else None

    def escalate(self, team_id: str, now: float | None = None) -> dict:
        """file one recovery card per silent card, and never one for a recovery card."""
        now = clock() if now is None else now
        if self.kanban is None:
            raise ValidationError("escalation needs a board to file cards on",
                                  reason="no_kanban")
        created: list[dict] = []
        existing: list[str] = []
        skipped: list[dict] = []
        snoozed: list[str] = []
        for card in self._cards(team_id):
            decision = self._decision(card["id"])
            level = classify(card, now, decision, self.suspicion, self.critical)
            if level in {"not_applicable", "ok"}:
                continue
            why = suppression(card, decision, now)
            if why in {"snoozed", "dismissed_false_positive"}:
                snoozed.append(card["id"])
                continue
            if why in {"recovery_origin", "blocked_source"}:
                skipped.append({"task_id": card["id"], "why": why})
                continue
            if level == "suspicious":
                # a finding, not an act: five minutes is a look, not a rescue
                skipped.append({"task_id": card["id"], "why": "suspicious_only"})
                continue
            open_recovery = self._recovery_card(card["id"])
            if open_recovery is not None:
                existing.append(card["id"])
                continue
            age = int(silence_age(card, now) or 0)
            filed = self.kanban.create_task(
                team_id,
                f"a card went silent: {card['title']}",
                description=(f"{card['id']} has been held by {card['lease_holder']} "
                             f"and has written nothing for {age // 60} minutes. "
                             f"find out whether the holder is alive, and either "
                             f"recover the work or say what stopped it."),
                column="ready",
                kind="ops",
                payload={"kind": "recovery", "recovery_of": card["id"],
                         "silent_for": age},
                priority=2,
            )
            with self.store.tx() as c:
                c.execute("update tasks set watchdog_of = ? where id = ?",
                          (card["id"], filed["id"]))
            created.append({"task_id": card["id"], "recovery_task_id": filed["id"]})
            if self.audit is not None and card.get("lease_holder"):
                self.audit.record(
                    team_id=team_id, agent_id=card["lease_holder"],
                    action="silence.escalated", actor_kind="system",
                    actor_id="watchdog", target_kind="task", target_id=card["id"],
                    payload={"recovery_task_id": filed["id"], "silence_age": age})
        return {"created": created, "existing": existing,
                "snoozed": snoozed, "skipped": skipped}


def _payload(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _new_id() -> str:
    return "sil_" + secrets.token_hex(8)
