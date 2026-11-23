"""importing a real openclaw workspace, and the four things it must not do silently.

the reader existed before this file; what it did with a workspace was weaker than what it
said. skills arrived as directory names with no body behind them, a tools file became
knowledge, a heartbeat became knowledge, and a workspace shipping a key was imported
without a word. each test here is one of those.
"""

from core.errors import ValidationError
from agents.agent_imports import from_openclaw, import_agent, adopt
from crittertown.tests.base import AppCase

WORKSPACE = {
    "scout/SOUL.md": "---\nname: scout\nrole: research\n---\n\n# voice\nyou are dry and exact.\n",
    "scout/AGENTS.md": "# rules\n- always cite the source, never the summary\n",
    "scout/IDENTITY.md": "---\nname: scout\ntimezone: asia/calcutta\n---\nidentity prose\n",
    "scout/USER.md": "anne runs the studio and dislikes the word company.\n",
    "scout/MEMORY.md": "# memory\nwe settled on postgres in november.\n",
    "scout/HEARTBEAT.md": "check the inbox every 30 minutes\n",
    "scout/TOOLS.md": "use bash and curl, and the `frobnicator`\n",
    "scout/skills/research/SKILL.md": (
        "---\nname: research\ndescription: use when a claim needs a source\n---\n"
        "cite the page, not the summary.\n"),
    "scout/skills/broken/SKILL.md": "---\nname: Broken Name\n---\nno description\n",
    "scout/memory/2026-11-20.md": "today we shipped the thing\n",
}


class ReaderTests(AppCase):
    def test_the_voice_and_the_rules_both_reach_the_prompt(self):
        manifest = from_openclaw(WORKSPACE)
        self.assertEqual(manifest.name, "scout")
        self.assertEqual(manifest.role, "research")
        self.assertIn("## persona", manifest.instructions)
        self.assertIn("## instructions", manifest.instructions)
        self.assertIn("never the summary", manifest.instructions)

    def test_memory_arrives_as_knowledge_and_never_as_instruction(self):
        manifest = from_openclaw(WORKSPACE)
        titles = [doc["title"] for doc in manifest.knowledge]
        self.assertIn("memory (imported)", titles)
        self.assertIn("about-the-user (imported)", titles)
        self.assertNotIn("postgres", manifest.instructions)
        self.assertNotIn("anne runs the studio", manifest.instructions)

    def test_a_daily_log_is_history_and_is_not_imported_as_knowledge(self):
        manifest = from_openclaw(WORKSPACE)
        self.assertFalse([d for d in manifest.knowledge if "2026-11-20" in d["title"]])
        self.assertTrue(any("daily log" in w for w in manifest.warnings))

    def test_a_tools_file_grants_only_names_we_have(self):
        manifest = from_openclaw(WORKSPACE)
        self.assertEqual(manifest.tools, ["shell", "http"])
        self.assertIn("tools.frobnicator", manifest.unmapped)
        # prose is not a permission, and a tools file is never knowledge
        self.assertFalse([d for d in manifest.knowledge if "tools" in d["title"].lower()])

    def test_a_heartbeat_is_a_schedule_decision_not_knowledge(self):
        manifest = from_openclaw(WORKSPACE)
        self.assertIn("schedule.heartbeat.md", manifest.unmapped)
        self.assertFalse([d for d in manifest.knowledge if "heartbeat" in d["title"].lower()])

    def test_a_valid_skill_is_kept_and_an_invalid_sibling_is_skipped(self):
        manifest = from_openclaw(WORKSPACE)
        self.assertEqual(manifest.skills, ["research"])
        self.assertEqual(len(manifest.metadata["skill_docs"]), 1)
        self.assertTrue(any("skill skipped" in w for w in manifest.warnings))

    def test_a_workspace_carrying_a_credential_is_refused_whole(self):
        files = dict(WORKSPACE)
        files["scout/.env"] = "OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz012345\n"
        with self.assertRaises(ValidationError) as caught:
            from_openclaw(files)
        self.assertEqual(caught.exception.details.get("reason"), "workspace_carries_a_secret")
        self.assertEqual(caught.exception.details.get("file"), "scout/.env")

    def test_a_secret_in_a_prompt_is_refused_and_never_echoed(self):
        files = dict(WORKSPACE)
        key = "sk-ant-abcdefghijklmnopqrstuvwxyz0123456789"
        files["scout/AGENTS.md"] = f"# rules\nthe key is {key}\n"
        with self.assertRaises(ValidationError) as caught:
            from_openclaw(files)
        self.assertNotIn(key, str(caught.exception))

    def test_a_workspace_with_no_voice_is_refused(self):
        with self.assertRaises(ValidationError) as caught:
            from_openclaw({"scout/notes.md": "just some notes\n"})
        self.assertEqual(caught.exception.details.get("reason"), "workspace_has_no_voice")

    def test_two_documents_with_one_name_is_ambiguous(self):
        files = dict(WORKSPACE)
        files["other/AGENTS.md"] = "# other rules\n"
        with self.assertRaises(ValidationError) as caught:
            from_openclaw(files)
        self.assertEqual(caught.exception.details.get("reason"), "workspace_ambiguous")

    def test_grouped_skills_are_one_per_file_not_one_per_name(self):
        files = dict(WORKSPACE)
        files["scout/skills/personal/research/SKILL.md"] = (
            "---\nname: research-two\ndescription: use when the personal notes matter\n---\n"
            "read the personal folder first.\n")
        manifest = from_openclaw(files)
        self.assertEqual(sorted(manifest.skills), ["research", "research-two"])


class AdoptionTests(AppCase):
    def test_an_imported_skill_arrives_with_its_body_and_its_scope(self):
        team = self.crew()
        manifest = from_openclaw(WORKSPACE)
        out = adopt(self.app, team["id"], manifest)
        stored = self.app.skills.list(team["id"])
        self.assertEqual([s["name"] for s in stored], ["research"])
        full = self.app.skills.get(stored[0]["id"])
        self.assertIn("cite the page", full["body"])
        self.assertEqual(full["agents"], [out["agent"]["id"]])
        self.assertIn("skill.research", out["profile_entries"])

    def test_the_imported_agent_starts_idle_with_no_computer_and_no_card(self):
        team = self.crew()
        out = adopt(self.app, team["id"], from_openclaw(WORKSPACE))
        agent = out["agent"]
        self.assertEqual(agent["status"], "idle")
        board = self.app.kanban.board(team["id"])
        rows = board.get("tasks") or [row for column in board.get("columns", [])
                                     for row in (column.get("tasks") or [])]
        self.assertFalse([t for t in rows if t.get("assignee_agent_id") == agent["id"]])

    def test_the_warnings_survive_the_adoption(self):
        team = self.crew()
        out = adopt(self.app, team["id"], from_openclaw(WORKSPACE))
        self.assertTrue(any("frobnicator" in w for w in out["warnings"]))
        self.assertTrue(any("daily log" in w for w in out["warnings"]))
