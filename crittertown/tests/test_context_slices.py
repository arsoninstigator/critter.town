"""working context, sliced.

the thing being tested is a budget, not a transcript. an agent gets the slices that bear
on its card, in a fixed order, each truncated to a stated size, and the assembly is
written down. if any of that stopped being true the prompt would still look plausible,
which is exactly why it is tested rather than read.
"""

from __future__ import annotations

import unittest

from base import AppCase

from agents.context import ORDER, SLICES


class SliceAssemblyTests(AppCase):
    def crew_with_agent(self) -> dict:
        team = self.crew(mission="sell things without a person in the loop")
        agent = self.agent(team["id"], "wren", role="writer")
        return {"team": team, "agent": agent}

    def test_every_slice_has_a_budget(self):
        self.assertEqual(set(SLICES), set(ORDER))
        for name, budget in SLICES.items():
            self.assertGreater(budget, 0, name)

    def test_an_agent_gets_the_slices_that_bear_on_its_card(self):
        ctx = self.crew_with_agent()
        self.app.intent.record(ctx["team"]["id"], "one good post a week")
        self.app.knowledge.record(ctx["team"]["id"], "preference", "squash merges",
                                  "always squash merges on main, never a merge commit")
        card = self.app.kanban.create_task(ctx["team"]["id"], "draft the post",
                                          column="ready", description="short and useful")
        snapshot = self.app.context.compose(ctx["agent"]["id"], task_id=card["id"])
        names = [s["name"] for s in snapshot["slices"]]
        self.assertEqual(names, [n for n in ORDER if n in names])
        self.assertIn("intent", names)
        self.assertIn("knowledge", names)
        self.assertIn("task", names)
        self.assertIn("draft the post", snapshot["block"])

    def test_a_slice_that_is_too_long_is_truncated_and_says_so(self):
        ctx = self.crew_with_agent()
        self.app.knowledge.record(
            ctx["team"]["id"], "process", "how we publish",
            "1. write the draft\n2. read it out loud\n3. ship it\n4. tell the channel")
        for index in range(6):
            self.app.knowledge.record(
                ctx["team"]["id"], "fact", f"endpoint {index}",
                "the staging endpoint is https://staging.example.test/api/v2/" + "x" * 120,
            )
        snapshot = self.app.context.compose(ctx["agent"]["id"])
        knowledge = [s for s in snapshot["slices"] if s["name"] == "knowledge"][0]
        self.assertLessEqual(knowledge["chars"], SLICES["knowledge"])
        self.assertTrue(knowledge["truncated"])
        self.assertLessEqual(snapshot["chars"], sum(SLICES.values()) + 200)

    def test_an_agent_can_ask_for_fewer_slices(self):
        ctx = self.crew_with_agent()
        self.app.intent.record(ctx["team"]["id"], "one good post a week, nothing else")
        self.app.profile.set(ctx["agent"]["id"], "scope", "context", ["task", "intent"])
        needs = self.app.context.needs_for(ctx["agent"]["id"])
        self.assertEqual(needs, ["intent", "task"])
        card = self.app.kanban.create_task(ctx["team"]["id"], "draft", column="ready")
        snapshot = self.app.context.compose(ctx["agent"]["id"], task_id=card["id"])
        self.assertEqual([s["name"] for s in snapshot["slices"]], ["intent", "task"])

    def test_an_unknown_slice_name_is_dropped_rather_than_guessed(self):
        ctx = self.crew_with_agent()
        self.app.profile.set(ctx["agent"]["id"], "scope", "context",
                             ["task", "the entire internet", "intent"])
        self.assertEqual(self.app.context.needs_for(ctx["agent"]["id"]), ["intent", "task"])

    def test_the_assembly_is_written_down(self):
        ctx = self.crew_with_agent()
        card = self.app.kanban.create_task(ctx["team"]["id"], "draft", column="ready")
        snapshot = self.app.context.compose(ctx["agent"]["id"], task_id=card["id"])
        stored = self.app.context.recent(ctx["agent"]["id"])
        self.assertEqual(stored[0]["id"], snapshot["id"])
        self.assertEqual(stored[0]["chars"], snapshot["chars"])
        self.assertTrue(stored[0]["slices"])

    def test_a_preview_leaves_no_row(self):
        ctx = self.crew_with_agent()
        self.app.context.compose(ctx["agent"]["id"], store=False)
        self.assertEqual(self.app.context.recent(ctx["agent"]["id"]), [])

    def test_what_an_agent_was_taught_is_a_slice(self):
        ctx = self.crew_with_agent()
        lesson = self.app.lessons.propose(ctx["agent"]["id"], "read the brief before drafting")
        self.app.lessons.approve(lesson["id"], actor="owner")
        self.app.lessons.promote(lesson["id"], actor="owner")
        snapshot = self.app.context.compose(ctx["agent"]["id"])
        self.assertIn("read the brief before drafting", snapshot["block"])
        self.assertIn("lessons", [s["name"] for s in snapshot["slices"]])

    def test_a_run_carries_the_snapshot_it_was_given(self):
        ctx = self.crew_with_agent()
        card = self.app.kanban.create_task(ctx["team"]["id"], "draft the post",
                                          column="ready")
        self.app.runner.dispatch(card["id"])
        snapshots = self.app.context.recent(ctx["agent"]["id"])
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["task_id"], card["id"])
        self.assertTrue(snapshots[0]["run_id"], "a snapshot without its run is a loose end")

    def test_the_intent_is_above_the_task(self):
        ctx = self.crew_with_agent()
        self.app.intent.record(ctx["team"]["id"], "one good post a week")
        card = self.app.kanban.create_task(ctx["team"]["id"], "draft", column="ready")
        block = self.app.context.compose(ctx["agent"]["id"], task_id=card["id"])["block"]
        self.assertLess(block.index("one good post a week"), block.index("card: draft"))


if __name__ == "__main__":
    unittest.main()
