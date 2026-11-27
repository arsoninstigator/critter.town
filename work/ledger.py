"""the decisions a crew builds on, as rows a card can be checked against.

the failure this exists for is the quiet one: a decision is made in a call, written in a doc,
and then contradicted three weeks later by work nobody thought to compare it with. the code
still runs, no test fails, and the only place the disagreement lives is a person's memory. the
six shapes it takes are the ones prelint names, and they are not about code: a pricing rule
changes, a compliance obligation is skipped, a second vendor appears beside the first, the same
entity is called merchant in one place and seller in another, a feature nobody asked for gets
built, an internal tool grows a public API. every one of those is a decision that drifted from
the record, and every one of them can happen in research, operations or writing as easily as in
a repository.

so a statement is a claim with an owner and a date, not a document. it declares which of the six
kinds of drift it belongs to, which subject it is about, where it came from, and whether a person
or an agent decided it. two active statements on the same subject that disagree are a
contradiction, and a contradiction is computed rather than judged: same declared subject, both
active, different text. that means the ledger can say "these two disagree" without a model and
without pretending it understood the prose.

three rules are deliberate:

- an agent may propose, only a person settles. a claim an agent made about what the product is
  is a proposal until someone answers for it, and a ledger where the machine's guesses count as
  settled facts is worse than no ledger.
- a contradiction is not resolved by silence. both sides are marked contradicting, and the way
  out is one act: supersede the loser with the winner, or retire it with a reason.
- the check never stops work by default. a card is dispatched with what the ledger says about it
  attached to the run, because a false match on a word must not cost a dispatch; a crew that
  wants the halt asks for it.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from core.db import Store, dumps, loads, new_id, now
from core.errors import NotAllowed, NotFound, ValidationError

# the six ways work drifts from the record. each is a kind of statement, so the ledger answers
# "what did we settle about pricing" and "which compliance claims contradict each other" with
# the same query.
DRIFT = ("logic", "compliance", "tooling", "language", "scope", "strategy")

# settled: a person answered for it. provisional: an agent proposed it. contradicting: another
# active statement on the same subject says something different.
CERTAINTY = ("settled", "provisional", "contradicting")
STATUSES = ("active", "superseded", "retired")
SOURCE_KINDS = ("person", "agent", "doc", "card", "call", "note")

# words too common to carry a subject. short words are dropped by length instead of by list, so
# this stays a stoplist and not a vocabulary.
STOPWORDS = frozenset("""
about above after again against also among another because before being below between both
could does doing down during each every from further have having here into itself just more
most much must only other over same should some such than that their them then there these
they this those through under until very what when where which while will with within would
your ours yours
""".split())

MAX_TEXT = 2000


def normalise(text: str) -> str:
    """one line, whitespace collapsed. a statement is a sentence, not a document."""
    return " ".join(str(text or "").split())


def terms(text: str) -> set[str]:
    """the words that could carry a subject, for matching a card against the ledger.

    a trailing s is dropped so `invoice` and `invoices` are the same subject, which is the one
    piece of stemming worth having here: plurals are how people name things, and a matcher that
    cannot see past them misses every claim written in the plural.
    """
    cleaned = []
    for word in normalise(text).lower().replace("_", " ").split():
        word = word.strip(".,;:!?()[]{}'\"`")
        if len(word) < 4 or word in STOPWORDS:
            continue
        if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        cleaned.append(word)
    return set(cleaned)


def shared_terms(a: str, b: str) -> set[str]:
    """the words two pieces of text have in common, which is a hint and not a finding."""
    return terms(a) & terms(b)


def strong(shared: set[str]) -> bool:
    """whether the overlap is worth naming.

    two shared words is a match, and so is one long one: `invoice` carries more than `days`
    does, and a rule that only counted the number of words would need a third to see it.
    """
    return len(shared) >= 2 or any(len(word) >= 6 for word in shared)


def normalise_subject(subject: str) -> str:
    """a subject is a handle two statements can share: lowercase, dots kept, spaces become dots.

    it is declared, not derived. deriving it would make two statements about the same thing
    agree by accident of wording, and a contradiction found that way is a guess wearing a
    finding's clothes.
    """
    return ".".join(part for part in str(subject or "").lower().strip().split() if part)


def digest_of(text: str) -> str:
    """the claim itself, so a restatement with a full stop is the same claim.

    punctuation at the edges is not a disagreement, and neither is capitalisation. the middle
    of the sentence is left alone: `30 days` and `60 days` have to stay different.
    """
    canonical = normalise(text).lower().strip(" .,;:!?")
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def contradicts(candidate: dict, others: list[dict]) -> list[str]:
    """which active statements this one disagrees with, by subject and not by meaning."""
    subject = normalise_subject(candidate.get("subject"))
    if not subject:
        return []
    mine = digest_of(candidate.get("text", ""))
    found = []
    for other in others:
        if str(other.get("status")) != "active":
            continue
        if str(other.get("id")) == str(candidate.get("id")):
            continue
        if normalise_subject(other.get("subject")) != subject:
            continue
        if digest_of(other.get("text", "")) != mine:
            found.append(other["id"])
    return sorted(found)


def relevant(rows: list[dict], subjects: list[str] | None = None,
             text: str = "", limit: int = 20) -> list[dict]:
    """the statements that bear on a piece of work, and why each one matched.

    a declared subject is a fact about the work; a shared word is a hint. both are reported, and
    the reason travels with the row so a person can tell one from the other.
    """
    wanted = [normalise_subject(s) for s in (subjects or []) if normalise_subject(s)]
    wanted_terms = terms(text)
    scored: list[dict] = []
    for row in rows:
        if str(row.get("status")) != "active":
            continue
        why = ""
        if wanted and normalise_subject(row.get("subject")) in wanted:
            why = "subject"
        else:
            shared = wanted_terms & terms(row.get("text", ""))
            if strong(shared):
                why = "terms:" + ",".join(sorted(shared)[:4])
        if not why:
            continue
        scored.append({**row, "matched": why,
                       "weight": 2 if why == "subject" else 1})
    scored.sort(key=lambda r: (-r["weight"], str(r.get("created_date") or 0)))
    return scored[:int(limit)]


def verdict(found: list[dict]) -> str:
    """what a card should do about what the ledger says.

    blocked is reserved for a contradiction on a subject the work declares, because that is the
    one case where building on the ledger would build on a disagreement. everything else is a
    question at most: a provisional statement is somebody's proposal, not a rule.
    """
    for row in found:
        if row.get("matched") == "subject" and row.get("certainty") == "contradicting":
            return "blocked"
    # a contradiction found by wording alone is not proof the card touches it, but it is a
    # reason to ask rather than to build
    if any(row.get("certainty") in ("contradicting", "provisional") for row in found):
        return "ask"
    return "clear"


ADVICE = {
    "clear": "the ledger has nothing contested about this work",
    "ask": "the ledger holds a proposal about this work: ask before building on it",
    "blocked": "two settled claims disagree on a subject this work declares: settle it first",
}


class Ledger:
    def __init__(self, store: Store, crews: Any = None, kanban: Any = None,
                 audit: Any = None):
        self.store = store
        self.crews = crews
        self.kanban = kanban
        self.audit = audit

    # ---- rows ----------------------------------------------------------
    def _row(self, row) -> dict:
        return {
            "id": row["id"],
            "team_id": row["team_id"],
            "text": row["text"],
            "drift": row["drift"],
            "subject": row["subject"],
            "certainty": row["certainty"],
            "source": row["source"],
            "source_kind": row["source_kind"],
            "decided_by": row["decided_by"],
            "decided_by_kind": row["decided_by_kind"],
            "contradicts": loads(row["contradicts"], []),
            "supersedes": row["supersedes"] or "",
            "status": row["status"],
            "note": row["note"] or "",
            "digest": row["digest"],
            "created_date": row["created_date"],
            "updated_date": row["updated_date"],
        }

    def get(self, statement_id: str) -> dict:
        row = self.store.one("select * from statements where id = ?", (statement_id,))
        if not row:
            raise NotFound(f"statement {statement_id} not found")
        return self._row(row)

    def statements(self, team_id: str, drift: str = "", status: str = "active",
                   limit: int = 100) -> list[dict]:
        if drift and drift not in DRIFT:
            raise ValidationError(f"drift must be one of {list(DRIFT)}", reason="unknown_drift")
        if status and status not in STATUSES:
            raise ValidationError(f"status must be one of {list(STATUSES)}",
                                  reason="unknown_status")
        clauses, args = ["team_id = ?"], [team_id]
        if drift:
            clauses.append("drift = ?")
            args.append(drift)
        if status:
            clauses.append("status = ?")
            args.append(status)
        rows = self.store.query(
            f"select * from statements where {' and '.join(clauses)}"
            " order by created_date desc limit ?", tuple(args) + (int(limit),))
        return [self._row(row) for row in rows]

    # ---- writing -------------------------------------------------------
    def _is_agent(self, actor: str) -> bool:
        return bool(self.store.one("select 1 from agents where id = ?", (actor,)))

    def propose(self, team_id: str, text: str, drift: str = "logic", subject: str = "",
                source: str = "", source_kind: str = "note", actor: str = "owner",
                supersedes: str = "") -> dict:
        """write a claim down. an agent's claim is a proposal; a person's is settled."""
        if self.crews is not None:
            self.crews.get_team(team_id)
        clean = normalise(text)
        if not clean:
            raise ValidationError("a statement needs text", reason="text_required")
        if len(clean) > MAX_TEXT:
            raise ValidationError(f"a statement is at most {MAX_TEXT} characters",
                                  reason="text_too_long")
        if drift not in DRIFT:
            raise ValidationError(
                f"a statement belongs to one of the six kinds of drift: {list(DRIFT)}",
                reason="unknown_drift")
        if source_kind not in SOURCE_KINDS:
            raise ValidationError(f"source_kind must be one of {list(SOURCE_KINDS)}",
                                  reason="unknown_source_kind")
        subject = normalise_subject(subject)
        agent_made = self._is_agent(actor)
        certainty = "provisional" if agent_made else "settled"
        older: dict | None = None
        if supersedes:
            older = self.get(supersedes)
            if older["team_id"] != team_id:
                raise ValidationError("a statement cannot supersede one from another crew",
                                      reason="supersedes_another_crew")
            if older["status"] != "active":
                raise ValidationError(
                    f"statement {supersedes} is already {older['status']}",
                    reason="already_resolved")
            # the replacement answers for the old one, so it inherits the subject when none was
            # given: a supersede is one act and should not need the handle typed twice
            subject = subject or normalise_subject(older["subject"])
        candidate = {"text": clean, "subject": subject, "status": "active", "id": ""}
        against = [] if older is not None else contradicts(candidate, self.statements(
            team_id, status="active", limit=1000))
        statement_id = new_id("stm")
        ts = now()
        with self.store.tx() as c:
            c.execute(
                """insert into statements (id, team_id, text, drift, subject, certainty, source,
                       source_kind, decided_by, decided_by_kind, contradicts, supersedes, status,
                       note, digest, created_date, updated_date)
                   values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (statement_id, team_id, clean, drift, subject,
                 "contradicting" if against else certainty, source, source_kind, actor,
                 "agent" if agent_made else "person", dumps(against), supersedes or "",
                 "active", "", digest_of(clean), ts, ts))
            for other_id in against:
                # both sides are marked. a contradiction that only one side knows about is a
                # warning the other half of the crew will never see
                other = self.get(other_id)
                both = sorted(set(other["contradicts"]) | {statement_id})
                c.execute(
                    "update statements set certainty = 'contradicting', contradicts = ?,"
                    " updated_date = ? where id = ?",
                    (dumps(both), ts, other_id))
            if older is not None:
                c.execute(
                    "update statements set status = 'superseded', note = ?, updated_date = ?"
                    " where id = ?",
                    (f"superseded by {statement_id}", ts, supersedes))
        return self.get(statement_id)

    def settle(self, statement_id: str, actor: str = "owner", note: str = "") -> dict:
        """a person answers for it. the only way a claim stops being a proposal."""
        row = self.get(statement_id)
        if row["status"] != "active":
            raise ValidationError(f"statement {statement_id} is {row['status']}",
                                  reason="already_resolved")
        if self._is_agent(actor):
            raise NotAllowed("an agent cannot settle a statement: it may propose one",
                             reason="agent_cannot_settle", statement_id=statement_id)
        ts = now()
        with self.store.tx() as c:
            c.execute(
                "update statements set certainty = ?, decided_by = ?, decided_by_kind ="
                " 'person', note = ?, updated_date = ? where id = ?",
                ("settled", actor, normalise(note), ts, statement_id))
            for other_id in row["contradicts"]:
                other = self.get(other_id)
                if other["status"] != "active":
                    continue
                # settling one side does not end a disagreement, it names a winner. the loser
                # stays marked until somebody retires or supersedes it, on purpose: a
                # contradiction that disappears because one side got louder is how a drift
                # gets buried
                c.execute(
                    "update statements set note = ?, updated_date = ? where id = ?",
                    (f"contradicts {statement_id}, which a person settled", ts, other_id))
        return self.get(statement_id)

    def retire(self, statement_id: str, reason: str = "", actor: str = "owner") -> dict:
        row = self.get(statement_id)
        if row["status"] == "retired":
            raise ValidationError("that statement is already retired", reason="already_resolved")
        ts = now()
        with self.store.tx() as c:
            c.execute(
                "update statements set status = 'retired', note = ?, updated_date = ?"
                " where id = ?", (normalise(reason) or "retired", ts, statement_id))
            # a retired claim stops contradicting anything, so the other side is cleared
            for other_id in row["contradicts"]:
                other = self.get(other_id)
                remaining = [i for i in other["contradicts"] if i != statement_id]
                certainty = other["certainty"]
                if not remaining and certainty == "contradicting":
                    certainty = "settled" if other["decided_by_kind"] == "person" \
                        else "provisional"
                c.execute(
                    "update statements set contradicts = ?, certainty = ?, updated_date = ?"
                    " where id = ?", (dumps(remaining), certainty, ts, other_id))
        return self.get(statement_id)

    # ---- reading, which is the point ------------------------------------
    def contradictions(self, team_id: str) -> list[dict]:
        rows = [row for row in self.statements(team_id, limit=1000)
                if row["certainty"] == "contradicting"]
        return rows

    def check(self, team_id: str, subjects: list[str] | None = None, text: str = "",
              task_id: str = "", actor: str = "", record: bool = True) -> dict:
        """what the ledger says about a piece of work, and whether it should be asked about."""
        found = relevant(self.statements(team_id, limit=1000), subjects, text)
        state = verdict(found)
        answer = {
            "team_id": team_id,
            "task_id": task_id,
            "verdict": state,
            "advice": ADVICE[state],
            "subjects": [normalise_subject(s) for s in (subjects or []) if normalise_subject(s)],
            "found": [{k: row[k] for k in ("id", "text", "drift", "subject", "certainty",
                                           "source", "decided_by_kind", "matched")}
                      for row in found],
            "contested": [row["id"] for row in found
                          if row["certainty"] == "contradicting"],
        }
        if record:
            with self.store.tx() as c:
                c.execute(
                    """insert into statement_checks (id, team_id, task_id, subjects, verdict,
                           found, actor, created_date) values (?,?,?,?,?,?,?,?)""",
                    (new_id("stc"), team_id, task_id, dumps(answer["subjects"]), state,
                     dumps([row["id"] for row in found]), actor, now()))
        return answer

    def subjects_of(self, task: dict) -> list[str]:
        """what a card declares it is about. declared, not inferred from its prose."""
        payload = task.get("payload") or {}
        declared = payload.get("subjects") if isinstance(payload, dict) else None
        if isinstance(declared, str):
            declared = [declared]
        return [normalise_subject(s) for s in (declared or []) if normalise_subject(s)]

    def for_card(self, task_id: str, actor: str = "") -> dict:
        task = self.kanban.get_task(task_id) if self.kanban is not None else None
        if task is None:
            row = self.store.one("select * from tasks where id = ?", (task_id,))
            if not row:
                raise NotFound(f"card {task_id} not found")
            task = {"id": row["id"], "team_id": row["team_id"], "title": row["title"],
                    "description": row["description"], "payload": loads(row["payload"], {})}
        task = dict(task)
        payload = task.get("payload") or {}
        if isinstance(payload, str):
            payload = loads(payload, {})
        task["payload"] = payload
        return self.check(
            task["team_id"], subjects=self.subjects_of(task),
            text=f"{task.get('title') or ''} {task.get('description') or ''}",
            task_id=task["id"], actor=actor)

    def for_dispatch(self, task: dict, agent: dict, strict: bool = False) -> dict:
        """the same check at the moment a card is handed over, recorded rather than fatal."""
        task = dict(task)
        agent = dict(agent)
        if not task.get("id") or not task.get("team_id"):
            return {"verdict": "clear", "found": [], "advice": ADVICE["clear"]}
        payload = task.get("payload") or {}
        if isinstance(payload, str):
            payload = loads(payload, {})
        task["payload"] = payload
        checked = self.check(
            task["team_id"], subjects=self.subjects_of(task),
            text=f"{task.get('title') or ''} {task.get('description') or ''}",
            task_id=task["id"], actor=str(agent.get("id") or ""))
        if checked["verdict"] == "blocked" and self.audit is not None:
            self.audit.record_event("ledger.contradiction", {
                "team_id": task["team_id"],
                "agent_id": agent.get("id") or "",
                "task": task,
                "data": {"task_id": task["id"], "contested": checked["contested"][:5]},
            })
        if strict and checked["verdict"] == "blocked":
            raise ValidationError(
                "two claims disagree on a subject this card declares; settle the ledger before"
                f" the work builds on it ({', '.join(checked['contested'][:3])})",
                reason="ledger_contradiction", contested=checked["contested"][:5])
        return checked
