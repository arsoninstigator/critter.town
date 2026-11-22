# the research front

what the field already does, what this repo already has, and what is actually worth building
next for four things the owner named: importing agents from other frameworks, skills, teams,
and guardrails. written after reading the dark factory repos and the standards they build on,
not from memory.

the short version: this repo already has the outer loop, the queue, the gates and the audit
chain that every dark factory repo reinvents. what it does not have is a way in for an agent
built somewhere else, a unit of packaged capability, a formation policy written down as a
rule, and a rail that fires on output rather than only before dispatch. those four are the
work.

---

## 1. what the dark factory repos actually build

five repos, read at the level of their design and their readmes. the shapes repeat.

**coleam00/dark-factory-experiment** is the most honest of them. three layers kept apart on
purpose: a workflow harness (archon) that stitches steps deterministically, the coding agent
(claude code) that holds the tools inside a step, and the model, chosen per node so routing is
a config decision rather than a property of the factory. the orchestrator is a pure bash cron
loop with no llm in it, every thirty minutes, reading the issue tracker and dispatching up to a
parallel cap. it says out loud that it runs at level four, not five: the factory does not write
its own issues, and there is a human authored perimeter it may never touch. it also keeps a
ratchet and a decisions log, which is the part most of these repos skip.

**fabro-sh/fabro** is the same idea with the process as a graph: nodes, verification gates
between them, failure edges that loop back into a fix step, model routing expressed as a
stylesheet, sandboxes for isolation, and durable event streams and checkpoints so a run can be
inspected afterwards. its pitch is disengagement time, not autonomy.

**michaellaws/dark-factory** contributes the one insight the others do not have in code: train
and test separation by filesystem. the generating agent works in a sparse checkout where the
holdout directory does not exist on disk, so it cannot optimise against its own acceptance
criteria. an independent evaluation runs the holdout, and merge happens on a metric rather than
on a judgement. that is the only mechanism here that makes an autonomous merge defensible, and
it is a wall rather than a prompt.

**luisgrandegg/dark-factory** is design only, and useful as a checklist: intake, spec, plan,
implement, qa, integrate, with guardrails named as tool allowlists, budgets and approval gates,
and observability named as token spend, success rate and failure clusters.

**paperclip.ing** is the control plane shape this repo already borrowed from: an org chart with
roles and permissions, tasks whose delegation flows up and down it, heartbeats, budgets,
approvals and an audit log, on the principle that if something can receive a heartbeat it can be
hired.

what they share, and what it means here: a deterministic outer loop, a durable queue of work
items, per node model routing, verification that is not the generator's own test, and a
perimeter. this repo has the loop (scheduler, tick, sweep), the queue (the board with leases),
verification gates (approvals, evidence refs, convergence barriers) and the perimeter (guardrails,
crossings, the agent vm with no ingredients). the two genuine gaps are the holdout wall and
model routing as a policy rather than a setting.

---

## 2. importing agents from other frameworks

three patterns exist in the field, and they are not equally expensive.

**the adapter.** crewai's bring your own agent is the clearest published example: three abstract
adapters, a base agent adapter (configure tools, configure structured output), a tool adapter and
a converter, and you write a compatibility layer per framework. it buys fidelity and costs a
class per framework per version, plus their dependencies inside your process.

**the heartbeat.** paperclip's version: a foreign agent is not imported at all. it polls, claims
work, reports back, and the platform never learns what runtime it is. the cost is that the
platform cannot see inside a turn, so evidence has to be produced by the agent and checked by
the platform, which is exactly the discipline this repo already enforces.

**the protocol.** a2a for agent to agent, mcp for tools. this repo already speaks a2a on the
wire (`comms/protocol.py`, written against the published schema), so a foreign agent that speaks
a2a can already reach a crittertown agent. what is missing is the other direction: a foreign
agent being given custody of a card.

the decision worth taking is the heartbeat, because it needs no adapter code, keeps the four
roots clean (nothing foreign is copied in), and every action a foreign agent takes is still a row
in the same audit chain. the shape:

- an agent row with `runtime = "external"`, a token scoped to exactly four calls: heartbeat,
  claim, renew, complete
- `POST /agents/{id}/heartbeat` answers with the next claimable card or `204`, and the answer
  carries the payload, the slice snapshot and the evidence scheme the card expects
- refusals that matter: an external agent may not review, may not integrate, may not hold a2a
  custody, and may not be dispatched a card whose kind it cannot satisfy. those are the same
  `may_fill` rules archetypes already applies, so nothing new is invented
- the lease, the silence watchdog and the strikes apply unchanged, which is the point: a foreign
  worker gets the same custody a local one does, and the same way of losing it

if an adapter is ever wanted, it belongs in `toolbox/`, called over an api, never imported.

---

## 3. skills

the standard is now published (agent skills, with agentskills.io as the spec): a directory
containing `SKILL.md`, yaml frontmatter with `name` (64 chars, lowercase and hyphens) and
`description` (1024 chars, what it does and when to use it), optional `license`,
`compatibility`, `metadata` and `allowed-tools`, then a markdown body, then optional
`scripts/`, `references/` and `assets/`. the whole design rests on progressive disclosure in
three levels: metadata always in context, body read when the skill looks relevant, bundled
files read only as the body points at them.

this repo has the machinery and not the unit. `skills` is a profile namespace, slices are
character budgets, lessons and knowledge feed the prompt, and `agents/context.py` already refuses
to hand an agent more than its seat allows. what is missing is a skill as a thing: named, versioned,
importable, with files.

the shape that fits what exists:

- a `skills` table: name, description, body, files (refs), allowed_tools, license, compatibility,
  source, version. scoped to a crew or to the town
- context assembly includes only the metadata (level one), which is what the slice budget wants
- a `load_skill` tool returns the body (level two), and `read_skill_file` returns one bundled
  file (level three), so the budget is spent deliberately rather than at startup
- an importer that reads a directory of `SKILL.md` folders and writes rows. it refuses a
  description that does not say when to use it, a body over the slice budget, a name that is not
  lowercase hyphenated, and a `allowed-tools` entry the seat cannot have. a skill may narrow
  permissions and never widen them, which is the same rule archetypes applies to kinds
- a bundled script runs on the agent's own computer, never the platform's. a skill is data plus
  files; the platform never executes it

---

## 4. teams

the field agrees on the vocabulary and disagrees on where the hierarchy lives. langchain's
guidance is the one worth keeping in the room: start single, add tools before agents, and only
graduate to multi agent shapes when the simpler thing fails. paperclip puts the org chart in the
product. this repo already went further than both on the decision side: `work/topology.py`
chooses a shape from declared signals, `work/roster.py` turns a shape into seats with role
entitlements and slice caps, `agents/archetypes.py` caps which seats a kind of worker may fill,
and `org/departments.py` holds coordinators, nesting, routing and escalation.

what is missing is three things, in increasing size:

1. **formation policy as a rule, written down.** it is already half implemented and not stated:
   which kind may fill which seat, which slices each seat may read, and what happens when nobody
   qualifies. the last one matters: today a gap is a gap, and a gap that silently degrades to
   solo is how a two seat job becomes one agent doing both badly. it should refuse with a named
   gap and offer the three fixes (widen the roster, split the card, accept the cheaper shape
   explicitly)
2. **a crew as a portable thing.** a crew template is the same idea as a skill one level up:
   departments, seats, kinds, slices, routines, rails, and the crossings it declares, exported
   as a file and imported into another town. this is what makes a crew reviewable in a diff and
   reusable across towns, and it is cheap because every part is already a row
3. **wings.** the owner's frame puts departments and wings between the town and the crew. the
   useful content of a wing is aggregation and scope: its own intent, its own budget ceiling,
   its own rails, and the crews under it inheriting all three. that is mostly a join and a
   budget roll up, not a new machine

---

## 5. guardrails

the field has two answers. nemo guardrails is the mature one: rails declared in colang and cel,
input rails that block before the model sees a request, output rails that check before the
answer is returned, tool call and tool result checks, and a proxy mode that puts all of it in
front of any app. microsoft's governance toolkit is the policy answer. the dark factory repos
take the cheap version: tool allowlists, budgets and approval gates.

this repo has the cheap version already, and better than most: six rail kinds checked before
dispatch, refusals counted separately from errors, a refusal recorded as a strike against the
seat that earned it, and approvals that open with single use signed links. what is missing is
three things:

1. **rails as data.** today a rail is a row written through the api. it should compile from a
   declarative file the way `work/permeability.py` compiles crossings: parse, refuse what cannot
   be enforced, store what compiled. a crew's rails then live in a diff, which is the only way a
   rail change gets reviewed
2. **an output rail.** every rail today fires before dispatch. nothing checks what an agent is
   about to emit or what a tool returned. egress covers hosts, not content. the smallest useful
   version: a rail kind that names a pattern or a predicate, checked at the moment output is
   recorded, refusing the turn and filing the strike the same way a dispatch refusal does
3. **refusal attribution.** refusals are counted, but not per rail, so "which limit is doing the
   work" cannot be answered. a rail id on every refusal row makes the brief able to rank rails
   by refusals, which is how a rail that is too tight gets found before it teaches everyone to
   work around it

---

## 6. what i would build, in order

1. **external agents over heartbeat.** unlocks importing agents without adapters, reuses
   custody, silence and strikes unchanged. small to medium, mostly a token scope and a route
2. **skills as rows plus files, with an importer.** medium. depends on nothing else here
3. **rails as data, then an output rail.** medium. the compile half is a straight copy of the
   crossings shape
4. **crew templates.** small, once skills have shown the export and import pattern
5. **the holdout wall.** medium, and the one that makes "self improving" trustworthy rather than
   confident: an eval whose scenarios the agent that is being improved cannot read

the honest caveat: this was read at the level of readmes, design docs and the standards they cite,
plus the paperclip clone that is already local. before porting anything from a repo, its code
needs the same treatment paperclip got, which is a read of the module rather than the pitch.

## sources

- https://github.com/coleam00/dark-factory-experiment
- https://github.com/fabro-sh/fabro
- https://github.com/michaellaws/dark-factory
- https://github.com/luisgrandegg/dark-factory
- https://github.com/paperclipai/paperclip
- https://docs.crewai.com/en/learn/bring-your-own-agent
- https://agentskills.io/specification
- https://www.anthropic.com/research/equipping-agents-for-the-real-world-with-agent-skills
- https://github.com/NVIDIA-NeMo/Guardrails
- https://www.langchain.com/blog/choosing-the-right-multi-agent-architecture
