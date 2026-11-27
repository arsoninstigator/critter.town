# backend

three boxes, and the rule that keeps them apart: **the app never holds a provider key, and
the database never holds one either.**

```
┌──────────────────┐        ┌───────────────────┐        ┌──────────────────┐
│ crittertown         │        │ postgres          │        │ agent-vault      │
│ api, worker,beat │───────▶│ insforge/supabase │        │ :14321 admin     │
│                  │        │ rows, auth, files │        │ :14322 proxy     │
└────────┬─────────┘        └───────────────────┘        └────────▲─────────┘
         │ spawns sandbox                                          │
         ▼                                                         │
┌──────────────────┐   HTTPS_PROXY + token, no keys   ─────────────┘
│ agent sandbox    │──────────────────────────────────────────────▶
└──────────────────┘
```

## the database

the store is sqlite today, and that is the runtime the 391 tests run against: one file,
wal mode, a serialized job claim. it is the right default for a single machine and it is
what `CRITTERTOWN_DB` points at.

`make ddl` writes `docs/schema.postgres.sql`, the same 34 tables in postgres dialect,
generated from the one schema definition rather than hand-maintained:

```bash
make ddl    # python scripts/export_ddl.py > docs/schema.postgres.sql
```

so hosting the rows on **insforge** (self-hosted, open source) or **supabase** is a store
adapter away, not a redesign:

1. bring up insforge or supabase, create the project, and open its sql editor
2. paste `docs/schema.postgres.sql` and run it. there are no extensions, no functions and
   no triggers to install: the audit chain and the queue claim are computed in python, so
   a postgres that only honours `create table` and `create index` is enough
3. point the app at it: set `CRITTERTOWN_DB_URL` and install the extra

```bash
pip install 'crittertown[postgres]'
export CRITTERTOWN_DB_URL=postgresql://user:pw@host:5432/crittertown
```

`core.db.open_store` reads that one setting: empty means the sqlite file, anything else
means `core.store_postgres.PostgresStore`. nothing downstream changes, because it
offers the same four methods (`conn`, `tx`, `query`, `one`) and the wrapper translates
sqlite dialect at the driver's edge: `?` becomes `%s`, `insert or replace` becomes
`on conflict (<the schema's real primary key>) do update`, and `executescript` becomes
one statement at a time. that is why the three services that issue their own sql keep
working unchanged.

the schema is not a second file. `core/dialect.py` translates `core.db.SCHEMA` for both
the runtime and `make ddl`, `crittertown/tests/test_dialect.py` fails if
`docs/schema.postgres.sql` drifts from it (it had: the published file was 34 tables
against a 45 table schema, with an `autoincrement` postgres rejects), and
`crittertown/tests/test_store_postgres.py` drives the store through a fake driver, so a
`?` reaching psycopg or a transaction that stops being a transaction is a test failure
rather than a production surprise.

what is still sqlite-shaped, said plainly: `conn()` opens a connection per call and
`tx()` serializes writers inside one process. that is enough for a single node and for
a worker set that shares one postgres, and it is not a distributed lock. two processes
relying on `tx()` for mutual exclusion need a real advisory lock, which is not built.

## human auth, and the one credential that is a link

- **platform auth** (insforge or supabase) is for people: sessions, identity, storage
- **`CRITTERTOWN_API_TOKEN`** is for machines. one shared bearer token, checked in middleware,
  and `/health` stays open so a load balancer does not need it
- **gates** (`/g/{token}`) are the deliberate exception: the approval link *is* the
  credential. single use, bound to one decision, expiring, and refused after use, because
  the alternative is asking a shop owner to make an account to say "yes"

if you are putting a real ui in front of this, the ui gets a platform session and calls
crittertown with the token on the server side. crittertown does not model end users, it models
agents and one owner, and pretending otherwise would be two half-auth systems.

## per-agent credentials: agentvault, baked in

an agent with a `GITHUB_TOKEN` in its environment is one prompt injection away from
leaking it, and an agent that echoes its environment puts the key in a run row. so a key
is never handed to an agent. it is handed to a proxy, and the agent is given a route
through it.

[agentvault](https://github.com/Infisical/agent-vault), infisical's open-source agent vault, is that proxy: a single binary
that stores credentials, exposes a management api on `14321` and a MITM proxy on `14322`,
and attaches the real credential to outbound requests. run it on a **different machine**
from the agents, because that separation is the security claim:

```bash
docker run -d --name agent-vault \
  -p 14321:14321 -p 14322:14322 \
  -e CREDENTIAL_BROKER_MASTER_PASSWORD=... \
  -v agent-vault-data:/data agent-vault   # their published image, or build their repo
```

then, per agent, crittertown stores a **grant**, never a secret:

```bash
curl -X POST localhost:8000/agents/$AGENT/vault -H 'content-type: application/json' -d '{
  "label": "github",
  "token_env": "WREN_VAULT_TOKEN",
  "scopes": ["github.com", "api.github.com"],
  "expires_hours": 8
}'
```

- `token_env` is the **name** of the variable the brokered token arrives in. crittertown reads
  it from its own environment at spawn time and never stores it, logs it or returns it
- `scopes` are host suffixes and they are required. a grant with no scopes used to defer
  to the proxy's own service rules, which meant "any host", so it is refused at write time
  now and a legacy unscoped row denies instead of deferring. the grant is enforced by
  crittertown at `POST /vault/egress`, which the proxy asks before it dials, and the
  sandbox gets the proxy as `HTTP(S)_PROXY` so the ask is on the path rather than
  optional. the honest caveat: this is a real check only for traffic that goes through
  the proxy, and a provider that lets a sandbox out directly is outside it
- `GET /agents/{id}/vault` shows the shape of what a sandbox would receive, with every
  value masked (`<set>` or `<missing>`), and `sandbox_env()` refuses to spawn an agent whose
  token is missing rather than starting it crippled
- revoking writes a row and an audit entry (`vault.revoked`). the grant history is the
  point: an audit trail that deletes itself is not one

### closing a card: the evidence gate

- `POST /tasks/{id}/approve` takes `{actor, note, evidence}` and walks the card to done.
  the evidence is checked before the card moves, so a refusal never leaves a half approved
  card behind
- `POST /tasks/{id}/move` takes `evidence` too, and refuses a move to `done` without one,
  so the gate cannot be routed around
- the card's `kind` (set at creation, one of ten) decides which schemes count, and the
  refusal names the schemes it wanted. a crew's `orchestration.evidence` may narrow them

### evals: does the crew still answer well

- `POST /crews/{id}/evals` takes `{name, scenarios, candidates, scorer}` and stores the
  grid as a draft. every id must be present and unique, every scenario needs an input,
  and the scorer is checked here, so a bad grid is refused before anything is spent
- `POST /evals/{id}/run` walks scenarios by candidates in declaration order, records one
  row per cell with the output, the score and the reason, and summarises the mean per
  candidate. a tie goes to the candidate declared first
- `GET /evals/{id}` and `GET /crews/{id}/evals` read the report back, cells included

a cell is one model turn with the candidate's `system_prompt`, never a sandbox, so an
eval measures the decision and not the execution. the scorer is `{kind, expect}` with
kinds `exact`, `contains` and `regex`, stored with the run so a report can still say how
it was judged after the code has moved on.

### topology: which shape the work needs

- `POST /topology` takes `{signals}` and answers with a shape, the one fact that decided
  it, what implements it here, and whether it needs a gate. nothing is stored
- `GET /topology/shapes` lists the five shapes, the primitives behind each, and the
  signal vocabulary

the signals live on a card under `payload.signals`, and `payload.structure` may declare a
shape. dispatch compares the two and refuses a card that declares orchestration its own
signals deny: that is the shape that costs seats, tokens and a convergence.

### the barrier: whether a fan-out is finished

- `POST /tasks/{id}/convergence` opens a barrier over a partition map, a join rule and a
  deadline. the plan is validated first, so a partition with no owner is refused before
  it is stored
- `POST /convergences/{id}/report` records one lane: `result` with a reference,
  `not_applicable`, or `blocked` with a reason. re-reporting replaces that lane
- `GET /convergences/{id}` answers with the barrier, the missing lanes, any two lanes
  that wrote the same surface, and whether it has stalled past its deadline
- `GET /tasks/{id}/convergence` is the same question asked from the card

a fan-out opens one automatically, one partition per child card, join `all`, and the
runner reports for a lane when its run ends. `approve` refuses a parent whose barrier is
open.

### the roster: who is on the work

- `POST /crews/{id}/roster` takes the same `signals` as `/topology` plus `parts`, reads
  the crew's real agents and each agent's enabled slices, and answers with seats: role,
  agent, slices, withheld slices, and one sentence per seat saying why it exists
- `GET /roster/roles` is the table itself: roles to slices, shapes to seats

`decided` says the shape and its seats are consistent; `staffed` says every seat has an
agent. `problems` are structural (a gated shape in a one-agent crew, a seat with no slices
left) and `gaps` are staffing. the runner passes a seat role into `context.compose`, which
intersects the role's entitlement with the agent's enabled slices.

### the output rail, and the rows it leaves

`POST /crews/{team_id}/rails/check-output` takes `{text, agent_id?, foreign_refs?}` and answers
with the verdict, naming the rail, the shape and the offset and never the matched text.
`GET /crews/{team_id}/rail-refusals` lists what refused recently, from `rail_refusals`, which
stores the rail, the kind, the offset and the length but never the answer.

the rail is set like any other: `secret_shapes` takes a list of shape names or `["all"]`,
`forbidden_patterns` a list of regexes (an uncompilable one is refused at set time),
`max_output_chars` a number, `foreign_refs` true or false. the runner calls it on every turn,
before a command runs or a message leaves, so a refused turn fails the run as blocked with the
rail named rather than posting the message and reporting afterwards.

### skills: three stages and two budgets

`POST /crews/{id}/skills` takes either a `SKILL.md` as `{text}` or the same fields as json, and
answers with the stored skill. `GET /crews/{id}/skills` is level 1: name, description, tool
list and file count, never a body, because a list of skills is what a caller reads to choose
one. `POST /skills/{id}/load` is level 2 and refuses a body over the budget it is given
(`body_over_budget`) instead of truncating it; `GET /skills/{id}/files?path=` is level 3.

the refusals are the security story: `frontmatter_missing`, `skill_name_not_a_slug`,
`description_must_say_when`, `description_too_thin`, `body_required`, `body_too_long`,
`allowed_tool_unscoped`, `skill_unreachable`, `file_path_escapes`, `file_too_large`. an empty
`agents` list means the whole crew; `PATCH /skills/{id}` sets `enabled` and `agents`, and a
disabled skill is neither offered at level 1 nor loadable.

the `skills` slice carries level 1 with a 500 character budget, and it is entitled to doer and
researcher seats only: a reviewer judging a card is not helped by the procedure that made it.

### a wake is recorded before it is acted on

`work/wake.py` holds one pure table, `decide(task, holder, attempts, max_attempts, live_run,
agent_id)`, and a thin queue around it. the decisions, first match wins: a card in `done` or
`blocked` is refused with its column named; `review` defers because a person owes the
decision; `backlog` refuses because a card nobody scheduled is not woken by a trigger; an
exhausted card refuses; a live holder that is not the asking agent coalesces onto its run; the
holder asking again is a replay and coalesces too; otherwise the wake runs.

`WakeQueue.admit` reuses the pending wake for a card rather than adding a second, writes the
decision with its reason, and `dispatch` returns that decision instead of raising when it is
not `run`. `delivered` marks the wake that became a run, and `settle(run_id, outcome)` puts a
coalesced wake back to pending when its run ends, because the run had no way to know it had
absorbed it. `revive(team_id)` brings deferred wakes back once the card leaves review, and
`dispatch_ready` calls it first: a deferral nobody revives is a drop with better manners.

routes: `POST /crews/{id}/wakes` with `task_id`, `source`, optional `note` and `agent_id`, and
`GET /crews/{id}/wakes` for the wakes still owed an answer.

## a mandate is checked against the card it carries

`manage/mandate.py` finds identifier-shaped tokens (endpoint paths, snake_case identifiers,
kebab-case test ids) in an agent's role and instructions, and compares them with the
vocabulary of the card it is about to be given. that vocabulary is derived, not listed: the
terms the card uses that no other card, knowledge doc or context entry uses. a match is a
finding naming the line, the kind and the term.

two rules keep it usable. a dispatch is never stopped by a finding: it lands in the run's
`mandate_findings`, and an audit row `mandate.overfit` is recorded against the card, so a
pattern across cards becomes visible and a false positive on a word costs nothing.
`for_dispatch(strict=True)` exists for a crew that wants the refusal, and refuses with
`mandate_names_the_card`. on a board with nothing else to compare against, only endpoint
paths are named, because saying "everything is distinctive" would flag every identifier a
seat legitimately uses.

routes: `POST /crews/{id}/mandate/check` with `agent_id` and `task_id`, and
`GET /cards/{id}/mandate/vocabulary` for the terms a mandate must not name.

## the seal carries a fingerprint

`holdout.seal()` writes a digest over the sealed criteria into `holdout_seals`, and every
scenario row keeps its own digest. `state()` reports `tampered` as the scenarios that moved,
`verify()` fails when any did, and `run()` marks such a run `clean: false` while still
recording the number. the digest is over what was sealed, not when or by whom, so two crews
sealing the same criteria share a fingerprint and re-sealing identical criteria is the same
commitment.

## what a model name means on the way in

every importer runs its model string through `agents/models.py`. a name is kept, a provider
prefix is stripped, and a sentence is refused into `unmapped` with a warning, because the
column is sent to a vendor and `gpt-4o, best for writing` is not a model. adoption records
the claim in the agent's `import-source` profile entry and says in one sentence who
decides: `the source asks for model 'gpt-4o', and this deployment answers with
'gpt-4.1-mini'`. there is no allowlist, and inventing one would refuse names that work
while blessing names that do not.

## reading a card somebody else wrote

an a2a agent card is a claim about a remote agent, written by that agent's operator. so
`from_a2a` reads it as testimony: the description becomes instructions with a header
saying whose account it is, the skills are reported as `a2a.skill:<id>` rather than adopted
(a skill name from a card is the remote party naming our capabilities), each skill's
description is kept as knowledge attributed to "the remote agent claims", and a card
carrying a credential is refused with `card_carries_a_secret`, because a card is a public
document. a card over plain http, or with no description at all, comes with a warning
rather than a silent pass.

## the skills the tool ships

`GET /skills/shipped` lists the SKILL.md the tool carries about itself, and
`POST /crews/{id}/skills/shipped` with `{"name": "using-the-board"}` installs one into a
crew (409 when the crew already has it, 422 naming the alternatives when the name is not
shipped). the skeleton crew carries both. the point is not documentation: a board with a
REST surface and no skill telling an agent how to drive it is a board agents use badly,
and the skill is validated by the same rules as a written one, so it cannot be the file
that quietly breaks them.

## importing an agent from another framework

`agents/agent_imports.py` reads six sources into one manifest: claude-code, openclaw,
crewai, a2a, an openai assistant object and our own export. two rules hold across all of
them. a tool name from another framework never becomes a local permission: crewai's
`SerperDevTool` maps to `search` through `CREWAI_TOOLS`, an openai function tool is
reported as `tool.function.<name>` unless its name is one of ours, and everything else
lands in `unmapped` for a person to decide. and nothing that is work is imported as
identity: a crewai `tasks:` block is reported, because tasks belong on the board.

## the crew's trail

`GET /crews/{id}/audit` answers the whole bundle: entries in crew order, the crew verdict,
each agent's verdict, and a verdict on the bundle itself. `GET /crews/{id}/audit.jsonl`
returns the same entries as ndjson, one object per line, so a trail can be grepped, diffed
and counted. `POST /crews/{id}/audit/verify` with `{"jsonl": "..."}` checks a trail that
arrived from somewhere else, using nothing but the trail.

## crew templates, and the skeleton

`GET /crews/{id}/template` answers a document: schema `crittertown.crew/1`, the crew's name,
mission and review mode, each agent's name, role, kind, hosting, model, system prompt, skills
and tools, the crew's rails by kind and value, and each skill as the SKILL.md it came from.

`POST /crews/{id}/template` imports one, and `POST /crews/skeleton` creates a crew and
imports the skeleton into it in a single call. `GET /templates/skeleton` reads the document
without standing anything up.

the refusals are the point. a document may not carry `scope` or `runtime`, so an imported
crew cannot arrive with permissions or a credential; `egress_hosts` may not contain `*`; a
rail may not name an agent that is not in the document; a rail kind outside the ten the
platform sets is refused by name. the document also has to be staffable as written: an
external worker cannot be hired as a reviewer, and a kind cannot fill a seat it never holds.
every one of those is checked before a single row is written.

### the holdout wall

the criteria a crew is measured on are the one thing it must not be able to read.

- `POST /crews/{id}/holdout` seals the scenarios and refuses three ways: no scenarios,
  already sealed, and `criteria_already_in_context`, which fires when the answers can
  already be found in the crew's knowledge, skills, lessons, intent or agent prompts. a wall
  around something already in the room is decoration.
- `GET /crews/{id}/holdout` answers how many and how the runs went, never what they say.
- `POST /crews/{id}/holdout/run` scores the crew with the configured model. the run records
  its metric even when the crew tried to look, and marks itself tainted, because a metric
  with a footnote is worth more than one with a lie attached.
- `GET /crews/{id}/holdout/verify` is the check that makes the wall a claim: it composes
  every agent's context and scans everything the crew can be handed for the criteria.
- `GET /crews/{id}/holdout/access` is the ledger. an agent presenting itself to the read path
  is refused by name and the refusal is a row, since an attempt is the interesting half.

### the heartbeat: an agent we do not run

`POST /agents/{id}/heartbeat` with `{key, ttl_seconds}` is how a worker built somewhere else asks
for work. it answers with the card it now holds (the payload, the capability the card was addressed
by, and the evidence scheme that will close it) or with `{ok: false, why: "nothing_ready"}`. beating
while already holding renews instead of double dispatching.

the refusals are the design: `not_external` keeps the route from becoming a second way to claim
work for an agent the platform drives, `key_mismatch` because a beat carries the worker's own key,
and `seat_not_live` for a frozen or burned seat, refused before the work is described. an external
worker may hold doer, sampler and researcher seats and may never review or integrate, so the
roster will not seat one in a judging seat and `POST /tasks/{id}/approve` refuses it with
`external_agent_cannot_review`. `GET /agents/{id}/heartbeat` reads back what it may do, when it
last asked and what it is holding.


a worker we do not run brings its own key, because a key we minted would make every
signature on its rows a statement about us:

- `POST /agents/{id}/identity` with `{pubkey}` registers a 32 byte x-only schnorr key for
  an external agent. it refuses a local agent (`not_external`), a malformed key
  (`bad_pubkey`), a second different key for the same agent (`key_already_registered`, since
  a principal that can rekey on request can deny what it signed yesterday) and a key
  already held by another agent (`pubkey_in_use`). `GET /agents/{id}/identity` answers the
  public half, the custody and whether we can sign for it, which for an external worker is
  no.
- `POST /agents/{id}/heartbeat` accepts `signature`, `at` and `nonce`. the signature is over
  `sha256(canonical({"at": ..., "nonce": ...}))`, so it is a statement the platform cannot
  make. a signed beat without `at` is refused (`beat_signature_needs_at`), a signature from
  a key that is not registered is refused (`no_registered_key`), and a bad one is refused
  (`signature_invalid`) before anything is written.
- the beat writes one `heartbeat` audit row. unsigned when the worker brought no signature,
  which is recorded rather than hidden, and attested when it did. `attests: payload` says
  the signature covers the payload; `attests: row` says we signed the row digest, as we do
  for an agent we run. `attest.verify_entries()` and `audit.verify()` both follow that flag,
  so a bundle still verifies with no database.

### custody: who holds a card

- `GET /tasks/{id}/custody` returns the holder, the lease expiry, the attempt count and
  the invariants the row breaks right now
- `GET /crews/{id}/custody` is the sweep as a question: every action it would take and
  every card it would name, without writing
- `POST /crews/{id}/sweep` applies the two repairs (reclaim, revoke) and reports the rest
- `POST /tasks/{id}/replan` takes a different brief, resets attempts and returns the card
  to `ready`. a card in someone's hands refuses it: fix the brief, give the card back, then
  replan

the runner holds the lease: it claims before the card becomes work, renews one turn at a
time, completes with `run:<id>` as the evidence reference, and hands the card back when a
run fails. a runtime failure is a retry and spends an attempt; a wrong brief blocks at
once. the runner never moves a card it no longer holds.

### sidecars, and what an agent was handed

- `POST /agents/{id}/sidecars` spawns a helper beside one card (`task_id` required).
  `GET /agents/{id}/sidecars` and `GET /tasks/{id}/sidecars` read them back; both list
  only active ones, because a retired sidecar is a row and a row is not a helper
- `GET /agents/{id}/context` returns the last snapshots, the slices the agent currently
  asks for, and the budgets; `POST /agents/{id}/context/preview` assembles a snapshot
  without storing it, which is how an operator checks the budget before a run rather
  than after one
- `GET /agents/{id}/child` is unchanged: a sidecar is not in it. the relation is in
  `agent_spawns.relation`, and `GET /agents/{id}/lineage` does not walk through a
  sidecar, because a sidecar has no lane of its own to walk

### how the env reaches the sandbox

two paths, chosen by the provider, and the difference is a credential's safety rather than
a preference:

- **at creation**, when the provider's create request has an env field. `ensure()` takes
  `env` and the adapter forwards it; `delivers_env = True`. this is ephemeral sandboxes and
  the local dev provider. nothing records the values: the handle that lands in the
  `computers` row carries a ref, not an env
- **at boot**, when it does not. a computer create request has no env field
  (`delivers_env = False`, same for hosted whose sdk surface is theirs), so the computer is
  handed nothing and its harness calls `GET /agents/{agent_id}/sandbox-env` once at boot,
  authenticated with the platform token, and keeps the values in memory
  (`agents/runtime.py: pull_env`). a secret never sits in an exec command line or on the
  machine's disk, which is where the obvious implementation leaves it

per-agent env never reaches a row, a response or a log. `GET /agents/{id}/vault` shows the
shape with every value masked, `pull_env` returns names and a count, and
`crittertown/tests/test_sandbox_env.py` asserts the token is absent from the computers row
and from the response that created it.

### where the codes actually live

the broker distributes; it is not the vault of record. the two baked-in ingredients are
[agentvault](https://github.com/Infisical/agent-vault) for brokering and
[kernel](https://github.com/kernel/kernel-images) for browsers (a throwaway chromium in your
own container, per `crittertown/thirdparty.md`).

kernel's managed auth also offers a vault route that points at 1password, and that is
declined on purpose. the codes live upstream in **proton pass** or **bitwarden** (both open
source, both self-hostable), permissioned per agent, and the grant is issued **up the command
chain**: the agent asks, its coordinator in the org chart is the one who approves, and the
approval is a gate row with an audit trail rather than a shared master password.

honest status: the request-and-approve path exists (`gates`, `a2a`, the org chart), and what
is not built is the adapter that mints or reads a code from proton pass or bitwarden when a
gate is approved. until it is, the operator issues the brokered token by hand, which is the
same shape with one manual step.

## env

| var | meaning |
| --- | --- |
| `CRITTERTOWN_DB` | sqlite path (the runtime today) |
| `CREDENTIAL_BROKER_URL` | agent-vault management api, e.g. `http://vault:14321` |
| `CREDENTIAL_BROKER_PROXY_URL` | agent-vault MITM proxy, e.g. `http://vault:14322` |
| `CREDENTIAL_BROKER_TTL_HOURS` | default grant lifetime, 24 |
| `CRITTERTOWN_GATE_SECRET` | signs approval links; generated per db when unset |
| `CRITTERTOWN_PUBLIC_BASE_URL` | the host approval links are built against |
| `CRITTERTOWN_API_TOKEN` | shared bearer token for machines |

## delegation, budget, health

- `POST /crews/{id}/delegations` offers a handoff, `POST /delegations/{id}/answer` accepts
  or rejects it, `POST /crews/{id}/delegations/sweep` expires the silent ones.
- `POST /tasks/{id}/breakdown` splits a card into owned parts and opens the barrier.
- `POST /crews/{id}/budget/reserve`, `POST /budget/{id}/settle`, `POST /budget/{id}/release`,
  `GET /crews/{id}/budget`. the spend guardrail reads the same ledger.
- `POST /crews/{id}/roster` and `GET /roster/roles` for seats and their slices,
  `GET /agent-kinds` for the four kinds of worker.
- `GET /health` is liveness with counts; `GET /health/ready` is readiness and answers 503
  when the store does not.

projects, claims, strikes, crossings:

- `POST /crews/{id}/projects`, `GET /crews/{id}/projects`, `GET /projects/{id}`
- `GET /projects/{id}/gate`, `POST /projects/{id}/advance`, `POST /projects/{id}/kill`
- `POST /crews/{id}/claims`, `GET /crews/{id}/claims/{thread}`
- `GET /agents/{id}/standing`, `GET /crews/{id}/strikes` (every seat and where it stands), `POST /crews/{id}/strikes`, `POST /crews/{id}/strikes/{agent}/clear`
- `POST /crews/{id}/crossings`, `GET /crews/{id}/crossings`, `POST /crews/{id}/crossings/authorize`, `POST /crossings/{id}/revoke`

`POST /a2a` is the one write that can cross a crew boundary. when the recipient belongs to
another crew the message is authorized against a declared crossing, using the envelope kind
the message kind maps to (`note` finding, `handoff` proposal, `request` and `review_request`
request, `result` receipt, `blocker` signal), and the size and rate are checked before the row
is written. a refusal is a `NotAllowed` carrying the wall's reason (`no_crossing`,
`kind_not_allowed`, `crossing_expired`, `crossing_disabled`, `too_large`, `rate_limited`). the
row records `to_team_id`, `crossing_id` and `envelope_kind`; a message inside the crew leaves
all three empty.

`POST /tasks/{id}/breakdown` takes `parts` (title, slice, surface, budget_cents,
assignee_agent_id), an optional `join`, and an optional `project_id`. the join defaults to
the card's shape; a looser join is refused.

seats, dispatch and objectives:

- `GET /agents/{id}/seat`, `POST /agents/{id}/freeze` (reason, ttl_days), `POST /agents/{id}/thaw`
- `POST /agents/{id}/burn` (reason), `POST /agents/{id}/duplicate` (name, role, skills)
- `POST /crews/{id}/seats/sweep` freezes seats quiet past the crew's `idle_freeze_days` and
  retires the ones frozen past their window
- `POST /crews/{id}/dispatch/next` decides the next card, claims it for 15 minutes, and
  answers `nothing_ready`, `kind_unregistered`, `no_seat_for_slice` or `all_seats_leased`
  when the honest answer is nothing
- `POST /crews/{id}/objectives`, `GET /crews/{id}/objectives`,
  `POST /objectives/{id}/measure`, `POST /crews/{id}/replenish`

a seat is a column set on `agents` (`seat_state`, `frozen_until`, `burned_reason`, `orphaned`,
`lineage`, `depth`, `parent_id`), not a table of its own, because the seat is the agent seen as
a job. objectives and measurements are their own tables. the schema is 18.

output silence:

- `POST /tasks/{id}/output` (agent_id, stream, size) is the holder's heartbeat; nobody else
  may report output for a card
- `GET /tasks/{id}/silence` and `GET /crews/{id}/silence` show the level, the age and what
  suppressed it
- `POST /tasks/{id}/silence/decide` (decision, actor, reason, snoozed_until) records a
  snooze, a continue or a dismissal; only the board or the assigned recovery owner may
- `POST /crews/{id}/silence/escalate` files the recovery cards; `POST /crews/{id}/sweep` runs
  the lease sweep and this in one call, under a `silence` key

the schema is 19: `tasks` gained `last_output_at`, `output_seq`, `lease_started_at` and
`watchdog_of`, and `silence_decisions` holds the answers.

## a premise is hashed, a beat is written down, a room is not the work

three mechanisms, all about the same failure: work that looks finished because nothing said
otherwise.

**premises.** `work/witness.py` hashes what a run was handed, in three kinds: `brief` (title,
description, skills, payload minus the keys a run writes back), `knowledge` (the entries
`Knowledge.cited` returns, the same call the prompt uses so the two cannot diverge) and
`parent` (the parent card's title, evidence and payload). rows go into `witnesses` at
dispatch, before the first turn, keyed by run. `compare` names three changes: `changed`,
`removed` and `added`. a removed premise is the loudest, because the run was made from
something that no longer exists.

routes: `GET /cards/{id}/premises?run_id=` for what one run was made from, `GET
/cards/{id}/drift` for one card, `GET /crews/{id}/drift?limit=` for the crew, which reports
`checked`, `drifted` and only the drifted cards. the sweep is read-only on purpose.

**waivers.** `manage/waivers.py` holds the rule the board applies. a card leaving `review`
for `done` on a reviewed crew may be signed by a person, by a crewmate, or by a service only
if a waiver exists on that card for the beat. the reason has a floor of 20 characters, one
waiver per beat, and the waiver is an audit-visible row rather than a flag. `kanban.edit`
exists so a brief can be corrected after a card exists, which is also what makes brief drift
reachable: `PATCH /cards/{id}` with `title`, `description`, `payload`, `priority` or
`required_skills`, recorded as an `edited` event.

routes: `POST /cards/{id}/waivers` (beat, reason, by), `GET /cards/{id}/waivers`, `GET
/crews/{id}/waivers`.

**env blocks.** a `ProviderNotConfigured` or `ProviderFailure` at sandbox start returns
`decision: env_blocked` instead of spending an attempt: the reservation is released, the
lease released, `env_blocks` incremented and the card moved back to `ready`. at
`MAX_ENV_BLOCKS` (3) it parks in `blocked` with the reason, so a provider outage does not
loop a queue. the card shape now carries `env_blocks` next to `attempts`, because the two
count different things.

the schema is 29: `witnesses` (team, task, run, kind, ref, digest, chars) and `waivers`
(team, task, beat, reason, waived_by), plus `tasks.env_blocks`.

## an inherited failure is not the child's failure

`work/lineage.py` walks `parent_id` with a limit of 12 and a visited set, so a row that names
itself as its own ancestor is reported as `cycle` rather than walked forever. `decide(parents)`
is a first-match table over the chain, nearest first: `blocked` gives `inherit_blocked`, a
parent in `review` gives `defer`, anything else runs. backlog is deliberately absent, because
a child queued ahead of an unscheduled parent is a scheduling mistake and parking the child
would hide it.

the runner reads the chain before a lease, a reservation or an attempt is spent. an inherited
block moves the child to `blocked` with `blocked_reason` written on the row (move() only
carries a note into review, so the reason is written beside it), records one
`task.inherited_failure` audit row naming the ancestor, and returns `decision:
inherit_blocked` with `run_id: ""`. a deferral changes nothing on the card and returns
`decision: defer`, so a person answering the parent is the only thing that moves the child.

routes: `GET /cards/{id}/lineage` returns the chain, `cycle` and the decision; `GET
/crews/{id}/lineage` returns the cards a block has taken out, split into `blocked` and
`deferred`, with the ancestor named on each.

no schema change: the chain is already `tasks.parent_id`, and the reason already has a column.

## what a correct outcome cost

`work/efficiency.py`. `summarise(cards)` divides spend by correct outcomes and reports the raw
counts beside the figure, so a reader can check the division: `cards`, `correct`, `rejected`,
`spend_cents`, `cost_per_correct`, `cost_per_card`, plus `correct_with_unknown_spend` and
`partial`. `rate()` returns None when nothing was correct, because no figure is honest there.

the service reads three things in a window: tasks with `column_name`, `evidence_ref`,
`assignee_agent_id` and `attempts`; settled `budget_reservations` grouped by `task_id`; and
`task_events` rows of kind `moved` whose data goes from `review` to anything but `done`, which
is one rejection each. a card is correct when its column is `done` and it carries an evidence
reference, so a card in review is not yet an outcome and a blocked card is not written off as
one.

durability is the checkpoint: a correct card counts as `durable` only when the window's end is
at least `durability_days` (7 by default) past the move that closed it, so a card closed this
morning is a correct outcome and not yet a durable one, and `cost_per_durable` is the figure to
read when the question is what survived. a card that left `done` counts as `came_back` and is
credited again only if it closes again, which is what makes churn visible without punishing a
fix.

`for_crew(team_id, since, until, durability_days)` returns the crew total, one block per agent sorted by cost per
outcome (unknown last), the ten cards that cost money and closed nothing, and the ten cards that
were retried or rejected, both by spend. `compare_windows(team_id, days)` measures the window and
the previous window of equal length and returns `verdict` of cheaper, dearer, level or unknown,
where unknown is what a window with unknown spend deserves. `compare()` puts the rule set in the
response and marks `reproducible: true`, because the baseline is this same query over earlier
rows, not a number borrowed from somewhere else.

routes: `GET /crews/{id}/efficiency?days=7&compare=true&durability_days=7` and
`GET /cards/{id}/efficiency`.

no schema change: the ledger already holds settled cents per task, and the review move already
sits in `task_events`.

## the visible half of the criteria, and the distance to the sealed half

`work/holdout.py` already keeps the criteria out of the crew's reach. what it could not say is
whether the crew did the work or fitted the examples it was handed, because there was nothing
to compare against. `holdout_public` holds the visible half: the examples a crew may read, set
through `POST /crews/{id}/holdout/public` and read back through `GET`. each example's scorer
carries its own expect, exactly as a sealed scenario does, so the same criterion cannot be read
one way in the visible half and another in the seal.

`partial(public_cells, holdout_cells, min_scenarios, min_holdout_rate, max_gap)` is the whole
rule and takes nothing but scores. a cell is decided only at full marks. it answers both rates,
the gap between them, a verdict of honest, fitted or thin, an `accepted` flag, named reasons
(`too_few_scenarios`, `holdout_below_floor`, `fitted_to_the_examples`, `no_public_examples`) and
the floors it used, so a refusal can be argued with instead of guessed at. a crew with no
visible examples is unmeasured rather than failed: `no_public_examples` blocks acceptance but is
not counted against the sealed half.

`gap(team_id, model)` scores both sets with the same model and the same scorer, and writes a
`holdout_gaps` row carrying both rates, the gap, the verdict, the reasons, whether the run was
clean (a tampered seal taints it, as with a plain run) and a digest of the example set, because
a gap measured against different examples is a different measurement. `acceptance(team_id)`
answers the gate from the last run, or `unmeasured` when there has never been one, and refuses a
tainted run by name.

routes: `GET|POST /crews/{id}/holdout/public`, `POST|GET /crews/{id}/holdout/gap`,
`GET /crews/{id}/holdout/acceptance`. schema 30 adds `holdout_public` and `holdout_gaps`.

## where a card's work went, and what a rejection changed

volume hides both halves. `work/distribution.py` reads one card's events, runs and settled
reservations and answers two questions: how the work was split across the people who did it, and
what each rejection actually changed.

`rounds(events)` is pure. it walks the card's events in order, opens a round at every move out of
review that is not a move to done, and closes it at the next move into review or done. inside a
round, an `edited` event is compared field by field against the fingerprint that field held
before, so a field rewritten to the same value counts as nothing. the card's own `created` event
carries fingerprints too, so the first fix after a rejection is provable instead of assumed. a
field with no earlier fingerprint lands in `assumed`, and a round with nothing proven but an edit
is `unproven`, not silent: the honest answer there is that it cannot be judged.

`share(units)` is pure too. turns come from the steps a run recorded, spend from settled
reservations, and spend with no agent on it is reported as `unattributed_cents` rather than split
evenly, since an even split invents a fact about who did the work. `concentrated` is true when one
agent holds at least three quarters of the turns on a card that more than one agent touched.

`summarise` counts rejections, answered rounds, reworked, silent and unproven, and reports
`silent_share` over the rounds that could be judged, leaving unproven ones out of both sides.

`DistributionService.for_card(task_id)` returns the split, the rounds (each with what the rework
cost, measured from the rejection to its answer), and the summary. `for_crew(team_id, since,
until)` adds the two lists a person should open first: `silent_rejections` and `concentrated`.

routes: `GET /crews/{id}/distribution?days=7&limit=50` and `GET /cards/{id}/distribution`. the
board's `created` and `edited` events carry a per-field digest; nothing else changed shape.

## the decisions a crew builds on

`work/ledger.py` keeps decisions as rows a card can be checked against, because the quiet failure
is a decision that was made once, written down once, and then contradicted by work nobody compared
it with. the six kinds are the six ways work drifts (`logic`, `compliance`, `tooling`, `language`,
`scope`, `strategy`), and none of them is specific to code: a research claim, an operations rule
and a pricing rule all live in `statements`.

a statement carries its text, its kind, a declared `subject` handle, its `source` and
`source_kind`, and who decided it. an agent's statement is `provisional`; a person's is `settled`,
and `settle()` refuses an agent by name, because a ledger where the machine's guesses count as
settled facts is worse than no ledger.

contradictions are computed, not judged. `contradicts()` compares the declared subject and the
digest of the text: same non-empty subject, both active, different text, and both rows are marked
`contradicting` with each other's id. a restatement that only differs by punctuation or
capitalisation is the same claim, so the digest is taken over the normalised text. the way out is
one act: `propose(..., supersedes=old)` retires the old one and clears both sides, `retire()`
clears the other side, and `settle()` deliberately does not, since a disagreement that vanishes
because one side got louder is how a drift gets buried.

`relevant()` matches a card by declared subject (a fact) or by shared words (a hint, two words or
one of six characters or more, plurals folded). `verdict()` answers `blocked` only for a
contradiction on a declared subject, `ask` for any other contradiction or a provisional claim, and
`clear` otherwise. `check()` records every question in `statement_checks`, and `for_dispatch()`
puts the answer on the run without halting it; `strict=True` refuses instead.

routes: `POST|GET /crews/{id}/statements`, `GET /crews/{id}/statements/contradictions`,
`POST /crews/{id}/statements/check`, `GET /statements/{id}`, `POST /statements/{id}/settle`,
`POST /statements/{id}/retire`, `GET /cards/{id}/statements`. a card declares what it is about in
`payload.subjects`. schema 31 adds `statements` and `statement_checks`.
