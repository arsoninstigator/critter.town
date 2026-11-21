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
