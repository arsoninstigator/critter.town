"""working context as slices, because a prompt is a budget and not a dump.

an agent does not need the crew's whole working life to write one post. it needs the
part that bears on this card: the intent above it, what the crew already knows, the few
things waiting on it, and what went wrong last time. the rest is noise that costs tokens
and, worse, dilutes the instruction.

so context is assembled from named slices. each slice has a builder, a character budget
and a reason to exist. an agent can ask for fewer (profile `scope.context`), nothing can
ask for more than the budget, and every assembly is written to `context_snapshots`, so
"what was this agent told" is a row rather than an inference from a prompt nobody kept.
"""

from __future__ import annotations

from typing import Any

from core.db import Store, dumps, loads, new_id, now

# name -> budget in characters. the budget is the point: a slice that cannot fit is
# truncated and says so, rather than pushing the card out of the prompt.
SLICES: dict[str, int] = {
    "intent": 600,
    "knowledge": 900,
    "lessons": 600,
    "task": 500,
    "board": 400,
    "inbox": 400,
    "failures": 400,
    "crew": 300,
}

ORDER = ("intent", "knowledge", "lessons", "task", "board", "inbox", "failures", "crew")


class ContextSlicer:
    def __init__(self, store: Store, crews=None, kanban=None, intent=None, knowledge=None,
                 profile=None, a2a=None):
        self.store = store
        self.crews = crews
        self.kanban = kanban
        self.intent = intent
        self.knowledge = knowledge
        self.profile = profile
        self.a2a = a2a

    # ---- the slices ----------------------------------------------------
    def _slice_intent(self, agent: dict, task: dict | None) -> list[str]:
        block = self.intent.prompt(agent["team_id"], agent["id"]) if self.intent else ""
        return [block] if block else []

    def _slice_knowledge(self, agent: dict, task: dict | None) -> list[str]:
        block = (self.knowledge.for_prompt(agent["team_id"], agent["id"])
                 if self.knowledge else "")
        return [block] if block else []

    def _slice_lessons(self, agent: dict, task: dict | None) -> list[str]:
        """what this agent was taught, which is the half of self improvement that pays.

        read from the profile rather than the lesson table on purpose: an approved lesson
        lives in `instructions` with `origin = lesson:<id>`, so what steers a run is what
        a person promoted, not what an engine proposed.
        """
        if self.profile is None:
            return []
        profile = self.profile.profile(agent["id"])
        entries = profile["namespaces"].get("instructions") or {}
        lines = [
            f"- {value}" for key, value in entries.items()
            if str(key).startswith("lesson:") and isinstance(value, str) and value.strip()
        ]
        return ["what this agent has been taught:"] + lines if lines else []

    def _slice_task(self, agent: dict, task: dict | None) -> list[str]:
        if not task:
            return []
        lines = [f"card: {task['title']} ({task['column']})"]
        if task.get("description"):
            lines.append(f"details: {task['description']}")
        if task.get("required_skills"):
            lines.append("skills the card asks for: " + ", ".join(task["required_skills"]))
        if task.get("payload"):
            lines.append(f"payload: {dumps(task['payload'])[:200]}")
        return lines

    def _slice_board(self, agent: dict, task: dict | None) -> list[str]:
        if self.kanban is None:
            return []
        rows = self.store.query(
            """select title, column_name from tasks
               where team_id = ? and column_name in ('ready', 'in_progress')
               order by priority desc, created_date limit 8""",
            (agent["team_id"],),
        )
        if not rows:
            return []
        return ["open work in the crew (not yours unless the card says so):"] + [
            f"- [{row['column_name']}] {row['title']}" for row in rows
        ]

    def _slice_inbox(self, agent: dict, task: dict | None) -> list[str]:
        if self.a2a is None:
            return []
        messages = self.a2a.inbox(agent["id"], limit=3) or []
        if not messages:
            return []
        return ["approved messages waiting for you:"] + [
            f"- [{m['kind']}] from {m['from_agent_id']}: {str(m['body'])[:120]}"
            for m in messages
        ]

    def _slice_failures(self, agent: dict, task: dict | None) -> list[str]:
        rows = self.store.query(
            """select error from runs where agent_id = ? and status = 'failed'
               and error != '' order by created_date desc limit 2""",
            (agent["id"],),
        )
        if not rows:
            return []
        return ["how this agent's recent runs failed:"] + [
            f"- {str(row['error'])[:180]}" for row in rows
        ]

    def _slice_crew(self, agent: dict, task: dict | None) -> list[str]:
        if self.crews is None:
            return []
        crew = self.crews.get_team(agent["team_id"])
        lines = [f"crew: {crew['name']} ({crew['mission'] or 'no mission set'})"]
        values = (self.crews.context(agent["team_id"]) or {}).get("values") or {}
        for key, value in list(values.items())[:5]:
            lines.append(f"{key} = {dumps(value)[:120]}")
        return lines

    def _build(self, name: str, agent: dict, task: dict | None) -> list[str]:
        builder = getattr(self, "_slice_" + name, None)
        if builder is None:
            return []
        try:
            return builder(agent, task) or []
        except Exception:  # a slice that breaks must not take the run with it
            return []

    # ---- assembly ------------------------------------------------------
    def needs_for(self, agent_id: str) -> list[str]:
        """what this agent asked for, in the tree's order, with nothing invented.

        the default is every slice, because an agent that has not said otherwise should
        not silently lose the intent. an explicit list is honoured, and unknown names are
        dropped rather than guessed at.
        """
        wanted: list[str] = list(ORDER)
        if self.profile is not None:
            try:
                scope = self.profile.profile(agent_id)["namespaces"].get("scope") or {}
            except Exception:
                scope = {}
            asked = scope.get("context")
            if isinstance(asked, list) and asked:
                wanted = [name for name in ORDER if name in asked]
        return wanted

    def compose(self, agent_id: str, task_id: str = "", run_id: str = "",
                needs: list[str] | None = None, store: bool = True,
                role: str = "") -> dict:
        if self.crews is None:
            raise ValueError("composing context needs the crew service")
        agent = self.crews.get_agent(agent_id)
        task = self.kanban.get_task(task_id) if (task_id and self.kanban) else None
        wanted = needs or self.needs_for(agent_id)
        if role:
            # a seat's role caps what it may be told, whatever the profile enabled: a
            # reviewer holding the doer's lessons is not reviewing, it is agreeing
            from work.roster import ROLE_SLICES

            entitled = ROLE_SLICES.get(role)
            if entitled is None:
                raise ValueError(f"unknown seat role '{role}'")
            wanted = [name for name in wanted if name in entitled]
        chosen = [name for name in ORDER if name in wanted]
        slices: list[dict] = []
        blocks: list[str] = []
        for name in chosen:
            budget = SLICES[name]
            lines = self._build(name, agent, task)
            if not lines:
                continue
            text = "\n".join(lines)
            truncated = len(text) > budget
            if truncated:
                # the ellipsis counts against the budget: a slice that reports 904 chars
                # against a 900 budget is a budget that is not one
                text = text[: max(0, budget - 4)].rstrip() + " ..."
            blocks.append(text)
            slices.append({"name": name, "chars": len(text), "lines": len(lines),
                           "truncated": truncated})
        block = ""
        if blocks:
            block = ("working context, sliced for this agent "
                     f"({len(slices)} slice(s); nothing outside this list bears on the card):\n"
                     + "\n".join(blocks))
        snapshot = {
            "id": new_id("ctx"),
            "team_id": agent["team_id"],
            "agent_id": agent["id"],
            "task_id": task_id or "",
            "run_id": run_id or "",
            "needs": chosen,
            "slices": slices,
            "chars": len(block),
            "created_date": now(),
        }
        if store:
            with self.store.tx() as cursor:
                cursor.execute(
                    """insert into context_snapshots
                       (id, team_id, agent_id, task_id, run_id, needs, slices, chars,
                        created_date) values (?,?,?,?,?,?,?,?,?)""",
                    (snapshot["id"], snapshot["team_id"], snapshot["agent_id"],
                     snapshot["task_id"], snapshot["run_id"], dumps(chosen),
                     dumps(slices), snapshot["chars"], snapshot["created_date"]),
                )
        snapshot["block"] = block
        return snapshot

    def recent(self, agent_id: str, limit: int = 3) -> list[dict]:
        rows = self.store.query(
            """select * from context_snapshots where agent_id = ?
               order by created_date desc limit ?""",
            (agent_id, max(1, limit)),
        )
        return [
            {
                "id": row["id"],
                "task_id": row["task_id"],
                "run_id": row["run_id"],
                "needs": loads(row["needs"], []),
                "slices": loads(row["slices"], []),
                "chars": int(row["chars"]),
                "created_date": row["created_date"],
            }
            for row in rows
        ]

    def for_run(self, snapshot: dict) -> str:
        """the block a run carries. empty when the agent asked for no slices at all."""
        return snapshot.get("block", "")
