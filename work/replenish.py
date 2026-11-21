"""an objective that is behind has to be able to ask for work.

until this existed the org finished what it was given. this turns a metric missing its target
into one concrete card, and it refuses often and loudly, because the failure mode of a machine
that can invent work is a thousand cards nobody can finish. so: one card per objective, a wip
limit regardless of seats, and a pace comparison against the clock rather than against the
target, since being below target at 5% of the period is on pace.

`observed_since` is the objective's first measurement, never its latest. measuring from the
latest collapses the period and makes every objective look ahead of itself.
"""

from __future__ import annotations

from core.db import Store, dumps, loads, new_id, now
from core.errors import NotFound, ValidationError
from work.evidence import KIND_EVIDENCE

SLACK = 0.15
WIP_LIMIT = 2
STATUSES = ("active", "met", "failed", "retired", "superseded")


def pace(objective: dict, value: float | None, at: float) -> dict:
    """how far through the period we are against how far through the work we are."""
    if value is None:
        return {"done": None, "elapsed": None, "behind": None}
    span = float(objective["target"]) - float(objective["baseline"])
    if span == 0:
        return {"done": 1.0, "elapsed": 1.0, "behind": False}
    done = (float(value) - float(objective["baseline"])) / span
    start = float(objective.get("observed_since") or objective["created_date"])
    left = float(objective["deadline"]) - start
    elapsed = 0.0 if left <= 0 else (at - start) / left
    return {"done": done, "elapsed": elapsed, "behind": done < elapsed * (1 - SLACK)}


def decide(objective: dict, measurements: list[dict], open_tasks: list[dict],
           at: float | None = None, kinds: tuple[str, ...] | set[str] = tuple(KIND_EVIDENCE)) -> dict:
    """whether this objective should be given more work, and the reason either way."""
    at = now() if at is None else at
    if objective["status"] != "active":
        return {"ok": False, "why": f"objective_{objective['status']}"}
    if at > float(objective["deadline"]):
        return {"ok": False, "why": "no_deadline_left"}
    mine = [task for task in open_tasks if task.get("objective_id") == objective["id"]]
    if mine:
        return {"ok": False, "why": "already_queued", "task_id": mine[0]["id"]}
    if len(mine) >= int(objective.get("wip_limit") or WIP_LIMIT):
        return {"ok": False, "why": "wip_limit"}
    wanted = [kind for kind in (objective.get("kinds") or []) if kind in set(kinds)]
    if not wanted:
        return {"ok": False, "why": "kind_unregistered",
                "kinds": list(objective.get("kinds") or [])}
    measured = [m for m in measurements if m.get("objective_id") == objective["id"]]
    if not measured:
        return {"ok": False, "why": "no_measurement"}
    latest = max(measured, key=lambda m: float(m.get("computed_at") or 0))
    shape = pace(objective, float(latest["value"]), at)
    if not shape["behind"]:
        return {"ok": False, "why": "on_track", "pace": shape}
    return {"ok": True, "kind": wanted[0], "kinds": wanted, "pace": shape,
            "deficit": round(float(objective["target"]) - float(latest["value"]), 4)}


class ReplenishService:
    def __init__(self, store: Store, kanban=None, crews=None):
        self.store = store
        self.kanban = kanban
        self.crews = crews

    def _shape(self, row) -> dict:
        return {"id": row["id"], "team_id": row["team_id"], "name": row["name"],
                "baseline": row["baseline"], "target": row["target"],
                "deadline": row["deadline"], "observed_since": row["observed_since"],
                "kinds": loads(row["kinds"], []), "wip_limit": int(row["wip_limit"]),
                "status": row["status"], "created_date": row["created_date"]}

    def create(self, team_id: str, name: str, baseline: float, target: float, deadline: float,
               kinds: list[str], observed_since: float | None = None) -> dict:
        if self.crews is not None:
            self.crews.get_team(team_id)
        if target == baseline:
            raise ValidationError("an objective whose target is its baseline cannot move")
        if deadline <= now():
            raise ValidationError("an objective whose deadline has passed is a decision, not a target")
        ts = now()
        oid = new_id("obj")
        with self.store.tx() as c:
            c.execute(
                """insert into objectives (id, team_id, name, baseline, target, deadline,
                   observed_since, kinds, wip_limit, status, created_date, updated_date)
                   values (?,?,?,?,?,?,?,?,?,'active',?,?)""",
                (oid, team_id, name, float(baseline), float(target), float(deadline),
                 float(observed_since) if observed_since else None, dumps(kinds or []),
                 WIP_LIMIT, ts, ts))
        return self.get(oid)

    def get(self, objective_id: str) -> dict:
        row = self.store.one("select * from objectives where id = ?", (objective_id,))
        if not row:
            raise NotFound(f"no objective {objective_id}")
        return self._shape(row)

    def list(self, team_id: str) -> list[dict]:
        return [self._shape(r) for r in self.store.query(
            "select * from objectives where team_id = ? order by created_date", (team_id,))]

    def measure(self, objective_id: str, value: float, at: float | None = None) -> dict:
        """a measurement is an observation from outside. the first one starts the period."""
        at = now() if at is None else at
        objective = self.get(objective_id)
        with self.store.tx() as c:
            c.execute(
                """insert into measurements (id, team_id, objective_id, value, computed_at)
                   values (?,?,?,?,?)""",
                (new_id("meas"), objective["team_id"], objective_id, float(value), at))
            if not objective["observed_since"]:
                c.execute("update objectives set observed_since = ?, updated_date = ? where id = ?",
                          (at, at, objective_id))
        return {"objective": self.get(objective_id), "pace": pace(self.get(objective_id), value, at)}

    def _measurements(self, team_id: str) -> list[dict]:
        return [dict(r) for r in self.store.query(
            "select * from measurements where team_id = ?", (team_id,))]

    def _open(self, team_id: str) -> list[dict]:
        rows = self.store.query(
            """select id, payload from tasks where team_id = ?
               and column_name not in ('done','archived')""", (team_id,))
        out = []
        for row in rows:
            payload = loads(row["payload"], {}) or {}
            out.append({"id": row["id"], "objective_id": payload.get("objective_id")})
        return out

    def replenish(self, team_id: str, at: float | None = None) -> dict:
        """one pass: each behind objective gets at most one card, and the rest is refusal."""
        at = now() if at is None else at
        measurements = self._measurements(team_id)
        open_tasks = self._open(team_id)
        created, refused = [], []
        for objective in self.list(team_id):
            verdict = decide(objective, measurements, open_tasks + created, at)
            if not verdict["ok"]:
                refused.append({"objective_id": objective["id"], "why": verdict["why"]})
                continue
            if self.kanban is None:
                raise ValidationError("replenishing needs the board")
            task = self.kanban.create_task(
                team_id=team_id,
                title=f"{objective['name']}: close the gap to target",
                description=f"{verdict['deficit']} short of {objective['target']} with "
                            f"{round((objective['deadline'] - at) / 86400, 1)} days left",
                column="ready", priority=2,
                payload={"objective_id": objective["id"], "kind": verdict["kind"]},
            )
            created.append(task)
        return {"created": created, "refused": refused}
