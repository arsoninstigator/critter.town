# stack decisions

what each named piece of the brief turned into, and why. the short version: the
things that are hard to get right (sandbox lifecycle, the review gate, the
transition table, cron math, queue claiming) are implemented, and the things that
are vendor choices are behind one adapter interface.

every section below credits prior art by its own name. crittertown's vocabulary is
its own: crews, channels (andos), shifts and handoffs, not another product's terms.

## symphony (by wix) → the crew model

symphony sells a coordinated team of 5-ish agents for a small business, sharing
context, each owning a domain (outreach, marketing, scheduling, research, finance).
the transferable part is not the agents, it is the *shape*: one shared memory, one
board, specialists with narrow jobs.

built: `teams` + `agents` + `context_entries` + a per-crew kanban whose columns come
from the crew's own policy. a crew can be forced into `reviewed` mode, which changes
both the kanban handoff and the a2a gate.

## naïve (usenaive.ai) → primitives

naïve's pitch is an "autonomous company runtime": deploy AI employees, and every
team gets a runtime plus real-world capabilities. the useful framing is that an
agent is *runtime + capabilities + job*, and that the runtime is durable.

built: `runtime` json on each agent (`computer`, `provider`, `image`,
`timeout_seconds`), provisioned by `ensure_computer` and reused. capabilities are
`skills` + `tools` on the agent and `required_skills` on the task, matched by the
selector.

## persistent → persistent computers

an agent's computer is created once and kept: files, packages and browser profile
survive between tasks. that persistence is what makes "the writer remembers the
brief it wrote last week" real rather than prompt stuffing.

built: `sandbox/persistent.py` behind `SandboxProvider`, plus `computers` rows keyed by
agent. `LocalProvider` reproduces the same lifecycle on disk for development.

## ephemeral → ephemeral sandboxes

anything an agent might break runs in a throwaway box. task-level
`isolation: "ephemeral"` sends the run to ephemeral instead of the agent's computer, and
the sandbox is destroyed in a `finally` block whether the run succeeded or raised.

built: `sandbox/ephemeral.py`, `Handle.kind == "ephemeral"`, destroy-on-exit. the test
suite asserts the directory is gone after the run.

## zop.dev / schedule → scheduling

kept the semantics in sqlite (`schedules` + `job_queue`) and made the transport
swappable. `InProcBroker` is the default and needs no infra; `ScheduleBroker` pushes
job ids to an existing cluster. cron is a real 5-field matcher (`*/15`, ranges,
lists, dow) with a bounded search, not a cron-syntax library.

why not schedule beat directly: beat owns its own schedule store, which forks the
source of truth. one table, one truth, any number of workers.

## orchestrator.inc / a2a → the message bus

a2a here is not a protocol claim, it is a schema: `from`, `to`, `task`, `thread`,
`kind`, `body`, `status`, `reviewed_by`. the deliberate addition over "just send the
message" is the **gate**: in `reviewed` mode a message is `pending` and the
recipient's inbox does not include it. telemetry-style "you can read it later"
review is not review.

## vibe-kanban / agent-kanban → board semantics

took the parts that make agent boards usable and skipped the ui: per-crew columns,
a validated transition table, an append-only event log per card, and a `review`
column that is a gate in the table rather than a convention.

that last part was wrong in the first version and is worth writing down: the table
allowed `in_progress → done`, so review could be skipped, and `move()` believed
whatever actor string it was handed, which defaulted to `owner`. a gate that a card can
walk around is a column. it is enforced now, in three places: no edge into `done` from
anywhere but `review`, an actor that has to exist and belong to the crew, and a rule
that the assignee may not be the one who signs its own work off. `approve()` walks the
gate; nothing jumps it.

## n8n / workflows / dify → workflow definitions

the definition is `{nodes: [...]}` with five types and a `depends_on` graph. saving
validates duplicates, dangling deps, unknown types and cycles. execution is
topological, supports `${trigger.x}` / `${node.field}` templating, pauses on
`human_gate`, and resumes from the exact card that blocked it.

that json is the integration point: an n8n-style canvas renders and edits it, this
engine runs it. no lock-in in either direction.

## ando.so / relay (block) → agent-only channels

not wired as transports. what they imply is that agent conversation is a first-class
channel with its own retention and review story, so messages are their own table
with their own status lifecycle instead of being folded into task comments. swapping
in an external agent-messaging transport means implementing `send` and `inbox`.

## what is deliberately not here

- no frontend, no canvas, no auth provider. this is the backend the canvas will call.
- no llm in the orchestrator. planning stays cheap and reproducible; the model does
  work inside a run, it does not decide the graph.
- no vector store. shared context is key-value with full history, which is enough
  until a crew's context stops fitting in a prompt.

## spawn kinds: subagent and sidecar

the first version had one kind of spawn and called it a subagent. the owner pointed out
that a fleet has two useful shapes and only one of them is a subagent: sometimes you want
another principal with its own lane and its own record, and sometimes you want a second
pair of eyes inside the run you are already making.

the split is a column (`agent_spawns.relation`) and a set of refusals, not a naming
convention. a sidecar always names a card, is capped at two per card, gets no permissions
of its own, may not be handed a card to own (the runner refuses it at dispatch), and is
retired when its run ends, whichever way it ends. a subagent keeps everything it had.

the thing worth remembering is why this is a column: a sidecar that acquires authority is
a subagent with a new name, and a subagent that loses its lineage is an agent nobody can
account for. the schema is where that stays true.

## loops, and the engine that learns from them

a flat dag cannot say "try again". a node succeeds or the run fails, and a run that failed
on attempt two of five looks exactly like a run that failed on attempt one. so workflows
gained a `loop` node: a body of node ids, a ceiling (1 to 20, enforced at save time), and
a condition that ends the loop early. body nodes declare `"loop": "<id>"` and are skipped
by the main pass, so the loop owns them and a card is not dispatched twice by accident.
each attempt resets its body, because keeping the previous outcome would make attempt two
a continuation of attempt one, which is how a loop claims convergence it did not reach.
a human gate inside a loop is refused at save time: a loop that stops for a person is a
workflow with a person in it, and the gate belongs outside.

the improvement engine is on the other side of that node. an exhausted loop is the only
place in the tree that knows the work did not converge, which is the one thing worth
learning from it, so it files a lesson: proposed, attributed to the agent whose work fell
short (from the card, then from the run, then from the node's named agent), with the
workflow run named in the statement. an `improve` node does the same deliberately at a
point the author chooses. neither can promote anything: promotion is `manage/lessons.py`
and a person walks it. that asymmetry is the whole safety argument, because an engine that
writes its own instructions is the failure mode self improvement is meant to avoid.

## context as slices

before this, a run got whatever the prompt builder happened to concatenate, and the answer
to "what was this agent told" was a code path. now context is eight named slices with
character budgets (intent, knowledge, lessons, task, board, inbox, failures, crew), chosen
in a fixed order, with an agent able to ask for fewer via `scope.context` and nobody able
to ask for more than the budget. the ellipsis counts against the budget, because a slice
that reports 904 characters against a 900 budget is a budget that is not one.

every assembly writes a row in `context_snapshots` with the slice names, per slice counts
and what was truncated. that row is what the runs view shows and what a question about a
bad run is answered from. a prompt nobody kept is a claim about a run, not a record of it.

## the shell, checked in a browser

the page suite asserts two things: the shell renders, and every route it calls exists. both
were true while the shell was broken twice over, which is why a headless browser now runs
against a seeded database before a release.

it found three defects that no route check could see. the board's move control sent an
agent's name as the actor, and the move endpoint has taken ids since the review gate was
enforced, so every move 422'd and the card silently stayed put. the guardrails panel read
`{summary, limits}` while the endpoint answers `{guardrails, refused_today}`, so it rendered
"no limits" forever and the pause button, the only way to stand a limit down from the page,
was never on screen. and a hash change is a navigation even when the document stays, so a
deep link to `#workflows` pasted into the address bar went nowhere.

the last one produced a feature as well as a fix: views live in the hash and a workflow can
be linked by id, so a screenshot, a bookmark and a bug report can name the same screen.
the payload bug produced a test: each view has to read at least one key its endpoint
actually returns, checked against a live response rather than against a string in the page.

what the run verifies, in a browser, on a real database: a card moves and the api agrees it
moved, the editor opens a definition containing a loop and a gate, the loop workflow fails
with its iterations and files a lesson, the gate run parks and answering the gate settles it,
and the runs view shows the eight slices and their budgets. screenshots land beside the run.

## a card in someone's hands is a lease

a dispatched card used to be a column and a busy flag. if the worker died, the card stayed
in progress with nobody holding it, and telling a working worker from a dead one took a
person reading the board.

so custody is a lease with an expiry on the card itself: holder, expiry, renewal count, all
three on the row, with `next_action_at` pointing at the expiry. a holder that stops renewing
becomes visible by comparing the row to the clock, and nothing has to remember that the run
existed. the sweep then reclaims the card, or revokes a lease that renews without progress,
which is the same row seen twice.

the second half is what a failure means. a runtime failure is a spent attempt, so the card
returns to ready and runs again until the cap parks it. a wrong brief blocks at once,
because retrying an instruction that cannot work spends good holders on a bad question.
`replan` is the way back, and it refuses the same brief, refuses a held card, and resets
the attempt count only because the previous attempts answered a different question.

evidence arrives with the completion: `run:<id>` is written to the card, and the scheme
that decides which references can prove which kind of work is the next thing to land, so
that a commit is signed off with a commit and a deal with a receipt.

## the gate demands a reference, and the kind decides which

the review gate was real in one direction and decorative in the other. an agent could not
sign its own work, which was right, and a person could sign anything with a sentence, which
made the gate a formality with a witness.

so a card now carries a `kind`, and a kind decides which references close it: code on
`commit:` or `run:`, financial on `stripe:` or `payout:` or `invoice:`, research on a
`sheet:` or a `sql:` or a `source:`, support on a `ticket:`. the scheme is checked, the
payload is not, because the point is that a reference points at something a person can go
and look at, and a sentence does not. `gate:` closes any kind, since a recorded decision is
a row with who, when and why in it, and that is a pointer too.

a crew may narrow the list for its own kinds and may never widen it. the direction matters:
widening is the request an agent under pressure would make, and a narrowing that is not a
subset refuses the card rather than silently accepting more than the kind allows.

the short way to done walks the same check as the long way, because a gate with a bypass is
a suggestion. the runner writes `run:<id>` when it finishes, so a dispatched card has its
reference before anyone signs it, and a routine writes its receipt. when the shell closes a
card it asks for the reference rather than inventing one.

## a prompt change needs a number, not a feeling

the crew could tell you that a run failed. it could not tell you whether the crew had
got better, because a better answer and a different answer look the same in a log.

so an eval is a grid: scenarios down one side, candidates across the other, one
deterministic scorer for the whole matrix, one row per cell. the grid and the scorer are
validated before the first cell runs, and the row keeps the scorer, so a report still
says how it was judged after the code that judged it has moved on.

two boundaries are deliberate. a cell is one model turn, never a sandbox, because a
matrix that runs commands is a weapon with a report attached. and a scorer that cannot
decide is an error rather than a zero: a zero is a result, and a scorer with no
expectation has not produced one. the summary names a winner by mean, and a tie goes to
the candidate declared first, since the order a person wrote is a preference and an
alphabetical tiebreak invents one.

## the shape of the work is a decision table, not a preference

a crew that reaches for orchestration because orchestration sounds serious builds the
expensive thing for the cheap task. the four production shapes named in the mindstudio
piece and the autogen paper it links agree on the rule: start with the simplest topology,
because it extends with the least code.

so the shape is chosen from declared facts and nothing else. an open question is research,
many similar items are a factory, independent parts with a second seat are orchestration,
coupled work with a programmatic check is a harness, and everything else is solo. the
answer carries the one fact that decided it, so a disagreement is about a signal rather
than about taste.

two refusals keep it honest. a missing signal refuses instead of defaulting, because
guessing that work is coupled costs a crew its lanes and guessing the other way costs a
convergence nobody can close. and a card may declare a cheaper shape than its signals
justify but never a more expensive one, which is why dispatch refuses a card that declares
orchestration its own signals deny.

## a fan-out closes on a record

a parent that spawned children had no way to say what "the children are done" meant. the
run that spawned them returned, the parent waited on nothing, and three of five lanes
answering read exactly like five of five, because silence is not a result and nothing was
counting the silences.

so a convergence is a row: an explicit partition map, a join rule, a deadline and the
reports. a lane answers `result` with a reference, `not_applicable`, or `blocked` with a
reason, and the barrier is a pure function of those rows and the clock, so it says which
lane is missing instead of saying "not yet". a parent whose barrier is open cannot be
signed off, which is the part that makes the record load-bearing rather than decorative.

the lanes are cards and not seats, deliberately: a card is what holds custody here, so a
card is what a barrier can name and what a missing lane points at. `first_success` cancels
the lanes that have not reported, because a lane that loses silently keeps spending.

## a seat is a role with a slice list

two questions were being answered by accident. who a task gets was answered by dispatch's
score, which says who is free and close enough, not who the task needs. what each of them
reads was answered by whatever slices an agent had enabled, so a reviewer could be handed
the doer's lessons and a lane could be handed the whole board.

formation is a decision table over the same signals the topology layer reads, and it
answers in seats: a role, the slices that role is entitled to, and a sentence saying why
the seat exists. the role caps the slices at compose time, so the entitlement is enforced
where the prompt is built rather than where the roster is drawn. a crew may narrow and
never widen, and the narrowing is reported as withheld rather than dropped, because a seat
missing a slice it needs is a seat doing the job blind.

the split that keeps it honest: decided is about the shape, staffed is about the crew. a
roster with no agents is a correct answer to the wrong question, and it says so.


## delegation is three rules, not a message bus

a handoff carries a reference and a precondition, never a summary, and expiry is an event
so a silent drop becomes a row. an envelope names its authority and a hop limit, and a
delivery stops retrying before its ttl instead of spending budget to hand over something
the receiver must reject on arrival. authority is an intersection with a trace: the level
that dropped a tool is named, and a secondment spends nothing unless its scope says so.

## the four units above a card

the reference code keeps a project (phases, gates over rows, kill criteria declared at
inception), discourse (claims with kinds, a synthesis that accounts for every claim), strikes
(a count per refusal, two freezes a seat, burn has no inverse) and permeability (declared
crossings with direction, kinds, rate, expiry, drain or cut). they were the last pieces
missing above the card, and they are ported with the same rule as the rest: the decision is a
pure function, the service reads the rows, and nothing grades itself.

two adaptations: a project's slices are matched to cards by title or payload, because our
unit of work is a card rather than a tier's contract; and a strike is recorded against an
agent, because our seat is an agent. a project in `review` with no verdict is a gate that has
not opened, not a third exit.

## a split is scoped, funded, staffed and doored

the four units above a card each had a hole in the breakdown, so the breakdown now reads
them: slices come from the project and a part outside the set is not created, budgets split
down from the parent with silent parts taking an even share of what is left, a frozen seat is
not handed a part, a part assigned outside the crew needs a declared crossing, and the join
is the shape's unless the caller asked for a stricter one. the split also writes a plan claim
per part, which is how a duplicate surface becomes contested at breakdown time rather than a
conflict at report time.

## four verbs and no stop, a dispatcher, and an objective that asks

the last three organs of the org chart, ported from the reference's `domain/agents.ts` and
`jobs/{dispatch,replenish}.ts`, and the three places the runtime now reads the units built
before them.

a seat is live, frozen or burned. "stop" is not a verb because it conflates paused, done and
dead. freezing is cheap and reversible, carries a window, releases every lease the seat held
and closes its open handoffs as undelivered, because a frozen seat holding a lease is an
outage nobody has been told about. thawing past the window refuses with
`frozen_window_expired` and says to spawn against the current ledger: state older than a
fortnight is archaeology, since the tools moved and the lease is somebody else's. burning
needs a reason of at least three characters, is terminal, and orphans the children, which
finish what they hold and never multiply again. duplication narrows skills and stops at depth
three, because a child that could do more than its parent is an escalation, and escalation is
a person's act, never a spawn parameter.

idleness is answered rather than moralised about: a seat quiet past the crew's declared
`idle_freeze_days` is frozen (off by default, since a quiet crew is not a broken one), and a
seat left frozen past its window is retired by the same sweep with the window in the reason.
firing after idle time is the end of that arc, not its beginning.

dispatch decides what gets worked on next and refuses with a reason: `nothing_ready`,
`kind_unregistered` (a card whose kind has no evidence rule could never be marked done, so
handing it out manufactures work that cannot finish), `no_seat_for_slice`, `all_seats_leased`.
work is addressed by capability, not by name: the card names its slice and any live seat
holding it may take it, which is why a dead seat stops the holder rather than the work. the
claim is 15 minutes and happens at the moment of the decision, so two ticks cannot hand out
the same card.

an objective is a metric with a baseline, a target and a deadline, measured from outside, and
behind means behind the clock rather than behind the target: `done < elapsed * 0.85`, with
`observed_since` fixed at the first measurement because measuring from the latest collapses
the period and makes every objective look ahead of itself. one card per objective, a wip limit
of two regardless of seats, and six refusals that are the safety property: a machine that can
invent work will invent infinite work unless something stops it.

the three wirings: a guardrail refusal records a strike against the seat that earned it, so a
limit is counted rather than described; a handoff that leaves the crew needs a declared
crossing of kind `request` and a live receiver; and the morning brief carries each open
project's unmet gate checks, so a stalled phase is a line in the brief rather than something a
person has to notice on a board.

## the watchdog reads output, not the lease

a lease that renews is not evidence that work is happening. the sweep's existing signal for
this was the renewal counter, and it is too slow to be useful: with a fifteen minute lease,
three renewals is forty five minutes in which a hung model looks exactly like a working one.
so output is recorded as its own fact. `last_output_at` and `output_seq` are written by the
holder on every turn, `lease_started_at` is written at claim, and the silence is measured
from `last_output_at`, then `lease_started_at`, then the card's creation. a card that has
never produced anything is measured from when it became work.

five minutes is `suspicious` and fifteen is `critical`. suspicion is a finding and files
nothing, because an alarm that acts on a hunch teaches people to mute it. critical files one
recovery card in the same crew, pointing back with `watchdog_of`, and the second sweep finds
that card already open instead of filing a second one.

three rules keep it from becoming noise. it only applies to a card somebody holds, because a
quiet backlog is the point of a backlog. a person can answer with a row: `snooze` (must say
until when, because an open ended snooze is a mute with a friendlier name), `continue` (buys
the thirty minute rearm window, not forever), or `dismissed_false_positive`. and only the
board or the agent the recovery card was assigned to may answer, because a stranger answering
for a silent run is how a watchdog gets silenced by the thing it is watching.

a recovery card that goes silent files nothing. a cascade is how a watchdog becomes the
outage. the same rule covers a card that is blocked, whose silence is the durable waiting
signal the column already carries, and a card that reached done with evidence newer than the
silence, which is late rather than stuck.

## the lifecycle is a loop of scoped builds, and operation is maintenance

the project had one pass: inception, build, assemble, launch, operate, review, then closed or
killed. that is the lifecycle of a launch, not of a business. a thing that works at all is v0.
a thing that survives people using it is v1. a thing that adds a market, a surface or another
revenue stream is v2 and after. none of those are the same project ending and a new one
starting, and treating them that way makes the review that should have opened the next build
look like the death of the last one.

so review has a third exit. a moved metric with a named next scope returns the project to
build with `version` incremented, and the build that just ended is appended to `builds` with
its version, its verdict, the metric it was measured on and the reason the next one exists. a
next build needs both halves: a moved metric, because growth on a basis that did not hold is a
killed project wearing a version number, and a scope longer than a label, because "v2" is not
something anyone can review.

what sits between builds is operation, and operation is maintenance rather than a phase that
ends. the routines keep firing, the metric keeps being sampled daily, and the operate gate
already refuses to move a project that has not held a month of that. a project that only moves
while somebody is building it is a project that rots the moment the build ends, which is why
the cadence lives in routines and the samples are a gate rather than a dashboard.

## a message that leaves the crew is a crossing

a2a had one rule about crews: a sender and a recipient in different crews was refused outright.
that is safe and useless, because two crews working on one thing have to talk, and the only
alternatives were a shared crew (no wall) or a person relaying (a person in the loop).

so the wall decides instead of the message layer. when the recipient belongs to another crew,
the message is mapped to the crossing envelope vocabulary (`note` is a finding, `handoff` a
proposal, `result` a receipt, `blocker` a signal, the two request kinds are requests) and
authorized against a declared crossing: direction, kind, size, rate, expiry. the default is
still deny, so an undeclared pair refuses with the wall's own reason rather than a generic
"not allowed", which is the difference between a refusal someone can fix and one they cannot.

three details are deliberate. the size and the rate are checked before the row is written, so a
refused message leaves nothing to clean up and nothing to explain later. the row keeps
`to_team_id`, `crossing_id` and `envelope_kind`, because "which door did this cross" is the
question an audit asks and it cannot be inferred once the door has expired. and a message
inside the crew stores all three empty, because a message that went nowhere should not look
like it went somewhere.

## two units came out of the api and onto a page

projects and strikes were complete and unreachable. a phase moved, a gate held and a seat froze
by calling the api directly, which means the only people who could see either were people who
already knew to ask. a unit that cannot be seen is a unit that is not in the loop.

so the shell gained two views. projects lists each one with its phase, its version, the last
three builds with the verdict and the metric each was measured on, and the gate it is standing
at, read before the advance button is offered: a button that always refuses is worse than no
button. the kill action asks for a declared criterion and a reason, because both are refused by
the api when they are missing and a prompt is cheaper than a 422. strikes lists every seat in
the crew with its count, its state and its last strike, and clearing asks who is clearing,
since a cleared row with no name on it is a shrug.

one route was missing rather than one page: standing was per agent, so a crew view would have
been n calls. `GET /crews/{id}/strikes` answers for every seat in one read, and names the
frozen and burned lists so a page does not have to re-derive the rule from the counts.

## the door a foreign agent knocks on

**decided.** an agent built somewhere else is hired by a heartbeat, not by an adapter.

the field offers two ways in. an adapter, the way crewai's bring-your-own-agent does it, buys real
fidelity and costs a compatibility class per framework per version, plus that framework's
dependencies inside this process. a heartbeat costs one route and keeps the foreign worker outside
the process entirely: it asks for a card, holds it under the same lease, reports output through the
same route, and completes with the same evidence. paperclip's version of the rule is that if
something can receive a heartbeat it can be hired, and the reason to prefer it here is that the
audit chain stays whole: every action a foreign worker takes is still a row.

so `hosting` is a column, not a feature. `external` means something else runs the model and polls;
`local` means the platform drives it and the tick dispatches it. the same create-agent call hires
both, because the difference is who runs the model, not what the agent is.

the boundary is written in `agents/external.py` as a rule: an external worker may hold doer,
sampler and researcher seats, and may never review or integrate. the refusal is not distrust of
the worker, it is the same rule the review gate already applies to the assignee: a reviewer has to
be someone who can be wrong about this card. a worker whose turn we cannot see cannot sign one
off, and the roster will not seat it in a judging seat in the first place, so the refusal arrives
before the work rather than after it.

the heartbeat answers with more than an id. it carries the lease, the capability the card was
addressed by, and the evidence scheme that will close the card, because a worker that never read
this repo's rules should not have to guess what "done" means. beating while holding renews rather
than double dispatching, and a frozen or burned seat is refused before the work is described: a
worker that may not work should not be told what the work is.

one wart was fixed on the way through. `add_agent` reported every insert failure as "agent already
exists", which turned a missing column into a duplicate name and cost a debugging round. it now
catches only an integrity violation, because an error that lies about its cause is worse than one
that is vague.

## a skill is loaded in three stages or it is not loaded

**decided.** skills follow the agentskills.io `SKILL.md` contract, stored as rows with their
bundled files beside them, and loaded in three stages whose budgets are enforced.

the format is worth adopting rather than inventing because it is already what other tools
export and because it is small enough to be a contract: a name, a description, a body, optional
allowed-tools. the description is the whole retrieval mechanism, so the loader refuses one that
does not say when to use the skill. a description that only says what the skill does is a skill
that never fires, and a skill that never fires is a file nobody reads. thin, absent and
when-less descriptions are all refused for that reason, which is stricter than the spec and
deliberately so: the spec's own guidance is the rule, and a rule that is only guidance is one
nobody follows.

progressive disclosure became a slice rather than a promise. level 1 is one line per skill in
the `skills` slice, budgeted at 500 characters and reporting how many did not fit. level 2 is
`load_skill`, which refuses a body over its budget instead of truncating it. level 3 is
`read_skill_file`, which never enters a slice at all. the alternative, pasting every body into
the prompt, spends the card's context on the forty skills it does not need, and an agent that
read none of it is not better informed, it is less.

the security rule is the part that had to be a rule and not a check. a skill may not widen a
permission: if it could, importing a skill would be importing a permission decision from
outside, which is exactly what the four roots and the crossing wall exist to prevent. so an
unscoped `bash` in allowed-tools is refused, a bundled path may not climb out of the skill, and
bundled scripts run on the agent's own computer, under the same rails as any other call.

one more thing is refused by name: a skill with `disable-model-invocation: true` and
`user-invocable: false` is unreachable by both routes, so it is a file rather than a skill and
the loader says so.

## the rail that runs after the model speaks

**decided.** guardrails stay data, and the one rail that cannot be pre-dispatch is a row like
the rest.

the guardrail module has said since it was written that `check` is the only entry point, because
a limit enforced in one path and forgotten in another is not a limit. that argument survives
here: the output rail is `check_output` and `enforce_output` beside `check` and `enforce`, over
the same table, with the same `agent_id` scoping, the same strike on refusal and the same audit
event. what is new is the action, `output`, and the four kinds that only make sense on it.

why it had to exist. dispatch, sandbox, egress and card-move all answer before the work, which
means none of them can see an answer: a credential pasted into a message, another crew's card
quoted back, or a turn that says more than the card is worth are all invisible until the text
exists. the crawled frameworks agree on the taxonomy and disagree on coverage, and the one thing
they agree on is that agent-level rails fire only at chain endpoints, which is why the tool rail
is separate from the input rail. this is the same argument one layer further out: the endpoint
of a turn is the answer, so the answer needs its own check.

three decisions inside it are worth naming. the rail reads the whole turn, not the message,
because the command is where a credential most often appears and a rail that skips it is a rail
that misses the case it was built for. the refusal never echoes the match, because a rail that
prints the secret has written the secret into the log it protects; the row keeps the shape, the
offset and the length, which is enough to find the line in the run's steps. and a forbidden
pattern that does not compile is refused when the rail is set rather than when it fires, because
a regex with a typo passes everything and looks exactly like a rail that is working.

the foreign_refs rail is deliberately narrow. it refuses an answer that names a card id from
another crew, and it does not try to judge whether the answer leaked the idea behind the card.
a rail that guesses is a rail whose refusals have to be argued with at 3am, and the whole point
of a guardrail over a gate is that nobody has to be awake for it.

## a crew is a document, and the smallest one is two seats

two things were missing and they turned out to be the same thing. a crew could not be handed
to anybody, because it only existed as rows in one database, and a crew could not be started
without answering eight questions first. a document fixes both: export what a crew knows and
how it is staffed, import it into a new one, and ship a document that is already the answer
to the eight questions.

the document is deliberately smaller than the crew. it carries names, roles, kinds, models,
prompts, skills, tools, and the rails. it does not carry the scope, because a template that
ships permissions is not a template, it is a copy of somebody's authority with a new name on
it, and it does not carry a credential for the same reason. a wildcard egress host is refused
too: a wall a template can open is not a wall. the rule behind all three is that an import
may narrow what a crew can reach and never widen it, so a refusal names the field instead of
dropping it, since a dropped field reads as a promise the template kept.

the skeleton is where that rule pays off. the obvious skeleton is one agent, and it is wrong,
because the review gate refuses to let a seat sign off its own work: a crew of one works a
card to review and stops there, and the first thing a person would conclude is that the
platform is broken. the smallest crew that can finish a card is a doer and a reviewer, so the
skeleton is those two, three rails and one skill that says how to close a card with the
reference its kind asks for. `POST /crews/skeleton` is now the shortest honest answer to
"how do i start".

## the holdout wall: measure a crew on what it cannot read

the last of the four research fronts, and the one that decides whether any of the others can
be trusted. michaellaws/dark-factory has the sharpest statement of it: the generating agent
works in a sparse checkout where the holdout directory does not exist, so it cannot read the
test it will be measured on, and a separate process runs that test and merges on the number.
without that wall an agent that is told to improve its score will improve its score.

there is no filesystem here to leave a directory out of, so the wall is three rules. the
scenarios live in their own tables and no slice builder reads them, which is structural, and
`verify` proves it rather than trusting it: it composes every agent's context and every body
of reachable text and looks for the criteria inside them, so a slice added in six months that
happens to carry a scenario fails there instead of quietly making the next metric
meaningless. a read is a row: the eval runner reads as `eval`, and anything presenting itself
as an agent is refused by name and logged, because an attempt is the interesting half of a
wall. and a run is clean only if nobody touched the scenarios between the seal and the score,
which is why a tainted run still records its number and says so.

the seal refuses a crew that can already read its own criteria. that refusal is the one that
matters most in practice, because the usual way a holdout leaks is not an attack: somebody
pastes the acceptance criteria into a skill so the agents know what good looks like, and from
then on the number is a compliment.

## whose signature is on an external worker's row

the external-agent unit opened a hole in the trail that nobody could see from the outside.
`add_agent` minted a keypair for every agent it created, and the audit chain signed each
row with the key belonging to the agent the row named. for an agent we run that is exactly
right: the signature says the platform's own custody wrote this, which is what it means. for
a worker we do not run it says something false, because we made the key. every row
attributed to that worker carried a signature we produced, and a reader had no way to tell
it apart from testimony.

the fix is not a flag on the row, it is custody. `comms_identities.custody` already existed
and already read `local`; it now has an `external` value that means the platform holds the
public half and no secret. `KeyStore.for_agent` returns nothing for such an agent, and
`_mint` refuses one outright, because that function is the only place a forged signature
could be born. the worker registers its own key with `POST /agents/{id}/identity`.

that made one thing impossible and it is worth being precise about. we used to sign a digest
over the whole row, which is the strongest version: sequence number, previous hash, action,
payload, all of it. a worker outside this process cannot compute that digest, because the
sequence number and the previous hash are ours. so a worker signs its canonical payload
instead, and the row carries `attests: payload` to say which digest the signature covers.
the row's own hash still chains that payload into place, so editing the payload breaks the
chain and forging the signature fails against the registered key. it is a weaker statement
than ours, honestly labelled, rather than an equally strong one that is not true.

two refusals carry the reasoning. a rekey is refused rather than replaced, since a principal
that can change its key on request can deny what it signed yesterday. and an unsigned beat
is recorded as unsigned: a row that says nothing about who made it is still a row, and
dressing it up is the failure this path exists to prevent.

## what an imported workspace is allowed to become

the importer existed before the workspace was tested against a real one, and the difference
was four silences. a skill arrived as a directory name with no body behind it, so the agent
carried a label for know-how it did not have. a `TOOLS.md` became a knowledge entry, which
is the wrong drawer and also the wrong idea, since prose is not a permission. a
`HEARTBEAT.md` became knowledge too, turning a cadence into a paragraph. and a workspace
shipping a key was imported without a word.

the fixes follow one rule each. a skill is validated by the same rules a hand written one
meets and imported as the `SKILL.md` it was, because a name on a profile with no body is
worse than nothing. a tool name is mapped or reported, never copied, since `bash` in another
tool means a specific thing there and granting the word here would look like a permission
without being one. a cadence is a crew decision, so a heartbeat file becomes an unmapped
item rather than knowledge. and a workspace carrying a credential is refused whole, because
the key may also sit inside a prompt, and quietly dropping the one file it was found in
teaches nobody anything.

the part worth keeping is the one that was already right: memory arrives as knowledge. text
we did not write is data an agent may read and weigh, never instruction it must follow, and
an imported `MEMORY.md` in a prompt would be somebody else's orders wearing this crew's name.

## the interleaving is part of the evidence

the trail had one chain per agent, and that answers a narrower question than it looked
like it did. it proves an agent's own rows were not edited relative to each other. it
cannot prove that agent a's row came before agent b's, because a row lifted out of one
chain and dropped into the other leaves both chains internally consistent. when the only
witnesses are agents, the order between them is exactly the thing a person will want to
argue about.

so there is a second chain: one head per crew, advanced in the same transaction as the row
it covers, over the per-agent digest plus the agent id and the crew position. the two
chains fail differently on purpose. edit a row's content and the agent chain names the
sequence; move a row between agents or delete one and the crew chain names the position.
a test for each, since a check that only fails one way is a check nobody trusts.

rows written before this read back as crew position zero and are counted as pre-crew. a
migration that rewrote old rows to look like new ones would make the chain claim a history
it did not witness.

and the export is jsonl as well as json. an attestation is only worth something if
somebody else can check it, and a bundle that needs our parser to read is a bundle that
needs our word for it. one object per line survives grep, diff, wc and a spreadsheet, and
`POST /crews/{id}/audit/verify` checks it with nothing but the text.

## a foreign tool name is not a permission

the crewai and openai readers had never been run against a real file. both were doing the
same wrong thing in different places: passing a foreign tool name through as a local one.
crewai ships tool classes, so an imported agent arrived holding `SerperDevTool`, and openai
ships function tools, so it arrived holding `archive`. neither exists here. the profile
then showed a tool that granted nothing while reading like a grant, which is the exact
failure the openclaw workspace taught us to refuse.

so both now map or report. crewai class names go through a table, an openai function tool
is `tool.function.<name>` unless its name is already one of ours, and anything unknown
lands in `unmapped` where a person sees it. the same rule as the workspace importer, in a
second place, because the first place was not a rule yet, only a habit.

the second finding is worse than it looks. crewai configs are yaml, pyyaml is an optional
extra here, so the crewai reader refused every real file on a default install with
"install pyyaml". an importer that cannot read the format its framework ships is not an
importer. `agents/simpleyaml.py` reads the subset those files use and refuses anchors, flow
mappings, multiple documents and tab indentation by line number. a config reader that
silently turns an anchor into a string is worse than one that says it cannot read line 12.

and a small thing the test caught: crewai keeps the agent's identity in `role`, not in
prose, so the imported prompt had a backstory and a goal but never said who the agent was.
`## role` now leads the prompt. a test written against a real file finds that in a way no
test written against our own shape would.

## the tool ships a skill about itself

pad ships an agent skill beside its CLI and web ui, and that is the one thing worth taking
from it. our board has a REST surface, a custody lease, an evidence gate and a crew stream,
and an agent learns all four by being refused at them. a skill is cheaper than four
refusals and arrives before the mistake.

so `using-the-board` ships in `work/templates.py` as text, alongside `closing-a-card`, and
`POST /crews/{id}/skills/shipped` installs it. three rules held. it goes through the same
validator a written skill meets, so the shipped one cannot be the file that skips the
rules. it names what the code actually accepts (the six columns, the schemes per kind, the
gate), and a test reads the columns from the board rather than trusting the prose. and it
carries no paths or route strings, because a skill that names `/tasks/{id}/claim` is a
skill that goes stale the first time a route moves, and the agent then believes a 404 is a
refusal.

## a card is testimony, not instruction

the a2a reader was the last import path never run against a real card, and it was doing
the opposite of the openclaw and crewai readers in one place and the same wrong thing in
another. it copied every skill id onto our agent's profile, so a stranger's card decided
which capabilities our agent appeared to have. and it stuffed the card's blurb into `role`,
truncated at two hundred characters, while `instructions` stayed empty, so the one
paragraph a card actually gives you was thrown away and the sentence kept was cut mid-word.

both are fixed by deciding what a card is. it is testimony: somebody describing an agent
they run. the description becomes the instructions with a header saying it is their
account and not an order, the role is its first sentence, the skills are reported as
`a2a.skill:<id>` and never adopted, and each skill's description is kept as knowledge that
begins "the remote agent claims", with the card's url as its source. the security and
transport keys stay unmapped, and a card carrying a credential is refused outright,
because a card is served in public and a key in one is a key that has already leaked.

## a model name is a request, not a promise

the importers put whatever the source said into the model column, so a crewai
`llm: gpt-4o, best for writing` became a value a vendor was asked to serve. the obvious
fix is an allowlist of models we can run, and it would be wrong: this platform talks to an
openai-compatible endpoint, so the list belongs to whoever's provider is behind it. an
allowlist here would refuse names that work and bless names that do not, and a refusal is
harder to argue with than a warning.

so the check is shape. a name has no spaces, commas, paths or urls, provider prefixes are
stripped because `openai/gpt-4o` and `anthropic/claude-sonnet-4` are how other frameworks
spell two names we already know, and a sentence is refused into `unmapped` rather than
trimmed to its first token. trimming would have hidden the part the source was telling us.

the half that mattered more: a name this deployment does not serve is kept, recorded in the
`import-source` profile entry, and explained in one sentence naming both values. dropping it
would make the import look clean and leave the operator's mismatch for the first turn to
discover, which is the failure this whole import path exists to move earlier.

## the seal carries a fingerprint, because criteria are rows

reading the band dark factory submissions on github turned up one mechanism we did not have
and one we had half of. the one we did not have, from the DarkestFactory entry: the examiner
writes a sealed acceptance suite from the specification alone and registers it by hash before
any work is accepted. our wall hid the criteria and refused re-sealing, which stops a swap by
the front door. it does not stop an edit to the rows, and an edited row is worse than a
replaced one: the crew still cannot read it, so the wall looks intact, and the metric now
measures something nobody agreed to.

so a seal writes two fingerprints. one over the whole set, kept in `holdout_seals`, which
answers "is this the criteria that were promised". one per scenario, kept on the row, which
answers "which one moved". `state()` names the rows, `verify()` fails when any moved, and
`run()` records the number with `clean: false` instead of pretending. the digest leaves out
the timestamp on purpose: re-sealing identical criteria is the same commitment, and a clock in
the hash would make an honest re-seal look like a new one.

the half we had: `already_sealed` refused a second seal, which is the same instinct reaching
only the obvious door. the general rule from that submission is that a claim about criteria
has to be checkable by whoever is being measured, and a promise is not checkable.

## a mandate names how a seat works, and the check derives its vocabulary from the board

band's harness scans every seat mandate against a list of the problem's distinctive
identifiers, and a match disqualifies the entry: a mandate that names the answer is a script
for one problem wearing a role's clothes. their shapes are the part worth keeping, and they
are kept exactly: an endpoint path, a snake_case identifier and a kebab-case test id are the
three ways a specific answer shows up in prose, and prose, numbers and quoted fragments are
not evidence of anything.

the vocabulary is the part that could not come across. theirs is generated from the specs,
which exist for a tournament and do not exist here. an allowlist of banned words would be a
list of our own guesses, and it would fail a seat for using a word the board taught it. so
the vocabulary is derived per card: the terms this card uses that no other card, knowledge
doc, or context entry uses. a term in any of those is a term every seat may legitimately
know, which is what "generic" actually means here.

the other difference is what a finding does. their check disqualifies, because a tournament
has to rank. here a finding is recorded against the card and the run goes ahead: a false
positive on a word must not cost a dispatch, and one card's finding is worth less than the
pattern across ten. `strict=True` keeps the refusal for a crew that asks for it.

the limit worth naming: on a board with one card and no knowledge, nothing else exists to
compare against, so the check falls back to endpoint paths only and says so. a thin board
gets a thin check rather than a loud wrong one.

## a wake joins the run already on the card, and nothing about it is dropped

the second wake was the bug. an agent's message or a trigger arrived while another agent
held the card, custody answered `Conflict: not_holder`, and the reason to work was gone.
nobody had recorded that someone had asked, so the only way back was a person noticing.

paperclip's wake queue states the rule the other way round, and it is right: a wake runs the
card, joins the run already on it, or waits behind it, and refusing is reserved for a card
that genuinely cannot be woken. four decisions here, each a fact about the card row rather
than a guess about intent, so the table is a pure function and can be argued with: closed or
blocked refuses with the column named, review defers because a person owes the decision,
backlog refuses because an unscheduled card is not woken by a trigger, exhausted refuses, a
live holder coalesces, the holder asking again is a replay, and otherwise it runs.

two things make coalescing safe rather than a shrug. the queue reuses the pending wake for a
card instead of adding a second, so one card has one reason to work no matter how many
triggers fire. and when a run ends, the wakes that joined it go back to pending: the run had
no way to know it had absorbed them, so the next admission decides them on the card's merits
and records the refusal if that is the answer. a deferral is revived by `dispatch_ready` once
the card leaves review, because a deferral nobody revives is a drop with better manners.

the alternative we rejected was making the conflict retryable at the caller. that puts the
same decision in every trigger, schedule and message handler, and the handler that forgets is
the one that loses the wake.

## what a card was made from, and what it skipped

three things were silent in the same way. a card could close against a brief, a knowledge
entry or a parent result that had changed since the run read it; a reviewed card could leave
review on a service's move with nothing on the record; and a sandbox that never started cost
the card an attempt, so an outage looked like the work failing.

the witness answers the first. hashing the premises at dispatch makes drift a fact rather
than a suspicion, and the sweep reports instead of reopening, because re-running closed work
spends money and reopening a cosmetic drift spends attention. what is hashed is deliberately
narrow: the brief, the knowledge the seat is shown, the parent result. hashing the whole
prompt would report drift every time anyone spoke, which is the same as reporting nothing.

the waiver answers the second. the rule is not "review always", because a one line copy fix
does not need a reviewer's hour; the rule is that skipping a beat is a decision someone
wrote down. a person and a crewmate are signatures, a service needs a sentence, and the
reason has a floor, because "n/a" is the silence with a word in it.

the env block answers the third by separating the room from the work. the alternative was to
treat a dead provider as a runtime failure, which is what the old code did and what made an
outage look like three bad attempts on a good card.

## a failure is charged to the card that owns it

band's factory charges a stage that did not start to every stage above it, which is the same
insight from the other end of the pipe: one broken step should read as one failure. here the
children of a blocked card were picked up one by one, each spending an attempt against a
premise that did not exist, and the board ended up with five blocked cards where one card had
the problem.

the chain is read before the work is. what made this cheap is that nothing new has to be
stored: `parent_id` already says who the parent is, and `blocked_reason` already has a column
to say why. what made it necessary is the attempt counter, which is the one number a crew is
judged on, and it was being spent on other cards' failures.

the alternative we rejected was leaving the children in `ready` and letting the dispatcher
notice at failure time. by then the lease is taken, the reservation is held, the run row
exists and the attempt is spent, so the child has already been charged for its parent.

## a correct outcome is the unit, and the baseline is our own earlier window

every dark factory submission that measured anything measured money per correct outcome, and the
ones that measured volume were measuring the wrong thing in a way that looked like success. the
trap is subtle: a crew that retries more closes more cards, because retries are how a card gets
closed, so cards per day rises while the work gets worse. one dossier cited 8.2 units per
correct field against a baseline, and the number is meaningless without the baseline, which is
the part most submissions skipped.

so the division is by correct outcomes, and a retried card carries every attempt's spend into
that one outcome. the baseline is the same query over the previous window of equal length, with
the rule set travelling in the response. we rejected comparing against a published figure
because a rival's number is measured on a rival's cards, and a figure nobody can re-run is an
advertisement.

one refinement came from reading the metric written down properly rather than inferred from
submissions: cost per accepted durable outcome divides by outcomes still useful at a checkpoint,
because a card that closes and comes back was never an outcome. we kept both figures instead of
replacing one, since a crew needs to know what it produced this week and what of last month's
work survived. the checkpoint is seven days by default and the window's end is what it is
measured from, so the same card is correct today and durable next week.

the honest hole is spend we cannot see: a card that closed with no reservation has unknown
spend, not free spend. `partial` and `correct_with_unknown_spend` say so, and a comparison
involving a partial window answers `unknown` instead of a delta, because a cheaper number built
from missing rows is worse than no number.

## the sealed number is only half a measurement, so the gap is the other half

a holdout rate on its own cannot tell a crew that did the work from a crew that fitted the
examples it was handed, because both clear the sealed set at some rate and the rate alone does
not say which. the dark-factory submissions that measured this measured the distance between
the public acceptance examples an agent could read and the holdout it could not, and that
distance is the tell: a crew at a hundred percent on the examples and forty on the sealed set
did the examples.

we kept grading partial rather than binary, since three of five is worse than five and better
than none, and rounding it to failed throws away the number worth comparing next week. what
acceptance adds is two floors: the sealed rate has a floor and the gap has a ceiling, so a crew
that clears every example it could read accepts nothing. a crew with no visible examples
answers thin and unmeasured, not failed, because we have no evidence about fitting either way.

the record keeps a digest of the example set beside both rates. without it, next week's gap
would be compared against a number measured on different examples, which is the same mistake as
quoting a rival's baseline.
