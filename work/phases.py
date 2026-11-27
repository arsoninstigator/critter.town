"""what a card of a given kind owes before it may move, and the questions it still has open.

a board that lets every card walk the same path teaches a crew that a code change, a research
question and a support reply are the same object. they are not. a code card is not reviewable
while somebody's question about it is still open, and it does not close until a run of it has
actually succeeded against acceptance criteria that were sealed before the work began.

the requirements are typed by the kind a card already declares, they are measured from records the
system already holds, and the board asks before it moves. that is the whole mechanism.

four refusals are deliberate:

- a requirement is measured, never asserted. "the acceptance suite is sealed" means a seal row
  exists for the crew with scenarios in it, not that somebody ticked a box saying tests were
  written.
- a waiver is a person's act. an agent that can waive its own gate has no gate, so waive()
  refuses an agent by name, the same way settling a statement does.
- a kind this board does not know is a refusal, not a default. a card nobody described should not
  be handed the lightest path available.
- the gate is opt-in per crew, and the reading is not. a crew that has not asked for phase checks
  is not going to have its board start refusing cards, so enforcement follows the crew's policy
  while readiness() answers for any card at any time. a gate nobody turned on is a surprise, and
  a surprise is not a standard.
"""

from __future__ import annotations

from typing import Any

from core.db import Store, loads, new_id, now
from core.errors import Conflict, NotAllowed, NotFound, ValidationError
from work.evidence import KINDS as EVIDENCE_KINDS

# the columns where a card's kind can owe something. every other move is the crew's business
GATED_COLUMNS = ("review", "done")

# what each kind owes, by default, once a crew turns phase checks on. code is the only kind that
# owes the acceptance machinery, because it is the only kind where "it ran and it passed" is a
# thing the records can hold. everything else owes an answer to its own questions.
DEFAULT_TABLE: dict[str, dict[str, tuple[str, ...]]] = {
    kind: {"review": ("no_open_questions", "proposals_settled"),
           "done": ("no_open_questions", "proposals_settled")}
    for kind in EVIDENCE_KINDS
}
DEFAULT_TABLE["code"] = {
    "review": ("no_open_questions", "proposals_settled"),
    "done": ("no_open_questions", "proposals_settled", "succeeded_run", "sealed_suite"),
}

REQUIREMENTS = ("no_open_questions", "proposals_settled", "succeeded_run", "sealed_suite")
QUESTION_STATUSES = ("open", "answered", "dropped")
# who may waive. an agent is not in it, and the list is short on purpose
WAIVER_KINDS = ("human", "owner")


def kind_of(task: dict) -> str:
    """the kind a card declares. the column is the truth; the payload is older than it."""
    kind = str(task.get("kind") or "").strip().lower()
    if not kind:
        payload = task.get("payload") or {}
        if isinstance(payload, str):
            payload = loads(payload, {})
        kind = str(payload.get("kind") or "").strip().lower()
    return kind or "code"


def _table(policy: Any) -> dict[str, dict[str, tuple[str, ...]]]:
    """a crew may state its own table. a stated table replaces the default for the kinds it names
    and leaves the rest alone, because a crew that tightened code did not mean to loosen research."""
    if not isinstance(policy, dict):
        return {kind: {col: tuple(reqs) for col, reqs in columns.items()}
                for kind, columns in DEFAULT_TABLE.items()}
    table = {kind: {col: tuple(reqs) for col, reqs in columns.items()}
             for kind, columns in DEFAULT_TABLE.items()}
    for kind, columns in policy.items():
        kind = str(kind).lower()
        if not isinstance(columns, dict):
            continue
        table[kind] = {str(col): tuple(str(r) for r in (reqs or []))
                       for col, reqs in columns.items()}
    return table


def required_for(kind: str, column: str,
                 table: dict | None = None) -> tuple[str, ...]:
    """what this kind of card owes before it may enter this column. nothing is owed by default."""
    if kind not in EVIDENCE_KINDS:
        raise ValidationError(f"kind must be one of {list(EVIDENCE_KINDS)}", kind=kind)
    if column not in GATED_COLUMNS:
        return ()
    return tuple((table or DEFAULT_TABLE).get(kind, {}).get(column, ()))


def unmet_reason(requirement: str, measured: dict) -> str:
    """one sentence a person can act on, per unmet requirement."""
    if requirement == "sealed_suite":
        return "no acceptance suite is sealed for this crew, so there is nothing to accept against"
    if requirement == "succeeded_run":
        return "no run of this card has succeeded, so there is no work to accept"
    if requirement == "no_open_questions":
        count = int(measured.get("open_questions") or 0)
        return f"{count} question{'s' if count != 1 else ''} on this card are still open"
    if requirement == "proposals_settled":
        count = int(measured.get("open_proposals") or 0)
        return (f"{count} proposal{'s' if count != 1 else ''} to another department's surface "
                f"{'are' if count != 1 else 'is'} still waiting on its owner")
    return f"{requirement} is not a requirement this board can measure"


def met(requirement: str, measured: dict) -> bool:
    if requirement == "sealed_suite":
        return bool(measured.get("sealed_suite"))
    if requirement == "succeeded_run":
        return bool(measured.get("succeeded_run"))
    if requirement == "no_open_questions":
        return int(measured.get("open_questions") or 0) == 0
    if requirement == "proposals_settled":
        return int(measured.get("open_proposals") or 0) == 0
    return False


class Phases:
    def __init__(self, store: Store, kanban: Any = None, holdout: Any = None,
                 audit: Any = None):
        self.store = store
        self.kanban = kanban
        self.holdout = holdout
        self.audit = audit

    # ---- policy --------------------------------------------------------
    def policy(self, team_id: str) -> Any:
        row = self.store.one("select orchestration from teams where id = ?", (team_id,))
        if not row:
            raise NotFound(f"crew {team_id} not found")
        return (loads(row["orchestration"], {}) or {}).get("phases")

    def enabled(self, team_id: str) -> bool:
        return bool(self.policy(team_id))

    def table(self, team_id: str) -> dict:
        return _table(self.policy(team_id))

    # ---- rows ----------------------------------------------------------
    def _task(self, task_id: str) -> dict:
        row = self.store.one(
            """select id, team_id, title, column_name, kind, payload, result from tasks
               where id = ?""", (task_id,))
        if not row:
            raise NotFound(f"card {task_id} not found")
        return {"id": row["id"], "team_id": row["team_id"], "title": row["title"],
                "column": row["column_name"], "kind": row["kind"] or "",
                "payload": loads(row["payload"], {}), "result": loads(row["result"], {})}

    def _question(self, question_id: str) -> dict:
        row = self.store.one("select * from questions where id = ?", (question_id,))
        if not row:
            raise NotFound(f"question {question_id} not found")
        return self._row(row)

    @staticmethod
    def _row(row) -> dict:
        return {"id": row["id"], "team_id": row["team_id"], "task_id": row["task_id"],
                "text": row["text"], "asked_by": row["asked_by"], "status": row["status"],
                "answer": row["answer"], "answered_by": row["answered_by"],
                "created_date": row["created_date"], "closed_date": row["closed_date"]}

    # ---- questions -----------------------------------------------------
    def ask(self, task_id: str, text: str, asked_by: str = "") -> dict:
        """an open question is a record, not a comment: it has an asker, and it blocks."""
        text = " ".join(str(text or "").split())
        if not text:
            raise ValidationError("a question needs text")
        task = self._task(task_id)
        question_id = new_id("q")
        with self.store.tx() as c:
            c.execute(
                """insert into questions (id, team_id, task_id, text, asked_by, status,
                       created_date) values (?,?,?,?,?,'open',?)""",
                (question_id, task["team_id"], task_id, text, asked_by or "", now()))
        return self._question(question_id)

    def answer(self, question_id: str, text: str, by: str = "") -> dict:
        text = " ".join(str(text or "").split())
        if not text:
            raise ValidationError("an answer needs text")
        question = self._question(question_id)
        if question["status"] != "open":
            raise Conflict(f"question is already {question['status']}")
        with self.store.tx() as c:
            c.execute(
                """update questions set status = 'answered', answer = ?, answered_by = ?,
                       closed_date = ? where id = ?""",
                (text, by or "", now(), question_id))
        return self._question(question_id)

    def drop(self, question_id: str, why: str, by: str = "") -> dict:
        """a dropped question is not an answered one: the text stays, and so does the reason."""
        why = " ".join(str(why or "").split())
        if not why:
            raise ValidationError("dropping a question needs a reason")
        question = self._question(question_id)
        if question["status"] != "open":
            raise Conflict(f"question is already {question['status']}")
        with self.store.tx() as c:
            c.execute(
                """update questions set status = 'dropped', answer = ?, answered_by = ?,
                       closed_date = ? where id = ?""",
                (f"dropped: {why}", by or "", now(), question_id))
        return self._question(question_id)

    def questions(self, task_id: str, status: str | None = None) -> list[dict]:
        sql = "select * from questions where task_id = ?"
        params: list = [task_id]
        if status:
            if status not in QUESTION_STATUSES:
                raise ValidationError(f"status must be one of {list(QUESTION_STATUSES)}")
            sql += " and status = ?"
            params.append(status)
        sql += " order by created_date"
        return [self._row(row) for row in self.store.query(sql, tuple(params))]

    def crew_questions(self, team_id: str, status: str = "open") -> dict:
        if status not in QUESTION_STATUSES:
            raise ValidationError(f"status must be one of {list(QUESTION_STATUSES)}")
        rows = self.store.query(
            """select q.*, t.title as card_title from questions q
               left join tasks t on t.id = q.task_id
               where q.team_id = ? and q.status = ? order by q.created_date desc""",
            (team_id, status))
        return {"team_id": team_id, "status": status, "count": len(rows),
                "questions": [{**self._row(row), "card_title": row["card_title"]}
                              for row in rows]}

    # ---- waivers -------------------------------------------------------
    def waive(self, task_id: str, column: str, reason: str, by: str = "owner",
              by_kind: str = "human") -> dict:
        reason = " ".join(str(reason or "").split())
        if not reason:
            raise ValidationError("a waiver needs a reason")
        if by_kind not in WAIVER_KINDS:
            raise NotAllowed(f"a {by_kind} cannot waive a phase: only a person can")
        if column not in GATED_COLUMNS:
            raise ValidationError(f"column must be one of {list(GATED_COLUMNS)}", column=column)
        task = self._task(task_id)
        required_for(kind_of(task), column, self.table(task["team_id"]))
        waiver_id = new_id("phw")
        with self.store.tx() as c:
            c.execute(
                """insert into phase_waivers (id, team_id, task_id, column_name, reason,
                       waived_by, created_date) values (?,?,?,?,?,?,?)""",
                (waiver_id, task["team_id"], task_id, column, reason, by or "owner", now()))
        return self._waiver(waiver_id)

    def _waiver(self, waiver_id: str) -> dict:
        row = self.store.one("select * from phase_waivers where id = ?", (waiver_id,))
        if not row:
            raise NotFound(f"waiver {waiver_id} not found")
        return {"id": row["id"], "task_id": row["task_id"], "column": row["column_name"],
                "reason": row["reason"], "waived_by": row["waived_by"],
                "created_date": row["created_date"]}

    def waivers(self, task_id: str) -> list[dict]:
        rows = self.store.query(
            "select id from phase_waivers where task_id = ? order by created_date", (task_id,))
        return [self._waiver(row["id"]) for row in rows]

    # ---- the reading ---------------------------------------------------
    def measure(self, task: dict) -> dict:
        """what the records already say about this card, with nothing asked of the caller."""
        seal = self.store.one("select scenarios from holdout_seals where team_id = ?",
                              (task["team_id"],))
        run = self.store.one(
            "select count(*) as n from runs where task_id = ? and status = 'succeeded'",
            (task["id"],))
        open_questions = self.store.one(
            "select count(*) as n from questions where task_id = ? and status = 'open'",
            (task["id"],))
        proposals = self.store.query(
            """select id, surface, change, state, answer from org_proposals
               where task_id = ? order by created_date""", (task["id"],))
        return {
            "sealed_suite": bool(seal and int(seal["scenarios"] or 0) > 0),
            "sealed_scenarios": int(seal["scenarios"] or 0) if seal else 0,
            "succeeded_run": bool(run and int(run["n"] or 0) > 0),
            "open_questions": int(open_questions["n"] or 0) if open_questions else 0,
            "open_proposals": sum(1 for row in proposals if row["state"] == "open"),
            # a no is stored as 'declined'. it does not block, because the work may simply proceed
            # without the change. it is carried so a reviewer sees what the owner refused and can check it was not routed around
            "declined_proposals": [
                {"id": row["id"], "surface": row["surface"], "change": row["change"],
                 "answer": row["answer"] or ""}
                for row in proposals if row["state"] == "declined"],
            "accepted_proposals": [
                {"id": row["id"], "surface": row["surface"], "change": row["change"],
                 "answer": row["answer"] or ""}
                for row in proposals if row["state"] == "accepted"],
        }

    def readiness(self, task_id: str, to_column: str | None = None) -> dict:
        """what this card owes for the column it is heading to, and whether it has it."""
        task = self._task(task_id)
        kind = kind_of(task)
        column = to_column or task["column"]
        required = required_for(kind, column, self.table(task["team_id"]))
        measured = self.measure(task)
        waived = [w for w in self.waivers(task_id) if w["column"] == column]
        unmet = [{"requirement": name, "reason": unmet_reason(name, measured)}
                 for name in required if not met(name, measured)]
        return {
            "task_id": task_id,
            "kind": kind,
            "from": task["column"],
            "to": column,
            "enforced": self.enabled(task["team_id"]),
            "required": list(required),
            "measured": measured,
            "unmet": unmet,
            "waived": waived[0] if waived else None,
            "ready": (not unmet) or bool(waived),
        }

    def check_move(self, task: dict, column: str) -> None:
        """the hook the board calls. a refusal names the requirement and why it is unmet."""
        if column not in GATED_COLUMNS:
            return
        if not self.enabled(task["team_id"]):
            return
        reading = self.readiness(task["id"], to_column=column)
        if reading["ready"]:
            return
        raise Conflict(
            f"card is not ready for {column}: {reading['unmet'][0]['reason']}",
            kind=reading["kind"], unmet=reading["unmet"], column=column,
        )

    def for_crew(self, team_id: str) -> dict:
        """every card whose kind owes something it does not have, and the open questions."""
        rows = self.store.query(
            """select id, column_name, kind, payload from tasks
               where team_id = ? and column_name not in ('done', 'archived')""", (team_id,))
        blocked, unknown = [], []
        for row in rows:
            task = {"id": row["id"], "team_id": team_id, "column": row["column_name"],
                    "kind": row["kind"] or "", "payload": loads(row["payload"], {})}
            kind = kind_of(task)
            if kind not in EVIDENCE_KINDS:
                unknown.append({"task_id": row["id"], "kind": kind})
                continue
            reading = self.readiness(row["id"], to_column="review")
            if not reading["ready"]:
                blocked.append({"task_id": row["id"], "column": row["column_name"],
                                "kind": reading["kind"], "unmet": reading["unmet"]})
        open_questions = self.store.one(
            "select count(*) as n from questions where team_id = ? and status = 'open'",
            (team_id,))
        open_proposals = self.store.one(
            "select count(*) as n from org_proposals where team_id = ? and state = 'open'",
            (team_id,))
        return {"team_id": team_id, "cards": len(rows), "enforced": self.enabled(team_id),
                "blocked": blocked, "unknown_kind": unknown,
                "open_questions": int(open_questions["n"] or 0) if open_questions else 0,
                "open_proposals": int(open_proposals["n"] or 0) if open_proposals else 0}
