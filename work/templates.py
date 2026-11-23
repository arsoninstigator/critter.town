"""a crew as a document, and the smallest crew that can close a card.

an export that carries authority is not a template, it is a copy of somebody's permissions
with a new name on it. so the document carries what a crew knows and how it is staffed, and
refuses to carry what it can reach: scope permissions, credentials, wildcard egress.

the skeleton answers a smaller question: what is the least a crew can be and still finish
work here. the honest answer is two seats, not one, because the platform refuses to let a
seat sign off its own work. a one-agent crew reaches review and stops. so the skeleton is a
doer, a reviewer, and the rails that keep the pair from spending without a ceiling.
"""

from __future__ import annotations

from typing import Any

from core.db import Store, dumps, loads, new_id, now
from core.errors import Conflict, NotFound, ValidationError

from agents import archetypes, external

SCHEMA = "crittertown.crew/1"

# what a document may say about an agent. anything else is refused rather than ignored,
# because a field silently dropped on import is a promise the template made and broke.
AGENT_FIELDS = (
    "name", "role", "kind", "hosting", "model", "system_prompt", "skills", "tools",
    "specializations", "verticals", "persistence", "context",
)

# a rail is a number or a list. a template may narrow a wall, never widen it.
RAIL_KINDS = (
    "spend_per_day", "open_cards", "parallel_runs", "card_moves_per_day",
    "egress_hosts", "forbidden_tools", "secret_shapes", "forbidden_patterns",
    "max_output_chars", "foreign_refs",
)

SKELETON_AGENTS = (
    {
        "name": "doer",
        "role": "doer",
        "kind": "harness",
        "system_prompt": (
            "you are the doer. take one card at a time, say what you changed, and close it "
            "with the reference the card's kind asks for. if the brief is wrong, say so "
            "instead of guessing at it."
        ),
    },
    {
        "name": "reviewer",
        "role": "reviewer",
        "kind": "harness",
        "system_prompt": (
            "you are the reviewer. read the card, read what the doer says it did, and check "
            "the reference before you sign. a card you cannot check is a card you refuse, "
            "with the reason named."
        ),
    },
)

SKELETON_RAILS = (
    {"kind": "open_cards", "value": 12},
    {"kind": "spend_per_day", "value": 500},
    {"kind": "forbidden_tools", "value": ["rm -rf /", "curl | sh"]},
)

SKELETON_SKILL = """---
name: closing-a-card
description: how to close a card with the reference its kind asks for. use when a card is finished and needs to move to review or done.
---

# closing a card

a card moves to done with a reference, and the kind of work decides which references count.

- code: `commit:<sha>` for the change, `run:<id>` for the test run that passed.
- research: `url:` or `dataset:` for where the answer came from.
- anything else: the reference the card's own kind names.

if you cannot produce the reference, the card is not done. say what is missing instead of
moving it and explaining afterwards.
"""


def validate(doc: Any) -> dict:
    """what a document must be before anything is created from it."""
    if not isinstance(doc, dict):
        raise ValidationError("a template is an object", got=type(doc).__name__)
    if doc.get("schema") != SCHEMA:
        raise ValidationError(
            f"a template declares schema {SCHEMA}", schema=str(doc.get("schema") or ""))
    agents = doc.get("agents")
    if not isinstance(agents, list) or not agents:
        raise ValidationError("a template needs at least one agent", reason="no_agents")
    seen: set[str] = set()
    for agent in agents:
        if not isinstance(agent, dict):
            raise ValidationError("every agent in a template is an object")
        name = str(agent.get("name") or "").strip()
        if not name:
            raise ValidationError("every agent in a template has a name", reason="agent_needs_a_name")
        if name.lower() in seen:
            raise ValidationError(f"two agents are called '{name}'", reason="duplicate_agent")
        seen.add(name.lower())
        unknown = sorted(set(agent) - set(AGENT_FIELDS))
        if unknown:
            raise ValidationError(
                f"'{name}' carries fields a template cannot import: {', '.join(unknown)}",
                reason="unknown_agent_field", fields=unknown)
        kind = str(agent.get("kind") or "general")
        archetypes.validate(kind)
        hosting = external.validate(str(agent.get("hosting") or "local"))
        role = str(agent.get("role") or "")
        verdict = external.may_hold({"hosting": hosting, "kind": kind}, role)
        if not verdict["ok"]:
            raise ValidationError(
                f"'{name}' is {hosting} and cannot hold the {role or 'unlabelled'} seat: "
                f"{verdict['why']}", reason="agent_cannot_hold_seat")
        # the kind is a ceiling under the role, so a template cannot invent a seat that the
        # kind it declares would never be given here
        fills, why = archetypes.may_fill(kind, role)
        if role and not fills:
            raise ValidationError(f"'{name}' is a {kind} agent and cannot fill '{role}': {why}",
                                  reason="kind_cannot_fill_seat")
    for rail in doc.get("rails") or []:
        if not isinstance(rail, dict):
            raise ValidationError("every rail in a template is an object")
        kind = str(rail.get("kind") or "")
        if kind not in RAIL_KINDS:
            raise ValidationError(f"'{kind}' is not a rail a template can set",
                                  reason="unknown_rail_kind")
        value = rail.get("value")
        if kind == "egress_hosts" and isinstance(value, list) and "*" in value:
            raise ValidationError(
                "a template cannot open egress to every host", reason="template_widens_egress")
        if rail.get("agent") and str(rail["agent"]).lower() not in seen:
            raise ValidationError(
                f"a rail names '{rail['agent']}', which is not an agent in this template",
                reason="rail_names_a_stranger")
    return doc


def skeleton(name: str = "skeleton") -> dict:
    """the smallest crew that can close a card, as a document."""
    return validate({
        "schema": SCHEMA,
        "name": name,
        "mission": "the least a crew can be and still finish a card here.",
        "agents": [dict(agent) for agent in SKELETON_AGENTS],
        "rails": [dict(rail) for rail in SKELETON_RAILS],
        "skills": [SKELETON_SKILL],
    })


class TemplateService:
    """export a crew, import one, and stand up the skeleton."""

    def __init__(self, store: Store, crews=None, guardrails=None, skills=None, intent=None):
        self.store = store
        self.crews = crews
        self.guardrails = guardrails
        self.skills = skills
        self.intent = intent

    # ---- out -----------------------------------------------------------
    def export(self, team_id: str) -> dict:
        """what a crew is, in a form that can be handed to somebody else.

        the scope is exported as names of permissions, not as the permission object, so an
        importer sees what the crew could do without being handed the ability to do it.
        """
        if self.crews is None:
            raise ValidationError("exporting a crew needs the crew service")
        team = self.crews.get_team(team_id)
        agents = self.crews.list_agents(team_id)
        rows = []
        for agent in agents:
            rows.append({
                "name": agent["name"],
                "role": agent["role"],
                "kind": agent.get("kind") or "general",
                "hosting": agent.get("hosting") or "local",
                "model": agent["model"],
                "system_prompt": agent["system_prompt"],
                "skills": [s for s in (agent.get("skills") or [])],
                "tools": [t for t in (agent.get("tools") or [])],
                "specializations": list(agent.get("specializations") or []),
                "verticals": list(agent.get("verticals") or []),
            })
        doc: dict = {
            "schema": SCHEMA,
            "name": team["name"],
            "mission": team.get("mission", ""),
            "review_mode": team.get("review_mode", "reviewed"),
            "agents": rows,
            "rails": [],
            "skills": [],
        }
        if self.guardrails is not None:
            for rail in self.guardrails.list(team_id):
                entry = {"kind": rail["kind"], "value": rail["value"]}
                if rail.get("agent_id"):
                    match = [a["name"] for a in agents if a["id"] == rail["agent_id"]]
                    if not match:
                        continue
                    entry["agent"] = match[0]
                doc["rails"].append(entry)
        if self.skills is not None:
            for skill in self.skills.list(team_id):
                doc["skills"].append(skill["text"] if skill.get("text") else self._skill_text(skill))
        return doc

    def _skill_text(self, skill: dict) -> str:
        """a skill as the SKILL.md it came from, rebuilt from the row and its files.

        the file list is the one thing a document cannot carry yet: a skill with bundled
        references exports its body and names what it left behind, so an importer is not
        handed a skill that quietly lost half of itself.
        """
        full = self.skills.get(skill["id"]) if self.skills is not None else skill
        front = ["---", f"name: {full['name']}", f"description: {full['description']}"]
        tools = full.get("allowed_tools") or []
        if tools:
            front.append("allowed-tools: " + " ".join(tools))
        if full.get("license"):
            front.append(f"license: {full['license']}")
        front.append("---")
        body = full.get("body") or ""
        text = "\n".join(front) + "\n\n" + body
        files = full.get("files") or []
        if files:
            text += "\n\nbundled files not carried by this template: " + ", ".join(
                f["path"] for f in files)
        return text

    # ---- in ------------------------------------------------------------
    def import_document(self, team_id: str, doc: dict, actor: str = "human") -> dict:
        """create a crew's staff, rails and skills from a document.

        nothing is created until the whole document validates, so a template that fails on
        its third agent does not leave the first two hired.
        """
        if self.crews is None:
            raise ValidationError("importing a template needs the crew service")
        validate(doc)
        self.crews.get_team(team_id)
        existing = {a["name"].lower() for a in self.crews.list_agents(team_id)}
        clashes = sorted({a["name"].lower() for a in doc["agents"]} & existing)
        if clashes:
            raise Conflict(
                f"this crew already has an agent called {', '.join(clashes)}",
                reason="agent_already_in_crew", agents=clashes)

        created_agents: list[dict] = []
        created_rails: list[dict] = []
        created_skills: list[dict] = []
        for agent in doc["agents"]:
            created_agents.append(self.crews.add_agent(
                team_id,
                name=agent["name"],
                role=agent.get("role", ""),
                model=agent.get("model", ""),
                system_prompt=agent.get("system_prompt", ""),
                skills=agent.get("skills") or [],
                tools=agent.get("tools") or [],
                specializations=agent.get("specializations") or [],
                verticals=agent.get("verticals") or [],
                persistence=agent.get("persistence"),
                kind=agent.get("kind") or "general",
                hosting=agent.get("hosting") or "local",
            ))
        by_name = {a["name"].lower(): a for a in created_agents}
        if self.guardrails is not None:
            for rail in doc.get("rails") or []:
                target = by_name.get(str(rail.get("agent") or "").lower())
                created_rails.append(self.guardrails.set(
                    team_id, rail["kind"], rail["value"],
                    agent_id=(target["id"] if target else ""), actor=actor))
        if self.skills is not None:
            for text in doc.get("skills") or []:
                created_skills.append(self.skills.create(team_id, text, origin="imported"))
        return {
            "agents": created_agents,
            "rails": created_rails,
            "skills": created_skills,
            "template": {"name": doc.get("name", ""), "schema": doc["schema"]},
        }

    def stand_up(self, name: str, doc: dict | None = None, actor: str = "human") -> dict:
        """a crew from a document in one call: create it, then import into it."""
        if self.crews is None:
            raise ValidationError("standing up a crew needs the crew service")
        document = validate(doc or skeleton(name))
        team = self.crews.create_team(
            name, mission=document.get("mission", ""),
            review_mode=document.get("review_mode", "reviewed"))
        imported = self.import_document(team["id"], document, actor=actor)
        return {"team": team, **imported}
