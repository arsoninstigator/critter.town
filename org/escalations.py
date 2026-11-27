"""an escalation is a decision put to a person, and it carries the answer it will take without one.

critter.town had the first step and not the rest. a department could push one message up one level
and stop, so a decision that needed a person had no way to reach one, no clock, and no outcome if
nobody answered. the owner's own org notes say what an escalation is, and clawcrew's lease ladder
says how it climbs, so this is both.

four lines, always: what happened, what the org already tried, the decision needed, and the default
it will take if nobody answers. the fourth line is what makes it honest. an escalation with no
default is a hostage note, so one is refused without it.

it climbs a ladder keyed on time since it was raised, and the ladder is data so it can be read and
tuned in one place: the next holder, the department's coordinator, the parent department's
coordinator, then a person. a rung that has no one behind it is skipped and named, not stalled on.

what does not earn a wake is refused at the door, with the reason, because an org that asks
constantly has moved the work instead of doing it: a broken build, a metric that has not moved yet,
a decision with a reversible default, a conflict two slices already settle. a person is woken for
something irreversible, something that costs money, a person outside the org, a legal question, or
a widening of authority.

when the deadline passes with no answer the default is taken and the record says so. that is the
org acting on its own stated default, never the person agreeing, and the two are different rows so
nobody can later read silence as consent.

escalations are counted per crew per month, and a crew over its budget gets a finding about its
scoping, not a nag. nothing here blocks a dispatch.
"""

from __future__ import annotations

import time
from typing import Any

from core.db import Store, loads, dumps, new_id, now
from core.errors import Conflict, NotFound, ValidationError

# what earns a wake. these are the categories a person is woken for, and a record names one.
WAKE_CATEGORIES = ("irreversible", "money", "a_person", "legality", "authority")

# what does not earn a wake, matched against the stated reason so the refusal can say which.
NOT_A_WAKE = {
    "broken_build": ("build is broken", "build broke", "tests fail", "ci is red", "compile error"),
    "metric_not_moved": ("metric has not moved", "not moved yet", "no movement yet"),
    "reversible_default": ("reversible default", "can be undone", "easily reverted"),
    "slice_conflict": ("conflict between two slices", "two slices disagree"),
}

# the ladder, as data. afterminutes is the time since the escalation was raised.
LADDER = (
    {"rung": "next_holder", "after_minutes": 0},
    {"rung": "coordinator", "after_minutes": 10},
    {"rung": "parent_coordinator", "after_minutes": 30},
    {"rung": "person", "after_minutes": 60},
)

STATES = ("open", "answered", "defaulted", "withdrawn")

# the target is a few a month. a crew over it is mis-scoped, and that is the finding.
DEFAULT_MONTHLY_BUDGET = 5

# a default must be something the org can actually take. these words say it is waiting for a person
# again, which is a loop and not a default.
EMPTY_DEFAULTS = ("wait", "ask again", "escalate", "tbd", "unknown", "none", "n/a", "nothing")


def month_key(at: float) -> str:
    return time.strftime("%Y-%m", time.gmtime(at))


def not_a_wake(*texts: str) -> list[str]:
    """the reasons a stated escalation is not one a person should be woken for."""
    haystack = " ".join(str(t or "").lower() for t in texts)
    return [name for name, needles in NOT_A_WAKE.items()
            if any(needle in haystack for needle in needles)]


def reads_as_empty_default(text: str) -> bool:
    cleaned = " ".join(str(text or "").lower().replace(".", " ").split())
    return cleaned in EMPTY_DEFAULTS or not cleaned


class Escalations:
    def __init__(self, store: Store, departments: Any = None, a2a: Any = None,
                 audit: Any = None):
        self.store = store
        self.departments = departments
        self.a2a = a2a
        self.audit = audit

    # ---- policy --------------------------------------------------------
    def policy(self, team_id: str) -> dict:
        row = self.store.one("select orchestration from teams where id = ?", (team_id,))
        if not row:
            raise NotFound(f"crew {team_id} not found")
        return (loads(row["orchestration"], {}) or {}).get("escalation") or {}

    def budget(self, team_id: str) -> int:
        value = self.policy(team_id).get("monthly_budget", DEFAULT_MONTHLY_BUDGET)
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return DEFAULT_MONTHLY_BUDGET

    def ladder(self, team_id: str) -> list[dict]:
        """the crew's own ladder when it set one, the default otherwise. never empty, never
        unordered, and the first rung always fires at zero so a raised escalation reaches someone."""
        custom = self.policy(team_id).get("ladder")
        if not isinstance(custom, list) or not custom:
            return [dict(step) for step in LADDER]
        steps = []
        for item in custom:
            if not isinstance(item, dict) or item.get("rung") not in {s["rung"] for s in LADDER}:
                raise ValidationError("a ladder rung must be one of "
                                      f"{[s['rung'] for s in LADDER]}", rung=item)
            steps.append({"rung": item["rung"], "after_minutes": max(0, int(item.get("after_minutes", 0)))})
        steps.sort(key=lambda step: step["after_minutes"])
        if steps[0]["after_minutes"] != 0:
            steps.insert(0, dict(LADDER[0]))
        return steps

    # ---- raising -------------------------------------------------------
    def _row(self, escalation_id: str) -> dict:
        row = self.store.one("select * from escalations where id = ?", (escalation_id,))
        if not row:
            raise NotFound(f"escalation {escalation_id} not found")
        return self._shape(row)

    def _shape(self, row: Any) -> dict:
        return {
            "id": row["id"], "team_id": row["team_id"], "raised_by": row["raised_by"],
            "task_id": row["task_id"], "category": row["category"],
            "what_happened": row["what_happened"], "already_tried": row["already_tried"],
            "decision_needed": row["decision_needed"], "default_action": row["default_action"],
            "default_after": row["default_after"], "state": row["state"],
            "answer": row["answer"], "answered_by": row["answered_by"],
            "outcome": row["outcome"], "month": row["month"],
            "raised_date": row["raised_date"], "settled_date": row["settled_date"],
            "reached": loads(row["reached"], []),
        }

    def raise_(self, agent_id: str, category: str, what_happened: str, already_tried: str,
               decision_needed: str, default_action: str, default_after_minutes: float,
               task_id: str | None = None) -> dict:
        """put a decision to a person. all four lines are required, and so is a deadline."""
        if category not in WAKE_CATEGORIES:
            raise ValidationError(
                f"a person is woken for one of {list(WAKE_CATEGORIES)}, and a reversible "
                f"default is the org's to take", category=category)
        lines = {"what_happened": what_happened, "already_tried": already_tried,
                 "decision_needed": decision_needed, "default_action": default_action}
        for name, value in lines.items():
            if not " ".join(str(value or "").split()):
                raise ValidationError(f"an escalation states {name.replace('_', ' ')}, always",
                                      line=name)
        if reads_as_empty_default(default_action):
            raise ValidationError(
                "an escalation with no default is a hostage note: say what the org will do if "
                "nobody answers", line="default_action")
        refused = not_a_wake(what_happened, already_tried, decision_needed)
        if refused:
            raise ValidationError(
                "that is the org's to handle, not a person's to be woken for: " + refused[0],
                reasons=refused)
        minutes = float(default_after_minutes or 0)
        if minutes <= 0:
            raise ValidationError("an escalation needs a deadline after which the default is taken")
        agent = self.store.one("select team_id from agents where id = ?", (agent_id,))
        if not agent:
            raise NotFound(f"agent {agent_id} not found")
        team_id = agent["team_id"]
        if task_id:
            task = self.store.one("select team_id from tasks where id = ?", (task_id,))
            if not task or task["team_id"] != team_id:
                raise NotFound(f"card {task_id} is not in crew {team_id}")
        moment = now()
        escalation_id = new_id("esc")
        with self.store.tx() as c:
            c.execute(
                """insert into escalations (id, team_id, raised_by, task_id, category,
                       what_happened, already_tried, decision_needed, default_action,
                       default_after, state, month, raised_date, reached)
                   values (?,?,?,?,?,?,?,?,?,?, 'open', ?, ?, '[]')""",
                (escalation_id, team_id, agent_id, task_id, category,
                 " ".join(what_happened.split()), " ".join(already_tried.split()),
                 " ".join(decision_needed.split()), " ".join(default_action.split()),
                 moment + minutes * 60, month_key(moment), moment))
        self._record(team_id, agent_id, "escalation.raised", escalation_id,
                     {"category": category, "default_after_minutes": minutes})
        return self._row(escalation_id)

    # ---- the climb -----------------------------------------------------
    def due_rungs(self, escalation: dict, at: float | None = None) -> list[dict]:
        """the rungs whose time has come and have not yet been reached, in order."""
        moment = at if at is not None else now()
        elapsed = (moment - escalation["raised_date"]) / 60.0
        reached = {item["rung"] for item in escalation["reached"]}
        return [step for step in self.ladder(escalation["team_id"])
                if step["after_minutes"] <= elapsed and step["rung"] not in reached]

    def _target(self, escalation: dict, rung: str) -> str | None:
        """who is behind a rung, or None when nobody is. a rung with no one behind it is named
        and skipped rather than stalled on."""
        agent_id = escalation["raised_by"]
        if rung == "person":
            return "person"
        chain = self.departments.chain(agent_id) if self.departments is not None else []
        if rung == "next_holder":
            if escalation["task_id"] and self.departments is not None:
                row = self.store.one("select assignee_agent_id from tasks where id = ?",
                                     (escalation["task_id"],))
                holder = row["assignee_agent_id"] if row else None
                return holder if holder and holder != agent_id else None
            return None
        if rung == "coordinator":
            first = chain[0] if chain else None
            target = first["coordinator_agent_id"] if first else None
            return target if target and target != agent_id else None
        if rung == "parent_coordinator":
            parent = chain[1] if len(chain) > 1 else None
            target = parent["coordinator_agent_id"] if parent else None
            return target if target and target != agent_id else None
        return None

    def climb(self, escalation_id: str, at: float | None = None) -> dict:
        """reach every rung that is due. idempotent: a rung already reached is not reached twice,
        and a rung with nobody behind it is recorded as skipped so the gap is visible."""
        escalation = self._row(escalation_id)
        if escalation["state"] != "open":
            return {"escalation": escalation, "reached": [], "skipped": []}
        moment = at if at is not None else now()
        reached: list[dict] = []
        skipped: list[dict] = []
        log = list(escalation["reached"])
        for step in self.due_rungs(escalation, at=moment):
            target = self._target(escalation, step["rung"])
            entry = {"rung": step["rung"], "at": moment, "target": target}
            if target is None:
                entry["skipped"] = True
                skipped.append(entry)
            else:
                reached.append(entry)
                if target != "person" and self.a2a is not None:
                    self.a2a.send(
                        team_id=escalation["team_id"], from_agent_id=escalation["raised_by"],
                        to_agent_id=target, task_id=escalation["task_id"], kind="blocker",
                        body=self.four_lines(escalation))
            log.append(entry)
        if reached or skipped:
            with self.store.tx() as c:
                c.execute("update escalations set reached = ? where id = ?",
                          (dumps(log), escalation_id))
            for entry in reached:
                self._record(escalation["team_id"], escalation["raised_by"],
                             "escalation.reached", escalation_id,
                             {"rung": entry["rung"], "target": entry["target"]})
        return {"escalation": self._row(escalation_id), "reached": reached, "skipped": skipped}

    def four_lines(self, escalation: dict) -> str:
        """what a person reads. the four lines in the owner's order, and nothing else."""
        return "\n".join([
            f"what happened: {escalation['what_happened']}",
            f"what the org already tried: {escalation['already_tried']}",
            f"the decision needed: {escalation['decision_needed']}",
            f"the default it takes if nobody answers: {escalation['default_action']}",
        ])

    # ---- settling ------------------------------------------------------
    def answer(self, escalation_id: str, by: str, answer: str) -> dict:
        """a person answers. an agent cannot: an escalation is a human act by definition."""
        escalation = self._row(escalation_id)
        if escalation["state"] != "open":
            raise Conflict(f"escalation {escalation_id} is already {escalation['state']}",
                           state=escalation["state"])
        if self.store.one("select id from agents where id = ?", (by,)):
            raise ValidationError("an escalation is answered by a person, never by an agent",
                                  by=by)
        answer = " ".join(str(answer or "").split())
        if not answer:
            raise ValidationError("an answer says what was decided")
        with self.store.tx() as c:
            c.execute(
                """update escalations set state = 'answered', answer = ?, answered_by = ?,
                       outcome = 'person_decided', settled_date = ? where id = ?""",
                (answer, by, now(), escalation_id))
        self._record(escalation["team_id"], escalation["raised_by"], "escalation.answered",
                     escalation_id, {"category": escalation["category"]},
                     actor_kind="human", actor_id=by)
        return self._row(escalation_id)

    def withdraw(self, escalation_id: str, agent_id: str) -> dict:
        escalation = self._row(escalation_id)
        if escalation["state"] != "open":
            raise Conflict(f"escalation {escalation_id} is already {escalation['state']}",
                           state=escalation["state"])
        if agent_id != escalation["raised_by"]:
            raise ValidationError("only the agent that raised an escalation withdraws it")
        with self.store.tx() as c:
            c.execute("update escalations set state = 'withdrawn', outcome = 'withdrawn', "
                      "settled_date = ? where id = ?", (now(), escalation_id))
        return self._row(escalation_id)

    def take_defaults(self, team_id: str, at: float | None = None) -> list[dict]:
        """every open escalation past its deadline resolves to its stated default. recorded as the
        org acting, never as a person agreeing: the outcome is its own value so silence cannot be
        read as consent later."""
        moment = at if at is not None else now()
        taken: list[dict] = []
        for row in self.store.query(
                """select id from escalations where team_id = ? and state = 'open'
                   and default_after <= ?""", (team_id, moment)):
            escalation = self._row(row["id"])
            with self.store.tx() as c:
                c.execute(
                    """update escalations set state = 'defaulted', outcome = 'org_took_default',
                           answer = default_action, settled_date = ? where id = ?""",
                    (moment, escalation["id"]))
            self._record(escalation["team_id"], escalation["raised_by"],
                         "escalation.defaulted", escalation["id"],
                         {"default": escalation["default_action"],
                          "outcome": "org_took_default"},
                         actor_kind="system", actor_id="escalation.tick")
            taken.append(self._row(escalation["id"]))
        return taken

    def tick(self, team_id: str, at: float | None = None) -> dict:
        """one pass: climb what is due, then take defaults that are past their deadline. it
        decides nothing the record did not already state."""
        moment = at if at is not None else now()
        climbed = []
        for row in self.store.query(
                "select id from escalations where team_id = ? and state = 'open'", (team_id,)):
            result = self.climb(row["id"], at=moment)
            if result["reached"] or result["skipped"]:
                climbed.append({"id": row["id"], "reached": result["reached"],
                                "skipped": result["skipped"]})
        return {"climbed": climbed, "defaulted": self.take_defaults(team_id, at=moment)}

    # ---- reading -------------------------------------------------------
    def list(self, team_id: str, state: str | None = None) -> list[dict]:
        if state is not None and state not in STATES:
            raise ValidationError(f"state must be one of {list(STATES)}", state=state)
        sql = "select id from escalations where team_id = ?"
        args: list[Any] = [team_id]
        if state:
            sql += " and state = ?"
            args.append(state)
        return [self._row(row["id"]) for row in self.store.query(
            sql + " order by raised_date desc", tuple(args))]

    def spend(self, team_id: str, month: str | None = None) -> dict:
        """what this crew has used of its month. over budget is a finding about scoping."""
        month = month or month_key(now())
        used = self.store.one(
            "select count(*) as c from escalations where team_id = ? and month = ?",
            (team_id, month))["c"]
        budget = self.budget(team_id)
        over = used > budget
        return {"month": month, "used": used, "budget": budget, "over": over,
                "finding": ("this crew is mis-scoped: it asked a person more than its budget "
                            "allows, so the work moved instead of getting done" if over else ""),
                "defaulted": self.store.one(
                    """select count(*) as c from escalations where team_id = ? and month = ?
                       and outcome = 'org_took_default'""", (team_id, month))["c"]}

    def reading(self, team_id: str, at: float | None = None) -> dict:
        moment = at if at is not None else now()
        open_ones = self.list(team_id, state="open")
        return {
            "team_id": team_id,
            "spend": self.spend(team_id),
            "open": open_ones,
            "overdue": [e for e in open_ones if e["default_after"] <= moment],
            "ladder": self.ladder(team_id),
        }

    def _record(self, team_id: str, agent_id: str, action: str, escalation_id: str,
                payload: dict, actor_kind: str = "agent", actor_id: str = "") -> None:
        """the row belongs to the agent whose work the escalation is about, the same rule the
        silence decisions follow, with the person named as the actor so a human answer is never
        read as the agent's. a person who is not an agent has no trail of their own to hang it on."""
        if self.audit is None:
            return
        self.audit.record(team_id=team_id, agent_id=agent_id, action=action,
                          actor_kind=actor_kind, actor_id=actor_id or agent_id,
                          target_kind="escalation", target_id=escalation_id, payload=payload)
