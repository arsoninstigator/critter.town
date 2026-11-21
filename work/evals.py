"""an eval: the same input against every candidate, scored the same way, recorded.

the problem it solves is that a prompt change has no number. an agent that starts
answering better looks exactly like an agent that started answering differently, and
the only instrument for "better" was a person reading a few outputs and trusting the
feeling. that is not a measurement, it is a mood with a log.

so an eval is a matrix: scenarios down one side, candidates across the other, one
deterministic scorer for the whole grid. every cell is a row, so a rerun can be
compared with the last one instead of remembered.

three boundaries are deliberate:

- an eval measures the decision, not the execution. a cell is one model turn with the
  candidate's prompt, never a sandbox and never a shell. a matrix that runs commands is
  a weapon with a nice report attached.
- the grid is validated before the first cell runs, and nothing is written when it is
  wrong. a half-finished matrix that scored three of eight cells is worse than none.
- the scorer is a name and a payload, not a function, so a row can say how it was
  judged after the code that judged it has moved on.
"""

from __future__ import annotations

import re

from core.db import Store, dumps, loads, new_id, now
from core.errors import NotFound, ValidationError

SCORERS = ("exact", "contains", "regex")
STATUSES = ("draft", "done", "failed")

DEFAULT_SYSTEM_PROMPT = "you are an agent on a crew. answer in one short paragraph."


def score(scorer: dict, output: str) -> tuple[float, str]:
    """the whole scorer vocabulary, and every refusal in it.

    a scorer that cannot decide is an error, not a zero: a zero is a result, and a
    scorer with no `expect` has not produced one.
    """
    kind = str((scorer or {}).get("kind", ""))
    if kind not in SCORERS:
        raise ValidationError(
            f"scorer kind must be one of {list(SCORERS)}", scorer=kind or None
        )
    expect = scorer.get("expect")
    if expect is None or str(expect) == "":
        raise ValidationError("a scorer needs an expect value", kind=kind)
    expect = str(expect)
    text = output or ""

    if kind == "exact":
        ok = text.strip() == expect.strip()
        return (1.0 if ok else 0.0, "exact match" if ok else f"not exactly {expect!r}")
    if kind == "contains":
        ok = expect.lower() in text.lower()
        return (1.0 if ok else 0.0, "contains" if ok else f"missing {expect!r}")
    try:
        ok = re.search(expect, text, re.MULTILINE) is not None
    except re.error as bad:
        raise ValidationError(f"the scorer pattern does not compile: {bad}",
                              pattern=expect) from bad
    return (1.0 if ok else 0.0, "matched" if ok else f"no match for {expect!r}")


def matrix(scenarios: list[dict], candidates: list[dict]) -> list[tuple[dict, dict]]:
    """every pair, in declaration order. the order is the tiebreak, so it is kept.

    the grid is refused before it runs when an id is missing, empty or repeated: a
    report keyed by two rows called `b` cannot be read, and finding that out halfway
    through a paid run is the expensive way.
    """
    for kind, values in (("scenario", scenarios), ("candidate", candidates)):
        if not values:
            raise ValidationError(f"{kind} list must not be empty")
        seen: set[str] = set()
        for value in values:
            if not isinstance(value, dict):
                raise ValidationError(f"each {kind} must be an object")
            name = str(value.get("id", "")).strip()
            if not name:
                raise ValidationError(f"{kind} id must not be empty")
            if name in seen:
                raise ValidationError(f"duplicate {kind} id: {name}", id=name)
            seen.add(name)
    return [(scenario, candidate) for scenario in scenarios for candidate in candidates]


def preflight(scenarios: list[dict], candidates: list[dict], scorer: dict) -> None:
    """fail closed, before anything is spent.

    the scorer is checked with the same call a real cell makes, so a pattern that does
    not compile is a configuration error rather than a grid of zeros.
    """
    matrix(scenarios, candidates)
    for scenario in scenarios:
        if not str(scenario.get("input", "")).strip():
            raise ValidationError("every scenario needs an input", id=scenario["id"])
    for candidate in candidates:
        config = candidate.get("config")
        if config is not None and not isinstance(config, dict):
            raise ValidationError("candidate config must be an object",
                                  id=candidate["id"])
    score(scorer, "")


def output_of(action: dict) -> str:
    """what the model said, in the order a person would read it."""
    message = action.get("message") if isinstance(action, dict) else None
    if isinstance(message, dict) and str(message.get("body", "")).strip():
        return str(message["body"]).strip()
    if isinstance(action, dict) and str(action.get("thought", "")).strip():
        return str(action["thought"]).strip()
    return ""


class EvalService:
    def __init__(self, store: Store, crews=None, model=None):
        self.store = store
        self.crews = crews
        self.model = model

    # ---- rows ----------------------------------------------------------
    def _row(self, row) -> dict:
        return {
            "id": row["id"],
            "team_id": row["team_id"],
            "name": row["name"],
            "scenarios": loads(row["scenarios"], []),
            "candidates": loads(row["candidates"], []),
            "scorer": loads(row["scorer"], {}),
            "status": row["status"],
            "summary": loads(row["summary"], {}),
            "created_date": row["created_date"],
            "updated_date": row["updated_date"],
        }

    def get(self, eval_id: str) -> dict:
        row = self.store.one("select * from eval_runs where id = ?", (eval_id,))
        if not row:
            raise NotFound(f"eval {eval_id} not found")
        return self._row(row)

    def list(self, team_id: str, limit: int = 50) -> list[dict]:
        rows = self.store.query(
            "select * from eval_runs where team_id = ? order by created_date desc limit ?",
            (team_id, int(limit)),
        )
        return [self._row(row) for row in rows]

    def cases(self, eval_id: str) -> list[dict]:
        rows = self.store.query(
            "select * from eval_cases where eval_id = ? order by created_date, rowid",
            (eval_id,),
        )
        return [
            {
                "id": row["id"],
                "scenario_id": row["scenario_id"],
                "candidate_id": row["candidate_id"],
                "output": row["output"],
                "score": row["score"],
                "detail": row["detail"],
            }
            for row in rows
        ]

    # ---- the matrix ----------------------------------------------------
    def create(self, team_id: str, scenarios: list[dict], candidates: list[dict],
               scorer: dict, name: str = "") -> dict:
        if self.crews is not None:
            self.crews.get_team(team_id)
        preflight(scenarios, candidates, scorer)
        eval_id = new_id("eval")
        ts = now()
        with self.store.tx() as c:
            c.execute(
                """insert into eval_runs (id, team_id, name, scenarios, candidates, scorer,
                       status, summary, created_date, updated_date)
                   values (?,?,?,?,?,?, 'draft', '{}', ?, ?)""",
                (eval_id, team_id, name or "", dumps(scenarios), dumps(candidates),
                 dumps(scorer), ts, ts),
            )
        return self.get(eval_id)

    def run(self, eval_id: str) -> dict:
        run = self.get(eval_id)
        # the grid is checked again here: the row may have been written by an older
        # scorer, and a matrix is only worth running if it can still be read
        preflight(run["scenarios"], run["candidates"], run["scorer"])
        model = self.model
        if model is None:
            raise ValidationError("no model is configured, so there is nothing to evaluate")

        with self.store.tx() as c:
            c.execute("delete from eval_cases where eval_id = ?", (eval_id,))

        cells: list[dict] = []
        for scenario, candidate in matrix(run["scenarios"], run["candidates"]):
            system_prompt = str((candidate.get("config") or {}).get(
                "system_prompt") or DEFAULT_SYSTEM_PROMPT)
            action = model.act(system_prompt, str(scenario["input"]))
            output = output_of(action if isinstance(action, dict) else {})
            points, detail = score(run["scorer"], output)
            cells.append({"scenario_id": scenario["id"], "candidate_id": candidate["id"],
                          "output": output, "score": points, "detail": detail})

        summary = self._summarise(run["candidates"], cells)
        ts = now()
        with self.store.tx() as c:
            for cell in cells:
                c.execute(
                    """insert into eval_cases (id, eval_id, scenario_id, candidate_id,
                           output, score, detail, created_date)
                       values (?,?,?,?,?,?,?,?)""",
                    (new_id("evc"), eval_id, cell["scenario_id"], cell["candidate_id"],
                     cell["output"], float(cell["score"]), cell["detail"], ts),
                )
            c.execute(
                "update eval_runs set status = 'done', summary = ?, updated_date = ?"
                " where id = ?",
                (dumps(summary), ts, eval_id),
            )
        return {**self.get(eval_id), "cases": self.cases(eval_id)}

    @staticmethod
    def _summarise(candidates: list[dict], cells: list[dict]) -> dict:
        """mean per candidate, and the winner by that mean.

        a tie goes to the candidate declared first, because the order a person wrote is
        a preference and a report that breaks ties alphabetically invents one.
        """
        rows = []
        for candidate in candidates:
            mine = [cell for cell in cells if cell["candidate_id"] == candidate["id"]]
            scored = len(mine)
            mean = (sum(cell["score"] for cell in mine) / scored) if scored else 0.0
            rows.append({"candidate_id": candidate["id"], "cases": scored,
                         "mean": round(mean, 4),
                         "decided": sum(1 for cell in mine if cell["score"] > 0)})
        best = None
        for row in rows:
            if best is None or row["mean"] > best["mean"]:
                best = row
        return {"candidates": rows,
                "best": best["candidate_id"] if best else None,
                "best_mean": best["mean"] if best else 0.0,
                "cases": len(cells)}
