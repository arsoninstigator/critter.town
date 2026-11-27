"""the orchestrator: turns a kanban task into a sandboxed agent run.

the loop per task:

1. validate the task and the crew, move it to `in_progress`
2. choose an agent (explicit assignee wins; otherwise capability match)
3. build the agent's computer - the agent's persistent one, or a throwaway
   ephemeral sandbox when the task asks for isolation
4. run up to `max_turns` model turns, executing shell commands in the sandbox and
   logging every one
5. post the agent's outgoing message through the a2a gate
6. write the result, then move the card to `review` (reviewed crews) or `done`
7. destroy ephemeral sandboxes, never the persistent ones

every step is persisted, so a crashed worker leaves a readable `runs` row instead
of an orphaned process. the orchestrator holds no state between calls.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from comms.a2a import A2AService
from core.config import Settings
from agents.crews import CrewService
from core.db import Store, dumps, loads, new_id, now
from work import topology
from core.errors import (CritterTownError, NotAllowed, NotFound, ProviderFailure,
                         ProviderNotConfigured, ValidationError)
from ingredients.projectmanagement.kanban.board import KanbanService
from agents.runtime import AgentModel, answer_text, build_model, render_prompt
from sandbox import Handle, ProviderRegistry

# how many times a card may be handed to an environment that never started before it stops
# asking. three is the number of providers a crew typically has configured, so a card that
# has tried all of them has learned what it can from retrying
MAX_ENV_BLOCKS = 3


class CrewRunner:
    events_hook = None
    def __init__(
        self,
        store: Store,
        registry: ProviderRegistry,
        settings: Settings,
        model: AgentModel | None = None,
        intent=None,
        guardrails=None,
        profile=None,
    ):
        self.store = store
        self.registry = registry
        self.settings = settings
        self.model = model or build_model(settings)
        # set by the composition root: the crew's intent is read into every prompt
        self.intent = intent
        # set by the composition root: the standing limits, checked before a card
        # is allowed to become work
        self.guardrails = guardrails
        self.profile = profile
        # set by the composition root: a dispatched card is held under a lease, so a
        # holder that dies leaves an expiry instead of a card nobody can explain
        self.custody = None
        # set by the composition root: a fan-out closes on a record, not on a hope
        self.convergence = None
        # set by the composition root: the ledger a declared budget is reserved in
        self.budget = None
        # set by the composition root: output is the heartbeat the watchdog reads, so
        # a lease that renews while the work is silent stops looking alive
        self.silence = None
        self.knowledge = None
        # set by the composition root: the prompt is assembled from slices
        self.context = None
        # set by the composition root: a mandate that names the card is a script for one
        # card, so the check runs when the card is handed over
        self.mandate = None
        # set by the composition root: a reason to work is recorded before it is acted on,
        # so a wake that cannot run now is joined or kept instead of lost
        self.wakes = None
        # set by the composition root: what the run was handed, hashed, so drift is visible
        self.witness = None
        # set by the composition root: a card whose ancestor is blocked inherits that
        # rather than spending an attempt on a premise that does not exist yet
        self.lineage = None
        # set by the composition root: an inherited failure is a decision worth a row
        self.audit = None
        self.crews = CrewService(store, registry)
        self.kanban = KanbanService(store)
        self.a2a = A2AService(store)

    # ---- agent selection ----------------------------------------------
    def pick_agent(self, team_id: str, task: dict) -> dict:
        agents = [a for a in self.crews.list_agents(team_id) if a["status"] != "retired"]
        if task.get("assignee_agent_id"):
            for agent in agents:
                if agent["id"] == task["assignee_agent_id"]:
                    return agent
            raise NotFound(f"assigned agent {task['assignee_agent_id']} is not in this crew")
        if not agents:
            raise ValidationError("this crew has no agents to run the task")
        required = {s.lower() for s in task.get("required_skills") or []}
        words = {w.lower() for w in (task.get("title", "") + " " + task.get("description", "")).split() if len(w) > 3}

        def score(agent: dict) -> tuple[int, int]:
            skills = {s.lower() for s in agent["skills"]}
            role = {w.lower() for w in (agent["role"] or "").split() if len(w) > 3}
            skill_hits = len(required & skills) * 4 + len(words & (skills | role))
            return (skill_hits, 1 if agent["status"] == "idle" else 0)

        return sorted(agents, key=score, reverse=True)[0]

    # ---- planning ------------------------------------------------------
    def plan(self, task_id: str) -> list[dict]:
        """expand a task into subtasks.

        explicit subtasks in the task payload win. otherwise the crew policy
        decides whether the task stays whole. no model call here on purpose:
        planning must be cheap and reproducible, the model does the work.
        """
        task = self.kanban.get_task(task_id)
        team = self.crews.get_team(task["team_id"])
        policy = team["orchestration"]
        declared = task["payload"].get("subtasks") if isinstance(task["payload"], dict) else None
        if not isinstance(declared, list) or not declared:
            if not policy.get("decompose") or task["payload"].get("subtasks") is False:
                return []
            return []
        created = []
        for index, item in enumerate(declared):
            if not isinstance(item, dict) or not item.get("title"):
                continue
            created.append(
                self.kanban.create_task(
                    team_id=task["team_id"],
                    title=str(item["title"])[:200],
                    description=str(item.get("description", "")),
                    column="ready",
                    priority=int(item.get("priority", task["priority"])),
                    required_skills=list(item.get("required_skills") or []),
                    isolation=item.get("isolation", task["isolation"]),
                    payload=item.get("payload") or {},
                    parent_id=task["id"],
                )
            )
        if len(created) > 1 and self.convergence is not None:
            # the lanes are cards, not seats: a card is what holds custody here, so it is
            # what a barrier can name. join is all, because a parent that closes on one
            # lane has cancelled the others by accident.
            lanes = {child["id"]: child["id"] for child in created}
            self.convergence.open(task_id, lanes, list(lanes), join="all")
        return created

    # ---- the run -------------------------------------------------------
    def dispatch(self, task_id: str, max_turns: int = 6, actor: str = "orchestrator") -> dict:
        task = self.kanban.get_task(task_id)
        team = self.crews.get_team(task["team_id"])
        if task["column"] not in {"ready", "in_progress"}:
            raise ValidationError(
                f"task is in '{task['column']}'; move it to 'ready' before dispatching"
            )
        # the limits answer before a card becomes work. team wide first, so a crew at
        # its ceiling refuses without even choosing an agent
        if self.guardrails is not None:
            self.guardrails.enforce(task["team_id"], "dispatch", {}, task=task)
        agent = self.pick_agent(task["team_id"], task)
        if self.profile is not None and self.profile.relation_of(agent["id"]) == "sidecar":
            # a sidecar works beside a card, inside its parent's run. giving it one of
            # its own would make it a child agent with a child's paperwork and none of
            # a child's lineage, which is the confusion this split exists to end.
            raise NotAllowed(
                f"{agent['name']} is a sidecar on another card; a sidecar cannot own one"
            )
        # a wake that arrives while another agent holds this card used to come back as a
        # conflict and vanish. the queue decides instead: run it, join the run already on
        # it, wait behind a person, or refuse with a reason. only `run` continues here
        wake: dict | None = None
        if self.wakes is not None:
            wake = self.wakes.admit(task, agent_id=agent["id"], source=actor)
            if wake["decision"] != "run":
                return {
                    "run_id": "",
                    "task": task,
                    "agent": {"id": agent["id"], "name": agent["name"]},
                    "decision": wake["decision"],
                    "reason": wake["reason"],
                    "wake": wake,
                    "steps": [],
                    "result": {},
                    "error": "",
                }
        if self.guardrails is not None:
            self.guardrails.enforce(
                task["team_id"], "dispatch",
                {"agent_id": agent["id"], "tools": agent.get("tools") or []},
                task=task,
            )
        # a card is not judged on its own when its parent never landed. the chain is read
        # before a lease, a reservation or an attempt is spent, because a child of a blocked
        # card has no premise to work from and would fail for its parent's reason
        if self.lineage is not None:
            inherited = self.lineage.for_dispatch(task)
            if inherited["decision"] != "run":
                return self._inherited(task, agent, inherited)
        # the seat's mandate is read against the card it is about to carry. this records
        # a finding and never stops the work: a false positive on a word must not cost a
        # dispatch, and a pattern across cards is the thing worth seeing
        mandate_findings: list[dict] = []
        if self.mandate is not None:
            checked = self.mandate.for_dispatch(task, agent)
            mandate_findings = checked.get("findings") or []
        # a card may declare the shape it wants, and the one refusal is a card that
        # asks for orchestration its own signals do not justify: that shape costs seats,
        # tokens and a convergence, and it is what a big-feeling task asks for
        if topology is not None:
            topology.check_declared(task.get("payload"), topology.signals_of(task))
        reservation = None
        if self.budget is not None:
            declared = (task.get("payload") or {}).get("budget_cents")
            if declared:
                # the check and the claim are one act, so two parallel cards cannot both
                # read the same under-budget number and both proceed
                reservation = self.budget.hold(
                    task["team_id"], declared, task_id=task_id, agent_id=agent["id"],
                    reason=f"run of {task['title'][:60]}")
        # custody before the column: a card in progress that nobody holds is the state
        # that used to need a person to notice it
        if self.custody is not None:
            self.custody.claim(task_id, agent["id"])
        if task["column"] == "ready":
            task = self.kanban.move(task_id, "in_progress", actor)
        self.crews.update_agent(agent["id"], status="working")

        run_id = new_id("run")
        provider = None
        handle: Handle | None = None
        steps: list[dict] = []
        error = ""
        result: dict = {}
        try:
            try:
                provider, handle = self._sandbox(agent, task)
            except (ProviderNotConfigured, ProviderFailure) as blocked:
                # the work did not fail, the room did not open. spending an attempt on
                # that charges the card for the environment's problem
                return self._environment_blocked(task, agent, blocked, wake, reservation)
            self._insert_run(run_id, task, agent, provider.name, handle.ref)
            if wake is not None:
                self.wakes.delivered(wake["wake_id"], run_id)
            inbox = self.a2a.inbox(agent["id"], limit=10)
            context = self.crews.context(task["team_id"])["values"]
            intent_block = self.intent.prompt(team["id"], agent["id"]) if self.intent else ""
            # one assembly, recorded: what this agent was handed for this run, as slices.
            # the knowledge block lives inside it now, so nothing is said twice.
            snapshot = None
            if self.context is not None:
                try:
                    snapshot = self.context.compose(
                        agent["id"], task_id=task_id, run_id=run_id
                    )
                except CritterTownError:
                    snapshot = None
            prompt = render_prompt(
                task, agent, team, context, inbox,
                intent=intent_block,
                knowledge=(snapshot["block"] if snapshot else (
                    self.knowledge.for_prompt(team["id"], agent["id"])
                    if self.knowledge is not None else ""
                )),
            )
            if self.witness is not None:
                # hashed here rather than at claim time, because this is the moment the
                # set is decided: the brief, the knowledge this seat is shown, the parent
                try:
                    self.witness.record_for(run_id, task, agent_id=agent["id"])
                except CritterTownError:
                    # a card that cannot be witnessed is still worth running; the sweep
                    # reports it as unwitnessed rather than pretending it was checked
                    pass
            system_prompt = agent["system_prompt"] or (
                f"you are {agent['name']}, {agent['role'] or 'a member'} of the {team['name']} crew."
            )
            done = False
            for turn in range(1, max_turns + 1):
                action = self.model.act(system_prompt, prompt)
                if self.guardrails is not None:
                    # the answer is checked before a command runs or a message leaves.
                    # a rail that fires afterwards is a report, not a rail.
                    self.guardrails.enforce_output(
                        task["team_id"], answer_text(action), agent_id=agent["id"],
                        context={"foreign_refs": self._foreign_refs(task["team_id"])},
                        task=task, run_id=run_id)
                turn_steps = []
                for command in action.get("commands", []):
                    outcome = provider.exec(handle, command, timeout=min(self.settings.sandbox_timeout_seconds, 600))
                    turn_steps.append(
                        {"kind": "command", "command": command, **outcome.to_dict()}
                    )
                    prompt += f"\n\n$ {command}\n{outcome.stdout[-1500:]}{outcome.stderr[-500:]}"
                message = action.get("message")
                if message:
                    posted = self._post_message(team, agent, task, message)
                    turn_steps.append({"kind": "a2a", "message_id": posted["id"], "status": posted["status"]})
                steps.append({"turn": turn, "thought": action.get("thought", ""), "steps": turn_steps})
                self._update_run(run_id, steps)
                if self.silence is not None:
                    # a turn happened, so the work produced something. recorded before
                    # the renew, because the renew is what makes a silent run look alive
                    self.silence.record_output(task_id, agent["id"],
                                               size=len(prompt))
                if self.custody is not None:
                    try:
                        self.custody.renew(task_id, agent["id"])
                    except CritterTownError as lost:
                        # the lease went while we were thinking. the sweep owns the card
                        # now, and a run that keeps writing would be writing over it.
                        raise NotAllowed(
                            "custody of this card was lost mid-run: the lease expired",
                            task_id=task_id, reason="lease_expired") from lost
                if action.get("done"):
                    done = True
                    result = action.get("result") or {}
                    break
            if not done:
                result = {"summary": "turn budget exhausted", "turns": max_turns}
                steps.append({"turn": max_turns, "thought": "turn budget exhausted", "steps": []})
        except CritterTownError as exc:
            error = f"{exc.code}: {exc.message}"
            self._update_run(run_id, steps, status="failed", error=error)
            # a bad instruction is not a failed holder. a validation refusal means the
            # brief was wrong, so the card blocks at once and replan is the way forward;
            # a runtime failure spends one attempt and puts the card back in play.
            target = "blocked"
            if self.custody is not None:
                try:
                    handed_back = self.custody.abandon(
                        task_id, agent["id"], error,
                        terminal=isinstance(exc, (ValidationError, NotAllowed)))
                    target = handed_back["column"]
                except CritterTownError:
                    # the lease is gone, so the sweep is the one that can reclaim it
                    target = "blocked"
            if reservation is not None and self.budget is not None:
                # the work did not happen, so the reservation goes back rather than
                # settling at the estimate and quietly charging for a failure
                try:
                    self.budget.release(reservation["id"], reason=error[:200])
                except CritterTownError:
                    pass
            self.kanban.move(task_id, target, agent["id"], note=error[:500])
            if self.convergence is not None:
                # a lane that failed is a blocked report, not a silence: the barrier has
                # to be able to say this lane is out and why
                lane = self.convergence.for_partition(task_id)
                if lane:
                    try:
                        self.convergence.report(lane["id"], {
                            "partition": task_id, "participant": task_id,
                            "outcome": "blocked", "reason": error[:300],
                        })
                    except CritterTownError:
                        pass
            self._retire_sidecars(task_id)
            if self.wakes is not None:
                # a wake that joined this run is not answered by a run that broke
                self.wakes.settle(run_id, outcome="failed")
            if self.events_hook is not None:
                self.events_hook("run.failed", {
                    "team_id": task["team_id"], "task_id": task_id, "run_id": run_id,
                    "agent_id": agent["id"], "error": error, "actor": agent["id"],
                })
            self.crews.update_agent(agent["id"], status="idle")
            raise
        finally:
            if provider is not None and handle is not None and handle.kind == "ephemeral":
                try:
                    provider.destroy(handle)
                except CritterTownError:
                    pass
            # a sidecar exists for one run of one card. when the run ends, whichever way
            # it ends, the helper's job is done, and a helper that outlives its run is an
            # agent nobody assigned to anything.
            self._retire_sidecars(task_id)

        self._update_run(run_id, steps, status="succeeded")
        if self.wakes is not None:
            self.wakes.settle(run_id, outcome="succeeded")
        self.crews.update_agent(agent["id"], status="idle")
        self.crews.put_context(
            task["team_id"],
            f"task:{task_id}:result",
            {"agent": agent["name"], "result": result},
            author=agent["name"],
        )
        if self.custody is not None:
            self.custody.complete(task_id, agent["id"], evidence=f"run:{run_id}")
        reviewed = team["review_mode"] == "reviewed" and "review" in self.kanban.columns(team["id"])
        task = self.kanban.complete(
            task_id, {**result, "run_id": run_id, "agent_id": agent["id"]},
            actor=agent["id"], column="review" if reviewed else "done",
            evidence=f"run:{run_id}",
        )
        if reservation is not None and self.budget is not None:
            spent = (task.get("payload") or {}).get("spent_cents")
            self.budget.settle(reservation["id"], spent)
        if self.convergence is not None:
            lane = self.convergence.for_partition(task_id)
            if lane:
                try:
                    self.convergence.report(lane["id"], {
                        "partition": task_id, "participant": task_id, "outcome": "result",
                        "artifact_ref": f"run:{run_id}",
                        "touches": [task.get("payload", {}).get("surface") or task_id],
                    })
                except CritterTownError:
                    # a lane that cannot report is a barrier that will stall, and the
                    # sweep will say so. failing the lane here would hide it
                    pass
        return {
            "run_id": run_id,
            "task": task,
            "agent": {"id": agent["id"], "name": agent["name"]},
            "provider": provider.name if provider else "",
            "sandbox": handle.ref if handle else "",
            "steps": steps,
            "result": result,
            "error": error,
            "mandate_findings": mandate_findings,
        }

    def dispatch_ready(self, team_id: str, limit: int = 10, max_turns: int = 6) -> list[dict]:
        if self.wakes is not None:
            # a wake deferred behind a person comes back on its own once the card leaves
            # review, because a deferral nobody revives is a drop with better manners
            self.wakes.revive(team_id)
        ready = self.kanban.list_tasks(team_id, column="ready", limit=limit)
        if not ready:
            return []
        workers = max(1, min(self.settings.max_parallel_runs, len(ready)))
        outcomes: list[dict] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self.dispatch, t["id"], max_turns): t for t in ready}
            for future, task in futures.items():
                try:
                    outcomes.append(future.result())
                except CritterTownError as exc:
                    outcomes.append({"task": task, "error": exc.to_dict()})
        return outcomes

    # ---- sandbox -------------------------------------------------------
    def _sandbox(self, agent: dict, task: dict) -> tuple[Any, Handle]:
        """which machine this run gets.

        persistence decides, not the task: an agent configured for `task_state` never
        gets a computer, even for a task that asked to be persistent, because the whole
        point of that setting is that nothing survives the run. it gets a throwaway
        sandbox keyed to the task instead, so the work still happens and still leaves
        nothing behind.
        """
        runtime = agent["runtime"] or {}
        task_state = (agent.get("persistence") or "workspace") == "task_state"
        if task["isolation"] == "ephemeral" or task_state:
            provider = self.registry.get("ephemeral")
            spec = {
                "kind": "ephemeral",
                "template": runtime.get("template") or self.settings.ephemeral_default_template,
                "ttl_seconds": runtime.get("ttl_seconds") or self.settings.sandbox_timeout_seconds,
            }
            return provider, provider.ensure(f"task-{task['id']}", spec)
        computer = self.crews.ensure_computer(agent["id"])
        provider = self.registry.get(computer["provider"])
        return provider, Handle.from_dict(computer["handle"])

    # ---- runs ----------------------------------------------------------
    def _foreign_refs(self, team_id: str, limit: int = 400) -> list[str]:
        """the card ids that belong to other crews.

        this is what makes the foreign_refs rail answerable: an answer that names one of
        these has quoted work its crew cannot see, and the rail does not need to know
        which crew or what the card said to refuse it.
        """
        rows = self.store.query(
            "select id from tasks where team_id != ? order by created_date desc limit ?",
            (team_id, limit))
        return [row["id"] for row in rows]

    def _retire_sidecars(self, task_id: str) -> None:
        if self.profile is None:
            return
        try:
            self.profile.retire_sidecars(task_id)
        except CritterTownError:
            pass

    def _inherited(self, task: dict, agent: dict, verdict: dict) -> dict:
        """the child of a card that never landed: parked, or left waiting on a person.

        no attempt is spent and no lease is taken, because neither belongs to this card: the
        failure is the ancestor's, and charging it twice is how one broken step reads as
        five.
        """
        from work.lineage import park

        task_id = task["id"]
        if verdict["decision"] == "inherit_blocked":
            note = park(task, verdict)
            try:
                self.kanban.move(task_id, "blocked", "system")
            except CritterTownError:
                pass
            # the reason is written on the row, not only in the event: a blocked card that
            # cannot say what blocked it is the silence this whole unit is about
            with self.store.tx() as c:
                c.execute("update tasks set blocked_reason = ? where id = ?", (note, task_id))
            task = self.kanban.get_task(task_id)
        if self.audit is not None:
            self.audit.record_event("task.inherited_failure", {
                "team_id": task["team_id"], "agent_id": agent["id"], "task": task,
                "data": {"task_id": task_id, "decision": verdict["decision"],
                         "reason": verdict["reason"], "ancestor": verdict["ancestor"]},
            })
        return {
            "run_id": "",
            "task": task,
            "agent": {"id": agent["id"], "name": agent["name"]},
            "decision": verdict["decision"],
            "reason": verdict["reason"],
            "ancestor": verdict["ancestor"],
            "steps": [],
            "result": {},
            "error": "",
        }

    def _environment_blocked(self, task: dict, agent: dict, exc: CritterTownError,
                             wake: dict | None, reservation: dict | None) -> dict:
        """the room did not open: no attempt spent, the card goes back, the wake stays.

        repeated blocks are the one case that must not loop: after MAX_ENV_BLOCKS the card
        blocks with the reason, because a crew whose provider is down should stop asking
        rather than burn a queue on it.
        """
        task_id = task["id"]
        blocks = int(task.get("env_blocks") or 0) + 1
        if reservation is not None and self.budget is not None:
            self.budget.release(reservation["id"], reason="the environment did not start")
        if self.custody is not None:
            try:
                self.custody.release(task_id)
            except CritterTownError:
                pass
        with self.store.tx() as c:
            c.execute("update tasks set env_blocks = ? where id = ?", (blocks, task_id))
        self.crews.update_agent(agent["id"], status="idle")
        target = "ready"
        if blocks >= MAX_ENV_BLOCKS:
            target = "blocked"
        try:
            self.kanban.move(task_id, target, "system",
                             note=f"environment unavailable: {exc.code} ({blocks}x)")
        except CritterTownError:
            # a card that cannot be moved is still reported as blocked by the return value
            pass
        return {
            "run_id": "",
            "task": self.kanban.get_task(task_id),
            "agent": {"id": agent["id"], "name": agent["name"]},
            "decision": "env_blocked",
            "reason": exc.code,
            "wake": wake,
            "env_blocks": blocks,
            "steps": [],
            "result": {},
            "error": f"{exc.code}: {exc.message}",
        }

    def _insert_run(self, run_id: str, task: dict, agent: dict, provider: str, sandbox: str) -> None:
        with self.store.tx() as c:
            c.execute(
                """insert into runs (id, task_id, agent_id, provider, sandbox_id, status, steps,
                       started_at, created_date) values (?,?,?,?,?, 'running', '[]', ?, ?)""",
                (run_id, task["id"], agent["id"], provider, sandbox, now(), now()),
            )

    def _update_run(self, run_id: str, steps: list[dict], status: str | None = None, error: str = "") -> None:
        with self.store.tx() as c:
            c.execute("update runs set steps = ? where id = ?", (dumps(steps), run_id))
            if status:
                c.execute(
                    "update runs set status = ?, error = ?, ended_at = ? where id = ?",
                    (status, error, now(), run_id),
                )

    def get_run(self, run_id: str) -> dict:
        row = self.store.one("select * from runs where id = ?", (run_id,))
        if not row:
            raise NotFound(f"run {run_id} not found")
        return {
            "id": row["id"],
            "task_id": row["task_id"],
            "agent_id": row["agent_id"],
            "provider": row["provider"],
            "sandbox_id": row["sandbox_id"],
            "status": row["status"],
            "steps": loads(row["steps"], []),
            "error": row["error"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
        }

    def list_runs(self, task_id: str) -> list[dict]:
        return [
            {"id": r["id"], "status": r["status"], "agent_id": r["agent_id"],
             "provider": r["provider"], "created_date": r["created_date"]}
            for r in self.store.query(
                "select * from runs where task_id = ? order by created_date desc", (task_id,)
            )
        ]

    # ---- messages ------------------------------------------------------
    def _post_message(self, team: dict, agent: dict, task: dict, message: dict) -> dict:
        to_name = message.get("to")
        to_agent_id = None
        if to_name:
            match = [a for a in self.crews.list_agents(team["id"]) if a["name"] == to_name]
            if not match:
                raise ValidationError(f"message recipient '{to_name}' is not in this crew")
            to_agent_id = match[0]["id"]
        kind = message.get("kind") or "note"
        return self.a2a.send(
            team_id=team["id"],
            from_agent_id=agent["id"],
            body=str(message.get("body", ""))[:8000],
            to_agent_id=to_agent_id,
            task_id=task["id"],
            kind=kind if kind in {"note", "handoff", "request", "result", "blocker", "review_request"} else "note",
        )
