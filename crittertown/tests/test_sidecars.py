"""sidecars: the second kind of spawn, and the rules that make it a helper.

a subagent is a junior agent: it has a lineage the parent signed, a principal of its own,
and it can be handed a card. a sidecar is beside a card rather than below an agent: it
works inside its parent's run, holds nothing, and stops existing when the card closes.
the two fail differently if you confuse them, which is why the difference is a column and
a set of refusals rather than a naming convention.
"""

from __future__ import annotations

import unittest

from base import AppCase

from core.errors import NotAllowed, ValidationError


class SpawnRelationTests(AppCase):
    def parent(self, spawn_enabled: bool = True) -> dict:
        team = self.crew()
        agent = self.agent(team["id"], "wren", role="writer")
        self.app.profile.set_spawn_policy(
            agent["id"], enabled=spawn_enabled, mode="ephemeral", max_children=4
        )
        return {"team": team, "agent": agent}

    def test_a_sidecar_records_its_relation_and_its_card(self):
        ctx = self.parent()
        card = self.app.kanban.create_task(ctx["team"]["id"], "draft the post", column="ready")
        spawn = self.app.profile.spawn(
            ctx["agent"]["id"], "scout", role="research",
            task_id=card["id"], relation="sidecar",
        )
        self.assertEqual(self.app.profile.relation_of(spawn["child"]["id"]), "sidecar")
        listed = self.app.profile.sidecars(task_id=card["id"])
        self.assertEqual([s["name"] for s in listed], ["scout"])
        self.assertEqual(listed[0]["task_id"], card["id"])

    def test_a_subagent_is_still_a_child(self):
        ctx = self.parent()
        card = self.app.kanban.create_task(ctx["team"]["id"], "draft", column="ready")
        spawn = self.app.profile.spawn(ctx["agent"]["id"], "jr", role="writer",
                                       task_id=card["id"])
        self.assertEqual(self.app.profile.relation_of(spawn["child"]["id"]), "child")
        self.assertEqual(self.app.profile.sidecars(task_id=card["id"]), [])

    def test_a_sidecar_needs_a_card_to_sit_beside(self):
        ctx = self.parent()
        with self.assertRaises(ValidationError):
            self.app.profile.spawn(ctx["agent"]["id"], "scout", relation="sidecar")

    def test_an_unknown_relation_is_refused(self):
        ctx = self.parent()
        card = self.app.kanban.create_task(ctx["team"]["id"], "draft", column="ready")
        with self.assertRaises(ValidationError):
            self.app.profile.spawn(ctx["agent"]["id"], "scout", task_id=card["id"],
                                   relation="cousin")

    def test_sidecars_are_capped_per_card(self):
        ctx = self.parent()
        card = self.app.kanban.create_task(ctx["team"]["id"], "draft", column="ready")
        for index in range(2):
            self.app.profile.spawn(ctx["agent"]["id"], f"scout{index}",
                                   task_id=card["id"], relation="sidecar")
        with self.assertRaises(Exception):
            self.app.profile.spawn(ctx["agent"]["id"], "scout9", task_id=card["id"],
                                   relation="sidecar")

    def test_a_sidecar_gets_no_authority_of_its_own(self):
        ctx = self.parent()
        card = self.app.kanban.create_task(ctx["team"]["id"], "draft", column="ready")
        spawn = self.app.profile.spawn(ctx["agent"]["id"], "scout", role="research",
                                       task_id=card["id"], relation="sidecar")
        child = self.app.crews.get_agent(spawn["child"]["id"])
        perms = child["scope"]["permissions"]
        self.assertEqual([k for k, v in perms.items() if v], [],
                         "a sidecar that holds a permission is a subagent with a new name")
        self.assertIn("sidecar of", child["scope"]["instructions"])
        self.assertIn(card["id"], child["scope"]["instructions"])

    def test_a_sidecar_cannot_own_a_card(self):
        ctx = self.parent()
        card = self.app.kanban.create_task(ctx["team"]["id"], "draft", column="ready")
        other = self.app.kanban.create_task(ctx["team"]["id"], "second", column="ready")
        spawn = self.app.profile.spawn(ctx["agent"]["id"], "scout",
                                       task_id=card["id"], relation="sidecar")
        self.app.kanban.assign(other["id"], spawn["child"]["id"])
        # dispatch is where it matters: a card may be reserved, but a sidecar may not run it
        with self.assertRaises(NotAllowed):
            self.app.runner.dispatch(other["id"])

    def test_closing_the_card_retires_the_helpers(self):
        ctx = self.parent()
        card = self.app.kanban.create_task(ctx["team"]["id"], "draft", column="ready")
        spawn = self.app.profile.spawn(ctx["agent"]["id"], "scout",
                                       task_id=card["id"], relation="sidecar")
        retired = self.app.profile.retire_sidecars(card["id"])
        self.assertEqual(retired, [spawn["child"]["id"]])
        self.assertEqual(self.app.profile.sidecars(task_id=card["id"]), [])
        self.assertEqual(self.app.profile.relation_of(spawn["child"]["id"]), "self")

    def test_a_failed_run_retires_the_helpers_too(self):
        ctx = self.parent()
        card = self.app.kanban.create_task(ctx["team"]["id"], "draft", column="ready")
        self.app.profile.spawn(ctx["agent"]["id"], "scout", task_id=card["id"],
                               relation="sidecar")
        # the deterministic model returns an empty turn, which the runner treats as a
        # run that did not produce a result; either way the card settles and the sidecar
        # must not outlive it
        try:
            self.app.runner.dispatch(card["id"])
        except Exception:
            pass
        self.assertEqual(self.app.profile.sidecars(task_id=card["id"]), [])


if __name__ == "__main__":
    unittest.main()
