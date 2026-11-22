"""skills: know-how an agent can carry, and the three stages it is loaded in.

the spec this follows is agentskills.io's SKILL.md, which is small enough to be a contract
rather than a format: a name, a description that says what the skill does and when to use it,
and an optional body plus bundled files. the reason it is worth implementing rather than
inventing is that the description is the whole retrieval mechanism. an agent decides whether a
skill applies by reading one line, so a vague description is a skill that never fires, and a
skill that never fires is a file nobody reads.

progressive disclosure is the part that has to be built rather than promised:

  level 1  name and description, resident in the context, one line per skill
  level 2  the body, fetched with load_skill when the agent decides the skill applies
  level 3  a bundled file, fetched with read_skill_file at the moment it is needed

the budget is the point. a crew with forty skills must not spend forty bodies of context on
the card that needs none of them, so level 1 is charged to a slice and levels 2 and 3 are
charged to the call that asked for them, never to the slice.

what the loader refuses is the security story. a skill is text a foreign author wrote, and
its bundled scripts run on the agent's computer, so the refusals are about reach rather than
style: an unscoped shell in allowed-tools, a body too large to read in one turn, a name that
is not a name, a description that does not say when. the skill cannot widen a permission,
because a skill that could would be a way to import a permission decision from outside.
"""

from __future__ import annotations

import re

from core.db import Store, dumps, loads, new_id, now
from core.errors import Conflict, NotFound, ValidationError

NAME_MAX = 64
DESCRIPTION_MAX = 1024
BODY_MAX = 20000
FILE_MAX = 200000
DISCOVERY_BUDGET = 500
BODY_BUDGET = 8000

NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# the tools that reach outside the agent's own reasoning. an unscoped one is a blank cheque
REACHING = ("bash", "shell", "sh", "exec", "run", "powershell", "cmd")


# ---- the file format ---------------------------------------------------
def parse_skill_md(text: str) -> dict:
    """a SKILL.md, split into frontmatter and body.

    yaml is read here as what it is in practice for this file: flat keys, and lists either
    as `- item` lines or as `[a, b]`. a parser that pulls in a yaml dependency to read four
    keys is a dependency that has to be patched for security.
    """
    raw = (text or "").replace("\r\n", "\n")
    if not raw.strip():
        raise ValidationError("a skill is a SKILL.md, and this is empty", why="skill_empty")
    if not raw.lstrip().startswith("---"):
        raise ValidationError("a SKILL.md opens with frontmatter between --- lines",
                              why="frontmatter_missing")
    stripped = raw.lstrip()
    end = stripped.find("\n---", 3)
    if end < 0:
        raise ValidationError("the frontmatter is not closed with ---",
                              why="frontmatter_unclosed")
    front = stripped[3:end]
    body = stripped[end + 4:].lstrip("\n")
    meta: dict = {}
    key = ""
    for line in front.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        if line.lstrip().startswith("- ") and key:
            current = meta.get(key)
            if not isinstance(current, list):
                current = []
                meta[key] = current
            current.append(_unquote(line.lstrip()[2:].strip()))
            continue
        if ":" not in line:
            raise ValidationError(f"frontmatter line is not a key: {line[:40]}",
                                  why="frontmatter_unreadable")
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if not value:
            meta[key] = []
            continue
        if value.startswith("[") and value.endswith("]"):
            meta[key] = [_unquote(part.strip()) for part in value[1:-1].split(",") if part.strip()]
            continue
        meta[key] = _unquote(value)
    return {"meta": meta, "body": body.strip()}


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def validate(parsed: dict) -> dict:
    """everything a skill must be before it is stored, as refusals rather than defaults.

    a default here would be an invented description, and an invented description is a skill
    that fires on the wrong cards.
    """
    meta = parsed.get("meta") or {}
    body = (parsed.get("body") or "").strip()
    name = str(meta.get("name") or "").strip()
    description = str(meta.get("description") or "").strip()
    if not name:
        raise ValidationError("a skill needs a name", why="skill_name_required")
    if len(name) > NAME_MAX:
        raise ValidationError(f"a skill name is at most {NAME_MAX} characters",
                              why="skill_name_too_long", length=len(name))
    if not NAME_RE.match(name):
        raise ValidationError(
            "a skill name is lowercase letters, digits and single hyphens, no edges",
            why="skill_name_not_a_slug", name=name)
    if not description:
        raise ValidationError("a skill needs a description", why="description_required")
    if len(description) > DESCRIPTION_MAX:
        raise ValidationError(f"a description is at most {DESCRIPTION_MAX} characters",
                              why="description_too_long", length=len(description))
    if len(description) < 20:
        raise ValidationError(
            "a description is what the agent matches on, so it has to say what the skill "
            "does and when to use it", why="description_too_thin", length=len(description))
    if "use" not in description.lower():
        # the spec's own rule, enforced: without a "use when", nothing decides to load it
        raise ValidationError(
            "a description must say when to use the skill, not only what it does",
            why="description_must_say_when")
    if not body:
        raise ValidationError("a skill needs a body; the description is the trigger",
                              why="body_required")
    if len(body) > BODY_MAX:
        raise ValidationError(f"a body is at most {BODY_MAX} characters, or it is a file",
                              why="body_too_long", length=len(body))
    tools = meta.get("allowed-tools")
    if isinstance(tools, str):
        tools = [part for part in re.split(r"[,\s]+", tools) if part]
    tools = [str(tool).strip() for tool in (tools or []) if str(tool).strip()]
    for tool in tools:
        base = tool.split("(")[0].strip().lower()
        if base in REACHING and "(" not in tool:
            raise ValidationError(
                f"allowed-tools may not grant an unscoped {base}: scope it, as in {base}(git)",
                why="allowed_tool_unscoped", tool=tool)
    if _truthy(meta.get("disable-model-invocation")) and not _truthy(meta.get("user-invocable")):
        raise ValidationError(
            "a skill nobody can invoke and no model can match is a file, not a skill",
            why="skill_unreachable")
    return {"name": name, "description": description, "body": body,
            "allowed_tools": tools, "license": str(meta.get("license") or ""),
            "metadata": {k: v for k, v in meta.items()
                         if k not in {"name", "description", "allowed-tools", "license"}}}


def _truthy(value) -> bool:
    return str(value).strip().lower() in {"true", "yes", "1"}


def check_path(path: str) -> str:
    """a bundled file path, kept inside the skill. a skill that can name ../../ is a skill
    that can read the agent's credentials, so the path is relative, clean and flat."""
    clean = (path or "").strip().replace("\\", "/")
    if not clean:
        raise ValidationError("a bundled file needs a path", why="file_path_required")
    if clean.startswith("/") or ":" in clean.split("/")[0]:
        raise ValidationError("a bundled path is relative to the skill",
                              why="file_path_absolute", path=clean)
    parts = [part for part in clean.split("/") if part not in {"", "."}]
    if any(part == ".." for part in parts):
        raise ValidationError("a bundled path may not climb out of the skill",
                              why="file_path_escapes", path=clean)
    return "/".join(parts)


class SkillService:
    def __init__(self, store: Store, crews=None):
        self.store = store
        self.crews = crews

    # ---- writes --------------------------------------------------------
    def create(self, team_id: str, text: str = "", *, name: str = "", description: str = "",
               body: str = "", allowed_tools: list[str] | None = None,
               agents: list[str] | None = None, origin: str = "authored",
               source: str = "") -> dict:
        """one entry point for both shapes: a SKILL.md as text, or the same fields as json.

        the text form is the one that matters, because it is what other tools export and
        what a person can paste. the json form exists so a caller can import a skill whose
        files come in a second call.
        """
        if self.crews is not None:
            self.crews.get_team(team_id)
        if text:
            parsed = parse_skill_md(text)
            checked = validate(parsed)
        else:
            checked = validate({"meta": {"name": name, "description": description,
                                         "allowed-tools": allowed_tools or []},
                                "body": body})
        existing = self.store.one("select id from skills where team_id = ? and name = ?",
                                  (team_id, checked["name"]))
        if existing:
            raise Conflict(f"a skill named '{checked['name']}' already exists in this crew",
                           skill_id=existing["id"])
        for agent_id in (agents or []):
            if self.crews is not None:
                self.crews.get_agent(agent_id)
        skill_id = new_id("skl")
        ts = now()
        with self.store.tx() as c:
            c.execute(
                """insert into skills
                   (id, team_id, name, description, body, allowed_tools, metadata, license,
                    agents, origin, source, enabled, body_chars, created_date, updated_date)
                   values (?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)""",
                (skill_id, team_id, checked["name"], checked["description"], checked["body"],
                 dumps(checked["allowed_tools"]), dumps(checked["metadata"]),
                 checked["license"], dumps(agents or []), origin, source,
                 len(checked["body"]), ts, ts))
        return self.get(skill_id)

    def add_file(self, skill_id: str, path: str, content: str) -> dict:
        skill = self.get(skill_id)
        clean = check_path(path)
        if len(content or "") > FILE_MAX:
            raise ValidationError(f"a bundled file is at most {FILE_MAX} characters",
                                  why="file_too_large", length=len(content or ""))
        existing = self.store.one(
            "select id from skill_files where skill_id = ? and path = ?", (skill_id, clean))
        ts = now()
        with self.store.tx() as c:
            if existing:
                c.execute("update skill_files set content = ?, bytes = ? where id = ?",
                          (content or "", len(content or ""), existing["id"]))
                file_id = existing["id"]
            else:
                file_id = new_id("skf")
                c.execute(
                    """insert into skill_files (id, skill_id, path, content, bytes, created_date)
                       values (?,?,?,?,?,?)""",
                    (file_id, skill_id, clean, content or "", len(content or ""), ts))
            c.execute("update skills set updated_date = ? where id = ?", (ts, skill_id))
        return {"id": file_id, "skill_id": skill_id, "path": clean, "bytes": len(content or "")}

    def patch(self, skill_id: str, enabled: bool | None = None,
              agents: list[str] | None = None) -> dict:
        self.get(skill_id)
        patch: dict = {}
        if enabled is not None:
            patch["enabled"] = 1 if enabled else 0
        if agents is not None:
            patch["agents"] = dumps(agents)
        if not patch:
            return self.get(skill_id)
        sets = ", ".join(f"{key} = ?" for key in patch)
        with self.store.tx() as c:
            c.execute(f"update skills set {sets}, updated_date = ? where id = ?",
                      (*patch.values(), now(), skill_id))
        return self.get(skill_id)

    def delete(self, skill_id: str) -> dict:
        skill = self.get(skill_id)
        with self.store.tx() as c:
            c.execute("delete from skills where id = ?", (skill_id,))
        return {"deleted": skill_id, "name": skill["name"]}

    # ---- reads ---------------------------------------------------------
    def get(self, skill_id: str) -> dict:
        row = self.store.one("select * from skills where id = ?", (skill_id,))
        if not row:
            raise NotFound(f"skill {skill_id} not found")
        files = self.store.query(
            "select id, path, bytes from skill_files where skill_id = ? order by path",
            (skill_id,))
        return {
            "id": row["id"], "team_id": row["team_id"], "name": row["name"],
            "description": row["description"], "body": row["body"],
            "body_chars": row["body_chars"],
            "allowed_tools": loads(row["allowed_tools"], []),
            "metadata": loads(row["metadata"], {}), "license": row["license"],
            "agents": loads(row["agents"], []), "origin": row["origin"],
            "source": row["source"], "enabled": bool(row["enabled"]),
            "files": [{"path": f["path"], "bytes": f["bytes"]} for f in files],
            "created_date": row["created_date"], "updated_date": row["updated_date"],
        }

    def list(self, team_id: str, include_disabled: bool = False,
             agent_id: str = "") -> list[dict]:
        """level 1 and nothing else: a list of skills is a list of names and descriptions,
        because that is all the caller needs to decide which one to open."""
        rows = self.store.query(
            "select * from skills where team_id = ? order by name", (team_id,))
        out = []
        for row in rows:
            if not include_disabled and not row["enabled"]:
                continue
            agents = loads(row["agents"], [])
            if agent_id and agents and agent_id not in agents:
                continue
            out.append({"id": row["id"], "name": row["name"],
                        "description": row["description"], "enabled": bool(row["enabled"]),
                        "body_chars": row["body_chars"],
                        "allowed_tools": loads(row["allowed_tools"], []),
                        "agents": agents, "files": self.store.one(
                            "select count(*) as n from skill_files where skill_id = ?",
                            (row["id"],))["n"]})
        return out

    def read_file(self, skill_id: str, path: str) -> dict:
        clean = check_path(path)
        row = self.store.one("select * from skill_files where skill_id = ? and path = ?",
                             (skill_id, clean))
        if not row:
            raise NotFound(f"skill {skill_id} has no file {clean}")
        return {"skill_id": skill_id, "path": row["path"], "bytes": row["bytes"],
                "content": row["content"]}

    # ---- the two loading stages ----------------------------------------
    def discovery(self, team_id: str, agent_id: str = "",
                  budget: int = DISCOVERY_BUDGET) -> dict:
        """level 1, charged to a slice: one line per skill, and the budget is real."""
        lines: list[str] = []
        used = 0
        listed = self.list(team_id, agent_id=agent_id)
        for skill in listed:
            line = f"- {skill['name']}: {skill['description']}"
            if used + len(line) + 1 > budget:
                break
            lines.append(line)
            used += len(line) + 1
        return {"lines": lines, "chars": used, "offered": len(lines),
                "withheld": max(0, len(listed) - len(lines)), "budget": budget}

    def load(self, skill_id: str, budget: int = BODY_BUDGET) -> dict:
        """level 2, charged to the call. a body over the budget is refused rather than
        truncated, because half a procedure read as a whole one is worse than no skill."""
        skill = self.get(skill_id)
        if not skill["enabled"]:
            raise ValidationError("this skill is disabled", why="skill_disabled",
                                  skill_id=skill_id)
        if len(skill["body"]) > budget:
            raise ValidationError(
                f"this body is {len(skill['body'])} characters and the budget is {budget}; "
                "read it as a file or split the skill", why="body_over_budget",
                length=len(skill["body"]), budget=budget)
        return {"id": skill["id"], "name": skill["name"], "body": skill["body"],
                "allowed_tools": skill["allowed_tools"],
                "files": skill["files"], "chars": len(skill["body"])}
