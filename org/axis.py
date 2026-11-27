"""the second axis of an org chart: who owns a surface, and who may borrow a seat.

critter.town already had the first axis. a department has a parent, a coordinator and a chain,
so authority flows up and routing is a chain instead of a popularity contest. what it did not
have was the axis that stops one giant queue from being one giant blast radius: the question of
which department is allowed to change which surface.

the rule is the one the owner's own org notes state, and it is short enough to enforce:

- a surface has exactly one owning department. not primarily, and not with input from.
- reaching into someone else's surface is a proposal, written down and settled by the owner of
  that surface. it is never an edit, so a generic "write anywhere" seat cannot quietly become
  an admin.
- a bloc borrows seats from departments for one objective, as a grant with an expiry. the seats
  keep their home departments and go home when the grant lapses, so nothing was reorganised.
- a wing holds a capability nobody's metric rewards (security review, legal, on-call). it has a
  veto in its domain and no budget of its own, and a wing handed a growth target has become a
  department with a nicer name, so the target is refused at the door.

nothing here halts a dispatch by default. `orchestration.org.enforce` turns the refusal on for a
crew that has decided it wants one, and until then a card that names a surface it does not own
is recorded on the run as a finding. a gate that appears unasked for reads as a bug.
"""

from __future__ import annotations

from typing import Any

from core.db import Store, loads, dumps, new_id, now
from core.errors import Conflict, NotFound, ValidationError

# the three decision ceilings. a layer is the kind of question an agent may answer, not a title:
# a directive says why and what is refused, a division decides which surface and how work is cut,
# a seat does one task on one surface and may not change the grant it was handed.
LAYERS = ("directive", "division", "seat")
LAYER_RANK = {name: index for index, name in enumerate(LAYERS)}

PROPOSAL_STATES = ("open", "accepted", "declined", "withdrawn")

# words that make a directive a spec in disguise. a directive that names a function, a column or a
# route has stopped saying why and started saying how, and how is the division's question.
SPEC_SHAPES = (
    "()", "::", "->", "def ", "select ", "insert ", "update ", "/api/", "http://", "https://",
)

# a wing is defined by having nothing to grow. these are the fields that would give it something
# to grow, and each is refused rather than ignored.
WING_FORBIDDEN = ("budget_cents", "growth_target", "target", "quota")

OBJECTIVE_KINDS = ("objective", "card", "")


def layer_rank(layer: str) -> int:
    if layer not in LAYER_RANK:
        raise ValidationError(f"layer must be one of {list(LAYERS)}", layer=layer)
    return LAYER_RANK[layer]


def reads_as_spec(text: str) -> list[str]:
    """the spec-shaped fragments in a directive, so the refusal can name them."""
    lowered = str(text or "").lower()
    return [shape.strip() for shape in SPEC_SHAPES if shape in lowered]


class OrgAxis:
    def __init__(self, store: Store, departments: Any = None, kanban: Any = None,
                 audit: Any = None):
        self.store = store
        self.departments = departments
        self.kanban = kanban
        self.audit = audit

    # ---- policy --------------------------------------------------------
    def policy(self, team_id: str) -> dict:
        row = self.store.one("select orchestration from teams where id = ?", (team_id,))
        if not row:
            raise NotFound(f"crew {team_id} not found")
        return (loads(row["orchestration"], {}) or {}).get("org") or {}

    def enforced(self, team_id: str) -> bool:
        return bool(self.policy(team_id).get("enforce"))

    # ---- surfaces ------------------------------------------------------
    def _surface(self, surface_id: str) -> dict:
        row = self.store.one("select * from org_surfaces where id = ?", (surface_id,))
        if not row:
            raise NotFound(f"surface {surface_id} not found")
        return self._surface_row(row)

    def _surface_row(self, row: Any) -> dict:
        return {"id": row["id"], "team_id": row["team_id"], "name": row["name"],
                "owner_department_id": row["owner_department_id"],
                "evidence": loads(row["evidence"], []), "purpose": row["purpose"],
                "created_date": row["created_date"]}

    def register_surface(self, team_id: str, name: str, owner_department_id: str,
                         evidence: list[str] | None = None, purpose: str = "") -> dict:
        """a surface is registered once, with one owner and the evidence its work is checked by.

        a department that cannot state its surface and its evidence in one row is not a
        department yet, it is an intention, so evidence is required.
        """
        name = " ".join(str(name or "").lower().split())
        if not name:
            raise ValidationError("a surface needs a name")
        evidence = [str(item).strip() for item in (evidence or []) if str(item).strip()]
        if not evidence:
            raise ValidationError(
                "a surface states the evidence its work is checked by, or it is an intention",
                surface=name)
        department = self.store.one("select * from departments where id = ?",
                                    (owner_department_id,))
        if not department or department["team_id"] != team_id:
            raise NotFound(f"department {owner_department_id} is not in crew {team_id}")
        existing = self.store.one(
            "select * from org_surfaces where team_id = ? and name = ?", (team_id, name))
        if existing:
            raise Conflict(
                f"surface {name} is already owned by {existing['owner_department_id']}",
                surface=name, owner=existing["owner_department_id"])
        surface_id = new_id("srf")
        with self.store.tx() as c:
            c.execute(
                """insert into org_surfaces (id, team_id, name, owner_department_id, evidence,
                       purpose, created_date) values (?,?,?,?,?,?,?)""",
                (surface_id, team_id, name, owner_department_id, dumps(evidence), purpose, now()))
        return self._surface(surface_id)

    def surfaces(self, team_id: str) -> list[dict]:
        return [self._surface_row(row) for row in self.store.query(
            "select * from org_surfaces where team_id = ? order by name", (team_id,))]

    def owner_of(self, team_id: str, name: str) -> dict | None:
        row = self.store.one(
            "select * from org_surfaces where team_id = ? and name = ?",
            (team_id, " ".join(str(name or "").lower().split())))
        return self._surface_row(row) if row else None

    # ---- who may act on a surface -------------------------------------
    def departments_of(self, agent_id: str) -> list[str]:
        return [row["department_id"] for row in self.store.query(
            "select department_id from department_members where agent_id = ?", (agent_id,))]

    def borrowed(self, agent_id: str, at: float | None = None) -> list[dict]:
        """the blocs this seat is on loan to right now. a lapsed grant is not a grant."""
        moment = at if at is not None else now()
        return [self._grant_row(row) for row in self.store.query(
            """select g.* from org_bloc_grants g where g.agent_id = ? and g.revoked_at is null
               and g.expires_at > ?""", (agent_id, moment))]

    def may_act(self, agent_id: str, surface_name: str, at: float | None = None) -> dict:
        """whether this seat may change this surface, and the reason in a sentence.

        three ways in, in order: the seat belongs to the owning department; the seat is on
        loan to a live bloc whose departments include the owner; or the surface is unowned.
        an unowned surface is allowed and reported as such, because refusing every card until
        every surface is registered would stop a crew that has not adopted the axis yet.
        """
        agent = self.store.one("select team_id from agents where id = ?", (agent_id,))
        if not agent:
            raise NotFound(f"agent {agent_id} not found")
        team_id = agent["team_id"]
        surface = self.owner_of(team_id, surface_name)
        if surface is None:
            return {"allowed": True, "via": "unowned", "surface": surface_name,
                    "reason": f"{surface_name} has no registered owner"}
        owner = surface["owner_department_id"]
        if owner in self.departments_of(agent_id):
            return {"allowed": True, "via": "owner", "surface": surface_name,
                    "owner_department_id": owner,
                    "reason": "the seat belongs to the department that owns it"}
        for grant in self.borrowed(agent_id, at=at):
            bloc = self._bloc(grant["bloc_id"])
            if owner in bloc["department_ids"]:
                return {"allowed": True, "via": "bloc", "surface": surface_name,
                        "owner_department_id": owner, "bloc_id": bloc["id"],
                        "until": grant["expires_at"],
                        "reason": f"on loan to bloc {bloc['name']} until the objective closes"}
        return {"allowed": False, "via": "none", "surface": surface_name,
                "owner_department_id": owner, "code": "surface_not_owned",
                "reason": (f"{surface_name} is owned by another department, so a change to it "
                           f"is a proposal and not an edit")}

    # ---- proposals: reaching into another surface ----------------------
    def _proposal(self, proposal_id: str) -> dict:
        row = self.store.one("select * from org_proposals where id = ?", (proposal_id,))
        if not row:
            raise NotFound(f"proposal {proposal_id} not found")
        return {"id": row["id"], "team_id": row["team_id"], "surface": row["surface"],
                "owner_department_id": row["owner_department_id"],
                "proposed_by": row["proposed_by"], "task_id": row["task_id"],
                "change": row["change"], "state": row["state"], "settled_by": row["settled_by"],
                "answer": row["answer"], "created_date": row["created_date"],
                "settled_date": row["settled_date"]}

    def propose(self, agent_id: str, surface_name: str, change: str,
                task_id: str | None = None) -> dict:
        """the lawful way across a boundary: write down the change and let the owner settle it."""
        change = " ".join(str(change or "").split())
        if not change:
            raise ValidationError("a proposal states the change it wants")
        agent = self.store.one("select team_id from agents where id = ?", (agent_id,))
        if not agent:
            raise NotFound(f"agent {agent_id} not found")
        surface = self.owner_of(agent["team_id"], surface_name)
        if surface is None:
            raise NotFound(f"{surface_name} has no owner to propose to")
        if self.may_act(agent_id, surface_name)["allowed"]:
            raise ValidationError(
                "this seat may already change that surface, so there is nothing to propose",
                surface=surface_name)
        proposal_id = new_id("prp")
        with self.store.tx() as c:
            c.execute(
                """insert into org_proposals (id, team_id, surface, owner_department_id,
                       proposed_by, task_id, change, state, created_date)
                   values (?,?,?,?,?,?,?,'open',?)""",
                (proposal_id, agent["team_id"], surface["name"], surface["owner_department_id"],
                 agent_id, task_id, change, now()))
        return self._proposal(proposal_id)

    def settle(self, proposal_id: str, by_agent_id: str, accept: bool, answer: str = "") -> dict:
        """only a seat in the owning department settles. the proposer cannot settle its own."""
        proposal = self._proposal(proposal_id)
        if proposal["state"] != "open":
            raise Conflict(f"proposal {proposal_id} is already {proposal['state']}",
                           state=proposal["state"])
        if by_agent_id == proposal["proposed_by"]:
            raise ValidationError("a proposal is settled by the owner, never by its author")
        if proposal["owner_department_id"] not in self.departments_of(by_agent_id):
            raise ValidationError(
                "only a seat in the owning department settles a proposal against its surface",
                owner=proposal["owner_department_id"])
        state = "accepted" if accept else "declined"
        with self.store.tx() as c:
            c.execute(
                """update org_proposals set state = ?, settled_by = ?, answer = ?,
                       settled_date = ? where id = ?""",
                (state, by_agent_id, str(answer or ""), now(), proposal_id))
        return self._proposal(proposal_id)

    def withdraw(self, proposal_id: str, by_agent_id: str) -> dict:
        proposal = self._proposal(proposal_id)
        if proposal["state"] != "open":
            raise Conflict(f"proposal {proposal_id} is already {proposal['state']}",
                           state=proposal["state"])
        if by_agent_id != proposal["proposed_by"]:
            raise ValidationError("only the author withdraws a proposal")
        with self.store.tx() as c:
            c.execute("update org_proposals set state = 'withdrawn', settled_date = ? "
                      "where id = ?", (now(), proposal_id))
        return self._proposal(proposal_id)

    def proposals(self, team_id: str, state: str | None = None,
                  task_id: str | None = None) -> list[dict]:
        if state is not None and state not in PROPOSAL_STATES:
            raise ValidationError(f"state must be one of {list(PROPOSAL_STATES)}", state=state)
        sql = "select id from org_proposals where team_id = ?"
        args: list[Any] = [team_id]
        if state:
            sql += " and state = ?"
            args.append(state)
        if task_id:
            sql += " and task_id = ?"
            args.append(task_id)
        return [self._proposal(row["id"]) for row in self.store.query(
            sql + " order by created_date desc", tuple(args))]

    # ---- blocs: a temporary alliance around one objective --------------
    def _bloc(self, bloc_id: str) -> dict:
        row = self.store.one("select * from org_blocs where id = ?", (bloc_id,))
        if not row:
            raise NotFound(f"bloc {bloc_id} not found")
        return {"id": row["id"], "team_id": row["team_id"], "name": row["name"],
                "objective": row["objective"],
                "department_ids": loads(row["department_ids"], []),
                "state": row["state"], "closes_at": row["closes_at"],
                "created_date": row["created_date"], "closed_date": row["closed_date"]}

    def _grant_row(self, row: Any) -> dict:
        return {"id": row["id"], "bloc_id": row["bloc_id"], "agent_id": row["agent_id"],
                "expires_at": row["expires_at"], "revoked_at": row["revoked_at"],
                "created_date": row["created_date"]}

    def form_bloc(self, team_id: str, name: str, objective: str, department_ids: list[str],
                  closes_at: float) -> dict:
        """a bloc exists for the length of an objective. a bloc with no end is a department
        nobody named, and it will start protecting itself, so the end is required."""
        name = " ".join(str(name or "").split())
        objective = " ".join(str(objective or "").split())
        if not name:
            raise ValidationError("a bloc needs a name")
        if not objective:
            raise ValidationError("a bloc exists for one objective, so it names it")
        if not closes_at or float(closes_at) <= now():
            raise ValidationError("a bloc has an end in the future, or it is a department")
        wanted = [str(item) for item in (department_ids or [])]
        if len(set(wanted)) < 2:
            raise ValidationError("a bloc is an alliance of at least two departments")
        for department_id in wanted:
            row = self.store.one("select team_id from departments where id = ?", (department_id,))
            if not row or row["team_id"] != team_id:
                raise NotFound(f"department {department_id} is not in crew {team_id}")
        bloc_id = new_id("blc")
        with self.store.tx() as c:
            c.execute(
                """insert into org_blocs (id, team_id, name, objective, department_ids, state,
                       closes_at, created_date) values (?,?,?,?,?,'open',?,?)""",
                (bloc_id, team_id, name, objective, dumps(sorted(set(wanted))),
                 float(closes_at), now()))
        return self._bloc(bloc_id)

    def lend(self, bloc_id: str, agent_id: str, expires_at: float | None = None) -> dict:
        """lend one seat to a bloc. the grant can never outlive the bloc's own end, and the
        seat keeps its home department throughout."""
        bloc = self._bloc(bloc_id)
        if bloc["state"] != "open":
            raise Conflict(f"bloc {bloc_id} is {bloc['state']}", state=bloc["state"])
        agent = self.store.one("select team_id from agents where id = ?", (agent_id,))
        if not agent or agent["team_id"] != bloc["team_id"]:
            raise NotFound(f"agent {agent_id} is not in crew {bloc['team_id']}")
        if not self.departments_of(agent_id):
            raise ValidationError("a seat is lent from a department, and this one has none",
                                  agent=agent_id)
        ends = float(expires_at) if expires_at else float(bloc["closes_at"])
        ends = min(ends, float(bloc["closes_at"]))
        if ends <= now():
            raise ValidationError("a grant that has already expired lends nothing")
        existing = self.store.one(
            """select id from org_bloc_grants where bloc_id = ? and agent_id = ?
               and revoked_at is null and expires_at > ?""", (bloc_id, agent_id, now()))
        if existing:
            raise Conflict("that seat is already on loan to this bloc", grant=existing["id"])
        grant_id = new_id("grt")
        with self.store.tx() as c:
            c.execute(
                """insert into org_bloc_grants (id, bloc_id, agent_id, expires_at, created_date)
                   values (?,?,?,?,?)""", (grant_id, bloc_id, agent_id, ends, now()))
        return self._grant_row(self.store.one(
            "select * from org_bloc_grants where id = ?", (grant_id,)))

    def close_bloc(self, bloc_id: str) -> dict:
        """the objective closed. every grant ends with it and the seats go home."""
        bloc = self._bloc(bloc_id)
        if bloc["state"] == "closed":
            return bloc
        moment = now()
        with self.store.tx() as c:
            c.execute("update org_blocs set state = 'closed', closed_date = ? where id = ?",
                      (moment, bloc_id))
            c.execute("update org_bloc_grants set revoked_at = ? where bloc_id = ? "
                      "and revoked_at is null", (moment, bloc_id))
        return self._bloc(bloc_id)

    def blocs(self, team_id: str, include_closed: bool = False) -> list[dict]:
        sql = "select id from org_blocs where team_id = ?"
        if not include_closed:
            sql += " and state = 'open'"
        return [self._bloc(row["id"]) for row in self.store.query(
            sql + " order by created_date desc", (team_id,))]

    def overdue_blocs(self, team_id: str, at: float | None = None) -> list[dict]:
        """a bloc past its end that nobody closed. the sweep names it rather than closing it,
        because whether the objective actually landed is somebody's decision."""
        moment = at if at is not None else now()
        return [bloc for bloc in self.blocs(team_id) if float(bloc["closes_at"]) <= moment]

    # ---- wings: a veto in a domain, and nothing to grow ----------------
    def _wing(self, wing_id: str) -> dict:
        row = self.store.one("select * from org_wings where id = ?", (wing_id,))
        if not row:
            raise NotFound(f"wing {wing_id} not found")
        return {"id": row["id"], "team_id": row["team_id"], "name": row["name"],
                "domain": row["domain"], "agent_ids": loads(row["agent_ids"], []),
                "cadence": row["cadence"], "created_date": row["created_date"]}

    def create_wing(self, team_id: str, name: str, domain: str, agent_ids: list[str],
                    cadence: str = "", **forbidden: Any) -> dict:
        """a wing holds one capability the whole org needs and no metric rewards.

        anything that would give it something to grow is refused by name, because a wing with a
        growth target stops being a wing and the refusal is cheaper than the drift.
        """
        given = [key for key in WING_FORBIDDEN if forbidden.get(key) not in (None, "", 0)]
        if given:
            raise ValidationError(
                "a wing has a veto and nothing to grow, so it takes no " + ", ".join(given),
                fields=given)
        name = " ".join(str(name or "").split())
        domain = " ".join(str(domain or "").lower().split())
        if not name or not domain:
            raise ValidationError("a wing names itself and the domain it can veto in")
        members = [str(item) for item in (agent_ids or [])]
        if not members:
            raise ValidationError("a wing with nobody in it vetoes nothing")
        for agent_id in members:
            row = self.store.one("select team_id from agents where id = ?", (agent_id,))
            if not row or row["team_id"] != team_id:
                raise NotFound(f"agent {agent_id} is not in crew {team_id}")
        if self.store.one("select id from org_wings where team_id = ? and domain = ?",
                          (team_id, domain)):
            raise Conflict(f"a wing already holds the {domain} domain", domain=domain)
        wing_id = new_id("wng")
        with self.store.tx() as c:
            c.execute(
                """insert into org_wings (id, team_id, name, domain, agent_ids, cadence,
                       created_date) values (?,?,?,?,?,?,?)""",
                (wing_id, team_id, name, domain, dumps(sorted(set(members))), cadence, now()))
        return self._wing(wing_id)

    def wings(self, team_id: str) -> list[dict]:
        return [self._wing(row["id"]) for row in self.store.query(
            "select id from org_wings where team_id = ? order by name", (team_id,))]

    def veto(self, wing_id: str, by_agent_id: str, task_id: str, reason: str) -> dict:
        """a wing's veto on one card in its domain. it stops the card and it states why, and it
        is a record: the same card can be un-vetoed by the same wing, and by nobody else."""
        wing = self._wing(wing_id)
        if by_agent_id not in wing["agent_ids"]:
            raise ValidationError("only a seat in the wing can veto in its domain",
                                  wing=wing_id)
        reason = " ".join(str(reason or "").split())
        if not reason:
            raise ValidationError("a veto says why, or it is only an obstacle")
        task = self.store.one("select id, team_id from tasks where id = ?", (task_id,))
        if not task or task["team_id"] != wing["team_id"]:
            raise NotFound(f"card {task_id} is not in crew {wing['team_id']}")
        live = self.store.one(
            "select id from org_vetoes where task_id = ? and wing_id = ? and lifted_date is null",
            (task_id, wing_id))
        if live:
            raise Conflict("that wing already holds a veto on this card", veto=live["id"])
        veto_id = new_id("vto")
        with self.store.tx() as c:
            c.execute(
                """insert into org_vetoes (id, team_id, wing_id, task_id, by_agent_id, reason,
                       created_date) values (?,?,?,?,?,?,?)""",
                (veto_id, wing["team_id"], wing_id, task_id, by_agent_id, reason, now()))
        return self._veto(veto_id)

    def _veto(self, veto_id: str) -> dict:
        row = self.store.one("select * from org_vetoes where id = ?", (veto_id,))
        if not row:
            raise NotFound(f"veto {veto_id} not found")
        return {"id": row["id"], "team_id": row["team_id"], "wing_id": row["wing_id"],
                "task_id": row["task_id"], "by_agent_id": row["by_agent_id"],
                "reason": row["reason"], "created_date": row["created_date"],
                "lifted_date": row["lifted_date"], "lifted_by": row["lifted_by"]}

    def lift(self, veto_id: str, by_agent_id: str) -> dict:
        veto = self._veto(veto_id)
        if veto["lifted_date"]:
            raise Conflict("that veto is already lifted", veto=veto_id)
        wing = self._wing(veto["wing_id"])
        if by_agent_id not in wing["agent_ids"]:
            raise ValidationError("a veto is lifted by the wing that placed it, and nobody else")
        with self.store.tx() as c:
            c.execute("update org_vetoes set lifted_date = ?, lifted_by = ? where id = ?",
                      (now(), by_agent_id, veto_id))
        return self._veto(veto_id)

    def vetoes(self, team_id: str, task_id: str | None = None,
               live_only: bool = True) -> list[dict]:
        sql = "select id from org_vetoes where team_id = ?"
        args: list[Any] = [team_id]
        if task_id:
            sql += " and task_id = ?"
            args.append(task_id)
        if live_only:
            sql += " and lifted_date is null"
        return [self._veto(row["id"]) for row in self.store.query(
            sql + " order by created_date desc", tuple(args))]

    # ---- layers: the question each seat may answer ---------------------
    def layer_of(self, agent_id: str) -> str:
        row = self.store.one("select runtime from agents where id = ?", (agent_id,))
        if not row:
            raise NotFound(f"agent {agent_id} not found")
        layer = (loads(row["runtime"], {}) or {}).get("layer") or "seat"
        layer_rank(layer)
        return layer

    def check_directive(self, text: str) -> dict:
        """a directive that names a function is a spec in disguise. it says how, and how is the
        question one layer down, so the refusal names the fragments that gave it away."""
        shapes = reads_as_spec(text)
        return {"ok": not shapes, "spec_shapes": shapes,
                "reason": ("a directive says why and what is refused, never how" if shapes
                           else "reads as a directive")}

    def may_declare(self, agent_id: str, layer: str) -> dict:
        """a seat may declare what its own layer or a lower one answers, and nothing above it."""
        own = self.layer_of(agent_id)
        allowed = layer_rank(layer) >= layer_rank(own)
        return {"allowed": allowed, "own": own, "asked": layer,
                "reason": ("within the layer's ceiling" if allowed else
                           f"a {own} may not answer a {layer} question")}

    # ---- the reading a dispatch is allowed to see ----------------------
    def for_dispatch(self, task: dict, agent: dict) -> dict:
        """what the org axis says about this card and this seat, and whether the crew asked for
        it to be a refusal. a card naming no surface reads as clear: the axis is opt-in."""
        task = dict(task)
        agent = dict(agent)
        payload = task.get("payload") or {}
        if isinstance(payload, str):
            payload = loads(payload, {})
        surface = (payload or {}).get("surface") if isinstance(payload, dict) else None
        findings: list[dict] = []
        if surface:
            access = self.may_act(agent["id"], str(surface))
            if not access["allowed"]:
                findings.append({"code": access["code"], "surface": access["surface"],
                                 "owner_department_id": access["owner_department_id"],
                                 "reason": access["reason"]})
        for veto in self.vetoes(task["team_id"], task_id=task["id"]):
            findings.append({"code": "vetoed", "veto": veto["id"], "wing": veto["wing_id"],
                             "reason": veto["reason"]})
        return {"verdict": "blocked" if findings else "clear", "findings": findings,
                "enforced": self.enforced(task["team_id"])}

    def check_dispatch(self, task: dict, agent: dict) -> dict:
        """a crew that asked for enforcement gets a refusal that names the rule it hit. a veto
        always refuses, because a veto that can be ignored is a suggestion."""
        checked = self.for_dispatch(task, agent)
        vetoed = [f for f in checked["findings"] if f["code"] == "vetoed"]
        if vetoed:
            raise Conflict(f"a wing has vetoed this card: {vetoed[0]['reason']}",
                           code="vetoed", findings=checked["findings"])
        if checked["verdict"] == "blocked" and checked["enforced"]:
            first = checked["findings"][0]
            raise Conflict(first["reason"], code=first["code"], findings=checked["findings"])
        return checked

    # ---- the whole second axis in one reading --------------------------
    def reading(self, team_id: str, at: float | None = None) -> dict:
        moment = at if at is not None else now()
        return {
            "team_id": team_id,
            "enforced": self.enforced(team_id),
            "surfaces": self.surfaces(team_id),
            "open_proposals": self.proposals(team_id, state="open"),
            "blocs": self.blocs(team_id),
            "overdue_blocs": self.overdue_blocs(team_id, at=moment),
            "wings": self.wings(team_id),
            "live_vetoes": self.vetoes(team_id),
            "unowned_departments": self.departments_without_surface(team_id),
        }

    def departments_without_surface(self, team_id: str) -> list[dict]:
        """a department that owns no surface is not a department yet, it is an intention."""
        rows = self.store.query(
            """select d.id, d.name, d.slug from departments d where d.team_id = ?
               and not exists (select 1 from org_surfaces s where s.owner_department_id = d.id)
               order by d.name""", (team_id,))
        return [{"id": row["id"], "name": row["name"], "slug": row["slug"]} for row in rows]
