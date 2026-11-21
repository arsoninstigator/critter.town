"""what may cross a wall, in which direction, how often.

a wall that everything crosses is not a wall. so a crossing is declared: from, to, which
kinds, how many per hour, how many bytes, and when it expires. the default is deny, and
when two doors match the narrower kind set governs, because unioning permissions is how
permission systems rot.

the rate is a token bucket with continuous refill, not a fixed window: a window leaks twice
its limit at the edge, which is exactly when someone is watching.
"""

from __future__ import annotations

import math

from core.db import Store, dumps, loads, new_id, now
from core.errors import NotFound, ValidationError

REVOKE_MODES = ("drain", "cut")
ENVELOPE_KINDS = ("finding", "proposal", "request", "signal", "artifact_ref", "receipt")


def compile_crossings(decls: list[dict], tiers: list[str], now_ts: float) -> dict:
    """declared config in, enforceable crossings or named problems out."""
    problems: list[dict] = []
    crossings: list[dict] = []
    seen: set[str] = set()
    for decl in decls:
        bad: list[str] = []
        cid = str(decl.get("id", ""))
        if cid in seen:
            bad.append("duplicate_id")
        seen.add(cid)
        if str(decl.get("from_scope")) not in tiers:
            bad.append(f"unknown_from:{decl.get('from_scope')}")
        if str(decl.get("to_scope")) not in tiers:
            bad.append(f"unknown_to:{decl.get('to_scope')}")
        if decl.get("from_scope") == decl.get("to_scope"):
            bad.append("self_crossing")
        kinds = [k for k in (decl.get("kinds") or []) if k in ENVELOPE_KINDS]
        if not kinds:
            bad.append("no_kinds")
        if not (decl.get("max_per_hour") or 0) > 0:
            bad.append("no_rate")
        if not decl.get("expires_at"):
            bad.append("no_expiry")
        if len(str(decl.get("reason", "")).strip()) < 8:
            bad.append("no_reason")
        if (decl.get("revoke_mode") or "drain") not in REVOKE_MODES:
            bad.append("unknown_revoke_mode")
        if bad:
            problems.append({"id": cid, "problem": ",".join(bad)})
            continue
        crossings.append({**decl, "kinds": kinds, "revoke_mode": decl.get("revoke_mode") or "drain",
                          "enabled": bool(decl.get("enabled", True))})
    return {"crossings": crossings, "problems": problems}


def resolve(crossings: list[dict], from_scope: str, to_scope: str, kind: str, at: float) -> dict:
    """default deny, and the narrowest matching door wins."""
    pair = [c for c in crossings if c["from_scope"] == from_scope and c["to_scope"] == to_scope]
    if not pair:
        return {"ok": False, "reason": "no_crossing"}
    enabled = [c for c in pair if c.get("enabled")]
    if not enabled:
        return {"ok": False, "reason": "crossing_disabled"}
    by_kind = [c for c in enabled if kind in (c.get("kinds") or [])]
    if not by_kind:
        return {"ok": False, "reason": "kind_not_allowed"}
    live = [c for c in by_kind if float(c.get("expires_at") or 0) > at]
    if not live:
        return {"ok": False, "reason": "crossing_expired"}
    live.sort(key=lambda c: len(c.get("kinds") or []))
    return {"ok": True, "crossing": live[0]}


def burst_for(max_per_hour: int) -> float:
    """a sixth of the hourly allowance, so a bucket is not a fixed window."""
    return max(1.0, math.ceil(max_per_hour / 6))


def refill_per_second(max_per_hour: int) -> float:
    return max_per_hour / 3600.0


def consume(crossing: dict, tokens: float | None, last_refill_at: float | None, at: float,
            cost: float = 1.0) -> dict:
    burst = burst_for(int(crossing["max_per_hour"]))
    refill = refill_per_second(int(crossing["max_per_hour"]))
    base = burst if tokens is None else float(tokens)
    since = at if last_refill_at is None else last_refill_at
    elapsed = max(0.0, at - since)
    available = min(burst, base + elapsed * refill)
    if available >= cost:
        return {"allowed": True, "tokens": available - cost, "last_refill_at": at, "retry_after_ms": 0}
    deficit = cost - available
    return {"allowed": False, "tokens": available, "last_refill_at": at,
            "retry_after_ms": int(math.ceil(deficit / refill * 1000))}


def authorize(crossings: list[dict], from_scope: str, to_scope: str, kind: str, at: float,
              state: dict | None = None, size_bytes: int = 0) -> dict:
    """the single door the relay calls before anything crosses."""
    found = resolve(crossings, from_scope, to_scope, kind, at)
    if not found["ok"]:
        return {"ok": False, "reason": found["reason"]}
    crossing = found["crossing"]
    limit = crossing.get("max_bytes")
    if limit is not None and size_bytes > limit:
        return {"ok": False, "reason": "too_large", "limit": limit, "size": size_bytes}
    spent = consume(crossing, (state or {}).get("tokens"), (state or {}).get("last_refill_at"), at)
    if not spent["allowed"]:
        return {"ok": False, "reason": "rate_limited", "retry_after_ms": spent["retry_after_ms"],
                "state": spent}
    return {"ok": True, "crossing": crossing, "state": spent}


class CrossingService:
    def __init__(self, store: Store, crews=None):
        self.store = store
        self.crews = crews

    def _shape(self, row) -> dict:
        return {"id": row["id"], "team_id": row["team_id"], "from_scope": row["from_scope"],
                "to_scope": row["to_scope"], "kinds": loads(row["kinds"], []),
                "max_per_hour": row["max_per_hour"], "max_bytes": row["max_bytes"],
                "expires_at": row["expires_at"], "authorized_by": row["authorized_by"],
                "reason": row["reason"], "revoke_mode": row["revoke_mode"],
                "enabled": bool(row["enabled"]), "tokens": row["tokens"],
                "last_refill_at": row["last_refill_at"]}

    def declare(self, team_id: str, decls: list[dict], tiers: list[str] | None = None) -> dict:
        """compile, then store what compiled. a crossing that cannot be enforced is refused."""
        ts = now()
        scope_tiers = list(tiers or [d.get("from_scope") for d in decls] + [d.get("to_scope") for d in decls])
        compiled = compile_crossings(decls, scope_tiers, ts)
        if compiled["problems"]:
            raise ValidationError("these crossings cannot be enforced: "
                                  + "; ".join(f"{p['id']}: {p['problem']}" for p in compiled["problems"]),
                                  problems=compiled["problems"])
        with self.store.tx() as c:
            for crossing in compiled["crossings"]:
                c.execute(
                    """insert into crossings (id, team_id, from_scope, to_scope, kinds, max_per_hour,
                       max_bytes, expires_at, authorized_by, reason, revoke_mode, enabled, tokens,
                       last_refill_at, created_date, updated_date)
                       values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (new_id("cross"), team_id, crossing["from_scope"], crossing["to_scope"],
                     dumps(crossing["kinds"]), int(crossing["max_per_hour"]),
                     crossing.get("max_bytes"), float(crossing["expires_at"]),
                     str(crossing.get("authorized_by", "")), str(crossing.get("reason", "")),
                     crossing["revoke_mode"], 1 if crossing["enabled"] else 0,
                     burst_for(int(crossing["max_per_hour"])), ts, ts, ts))
        return {"declared": len(compiled["crossings"]), "crossings": self.list(team_id)}

    def list(self, team_id: str) -> list[dict]:
        rows = self.store.query("select * from crossings where team_id = ? order by created_date", (team_id,))
        return [self._shape(r) for r in rows]

    def authorize(self, team_id: str, from_scope: str, to_scope: str, kind: str,
                  size_bytes: int = 0, at: float | None = None) -> dict:
        at = now() if at is None else at
        crossings = self.list(team_id)
        found = resolve(crossings, from_scope, to_scope, kind, at)
        if not found["ok"]:
            return {"ok": False, "reason": found["reason"]}
        crossing = found["crossing"]
        outcome = authorize(crossings, from_scope, to_scope, kind, at,
                            state={"tokens": crossing["tokens"],
                                   "last_refill_at": crossing["last_refill_at"]},
                            size_bytes=size_bytes)
        if outcome["ok"]:
            with self.store.tx() as c:
                c.execute("update crossings set tokens = ?, last_refill_at = ?, updated_date = ? where id = ?",
                          (outcome["state"]["tokens"], at, at, crossing["id"]))
        return outcome

    def revoke(self, crossing_id: str, mode: str = "drain", by: str = "") -> dict:
        """drain lets what is already in flight land; cut stops it where it stands."""
        if mode not in REVOKE_MODES:
            raise ValidationError(f"revoke mode must be one of {list(REVOKE_MODES)}")
        row = self.store.one("select * from crossings where id = ?", (crossing_id,))
        if not row:
            raise NotFound(f"no crossing {crossing_id}")
        with self.store.tx() as c:
            c.execute("update crossings set enabled = 0, revoke_mode = ?, authorized_by = ?, updated_date = ? where id = ?",
                      (mode, by or row["authorized_by"], now(), crossing_id))
        return self._shape(self.store.one("select * from crossings where id = ?", (crossing_id,)))
