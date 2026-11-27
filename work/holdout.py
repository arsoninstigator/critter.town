"""the acceptance criteria a crew is measured on, kept where the crew cannot read them.

the failure this exists for is quiet and it looks like success. a generating agent that can
see its own acceptance criteria does not fail them; it optimises for them, and the metric
goes up while the work does not. michaellaws/dark-factory names the fix: the generator works
in a checkout where the holdout directory does not exist, so it cannot read the test it will
be measured on, and a separate process runs that test and merges on the number.

there is no filesystem here to leave a directory out of, so the wall is three rules instead:

* the scenarios live in their own tables and no slice builder reads them. that is structural,
  and `verify` proves it rather than trusting it: it composes every agent's context and every
  body of reachable text and looks for the criteria in them.
* a read is a row. the eval runner reads as `eval`; anything that presents itself as an agent
  is refused and the refusal is logged, because an attempt is the interesting half of a wall.
* a run is clean only if nobody touched the scenarios between the seal and the score. a
  tainted run still records its number and says so, since a metric with a footnote is worth
  more than a metric with a lie attached.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from core.db import Store, dumps, loads, new_id, now
from core.errors import NotAllowed, NotFound, ValidationError
from work import evals

# who may read the criteria. everything else is refused by name, not by omission.
READERS = ("eval", "owner", "system")


def _norm(text: str) -> str:
    return " ".join(str(text or "").lower().split())


def _row_digest(row: dict) -> str:
    """one scenario's fingerprint, so a tampered set can name the row that moved."""
    canonical = json.dumps(
        {"name": row["name"], "input": row["input"], "expect": row["expect"],
         "scorer": loads(row["scorer"]) if isinstance(row["scorer"], str) else row["scorer"]},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _digest(rows: list[dict]) -> str:
    """the fingerprint of a sealed set: what was sealed, not when or by whom.

    taken over name, input, expect and scorer in a fixed order, so two crews that sealed
    the same criteria get the same digest and one crew that edited a row gets a different
    one. the timestamp is left out on purpose: re-sealing identical criteria is the same
    commitment, and including the clock would make it look like a new one.
    """
    canonical = json.dumps(
        [{"name": r["name"], "input": r["input"], "expect": r["expect"],
          "scorer": loads(r["scorer"]) if isinstance(r["scorer"], str) else r["scorer"]}
         for r in sorted(rows, key=lambda r: r["name"])],
        sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class HoldoutService:
    def __init__(self, store: Store, crews=None, kanban=None, knowledge=None,
                 lessons=None, intent=None, skills=None, context=None):
        self.store = store
        self.crews = crews
        self.kanban = kanban
        self.knowledge = knowledge
        self.lessons = lessons
        self.intent = intent
        self.skills = skills
        self.context = context

    # ---- reachable text: everything a crew can already see ------------------
    def reachable(self, team_id: str) -> list[dict]:
        """every text a crew member can be handed, with where it came from.

        this is the other side of the wall: the point is not that the criteria are hidden in
        the database, it is that they are not already in the prompt.
        """
        out: list[dict] = []
        if self.knowledge is not None:
            try:
                for row in self.knowledge.search(team_id, limit=500):
                    out.append({"source": "knowledge", "ref": row.get("id", ""),
                                "text": f"{row.get('title', '')} {row.get('body', '')}"})
            except Exception:
                pass
        if self.skills is not None:
            for row in self.store.query("select id, name, description, body from skills where team_id = ?",
                                        (team_id,)):
                out.append({"source": "skill", "ref": row["name"],
                            "text": f"{row['name']} {row['description']} {row['body']}"})
        if self.lessons is not None:
            try:
                for row in self.store.query(
                        "select id, statement from lessons where team_id = ?", (team_id,)):
                    out.append({"source": "lesson", "ref": row["id"], "text": row["statement"]})
            except Exception:
                pass
        if self.intent is not None:
            try:
                # the intent block is what the prompt actually carries, so it is the honest
                # corpus for this question rather than the rows behind it
                out.append({"source": "intent", "ref": "block",
                            "text": self.intent.prompt(team_id)})
            except Exception:
                pass
        for row in self.store.query(
                "select id, name, system_prompt from agents where team_id = ?", (team_id,)):
            out.append({"source": "agent_prompt", "ref": row["name"],
                        "text": row["system_prompt"] or ""})
        return out

    def find(self, team_id: str, needles: list[str]) -> list[dict]:
        """where the criteria already are, if they are anywhere. empty is the answer we want."""
        found: list[dict] = []
        corpus = self.reachable(team_id)
        for needle in needles:
            want = _norm(needle)
            if len(want) < 12:
                # a short criterion matches half the corpus by accident; the wall is about
                # text that could actually teach the answer, so short ones are checked by
                # the run and not by the search
                continue
            for item in corpus:
                if want in _norm(item["text"]):
                    found.append({"needle": needle[:80], "source": item["source"],
                                  "ref": item["ref"]})
        return found

    # ---- the seal ----------------------------------------------------------
    def seal(self, team_id: str, scenarios: list[dict], actor: str = "owner") -> dict:
        """put the criteria out of reach, and refuse if they are already in it."""
        if self.crews is not None:
            self.crews.get_team(team_id)
        if not scenarios:
            raise ValidationError("a holdout with no scenarios measures nothing",
                                  reason="no_scenarios")
        if self.state(team_id)["scenarios"]:
            raise ValidationError(
                "this crew already has a sealed holdout; sealing again would let the "
                "criteria be swapped after the work was done",
                reason="already_sealed")
        clean: list[dict] = []
        for index, scenario in enumerate(scenarios):
            if not isinstance(scenario, dict):
                raise ValidationError(f"scenario {index} is not an object", reason="bad_scenario")
            text = str(scenario.get("input", "")).strip()
            expect = str(scenario.get("expect", "")).strip()
            if not text or not expect:
                raise ValidationError(
                    f"scenario {index} needs an input and an expect", reason="bad_scenario")
            # the scorer carries its own target, so the scenario's expect is the default
            scorer = dict(scenario.get("scorer") or {"kind": "contains"})
            scorer.setdefault("expect", expect)
            # a scorer that cannot decide is caught here, not on the night of the run
            evals.score(scorer, expect)
            clean.append({"name": str(scenario.get("name") or f"case-{index + 1}"),
                          "input": text, "expect": expect, "scorer": scorer})
        leaks = self.find(team_id, [s["input"] for s in clean] + [s["expect"] for s in clean])
        if leaks:
            raise ValidationError(
                "the crew can already read its own acceptance criteria: "
                + ", ".join(f"{l['source']}:{l['ref']}" for l in leaks[:3]),
                reason="criteria_already_in_context", leaks=leaks[:5])
        ts = now()
        with self.store.tx() as c:
            for scenario in clean:
                c.execute(
                    """insert into holdout_scenarios (id, team_id, name, input, expect,
                           scorer, sealed_date, created_date, digest)
                       values (?,?,?,?,?,?,?,?,?)""",
                    (new_id("hld"), team_id, scenario["name"], scenario["input"],
                     scenario["expect"], dumps(scenario["scorer"]), ts, ts,
                     _row_digest(scenario)))
            c.execute(
                """insert or replace into holdout_seals
                       (team_id, digest, scenarios, actor, sealed_date)
                   values (?,?,?,?,?)""",
                (team_id, _digest(clean), len(clean), actor, ts))
        self._log(team_id, "", actor, "seal")
        return self.state(team_id)

    # ---- reading, which is the thing being rationed -------------------------
    def _log(self, team_id: str, scenario_id: str, actor: str, reason: str,
             refused: bool = False) -> None:
        with self.store.tx() as c:
            c.execute(
                """insert into holdout_access (id, team_id, scenario_id, actor, reason,
                       refused, created_date) values (?,?,?,?,?,?,?)""",
                (new_id("hla"), team_id, scenario_id, actor, reason, 1 if refused else 0, now()))

    def _is_agent(self, actor: str) -> bool:
        return bool(self.store.one("select 1 from agents where id = ?", (actor,)))

    def scenarios(self, team_id: str, actor: str = "owner", reason: str = "") -> list[dict]:
        """the criteria, for a reader that is allowed to see them.

        an agent presenting itself here is refused and logged. this is the tool-side half of
        the wall: the tables are unreadable to the things being measured, not merely absent
        from their prompt.
        """
        if self._is_agent(actor) or actor not in READERS:
            self._log(team_id, "", actor, reason or "unrecognised reader", refused=True)
            raise NotAllowed(
                f"'{actor}' may not read the holdout: the crew is measured on these scenarios",
                reason="holdout_is_sealed")
        rows = self.store.query(
            "select * from holdout_scenarios where team_id = ? order by created_date, name",
            (team_id,))
        for row in rows:
            self._log(team_id, row["id"], actor, reason)
        return [{"id": r["id"], "name": r["name"], "input": r["input"],
                 "expect": r["expect"], "scorer": loads(r["scorer"], {})} for r in rows]

    def state(self, team_id: str) -> dict:
        """what a person may see without seeing the criteria: how many, when, and how it went."""
        rows = self.store.query(
            "select id, name, sealed_date from holdout_scenarios where team_id = ? order by name",
            (team_id,))
        runs = self.store.query(
            "select * from holdout_runs where team_id = ? order by created_date desc limit 5",
            (team_id,))
        seal_row = self.store.one(
            "select * from holdout_seals where team_id = ?", (team_id,))
        return {
            "scenarios": len(rows),
            "names": [r["name"] for r in rows],
            "digest": seal_row["digest"] if seal_row else "",
            "tampered": self._tampered(team_id),
            "sealed_date": rows[0]["sealed_date"] if rows else 0,
            "runs": [{"id": r["id"], "metric": r["metric"], "scenarios": r["scenarios"],
                      "passed": r["passed"], "clean": bool(r["clean"]),
                      "created_date": r["created_date"]} for r in runs],
            "last_metric": runs[0]["metric"] if runs else None,
        }

    def access(self, team_id: str, limit: int = 50) -> list[dict]:
        rows = self.store.query(
            """select * from holdout_access where team_id = ?
               order by created_date desc limit ?""", (team_id, limit))
        return [{"id": r["id"], "actor": r["actor"], "reason": r["reason"],
                 "refused": bool(r["refused"]), "scenario_id": r["scenario_id"],
                 "created_date": r["created_date"]} for r in rows]

    def _tampered(self, team_id: str) -> list[str]:
        """which sealed scenarios no longer match the fingerprint taken when they were sealed.

        a row edited between the seal and the gate is the quiet version of the failure this
        module exists for: the crew still cannot read the criteria, and the number it is
        measured on is not the number it was promised. this names the rows that moved,
        because "something changed" is a warning nobody can act on.
        """
        seal_row = self.store.one(
            "select * from holdout_seals where team_id = ?", (team_id,))
        if seal_row is None:
            return []
        rows = self.store.query(
            """select name, input, expect, scorer, digest from holdout_scenarios
               where team_id = ? order by name""", (team_id,))
        moved: list[str] = []
        if _digest(rows) == seal_row["digest"]:
            return moved
        if len(rows) != int(seal_row["scenarios"] or 0):
            moved.append(f"the set holds {len(rows)} scenarios and was sealed with "
                         f"{int(seal_row['scenarios'] or 0)}")
        for row in rows:
            if not row["digest"]:
                moved.append(f"{row['name']}: sealed before fingerprints were kept")
            elif _row_digest(row) != row["digest"]:
                moved.append(f"{row['name']}: edited since it was sealed")
        return moved

    # ---- the check that makes the wall a claim and not a hope ---------------
    def verify(self, team_id: str) -> dict:
        """is any acceptance criterion already inside what the crew can be handed.

        the composition is the point: this asks every agent's assembled context the same
        question, so a slice added later that happens to carry a scenario fails here instead
        of quietly making the next metric meaningless.
        """
        rows = self.store.query(
            "select name, input, expect from holdout_scenarios where team_id = ?", (team_id,))
        tampered = self._tampered(team_id)
        if not rows:
            return {"ok": not tampered, "scenarios": 0, "leaks": [], "tampered": tampered,
                    "checked": 0}
        leaks = self.find(team_id, [r["input"] for r in rows] + [r["expect"] for r in rows])
        checked = 0
        if self.context is not None:
            for agent in self.store.query("select id from agents where team_id = ?", (team_id,)):
                slicer = self.context
                try:
                    snapshot = slicer.compose(agent["id"], store=False)
                except Exception:
                    continue
                checked += 1
                block = _norm(snapshot.get("block", ""))
                for row in rows:
                    for needle in (row["input"], row["expect"]):
                        if len(_norm(needle)) >= 12 and _norm(needle) in block:
                            leaks.append({"needle": needle[:80], "source": "context",
                                          "ref": agent["id"]})
        return {"ok": not leaks and not tampered, "scenarios": len(rows), "leaks": leaks,
                "tampered": tampered, "checked": checked}

    # ---- the run ------------------------------------------------------------
    def run(self, team_id: str, model: Any, actor: str = "owner") -> dict:
        """score the crew on the criteria it cannot read, and say whether the score is clean."""
        if model is None:
            raise ValidationError("no model is configured, so there is nothing to measure",
                                  reason="no_model")
        cases = self.scenarios(team_id, actor="eval", reason="run")
        if not cases:
            raise ValidationError("no sealed holdout to run", reason="no_scenarios")
        state = self.state(team_id)
        sealed = state["sealed_date"]
        tampered = state["tampered"]
        during = self.store.query(
            """select count(*) as n from holdout_access
               where team_id = ? and refused = 1 and created_date >= ?""",
            (team_id, sealed))
        attempts = int(during[0]["n"] or 0) if during else 0
        system_prompt = ("you are an agent on a crew. answer the question directly and "
                         "briefly.")
        passed = 0
        cells = []
        for case in cases:
            action = model.act(system_prompt, case["input"])
            output = evals.output_of(action if isinstance(action, dict) else {})
            points, detail = evals.score(case["scorer"], output)
            passed += 1 if points >= 1.0 else 0
            cells.append({"scenario": case["name"], "score": points, "detail": detail})
        metric = passed / len(cases)
        run_id = new_id("hlr")
        with self.store.tx() as c:
            c.execute(
                """insert into holdout_runs (id, team_id, scenarios, passed, metric, clean,
                       access_during, actor, created_date) values (?,?,?,?,?,?,?,?,?)""",
                (run_id, team_id, len(cases), passed, metric,
                 0 if attempts or tampered else 1, attempts, actor, now()))
        return {"id": run_id, "metric": metric, "passed": passed, "scenarios": len(cases),
                "clean": not attempts and not tampered, "access_during": attempts,
                "tampered": tampered, "cells": cells}
