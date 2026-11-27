"""the second axis of an org chart: who owns a surface, who may borrow a seat.

the properties under test are the ones the owner's own org notes state and the ones that make the
axis worth having: a surface has exactly one owner, reaching across is a proposal only the owner
settles, a bloc lends seats and the seats go home, a wing has a veto and nothing to grow, and a
veto refuses even when the crew never asked for enforcement.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from base import AppCase  # noqa: E402
from core.db import new_id, now  # noqa: E402
from core.errors import Conflict, NotFound, ValidationError  # noqa: E402
from org import axis as ax  # noqa: E402


class PureTests(unittest.TestCase):
    def test_layers_are_ordered_from_the_widest_question_down(self):
        self.assertLess(ax.layer_rank("directive"), ax.layer_rank("division"))
        self.assertLess(ax.layer_rank("division"), ax.layer_rank("seat"))

    def test_an_unknown_layer_is_refused(self):
        with self.assertRaises(ValidationError):
            ax.layer_rank("emperor")

    def test_a_directive_naming_a_function_reads_as_a_spec(self):
        self.assertIn("()", ax.reads_as_spec("call refund_order() when angry"))

    def test_a_directive_naming_a_route_reads_as_a_spec(self):
        self.assertTrue(ax.reads_as_spec("post to /api/refunds"))

    def test_a_plain_directive_reads_as_one(self):
        self.assertEqual(ax.reads_as_spec("we never contact a customer without consent"), [])


class AxisCase(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew()
        self.tid = self.team["id"]
        crews = self.app.crews
        self.anne = crews.add_agent(self.tid, "anne", role="doer")
        self.ben = crews.add_agent(self.tid, "ben", role="doer")
        self.cy = crews.add_agent(self.tid, "cy", role="reviewer")
        self.support = self.app.departments.create(
            self.tid, "support", coordinator_agent_id=self.anne["id"])
        self.code = self.app.departments.create(
            self.tid, "code", coordinator_agent_id=self.ben["id"])
        self.docs = self.app.departments.create(self.tid, "docs", members=[self.cy["id"]])

    def surface(self, name="code.repo", owner=None, evidence=None) -> dict:
        return self.app.axis.register_surface(
            self.tid, name, (owner or self.code)["id"], evidence or ["commit:"])

    def card(self, surface=None, title="ship it") -> dict:
        payload = {"surface": surface} if surface else {}
        return self.app.kanban.create_task(self.tid, title, description="x", column="ready",
                                           payload=payload)


class SurfaceTests(AxisCase):
    def test_a_surface_is_registered_with_one_owner(self):
        surface = self.surface()
        self.assertEqual(surface["owner_department_id"], self.code["id"])

    def test_a_surface_needs_evidence_or_it_is_an_intention(self):
        with self.assertRaises(ValidationError):
            self.app.axis.register_surface(self.tid, "code.repo", self.code["id"], [])

    def test_a_surface_cannot_have_two_owners(self):
        self.surface()
        with self.assertRaises(Conflict):
            self.surface(owner=self.support)

    def test_a_surface_name_is_case_and_space_insensitive(self):
        self.surface("Code.Repo")
        self.assertIsNotNone(self.app.axis.owner_of(self.tid, "  code.repo "))

    def test_an_owner_must_be_a_department_in_this_crew(self):
        other = self.crew("elsewhere")
        foreign = self.app.departments.create(other["id"], "legal")
        with self.assertRaises(NotFound):
            self.app.axis.register_surface(self.tid, "x", foreign["id"], ["file:"])


class AccessTests(AxisCase):
    def test_a_member_of_the_owning_department_may_act(self):
        self.surface()
        got = self.app.axis.may_act(self.ben["id"], "code.repo")
        self.assertTrue(got["allowed"])
        self.assertEqual(got["via"], "owner")

    def test_a_seat_in_another_department_may_not(self):
        self.surface()
        got = self.app.axis.may_act(self.anne["id"], "code.repo")
        self.assertFalse(got["allowed"])
        self.assertEqual(got["code"], "surface_not_owned")

    def test_an_unowned_surface_is_allowed_and_says_so(self):
        got = self.app.axis.may_act(self.anne["id"], "nobody.owns.this")
        self.assertTrue(got["allowed"])
        self.assertEqual(got["via"], "unowned")

    def test_a_department_owning_nothing_is_reported_as_an_intention(self):
        self.surface()
        names = [d["slug"] for d in self.app.axis.departments_without_surface(self.tid)]
        self.assertIn("support", names)
        self.assertNotIn("code", names)


class ProposalTests(AxisCase):
    def setUp(self) -> None:
        super().setUp()
        self.surface()

    def test_reaching_across_is_a_recorded_proposal(self):
        proposal = self.app.axis.propose(self.anne["id"], "code.repo", "rename the helper")
        self.assertEqual(proposal["state"], "open")
        self.assertEqual(proposal["owner_department_id"], self.code["id"])

    def test_a_seat_that_already_owns_it_has_nothing_to_propose(self):
        with self.assertRaises(ValidationError):
            self.app.axis.propose(self.ben["id"], "code.repo", "rename the helper")

    def test_a_proposal_needs_a_change(self):
        with self.assertRaises(ValidationError):
            self.app.axis.propose(self.anne["id"], "code.repo", "  ")

    def test_a_proposal_to_an_unowned_surface_has_no_one_to_go_to(self):
        with self.assertRaises(NotFound):
            self.app.axis.propose(self.anne["id"], "nobody.owns.this", "a change")

    def test_the_owner_settles_it(self):
        proposal = self.app.axis.propose(self.anne["id"], "code.repo", "rename the helper")
        settled = self.app.axis.settle(proposal["id"], self.ben["id"], True, "fine")
        self.assertEqual(settled["state"], "accepted")
        self.assertEqual(settled["settled_by"], self.ben["id"])

    def test_the_author_cannot_settle_its_own_proposal(self):
        proposal = self.app.axis.propose(self.anne["id"], "code.repo", "rename the helper")
        with self.assertRaises(ValidationError):
            self.app.axis.settle(proposal["id"], self.anne["id"], True)

    def test_a_seat_outside_the_owning_department_cannot_settle(self):
        proposal = self.app.axis.propose(self.anne["id"], "code.repo", "rename the helper")
        with self.assertRaises(ValidationError):
            self.app.axis.settle(proposal["id"], self.cy["id"], True)

    def test_a_settled_proposal_stays_settled(self):
        proposal = self.app.axis.propose(self.anne["id"], "code.repo", "rename the helper")
        self.app.axis.settle(proposal["id"], self.ben["id"], False, "no")
        with self.assertRaises(Conflict):
            self.app.axis.settle(proposal["id"], self.ben["id"], True)

    def test_only_the_author_withdraws(self):
        proposal = self.app.axis.propose(self.anne["id"], "code.repo", "rename the helper")
        with self.assertRaises(ValidationError):
            self.app.axis.withdraw(proposal["id"], self.ben["id"])
        self.assertEqual(self.app.axis.withdraw(proposal["id"], self.anne["id"])["state"],
                         "withdrawn")

    def test_proposals_list_by_state(self):
        self.app.axis.propose(self.anne["id"], "code.repo", "one")
        self.assertEqual(len(self.app.axis.proposals(self.tid, state="open")), 1)
        self.assertEqual(self.app.axis.proposals(self.tid, state="accepted"), [])
        with self.assertRaises(ValidationError):
            self.app.axis.proposals(self.tid, state="sulking")


class BlocTests(AxisCase):
    def setUp(self) -> None:
        super().setUp()
        self.surface()
        self.end = now() + 3600

    def bloc(self, **kw) -> dict:
        args = dict(name="launch", objective="ship the storefront",
                    department_ids=[self.support["id"], self.code["id"]], closes_at=self.end)
        args.update(kw)
        return self.app.axis.form_bloc(self.tid, **args)

    def test_a_bloc_needs_an_objective(self):
        with self.assertRaises(ValidationError):
            self.bloc(objective="")

    def test_a_bloc_needs_an_end_in_the_future(self):
        with self.assertRaises(ValidationError):
            self.bloc(closes_at=now() - 5)

    def test_a_bloc_is_an_alliance_of_two_departments_at_least(self):
        with self.assertRaises(ValidationError):
            self.bloc(department_ids=[self.code["id"]])

    def test_a_lent_seat_may_act_on_the_borrowed_surface(self):
        bloc = self.bloc()
        self.app.axis.lend(bloc["id"], self.anne["id"])
        got = self.app.axis.may_act(self.anne["id"], "code.repo")
        self.assertTrue(got["allowed"])
        self.assertEqual(got["via"], "bloc")

    def test_a_grant_cannot_outlive_the_bloc(self):
        bloc = self.bloc()
        grant = self.app.axis.lend(bloc["id"], self.anne["id"], expires_at=self.end + 99999)
        self.assertEqual(grant["expires_at"], self.end)

    def test_a_seat_with_no_home_department_cannot_be_lent(self):
        loner = self.app.crews.add_agent(self.tid, "loner", role="doer")
        with self.assertRaises(ValidationError):
            self.app.axis.lend(self.bloc()["id"], loner["id"])

    def test_the_same_seat_is_not_lent_twice(self):
        bloc = self.bloc()
        self.app.axis.lend(bloc["id"], self.anne["id"])
        with self.assertRaises(Conflict):
            self.app.axis.lend(bloc["id"], self.anne["id"])

    def test_the_seats_go_home_when_the_bloc_closes(self):
        bloc = self.bloc()
        self.app.axis.lend(bloc["id"], self.anne["id"])
        self.app.axis.close_bloc(bloc["id"])
        got = self.app.axis.may_act(self.anne["id"], "code.repo")
        self.assertFalse(got["allowed"])

    def test_a_closed_bloc_lends_nothing_more(self):
        bloc = self.bloc()
        self.app.axis.close_bloc(bloc["id"])
        with self.assertRaises(Conflict):
            self.app.axis.lend(bloc["id"], self.anne["id"])

    def test_a_lapsed_grant_is_not_a_grant(self):
        bloc = self.bloc()
        self.app.axis.lend(bloc["id"], self.anne["id"])
        later = self.end + 10
        self.assertFalse(self.app.axis.may_act(self.anne["id"], "code.repo", at=later)["allowed"])

    def test_a_bloc_past_its_end_is_named_and_not_closed_for_you(self):
        bloc = self.bloc()
        overdue = self.app.axis.overdue_blocs(self.tid, at=self.end + 10)
        self.assertEqual([b["id"] for b in overdue], [bloc["id"]])
        self.assertEqual(self.app.axis._bloc(bloc["id"])["state"], "open")

    def test_a_bloc_that_covers_another_department_does_not_cover_a_third(self):
        bloc = self.bloc()
        self.app.axis.lend(bloc["id"], self.anne["id"])
        self.app.axis.register_surface(self.tid, "docs.site", self.docs["id"], ["url:"])
        self.assertFalse(self.app.axis.may_act(self.anne["id"], "docs.site")["allowed"])


class WingTests(AxisCase):
    def wing(self, **kw) -> dict:
        args = dict(name="security", domain="security", agent_ids=[self.cy["id"]])
        args.update(kw)
        return self.app.axis.create_wing(self.tid, **args)

    def test_a_wing_has_a_domain_and_members(self):
        self.assertEqual(self.wing()["agent_ids"], [self.cy["id"]])

    def test_a_wing_refuses_a_budget(self):
        with self.assertRaises(ValidationError) as caught:
            self.wing(budget_cents=5000)
        self.assertIn("budget_cents", caught.exception.details["fields"])

    def test_a_wing_refuses_a_growth_target(self):
        with self.assertRaises(ValidationError):
            self.wing(growth_target=10)

    def test_a_wing_with_nobody_vetoes_nothing(self):
        with self.assertRaises(ValidationError):
            self.wing(agent_ids=[])

    def test_one_wing_per_domain(self):
        self.wing()
        with self.assertRaises(Conflict):
            self.wing(name="appsec")

    def test_a_wing_member_vetoes_a_card_and_says_why(self):
        wing = self.wing()
        card = self.card()
        veto = self.app.axis.veto(wing["id"], self.cy["id"], card["id"], "leaks a key")
        self.assertEqual(veto["reason"], "leaks a key")

    def test_a_veto_without_a_reason_is_only_an_obstacle(self):
        wing = self.wing()
        with self.assertRaises(ValidationError):
            self.app.axis.veto(wing["id"], self.cy["id"], self.card()["id"], " ")

    def test_someone_outside_the_wing_cannot_veto(self):
        wing = self.wing()
        with self.assertRaises(ValidationError):
            self.app.axis.veto(wing["id"], self.anne["id"], self.card()["id"], "no")

    def test_a_card_holds_one_live_veto_per_wing(self):
        wing = self.wing()
        card = self.card()
        self.app.axis.veto(wing["id"], self.cy["id"], card["id"], "one")
        with self.assertRaises(Conflict):
            self.app.axis.veto(wing["id"], self.cy["id"], card["id"], "two")

    def test_only_the_wing_lifts_its_own_veto(self):
        wing = self.wing()
        veto = self.app.axis.veto(wing["id"], self.cy["id"], self.card()["id"], "no")
        with self.assertRaises(ValidationError):
            self.app.axis.lift(veto["id"], self.anne["id"])
        self.assertIsNotNone(self.app.axis.lift(veto["id"], self.cy["id"])["lifted_date"])

    def test_a_lifted_veto_leaves_the_live_list(self):
        wing = self.wing()
        card = self.card()
        veto = self.app.axis.veto(wing["id"], self.cy["id"], card["id"], "no")
        self.app.axis.lift(veto["id"], self.cy["id"])
        self.assertEqual(self.app.axis.vetoes(self.tid, task_id=card["id"]), [])
        self.assertEqual(len(self.app.axis.vetoes(self.tid, task_id=card["id"], live_only=False)), 1)


class LayerTests(AxisCase):
    def test_a_seat_defaults_to_the_seat_layer(self):
        self.assertEqual(self.app.axis.layer_of(self.anne["id"]), "seat")

    def test_a_seat_may_not_answer_a_directive_question(self):
        got = self.app.axis.may_declare(self.anne["id"], "directive")
        self.assertFalse(got["allowed"])

    def test_a_seat_may_declare_at_its_own_layer(self):
        self.assertTrue(self.app.axis.may_declare(self.anne["id"], "seat")["allowed"])

    def test_a_directive_naming_a_function_is_refused_as_a_spec(self):
        got = self.app.axis.check_directive("call refund_order() on every complaint")
        self.assertFalse(got["ok"])
        self.assertIn("()", got["spec_shapes"])

    def test_a_plain_directive_passes(self):
        self.assertTrue(self.app.axis.check_directive("we refuse to publish a price unreviewed")["ok"])


class DispatchTests(AxisCase):
    def setUp(self) -> None:
        super().setUp()
        self.surface()

    def run_card(self, card, agent):
        self.app.kanban.assign(card["id"], agent["id"])
        return self.app.runner.dispatch(card["id"], max_turns=1)

    def test_a_card_naming_no_surface_reads_as_clear(self):
        out = self.run_card(self.card(), self.anne)
        self.assertEqual(out["axis"]["verdict"], "clear")

    def test_a_card_on_a_surface_the_seat_does_not_own_is_recorded_and_still_runs(self):
        out = self.run_card(self.card("code.repo"), self.anne)
        self.assertTrue(out["run_id"])
        self.assertEqual(out["axis"]["verdict"], "blocked")
        self.assertEqual(out["axis"]["findings"][0]["code"], "surface_not_owned")
        self.assertFalse(out["axis"]["enforced"])

    def test_the_owner_runs_its_own_surface_clean(self):
        out = self.run_card(self.card("code.repo"), self.ben)
        self.assertEqual(out["axis"]["verdict"], "clear")

    def test_an_enforcing_crew_refuses_the_card_before_it_runs(self):
        self.app.crews.update_team(self.tid, orchestration={"org": {"enforce": True}})
        card = self.card("code.repo")
        self.app.kanban.assign(card["id"], self.anne["id"])
        with self.assertRaises(Conflict) as caught:
            self.app.runner.dispatch(card["id"], max_turns=1)
        self.assertEqual(caught.exception.details["code"], "surface_not_owned")

    def test_a_veto_refuses_even_when_the_crew_never_asked_for_enforcement(self):
        wing = self.app.axis.create_wing(self.tid, "security", "security", [self.cy["id"]])
        card = self.card()
        self.app.axis.veto(wing["id"], self.cy["id"], card["id"], "leaks a key")
        self.app.kanban.assign(card["id"], self.ben["id"])
        with self.assertRaises(Conflict) as caught:
            self.app.runner.dispatch(card["id"], max_turns=1)
        self.assertEqual(caught.exception.details["code"], "vetoed")

    def test_lifting_the_veto_lets_the_card_run(self):
        wing = self.app.axis.create_wing(self.tid, "security", "security", [self.cy["id"]])
        card = self.card()
        veto = self.app.axis.veto(wing["id"], self.cy["id"], card["id"], "leaks a key")
        self.app.axis.lift(veto["id"], self.cy["id"])
        out = self.run_card(card, self.ben)
        self.assertEqual(out["axis"]["verdict"], "clear")

    def test_a_lent_seat_runs_the_borrowed_surface_clean_under_enforcement(self):
        self.app.crews.update_team(self.tid, orchestration={"org": {"enforce": True}})
        bloc = self.app.axis.form_bloc(self.tid, "launch", "ship it",
                                       [self.support["id"], self.code["id"]], now() + 3600)
        self.app.axis.lend(bloc["id"], self.anne["id"])
        out = self.run_card(self.card("code.repo"), self.anne)
        self.assertEqual(out["axis"]["verdict"], "clear")


class RouteTests(AxisCase):
    def test_the_routes_answer(self):
        from fastapi.testclient import TestClient
        from api.routes import create_api
        client = TestClient(create_api(self.app))
        made = client.post(f"/crews/{self.tid}/org/surfaces", json={
            "name": "code.repo", "owner_department_id": self.code["id"],
            "evidence": ["commit:"]}).json()
        self.assertEqual(made["name"], "code.repo")
        access = client.get(
            f"/crews/{self.tid}/org/surfaces/code.repo/access/{self.anne['id']}").json()
        self.assertFalse(access["allowed"])
        proposal = client.post(f"/crews/{self.tid}/org/proposals", json={
            "agent_id": self.anne["id"], "surface": "code.repo", "change": "rename"}).json()
        settled = client.post(f"/org/proposals/{proposal['id']}/settle", json={
            "agent_id": self.ben["id"], "accept": True, "answer": "ok"}).json()
        self.assertEqual(settled["state"], "accepted")
        bloc = client.post(f"/crews/{self.tid}/org/blocs", json={
            "name": "launch", "objective": "ship", "closes_at": now() + 3600,
            "department_ids": [self.support["id"], self.code["id"]]}).json()
        self.assertEqual(bloc["state"], "open")
        wing = client.post(f"/crews/{self.tid}/org/wings", json={
            "name": "security", "domain": "security", "agent_ids": [self.cy["id"]]}).json()
        self.assertEqual(wing["domain"], "security")
        refused = client.post(f"/crews/{self.tid}/org/wings", json={
            "name": "growth", "domain": "growth", "agent_ids": [self.cy["id"]],
            "budget_cents": 100})
        self.assertEqual(refused.status_code, 422)
        reading = client.get(f"/crews/{self.tid}/org/axis").json()
        self.assertEqual(len(reading["surfaces"]), 1)
        self.assertEqual(len(reading["wings"]), 1)
