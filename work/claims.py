"""discourse: claims, not summaries.

a synthesis that reads well and lost the disagreement is the failure this exists for. so a
message carries claims with kinds, and a fact has to point at something a person can open.
a plan stated as near-certain is refused, because that is the overclaim that turns a hope
into a decision.

the conservation rule is the whole design: every claim that went in is in exactly one
bucket that came out. kept, challenged, dropped with a sourced rebuttal, or unresolved with
a reason. a claim that is in no bucket was dropped by accident, which is how a synthesis
becomes a smoothing.
"""

from __future__ import annotations

import re

from core.db import Store, dumps, loads, new_id, now
from core.errors import ValidationError

CLAIM_KINDS = ("fact", "inference", "preference", "plan")

# a scheme, then a payload. `commit:abc`, `artifact://sha256/x`, `run:run_123`
REF = re.compile(r"^[a-z][a-z0-9]*:\s*\S")

LOW_CONFIDENCE = 0.5


def validate_message(message: dict) -> list[dict]:
    faults: list[dict] = []
    claims = list(message.get("claims") or [])
    if not str(message.get("intent", "")).strip():
        faults.append({"rule": "message_needs_an_intent", "detail": message.get("id", "")})
    if not claims and not (message.get("questions") or []):
        faults.append({"rule": "message_says_nothing", "detail": message.get("id", "")})
    seen: set[str] = set()
    for claim in claims:
        cid = str(claim.get("id", ""))
        if cid in seen:
            faults.append({"rule": "duplicate_claim_id", "detail": cid})
        seen.add(cid)
        kind = str(claim.get("kind", ""))
        if kind not in CLAIM_KINDS:
            faults.append({"rule": "unknown_claim_kind", "detail": cid})
        if kind == "fact" and not REF.match(str(claim.get("evidence", ""))):
            # a fact without a reference is an opinion with a straight face
            faults.append({"rule": "fact_without_evidence", "detail": cid})
        confidence = claim.get("confidence")
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            faults.append({"rule": "confidence_out_of_range", "detail": cid})
        elif kind == "plan" and confidence >= 0.9:
            faults.append({"rule": "plan_stated_as_fact", "detail": cid})
    known = {str(c.get("id", "")) for c in claims}
    for claim in claims:
        for other in claim.get("opposes") or []:
            if str(other) not in known:
                faults.append({"rule": "opposes_an_absent_claim", "detail": str(other)})
    return faults


def synthesize(branches: list[dict], rebuttals: dict | None = None) -> dict:
    """one bucket per claim, or a fault saying which claim was not accounted for."""
    rebuttals = rebuttals or {}
    all_claims = [c for branch in branches for c in (branch.get("claims") or [])]
    by_id: dict[str, dict] = {}
    faults: list[dict] = []
    for claim in all_claims:
        cid = str(claim.get("id", ""))
        clash = by_id.get(cid)
        if clash and clash.get("statement") != claim.get("statement"):
            # the id is the unit of accounting, so it has to mean one thing
            faults.append({"rule": "claim_id_collision", "detail": cid})
        by_id[cid] = claim

    opposed: set[str] = set()
    for claim in all_claims:
        for other in claim.get("opposes") or []:
            opposed.add(str(claim.get("id")))
            opposed.add(str(other))

    kept, challenged, dropped, unresolved = [], [], [], []
    for cid, claim in by_id.items():
        if cid in opposed:
            # disagreement is not ours to settle: it stays visible and stays contested
            challenged.append(claim)
            continue
        if (claim.get("confidence") or 0) < LOW_CONFIDENCE and not claim.get("evidence"):
            unresolved.append({"claim": claim, "why": "low_confidence_without_evidence"})
            continue
        rebuttal = rebuttals.get(cid)
        if rebuttal is not None:
            if claim.get("evidence") and not REF.match(str(rebuttal)):
                # you may not discard a sourced claim on vibes
                faults.append({"rule": "rebuttal_needs_evidence", "detail": cid})
                kept.append(claim)
                continue
            dropped.append({"claim": claim, "rebuttal": rebuttal})
            continue
        kept.append(claim)

    buckets = ([str(c.get("id")) for c in kept] + [str(c.get("id")) for c in challenged]
               + [str(d["claim"].get("id")) for d in dropped]
               + [str(u["claim"].get("id")) for u in unresolved])
    for cid in by_id:
        count = buckets.count(cid)
        if count != 1:
            faults.append({"rule": "claim_not_accounted_for", "detail": f"{cid}:{count}"})

    # two seats agreeing is corroboration, not evidence
    agreement = [str(c.get("id")) for c in kept
                 if not c.get("evidence")
                 and len({str(x.get("by")) for x in all_claims
                          if x.get("statement") == c.get("statement")}) > 1]

    return {"kept": kept, "challenged": challenged, "dropped": dropped, "unresolved": unresolved,
            "questions": sorted({q for b in branches for q in (b.get("questions") or [])}),
            "agreement_without_evidence": agreement, "faults": faults}


def is_sound(synthesis: dict) -> bool:
    """a synthesis is usable when it lost nothing and every rebuttal was sourced."""
    total = (len(synthesis["kept"]) + len(synthesis["challenged"])
             + len(synthesis["dropped"]) + len(synthesis["unresolved"]))
    return not synthesis["faults"] and total > 0


class ClaimService:
    def __init__(self, store: Store, crews=None):
        self.store = store
        self.crews = crews

    def _shape(self, row) -> dict:
        return {"id": row["id"], "team_id": row["team_id"], "thread": row["thread"],
                "message_id": row["message_id"], "kind": row["kind"],
                "statement": row["statement"], "evidence": row["evidence"],
                "confidence": row["confidence"], "basis": row["basis"],
                "by": row["by_agent"], "opposes": loads(row["opposes"], []),
                "created_date": row["created_date"]}

    def post(self, team_id: str, thread: str, intent: str, claims: list[dict],
             by: str = "", questions: list[str] | None = None, message_id: str = "") -> dict:
        """write a message's claims, refusing the whole message when any claim is a fault."""
        message = {"id": message_id or new_id("msg"), "intent": intent,
                   "claims": list(claims or []), "questions": list(questions or [])}
        faults = validate_message(message)
        if faults:
            raise ValidationError("this message does not hold: "
                                  + ", ".join(f["rule"] for f in faults), faults=faults)
        ts = now()
        written = []
        with self.store.tx() as c:
            for claim in claims:
                cid = str(claim.get("id") or new_id("claim"))
                c.execute(
                    """insert into claims (id, team_id, thread, message_id, kind, statement,
                       evidence, confidence, basis, by_agent, opposes, created_date)
                       values (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (cid, team_id, thread, message["id"], str(claim.get("kind")),
                     str(claim.get("statement", "")), str(claim.get("evidence", "")),
                     float(claim.get("confidence") or 0), str(claim.get("basis", "")),
                     by or str(claim.get("by", "")), dumps(claim.get("opposes") or []), ts))
                written.append(cid)
        return {"message_id": message["id"], "thread": thread, "claims": written}

    def thread(self, team_id: str, thread: str) -> list[dict]:
        rows = self.store.query(
            "select * from claims where team_id = ? and thread = ? order by created_date",
            (team_id, thread))
        return [self._shape(r) for r in rows]

    def synthesize(self, team_id: str, thread: str, rebuttals: dict | None = None) -> dict:
        claims = self.thread(team_id, thread)
        branch = {"claims": claims, "questions": []}
        return synthesize([branch], rebuttals)
