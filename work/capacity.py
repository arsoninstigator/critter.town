"""a crew's model capacity is the allowance its providers give it, spread across the keys it connects.

there is no budget by default. critter.town invents no number: a crew connects an api key, says what
that key's provider allows (tokens a day, tokens a minute, requests a minute), and that is the budget.
a lane with no stated allowance is not capped, because a ceiling nobody chose is an obstacle and not
a control. the cents ledger in work/budget.py stays what it was, a reservation for money a caller
knows; this is the other thing, the thing a provider actually enforces.

it is distributed on purpose. a crew holds many lanes (one key, one provider, one model each) and a
run takes the lane with the most room, so one key running dry moves the work instead of stopping the
crew. a lane that answers 429 cools until the provider said it would stop refusing, and a run that
finds nothing usable parks with a reason, which is a different thing from failing.

what is measured is what the provider returned: the usage block of a response and its rate-limit
headers. an estimate is stored as an estimate with its own flag, never folded into the measured
total, because a figure that mixes the two reads as a measurement and is not one.

a key is never held here. a lane names the variable that carries it, the same way the credential
broker does, and the value is resolved at the moment of the call.
"""

from __future__ import annotations

import time
from typing import Any

from core.db import Store, dumps, loads, new_id, now
from core.errors import Conflict, NotFound, ValidationError

# the windows a provider enforces. seconds, because that is what a reset header is quoted in.
WINDOWS = {"minute": 60, "hour": 3600, "day": 86400}

# what an allowance can be stated in. each is a count per window and each is optional.
LIMIT_KINDS = ("tokens", "requests")

# how long a lane stays cooling when the provider refused and did not say for how long. a measured
# retry-after always wins over this.
FALLBACK_COOLDOWN = 60.0

STATES = ("active", "disabled")


def window_start(window: str, at: float | None = None) -> float:
    """the start of the window a moment falls in. a day is a utc day, the same as the cents ledger,
    so two ledgers in one crew never disagree about which day a call belongs to."""
    if window not in WINDOWS:
        raise ValidationError(f"window must be one of {sorted(WINDOWS)}", window=window)
    span = WINDOWS[window]
    moment = now() if at is None else at
    return moment - (moment % span)


def reset_in(headers: dict | None, at: float) -> float | None:
    """seconds until the provider says it will answer again, from the headers it sent.

    providers disagree on the unit and the name, so each form is read as it is documented:
    `retry-after` is seconds or an http date, and the openai style `x-ratelimit-reset-*` is a
    duration like `1s`, `6m0s` or `200ms`. none of them is guessed at when absent."""
    if not headers:
        return None
    lowered = {str(k).lower(): str(v) for k, v in headers.items()}
    raw = lowered.get("retry-after")
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    for key in ("x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
        parsed = parse_duration(lowered.get(key, ""))
        if parsed is not None:
            return parsed
    return None


def parse_duration(text: str) -> float | None:
    """`6m0s`, `1.5s`, `200ms`, `2h`: a provider's way of saying how long to wait."""
    text = str(text or "").strip().lower()
    if not text:
        return None
    total = 0.0
    number = ""
    index = 0
    seen = False
    while index < len(text):
        char = text[index]
        if char.isdigit() or char == ".":
            number += char
            index += 1
            continue
        if not number:
            return None
        if text.startswith("ms", index):
            unit, step = 0.001, 2
        elif char == "h":
            unit, step = 3600.0, 1
        elif char == "m":
            unit, step = 60.0, 1
        elif char == "s":
            unit, step = 1.0, 1
        else:
            return None
        try:
            total += float(number) * unit
        except ValueError:
            return None
        number = ""
        seen = True
        index += step
    if number:
        try:
            total += float(number)
        except ValueError:
            return None
        seen = True
    return total if seen else None


class Capacity:
    def __init__(self, store: Store, audit: Any = None, environ: dict | None = None):
        self.store = store
        self.audit = audit
        self.environ = environ

    # ---- lanes ---------------------------------------------------------
    def _lane(self, lane_id: str) -> dict:
        row = self.store.one("select * from model_lanes where id = ?", (lane_id,))
        if not row:
            raise NotFound(f"lane {lane_id} not found")
        return self._shape(row)

    def _shape(self, row: Any) -> dict:
        return {
            "id": row["id"], "team_id": row["team_id"], "label": row["label"],
            "provider": row["provider"], "base_url": row["base_url"], "model": row["model"],
            "token_env": row["token_env"], "state": row["state"],
            "limits": loads(row["limits"], {}), "cooling_until": row["cooling_until"],
            "weight": row["weight"], "created_date": row["created_date"],
        }

    def connect(self, team_id: str, label: str, provider: str, base_url: str, model: str,
                token_env: str, limits: dict | None = None, weight: float = 1.0) -> dict:
        """connect one key. the allowance is optional and so is every number in it: a lane with
        no stated limit is uncapped, and the response says so instead of supplying a default."""
        if not self.store.one("select id from teams where id = ?", (team_id,)):
            raise NotFound(f"crew {team_id} not found")
        label = " ".join(str(label or "").lower().split())
        if not label:
            raise ValidationError("a lane needs a label")
        token_env = str(token_env or "").strip()
        if not token_env or not token_env.isidentifier():
            raise ValidationError(
                "a lane names the environment variable that carries its key, never the key",
                field="token_env")
        base_url = str(base_url or "").strip()
        if not base_url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
            raise ValidationError("a lane's base_url is https, or loopback for a local model",
                                  field="base_url")
        model = str(model or "").strip()
        if not model:
            raise ValidationError("a lane names the model it serves")
        provider = " ".join(str(provider or "").lower().split()) or "unknown"
        cleaned = self.clean_limits(limits)
        try:
            weight = float(weight)
        except (TypeError, ValueError):
            raise ValidationError("weight must be a number", weight=weight)
        if weight <= 0:
            raise ValidationError("weight must be above zero, or disable the lane instead")
        if self.store.one("select id from model_lanes where team_id = ? and label = ?",
                          (team_id, label)):
            raise Conflict(f"lane {label} already exists in {team_id}", label=label)
        lane_id = new_id("lane")
        with self.store.tx() as c:
            c.execute(
                """insert into model_lanes (id, team_id, label, provider, base_url, model,
                       token_env, state, limits, cooling_until, weight, created_date)
                   values (?,?,?,?,?,?,?, 'active', ?, 0, ?, ?)""",
                (lane_id, team_id, label, provider, base_url, model, token_env,
                 dumps(cleaned), weight, now()))
        lane = self._lane(lane_id)
        lane["capped"] = bool(cleaned)
        return lane

    def clean_limits(self, limits: dict | None) -> dict:
        """`{"tokens": {"day": 2000000, "minute": 90000}, "requests": {"minute": 500}}`. a zero or
        negative number is refused rather than read as 'unlimited', which is the opposite of what
        it looks like. leaving the key out is how a lane says it has no cap."""
        out: dict = {}
        for kind, windows in (limits or {}).items():
            if kind not in LIMIT_KINDS:
                raise ValidationError(f"a limit is one of {list(LIMIT_KINDS)}", kind=kind)
            if not isinstance(windows, dict):
                raise ValidationError("a limit is stated per window", kind=kind)
            for window, amount in windows.items():
                if window not in WINDOWS:
                    raise ValidationError(f"window must be one of {sorted(WINDOWS)}",
                                          window=window)
                try:
                    number = int(amount)
                except (TypeError, ValueError):
                    raise ValidationError("a limit is a whole number", kind=kind, window=window)
                if number <= 0:
                    raise ValidationError(
                        "a limit above zero, or leave it out for no cap: zero reads as "
                        "unlimited and means the reverse", kind=kind, window=window)
                out.setdefault(kind, {})[window] = number
        return out

    def set_limits(self, lane_id: str, limits: dict | None) -> dict:
        self._lane(lane_id)
        with self.store.tx() as c:
            c.execute("update model_lanes set limits = ? where id = ?",
                      (dumps(self.clean_limits(limits)), lane_id))
        return self._lane(lane_id)

    def set_state(self, lane_id: str, state: str) -> dict:
        if state not in STATES:
            raise ValidationError(f"state must be one of {list(STATES)}", state=state)
        self._lane(lane_id)
        with self.store.tx() as c:
            c.execute("update model_lanes set state = ? where id = ?", (state, lane_id))
        return self._lane(lane_id)

    def lanes(self, team_id: str) -> list[dict]:
        return [self._shape(row) for row in self.store.query(
            "select * from model_lanes where team_id = ? order by label", (team_id,))]

    def key_present(self, lane: dict) -> bool:
        """whether the variable the lane names is set, without reading what is in it."""
        import os
        env = self.environ if self.environ is not None else os.environ
        return bool(env.get(lane["token_env"]))

    # ---- measuring -----------------------------------------------------
    def record(self, lane_id: str, run_id: str | None, usage: dict | None,
               headers: dict | None = None, status: int = 200, estimated: bool = False,
               at: float | None = None) -> dict:
        """one call, as the provider reported it. `usage` is the provider's own block (`prompt_tokens`
        and `completion_tokens`, or `input_tokens` and `output_tokens`). a call whose provider sent no
        usage block is recorded as unmeasured with zero tokens, and never given a guessed count."""
        lane = self._lane(lane_id)
        moment = now() if at is None else at
        usage = usage or {}
        tokens_in = _first_int(usage, "prompt_tokens", "input_tokens")
        tokens_out = _first_int(usage, "completion_tokens", "output_tokens")
        measured = (tokens_in is not None or tokens_out is not None) and not estimated
        call_id = new_id("mcl")
        with self.store.tx() as c:
            c.execute(
                """insert into model_calls (id, team_id, lane_id, run_id, tokens_in, tokens_out,
                       measured, estimated, status, created_date)
                   values (?,?,?,?,?,?,?,?,?,?)""",
                (call_id, lane["team_id"], lane_id, run_id, tokens_in or 0, tokens_out or 0,
                 1 if measured else 0, 1 if estimated else 0, int(status), moment))
        cooled = None
        if int(status) == 429:
            wait = reset_in(headers, moment)
            wait = FALLBACK_COOLDOWN if wait is None else wait
            cooled = moment + wait
            with self.store.tx() as c:
                c.execute("update model_lanes set cooling_until = ? where id = ?",
                          (max(cooled, lane["cooling_until"] or 0), lane_id))
        return {"id": call_id, "lane_id": lane_id, "tokens": (tokens_in or 0) + (tokens_out or 0),
                "measured": measured, "estimated": estimated, "cooling_until": cooled}

    def used(self, lane_id: str, window: str, at: float | None = None) -> dict:
        """what this lane has consumed in the window the moment falls in. measured and estimated
        are returned apart, so a reader can see how much of the figure is the provider's word."""
        moment = now() if at is None else at
        since = window_start(window, moment)
        row = self.store.one(
            """select coalesce(sum(case when estimated = 0 then tokens_in + tokens_out end), 0) as m,
                      coalesce(sum(case when estimated = 1 then tokens_in + tokens_out end), 0) as e,
                      count(*) as n,
                      coalesce(sum(case when status = 429 then 1 else 0 end), 0) as refused
               from model_calls where lane_id = ? and created_date >= ?""", (lane_id, since))
        return {"window": window, "since": since, "measured_tokens": int(row["m"]),
                "estimated_tokens": int(row["e"]), "requests": int(row["n"]),
                "refused": int(row["refused"])}

    # ---- room ----------------------------------------------------------
    def room(self, lane: dict, at: float | None = None) -> dict:
        """how much this lane can still take, the most constrained window deciding.

        a lane with no limits returns room of None, which is not zero and not a large number: it
        means nothing was stated, so nothing is claimed. the figure that decides is the smallest
        remaining fraction across every stated window, and the window that decided is named."""
        moment = now() if at is None else at
        if lane["state"] != "active":
            return {"usable": False, "why": "disabled", "remaining": None}
        if (lane["cooling_until"] or 0) > moment:
            return {"usable": False, "why": "cooling", "remaining": None,
                    "until": lane["cooling_until"]}
        if not self.key_present(lane):
            return {"usable": False, "why": "key_not_set", "remaining": None,
                    "env": lane["token_env"]}
        tightest: dict | None = None
        for kind, windows in (lane["limits"] or {}).items():
            for window, cap in windows.items():
                used = self.used(lane["id"], window, moment)
                spent = used["measured_tokens"] + used["estimated_tokens"] if kind == "tokens" \
                    else used["requests"]
                left = cap - spent
                fraction = left / cap
                entry = {"kind": kind, "window": window, "cap": cap, "used": spent,
                         "left": left, "fraction": fraction}
                if tightest is None or fraction < tightest["fraction"]:
                    tightest = entry
        if tightest is None:
            return {"usable": True, "why": "uncapped", "remaining": None, "fraction": None}
        if tightest["left"] <= 0:
            return {"usable": False, "why": "allowance_spent", "remaining": 0,
                    "fraction": tightest["fraction"], "binding": tightest}
        return {"usable": True, "why": "capped", "remaining": tightest["left"],
                "fraction": tightest["fraction"], "binding": tightest}

    def pick(self, team_id: str, model: str | None = None, need_tokens: int = 0,
             at: float | None = None) -> dict:
        """the lane a run should use, or the reason none can be.

        the rule is the most room first, with a lane weight breaking a tie, and an uncapped lane
        ranking by weight alone since it has no fraction to compare. a lane that cannot hold the
        call (need_tokens above what its tightest window has left) is skipped and named. nothing is
        raised: an empty answer carries every lane's reason, so a run can park with the truth."""
        moment = now() if at is None else at
        candidates, skipped = [], []
        for lane in self.lanes(team_id):
            if model and lane["model"] != model:
                skipped.append({"lane_id": lane["id"], "label": lane["label"],
                                "why": "other_model"})
                continue
            room = self.room(lane, moment)
            if not room["usable"]:
                skipped.append({"lane_id": lane["id"], "label": lane["label"], "why": room["why"]})
                continue
            remaining = room.get("remaining")
            if need_tokens and remaining is not None and remaining < need_tokens:
                skipped.append({"lane_id": lane["id"], "label": lane["label"],
                                "why": "too_little_left", "remaining": remaining})
                continue
            fraction = room.get("fraction")
            score = (1.0 if fraction is None else fraction) * lane["weight"]
            candidates.append((score, lane["weight"], lane, room))
        if not candidates:
            return {"lane": None, "skipped": skipped,
                    "reason": ("no lane is connected" if not skipped else
                               "every lane is cooling, spent, disabled or has no key set")}
        candidates.sort(key=lambda item: (-item[0], -item[1], item[2]["label"]))
        score, _, lane, room = candidates[0]
        return {"lane": lane, "room": room, "skipped": skipped, "reason": ""}

    # ---- reading -------------------------------------------------------
    def reading(self, team_id: str, at: float | None = None) -> dict:
        moment = now() if at is None else at
        out = []
        for lane in self.lanes(team_id):
            windows = {w: self.used(lane["id"], w, moment) for w in WINDOWS}
            out.append({"lane": lane, "capped": bool(lane["limits"]),
                        "room": self.room(lane, moment), "used": windows})
        return {"team_id": team_id, "lanes": out,
                "capped_lanes": sum(1 for item in out if item["capped"]),
                "note": ("a lane with no stated allowance is not capped, and no number is "
                         "supplied for it")}

    def share(self, team_id: str, window: str = "day", at: float | None = None) -> list[dict]:
        """where this crew's tokens went across its lanes, so a lopsided spread is visible."""
        moment = now() if at is None else at
        rows = [(lane, self.used(lane["id"], window, moment)) for lane in self.lanes(team_id)]
        total = sum(u["measured_tokens"] + u["estimated_tokens"] for _, u in rows)
        return [{"lane_id": lane["id"], "label": lane["label"],
                 "tokens": u["measured_tokens"] + u["estimated_tokens"],
                 "share": round((u["measured_tokens"] + u["estimated_tokens"]) / total, 4)
                 if total else 0.0} for lane, u in rows]


def _first_int(usage: dict, *names: str) -> int | None:
    for name in names:
        value = usage.get(name)
        if value is None:
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number >= 0:
            return number
    return None
