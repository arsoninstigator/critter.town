"""limits that hold when nobody is awake to answer a gate.

a gate is a yes. a guardrail is a no that was decided in advance, so the difference is
not severity but attendance: a guardrail has to be answerable at 3am by the process
itself. that is why every `value` here is a number or a list and never prose. a limit
that needs interpreting is a gate wearing the wrong name, and it will be interpreted
generously by the thing it is meant to stop.

two rules the rest of the code relies on:

* a refusal is not a failure. a refused card goes to `blocked` carrying the guardrail
  that refused it, and the morning brief counts refusals apart from errors, because an
  agent that could not start is a different problem from an agent that tried and broke.
* `check` is the only entry point. dispatch, sandbox creation and the egress check all
  ask the same question, so a limit cannot be enforced in one path and forgotten in
  another.
"""

from __future__ import annotations

import re
import time
from typing import Any

from core.db import Store, dumps, loads, new_id, now
from core.errors import NotAllowed, NotFound, ValidationError

# a number (per day or at once) or a list (hosts, tools)
NUMERIC = ("spend_per_day", "open_cards", "parallel_runs", "card_moves_per_day",
           "max_output_chars")
LISTS = ("egress_hosts", "forbidden_tools", "secret_shapes", "forbidden_patterns")
# a limit on what the answer carries out, which nothing before dispatch can see
CONTENT = ("secret_shapes", "forbidden_patterns", "max_output_chars", "foreign_refs")
KINDS = NUMERIC + LISTS + ("foreign_refs",)

# which action each kind can answer
ANSWERS = {
    "dispatch": ("open_cards", "parallel_runs", "spend_per_day", "forbidden_tools"),
    "sandbox": ("forbidden_tools",),
    "egress": ("egress_hosts",),
    "card_move": ("card_moves_per_day",),
    # the only action that runs after the model has spoken
    "output": CONTENT,
}

# the shapes a secret takes in practice. these are matched, never echoed: a rail that
# prints what it caught writes the secret into the log it was protecting.
SECRET_SHAPES = {
    "private_key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    # the negative lookahead is not fussiness: without it every anthropic key is
    # reported as an openai key, and a refusal that names the wrong provider sends
    # whoever reads it to rotate the wrong credential
    "openai_key": r"\bsk-(?!ant-)[A-Za-z0-9_-]{20,}",
    "anthropic_key": r"\bsk-ant-[A-Za-z0-9_-]{20,}",
    "github_token": r"\bgh[pousr]_[A-Za-z0-9]{20,}",
    "aws_key": r"\bAKIA[0-9A-Z]{16}\b",
    "slack_token": r"\bxox[abprs]-[A-Za-z0-9-]{10,}",
    "jwt": r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.",
    "bearer": r"\bBearer\s+[A-Za-z0-9._-]{20,}",
    "env_assignment": r"\b[A-Z][A-Z0-9_]*(?:TOKEN|KEY|SECRET|PASSWORD)\s*=\s*\S{8,}",
}


def day_bounds(ts: float | None = None) -> tuple[float, float]:
    """the calendar day in utc. a daily limit that resets when someone feels like it
    is not a daily limit."""
    ts = now() if ts is None else ts
    start = ts - (ts % 86400)
    return start, start + 86400


class GuardrailService:
    def __init__(self, store: Store, kanban=None, crews=None, audit=None):
        self.store = store
        self.kanban = kanban
        self.crews = crews
        self.audit = audit
        # set by the composition root: the ledger the spend limit is measured against
        self.budget = None
        # and the count behind a freeze, so a refusal costs the seat something
        self.strikes = None

    # ---- rows ----------------------------------------------------------
    def _row(self, row) -> dict:
        return {
            "id": row["id"],
            "team_id": row["team_id"],
            "agent_id": row["agent_id"],
            "kind": row["kind"],
            "value": loads(row["value"], None),
            "status": row["status"],
            "actor": row["actor"],
            "created_date": row["created_date"],
            "updated_date": row["updated_date"],
        }

    def set(
        self,
        team_id: str,
        kind: str,
        value: Any,
        agent_id: str = "",
        actor: str = "human",
    ) -> dict:
        """set a limit. setting the same (crew, agent, kind) again replaces it: one
        live limit per slot, so there is never an argument about which one applies."""
        if kind not in KINDS:
            raise ValidationError(f"kind must be one of {list(KINDS)}")
        if kind in NUMERIC:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValidationError(f"{kind} takes a number")
            if value < 0:
                raise ValidationError(f"{kind} cannot be negative")
        elif kind == "foreign_refs":
            # a switch, not a number: naming another crew's card in an answer is either
            # allowed or it is not, and there is nothing to count
            if not isinstance(value, bool):
                raise ValidationError("foreign_refs takes true or false")
        elif kind == "forbidden_patterns":
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ValidationError("forbidden_patterns takes a list of patterns")
            for pattern in value:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    # a pattern that does not compile would pass every answer, which is
                    # a rail that is not there. refused at set time instead.
                    raise ValidationError(
                        f"that pattern does not compile: {exc}", pattern=pattern) from exc
            value = [v for v in value if v.strip()]
        elif kind == "secret_shapes":
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ValidationError("secret_shapes takes a list of shape names")
            # "all" is the shorthand for every shape, so it is a shape name too
            unknown = sorted(set(value) - set(SECRET_SHAPES) - {"all"})
            if unknown:
                raise ValidationError(
                    f"unknown secret shape(s): {', '.join(unknown)}; "
                    f"known ones are {sorted(SECRET_SHAPES)}")
            value = sorted(set(value))
        else:
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ValidationError(f"{kind} takes a list of strings")
            value = [v.strip() for v in value if v.strip()]
        ts = now()
        existing = self.store.one(
            "select id from guardrails where team_id = ? and agent_id = ? and kind = ?",
            (team_id, agent_id, kind),
        )
        if existing:
            with self.store.tx() as c:
                c.execute(
                    "update guardrails set value = ?, status = 'active', actor = ?,"
                    " updated_date = ? where id = ?",
                    (dumps(value), actor, ts, existing["id"]),
                )
            rail_id = existing["id"]
        else:
            rail_id = new_id("rail")
            with self.store.tx() as c:
                c.execute(
                    """insert into guardrails (id, team_id, agent_id, kind, value, status,
                       actor, created_date, updated_date) values (?,?,?,?,?,?,?,?,?)""",
                    (rail_id, team_id, agent_id, kind, dumps(value), "active", actor, ts, ts),
                )
        self._audit("guardrail.set", team_id, agent_id,
                    {"guardrail_id": rail_id, "kind": kind, "value": value}, actor)
        return self.get(rail_id)

    def get(self, rail_id: str) -> dict:
        row = self.store.one("select * from guardrails where id = ?", (rail_id,))
        if not row:
            raise NotFound(f"guardrail {rail_id} not found")
        return self._row(row)

    def retire(self, rail_id: str, actor: str = "human") -> dict:
        rail = self.get(rail_id)
        with self.store.tx() as c:
            c.execute(
                "update guardrails set status = 'retired', actor = ?, updated_date = ?"
                " where id = ?",
                (actor, now(), rail_id),
            )
        self._audit("guardrail.retired", rail["team_id"], rail["agent_id"],
                    {"guardrail_id": rail_id, "kind": rail["kind"]}, actor)
        return self.get(rail_id)

    def list(self, team_id: str, agent_id: str | None = None, status: str = "active") -> list[dict]:
        sql = "select * from guardrails where team_id = ? and status = ?"
        params: list = [team_id, status]
        if agent_id is not None:
            sql += " and agent_id = ?"
            params.append(agent_id)
        sql += " order by kind, agent_id"
        return [self._row(r) for r in self.store.query(sql, tuple(params))]

    # ---- the one question ----------------------------------------------
    def check(self, team_id: str, action: str, context: dict | None = None) -> dict:
        """may this happen. the answer names the row that said no, so the run's error
        can say "open_cards: 60 of 50" instead of "denied"."""
        if action not in ANSWERS:
            raise ValidationError(f"unknown action {action}; expected one of {list(ANSWERS)}")
        context = context or {}
        agent_id = str(context.get("agent_id") or "")
        rails = [r for r in self.list(team_id) if r["agent_id"] in ("", agent_id)]
        for rail in rails:
            kind = rail["kind"]
            if kind not in ANSWERS[action]:
                continue
            verdict = self._verdict(rail, action, context)
            if verdict:
                return {
                    "allowed": False,
                    "guardrail_id": rail["id"],
                    "kind": kind,
                    "value": rail["value"],
                    "reason": verdict,
                    "agent_id": rail["agent_id"],
                    "action": action,
                }
        return {"allowed": True, "action": action}

    # ---- the output rail -------------------------------------------------
    def check_output(self, team_id: str, text: str, agent_id: str = "",
                     context: dict | None = None) -> dict:
        """what the answer carries out. the one place a rail runs after the fact.

        every other action here happens before the work: dispatch, sandbox, egress, a
        card move. none of them can see an answer, because the answer does not exist
        yet, so a secret pasted into a message, another crew's card quoted back, or a
        tool call proposed in prose all leave through a door no pre-dispatch rail covers.

        the refusal names the rail, the shape and where in the text it was, and never
        the text itself: a rail that echoes what it caught has written the secret into
        the log it was protecting.
        """
        context = context or {}
        rails = [r for r in self.list(team_id)
                 if r["kind"] in CONTENT and r["agent_id"] in ("", agent_id)]
        for rail in rails:
            found = self._output_verdict(rail, text or "", context)
            if found:
                return {"allowed": False, "guardrail_id": rail["id"], "kind": rail["kind"],
                        "value": rail["value"], "reason": found["reason"],
                        "at": found["at"], "agent_id": rail["agent_id"],
                        "action": "output", "chars": len(text or "")}
        return {"allowed": True, "action": "output", "chars": len(text or "")}

    def enforce_output(self, team_id: str, text: str, agent_id: str = "",
                       context: dict | None = None, task: dict | None = None,
                       run_id: str = "") -> dict:
        """check the answer, and if it is refused, leave the receipt and raise.

        the receipt is a row rather than a task event, because an output refusal has no
        card movement to attach to and the answer itself must not be stored. what is
        stored is the rail, the shape, the offset and the length, which is enough to
        find the line in the run's own steps and not enough to leak it.
        """
        verdict = self.check_output(team_id, text, agent_id=agent_id, context=context)
        if verdict["allowed"]:
            return verdict
        with self.store.tx() as c:
            c.execute(
                """insert into rail_refusals
                   (id, team_id, agent_id, task_id, run_id, guardrail_id, kind, action,
                    reason, at, chars, created_date)
                   values (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (new_id("rrf"), team_id, agent_id, (task or {}).get("id", "") or "",
                 run_id, verdict["guardrail_id"], verdict["kind"], "output",
                 verdict["reason"], int(verdict.get("at") or 0), verdict["chars"], now()))
        if self.strikes is not None and agent_id:
            try:
                self.strikes.record(team_id, agent_id, "output_refused",
                                    tool=verdict["kind"], note=str(verdict["reason"])[:200])
            except Exception:
                pass
        self._audit("guardrail.refused", team_id, agent_id,
                    {"guardrail_id": verdict["guardrail_id"], "action": "output",
                     "kind": verdict["kind"], "reason": verdict["reason"]}, "guardrail")
        raise NotAllowed(verdict["reason"], guardrail=verdict["guardrail_id"],
                         kind=verdict["kind"], action="output")

    def refusals(self, team_id: str, limit: int = 50) -> list[dict]:
        rows = self.store.query(
            """select * from rail_refusals where team_id = ?
               order by created_date desc limit ?""", (team_id, limit))
        return [{"id": r["id"], "agent_id": r["agent_id"], "task_id": r["task_id"],
                 "run_id": r["run_id"], "guardrail_id": r["guardrail_id"],
                 "kind": r["kind"], "action": r["action"], "reason": r["reason"],
                 "at": r["at"], "chars": r["chars"], "created_date": r["created_date"]}
                for r in rows]

    def _output_verdict(self, rail: dict, text: str, context: dict) -> dict | None:
        kind, value = rail["kind"], rail["value"]
        if kind == "max_output_chars":
            if len(text) > int(value):
                return {"reason": f"max_output_chars: the answer is {len(text)} characters, "
                                  f"the limit is {int(value)}", "at": int(value)}
            return None
        if kind == "secret_shapes":
            shapes = list(SECRET_SHAPES) if value == ["all"] else list(value)
            for name in shapes:
                pattern = SECRET_SHAPES.get(name)
                if not pattern:
                    continue
                match = re.search(pattern, text)
                if match:
                    return {"reason": f"secret_shapes: {name} at character {match.start()} "
                                      f"({match.end() - match.start()} characters)",
                            "at": match.start()}
            return None
        if kind == "forbidden_patterns":
            for pattern in value:
                match = re.search(pattern, text)
                if match:
                    return {"reason": f"forbidden_patterns: {pattern} matched at character "
                                      f"{match.start()}", "at": match.start()}
            return None
        if kind == "foreign_refs":
            if value is False:
                return None
            foreign = [ref for ref in (context.get("foreign_refs") or [])
                       if ref and ref in text]
            if foreign:
                # the id is named, the card behind it is not: the refusal goes to the
                # crew that owns the run, and the other crew's title is not its business
                return {"reason": f"foreign_refs: the answer names {len(foreign)} id(s) "
                                  f"from another crew", "at": text.find(foreign[0])}
            return None
        return None

    def _audit(self, event: str, team_id: str, agent_id: str, payload: dict,
               actor: str) -> None:
        """audit through `record_event`, which decides attribution. a team wide limit
        that refused nothing in particular is not faked into an agent's trail."""
        if self.audit is None:
            return
        self.audit.record_event(event, {
            "team_id": team_id,
            "agent_id": agent_id,
            "actor": actor,
            "data": payload,
        })

    def _verdict(self, rail: dict, action: str, context: dict) -> str:
        """the sentence for a refusal, or "" to allow."""
        kind, value = rail["kind"], rail["value"]

        if kind == "open_cards":
            limit = int(value)
            open_now = self._open_cards(rail["team_id"], rail["agent_id"])
            if open_now >= limit:
                return f"open_cards: {open_now} open cards, the limit is {limit}"

        elif kind == "parallel_runs":
            limit = int(value)
            running = self._parallel_runs(rail["team_id"], rail["agent_id"])
            if running >= limit:
                return f"parallel_runs: {running} running, the limit is {limit}"

        elif kind == "card_moves_per_day":
            limit = int(value)
            moved = self._card_moves_today(rail["team_id"])
            if moved >= limit:
                return f"card_moves_per_day: {moved} moves today, the limit is {limit}"

        elif kind == "forbidden_tools":
            wanted = {str(t).lower() for t in (context.get("tools") or [])}
            hit = sorted(wanted & {str(t).lower() for t in value})
            if hit:
                return f"forbidden_tools: {', '.join(hit)} is not allowed for this crew"

        elif kind == "egress_hosts":
            host = str(context.get("host") or "").lower()
            allowed = [str(h).lower() for h in value]
            if host and not any(host == h or host.endswith("." + h) for h in allowed):
                return f"egress_hosts: {host} is not in this crew's allowlist"

        elif kind == "spend_per_day":
            # the figure comes from the budget ledger when one is wired: settled plus held,
            # because money that is committed is not money that is available. a caller may
            # still pass a number, which is what a deployment without a ledger does.
            spend = context.get("spend_today")
            if spend is None and self.budget is not None:
                spend = self.budget.committed_today(rail["team_id"])
            if spend is not None and float(spend) >= float(value):
                return f"spend_per_day: {spend} today, the limit is {value}"

        return ""

    # ---- what the limits are measured against --------------------------
    def _open_cards(self, team_id: str, agent_id: str = "") -> int:
        sql = ("select count(*) as n from tasks where team_id = ?"
               " and column_name in ('ready', 'in_progress')")
        params: list = [team_id]
        if agent_id:
            sql += " and assignee_agent_id = ?"
            params.append(agent_id)
        return int(self.store.one(sql, tuple(params))["n"])

    def _parallel_runs(self, team_id: str, agent_id: str = "") -> int:
        sql = ("select count(*) as n from runs r join tasks t on t.id = r.task_id"
               " where t.team_id = ? and r.status in ('queued', 'running')")
        params: list = [team_id]
        if agent_id:
            sql += " and r.agent_id = ?"
            params.append(agent_id)
        return int(self.store.one(sql, tuple(params))["n"])

    def _card_moves_today(self, team_id: str) -> int:
        start, end = day_bounds()
        row = self.store.one(
            """select count(*) as n from task_events e join tasks t on t.id = e.task_id
               where t.team_id = ? and e.kind = 'moved'
                 and e.created_date >= ? and e.created_date < ?""",
            (team_id, start, end),
        )
        return int(row["n"])

    # ---- enforcement ---------------------------------------------------
    def enforce(self, team_id: str, action: str, context: dict | None = None,
                task: dict | None = None) -> dict:
        """check, and if refused, leave the receipt before raising.

        the card is parked in `blocked` rather than left in `ready`, because a card
        that stays ready is a card every later tick will try again, and a limit should
        slow work down rather than make it noisy.
        """
        verdict = self.check(team_id, action, context)
        if verdict["allowed"]:
            return verdict
        if task and self.kanban is not None:
            try:
                self.kanban.move(task["id"], "blocked", f"guardrail:{verdict['guardrail_id']}")
            except Exception:
                pass  # a card already in blocked, or not movable from where it is
            with self.store.tx() as c:
                c.execute(
                    """insert into task_events (id, task_id, kind, data, actor, created_date)
                       values (?,?,?,?,?,?)""",
                    (new_id("evt"), task["id"], "guardrail_refused",
                     dumps({"guardrail_id": verdict["guardrail_id"], "kind": verdict["kind"],
                            "reason": verdict["reason"], "action": action}),
                     "guardrail", now()),
                )
        # a refusal is a strike: two of them freeze the seat, which is the difference
        # between a limit that is counted and a limit that is described
        if self.strikes is not None and task and task.get("assignee_agent_id"):
            try:
                self.strikes.record(team_id, task["assignee_agent_id"], "guardrail_refused",
                                    tool=action, note=str(verdict["reason"])[:200])
            except Exception:
                pass  # a strike that cannot be written must not hide the refusal itself
        self._audit("guardrail.refused", team_id,
                    (task or {}).get("assignee_agent_id", "") or "",
                    {"guardrail_id": verdict["guardrail_id"], "action": action,
                     "kind": verdict["kind"], "reason": verdict["reason"]}, "guardrail")
        raise NotAllowed(verdict["reason"], guardrail=verdict["guardrail_id"],
                         kind=verdict["kind"], action=action)

    def summary(self, team_id: str, date: str | None = None) -> dict:
        """what refused last night, for the brief. counts refusals apart from errors."""
        start, end = day_bounds()
        rows = self.store.query(
            """select e.data from task_events e join tasks t on t.id = e.task_id
               where t.team_id = ? and e.kind = 'guardrail_refused'
                 and e.created_date >= ? and e.created_date < ?""",
            (team_id, start, end),
        )
        # counted in python rather than by json_extract: the same query has to run on
        # sqlite and on a hosted postgres
        refused: dict[str, int] = {}
        for row in rows:
            kind = str((loads(row["data"], {}) or {}).get("kind") or "unknown")
            refused[kind] = refused.get(kind, 0) + 1
        return {"refused": refused, "total": sum(refused.values()),
                "limits": self.list(team_id)}
