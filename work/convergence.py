"""a barrier: the record that says whether a fan-out is finished, and who is missing.

a parent card that spawns children had no way to say what "the children are done" means.
the run that spawned them returned, the parent waited on nothing, and three of five lanes
answering read exactly like five of five, because silence is not a result and nothing was
counting the silences.

so a convergence is a row: an explicit partition map, a join rule, a deadline, and the
reports. every rule is a pure function of that row and a clock, and every refusal is
named, because a barrier that cannot say which lane is missing is one nobody can close.
"""

from __future__ import annotations

from core.db import Store, dumps, loads, new_id, now
from core.errors import Conflict, NotFound, ValidationError

JOINS = ("all", "quorum", "first_success")
OUTCOMES = ("result", "not_applicable", "blocked")
DEFAULT_DEADLINE_SECONDS = 24 * 3600.0


def validate(plan: dict) -> list[str]:
    """what is wrong with this plan, in the plan's own words.

    exhaustive by construction is the point: a partition with no owner, or an owner who
    is not a participant, is a lane that can never report and so is never missed.
    """
    problems: list[str] = []
    partitions = plan.get("partitions") or {}
    participants = list(plan.get("participants") or [])
    join = plan.get("join", "all")
    if not partitions:
        problems.append("no_partitions")
    if len(participants) < 2:
        problems.append("a_convergence_needs_two_participants")
    if join not in JOINS:
        problems.append(f"join_must_be_one_of_{'|'.join(JOINS)}")
    for partition, owner in partitions.items():
        if owner not in participants:
            problems.append(f"owner_not_a_participant:{partition}:{owner}")
    if join == "quorum":
        quorum = plan.get("quorum")
        if not isinstance(quorum, int) or quorum < 1:
            problems.append("quorum_needs_a_number")
        elif quorum > len(partitions):
            problems.append("quorum_is_more_than_the_partitions")
    return problems


def add_report(plan: dict, entry: dict, at: float) -> dict:
    """add a report, or refuse it by name.

    three refusals matter. a partition the plan does not name is somebody else's work
    arriving in this barrier. a result with no reference is a claim. a blocked lane with
    no reason is a silence wearing a status.
    """
    partition = str(entry.get("partition") or "")
    participant = str(entry.get("participant") or "")
    outcome = str(entry.get("outcome") or "")
    partitions = plan.get("partitions") or {}
    if partition not in partitions:
        raise ValidationError(f"'{partition}' is not a partition of this convergence")
    if partitions[partition] != participant:
        raise ValidationError(
            f"partition '{partition}' belongs to {partitions[partition]}, not {participant}")
    if outcome not in OUTCOMES:
        raise ValidationError(f"outcome must be one of {list(OUTCOMES)}")
    if outcome == "result" and not str(entry.get("artifact_ref") or ""):
        raise ValidationError("a result needs a reference, not a claim")
    if outcome == "blocked" and not str(entry.get("reason") or ""):
        raise ValidationError("a blocked partition needs a reason")
    kept = [r for r in plan.get("reports") or [] if r.get("partition") != partition]
    kept.append({
        "partition": partition,
        "participant": participant,
        "outcome": outcome,
        "artifact_ref": str(entry.get("artifact_ref") or ""),
        "reason": str(entry.get("reason") or ""),
        "touches": [str(t) for t in (entry.get("touches") or [])],
        "at": at,
    })
    return {**plan, "reports": kept}


def barrier(plan: dict) -> dict:
    """is it closed, and by which rule.

    missing names every partition that has not reported, because that list is the only
    useful thing a stalled barrier can say.
    """
    partitions = plan.get("partitions") or {}
    reports = plan.get("reports") or []
    seen = {r["partition"]: r for r in reports}
    missing = [p for p in partitions if p not in seen]
    results = [r for r in reports if r.get("outcome") == "result"]
    blocked = [r for r in reports if r.get("outcome") == "blocked"]
    join = plan.get("join", "all")
    if join == "all":
        resolved = not missing
    elif join == "quorum":
        resolved = len(results) >= int(plan.get("quorum") or 0)
    else:
        resolved = bool(results)
    return {
        "resolved": resolved,
        "join": join,
        "missing": missing,
        "counts": {"partitions": len(partitions), "reported": len(seen),
                   "results": len(results), "blocked": len(blocked)},
        "blocked": [r["partition"] for r in blocked],
    }


def conflicts(plan: dict) -> list[dict]:
    """two lanes that wrote the same surface were not decoupled, whatever the plan said."""
    seen: dict[str, str] = {}
    found: list[dict] = []
    for entry in plan.get("reports") or []:
        for surface in entry.get("touches") or []:
            owner = seen.get(surface)
            if owner and owner != entry["partition"]:
                found.append({"surface": surface, "partitions": [owner, entry["partition"]]})
            else:
                seen[surface] = entry["partition"]
    return found


def stalled(plan: dict, at: float) -> bool:
    deadline = plan.get("deadline")
    if deadline is None:
        return False
    return at > float(deadline) and not barrier(plan)["resolved"]


def cancelled_by_first_success(plan: dict) -> list[str]:
    """with first_success the lanes that have not reported are losers, and a lane that
    loses silently keeps spending."""
    if plan.get("join") != "first_success" or not barrier(plan)["resolved"]:
        return []
    seen = {r["partition"] for r in plan.get("reports") or []}
    return [p for p in (plan.get("partitions") or {}) if p not in seen]


class ConvergenceService:
    def __init__(self, store: Store, kanban=None):
        self.store = store
        self.kanban = kanban

    def open(self, task_id: str, partitions: dict, participants: list[str],
             join: str = "all", quorum: int | None = None,
             deadline_seconds: float = DEFAULT_DEADLINE_SECONDS, parent: str = "") -> dict:
        task = self._task(task_id)
        plan = {"partitions": dict(partitions), "participants": list(participants),
                "join": join, "quorum": quorum, "reports": [],
                "deadline": now() + float(deadline_seconds)}
        problems = validate(plan)
        if problems:
            raise ValidationError("this convergence cannot be closed by any report",
                                  problems=problems)
        convergence_id = new_id("conv")
        ts = now()
        with self.store.tx() as c:
            c.execute(
                """insert into convergences (id, team_id, task_id, parent, join_rule, quorum,
                       deadline, partitions, reports, status, created_date, updated_date)
                   values (?,?,?,?,?,?,?,?,?,'open',?,?)""",
                (convergence_id, task["team_id"], task_id, parent, join, quorum,
                 plan["deadline"], dumps(plan["partitions"]), dumps([]), ts, ts),
            )
        return self.get(convergence_id)

    def get(self, convergence_id: str) -> dict:
        row = self.store.one("select * from convergences where id = ?", (convergence_id,))
        if not row:
            raise NotFound(f"convergence {convergence_id} not found")
        return self._shape(dict(row))

    def for_task(self, task_id: str, status: str = "open") -> dict | None:
        """the newest barrier on a card. `status=""` asks for any status, which is what
        a reader wants: whether the barrier is open, closed or stalled is the answer."""
        if status:
            row = self.store.one(
                "select * from convergences where task_id = ? and status = ?"
                " order by created_date desc", (task_id, status))
        else:
            row = self.store.one(
                "select * from convergences where task_id = ? order by created_date desc",
                (task_id,))
        return self._shape(dict(row)) if row else None

    def for_partition(self, task_id: str) -> dict | None:
        """the open convergence whose partitions name this card as a lane."""
        for row in self.store.query("select * from convergences where status = 'open'"):
            shaped = self._shape(dict(row))
            if task_id in shaped["partitions"]:
                return shaped
        return None

    def report(self, convergence_id: str, entry: dict) -> dict:
        current = self.get(convergence_id)
        if current["status"] != "open":
            raise Conflict(f"convergence is {current['status']}, not open")
        updated = add_report(current, entry, now())
        state = barrier(updated)
        with self.store.tx() as c:
            c.execute(
                "update convergences set reports = ?, status = ?, updated_date = ? where id = ?",
                (dumps(updated["reports"]), "closed" if state["resolved"] else "open",
                 now(), convergence_id))
        result = self.get(convergence_id)
        result["barrier"] = barrier(result)
        result["conflicts"] = conflicts(result)
        return result

    def check(self, convergence_id: str) -> dict:
        current = self.get(convergence_id)
        state = barrier(current)
        if current["status"] == "open" and stalled(current, now()):
            with self.store.tx() as c:
                c.execute("update convergences set status = 'stalled', updated_date = ? where id = ?",
                          (now(), convergence_id))
            current = self.get(convergence_id)
        return {**current, "barrier": state, "conflicts": conflicts(current),
                "stalled": stalled(current, now()),
                "cancel": cancelled_by_first_success(current)}

    def _task(self, task_id: str) -> dict:
        row = self.store.one("select * from tasks where id = ?", (task_id,))
        if not row:
            raise NotFound(f"task {task_id} not found")
        return dict(row)

    def _shape(self, row: dict) -> dict:
        return {
            "id": row["id"], "team_id": row["team_id"], "task_id": row["task_id"],
            "parent": row["parent"], "join": row["join_rule"], "quorum": row["quorum"],
            "deadline": row["deadline"], "partitions": loads(row["partitions"], {}),
            "reports": loads(row["reports"], []), "status": row["status"],
            "created_date": row["created_date"], "updated_date": row["updated_date"],
        }
