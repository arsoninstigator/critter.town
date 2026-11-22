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
- `GET /agents/{id}/standing`, `POST /crews/{id}/strikes`, `POST /crews/{id}/strikes/{agent}/clear`
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
