"""skills: know-how carried in three stages, and the refusals that keep it safe.

the failure this exists for is the crew that loads everything. forty skills pasted into a
system prompt is forty procedures spent on the card that needed one of them, and the agent
that reads none of it is not better informed, it is less. so level 1 is a line, level 2 is a
call, level 3 is a call, and the budget is enforced at each.

the second failure is the skill as a way in. a skill is text a foreign author wrote and its
scripts run on the agent's computer, so an unscoped shell in allowed-tools, a body too large
to read in one turn, or a path that climbs out of the skill are refused rather than warned
about.
"""

from __future__ import annotations

import unittest

from base import AppCase

from core.errors import Conflict, NotFound, ValidationError
from work.skills import BODY_MAX, DISCOVERY_BUDGET, parse_skill_md, validate, check_path

GOOD = """---
name: pdf-forms
description: Fill, merge and split pdf forms. Use when a card involves a pdf form.
allowed-tools: [read, bash(pdftk)]
---
1. open the form
2. fill the fields
3. save it
"""


class ParsingTests(AppCase):
    def test_a_skill_md_splits_into_frontmatter_and_body(self):
        parsed = parse_skill_md(GOOD)
        self.assertEqual(parsed["meta"]["name"], "pdf-forms")
        self.assertTrue(parsed["body"].startswith("1. open the form"))

    def test_a_list_may_be_written_either_way(self):
        block = GOOD.replace("[read, bash(pdftk)]", "")
        block = block.replace("allowed-tools:", "allowed-tools:\n  - read\n  - bash(pdftk)")
        self.assertEqual(validate(parse_skill_md(block))["allowed_tools"],
                         ["read", "bash(pdftk)"])

    def test_a_file_without_frontmatter_is_refused(self):
        with self.assertRaises(ValidationError) as caught:
            parse_skill_md("just some text")
        self.assertEqual(caught.exception.details.get("why"), "frontmatter_missing")

    def test_unclosed_frontmatter_is_refused(self):
        with self.assertRaises(ValidationError) as caught:
            parse_skill_md("---\nname: x\nbody")
        self.assertEqual(caught.exception.details.get("why"), "frontmatter_unclosed")


class ValidationTests(AppCase):
    def refused(self, block: str) -> str:
        with self.assertRaises(ValidationError) as caught:
            validate(parse_skill_md(block))
        return caught.exception.details.get("why")

    def test_a_name_is_a_slug(self):
        for name in ("PDF_Forms", "pdf--forms", "-pdf", "pdf forms", "a" * 65):
            block = GOOD.replace("pdf-forms", name)
            self.assertIn(self.refused(block),
                          {"skill_name_not_a_slug", "skill_name_too_long"}, name)

    def test_a_description_has_to_say_when(self):
        block = GOOD.replace(
            "Fill, merge and split pdf forms. Use when a card involves a pdf form.",
            "Fills and merges pdf documents for the crew")
        self.assertEqual(self.refused(block), "description_must_say_when")

    def test_a_thin_description_is_refused(self):
        block = GOOD.replace(
            "Fill, merge and split pdf forms. Use when a card involves a pdf form.", "use pdf")
        self.assertEqual(self.refused(block), "description_too_thin")

    def test_a_body_is_required(self):
        block = GOOD.split("---\n1.")[0] + "---\n"
        self.assertEqual(self.refused(block), "body_required")

    def test_an_oversized_body_is_a_file_not_a_skill(self):
        block = GOOD + ("x" * BODY_MAX)
        self.assertEqual(self.refused(block), "body_too_long")

    def test_an_unscoped_shell_is_refused(self):
        block = GOOD.replace("[read, bash(pdftk)]", "[read, bash]")
        self.assertEqual(self.refused(block), "allowed_tool_unscoped")

    def test_a_skill_nobody_can_reach_is_refused(self):
        block = GOOD.replace("---\n1.", "disable-model-invocation: true\nuser-invocable: false\n---\n1.")
        self.assertEqual(self.refused(block), "skill_unreachable")

    def test_a_bundled_path_may_not_climb_out(self):
        for path in ("../creds", "/etc/passwd", "a/../../b", "C:/x"):
            with self.assertRaises(ValidationError):
                check_path(path)
        self.assertEqual(check_path("./refs//fields.md"), "refs/fields.md")


class SkillTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew()
        self.skill = self.app.skills.create(self.team["id"], text=GOOD)

    def test_a_skill_is_stored_with_its_tools_and_metadata(self):
        self.assertEqual(self.skill["name"], "pdf-forms")
        self.assertEqual(self.skill["allowed_tools"], ["read", "bash(pdftk)"])
        self.assertTrue(self.skill["enabled"])
        self.assertEqual(self.skill["origin"], "authored")

    def test_two_skills_may_not_share_a_name_in_a_crew(self):
        with self.assertRaises(Conflict):
            self.app.skills.create(self.team["id"], text=GOOD)

    def test_a_bundled_file_is_read_on_demand(self):
        self.app.skills.add_file(self.skill["id"], "refs/fields.md", "field map")
        listed = self.app.skills.get(self.skill["id"])["files"]
        self.assertEqual([f["path"] for f in listed], ["refs/fields.md"])
        read = self.app.skills.read_file(self.skill["id"], "refs/fields.md")
        self.assertEqual(read["content"], "field map")

    def test_an_unknown_file_is_not_found(self):
        with self.assertRaises(NotFound):
            self.app.skills.read_file(self.skill["id"], "refs/nope.md")

    def test_disabling_hides_it_from_level_one_and_refuses_a_load(self):
        self.app.skills.patch(self.skill["id"], enabled=False)
        self.assertEqual(self.app.skills.list(self.team["id"]), [])
        self.assertEqual(len(self.app.skills.list(self.team["id"], include_disabled=True)), 1)
        with self.assertRaises(ValidationError) as caught:
            self.app.skills.load(self.skill["id"])
        self.assertEqual(caught.exception.details.get("why"), "skill_disabled")

    def test_a_skill_addressed_to_somebody_is_only_offered_to_them(self):
        wren = self.app.crews.add_agent(self.team["id"], "wren", role="doer")
        rus = self.app.crews.add_agent(self.team["id"], "rus", role="researcher")
        self.app.skills.patch(self.skill["id"], agents=[wren["id"]])
        self.assertEqual(len(self.app.skills.list(self.team["id"], agent_id=wren["id"])), 1)
        self.assertEqual(self.app.skills.list(self.team["id"], agent_id=rus["id"]), [])

    def test_a_body_over_the_budget_is_refused_not_truncated(self):
        with self.assertRaises(ValidationError) as caught:
            self.app.skills.load(self.skill["id"], budget=10)
        self.assertEqual(caught.exception.details.get("why"), "body_over_budget")

    def test_deleting_a_skill_takes_its_files(self):
        self.app.skills.add_file(self.skill["id"], "refs/fields.md", "field map")
        self.app.skills.delete(self.skill["id"])
        with self.assertRaises(NotFound):
            self.app.skills.get(self.skill["id"])
        self.assertEqual(self.app.store.one(
            "select count(*) as n from skill_files")["n"], 0)


class DisclosureTests(AppCase):
    def test_level_one_is_a_line_per_skill_and_nothing_more(self):
        team = self.crew()
        self.app.skills.create(team["id"], text=GOOD)
        wren = self.app.crews.add_agent(team["id"], "wren", role="doer")
        card = self.app.kanban.create_task(team["id"], "fill a form")
        snapshot = self.app.context.compose(wren["id"], card["id"])
        slice_ = [s for s in snapshot["slices"] if s["name"] == "skills"][0]
        self.assertLessEqual(slice_["chars"], DISCOVERY_BUDGET)
        self.assertIn("pdf-forms", snapshot["block"])
        # the body is not in the context; it arrives with a call
        self.assertNotIn("open the form", snapshot["block"])

    def test_a_crew_with_too_many_skills_says_how_many_did_not_fit(self):
        team = self.crew()
        for index in range(12):
            text = GOOD.replace("pdf-forms", f"skill-{index}").replace(
                "Fill, merge and split pdf forms.",
                f"Skill number {index} does a long and specific thing for the crew.")
            self.app.skills.create(team["id"], text=text)
        offered = self.app.skills.discovery(team["id"], budget=200)
        self.assertGreater(offered["withheld"], 0)
        self.assertLessEqual(offered["chars"], 200)

    def test_a_doer_is_told_about_skills_and_a_reviewer_is_not(self):
        from work.roster import ROLE_SLICES

        self.assertIn("skills", ROLE_SLICES["doer"])
        self.assertNotIn("skills", ROLE_SLICES["reviewer"])


if __name__ == "__main__":
    unittest.main()
