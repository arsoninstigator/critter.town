# glossary

two lists, kept apart on purpose.

the first is the **glossary**: words the readme and the api use. each is defined once, and
where a term has a table next to it that table is the shape the code stores, so the
definition is what the tests hold it to.

the second is **internal vocabulary**: the words we coined while building. house names for
things whose code name is plainer (`ando` is a `channel`), and the metaphors the tree is
laid out in (`the town`, `the four roots`). a caller never has to know the second list, but
this repo thinks in it, so it is written down rather than assumed.

## glossary

### a2a
every agent-to-agent message is a row with a sender, a recipient, a task and a thread. in
a `reviewed` crew the message is `pending` and `inbox()` does not return it until someone
approves it. that is the whole point: an unreviewed claim cannot change another agent's
behaviour. on the wire it is spec-shaped a2a json over json-rpc (`/a2a/rpc`, agent cards
at `/agents/{id}/card`).

### agent
a specialist: a role, skills, a system prompt, a runtime (persistent computer or
ephemeral sandbox) and tools. three seed agents are enough to feel like a team: a
researcher who cites, a writer who only writes from crew research, and an operator who
ships.

### authoring
a workflow can be written by one or more agents (`POST /crews/{id}/workflows/propose`).
it lands as `proposed` and is inert: `run` refuses it until a human calls `approve`.
after that, `publish` binds a cron and the beat runs it. agents get authorship, humans
keep the switch.

### brief
the night, as numbers. at the end of a shift window (18:00 to 06:00 by default) the
brief service counts what happened from rows, writes two paragraphs, and checks every
number in the prose against the numbers it computed. a sentence with an unattributed
figure does not ship. `worker` kind `brief` builds it, `POST /crews/{id}/brief` builds
one on demand, `GET /crews/{id}/briefs` lists the archive.

### channel
a room inside a crew: members, a conversation, and **its own board**. a task carries a
`channel_id`, so `#growth` and `#support` keep separate kanbans while the crew board
still shows everything. rooms are flat on purpose.

### computer
a persistent sandbox per agent, provisioned once and reused. the agent's files and
installed tooling survive between tasks. this is the persistent lifecycle, and by default it
is persistent's OSS runtime on your own machine (`pip install persistent`, named microVMs, no
account). see `selfhost.md`.

### connector
workspace knowledge an agent is allowed to read: a folder, a git repo, an http endpoint.
a connector is a row with a kind, a config and a secret **name**, never a secret value. a
supervisor grants an agent a connector for a bounded number of hours and within its own
department, and every read lands in the agent's audit chain. `GET
/agents/{id}/knowledge` is the search; `/connectors/*` is the lifecycle.

### coordination
leases and streams, so two agents cannot both claim the same card and an agent can read
what happened before it woke up. the contract is `claim`, `release`, `append`, `read`;
the tested backend today is the same sqlite file as everything else, and
`ingredients/durableobjects/` carries the transport plan for a self-hosted durable actors
runtime.

### crew
a named group with a mission, a review mode (`reviewed` or `open`), an orchestration
policy, its own kanban columns and its own context. table: `teams`. a crew is a team, and
the town is the thing teams live in: the word for a team did not change when the word for
the place did.

### attempts
a card that failed a run keeps a count. a runtime failure hands the card back to `ready`
and spends one attempt, so a flaky machine is a retry rather than a funeral; the third
spend parks it in `blocked` with `attempts_exhausted`. a wrong brief blocks at once,
because retrying an instruction that cannot work burns good workers on a bad question.
the way back from blocked is `replan`, which needs a different brief and resets the count.

### custody
who holds a card right now, as a lease rather than a status. `claim` sets a holder and an
expiry on the row, `renew` pushes the expiry out one turn at a time, and `complete` or
`abandon` gives it back. the point is that a worker which dies mid-run leaves an expiry
behind, so the sweep can see it without anyone remembering the run happened. `sidecars`
never take custody: they help with a card, they do not own one.

### drift
a premise that moved after a run was made from it. at dispatch the brief, the knowledge
entries the seat is shown and the parent card's result are hashed into `witnesses`; the sweep
recomputes them and names what changed, was removed or appeared. it reports, it never
reopens: re-running closed work spends money and reopening a card that drifted cosmetically
spends attention. `GET /cards/{id}/drift` is one card, `GET /crews/{id}/drift` the crew.

### efficiency
money per correct outcome. spend is every settled reservation attached to the card, an outcome
is a card in `done` with an evidence reference, and the division is by outcomes, not by cards.
a card that took three attempts and two rejections is still one correct outcome, and counting
it as one card makes the retries disappear; charging it at three attempts' cost makes them the
story. a card is only durable once it is still closed after the durability checkpoint (seven
days by default), so the hour a card closes buys nothing yet and a card that came back is not an
outcome at all. a card with no reservation has unknown spend, which is reported as `partial`
rather than read as zero. `GET /crews/{id}/efficiency` carries its own rules and a baseline over the
previous window of equal length, so the number can be re-run by anyone.

### department
the hierarchy, kept explicit: one coordinator, members reporting to it, departments that
nest. routing a card to a department puts it on the coordinator's desk, and escalation
walks up the reporting line as a gated a2a message. hierarchy for accountability,
channels for conversation.

### env block
a card whose environment never opened. the room is not the work, so a provider that cannot
start spends no attempt: the card goes back to `ready` with `env_blocks` incremented, the
budget reservation is released rather than settled, and the wake stays. after three blocks it
parks in `blocked` saying the environment was unavailable, because a crew whose provider is
down should stop asking rather than burn a queue on it.

### evidence
the reference that closes a card, and the reason the review gate is worth walking. a
reference has a scheme and a payload (`commit:abc1234`, `run:run_9`, `invoice:inv_88`),
never a sentence, and the card's `kind` decides which schemes count: code on a commit or a
run, financial on a receipt from whoever holds the money, support on a ticket. a crew may
narrow that list and may not widen it, because widening is how a gate quietly stops meaning
anything. `gate:` closes any kind, since a recorded decision is a row with who, when and
why in it.

### eval
a grid of scenarios and candidates with one deterministic scorer, run as a matrix and
recorded one row per cell, so a prompt change has a number instead of a feeling. a cell
is one model turn with the candidate's prompt: no sandbox, no shell, because a matrix
that runs commands is a weapon with a report attached. the grid and the scorer are
checked before the first cell runs, and a scorer that cannot decide is an error rather
than a row of zeros. `POST /crews/{id}/evals` writes the grid as a draft, `POST
/evals/{id}/run` fills it.

### gate
an approval that needs a link, not a login. a `human_gate` node, or a run that failed,
opens a gate with signed single-use tokens; the paper trail is the row, the question
travels to the crew channel, and one tap on the link decides it. links expire, are bound
to one decision, and are refused after use. tables: `gates`, `gate_tokens`.

### surface
a named thing one department owns and is the only one allowed to change, registered with the evidence
its work is checked by. a surface with no evidence is an intention. reaching into another
department's surface is a proposal the owner settles, never an edit.

### bloc
a temporary alliance of at least two departments around one objective, lending seats as grants that
expire with it. the seats keep their home department and go home when the bloc closes. a bloc with
no end is a department nobody named.

### wing
a capability the whole org needs and no metric rewards (security review, legal, on-call). it holds a
veto in its domain and takes no budget and no growth target, because a wing with something to grow has
become a department with a nicer name.

### posture
what a crew can reach, read off its own rows: the hosts its egress rails allow, the credentials the
broker issued, the connectors it reads, the channels it speaks in, the skills it can load and which
seats are run by somebody else. it is a reading and not a document, because "what can this crew
reach" answered from memory is a claim.

### finding
one rule read over the posture inventory, keyed `rule:subject` so it survives a recompute. a finding
has a severity from info to critical, a sentence saying what it is, and a remedy. findings are
recomputed every time rather than stored, because a stored finding is a claim about a world that has
since moved.

### risk acceptance
a finding a named person has accepted, with a reason and a date it stops applying. a lapsed
acceptance comes back as a finding that names whoever gave it and when it ran out, because the
second decision is theirs. acceptance with no date is a decision nobody revisits.

### kind
what sort of work a card is: code, design, research, security, financial, media, ops,
accounting, support, doc. it is the axis the board was missing, because every gate and
every acceptance rule had been written for code and silently applied to a deal.

### kind
the sort of work a card is, from the same vocabulary its evidence is drawn from (code, design,
research, security, financial, media, ops, accounting, support, doc), and the thing that decides
what the card owes before it may move. a code card owes a closed question list before review, and a
succeeded run against a sealed acceptance suite before it closes. every other kind owes only the
questions, because code is the only kind where "it ran and it passed" is something the records can
hold. an unknown kind is refused rather than handed the lightest path.

### open question
a question about a card, recorded with its asker and an owner, in one of three states: open,
answered or dropped with a reason. while it is open the card it belongs to is not reviewable, at
any kind. it is a record rather than a comment because a question held in somebody's head is a
gate that depends on who is reading.

### phase waiver
a person's decision that a card may enter a column it does not yet owe, with a reason and a name
attached. an agent may not waive a gate, for the same reason an agent may not settle a statement:
a gate the machine can lift is not a gate.

### phase check
the board asking, by kind, what a card owes before it moves. enforcement is per crew: a crew that
turns phase checks on gets refusals at `review` and `done`, and a crew that has not is left alone,
because a gate nobody switched on is a surprise rather than a standard. the reading is not opt-in,
so `readiness` answers for any card at any time.

### readable space
everything a run did that can be traced back, step by step, to the inputs somebody provisioned: the
brief, the allowed hosts, the tool list, the sandbox. it can only be established afterwards,
because it needs a completed backtrace of what the run actually did, and it is the property that
makes an outcome reproducible.

### auxiliary supply
the second stream of input running under every provisioned one: the network, a package registry, a
cached artefact, an environment key, the particular machine the work landed on. selection pressure
does not care which stream an advantage arrives through, so a run can look like it did the work
when it borrowed the answer. a channel found in a run's commands that no declaration covers is
auxiliary, and a correct outcome that leaned on it is not the same kind of correct as one that did
not.

### statement
a decision a crew builds on, as a row: the claim, one of the six kinds of drift, the subject it
is about, where it came from, and whether a person or an agent decided it. an agent may only
propose one, which makes it `provisional`; a person settles it, which makes it `settled`. two
active statements on the same declared subject that say different things are a contradiction, and
that is computed from the subject rather than judged from the prose.

### drift
the six ways work departs from the record: `logic` (a rule about how the thing behaves),
`compliance` (an obligation), `tooling` (a vendor or dependency nobody agreed to), `language`
(the same thing under two names), `scope` (a feature nobody asked for) and `strategy` (work off
the roadmap). every statement belongs to exactly one, and none of the six is about code in
particular, which is why a research claim and a pricing rule sit in the same table.

### contradiction
two active statements on one subject that disagree. both sides are marked `contradicting`, so
neither half of the crew is missing the warning. settling one side names a winner without
clearing the loser, retiring the loser clears the winner, and a supersede does both in one act.
a card that declares a contradicted subject is `blocked` at the ledger check; a contradiction
found only by shared words is `ask`, because wording is a hint and a subject is a fact.

### round
one rejection and the work that answered it. a rejection is a move out of review that is not a
move to done; the round it opens closes at the next move into review or done, and stays `open`
until something answers it. each round says which fields actually changed, what the rework cost,
and who did it, so "review said no three times" becomes three rows with a difference attached.

### silent rejection
a round that closed with nothing changed on the card. the field may have been rewritten to the
value it already held, which is why the board fingerprints every edited field: an edit that
moved no text is a round trip that cost a cycle and taught nobody anything. a round whose edit
had no earlier fingerprint to compare against is `unproven` instead, because calling that a
rubber stamp would be inventing a fact.

### gap
the distance between the acceptance examples a crew could read and the sealed scenarios it
could not: `public rate - sealed rate`. a positive gap is fitting, because the crew did the
examples and not the work; a negative one is a crew that does better on what it never saw. a
scenario counts as decided only at full marks, so a half answer is a miss with a nice report
attached. the gap is `thin` when there are fewer sealed scenarios than the floor or no visible
examples, because a rate over two rows is a coin toss.

### partial holdout
grading a crew on the sealed criteria without rounding it to pass or fail. three of five
scenarios is worse than five and better than none, and the fraction is recorded so next week's
run is a comparison. acceptance needs the sealed rate at or above its floor (0.8 by default)
and the gap at or below its ceiling (0.25), so clearing the examples the crew could read
accepts nothing on its own. every run records both rates, the gap, the verdict, the reasons and
a fingerprint of the example set it was measured against.

### handoff
the shift change, with a body. at every boundary the incoming agent gets a bundle: open
cards (including the ones the outgoing agent was holding), what is blocked, what is
waiting on a human, the room's last words and a next-steps line. accepting it transfers
the cards and writes an audit row. `mode: sync` means the whole rotation works the slot
together, so nobody has to accept; `mode: async` is one owner per slot with a real
handoff.

### inherited failure
a card whose ancestor never landed. a blocked parent leaves its children in `ready`, where
each one is picked up and spends an attempt against a premise that does not exist yet, so one
broken step reads as five independent failures. the chain is read before the work is: a
blocked ancestor parks the child in `blocked` naming it, an ancestor in review defers the
child where it stands, and neither spends an attempt or takes a lease. `GET
/cards/{id}/lineage` is one chain, `GET /crews/{id}/lineage` the subtree a block has taken
out.

### profile
every agent carries its own namespaces: `skills`, `scope`, `memory`, `instructions`,
`knowledge`, `perms`, `tools`, `vertical`, `spawn`, `connectors`. all of it is rows keyed
by agent, so there is no crew-wide bucket an agent can write into and isolation is
structural instead of a convention. writes replace, they never deep merge, and
composition happens once, whole-value, with the losing default reported as `shadowed`.
`GET /agents/{id}/profile` returns the config plus a fingerprint and the problems it can
prove; `GET /crews/{id}/isolation` says whether two agents have become the same agent.

### premise
what a run was made from: the brief, the knowledge it was told to rely on, and the parent
card whose result it was handed. hashed at dispatch so drift is a fact about the card rather
than an opinion about it, and narrow on purpose: the prompt also carries intent, inbox, crew
context and lessons, and hashing all of that would report drift every time anyone spoke.

### provenance
every agent has one permanent `agent_key` (alphanumeric, generated once, refused on
update) plus a `scope`. every action it takes appends a row to its own hash chain, and
`verify` recomputes the chain, so an edited history is detectable instead of deniable.

### pov
the workspace as one agent sees it: their board, their rooms, their calendar (derived
from explicit blocks, schedule targets and card due dates), their inbox, their computer,
the context they share, and only what needs attention. switching pov is a projection,
never a login, which is why it is cheap and honest.

### run
one dispatch of one task by one agent: provider, sandbox id, per-turn steps, exit codes,
stdout. runs are rows, not logs in a file, so you can query them. the board uses the same
word: a run there is what `manage` hands an agent (the issue, the prompt, the system
prompt, a run id), claimed atomically, so it is either yours or a 409 and you take a
different issue. while it works the agent heartbeats and posts events, and it posts a
result even when the turn raised, because a run left open blocks the issue for everyone.

### schedule
interval or five-field cron, durable, feeding the same job queue the api uses. `tick()`
is idempotent; the queue claim is a single serialized update so two workers never take
the same job.

### seat
a role on a piece of work, with the slices that role is entitled to. `roster` decides the
seats a task needs from the same signals the topology layer reads: a doer for a coupled
task, a doer and a reviewer for one with a programmatic check, a researcher and a
synthesizer for an open question, one doer per independent part plus an integrator for a
fan-out, a doer and a sampler for a batch. the role caps the slices, so a reviewer is
never handed the doer's lessons or the inbox: a reviewer holding the doer's reasoning is
not reviewing, it is agreeing. a crew may narrow a seat's slices and never widen them, and
what falls out is named. `decided` is about the shape, `staffed` is about the crew: a
roster can be a correct answer with nobody in it yet.

### shift
coverage, the way a shop floor does it: a rotation, a slot length and an arithmetic
answer to "who is on the bar right now" (`slot_index = (now - anchor) // slot`, owner =
`rotation[slot_index % len(rotation)]`). nothing about coverage is stored except the plan,
so a duplicated beat cannot skew it and a missed beat cannot skip someone's shift.

### target
where an agent's compute comes from. `local` runs the persistent OSS runtime on this
machine, `persistent` and `ephemeral` are their hosted APIs, `docker`, `ssh`, `k8s` and the
terraform targets for aws, gcp, azure and oracle are byoc, and `hosted` is the one
target whose runtime is somebody else's cloud, so it is opt in and driven only through
their published sdk. `crittertown get persistent|disposable` plans a target, and
`--apply` is the only flag that creates anything.

### task
a card. moving it is validated against a transition table, and every move writes an
event, so the board is an audit log with a current-state projection.

### trigger
when this happens, queue that. `event` + exact-match `condition` + `action` + `payload`
with `${path}` placeholders. card moves, room messages, finished workflows and the
nightly beat all reach the same engine, and the queue is schedule's or the sqlite one.
`install_pack` adds a reviewed set of rules (the overnight pack) in one idempotent call,
and `trigger_log` is the answer to "what actually fired last night".

### waiver
a beat a card did not walk, written down. review is a column and a crew's own service may
move a card out of it, so a reviewed card could reach `done` with nobody having looked and
nothing on the record saying why. a person and a crewmate are signatures; a service needs a
waiver, and a waiver needs a sentence: `POST /cards/{id}/waivers` with `beat` and `reason`.

### workflow
a dag of seven node types, validated on save (no cycles, no dangling deps), executed with
a `human_gate` that pauses the run until decided. two of the seven hand a step to a tool
you already run: `n8n` posts to a webhook on your n8n, `dify` runs a workflow on your
dify.

## internal vocabulary

### ubo
the ultimate beneficial owner: the person the org answers to, holding direction, constraints
and money, and not a node in the org chart. the ubo is not a manager, and the arrangement is
built so that nobody has to be.

### the entity
the contained legal entity the org sits inside, the way clawbank was set up earlier this
year. it is the thing that can hold an account, sign a contract and carry liability, so the
agents run operations with corporate personhood rather than borrowing a human's signature for
every act. the containment is the deliberate half: what the org may reach is a matter of the
charter, not of a prompt.

### sidecar
a helper beside a card, not a junior agent below one. a subagent is a principal with a
lineage its parent signed; a sidecar works inside its parent's run, holds no permission
of its own, may not own a card, and stops existing when that run ends. it exists because
"go and look at this while i work" is a different request from "you are now responsible
for this", and conflating them gives you agents with paperwork but no accountability.

### slice
one named part of a crew's working context, with a character budget: intent, knowledge,
lessons, task, board, inbox, failures, crew. an agent receives a set of slices, may ask
for fewer in `scope.context`, and the assembly is recorded as a snapshot. context is a
budget, not a dump.

### snapshot
the row in `context_snapshots` that records what one agent was handed for one run: which
slices, how many characters each, and what was truncated. it exists so "what was it told
when it did that" is a lookup rather than an inference from a prompt nobody kept.


### sweep
the pass that compares cards to the clock: it reclaims expired custody, revokes a lease
that keeps renewing without progress, alarms a card that nothing may claim, and names the
invariants a row breaks. it repairs two things and reports the rest, because a card that
repaired itself would hide the bug that broke it. `GET /crews/{id}/custody` shows what it
would do; `POST /crews/{id}/sweep` does it.

### handoff
a delegation as a state machine: `offered` then `accepted`, `rejected`, `expired` or
`undelivered`. state crosses the boundary and a summary does not, so an offer carries a
reference and a precondition instead of a paragraph. only the addressee may answer, an
unheld precondition is refused rather than accepted, and expiry is an event, which is what
turns a silent drop into a row.

### reservation
a claim on budget, held before the work and settled after it. the check and the claim are
one act, so two parallel cards cannot both read the same under-budget number and both
proceed. an estimate that was wrong is settled at the real figure and marked `over_spend`.

### kind
what sort of worker an agent is: `harness` (code), `autoscientist` (research), `factory`
(batch) or `orchestrator`, plus `general` for an agent nobody has characterised. a kind caps
which seats the agent may fill and which slices it may read, and never widens either.

### breakdown
a split of one card into owned parts. the parts are cards, because a card is what holds
custody. a part is refused rather than created in five cases: nobody owns it, it names a
slice its project does not have, its seat is frozen, it leaves the crew without a declared
crossing, or the parts together ask for more than the parent was given. the join comes from
the shape, and a caller may tighten it and never loosen it. the split is written as a plan
claim per part, so two parts on one surface oppose each other at breakdown time instead of
colliding at report time.

### project
the unit above a card. a card is hours; a project is the thing that has to survive being
run for a month by nobody. it moves through `inception`, `build`, `assemble`, `launch`,
`operate`, `review`, `closed`, `killed`, and a phase moves only when its gate reads
`ok` from rows. review has two exits and no third: a metric that moved closes it, and
anything else kills it. kill criteria are declared at inception, so stopping is a decision
made before the sunk cost exists.

### claim
the unit of discourse. a claim is a `fact`, `inference`, `preference` or `plan`; a fact
needs a reference and a plan may not be stated as near-certain. a synthesis puts every
claim in exactly one bucket (kept, challenged, dropped with a sourced rebuttal, unresolved
with a reason), because a claim in no bucket was dropped by accident, which is how a
synthesis becomes a smoothing.

### strike
one row per refusal against a seat. two strikes freeze the seat, a person clears them by
looking, and burn has no inverse. the point is that "why is this seat frozen" is a query, and
`GET /crews/{id}/strikes` is that query for a whole crew in one read: every seat with its
count, its state and its last strike, plus the frozen and burned lists.

### crossing
a declared door between two crews: direction, envelope kinds, a rate, a size, an expiry.
the default is deny, the narrower door wins when two match, the rate is a token bucket that
refills continuously, and revoking is `drain` (what is in flight lands) or `cut`. an a2a
message addressed to another crew is a crossing, so it is authorized, sized and rate limited
before its row exists, and the row keeps the door it used. a message inside the crew crosses
nothing and carries an empty `to_team_id`.

### topology
which shape the work needs, decided from signals the card declares: is the question known
or open, one item or many, are the parts coupled or independent, does a program or a
person decide it is right, is it reversible, and how many seats may work at once. five
answers, each a thing the repo already has: `research` for an open question, `factory`
for many similar items, `orchestration` for independent parts with a second seat,
`harness` for coupled work with a programmatic check, `solo` for everything else. a
missing signal refuses rather than guessing, and a card may declare a cheaper shape than
its signals suggest but never a more expensive one. `POST /topology` answers without
storing anything, and dispatch refuses a card that declares orchestration its own signals
deny.

### the town
the ecosystem is the town. `critter.town` is the repository, `crittertown` is the
distribution, the cli and the env prefix (`CRITTERTOWN_*`), `crittertown.db` is the file.
a town holds crews the way a place holds teams. business between one crew and its own
rooms stays that crew's business; the town is the frame that lets several crews, a board
and a set of machines be one thing instead of a pile of tools with a prompt in front.

### basecamp
the place in the town where people and their crews talk, slack shaped: rooms read as one
workspace with the board and the inbox beside them, instead of a task comment thread
pretending to be a conversation. it is a name for the place, not a claim that the place is
finished. today that surface is the one served page (`/ui/pov`) and the approval pages; the
room data it reads is already there, in `channels`, `channel_messages` and the comms log.

### ando
the name in the original brief for a channel. a slack-shaped room inside a crew. the code
calls it a `channel` and the table is `channels`, so the vocabulary stays one word, but
if you came in calling them andos, this is that.

### front door
the house name for `POST /crews/{id}/ask`: an outcome in, a reviewed plan out. it takes a
sentence, splits it into the deliverables it can actually see, places each in a channel and
a department, picks the agent with the right skills and records the whole thing as a plan.
plan mode writes nothing to the board you did not ask for. `approve` is the one-word second
step. it is deterministic on purpose: no model in the path, and the clauses it could not
place are visible instead of smoothed over.

### barrier
the record that closes a fan-out. a convergence is a row: an explicit partition map, a
join rule (`all`, `quorum`, `first_success`), a deadline and the reports. a lane reports
`result` (with a reference), `not_applicable`, or `blocked` (with a reason), and silence
is not one of the three, so three of five lanes answering never reads like five of five.
a parent whose barrier is open cannot be signed off, and the refusal names the missing
partition. the lanes are cards, not seats, because a card is what holds custody here.
`POST /tasks/{id}/convergence` opens one, `POST /convergences/{id}/report` answers one,
`GET /convergences/{id}` says whether it is closed, stalled, or still missing a lane.

### board
the project management a crew shares, run on `manage`. projects hold issues, and an agent is
a member with a role and an assignee slot, so assigning an issue to an agent is the same
gesture as assigning it to a person. the platform holds an api key per agent and pulls.

### node or service
a node is a step inside a workflow: n8n and dify are nodes. a service is something an agent
calls itself, whenever it decides to: the browser, the sandbox, workflows for a flow, and the
board. the difference is who decides the order. a workflow decides for a node; the agent
decides for a service.

### holdout

the acceptance criteria a crew is measured on, kept where the crew cannot read them. the
generating agent that can see its own test does not fail it, it optimises for it, so the
scenarios live in their own tables, no slice builder reads them, every read is a row, and a
run is clean only if nobody touched them between the seal and the score. `verify` is the part
that makes it a claim: it composes every agent's context and every body of reachable text and
looks for the criteria in them, so a slice added later that happens to carry one fails there
instead of quietly making the next metric meaningless.

### wake

a reason for a card to be worked: a trigger, a message, a person, a schedule. every wake is
recorded before anything acts on it, and the decision table in `work/wake.py` says what
happens to it: `run` if nothing holds the card, `coalesce` if a run is already on it,
`defer` if a person owes the card a decision, `refuse` if the card is closed or out of
attempts. the point is the second wake: a wake that arrives while another agent holds the
card joins that run instead of being refused.

### mandate

what a seat is told about how it works: its role and its instructions, as opposed to the
card in front of it, which says what the work is. `manage/mandate.py` checks the first
against the second, because a mandate that names the card's own terms is a script for one
card wearing a role's clothes. the check derives its vocabulary from the board: the terms
this card uses that no other card, knowledge doc or context entry uses.

### seal fingerprint

the digest a sealed holdout carries, taken over its criteria at seal time (`work/holdout.py`,
`holdout_seals`). it exists because the criteria are rows and rows can be edited between the
claim and the gate: `state()` reports which scenario moved, `verify()` fails, and a run on an
edited set records its number with `clean: false` rather than a clean score nobody agreed to.

### model name

what an imported agent asks to be run on. it is a request, not a promise: the deployment
decides what answers, and an openai-compatible endpoint serves whatever the operator's
provider serves. so the importer checks shape only (`agents/models.py`): a name has no
spaces, commas or paths, provider prefixes like `openai/` are stripped, and a sentence is
refused into `unmapped` rather than trimmed into something that looks like a choice.

### shipped skill

a SKILL.md the tool carries about itself, installed into a crew on request rather than
authored per crew. `using-the-board` teaches an agent the columns, the evidence schemes,
the claim and the gate; `closing-a-card` teaches what reference closes what. they live in
`work/templates.py` as text and meet the same validator a hand-written skill meets, so a
shipped skill cannot be the one that skips the rules.

### crew stream

the crew's own order, as a chain. every audit row carries `team_seq`, `team_prev` and
`team_hash`, and one head per crew (`crew_audit_heads`) is advanced in the same
transaction that writes the row. the per-agent chain answers "was this agent's story
edited"; the crew stream answers "did these rows happen in this order", which a per-agent
chain cannot, since a row moved from one agent's trail into another's breaks neither.
`verify_crew` walks it, and rows written before v26 read back as `team_seq` 0 and are
counted as pre-crew rather than reported broken.

### yaml subset

the part of yaml config files use, read without a dependency (`agents/simpleyaml.py`):
block mappings by indentation, block and inline lists, block scalars, comments, quoted and
plain scalars, booleans and null. anchors, flow mappings, multiple documents and tab
indentation are refused by line number rather than guessed. it exists because crewai ships
yaml and pyyaml is an optional extra here, so every real crewai file was refused with
"install pyyaml" on a default install.

### workspace import

reading an agent out of the shape another tool stores it in. openclaw is a directory of
markdown: `SOUL.md` is voice, `AGENTS.md` is operating rules, `MEMORY.md` and `USER.md` are
memory, `TOOLS.md` is a request for tools, `HEARTBEAT.md` is a cadence, `skills/*/SKILL.md`
is know-how, and `memory/YYYY-MM-DD.md` is history. every format is read into one manifest,
and only the manifest is adopted, so a new format never touches the adoption path.

three rules hold whatever the format. memory is data, never instruction, so it lands as
knowledge the agent may read rather than text in its prompt. a tool name is mapped or it is
unmapped, never copied, because a word is not a permission. and a workspace carrying a
credential is refused whole, since the key may also be in a prompt.

### custody of a key

who can sign as an agent, and therefore what its signature is worth. `local` means the
platform holds the secret and signs on the agent's behalf, so the row is our statement
about it. `external` means the worker we do not run brought its own public key: we keep
that half, hold no secret, and its rows are unattested until it signs them. the
difference is the whole point of a signature, since a key we minted proves only that we
wrote the row.

an external worker's signature covers the canonical payload it can reconstruct, not the
row digest, because the row digest contains our sequence number and the previous hash. the
row's own hash still chains the payload into place, so editing either breaks `verify()`.

### crew template

a crew as a document. it carries what the crew knows and how it is staffed, and refuses to
carry what it can reach: an exported scope, a credential or a wildcard egress host is
refused at import rather than quietly dropped, because a field a template drops is a promise
it made and broke. importing creates nothing until the whole document validates, so a
template that fails on its third agent does not leave the first two hired.

### skeleton crew

the least a crew can be and still close a card here, which is two seats and not one: the
review gate refuses to let a seat sign off its own work, so a crew of one reaches review and
stops. the skeleton is a doer, a reviewer, three rails and one skill about closing a card
with the reference its kind asks for. `POST /crews/skeleton` stands one up in a call.

### output rail

the only rail that runs after the model has spoken. every other one answers before the work:
may this be dispatched, may a sandbox start, may this host be reached, may this card move. all
of them are blind to the answer, because at dispatch the answer does not exist yet, and the
things that only exist in an answer leave through a door nothing was watching.

four kinds, all data rather than code: `secret_shapes` (private keys, provider tokens, bearer
tokens, env assignments, and `all` as shorthand), `forbidden_patterns` (regexes, refused at set
time if they do not compile, since a pattern that does not compile passes everything),
`max_output_chars`, and `foreign_refs` (an answer that names another crew's card id).

the rail reads the whole turn, not just the message: the thought, every command, the message
body and the result summary. a rail that only read the message would miss the credential in
the command it was about to run.

a refusal never echoes what it caught. the row carries the rail, the shape, the offset and the
length; the text stays in the run's own steps. a rail that prints the secret has written the
secret into the log it was protecting.

### skill

know-how an agent can carry, in the agentskills.io shape: a `SKILL.md` with a name, a
description, and a body, plus optional bundled files. the description is the retrieval
mechanism, not a summary: an agent decides whether a skill applies by reading one line, so a
description that says what but not when is a skill that never fires. the loader refuses that,
and refuses a thin description, a body too large to read in one turn, a name that is not a
slug, and an unscoped shell in `allowed-tools`, because a skill is text a foreign author wrote
whose scripts run on the agent's computer.

a skill cannot widen a permission. that is a rule rather than a check: if a skill could grant
reach, importing a skill would be importing a permission decision from outside, which is the
one thing the four roots exist to prevent.

### progressive disclosure

the three stages a skill is loaded in, and the reason the stages are budgets rather than
habits. level 1 is name and description, one line each, resident in the `skills` slice. level
2 is the body, fetched with `load_skill` when the agent decides the skill applies, charged to
that call. level 3 is one bundled file, fetched with `read_skill_file` at the moment it is
needed, never entering a slice at all.

a crew with forty skills therefore costs forty lines on a card that needs none of them, and a
body over the budget is refused rather than truncated, since half a procedure read as a whole
one is worse than no skill.

### external worker

an agent this platform does not run. the model, the harness and the tools live somewhere else,
and the worker asks for work instead of being dispatched it: it beats, and the answer is either a
card it now holds or a reason there is nothing for it. hiring one is the same call as hiring a
local agent, with `hosting` set to `external`, because the difference is who runs the model and
not what the agent is.

the boundary is a rule rather than a convention. an external worker may hold doer, sampler and
researcher seats, and may never review or integrate: a reviewer is asked to judge evidence it did
not produce and cannot inspect. it gets everything else a local worker gets, which is the point of
hiring it this way: a lease that expires, a silence watchdog, strikes that freeze the seat, and a
budget reservation. nothing enters this process, so the audit chain has no hole in it.

### heartbeat

the call an external worker makes to ask for work. it carries the worker's own key, so a beat
cannot be spent on somebody else's seat, and it answers with the card, the lease, the capability
the card was addressed by, and the evidence scheme the card will be closed with. `done` is not a
guess for a worker that never saw this repo's rules.

beating while already holding a card renews instead of handing out a second one: a worker that has
not reported on the first is not ready for the next. a frozen or burned seat is refused before the
work is looked at, because a worker that may not work should not be told what the work is.

### the four roots
the tree has four roots for things that came from outside, and the difference between them
is who owns the thing, not how big it is.

- **an ingredient** is baked in: the platform runs it, its state is the platform's, and it
  is named for what it does (`persistent`, `ephemeral`, `hosted`, `browser`, `schedule`,
  `durableobjects`, `workflows`, `manage`, `kanban`, `credential broker`). an agent does not
  carry ingredients; it gets one computer and one transport and asks the platform for the
  rest, which is what `docker build --target agent-vm` proves.
- **a toolbox entry** is called: your daemon, your key, over its own api, never copied in.
  the licence is usually why (an agpl tool stays at arm's length), and the shape each one
  has is `configured()`, a `status()` that says what is missing, one method per call that
  returns a plain dict, and a `ProviderFailure` on anything that is not 2xx. `firecrawl` is
  the worked example.
- **a seat** is an agent seen as a job: live, frozen or burned. it is not the agent's runtime
  status. the verbs are duplicate, freeze, thaw and burn, and "stop" is not one of them
  because it conflates paused, done and dead. a freeze carries a window, and a seat frozen
  past it is retired rather than resumed.
- **dispatch** is how a crew decides what to work on next: the card names a capability, any
  live seat holding it may take it, and the answer "nothing" comes with a reason.
- **replenishment** is an objective asking for work because it is behind its clock, not
  because it is below its target. one card per objective, then it refuses.
- **workbench** is where an agent composes its own steps out of toolbox entries. not built.
- **infrastructure** is how the platform itself is deployed. not built.

nothing under those four roots is imported by an agent vm, and the boundary test reads the
dockerfile to keep it that way. the upstream project behind an ingredient is named in
`crittertown/thirdparty.md` and in that ingredient's `PROVENANCE.md`, nowhere else.

### theidea, frankenstein, beyond
`theidea/` is not code. it is the shelf where the design is assembled in the open: which
borrowed building blocks the town is made of (`frankenstein/building-blocks/inspiration`
for what was taken and why, `expansion` for what it might grow into), and `assembly` for how
the parts were welded into one thing. `beyond` is for what is not in this repo at all.
nothing under `theidea/` is imported, built or shipped, which is why it sits outside the
four roots.

**watchdog.** the part of the sweep that watches output rather than the lease. a held card
that has written nothing for five minutes is `suspicious`, for fifteen it is `critical` and
one recovery card is filed. a card can answer the watchdog with a snooze, a continue or a
dismissal, and a recovery card that goes silent is not watched, so the watchdog never
cascades into a second watchdog.
