"""what a correct outcome cost, not how much work was done.

the number a dark factory is judged on is not cards closed or tokens spent. it is money per
correct outcome, measured against a baseline someone else can reproduce. volume hides the
thing worth seeing: a card that took three attempts and two rejections is still one correct
outcome, and counting it as one card makes the retries disappear. counting it at the cost of
three attempts makes them the story.

so the rule here is: spend is every settled reservation attached to the card, correctness is
the card's own closing state, and the division is per correct outcome. a card that was
rejected at review and came back keeps its spend, because that spend bought nothing.

the honest limit, and the reason `partial` exists: a caller that never reserved has a card
with unknown spend, and unknown is not zero. a crew with unknown spend on a correct card gets
a figure marked partial plus the number of cards it cannot see, rather than a cost that reads
lower than the truth.

the baseline is reproducible because it is not a rival's published number. it is the same
measurement, same rules, over the previous window of equal length, and the response carries
the rule set so anyone can re-run it.
"""

from __future__ import annotations

from typing import Any

from core.db import Store, loads, now

DEFAULT_WINDOW_DAYS = 7
# an outcome is not durable the hour it closes. the checkpoint is long enough that a card
# which was going to come back has had its chance to
DEFAULT_DURABILITY_DAYS = 7
SECONDS_PER_DAY = 86400.0

RULES = (
    "spend is the sum of settled budget reservations whose settled_at falls in the window",
    "a correct outcome is a card in done with an evidence reference",
    "a rejection is a card that moved out of review to anything but done, counted once each",
    "cost per correct outcome is spend divided by correct outcomes, not by cards",
    "a durable outcome is one still closed after the durability checkpoint, never reopened",
    "a card with no settled reservation has unknown spend, and unknown is reported, not zero",
    "the baseline is the same measurement over the previous window of equal length",
)


def rate(spend_cents: float, correct: int) -> float | None:
    """money per correct outcome. no correct outcome means no figure, not a zero."""
    if correct <= 0:
        return None
    return round(float(spend_cents) / float(correct), 2)


def summarise(cards: list[dict]) -> dict:
    """the whole division, from rows a reader could have counted themselves.

    a card carries `spend_cents` as a number or None, `correct` as a bool, and `rejected` as
    the number of times it came back from review.
    """
    spend = 0.0
    correct = 0
    durable = 0
    rejected = 0
    unknown = 0
    known_spend_correct = 0
    for card in cards:
        amount = card.get("spend_cents")
        if card.get("correct"):
            correct += 1
            if amount is None:
                unknown += 1
            else:
                known_spend_correct += 1
        if card.get("durable"):
            durable += 1
        if amount is not None:
            spend += float(amount)
        rejected += int(card.get("rejected") or 0)
    return {
        "cards": len(cards),
        "correct": correct,
        "durable": durable,
        "rejected": rejected,
        "spend_cents": round(spend, 2),
        "cost_per_correct": rate(spend, correct),
        "cost_per_durable": rate(spend, durable),
        "cost_per_card": rate(spend, len(cards)),
        "correct_with_known_spend": known_spend_correct,
        "correct_with_unknown_spend": unknown,
        # a figure that leaves spend out is not a cheaper figure, it is an unfinished one
        "partial": unknown > 0,
    }


def compare(now_block: dict, before_block: dict) -> dict:
    """two windows, one measurement. the delta is only meaningful when both are whole."""
    current = now_block.get("cost_per_correct")
    earlier = before_block.get("cost_per_correct")
    delta = None
    if current is not None and earlier is not None:
        delta = round(current - earlier, 2)
    verdict = "unknown"
    if delta is not None and not (now_block.get("partial") or before_block.get("partial")):
        if delta < 0:
            verdict = "cheaper"
        elif delta > 0:
            verdict = "dearer"
        else:
            verdict = "level"
    return {
        "cost_per_correct_now": current,
        "cost_per_correct_before": earlier,
        "delta_cents": delta,
        # a baseline built from a window with unknown spend is not a baseline
        "verdict": verdict,
        "reproducible": True,
        "rules": list(RULES),
    }


class EfficiencyService:
    def __init__(self, store: Store, kanban: Any = None, budget: Any = None):
        self.store = store
        self.kanban = kanban
        self.budget = budget

    # ---- the rows ------------------------------------------------------
    def _cards(self, team_id: str, since: float, until: float,
               durability_days: float = DEFAULT_DURABILITY_DAYS) -> list[dict]:
        """every card that was touched in the window, with what it cost and how it ended."""
        tasks = self.store.query(
            """select id, title, column_name, assignee_agent_id, evidence_ref, attempts,
                      updated_date from tasks
               where team_id = ? and updated_date >= ? and updated_date <= ?""",
            (team_id, since, until))
        spend: dict[str, float] = {}
        for row in self.store.query(
                """select task_id, coalesce(sum(coalesce(settled_cents, amount_cents)), 0) as n
                   from budget_reservations
                   where team_id = ? and state = 'settled' and settled_at >= ?
                     and settled_at <= ? and task_id != '' group by task_id""",
                (team_id, since, until)):
            spend[row["task_id"]] = float(row["n"])
        rejected: dict[str, int] = {}
        reopened: dict[str, int] = {}
        closed_at: dict[str, float] = {}
        for row in self.store.query(
                """select task_id, data, created_date from task_events
                   where kind = 'moved' and created_date >= ? and created_date <= ?
                     and task_id in (select id from tasks where team_id = ?)
                   order by created_date""",
                (since, until, team_id)):
            data = loads(row["data"], {})
            if data.get("from") == "review" and data.get("to") != "done":
                rejected[row["task_id"]] = rejected.get(row["task_id"], 0) + 1
            if data.get("from") == "done":
                # a card that left done came back, and an outcome that came back is not one
                reopened[row["task_id"]] = reopened.get(row["task_id"], 0) + 1
                closed_at.pop(row["task_id"], None)
            if data.get("to") == "done":
                closed_at[row["task_id"]] = float(row["created_date"])
        cards = []
        for row in tasks:
            cards.append({
                "task_id": row["id"],
                "title": row["title"],
                "column": row["column_name"],
                "agent_id": row["assignee_agent_id"] or "",
                "attempts": int(row["attempts"] or 0),
                "spend_cents": spend.get(row["id"]),
                # a card in review is not correct yet, and a blocked card is a failure that
                # has not been written off, so neither is counted as an outcome at all
                "correct": bool(row["column_name"] == "done" and row["evidence_ref"]),
                # durable is the same card at the checkpoint: still closed, and it did not
                # leave done after the close it is being credited for
                "durable": bool(
                    row["column_name"] == "done" and row["evidence_ref"]
                    and row["id"] in closed_at
                    and until - closed_at[row["id"]] >= durability_days * SECONDS_PER_DAY),
                "closed_at": closed_at.get(row["id"]),
                "reopened": reopened.get(row["id"], 0),
                "rejected": rejected.get(row["id"], 0),
            })
        return cards

    # ---- the answers ---------------------------------------------------
    def for_crew(self, team_id: str, since: float | None = None,
                 until: float | None = None,
                 durability_days: float = DEFAULT_DURABILITY_DAYS) -> dict:
        until = now() if until is None else float(until)
        since = (until - DEFAULT_WINDOW_DAYS * SECONDS_PER_DAY) if since is None else float(since)
        cards = self._cards(team_id, since, until, durability_days=durability_days)
        by_agent: dict[str, list[dict]] = {}
        for card in cards:
            by_agent.setdefault(card["agent_id"], []).append(card)
        agents = []
        for agent_id, rows in sorted(by_agent.items()):
            block = summarise(rows)
            block["agent_id"] = agent_id
            agents.append(block)
        agents.sort(key=lambda a: (a["cost_per_correct"] is None, a["cost_per_correct"] or 0))
        return {
            "team_id": team_id,
            "since": since,
            "until": until,
            "window_days": round((until - since) / SECONDS_PER_DAY, 2),
            "durability_days": float(durability_days),
            "total": summarise(cards),
            "agents": agents,
            # the cards a person would actually look at: the ones that cost and did not pay
            "expensive_without_outcome": sorted(
                [c for c in cards if not c["correct"] and c["spend_cents"]],
                key=lambda c: -(c["spend_cents"] or 0))[:10],
            "came_back": sorted(
                [c for c in cards if c["reopened"]],
                key=lambda c: -(c["spend_cents"] or 0))[:10],
            "retried": sorted(
                [c for c in cards if c["rejected"] or c["attempts"] > 1],
                key=lambda c: -(c["spend_cents"] or 0))[:10],
        }

    def compare_windows(self, team_id: str, days: float = DEFAULT_WINDOW_DAYS,
                        durability_days: float = DEFAULT_DURABILITY_DAYS) -> dict:
        until = now()
        span = float(days) * SECONDS_PER_DAY
        current = summarise(self._cards(team_id, until - span, until, durability_days))
        before = summarise(self._cards(team_id, until - 2 * span, until - span,
                                       durability_days))
        return {
            "team_id": team_id,
            "window_days": float(days),
            "now": current,
            "before": before,
            **compare(current, before),
        }

    def for_card(self, task_id: str) -> dict:
        task = self.store.one(
            """select id, team_id, title, column_name, evidence_ref, attempts, blocked_reason
               from tasks where id = ?""", (task_id,))
        if task is None:
            from core.errors import NotFound
            raise NotFound(f"task {task_id} not found")
        spend = 0.0
        known = False
        for row in self.store.query(
                """select coalesce(sum(coalesce(settled_cents, amount_cents)), 0) as n,
                          count(*) as rows from budget_reservations
                   where task_id = ? and state = 'settled'""", (task_id,)):
            known = int(row["rows"] or 0) > 0
            spend = float(row["n"] or 0)
        rejected = 0
        for row in self.store.query(
                "select data from task_events where task_id = ? and kind = 'moved'", (task_id,)):
            data = loads(row["data"], {})
            if data.get("from") == "review" and data.get("to") != "done":
                rejected += 1
        correct = bool(task["column_name"] == "done" and task["evidence_ref"])
        block = summarise([{"spend_cents": spend if known else None, "correct": correct,
                            "rejected": rejected}])
        return {
            "task_id": task_id,
            "team_id": task["team_id"],
            "title": task["title"],
            "column": task["column_name"],
            "attempts": int(task["attempts"] or 0),
            "rejections": rejected,
            "correct": correct,
            "spend_known": known,
            "spend_cents": spend if known else None,
            "cost_per_correct": block["cost_per_correct"],
            "rules": list(RULES),
        }
