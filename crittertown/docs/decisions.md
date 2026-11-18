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

