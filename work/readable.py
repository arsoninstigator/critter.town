"""what a run actually reached for, against what it was provisioned with.

the essay that names this problem calls the difference readable space. a governor supplies inputs:
a brief, a set of allowed hosts, a tool list, a sandbox. everything a run does with those is
backtraceable to the supply. everything else arrives through what the same essay calls the
auxiliary supply: the second stream of input nobody provisioned, which is the network, a package
registry, a cached artefact, an environment variable holding a key, or the particular machine the
work happened to land on. selection pressure is indifferent to which stream an advantage arrives
through, so a run can look like it did the work when it actually borrowed the answer.

that matters here for one practical reason: an outcome that leaned on the auxiliary supply is not
reproducible, so it is not the same kind of correct as one that did not. a crew that counts both
at par learns nothing about what its own provisioned inputs can produce.

three refusals are deliberate:

- nothing is executed. signals are read out of the commands a run already recorded, by pattern,
  and a command the patterns do not understand produces no signal rather than a guess.
- a signal is not a violation. reaching an unprovisioned host is not forbidden, it is
  unaccounted for, and the report says which of the two it is.
- the reading never blocks a run. it is written after the fact, because an accounting layer that
  can stop work is a policy layer wearing a different name, and the rails already do that job.
"""

from __future__ import annotations

import re
from typing import Any

from core.db import Store, dumps, loads, new_id, now
from core.errors import NotFound

SECONDS_PER_DAY = 86400
DEFAULT_WINDOW_DAYS = 7

# the channels a command can reach through. each is a pattern and a name, never a parser: a
# command line is a shell sentence, and pretending to parse one is how a checker ends up wrong
# in the direction that looks confident.
URL = re.compile(r"https?://([A-Za-z0-9._-]+)", re.I)
GIT_REMOTE = re.compile(r"git\s+(?:clone|fetch|pull|push|remote\s+add)\s+\S*?([A-Za-z0-9.-]+\.[A-Za-z]{2,})",
                        re.I)
SCP_REMOTE = re.compile(r"\b[\w.-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,}):")
PACKAGES = (
    (re.compile(r"\bpip[23]?\s+install\s+(.+)", re.I), "python"),
    (re.compile(r"\buv\s+(?:pip\s+)?add\s+(.+)", re.I), "python"),
    (re.compile(r"\bnpm\s+(?:i|install|add)\s+(.+)", re.I), "node"),
    (re.compile(r"\bpnpm\s+(?:add|install)\s+(.+)", re.I), "node"),
    (re.compile(r"\byarn\s+add\s+(.+)", re.I), "node"),
    (re.compile(r"\bapt(?:-get)?\s+install\s+(.+)", re.I), "system"),
    (re.compile(r"\bcargo\s+add\s+(.+)", re.I), "rust"),
    (re.compile(r"\bgo\s+get\s+(.+)", re.I), "go"),
)
# a key in the environment is an input nobody provisioned in the brief, even when the crew owns it
ENV_REF = re.compile(r"\$\{?([A-Z][A-Z0-9_]{2,})\}?")
SECRET_ISH = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|PASS)", re.I)
# flags and their arguments are not package names
FLAGS = re.compile(r"^-")

CHANNELS = ("host", "package", "env")


def commands_of(steps: list[dict]) -> list[str]:
    """every command a run recorded, in order, without interpreting any of them."""
    found: list[str] = []
    for turn in steps or []:
        if not isinstance(turn, dict):
            continue
        for step in turn.get("steps") or []:
            if not isinstance(step, dict):
                continue
            if step.get("kind") == "command" and step.get("command"):
                found.append(str(step["command"]))
            elif step.get("command"):
                found.append(str(step["command"]))
        for command in turn.get("commands") or []:
            found.append(str(command))
    return found


def signals(commands: list[str]) -> list[dict]:
    """the channels these commands can reach through, each with the command that named it."""
    found: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, value: str, command: str) -> None:
        key = (kind, value.lower())
        if not value or key in seen:
            return
        seen.add(key)
        found.append({"channel": kind, "value": value.lower(), "command": command[:200]})

    for command in commands:
        line = " ".join(str(command).split())
        for match in URL.finditer(line):
            add("host", match.group(1), line)
        for match in GIT_REMOTE.finditer(line):
            add("host", match.group(1), line)
        for match in SCP_REMOTE.finditer(line):
            add("host", match.group(1), line)
        for pattern, ecosystem in PACKAGES:
            match = pattern.search(line)
            if not match:
                continue
            for token in match.group(1).split():
                name = token.strip("'\"")
                if not name or FLAGS.match(name) or name in ("-r", "--upgrade"):
                    continue
                add("package", f"{ecosystem}:{name}", line)
        for match in ENV_REF.finditer(line):
            name = match.group(1)
            if SECRET_ISH.search(name):
                add("env", name, line)
    return found


def provisioned(task: dict, rails: list[dict]) -> dict:
    """what this card and crew were actually given: hosts, tools, and declared channels.

    a crew with no egress allowlist has provisioned no hosts at all, which is not the same as
    having provisioned every host. the difference is the whole point of this module.
    """
    payload = task.get("payload") or {}
    if isinstance(payload, str):
        payload = loads(payload, {})
    hosts: set[str] = set()
    tools: set[str] = set()
    declared: set[str] = set()
    for rail in rails or []:
        kind = str(rail.get("kind") or "")
        value = rail.get("value")
        if isinstance(value, str):
            value = loads(value, value)
        if kind == "egress_hosts" and isinstance(value, list):
            hosts.update(str(host).lower() for host in value)
        elif kind == "forbidden_tools" and isinstance(value, list):
            # a forbidden tool is a provisioned decision about it, not a supply of it
            tools.update(str(tool).lower() for tool in value)
    for key in ("channels", "declared_inputs", "hosts"):
        raw = payload.get(key) if isinstance(payload, dict) else None
        if isinstance(raw, str):
            raw = [raw]
        for item in raw or []:
            declared.add(str(item).lower())
    return {"hosts": sorted(hosts), "tools": sorted(tools), "declared": sorted(declared)}


def covered(signal: dict, supply: dict) -> bool:
    """whether this channel was provisioned, named by the thing that provisioned it."""
    value = str(signal.get("value") or "").lower()
    channel = signal.get("channel")
    if value and value in set(supply.get("declared") or []):
        return True
    if channel == "host":
        for host in supply.get("hosts") or []:
            host = str(host).lower()
            if value == host or value.endswith("." + host):
                return True
    return False


def classify(found: list[dict], supply: dict) -> dict:
    """the readable part of a run, and the part that arrived from somewhere else."""
    readable, auxiliary = [], []
    for signal in found:
        (readable if covered(signal, supply) else auxiliary).append(signal)
    total = len(found)
    return {
        "readable": readable,
        "auxiliary": auxiliary,
        "signals": total,
        "readable_share": round(len(readable) / total, 4) if total else None,
        # the flag that matters: an outcome that leaned on something nobody provisioned is not
        # reproducible, whatever it scored
        "unprovisioned": bool(auxiliary),
        "why": sorted({f"{s['channel']}:{s['value']}" for s in auxiliary}),
    }


class Readable:
    def __init__(self, store: Store, kanban: Any = None, guardrails: Any = None,
                 audit: Any = None):
        self.store = store
        self.kanban = kanban
        self.guardrails = guardrails
        self.audit = audit

    # ---- rows ----------------------------------------------------------
    def _task(self, task_id: str) -> dict:
        row = self.store.one(
            "select id, team_id, title, column_name, payload, assignee_agent_id from tasks"
            " where id = ?", (task_id,))
        if not row:
            raise NotFound(f"card {task_id} not found")
        return {"id": row["id"], "team_id": row["team_id"], "title": row["title"],
                "column": row["column_name"], "payload": loads(row["payload"], {}),
                "assignee_agent_id": row["assignee_agent_id"] or ""}

    def _rails(self, team_id: str, agent_id: str = "") -> list[dict]:
        rows = self.store.query(
            """select kind, value from guardrails where team_id = ? and status = 'active'
               and (agent_id = '' or agent_id = ?)""", (team_id, agent_id or ""))
        return [{"kind": row["kind"], "value": loads(row["value"], None)} for row in rows]

    def supply(self, task_id: str, agent_id: str = "") -> dict:
        task = self._task(task_id)
        return provisioned(task, self._rails(task["team_id"], agent_id))

    # ---- the reading ---------------------------------------------------
    def for_run(self, run_id: str, record: bool = True) -> dict:
        """what one run reached for, and how much of it was provisioned."""
        row = self.store.one(
            "select id, task_id, agent_id, steps from runs where id = ?", (run_id,))
        if not row:
            raise NotFound(f"run {run_id} not found")
        task = self._task(row["task_id"])
        supply = provisioned(task, self._rails(task["team_id"], row["agent_id"] or ""))
        found = signals(commands_of(loads(row["steps"], [])))
        reading = classify(found, supply)
        reading.update({"run_id": run_id, "task_id": task["id"], "team_id": task["team_id"],
                        "supply": supply})
        if record:
            with self.store.tx() as c:
                c.execute(
                    """insert into readable_checks (id, team_id, task_id, run_id, signals,
                           readable, auxiliary, unprovisioned, why, created_date)
                       values (?,?,?,?,?,?,?,?,?,?)""",
                    (new_id("rdb"), task["team_id"], task["id"], run_id, reading["signals"],
                     len(reading["readable"]), len(reading["auxiliary"]),
                     1 if reading["unprovisioned"] else 0, dumps(reading["why"]), now()))
            reading["id"] = self.store.one(
                "select id from readable_checks where run_id = ? order by created_date desc"
                " limit 1", (run_id,))["id"]
        return reading

    def for_card(self, task_id: str) -> dict:
        """every channel this card's runs reached for, and whether the card leaned on any."""
        task = self._task(task_id)
        runs = self.store.query(
            "select id, agent_id from runs where task_id = ? order by created_date", (task_id,))
        readings = [self.for_run(row["id"], record=False) for row in runs]
        auxiliary: dict[str, dict] = {}
        readable: dict[str, dict] = {}
        for reading in readings:
            for signal in reading["auxiliary"]:
                auxiliary[f"{signal['channel']}:{signal['value']}"] = signal
            for signal in reading["readable"]:
                readable[f"{signal['channel']}:{signal['value']}"] = signal
        total = len(auxiliary) + len(readable)
        return {
            "task_id": task["id"],
            "title": task["title"],
            "column": task["column"],
            "runs": len(readings),
            "readable": sorted(readable.values(), key=lambda s: s["value"]),
            "auxiliary": sorted(auxiliary.values(), key=lambda s: s["value"]),
            "signals": total,
            "readable_share": round(len(readable) / total, 4) if total else None,
            "unprovisioned": bool(auxiliary),
            "supply": readings[0]["supply"] if readings else provisioned(task, self._rails(
                task["team_id"], task["assignee_agent_id"])),
        }

    def for_crew(self, team_id: str, since: float | None = None,
                 until: float | None = None) -> dict:
        until = now() if until is None else float(until)
        since = (until - DEFAULT_WINDOW_DAYS * SECONDS_PER_DAY) if since is None else float(since)
        rows = self.store.query(
            """select distinct task_id from readable_checks where team_id = ?
               and created_date >= ? and created_date <= ?""", (team_id, since, until))
        cards = [self.for_card(row["task_id"]) for row in rows]
        leaning = [card for card in cards if card["unprovisioned"]]
        counted = [card for card in cards if card["signals"]]
        return {
            "team_id": team_id,
            "since": since,
            "until": until,
            "window_days": round((until - since) / SECONDS_PER_DAY, 2),
            "cards": len(cards),
            "measured": len(counted),
            # the number worth reading: how much of the crew's work borrowed what it was not given
            "leaning": len(leaning),
            "leaning_share": round(len(leaning) / len(counted), 4) if counted else None,
            "cards_leaning": sorted(leaning, key=lambda c: -len(c["auxiliary"]))[:10],
        }
