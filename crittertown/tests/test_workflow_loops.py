"""loops, and the improvement engine that a loop feeds.

a flat dag cannot say "try again". a node either succeeds or the run fails, and a run
that failed at the second attempt of five looks exactly like a run that failed at the
first. the loop node is the smallest thing that fixes that: a body of nodes, a ceiling,
and a condition that ends it early.

the engine sits on the other side of the loop. an exhausted loop is the only place in the
tree that knows the work did not converge, which is the one thing worth learning. so it
files a lesson: proposed, attributed to the agent whose work fell short, and never
promoted by itself.
"""

from __future__ import annotations

import unittest

from base import AppCase

from core.errors import ValidationError


class LoopShapeTests(AppCase):
    def test_a_loop_needs_a_body(self):
        team = self.crew()
        with self.assertRaises(ValidationError):
            self.app.workflows.save(team["id"], "w", {"nodes": [
                {"id": "spin", "type": "loop", "max": 2},
            ]})

    def test_a_body_member_has_to_declare_its_loop(self):
        team = self.crew()
        with self.assertRaises(ValidationError):
            self.app.workflows.save(team["id"], "w", {"nodes": [
                {"id": "spin", "type": "loop", "body": ["draft"], "max": 2},
                {"id": "draft", "type": "agent_task", "title": "draft"},
            ]})

    def test_a_loop_needs_a_ceiling_under_twenty(self):
        team = self.crew()
        for cap in (0, 21, "lots"):
            with self.assertRaises(ValidationError):
                self.app.workflows.save(team["id"], f"w{cap}", {"nodes": [
                    {"id": "spin", "type": "loop", "body": ["draft"], "max": cap},
                    {"id": "draft", "type": "agent_task", "loop": "spin", "title": "d"},
                ]})

    def test_a_person_inside_a_loop_is_refused(self):
        """a loop that stops for a person is a workflow with a person in it."""
        team = self.crew()
        with self.assertRaises(ValidationError):
            self.app.workflows.save(team["id"], "w", {"nodes": [
                {"id": "spin", "type": "loop", "body": ["ask"], "max": 2},
                {"id": "ask", "type": "human_gate", "loop": "spin", "title": "ok?"},
            ]})

    def test_a_well_formed_loop_saves(self):
        team = self.crew()
        saved = self.app.workflows.save(team["id"], "w", {"nodes": [
            {"id": "spin", "type": "loop", "body": ["draft"], "max": 2,
             "until": "${node.draft.ok}"},
            {"id": "draft", "type": "agent_task", "loop": "spin", "title": "draft it"},
        ]})
        self.assertEqual(saved["name"], "w")


class LoopRunTests(AppCase):
    def loop_workflow(self, until: str, cap: int = 2) -> dict:
        team = self.crew()
        self.agent(team["id"], "wren", role="writer")
        return self.app.workflows.save(team["id"], "refine", {"nodes": [
            {"id": "spin", "type": "loop", "body": ["draft"], "max": cap, "until": until},
            {"id": "draft", "type": "agent_task", "loop": "spin", "title": "draft it"},
        ]})

    def test_body_nodes_do_not_also_run_in_the_main_pass(self):
        workflow = self.loop_workflow("${node.draft.ok}", cap=1)
        run = self.app.workflows.run(workflow["id"], {})
        # one card, not two: the loop owns its body
        tasks = self.app.kanban.list_tasks(workflow["team_id"])
        self.assertEqual(len(tasks), 1)

    def test_an_exhausted_loop_fails_and_proposes_a_lesson(self):
        workflow = self.loop_workflow("${node.draft.ok}", cap=2)
        run = self.app.workflows.run(workflow["id"], {})
        self.assertEqual(run["status"], "failed")
        spin = run["state"]["nodes"]["spin"]
        self.assertFalse(spin["converged"])
        self.assertEqual(spin["iterations"], 2)
        self.assertTrue(spin["lesson"]["proposed"])
        lessons = self.app.lessons.list(spin["lesson"]["agent_id"])
        self.assertEqual(len(lessons), 1)
        self.assertIn("did not converge", lessons[0]["statement"])
        self.assertEqual(lessons[0]["status"], "proposed",
                         "an engine that promotes its own lessons is not improving")

    def test_the_loop_records_every_attempt(self):
        workflow = self.loop_workflow("${node.draft.ok}", cap=3)
        run = self.app.workflows.run(workflow["id"], {})
        history = run["state"]["loops"]["spin"]
        self.assertEqual(len(history), 3)
        self.assertEqual([h["iteration"] for h in history], [1, 2, 3])

    def test_a_loop_that_converges_stops_early(self):
        team = self.crew()
        self.agent(team["id"], "wren", role="writer")
        workflow = self.app.workflows.save(team["id"], "once", {"nodes": [
            {"id": "spin", "type": "loop", "body": ["draft"], "max": 4, "until": "true"},
            {"id": "draft", "type": "agent_task", "loop": "spin", "title": "draft it"},
        ]})
        run = self.app.workflows.run(workflow["id"], {})
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(run["state"]["nodes"]["spin"]["iterations"], 1)


class ImproveNodeTests(AppCase):
    def test_an_improve_node_files_a_proposal_without_promoting_it(self):
        team = self.crew()
        agent = self.agent(team["id"], "wren", role="writer")
        workflow = self.app.workflows.save(team["id"], "learn", {"nodes": [
            {"id": "note", "type": "improve", "agent": "wren",
             "statement": "drafting before reading the brief wastes a turn"},
        ]})
        run = self.app.workflows.run(workflow["id"], {})
        self.assertEqual(run["status"], "succeeded")
        lesson = run["state"]["nodes"]["note"]["lesson"]
        self.assertTrue(lesson["proposed"])
        proposed = self.app.lessons.list(agent["id"])
        self.assertEqual(len(proposed), 1)
        self.assertEqual(proposed[0]["status"], "proposed")

    def test_an_improve_node_with_nothing_to_say_fails_loudly(self):
        team = self.crew()
        self.agent(team["id"], "wren", role="writer")
        workflow = self.app.workflows.save(team["id"], "mute", {"nodes": [
            {"id": "note", "type": "improve"},
        ]})
        run = self.app.workflows.run(workflow["id"], {})
        self.assertEqual(run["status"], "failed")
        self.assertIn("no statement", run["state"]["nodes"]["note"]["error"])


if __name__ == "__main__":
    unittest.main()
