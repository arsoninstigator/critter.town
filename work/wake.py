"""a wake is a reason for a card to be worked. this decides what happens to it.

the bug this replaces: a wake that arrived while another agent held the card came back as
`Conflict: not_holder` and was gone. nothing recorded that someone had asked, so the reason
to work had to be rediscovered by a person. paperclip's wake queue states the rule the other
way round, and it is the right way: a wake either runs the card, joins the run already on it,
or waits behind that run. refusing is reserved for a card that genuinely cannot be woken.

four decisions, in the order they are tried, and each one is a fact about the card rather
than a guess about intent:

* `run`      nothing holds the card and nothing forbids it, so the wake becomes the run.
* `coalesce` a run is already on this card. the wake joins it rather than queueing beside
             it, because two runs on one card is the race custody exists to stop, and
             dropping the wake is what we used to do.
* `defer`    the card is waiting on a person, not on an agent. the wake is kept and is
             revived when the card leaves review, because a deferral that never comes back
             is a drop with better manners.
* `refuse`   the card is closed, blocked, or out of attempts. recorded with its reason, so
             the wake that was not taken is still visible.

the decisions are a pure function of the row, so the table can be read and argued with
without a database. the service around it only writes what the table decided.
"""

from __future__ import annotations

from typing import Any

from core.db import Store, new_id, now
from core.errors import NotFound

DECISIONS = ("run", "coalesce", "defer", "refuse")

# a card in these columns cannot be woken by anything: done is finished and blocked is
# waiting for a person to replan it. review is different, and that difference is the point
CLOSED = ("done", "blocked")
AWAITING_PERSON = ("review",)


def decide(task: dict, holder: str = "", attempts: int = 0, max_attempts: int = 3,
           live_run: str = "", agent_id: str = "") -> dict:
    """the whole policy, as a function of the card.

    `holder` is a live holder only. an expired lease is not a holder, which is the rule
    custody already keeps, so a wake for a card whose worker died is admitted rather than
    deferred behind a ghost.
    """
    column = task.get("column") or task.get("column_name") or ""
    if column in CLOSED:
        return {"decision": "refuse", "reason": f"card_{column}",
                "detail": f"a card in '{column}' is not woken by a trigger"}
    if column in AWAITING_PERSON:
        return {"decision": "defer", "reason": "awaiting_review",
                "detail": "a person owes this card a decision, so a run would be a guess"}
    if column == "backlog":
        return {"decision": "refuse", "reason": "not_ready",
                "detail": "a card nobody scheduled is not woken by a trigger; move it to ready"}
    if attempts >= max_attempts:
        return {"decision": "refuse", "reason": "attempts_exhausted",
                "detail": f"{attempts} of {max_attempts} attempts are spent"}
    if holder and agent_id and holder == agent_id:
        # the holder asking again is the run that is already on it
        return {"decision": "coalesce", "reason": "replay", "run_id": live_run,
                "detail": f"{holder} is already running this card"}
    if holder:
        return {"decision": "coalesce", "reason": "held_by_another", "run_id": live_run,
                "detail": f"{holder} holds this card; the wake joins that run"}
    return {"decision": "run", "reason": "admitted", "run_id": live_run, "detail": ""}


class WakeQueue:
    """one wake per card per admission, and nothing is lost between admissions."""

    def __init__(self, store: Store):
        self.store = store

    # ---- reading ---------------------------------------------------------
    def get(self, wake_id: str) -> dict:
        row = self.store.one("select * from wakes where id = ?", (wake_id,))
        if row is None:
            raise NotFound(f"no wake {wake_id}")
        return dict(row)

    def open(self, team_id: str, limit: int = 50) -> list[dict]:
        """wakes that are still owed an answer: admitted, waiting, or joined to a run."""
        rows = self.store.query(
            """select * from wakes where team_id = ? and decision in ('run','coalesce','defer')
               and settled_at is null order by created_date desc limit ?""", (team_id, limit))
        return [dict(row) for row in rows]

    def for_task(self, task_id: str, limit: int = 20) -> list[dict]:
        rows = self.store.query(
            "select * from wakes where task_id = ? order by created_date desc limit ?",
            (task_id, limit))
        return [dict(row) for row in rows]

    def pending(self, task_id: str) -> dict | None:
        row = self.store.one(
            """select * from wakes where task_id = ? and decision = 'run'
               and settled_at is null order by created_date desc limit 1""", (task_id,))
        return dict(row) if row else None

    def live_run(self, task_id: str) -> str:
        row = self.store.one(
            """select id from runs where task_id = ? and status = 'running'
               order by created_date desc limit 1""", (task_id,))
        return row["id"] if row else ""

    # ---- the decision ----------------------------------------------------
    def admit(self, task: dict, agent_id: str = "", source: str = "", note: str = "") -> dict:
        """decide, record, and hand back the wake with its decision on it.

        the pending wake for this card is reused rather than a second one added: two wakes
        for one card are one reason to work, and a queue that counts them twice would
        report pressure that does not exist.
        """
        task = dict(task)
        task_id = task["id"]
        row = self.store.one(
            "select * from tasks where id = ?", (task_id,))
        if row is None:
            raise NotFound(f"no card {task_id}")
        full = dict(row)
        holder = full["lease_holder"] or ""
        if holder and (full["lease_expires"] or 0) <= now():
            holder = ""
        decision = decide(
            {**task, "column": full["column_name"]},
            holder=holder,
            attempts=int(full["attempts"] or 0),
            max_attempts=int(full["max_attempts"] or 3),
            live_run=self.live_run(task_id),
            agent_id=agent_id,
        )
        wake = self.pending(task_id) if decision["decision"] == "run" else None
        if wake is None:
            wake = self._insert(full["team_id"], task_id, source, note, holder)
        self._write(wake["id"], {
            "decision": decision["decision"],
            "reason": decision["reason"],
            "detail": decision.get("detail", ""),
            "holder": holder,
            "agent_id": agent_id,
            "run_id": decision.get("run_id", "") or "",
        })
        return {**wake, **decision, "wake_id": wake["id"]}

    def settle(self, run_id: str, outcome: str = "succeeded") -> list[dict]:
        """a run ended. a wake that joined it was not necessarily answered by it.

        coalesced wakes go back to pending rather than to done, because the run that
        absorbed them had no way to know it had; the next admission refuses, defers or runs
        them on the card's merits, and that refusal is recorded with its reason.
        """
        if not run_id:
            # no run to attribute them to, so there is nothing to revive here: the row is
            # already pending and the next admission will pick it up
            return []
        rows = self.store.query(
            """select * from wakes where run_id = ? and decision = 'coalesce'
               and settled_at is null""", (run_id,))
        revived: list[dict] = []
        for row in rows:
            self._write(row["id"], {"decision": "run", "reason": "run_ended",
                                    "detail": f"the run it joined ended: {outcome}",
                                    "run_id": ""})
            revived.append(self.get(row["id"]))
        return revived

    def revive(self, team_id: str) -> list[dict]:
        """deferred wakes whose reason has cleared come back on their own."""
        rows = self.store.query(
            """select * from wakes where team_id = ? and decision = 'defer'
               and settled_at is null""", (team_id,))
        revived: list[dict] = []
        for row in rows:
            task = self.store.one("select column_name from tasks where id = ?", (row["task_id"],))
            if task is None:
                # the cascade takes a wake with its card, so this is a guard rather than a
                # path: a row that outlived its card is not revived
                continue
            if task["column_name"] in AWAITING_PERSON:
                continue
            self._write(row["id"], {"decision": "run", "reason": "deferred_reason_cleared",
                                    "detail": f"the card left '{row['reason']}'"})
            revived.append(self.get(row["id"]))
        return revived

    def delivered(self, wake_id: str, run_id: str) -> dict:
        """the wake became a run. the row stays, so the reason is still on the record."""
        self._write(wake_id, {"decision": "run", "reason": "delivered", "run_id": run_id,
                              "settled_at": now()})
        return self.get(wake_id)

    def refuse(self, wake_id: str, reason: str = "", detail: str = "") -> dict:
        values: dict[str, Any] = {"decision": "refuse", "settled_at": now()}
        if reason:
            values["reason"] = reason
        if detail:
            values["detail"] = detail
        self._write(wake_id, values)
        return self.get(wake_id)

    # ---- writing ---------------------------------------------------------
    def _insert(self, team_id: str, task_id: str, source: str, note: str,
                holder: str) -> dict:
        wake_id = new_id("wake")
        with self.store.tx() as c:
            c.execute(
                """insert into wakes (id, team_id, task_id, source, note, decision, reason,
                       detail, holder, agent_id, run_id, created_date)
                   values (?,?,?,?,?, 'run', '', '', ?, '', '', ?)""",
                (wake_id, team_id, task_id, source, note, holder, now()),
            )
        return self.get(wake_id)

    def _write(self, wake_id: str, values: dict) -> None:
        if not values:
            return
        columns = ", ".join(f"{name} = ?" for name in values)
        with self.store.tx() as c:
            c.execute(f"update wakes set {columns} where id = ?",
                      tuple(values.values()) + (wake_id,))
