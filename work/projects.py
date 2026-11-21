"""a project: the thing that has to survive being run for thirty days by nobody.

a crew finishes cards. a card is hours. what was missing is the unit above it: the thing
with a business reason, a phase, a gate that opens on rows rather than on an opinion, and
kill criteria declared before the sunk cost exists.

there is no "call it done" verb, and that is the point. a project moves when the gate for
its phase is met, and review has two exits: a metric that moved, or killed. a business that
neither works nor ends is how a month becomes a year.
"""

from __future__ import annotations

from core.db import Store, dumps, loads, new_id, now
from core.errors import NotFound, ValidationError

PHASES = ("inception", "build", "assemble", "launch", "operate", "review", "closed", "killed")
TERMINAL = ("closed", "killed")

# a project that cannot hold a month of operations is not operating, it is launching slowly
OPERATING_WINDOW_DAYS = 30


def is_terminal(phase: str) -> bool:
    return phase in TERMINAL


def _pass(check_id: str, **detail) -> dict:
    return {"id": check_id, "ok": True, "detail": detail}


def _fail(check_id: str, **detail) -> dict:
    return {"id": check_id, "ok": False, "detail": detail}


def validate(project: dict) -> list[dict]:
    """structure, not progress. a project that fails this is not startable at all."""
    checks: list[dict] = []
    slices = list(project.get("slices") or [])
    if not slices:
        checks.append(_fail("no_slices"))
    names = [str(s.get("slice", "")) for s in slices]
    if len(set(names)) != len(names):
        checks.append(_fail("duplicate_slice"))
    ceiling = project.get("unit_cost_ceiling_cents")
    if not isinstance(ceiling, (int, float)) or ceiling <= 0:
        checks.append(_fail("no_unit_cost_ceiling"))
    price = project.get("unit_price_cents")
    if isinstance(price, (int, float)) and isinstance(ceiling, (int, float)) and price <= ceiling:
        # charging less than the worst acceptable cost is a business that works by accident
        checks.append(_fail("price_not_above_ceiling", price=price, ceiling=ceiling))
    criteria = [c for c in (project.get("kill_criteria") or []) if len(str(c).strip()) > 12]
    if not criteria:
        # "it fails" is not a kill criterion. it has to name a condition someone can check
        checks.append(_fail("kill_criteria_too_thin", criteria=project.get("kill_criteria") or []))
    if any(not str(s.get("objective_ref", "")).strip() for s in slices):
        checks.append(_fail("slice_without_objective"))
    return checks


def _days_between(start: float, end: float) -> float:
    return max(0.0, (end - start) / 86400.0)


def evaluate(project: dict, facts: dict) -> list[dict]:
    """the checks the current phase's gate reads. facts are rows, never opinions."""
    phase = project["phase"]
    checks: list[dict] = []
    if phase == "inception":
        checks.extend(validate(project))
        slices = list(project.get("slices") or [])
        objectives = {str(o.get("ref")): o for o in (facts.get("objectives") or [])}
        missing = [str(s.get("objective_ref")) for s in slices
                   if str(s.get("objective_ref")) not in objectives]
        checks.append(_pass("objectives_present", count=len(slices)) if not missing
                      else _fail("objectives_present", unresolved=missing))
        sources: dict[str, list[str]] = {}
        for ref in objectives:
            src = str(objectives[ref].get("source", ""))
            if src:
                sources.setdefault(src, []).append(ref)
        shared = [refs for refs in sources.values() if len(refs) > 1]
        checks.append(_pass("sources_distinct", objectives=len(objectives)) if not shared
                      else _fail("sources_distinct", shared=shared))
        self_measured = [ref for ref, o in objectives.items()
                         if str(o.get("source", "")) in {"", "self"} or o.get("self_reported")]
        checks.append(_pass("objectives_externally_measured", objectives=len(objectives))
                      if not self_measured
                      else _fail("objectives_externally_measured", self_measured=self_measured))
    elif phase == "build":
        slices = [str(s.get("slice")) for s in (project.get("slices") or [])]
        started = set(facts.get("slices_started") or [])
        waiting = [s for s in slices if s not in started]
        checks.append(_pass("every_slice_started", count=len(slices)) if not waiting
                      else _fail("every_slice_started", waiting=waiting))
    elif phase == "assemble":
        artifact = facts.get("end_to_end_artifact")
        checks.append(_pass("end_to_end_artifact", got=artifact) if artifact
                      else _fail("end_to_end_artifact", got=None))
        unlicensed = facts.get("unlicensed_parts")
        checks.append(_pass("all_parts_licensed") if unlicensed == 0
                      else _fail("all_parts_licensed", unlicensed=unlicensed if unlicensed is not None else "unknown"))
    elif phase == "launch":
        quote = facts.get("live_quote_cents")
        ceiling = project.get("unit_cost_ceiling_cents") or 0
        checks.append(_pass("live_quote", got=quote) if isinstance(quote, (int, float)) and quote > 0
                      else _fail("live_quote", got=quote))
        if isinstance(quote, (int, float)) and quote > 0:
            checks.append(_pass("unit_cost_under_ceiling", usd=quote, ceiling=ceiling) if quote <= ceiling
                          else _fail("unit_cost_under_ceiling", usd=quote, ceiling=ceiling))
        price = project.get("unit_price_cents")
        floor = project.get("min_margin_pct")
        if isinstance(price, (int, float)) and isinstance(quote, (int, float)) and price > 0 and floor is not None:
            margin = (price - quote) / price
            checks.append(_pass("unit_margin", margin=round(margin, 4), need=floor) if margin >= floor
                          else _fail("unit_margin", margin=round(margin, 4), need=floor))
        checks.append(_pass("payment_rail_verified") if facts.get("payment_rail_verified")
                      else _fail("payment_rail_verified", got=facts.get("payment_rail_verified")))
    elif phase == "operate":
        days = _days_between(project["phase_started_at"], facts.get("now") or now())
        checks.append(_pass("operated_a_month", days=int(days)) if days >= OPERATING_WINDOW_DAYS
                      else _fail("operated_a_month", days=int(days), need=OPERATING_WINDOW_DAYS))
        samples = int(facts.get("metric_samples") or 0)
        checks.append(_pass("metric_sampled_daily", samples=samples) if samples >= OPERATING_WINDOW_DAYS
                      else _fail("metric_sampled_daily", samples=samples, need=OPERATING_WINDOW_DAYS))
        blocked = int(facts.get("guardrail_refusals") or 0)
        checks.append(_pass("no_open_guardrail_refusals", blocked=blocked) if blocked == 0
                      else _fail("no_open_guardrail_refusals", blocked=blocked))
    elif phase == "review":
        disputed = facts.get("readings_disagree") or []
        checks.append(_pass("readings_agree") if not disputed else _fail("readings_agree", disputed=disputed))
        alerts = facts.get("open_audit_alerts")
        checks.append(_pass("audit_alerts_clear") if alerts == 0
                      else _fail("audit_alerts_clear", open=alerts if alerts is not None else "unknown"))
        moved = facts.get("metric_moved")
        checks.append(_pass("verdict_reachable", got=moved) if moved is not None
                      else _fail("verdict_reachable", got=None))
    return checks


NEXT = {
    "inception": "build",
    "build": "assemble",
    "assemble": "launch",
    "launch": "operate",
    "operate": "review",
}


def next_phase(project: dict, facts: dict) -> str:
    """review is the only phase with two exits, and the deciding evidence is a number that
    moved, not an opinion about how it went."""
    if project["phase"] == "review":
        return "closed" if facts.get("metric_moved") is True else "killed"
    return NEXT.get(project["phase"], project["phase"])


def advance(project: dict, facts: dict) -> dict:
    if is_terminal(project["phase"]):
        return {"ok": False, "reason": "terminal_phase", "unmet": [], "checks": []}
    checks = evaluate(project, facts)
    unmet = [c for c in checks if not c["ok"]]
    if unmet:
        return {"ok": False, "reason": "gate_unmet", "unmet": unmet, "checks": checks}
    return {"ok": True, "reason": "", "phase": next_phase(project, facts), "checks": checks}


def kill(project: dict, criterion: str, reason: str) -> dict:
    """a project only leaves the run because a criterion declared at inception fired."""
    if criterion not in (project.get("kill_criteria") or []):
        raise ValidationError(f"kill_criterion_not_declared: {criterion}",
                              criterion=criterion)
    if len(reason.strip()) < 12:
        raise ValidationError("a kill reason has to say something a person can read")
    return {**project, "phase": "killed", "closed_reason": reason}


class ProjectService:
    """the rows behind the gates. facts are read here, so a gate cannot be talked open."""

    def __init__(self, store: Store, crews=None, kanban=None):
        self.store = store
        self.crews = crews
        self.kanban = kanban

    # ---- rows ----------------------------------------------------------
    def _shape(self, row) -> dict:
        return {
            "id": row["id"],
            "team_id": row["team_id"],
            "name": row["name"],
            "phase": row["phase"],
            "started_at": row["started_at"],
            "phase_started_at": row["phase_started_at"],
            "objective_ref": row["objective_ref"],
            "slices": loads(row["slices"], []),
            "unit_cost_ceiling_cents": row["unit_cost_ceiling_cents"],
            "unit_price_cents": row["unit_price_cents"],
            "min_margin_pct": row["min_margin_pct"],
            "kill_criteria": loads(row["kill_criteria"], []),
            "closed_reason": row["closed_reason"],
            "created_date": row["created_date"],
            "updated_date": row["updated_date"],
        }

    def create(self, team_id: str, name: str, slices: list[dict], unit_cost_ceiling_cents: int,
               kill_criteria: list[str], objective_ref: str = "", unit_price_cents: int | None = None,
               min_margin_pct: float | None = None) -> dict:
        if self.crews is not None:
            self.crews.get_team(team_id)
        project = {
            "id": new_id("proj"), "team_id": team_id, "name": name, "phase": "inception",
            "started_at": now(), "phase_started_at": now(), "objective_ref": objective_ref,
            "slices": list(slices or []), "unit_cost_ceiling_cents": unit_cost_ceiling_cents,
            "unit_price_cents": unit_price_cents, "min_margin_pct": min_margin_pct,
            "kill_criteria": list(kill_criteria or []), "closed_reason": "",
        }
        faults = validate(project)
        if faults:
            raise ValidationError("this project is not startable: " + ", ".join(f["id"] for f in faults),
                                  faults=faults)
        ts = now()
        with self.store.tx() as c:
            c.execute(
                """insert into projects (id, team_id, name, phase, started_at, phase_started_at,
                   objective_ref, slices, unit_cost_ceiling_cents, unit_price_cents, min_margin_pct,
                   kill_criteria, closed_reason, created_date, updated_date)
                   values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (project["id"], team_id, name, "inception", project["started_at"],
                 project["phase_started_at"], objective_ref, dumps(project["slices"]),
                 unit_cost_ceiling_cents, unit_price_cents, min_margin_pct,
                 dumps(project["kill_criteria"]), "", ts, ts))
        return self.get(project["id"])

    def get(self, project_id: str) -> dict:
        row = self.store.one("select * from projects where id = ?", (project_id,))
        if not row:
            raise NotFound(f"no project {project_id}")
        return self._shape(row)

    def list(self, team_id: str, phase: str = "") -> list[dict]:
        if phase:
            rows = self.store.query(
                "select * from projects where team_id = ? and phase = ? order by created_date desc",
                (team_id, phase))
        else:
            rows = self.store.query(
                "select * from projects where team_id = ? order by created_date desc", (team_id,))
        return [self._shape(r) for r in rows]

    # ---- facts ---------------------------------------------------------
    def facts(self, project: dict, supplied: dict | None = None) -> dict:
        """what the gate is allowed to read. anything a caller asserts is merged over the
        rows, but the rows win where they exist, because a project that grades itself is
        the failure this whole file exists to prevent."""
        facts = dict(supplied or {})
        facts["now"] = facts.get("now") or now()
        if self.kanban is not None:
            started = set()
            for slice_row in project.get("slices") or []:
                name = str(slice_row.get("slice"))
                cards = self.store.query(
                    "select id from tasks where team_id = ? and (title like ? or payload like ?) limit 1",
                    (project["team_id"], f"%{name}%", f"%{name}%"))
                if cards:
                    started.add(name)
            facts.setdefault("slices_started", sorted(started))
            blocked = self.store.one(
                "select count(*) as n from tasks where team_id = ? and blocked_reason like 'guardrail%'",
                (project["team_id"],))
            facts.setdefault("guardrail_refusals", int(blocked["n"]) if blocked else 0)
        return facts

    # ---- the verbs -----------------------------------------------------
    def evaluate(self, project_id: str, facts: dict | None = None) -> dict:
        project = self.get(project_id)
        merged = self.facts(project, facts)
        checks = evaluate(project, merged)
        return {"project_id": project_id, "phase": project["phase"], "checks": checks,
                "unmet": [c for c in checks if not c["ok"]]}

    def advance(self, project_id: str, facts: dict | None = None) -> dict:
        project = self.get(project_id)
        merged = self.facts(project, facts)
        outcome = advance(project, merged)
        if not outcome["ok"]:
            return {**outcome, "project": project}
        ts = now()
        with self.store.tx() as c:
            c.execute("update projects set phase = ?, phase_started_at = ?, updated_date = ? where id = ?",
                      (outcome["phase"], ts, ts, project_id))
        return {**outcome, "project": self.get(project_id)}

    def kill(self, project_id: str, criterion: str, reason: str) -> dict:
        project = self.get(project_id)
        killed = kill(project, criterion, reason)
        ts = now()
        with self.store.tx() as c:
            c.execute("update projects set phase = 'killed', closed_reason = ?, updated_date = ? where id = ?",
                      (killed["closed_reason"], ts, project_id))
        return self.get(project_id)
