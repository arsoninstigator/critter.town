"""what a run reached for, against what it was given.

the property being tested is the one that decides whether an outcome is reproducible: an
advantage that arrived through the network, a package registry or an environment key is real, and
it did not come from the inputs anybody provisioned. a crew that counts it at par with its own
supply learns nothing about what its supply can produce.

the mechanism reads commands by pattern and never executes one, so the tests are mostly about
what it refuses to claim: an unparsed command produces no signal, a forbidden tool is a decision
rather than a supply, and an empty allowlist means no hosts provisioned rather than all of them.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from base import AppCase  # noqa: E402
from core.db import new_id, now  # noqa: E402
from core.errors import NotFound  # noqa: E402
from work import readable as rd  # noqa: E402


class SignalTests(unittest.TestCase):
    def test_a_url_names_its_host(self):
        found = rd.signals(["curl -s https://api.example.com/v1/x -o out.json"])
        self.assertEqual([(s["channel"], s["value"]) for s in found],
                         [("host", "api.example.com")])

    def test_a_git_remote_names_its_host(self):
        found = rd.signals(["git clone https://github.com/arsoninstigator/critter.town.git"])
        self.assertEqual(found[0]["value"], "github.com")

    def test_an_scp_remote_names_its_host(self):
        found = rd.signals(["git push git@github.com:someone/thing.git main"])
        self.assertIn("github.com", [s["value"] for s in found])

    def test_a_package_install_names_the_ecosystem_and_the_package(self):
        found = rd.signals(["pip install requests==2.31.0"])
        self.assertEqual([s["value"] for s in found], ["python:requests==2.31.0"])

    def test_flags_are_not_packages(self):
        found = rd.signals(["npm install --save-dev typescript"])
        self.assertEqual([s["value"] for s in found], ["node:typescript"])

    def test_a_key_in_the_environment_is_a_channel(self):
        found = rd.signals(["curl -H \"authorization: $GITHUB_TOKEN\" https://api.github.com/x"])
        values = {s["value"] for s in found}
        self.assertIn("github_token", values)
        self.assertIn("api.github.com", values)

    def test_an_ordinary_variable_is_not_a_key(self):
        self.assertEqual(rd.signals(["echo $HOME && ls $PWD"]), [])

    def test_the_same_channel_twice_is_one_signal(self):
        found = rd.signals(["curl https://example.com/a", "curl https://example.com/b"])
        self.assertEqual(len(found), 1)

    def test_a_command_it_does_not_understand_produces_nothing(self):
        self.assertEqual(rd.signals(["make check", "pytest -q"]), [])

    def test_commands_come_out_of_the_steps_a_run_recorded(self):
        steps = [{"turn": 1, "steps": [{"kind": "command", "command": "ls"},
                                       {"kind": "command", "command": "npm i lodash"}],
                  "commands": []},
                 {"turn": 2, "steps": [], "commands": ["pip install flask"]}]
        self.assertEqual(rd.commands_of(steps), ["ls", "npm i lodash", "pip install flask"])


class ClassifyTests(unittest.TestCase):
    def test_a_provisioned_host_is_readable(self):
        supply = {"hosts": ["github.com"], "tools": [], "declared": []}
        found = rd.classify([{"channel": "host", "value": "github.com"}], supply)
        self.assertEqual(len(found["readable"]), 1)
        self.assertFalse(found["unprovisioned"])
        self.assertEqual(found["readable_share"], 1.0)

    def test_a_subdomain_of_a_provisioned_host_is_readable(self):
        supply = {"hosts": ["github.com"], "tools": [], "declared": []}
        found = rd.classify([{"channel": "host", "value": "api.github.com"}], supply)
        self.assertTrue(found["unprovisioned"] is False)

    def test_a_host_nobody_provisioned_is_auxiliary(self):
        supply = {"hosts": ["github.com"], "tools": [], "declared": []}
        found = rd.classify([{"channel": "host", "value": "registry.npmjs.org"}], supply)
        self.assertTrue(found["unprovisioned"])
        self.assertEqual(found["why"], ["host:registry.npmjs.org"])

    def test_no_allowlist_is_no_hosts_not_every_host(self):
        # the whole point: silence is not permission
        found = rd.classify([{"channel": "host", "value": "example.com"}],
                            {"hosts": [], "tools": [], "declared": []})
        self.assertTrue(found["unprovisioned"])

    def test_a_declared_channel_covers_anything(self):
        supply = {"hosts": [], "tools": [], "declared": ["python:requests"]}
        found = rd.classify([{"channel": "package", "value": "python:requests"}], supply)
        self.assertFalse(found["unprovisioned"])

    def test_a_forbidden_tool_is_not_a_supply(self):
        task = {"payload": {}}
        supply = rd.provisioned(task, [{"kind": "forbidden_tools", "value": ["rm"]}])
        self.assertEqual(supply["hosts"], [])
        self.assertEqual(supply["tools"], ["rm"])

    def test_a_card_may_declare_what_it_may_reach(self):
        task = {"payload": {"channels": ["registry.npmjs.org"]}}
        supply = rd.provisioned(task, [])
        found = rd.classify([{"channel": "host", "value": "registry.npmjs.org"}], supply)
        self.assertFalse(found["unprovisioned"])


class ServiceTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew()
        self.doer = self.app.crews.add_agent(self.team["id"], "wren", role="doer")

    def card(self, payload: dict | None = None) -> dict:
        task = self.app.kanban.create_task(self.team["id"], "fetch the policy", description="x",
                                          column="ready", payload=payload or {})
        self.app.kanban.assign(task["id"], self.doer["id"])
        return self.app.kanban.get_task(task["id"])

    def record_run(self, task: dict, commands: list[str]) -> str:
        run_id = new_id("run")
        steps = [{"turn": 1, "thought": "", "steps": [
            {"kind": "command", "command": command} for command in commands]}]
        with self.app.store.tx() as c:
            c.execute(
                """insert into runs (id, task_id, agent_id, provider, status, steps,
                       created_date) values (?,?,?,?,?,?,?)""",
                (run_id, task["id"], self.doer["id"], "local", "done",
                 __import__("json").dumps(steps), now()))
        return run_id

    def test_a_run_that_only_used_its_allowlist_is_readable(self):
        self.app.guardrails.set(self.team["id"], "egress_hosts", ["example.com"])
        task = self.card()
        run_id = self.record_run(task, ["curl https://example.com/policy"])
        reading = self.app.readable.for_run(run_id)
        self.assertEqual(reading["signals"], 1)
        self.assertFalse(reading["unprovisioned"])
        self.assertEqual(reading["readable_share"], 1.0)

    def test_a_run_that_reached_outside_is_flagged(self):
        self.app.guardrails.set(self.team["id"], "egress_hosts", ["example.com"])
        task = self.card()
        run_id = self.record_run(task, ["curl https://example.com/policy",
                                 "pip install beautifulsoup4"])
        reading = self.app.readable.for_run(run_id)
        self.assertTrue(reading["unprovisioned"])
        self.assertEqual(reading["why"], ["package:python:beautifulsoup4"])

    def test_the_reading_is_recorded_once(self):
        task = self.card()
        self.record_run(task, ["curl https://example.com/x"])
        self.app.readable.for_run(self.app.store.one(
            "select id from runs where task_id = ?", (task["id"],))["id"])
        rows = self.app.store.query("select * from readable_checks where task_id = ?",
                                    (task["id"],))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["unprovisioned"], 1)

    def test_a_card_carries_every_channel_its_runs_reached(self):
        task = self.card()
        self.record_run(task, ["curl https://a.example.com/x"])
        self.record_run(task, ["pip install flask"])
        report = self.app.readable.for_card(task["id"])
        self.assertEqual(report["runs"], 2)
        self.assertTrue(report["unprovisioned"])
        self.assertEqual(len(report["auxiliary"]), 2)

    def test_a_card_with_no_runs_is_not_a_clean_one(self):
        task = self.card()
        report = self.app.readable.for_card(task["id"])
        self.assertEqual(report["runs"], 0)
        self.assertIsNone(report["readable_share"])
        self.assertFalse(report["unprovisioned"])

    def test_the_crew_report_counts_the_cards_that_leaned(self):
        clean = self.card({"channels": ["example.com"]})
        self.record_run(clean, ["curl https://example.com/x"])
        leaning = self.card()
        self.record_run(leaning, ["curl https://elsewhere.example.org/y"])
        self.app.readable.for_run(self.app.store.one(
            "select id from runs where task_id = ?", (clean["id"],))["id"])
        self.app.readable.for_run(self.app.store.one(
            "select id from runs where task_id = ?", (leaning["id"],))["id"])
        report = self.app.readable.for_crew(self.team["id"])
        self.assertEqual(report["cards"], 2)
        self.assertEqual(report["leaning"], 1)
        self.assertEqual([c["task_id"] for c in report["cards_leaning"]], [leaning["id"]])

    def test_a_run_that_does_not_exist_is_not_a_reading(self):
        with self.assertRaises(NotFound):
            self.app.readable.for_run("run_nope")

    def test_the_routes_answer(self):
        from fastapi.testclient import TestClient
        from api.routes import create_api
        task = self.card()
        run_id = self.record_run(task, ["npm i lodash"])
        client = TestClient(create_api(self.app))
        self.assertTrue(client.get(f"/runs/{run_id}/readable").json()["unprovisioned"])
        card = client.get(f"/cards/{task['id']}/readable").json()
        self.assertEqual(card["task_id"], task["id"])
        crew = client.get(f"/crews/{self.team['id']}/readable").json()
        self.assertEqual(crew["window_days"], 7.0)
