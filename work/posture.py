"""what a crew can actually reach, what that reach costs it, and who accepted the risk.

a crew's posture is not a document. it is the union of rows that already exist: the egress hosts
its rails allow, the credentials the broker issued it, the connectors it reads, the channels it
speaks in, the skills it can load, and which of its seats are run by somebody else. this module
gathers that into one inventory and reads a fixed set of rules over it, because "what can this crew
reach" answered from memory is a claim, and answered from rows is a fact.

three decisions shape the rest:

- a finding has a stable key, `rule:subject`, so a dismissal attaches to the thing and not to a
  position in a list. recompute the inventory and the dismissal still lands.
- a dismissal expires. risk acceptance without a date is a decision nobody revisits, so `until` is
  required and a lapsed acceptance comes back as a finding that names the person who gave it and
  when it ran out. that is not a punishment, it is the only way a temporary exception stays
  temporary.
- nothing here halts a dispatch by default. a crew may state `posture.block_at` and then findings at
  or above that severity stop the work, because a crew that has decided what blocks should be able
  to make it block. a crew that has not decided gets a reading and no surprise.
"""

from __future__ import annotations

from typing import Any

from core.db import Store, loads, new_id, now
from core.errors import Conflict, NotFound, ValidationError

# ordered lightest to heaviest, so a threshold is a comparison and not a lookup table
SEVERITIES = ("info", "low", "medium", "high", "critical")
RANK = {name: index for index, name in enumerate(SEVERITIES)}

# the roles whose signature closes a card. an external seat in one of these is a person-shaped
# hole in the gate: it judges evidence it did not produce and cannot inspect
REVIEW_ROLES = ("reviewer", "integrator", "approver")

# a credential's stated scope, read as a set of strings. anything that means "everything" is worth
# a person looking at it once
WIDE_SCOPES = ("*", "all", "admin", "root", "write:all", "full")

DISMISSAL_GRACE_DAYS = 0


def rank(severity: str) -> int:
    if severity not in RANK:
        raise ValidationError(f"severity must be one of {list(SEVERITIES)}", severity=severity)
    return RANK[severity]


def at_least(severity: str, threshold: str) -> bool:
    return rank(severity) >= rank(threshold)


def finding(rule: str, subject: str, severity: str, detail: str, remedy: str = "",
            **extra: Any) -> dict:
    """one finding, with a key that survives a recompute and a sentence a person can act on."""
    rank(severity)
    return {"key": f"{rule}:{subject}", "rule": rule, "subject": subject,
            "severity": severity, "detail": detail, "remedy": remedy, **extra}


def _scopes(value: Any) -> list[str]:
    if isinstance(value, str):
        value = loads(value, [])
    return [str(scope).strip().lower() for scope in (value or [])]


class Posture:
    def __init__(self, store: Store, crews: Any = None, audit: Any = None):
        self.store = store
        self.crews = crews
        self.audit = audit

    # ---- policy --------------------------------------------------------
    def policy(self, team_id: str) -> dict:
        row = self.store.one("select orchestration from teams where id = ?", (team_id,))
        if not row:
            raise NotFound(f"crew {team_id} not found")
        return (loads(row["orchestration"], {}) or {}).get("posture") or {}

    def threshold(self, team_id: str) -> str | None:
        """the severity at which this crew lets a finding stop work, or None if it has not said."""
        stated = self.policy(team_id).get("block_at")
        if not stated:
            return None
        rank(str(stated))
        return str(stated)

    # ---- inventory -----------------------------------------------------
    def inventory(self, team_id: str) -> dict:
        """what this crew can reach, as rows rather than as intentions."""
        rails = [{"id": row["id"], "agent_id": row["agent_id"], "kind": row["kind"],
                  "value": loads(row["value"], row["value"]), "status": row["status"]}
                 for row in self.store.query(
                     "select * from guardrails where team_id = ? and status = 'active'",
                     (team_id,))]
        hosts: dict[str, list[str]] = {}
        for rail in rails:
            if rail["kind"] != "egress_hosts" or not isinstance(rail["value"], list):
                continue
            for host in rail["value"]:
                hosts.setdefault(str(host).lower(), []).append(rail["agent_id"] or "crew")
        secrets = [{"id": row["id"], "label": row["label"], "token_env": row["token_env"],
                    "agent_id": row["agent_id"], "scopes": _scopes(row["scopes"]),
                    "status": row["status"], "expires_at": row["expires_at"]}
                   for row in self.store.query(
                       """select * from credential_broker where team_id = ?
                          and status = 'active' and revoked_at is null""", (team_id,))]
        connectors = [{"id": row["id"], "kind": row["kind"], "name": row["name"],
                       "status": row["status"], "doc_count": row["doc_count"]}
                      for row in self.store.query(
                          "select * from connectors where team_id = ?", (team_id,))]
        channels = [{"id": row["id"], "name": row["name"], "slug": row["slug"],
                     "kind": row["kind"]}
                    for row in self.store.query(
                        "select * from channels where team_id = ?", (team_id,))]
        skills = [{"id": row["id"], "name": row["name"], "origin": row["origin"],
                   "allowed_tools": loads(row["allowed_tools"], []),
                   "agents": loads(row["agents"], [])}
                  for row in self.store.query(
                      "select * from skills where team_id = ?", (team_id,))]
        agents = [{"id": row["id"], "name": row["name"], "role": row["role"],
                   "hosting": row["hosting"] if "hosting" in row.keys() else "local",
                   "status": row["status"]}
                  for row in self.store.query(
                      "select * from agents where team_id = ? and status != 'retired'",
                      (team_id,))]
        return {
            "team_id": team_id,
            "hosts": [{"host": host, "allowed_by": sorted(set(by))}
                      for host, by in sorted(hosts.items())],
            "secrets": secrets,
            "connectors": connectors,
            "channels": channels,
            "skills": skills,
            "agents": agents,
            "counts": {"hosts": len(hosts), "secrets": len(secrets),
                       "connectors": len(connectors), "channels": len(channels),
                       "skills": len(skills), "agents": len(agents),
                       "external_agents": sum(1 for a in agents if a["hosting"] == "external")},
        }

    # ---- rules ---------------------------------------------------------
    def findings(self, team_id: str, at: float | None = None) -> list[dict]:
        """every rule read over one inventory. rules are cheap, so they all run every time."""
        inv = self.inventory(team_id)
        moment = at if at is not None else now()
        found: list[dict] = []

        # egress: a crew that can reach anything has no allowlist, whatever the rail is called
        for entry in inv["hosts"]:
            host = entry["host"]
            if host in ("*", "0.0.0.0", "::"):
                found.append(finding(
                    "egress_unbounded", host, "high",
                    "the crew's egress rail allows every host, so the allowlist is a formality",
                    "name the hosts the crew actually needs, or record why it needs all of them",
                    allowed_by=entry["allowed_by"]))
            elif host.startswith("*."):
                found.append(finding(
                    "egress_wildcard", host, "medium",
                    f"{host} is every subdomain of that domain, which is usually a whole company",
                    "list the subdomains the crew uses", allowed_by=entry["allowed_by"]))

        # credentials: an active secret is a standing reach, and an expired one is a reach that
        # is about to fail in the middle of somebody's work rather than at a moment they chose
        for secret in inv["secrets"]:
            label = secret["label"] or secret["token_env"] or secret["id"]
            if secret["expires_at"] and float(secret["expires_at"]) < moment:
                found.append(finding(
                    "secret_expired", label, "high",
                    f"the credential {label} is active but its expiry has passed, so the next "
                    f"run that uses it fails for a reason nobody chose",
                    "reissue it, or revoke it so the failure is at dispatch"))
            elif secret["expires_at"] and float(secret["expires_at"]) - moment < 7 * 86400:
                found.append(finding(
                    "secret_expiring", label, "low",
                    f"the credential {label} expires within a week",
                    "reissue it before a run discovers it for you"))
            if not secret["scopes"]:
                found.append(finding(
                    "secret_unscoped", label, "medium",
                    f"the credential {label} states no scopes, so nobody can say what it reaches",
                    "state the scopes, or narrow them to what the work needs"))
            wide = [scope for scope in secret["scopes"] if scope in WIDE_SCOPES]
            if wide:
                found.append(finding(
                    "secret_wide", label, "medium",
                    f"the credential {label} carries {', '.join(wide)}, which is more than any "
                    f"single card needs",
                    "issue a narrower credential per work kind", scopes=wide))

        # seats: an external agent cannot sign a card off, so a crew that gave it the seat has a
        # reviewer on paper only
        for agent in inv["agents"]:
            if agent["hosting"] != "external":
                continue
            if str(agent["role"]).lower() in REVIEW_ROLES:
                found.append(finding(
                    "external_seat", agent["name"], "high",
                    f"{agent['name']} is run outside this platform and holds the {agent['role']} "
                    f"seat, so the review it would sign cannot be checked",
                    "move the review seat to a local agent, or narrow the role"))
            else:
                found.append(finding(
                    "external_agent", agent["name"], "info",
                    f"{agent['name']} is run outside this platform",
                    "none, as long as it holds no review seat"))

        # sources: a connector that is not connected is a source of truth that quietly stopped
        for connector in inv["connectors"]:
            if connector["status"] != "connected":
                found.append(finding(
                    "connector_down", connector["name"], "low",
                    f"the {connector['kind']} connector {connector['name']} is "
                    f"{connector['status']}, so anything read from it is as old as its last sync",
                    "reconnect it, or remove it so the crew stops believing it"))

        # skills: a skill that may call any tool is a grant nobody wrote down
        for skill in inv["skills"]:
            tools = [str(tool).lower() for tool in skill["allowed_tools"]]
            if "*" in tools or "all" in tools:
                found.append(finding(
                    "skill_open_tools", skill["name"], "medium",
                    f"the skill {skill['name']} may call any tool",
                    "name the tools it needs"))

        # the inventory itself: a crew with no egress rail has provisioned no hosts, which is not
        # a risk but is worth saying, because a silent empty allowlist reads as permission
        if inv["agents"] and not inv["hosts"]:
            found.append(finding(
                "no_egress_declared", team_id, "info",
                "no active egress rail names a host, so this crew's reach is whatever its "
                "sandbox happens to allow",
                "declare the hosts the work needs, so a run that leaves them is visible"))
        return sorted(found, key=lambda f: (-rank(f["severity"]), f["key"]))

    # ---- dismissals ----------------------------------------------------
    def _dismissal(self, dismissal_id: str) -> dict:
        row = self.store.one("select * from posture_dismissals where id = ?", (dismissal_id,))
        if not row:
            raise NotFound(f"dismissal {dismissal_id} not found")
        return {"id": row["id"], "team_id": row["team_id"], "key": row["finding_key"],
                "severity": row["severity"], "owner": row["owner"], "reason": row["reason"],
                "until": row["until_date"], "created_date": row["created_date"]}

    def dismiss(self, team_id: str, key: str, owner: str, reason: str, until: float) -> dict:
        """a risk accepted by a named person until a stated moment. all four are required."""
        key = str(key or "").strip()
        owner = str(owner or "").strip()
        reason = " ".join(str(reason or "").split())
        if not key:
            raise ValidationError("a dismissal names the finding it covers")
        if not owner:
            raise ValidationError("a dismissal names the person who accepts the risk")
        if not reason:
            raise ValidationError("a dismissal states why the risk is acceptable")
        if not until:
            raise ValidationError("a dismissal expires: give the moment it stops applying")
        if float(until) <= now():
            raise ValidationError("a dismissal that has already expired dismisses nothing")
        match = [f for f in self.findings(team_id) if f["key"] == key]
        if not match:
            raise NotFound(f"no open finding with key {key}")
        dismissal_id = new_id("dsm")
        with self.store.tx() as c:
            c.execute(
                """insert into posture_dismissals (id, team_id, finding_key, severity, owner,
                       reason, until_date, created_date) values (?,?,?,?,?,?,?,?)""",
                (dismissal_id, team_id, key, match[0]["severity"], owner, reason,
                 float(until), now()))
        return self._dismissal(dismissal_id)

    def dismissals(self, team_id: str, at: float | None = None,
                   include_expired: bool = True) -> list[dict]:
        moment = at if at is not None else now()
        rows = self.store.query(
            "select id from posture_dismissals where team_id = ? order by created_date desc",
            (team_id,))
        out = []
        for row in rows:
            dismissal = self._dismissal(row["id"])
            dismissal["expired"] = float(dismissal["until"]) <= moment
            if dismissal["expired"] and not include_expired:
                continue
            out.append(dismissal)
        return out

    # ---- the reading ---------------------------------------------------
    def reading(self, team_id: str, at: float | None = None) -> dict:
        """the inventory, the findings, and which of them somebody has already accepted."""
        moment = at if at is not None else now()
        active = {d["key"]: d for d in self.dismissals(team_id, at=moment, include_expired=False)}
        lapsed = {d["key"]: d for d in self.dismissals(team_id, at=moment)
                  if d["expired"]}
        open_findings, accepted = [], []
        for found in self.findings(team_id, at=moment):
            if found["key"] in active:
                accepted.append({**found, "dismissed": active[found["key"]]})
            elif found["key"] in lapsed:
                # an acceptance that ran out is not a finding again for the first time. it comes
                # back naming the person who accepted it, because the second decision is theirs
                open_findings.append({**found, "lapsed": lapsed[found["key"]]})
            else:
                open_findings.append(found)
        threshold = self.threshold(team_id)
        blocking = ([f for f in open_findings if at_least(f["severity"], threshold)]
                    if threshold else [])
        return {
            "team_id": team_id,
            "inventory": self.inventory(team_id),
            "findings": open_findings,
            "accepted": accepted,
            "counts": {severity: sum(1 for f in open_findings if f["severity"] == severity)
                       for severity in SEVERITIES},
            "block_at": threshold,
            "blocking": blocking,
        }

    def for_dispatch(self, task: dict, agent: dict | None = None) -> dict:
        """the reading a dispatch is allowed to see, and the decision the crew asked for."""
        team_id = task["team_id"]
        threshold = self.threshold(team_id)
        if not threshold:
            return {"verdict": "unset", "blocking": [], "findings": 0}
        reading = self.reading(team_id)
        blocking = reading["blocking"]
        return {"verdict": "blocked" if blocking else "clear",
                "block_at": threshold,
                "blocking": [{"key": f["key"], "severity": f["severity"],
                              "detail": f["detail"]} for f in blocking],
                "findings": len(reading["findings"])}

    def check_dispatch(self, task: dict, agent: dict | None = None) -> dict:
        """a crew that stated what blocks gets a refusal, not a note on a run."""
        checked = self.for_dispatch(task, agent)
        if checked["verdict"] == "blocked":
            first = checked["blocking"][0]
            raise Conflict(
                f"the crew's posture blocks this dispatch: {first['detail']}",
                key=first["key"], severity=first["severity"], block_at=checked["block_at"],
                blocking=checked["blocking"],
            )
        return checked
