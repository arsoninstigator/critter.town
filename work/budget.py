"""budget as a reservation, not a hope.

the failure this exists for: a crew that checks the day's spend at dispatch and then runs
four parallel cards, each of which sees the same "under budget" number, and the day ends at
four times the limit. a check that reads the ledger without writing to it is a check that
parallel work can pass at the same time.

so the check and the claim are the same act. a reservation is a row in state `held`, taken
before the work starts; it is settled when the work reports what it actually cost, and
released when the work does not happen. the day's spend is `settled + held`, because money
that is committed is not money that is available.

the honest limit: crittertown does not meter a provider's bill. a caller that knows the
cost of a run reserves it, and the settlement records the difference as `over_spend`
instead of pretending the estimate was the bill.
"""

from __future__ import annotations

from typing import Any

from core.db import Store, dumps, new_id, now
from core.errors import NotFound, ValidationError

STATES = ("held", "settled", "released")


def day_start(ts: float | None = None) -> float:
    ts = now() if ts is None else ts
    return ts - (ts % 86400)


def check_amount(amount_cents: float) -> None:
    """a reservation of nothing reserves nothing, and a negative one is a refund."""
    try:
        amount = float(amount_cents)
    except (TypeError, ValueError):
        raise ValidationError("a_reservation_needs_a_number", amount=amount_cents)
    if amount <= 0:
        raise ValidationError("a_reservation_needs_a_positive_amount", amount=amount)


class BudgetService:
    def __init__(self, store: Store, guardrails=None):
        self.store = store
        self.guardrails = guardrails

    # ---- reading the ledger --------------------------------------------
    def spent_today(self, team_id: str, at: float | None = None) -> float:
        row = self.store.one(
            "select coalesce(sum(coalesce(settled_cents, amount_cents)), 0) as n"
            " from budget_reservations where team_id = ? and state = 'settled'"
            " and settled_at >= ?", (team_id, day_start(at)))
        return float(row["n"] if row else 0.0)

    def held(self, team_id: str) -> float:
        row = self.store.one(
            "select coalesce(sum(amount_cents), 0) as n from budget_reservations"
            " where team_id = ? and state = 'held'", (team_id,))
        return float(row["n"] if row else 0.0)

    def committed_today(self, team_id: str, at: float | None = None) -> float:
        """what the day has spent plus what it has already promised."""
        return self.spent_today(team_id, at) + self.held(team_id)

    def limit(self, team_id: str) -> float | None:
        if self.guardrails is None:
            return None
        for row in self.guardrails.list(team_id):
            if row["kind"] == "spend_per_day" and row.get("status", "active") == "active":
                return float(row["value"])
        return None

    def available(self, team_id: str, at: float | None = None) -> float | None:
        limit = self.limit(team_id)
        return None if limit is None else limit - self.committed_today(team_id, at)

    # ---- the reservation ------------------------------------------------
    def hold(self, team_id: str, amount_cents: float, task_id: str = "", run_id: str = "",
             agent_id: str = "", reason: str = "", currency: str = "usd") -> dict:
        """check and claim in one act, so two parallel cards cannot both pass the check."""
        check_amount(amount_cents)
        amount = float(amount_cents)
        with self.store.tx() as c:
            # the check reads inside the same transaction as the insert. a single-writer
            # store makes this exact: no window where a second holder sees the old number
            limit = self.limit(team_id)
            if limit is not None:
                committed = self.committed_today(team_id)
                if committed + amount > limit:
                    raise ValidationError(
                        f"spend_per_day: {committed:.0f} committed today and this reserves"
                        f" {amount:.0f}, the limit is {limit:.0f}",
                        limit=limit, committed=committed, wanted=amount)
            reservation_id = new_id("bud")
            ts = now()
            c.execute(
                """insert into budget_reservations
                   (id, team_id, task_id, run_id, agent_id, amount_cents, currency, state,
                    reason, settled_cents, over_spend, created_date, settled_at, updated_date)
                   values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (reservation_id, team_id, task_id, run_id, agent_id, amount, currency,
                 "held", reason[:300], None, 0, ts, None, ts),
            )
        return self.get(reservation_id)

    def settle(self, reservation_id: str, spent_cents: float | None = None) -> dict:
        """record what it actually cost. an estimate that was wrong is recorded, not
        rewritten: over_spend is the number a person needs to see."""
        current = self.get(reservation_id)
        if current["state"] != "held":
            raise ValidationError(f"cannot settle a reservation in state {current['state']}",
                                  reservation=reservation_id)
        spent = float(current["amount_cents"] if spent_cents is None else spent_cents)
        if spent < 0:
            raise ValidationError("a settlement cannot be negative", spent=spent)
        with self.store.tx() as c:
            c.execute(
                "update budget_reservations set state = 'settled', settled_cents = ?,"
                " over_spend = ?, settled_at = ?, updated_date = ? where id = ? and state = 'held'",
                (spent, 1 if spent > float(current["amount_cents"]) else 0, now(), now(),
                 reservation_id),
            )
        return self.get(reservation_id)

    def release(self, reservation_id: str, reason: str = "") -> dict:
        current = self.get(reservation_id)
        if current["state"] != "held":
            raise ValidationError(f"cannot release a reservation in state {current['state']}",
                                  reservation=reservation_id)
        with self.store.tx() as c:
            c.execute(
                "update budget_reservations set state = 'released', reason = ?, updated_date = ?"
                " where id = ? and state = 'held'",
                ((reason or current.get("reason", ""))[:300], now(), reservation_id),
            )
        return self.get(reservation_id)

    def get(self, reservation_id: str) -> dict:
        row = self.store.one("select * from budget_reservations where id = ?", (reservation_id,))
        if not row:
            raise NotFound(f"reservation {reservation_id} not found")
        return self._shape(dict(row))

    def reservations(self, team_id: str, state: str | None = None, limit: int = 100) -> list[dict]:
        if state and state not in STATES:
            raise ValidationError(f"state must be one of {list(STATES)}")
        if state:
            rows = self.store.query(
                "select * from budget_reservations where team_id = ? and state = ?"
                " order by created_date desc limit ?", (team_id, state, limit))
        else:
            rows = self.store.query(
                "select * from budget_reservations where team_id = ?"
                " order by created_date desc limit ?", (team_id, limit))
        return [self._shape(dict(row)) for row in rows]

    def summary(self, team_id: str) -> dict:
        limit = self.limit(team_id)
        committed = self.committed_today(team_id)
        return {"limit": limit, "spent_today": self.spent_today(team_id),
                "held": self.held(team_id), "committed_today": committed,
                "available": None if limit is None else limit - committed,
                "reservations": len(self.reservations(team_id))}

    def _shape(self, row: dict) -> dict:
        row["amount_cents"] = float(row.get("amount_cents") or 0)
        row["over_spend"] = bool(row.get("over_spend"))
        if row.get("settled_cents") is not None:
            row["settled_cents"] = float(row["settled_cents"])
        return row
