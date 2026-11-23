"""a crew as a document, and the smallest crew that can close a card.

the property worth holding: a template carries what a crew knows and how it is staffed, and
cannot carry what it can reach. an export that ships permissions is not a template, it is a
copy of somebody's authority with a new name on it.

the second property is the one the skeleton exists to state: a crew of one cannot finish a
card here, because the platform refuses to let a seat sign off its own work.
"""

from __future__ import annotations

import unittest

import base  # noqa: F401

from base import AppCase

from core.errors import Conflict, ValidationError
from work import templates


class DocumentTests(AppCase):
    def test_the_skeleton_is_two_seats_and_says_why(self):
        doc = templates.skeleton("starter")
        self.assertEqual([a["name"] for a in doc["agents"]], ["doer", "reviewer"])
        self.assertEqual({a["role"] for a in doc["agents"]}, {"doer", "reviewer"})
        self.assertEqual(doc["schema"], templates.SCHEMA)
        # the rails are the pair's ceiling, not a suggestion
        self.assertEqual([r["kind"] for r in doc["rails"]],
                         ["open_cards", "spend_per_day", "forbidden_tools"])

    def test_a_document_with_no_agents_is_refused(self):
        with self.assertRaises(ValidationError) as caught:
            templates.validate({"schema": templates.SCHEMA, "agents": []})
        self.assertEqual(caught.exception.details.get("reason"), "no_agents")

    def test_an_unknown_schema_is_refused(self):
        with self.assertRaises(ValidationError) as caught:
            templates.validate({"schema": "crittertown.crew/99", "agents": [{"name": "a"}]})
        self.assertIn("crittertown.crew/1", str(caught.exception))

    def test_a_template_cannot_carry_permissions(self):
        with self.assertRaises(ValidationError) as caught:
            templates.validate({
                "schema": templates.SCHEMA,
                "agents": [{"name": "wren", "role": "doer",
                            "scope": {"permissions": {"spend": True}}}],
            })
        self.assertEqual(caught.exception.details.get("reason"), "unknown_agent_field")

    def test_a_template_cannot_carry_a_credential(self):
        with self.assertRaises(ValidationError) as caught:
            templates.validate({
                "schema": templates.SCHEMA,
                "agents": [{"name": "wren", "role": "doer",
                            "runtime": {"api_key": "sk-live"}}],
            })
        self.assertEqual(caught.exception.details.get("reason"), "unknown_agent_field")

    def test_a_template_cannot_open_egress_to_everything(self):
        with self.assertRaises(ValidationError) as caught:
            templates.validate({
                "schema": templates.SCHEMA,
                "agents": [{"name": "wren", "role": "doer"}],
                "rails": [{"kind": "egress_hosts", "value": ["*"]}],
            })
        self.assertEqual(caught.exception.details.get("reason"), "template_widens_egress")

    def test_a_rail_may_not_name_a_stranger(self):
        with self.assertRaises(ValidationError) as caught:
            templates.validate({
                "schema": templates.SCHEMA,
                "agents": [{"name": "wren", "role": "doer"}],
                "rails": [{"kind": "open_cards", "value": 3, "agent": "somebody-else"}],
            })
        self.assertEqual(caught.exception.details.get("reason"), "rail_names_a_stranger")

    def test_an_unknown_rail_kind_is_refused(self):
        with self.assertRaises(ValidationError) as caught:
            templates.validate({
                "schema": templates.SCHEMA,
                "agents": [{"name": "wren", "role": "doer"}],
                "rails": [{"kind": "vibes", "value": 1}],
            })
        self.assertEqual(caught.exception.details.get("reason"), "unknown_rail_kind")

    def test_a_worker_we_do_not_run_cannot_be_hired_as_a_reviewer(self):
        with self.assertRaises(ValidationError) as caught:
            templates.validate({
                "schema": templates.SCHEMA,
                "agents": [{"name": "kilo", "role": "reviewer", "hosting": "external"}],
            })
        self.assertEqual(caught.exception.details.get("reason"), "agent_cannot_hold_seat")

    def test_a_kind_cannot_fill_a_seat_it_never_holds(self):
        with self.assertRaises(ValidationError) as caught:
            templates.validate({
                "schema": templates.SCHEMA,
                "agents": [{"name": "scout", "role": "reviewer", "kind": "autoscientist"}],
            })
        self.assertEqual(caught.exception.details.get("reason"), "kind_cannot_fill_seat")


class StandUpTests(AppCase):
    def test_the_skeleton_stands_up_in_one_call(self):
        out = self.app.templates.stand_up("starter")
        self.assertEqual(out["team"]["name"], "starter")
        self.assertEqual([a["name"] for a in out["agents"]], ["doer", "reviewer"])
        self.assertEqual([r["kind"] for r in out["rails"]],
                         ["open_cards", "spend_per_day", "forbidden_tools"])
        self.assertEqual([s["name"] for s in out["skills"]], ["closing-a-card"])

    def test_a_skeleton_crew_can_close_a_card(self):
        out = self.app.templates.stand_up("starter")
        team_id = out["team"]["id"]
        doer, reviewer = out["agents"]
        card = self.app.kanban.create_task(team_id, "ship the thing", column="ready")
        self.app.kanban.move(card["id"], "in_progress", doer["id"])
        self.app.kanban.move(card["id"], "review", doer["id"])
        closed = self.app.kanban.approve(card["id"], actor=reviewer["id"],
                                         evidence="commit:abc1234")
        self.assertEqual(closed["column"], "done")

    def test_a_crew_of_one_cannot_close_its_own_card(self):
        # the reason the skeleton is two: the review gate refuses the assignee
        team = self.crew()
        solo = self.app.crews.add_agent(team["id"], "wren", role="doer")
        card = self.app.kanban.create_task(team["id"], "ship it alone", column="ready")
        self.app.kanban.assign(card["id"], solo["id"])
        self.app.kanban.move(card["id"], "in_progress", solo["id"])
        self.app.kanban.move(card["id"], "review", solo["id"])
        with self.assertRaises(ValidationError) as caught:
            self.app.kanban.approve(card["id"], actor=solo["id"], evidence="commit:abc1234")
        self.assertIn("cannot approve its own work", str(caught.exception))


class RoundTripTests(AppCase):
    def test_a_crew_exports_and_reimports(self):
        first = self.app.templates.stand_up("starter")
        doc = self.app.templates.export(first["team"]["id"])
        self.assertEqual(doc["schema"], templates.SCHEMA)
        self.assertEqual([a["name"] for a in doc["agents"]], ["doer", "reviewer"])
        second = self.app.templates.stand_up("copy", doc=doc)
        self.assertEqual([a["name"] for a in second["agents"]], ["doer", "reviewer"])
        self.assertEqual(len(second["rails"]), len(doc["rails"]))
        self.assertEqual([s["name"] for s in second["skills"]], ["closing-a-card"])

    def test_an_export_does_not_carry_permissions(self):
        first = self.app.templates.stand_up("starter")
        doc = self.app.templates.export(first["team"]["id"])
        for agent in doc["agents"]:
            self.assertNotIn("scope", agent)
            self.assertNotIn("runtime", agent)

    def test_importing_into_a_crew_that_already_has_the_names_is_refused(self):
        first = self.app.templates.stand_up("starter")
        doc = self.app.templates.export(first["team"]["id"])
        with self.assertRaises(Conflict) as caught:
            self.app.templates.import_document(first["team"]["id"], doc)
        self.assertEqual(caught.exception.details.get("reason"), "agent_already_in_crew")

    def test_a_refused_document_creates_nothing(self):
        team = self.crew()
        before = [a["name"] for a in self.app.crews.list_agents(team["id"])]
        with self.assertRaises(ValidationError):
            self.app.templates.import_document(team["id"], {
                "schema": templates.SCHEMA,
                "agents": [{"name": "wren", "role": "doer"},
                           {"name": "kilo", "role": "reviewer", "hosting": "external"}],
            })
        after = [a["name"] for a in self.app.crews.list_agents(team["id"])]
        self.assertEqual(after, before)
