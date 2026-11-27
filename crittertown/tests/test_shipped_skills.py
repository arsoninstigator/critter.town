"""the skills the tool ships about itself.

pad's lesson, taken seriously: a board with a REST surface and no skill telling an agent
how to drive it is a board agents use badly. these tests hold the shipped text to the
same validator a hand-written skill meets, and hold its claims to the code.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from base import AppCase
from api.routes import create_api
from core.errors import Conflict, ValidationError
from work import skills, templates


class ShippedSkillTests(AppCase):
    def test_every_shipped_skill_passes_the_validator_a_written_one_meets(self):
        self.assertEqual(len(templates.SHIPPED_SKILLS), 2)
        names = []
        for text in templates.SHIPPED_SKILLS:
            parsed = skills.validate(skills.parse_skill_md(text))
            names.append(parsed["name"])
            # the validator already refuses a thin description and a missing "use when"
            self.assertIn("use", parsed["description"].lower())
        self.assertEqual(sorted(names), ["closing-a-card", "using-the-board"])

    def test_the_board_skill_names_the_columns_the_board_actually_has(self):
        parsed = skills.validate(skills.parse_skill_md(templates.BOARD_SKILL))
        for column in self.kanban.columns(self.crew()["id"]):
            self.assertIn(column, parsed["body"], f"{column} is not described")

    def test_the_board_skill_names_the_schemes_the_gate_accepts(self):
        parsed = skills.validate(skills.parse_skill_md(templates.BOARD_SKILL))
        for scheme in ("commit", "run", "sheet", "source", "stripe", "ticket", "url", "gate"):
            self.assertIn(f"{scheme}:", parsed["body"], f"{scheme} is not offered")

    def test_the_skeleton_carries_both_skills(self):
        doc = templates.skeleton("studio")
        self.assertEqual(len(doc["skills"]), 2)
        installed = self.app.templates.import_document(self.crew("studio")["id"], doc)
        self.assertEqual(len(installed["skills"]), 2)


class ShippedRouteTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.client = TestClient(create_api(self.app))
        self.team = self.crew("studio")["id"]

    def test_the_shipped_list_is_readable_without_creating_anything(self):
        body = self.client.get("/skills/shipped").json()
        self.assertEqual([s["name"] for s in body["skills"]],
                         ["closing-a-card", "using-the-board"])
        self.assertEqual(self.app.skills.list(self.team), [])

    def test_installing_one_makes_a_real_skill_in_that_crew(self):
        created = self.client.post(f"/crews/{self.team}/skills/shipped",
                                   json={"name": "using-the-board"}).json()
        self.assertEqual(created["name"], "using-the-board")
        self.assertEqual(created["origin"], "shipped")
        self.assertEqual(len(self.app.skills.list(self.team)), 1)

    def test_installing_it_twice_is_refused_rather_than_duplicated(self):
        self.client.post(f"/crews/{self.team}/skills/shipped", json={"name": "using-the-board"})
        again = self.client.post(f"/crews/{self.team}/skills/shipped",
                                 json={"name": "using-the-board"})
        self.assertEqual(again.status_code, 409)
        self.assertIn("skill_already_installed", again.text)

    def test_an_unknown_name_says_what_is_available(self):
        response = self.client.post(f"/crews/{self.team}/skills/shipped",
                                    json={"name": "no-such-skill"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("using-the-board", response.text)


class ShippedSkillContractTests(AppCase):
    def test_the_board_skill_does_not_promise_a_route_that_does_not_exist(self):
        parsed = skills.validate(skills.parse_skill_md(templates.BOARD_SKILL))
        self.assertNotIn("/", parsed["body"].replace("->", ""), "a skill naming paths goes stale")
