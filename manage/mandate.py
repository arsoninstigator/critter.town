"""a mandate says how a seat works. a card says what the work is.

band's dark factory disqualifies an entry whose seat mandates name the problem's answer:
an endpoint path, a field name, an error code. their reasoning is the reason here too, and
it is not about the tournament. a mandate that names the answer is a script for one problem
wearing a role's clothes. it reads well, it works once, and the next card gets an agent
carrying the last card's details.

their lint is three identifier shapes and a vocabulary generated from the specs. the shapes
are the good half and they are kept: an endpoint, a snake_case identifier and a kebab-case
test id are the three ways a specific answer shows up in prose. the vocabulary is the half
that has to change. there are no specs here to generate one from, and an allowlist of banned
words would be a list of our own guesses. so the vocabulary is derived from the board: the
terms this card uses that no other card and no knowledge doc uses. that is what "specific to
this card" means, and it is checkable without anyone deciding in advance what the answer is.

what this deliberately does not do: it never refuses a dispatch. band's check disqualifies
because a tournament has to rank, and here a false positive would stop work over a word. a
finding names the line and the term and is recorded, so a reviewer can act on it and a
pattern across cards becomes visible. `strict` exists for a crew that wants the refusal.
"""

from __future__ import annotations

import re
from typing import Any

from core.db import Store, loads
from core.errors import NotFound, ValidationError

# identifier shapes only. prose, numbers and quoted fragments are not evidence of
# anything, and a check whose finding is a line number has to be something a person can
# look at and agree with. a path starts a token: in `import/export` the slash joins words.
SHAPES = (
    ("endpoint", re.compile(r"(?<![a-z0-9_])/[a-z][a-z0-9_]*(?:[/-][a-z0-9_{}]+)*")),
    ("identifier", re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")),
    ("test id", re.compile(r"\b[a-z][a-z0-9]*(?:-[a-z0-9]+)+\b")),
)

# the only shape that is evidence on its own. a path belongs to some api, never to a role,
# so it is worth naming even when the board is too thin to derive a vocabulary from.
ALWAYS = "endpoint"

# how much card and knowledge text the vocabulary is derived from. a card is small; a
# knowledge doc is not, and the vocabulary only needs the terms, not the argument.
CAP = 20000


def tokens_in(text: str) -> list[tuple[str, str]]:
    """(kind, term) for every identifier-shaped token in `text`, lowercased."""
    lowered = (text or "").lower()
    found: list[tuple[str, str]] = []
    for kind, pattern in SHAPES:
        found.extend((kind, term) for term in pattern.findall(lowered))
    return found


def distinctive(card: str, others: list[str]) -> list[str]:
    """the terms this card uses that nothing else in the crew uses.

    `others` is every other card and every knowledge doc, because both are read by every
    seat: a term that appears in either is a term a seat may reasonably know. what is left
    is this card's own vocabulary, and a mandate naming one of those is a mandate written
    for this card.
    """
    mine = {term for _, term in tokens_in(card)}
    theirs: set[str] = set()
    for text in others:
        theirs.update(term for _, term in tokens_in((text or "")[:CAP]))
    return sorted(mine - theirs)


def lint(text: str, terms: set[str] | list[str]) -> list[dict]:
    """findings, each naming the line, the kind and the term, so a person can fix the line."""
    vocabulary = {term.lower() for term in terms}
    findings: list[dict] = []
    seen: set[tuple[int, str]] = set()
    for number, line in enumerate((text or "").splitlines(), 1):
        for kind, term in tokens_in(line):
            if term not in vocabulary or (number, term) in seen:
                continue
            seen.add((number, term))
            findings.append({"line": number, "kind": kind, "term": term,
                             "text": line.strip()[:200]})
    return findings


class MandateService:
    """the check, run where a card meets the seat that will carry it."""

    def __init__(self, store: Store, crews=None, kanban=None, knowledge=None, audit=None):
        self.store = store
        self.crews = crews
        self.kanban = kanban
        self.knowledge = knowledge
        self.audit = audit

    # ---- what the crew already knows -----------------------------------
    def corpus(self, team_id: str, task_id: str) -> list[str]:
        """every text a seat could have learned the vocabulary from, minus this card."""
        texts: list[str] = []
        if self.kanban is not None:
            for task in self.kanban.list_tasks(team_id, limit=500):
                if task["id"] == task_id:
                    continue
                texts.append(f"{task['title']}\n{task['description']}")
                payload = loads(task.get("payload") or "{}")
                if isinstance(payload, dict):
                    texts.append(str(payload.get("brief") or payload.get("spec") or ""))
        rows = self.store.query(
            "select title, text from knowledge_docs where team_id = ? limit 200", (team_id,))
        texts.extend(f"{row['title']}\n{row['text']}" for row in rows)
        # the crew's own context entries are read by every seat too, so a term in one of
        # them is a term a seat may legitimately know
        for row in self.store.query(
                "select value from context_entries where team_id = ? limit 200", (team_id,)):
            texts.append(str(row["value"] or ""))
        return [text for text in texts if text]

    def vocabulary(self, team_id: str, task_id: str) -> dict:
        """the card's own terms, and whether the board was thick enough to trust them."""
        task = self.store.one("select * from tasks where id = ?", (task_id,))
        task = dict(task) if task is not None else None
        if task is None:
            raise NotFound(f"no card {task_id}")
        if task["team_id"] != team_id:
            raise ValidationError("that card belongs to another crew", reason="wrong_crew")
        payload = loads(task.get("payload") or "{}")
        brief = ""
        if isinstance(payload, dict):
            brief = str(payload.get("brief") or payload.get("spec") or "")
        card = f"{task['title']}\n{task['description']}\n{brief}"
        others = self.corpus(team_id, task_id)
        terms = distinctive(card, others)
        return {
            "card": card[:CAP],
            "terms": terms,
            "thick": bool(others),
            "derived_from": len(others),
        }

    # ---- the check -----------------------------------------------------
    def check(self, team_id: str, agent_id: str, task_id: str) -> dict:
        """does this seat's mandate name this card's own terms."""
        agent = self.store.one(
            "select * from agents where id = ? and team_id = ?", (agent_id, team_id))
        agent = dict(agent) if agent is not None else None
        if agent is None:
            raise NotFound(f"no agent {agent_id} on that crew")
        vocabulary = self.vocabulary(team_id, task_id)
        terms = vocabulary["terms"]
        if not vocabulary["thick"]:
            # nothing else in the crew to compare against, so only the shape that is
            # evidence on its own is worth naming. saying "everything is distinctive"
            # would flag every identifier the seat legitimately uses
            terms = [term for kind, term in tokens_in(vocabulary["card"]) if kind == ALWAYS]
        mandate = f"{agent['role']}\n{agent['system_prompt']}"
        findings = lint(mandate, set(terms))
        return {
            "agent": {"id": agent["id"], "name": agent["name"]},
            "task_id": task_id,
            "findings": findings,
            "terms": len(terms),
            "thick": vocabulary["thick"],
            "derived_from": vocabulary["derived_from"],
            "ok": not findings,
        }

    def for_dispatch(self, task: dict, agent: dict, strict: bool = False) -> dict:
        """the same check at the moment a card is handed over, recorded rather than fatal."""
        # rows arrive as sqlite3.Row from the runner and as dicts from the api
        task = dict(task)
        agent = dict(agent)
        if not task.get("id") or not agent.get("id"):
            return {"findings": [], "ok": True, "terms": 0}
        try:
            checked = self.check(task["team_id"], agent["id"], task["id"])
        except (NotFound, ValidationError):
            # a check that cannot run is not a finding. it also must not become a reason
            # a card cannot be dispatched, or the lint would be a gate by accident
            return {"findings": [], "ok": True, "terms": 0, "skipped": "no_card_text"}
        if checked["findings"] and self.audit is not None:
            self.audit.record_event("mandate.overfit", {
                "team_id": task["team_id"],
                "agent_id": agent["id"],
                "task": task,
                "data": {"task_id": task["id"], "findings": checked["findings"][:5],
                         "terms": checked["terms"]},
            })
        if strict and checked["findings"]:
            raise ValidationError(
                f"{agent['name']}'s mandate names this card's own terms "
                f"({', '.join(f['term'] for f in checked['findings'][:3])}); a mandate says "
                f"how the seat works, not what this card says",
                reason="mandate_names_the_card", findings=checked["findings"][:5])
        return checked
