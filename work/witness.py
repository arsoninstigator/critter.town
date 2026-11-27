"""what a run was made from, hashed, so a card cannot be closed against a premise that moved.

darkrun keeps `input_witnesses` on a unit: a map of premise to sha256 taken when the work was
made, compared later by a drift sweep. critter.town had the same hole from the other side.
a card is dispatched against a brief, a handful of knowledge entries and its parent's result,
all of which are mutable rows, and nothing recorded which version the run read. edit a
knowledge entry after the run and the card still closes as if it had been read.

so: at dispatch, hash the premises the run was handed. later, recompute. a premise that no
longer hashes the same is drift, and drift is a fact about the card, not an opinion about it.

what is witnessed is deliberately narrow. the prompt also carries the intent block, the
inbox, crew context and the lessons, and hashing all of that would report drift every time
anyone spoke. the three premises here are the ones a run is *about*: the brief, the knowledge
it was told to rely on, and the parent card it was handed the result of.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from core.db import Store, dumps, loads, new_id, now
from core.errors import NotFound

PREMISE_KINDS = ("brief", "knowledge", "parent")


def digest(text: str) -> str:
    """sha256 of a canonical form, so the same content always hashes the same."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def brief_text(task: dict) -> str:
    """the card as the run was told it: title, description, skills and payload.

    the payload is sorted and stripped of the keys the crew writes back during a run
    (spend, results), because those change by design and a witness that reports them as
    drift would report every card.
    """
    payload = dict(task.get("payload") or {})
    for volatile in ("spent_cents", "result", "run_id", "agent_id"):
        payload.pop(volatile, None)
    return json.dumps({
        "title": task.get("title") or "",
        "description": task.get("description") or "",
        "required_skills": sorted(task.get("required_skills") or []),
        "payload": payload,
    }, sort_keys=True, default=str)


def knowledge_text(entry: dict) -> str:
    return json.dumps({
        "title": entry.get("title") or "",
        "body": entry.get("body") or "",
        "kind": entry.get("kind") or "",
    }, sort_keys=True, default=str)


def parent_text(parent: dict) -> str:
    return json.dumps({
        "title": parent.get("title") or "",
        "evidence_ref": parent.get("evidence_ref") or "",
        "payload": parent.get("payload") or {},
    }, sort_keys=True, default=str)


def premises(task: dict, knowledge: list[dict] | None = None,
             parent: dict | None = None) -> list[dict]:
    """the premise list for one dispatch, as {kind, ref, digest, chars}.

    a card with no parent or no knowledge yields fewer premises rather than empty ones:
    an absent premise is not a premise that changed.
    """
    found = [{"kind": "brief", "ref": task["id"], "digest": digest(brief_text(task)),
              "chars": len(brief_text(task))}]
    for entry in knowledge or []:
        text = knowledge_text(entry)
        found.append({"kind": "knowledge", "ref": entry["id"], "digest": digest(text),
                      "chars": len(text)})
    if parent:
        text = parent_text(parent)
        found.append({"kind": "parent", "ref": parent["id"], "digest": digest(text),
                      "chars": len(text)})
    return found


def compare(witnessed: list[dict], current: list[dict]) -> list[dict]:
    """the findings, by comparing what was hashed against what is there now.

    three ways a premise drifts and each is named, because they mean different things: it
    changed, it was deleted, or it is new (the run read one and the card now relies on
    another). a missing premise is the loudest of the three: the run was made from
    something that no longer exists.
    """
    before = {(row["kind"], row["ref"]): row for row in witnessed}
    after = {(row["kind"], row["ref"]): row for row in current}
    findings: list[dict] = []
    for key, row in before.items():
        fresh = after.get(key)
        if fresh is None:
            findings.append({"kind": row["kind"], "ref": row["ref"], "change": "removed",
                             "was": row["digest"], "now": ""})
        elif fresh["digest"] != row["digest"]:
            findings.append({"kind": row["kind"], "ref": row["ref"], "change": "changed",
                             "was": row["digest"], "now": fresh["digest"]})
    for key, row in after.items():
        if key not in before:
            findings.append({"kind": row["kind"], "ref": row["ref"], "change": "added",
                             "was": "", "now": row["digest"]})
    return sorted(findings, key=lambda row: (row["kind"], row["ref"], row["change"]))


class Witness:
    """the hashes on disk, and the sweep that compares them."""

    def __init__(self, store: Store, kanban: Any = None, knowledge: Any = None):
        self.store = store
        self.kanban = kanban
        self.knowledge = knowledge

    # ---- recording -------------------------------------------------------
    def record(self, run_id: str, task: dict, knowledge: list[dict] | None = None,
               parent: dict | None = None) -> list[dict]:
        """write the premises of one run. called once, before the first turn."""
        found = premises(task, knowledge, parent)
        with self.store.tx() as c:
            for row in found:
                c.execute(
                    """insert into witnesses (id, team_id, task_id, run_id, kind, ref,
                           digest, chars, created_date) values (?,?,?,?,?,?,?,?,?)""",
                    (new_id("wit"), task["team_id"], task["id"], run_id, row["kind"],
                     row["ref"], row["digest"], row["chars"], now()),
                )
        return found

    def record_for(self, run_id: str, task: dict, agent_id: str = "") -> list[dict]:
        """the premises of one dispatch, selected the same way the prompt selects them."""
        entries: list[dict] = []
        if self.knowledge is not None:
            try:
                entries = self.knowledge.cited(task["team_id"], agent_id=agent_id)
            except Exception:  # a knowledge base that breaks must not stop a dispatch
                entries = []
        parent = None
        if task.get("parent_id") and self.kanban is not None:
            try:
                parent = self.kanban.get_task(task["parent_id"])
            except NotFound:
                parent = None
        return self.record(run_id, task, entries, parent)

    def for_run(self, run_id: str) -> list[dict]:
        rows = self.store.query(
            "select * from witnesses where run_id = ? order by kind, ref", (run_id,))
        return [dict(row) for row in rows]

    def for_task(self, task_id: str, run_id: str = "") -> list[dict]:
        """the premises of one run, defaulting to the card's latest."""
        if not run_id:
            run_id = self.latest_run(task_id)
        return self.for_run(run_id) if run_id else []

    def latest_run(self, task_id: str) -> str:
        row = self.store.one(
            """select run_id from witnesses where task_id = ?
               order by created_date desc limit 1""", (task_id,))
        return row["run_id"] if row else ""

    # ---- the sweep -------------------------------------------------------
    def current(self, task: dict, agent_id: str = "") -> list[dict]:
        """the premises the card has now, hashed the same way.

        the agent is the one who ran, not whoever holds the card today: the knowledge an
        entry is visible to is scoped per agent, so asking as a different seat would
        report a change that is only a change of reader.
        """
        entries: list[dict] = []
        if self.knowledge is not None:
            try:
                entries = self.knowledge.cited(task["team_id"], agent_id=agent_id)
            except Exception:  # a knowledge base that breaks must not stop the sweep
                entries = []
        parent = None
        if task.get("parent_id") and self.kanban is not None:
            try:
                parent = self.kanban.get_task(task["parent_id"])
            except NotFound:
                parent = None
        return premises(task, entries, parent)

    def drift(self, task_id: str) -> dict:
        """one card: what it was made from, what is there now, and what moved."""
        task = self.kanban.get_task(task_id) if self.kanban is not None else self._task(task_id)
        witnessed = self.for_task(task_id)
        if not witnessed:
            return {"task_id": task_id, "witnessed": False, "findings": [],
                    "why": "no run has been made from this card yet"}
        run_id = witnessed[0]["run_id"]
        findings = compare(witnessed, self.current(task, self._runner_of(run_id)))
        return {"task_id": task_id, "run_id": run_id,
                "witnessed": True, "drifted": bool(findings), "findings": findings}

    def sweep(self, team_id: str, limit: int = 200) -> dict:
        """every card in the crew that has witnesses, newest first.

        this is a report, not a transition: a drifted card is named, and what to do about
        it is a person's call, because re-running a closed card spends money and reopening
        one that drifted cosmetically spends attention.
        """
        rows = self.store.query(
            """select distinct task_id from witnesses where team_id = ?
               order by created_date desc limit ?""", (team_id, limit))
        cards: list[dict] = []
        for row in rows:
            try:
                cards.append(self.drift(row["task_id"]))
            except NotFound:
                continue
        drifted = [card for card in cards if card.get("drifted")]
        return {"crew": team_id, "checked": len(cards), "drifted": len(drifted),
                "cards": drifted}

    def _runner_of(self, run_id: str) -> str:
        row = self.store.one("select agent_id from runs where id = ?", (run_id,))
        return row["agent_id"] if row else ""

    def _task(self, task_id: str) -> dict:
        row = self.store.one("select * from tasks where id = ?", (task_id,))
        if row is None:
            raise NotFound(f"no card {task_id}")
        task = dict(row)
        task["column"] = task.pop("column_name")
        return task
