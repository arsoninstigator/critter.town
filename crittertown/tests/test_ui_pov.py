"""the served shell, held to the api it renders.

a page is the easiest thing in a repo to let rot, because nothing imports it. these
tests do the two things a page needs: it renders, and every route it calls exists.
"""

from __future__ import annotations

import re
import unittest

from base import AppCase

from api.pages import ISOLATION_PAGE, POV_PAGE
from api.routes import create_api

ROUTE_TABLE = re.compile(r"var R = \{(.*?)\};", re.S)
ENTRY = re.compile(r'(\w+):\s*"([^"]+)"')

# the only writes the shell is allowed: a card move, a calendar block, a profile value,
# a revoked grant, closing a routine's receipt, pausing a routine, and pausing a limit.
# everything else it renders is read-only by construction.
# canvas b adds three: saving a definition, running a workflow, and answering its gate.
# they are the operator acting in their own editor, not an agent acting through the shell.
# projects add three more: advancing a phase the gate already opened, killing a project on a
# criterion it declared at inception, and clearing strikes. all three are the operator acting
# on their own board, and each one is refused by the api when the rows do not support it.
WRITES = {"moveTask", "calendar", "profileEntry", "grant", "routineDone", "routinePause",
          "materialise", "guardrail", "saveWorkflow", "workflowRun", "workflowResume",
          "advanceProject", "killProject", "clearStrikes"}


def routes_in(page: str) -> dict[str, str]:
    table = ROUTE_TABLE.search(page)
    assert table, "the page has no route table"
    return dict(ENTRY.findall(table.group(1)))


def shape(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path)


class PageShapeTests(unittest.TestCase):
    def test_the_shell_carries_a_route_table(self):
        routes = routes_in(POV_PAGE)
        self.assertGreaterEqual(len(routes), 12)
        self.assertIn("agents/{agent}/profile/{namespace}/{key}", routes["profileEntry"])

    def test_the_nine_namespaces_are_all_in_the_editor(self):
        namespaces = re.search(r"var NAMESPACES = \[(.*?)\];", POV_PAGE, re.S)
        names = re.findall(r'"(\w+)"', namespaces.group(1))
        self.assertEqual(
            names,
            ["skills", "scope", "memory", "instructions", "knowledge", "perms",
             "tools", "vertical", "spawn"],
        )

    def test_the_page_writes_only_where_it_is_allowed_to(self):
        methods = re.findall(r'method:\s*"(\w+)"', POV_PAGE)
        self.assertEqual(sorted(methods),
                         ["DELETE", "DELETE", "POST", "POST", "POST", "POST", "POST",
                          "POST", "POST", "POST", "POST", "POST", "POST", "POST", "PUT"])
        for name in WRITES:
            self.assertIn(name, routes_in(POV_PAGE))

    def test_the_new_views_are_in_the_shell(self):
        self.assertIn('"routines","guardrails"', POV_PAGE)

    def test_projects_and_strikes_have_a_page(self):
        self.assertIn('"projects","strikes"', POV_PAGE)
        # a project page that cannot read its gate would offer an advance that always
        # refuses, which is worse than no button
        self.assertIn("projectGate", POV_PAGE)
        self.assertIn("gate held by", POV_PAGE)
        self.assertIn("declared criterion", POV_PAGE)
        # and a strike page has to name the seat, the count and who cleared it
        self.assertIn("renderStrikes", POV_PAGE)
        self.assertIn("strike(s) freeze a seat", POV_PAGE)
        self.assertIn("who is clearing this", POV_PAGE)

    def test_the_workflow_editor_is_in_the_shell(self):
        self.assertIn('"workflows"', POV_PAGE)
        self.assertIn("renderWorkflowEditor", POV_PAGE)
        # the editor has to offer a gate's two answers, or a parked run can only be
        # resumed from curl, which is the thing the canvas exists to stop
        self.assertIn('["approved", "rejected"]', POV_PAGE)
        self.assertIn("a missed routine reads as missed", POV_PAGE)
        self.assertIn("refused today", POV_PAGE)

    def test_a_save_shows_the_value_it_stores_not_a_merge(self):
        self.assertIn("there is no merged preview", POV_PAGE)
        self.assertIn("JSON.parse(box.value)", POV_PAGE)

    def test_the_header_shows_the_key_beside_the_name(self):
        self.assertIn("agentKey", POV_PAGE)
        self.assertIn("agent_key", POV_PAGE)

    def test_isolation_is_its_own_page(self):
        self.assertIn("identical_profiles", ISOLATION_PAGE)
        self.assertIn("crews/{team}/isolation", ISOLATION_PAGE)


class PageDataTests(AppCase):
    """the two new views read routes, so the routes have to answer with what the page draws."""

    def test_the_crew_strike_view_names_the_frozen_seat(self):
        team = self.crew("strikes")
        wren = self.agent(team["id"], "wren")
        self.agent(team["id"], "holt")
        self.app.strikes.record(team["id"], wren["id"], "guardrail_refused")
        self.app.strikes.record(team["id"], wren["id"], "guardrail_refused")
        out = self.app.strikes.crew(team["id"])
        self.assertEqual(out["frozen"], [wren["id"]])
        self.assertEqual(len(out["seats"]), 2)
        row = [s for s in out["seats"] if s["seat"] == wren["id"]][0]
        self.assertEqual(row["state"], "frozen")
        self.assertEqual(row["strikes"], 2)
        self.assertEqual(row["last_strike"]["code"], "guardrail_refused")
        # and clearing names a person, because a cleared row with no name is a shrug
        self.app.strikes.clear(team["id"], wren["id"], "owner")
        self.assertEqual(self.app.strikes.crew(team["id"])["frozen"], [])

    def test_the_project_view_reads_the_gate_it_offers(self):
        team = self.crew("projects")
        row = self.app.projects.create(
            team["id"], "sell the recipe",
            [{"slice": "recipe", "objective_ref": "obj1"}], 900,
            ["unit cost stays above the ceiling for a week"], objective_ref="obj1",
            unit_price_cents=1500, min_margin_pct=0.3)
        listed = self.app.projects.list(team["id"])
        self.assertEqual([p["id"] for p in listed], [row["id"]])
        gate = self.app.projects.evaluate(row["id"])
        self.assertIn("unmet", gate)
        self.assertTrue(gate["unmet"])


class PageRouteTests(AppCase):
    def test_every_route_in_the_shell_exists(self):
        api = create_api(self.app)
        known = {shape(getattr(route, "path", "")) for route in api.routes}
        for name, path in routes_in(POV_PAGE).items():
            self.assertIn(shape("/" + path), known, f"the shell calls {name}: /{path}")

    def test_every_route_in_the_isolation_page_exists(self):
        api = create_api(self.app)
        known = {shape(getattr(route, "path", "")) for route in api.routes}
        for name, path in routes_in(ISOLATION_PAGE).items():
            self.assertIn(shape("/" + path), known, f"the page calls {name}: /{path}")

    def test_the_pages_are_served(self):
        from fastapi.testclient import TestClient

        client = TestClient(create_api(self.app))
        pov = client.get("/ui/pov")
        self.assertEqual(pov.status_code, 200)
        self.assertIn("crittertown · pov", pov.text)
        isolation = client.get("/ui/isolation")
        self.assertEqual(isolation.status_code, 200)
        self.assertIn("crittertown · isolation", isolation.text)

    def test_the_shell_renders_a_real_crew(self):
        from fastapi.testclient import TestClient

        team = self.crew("shop")
        agent = self.agent(team["id"], "wren", role="writer", skills=["writing"])
        self.kanban.create_task(team["id"], "publish the post", column="ready",
                                assignee_agent_id=agent["id"])
        client = TestClient(create_api(self.app))
        board = client.get(f"/agents/{agent['id']}/board").json()
        self.assertEqual(board["total"], 1)
        self.assertTrue(any(c["name"] == "ready" for c in board["columns"]))
        profile = client.get(f"/agents/{agent['id']}/profile").json()
        self.assertIn("skills", profile["namespaces"])
        self.assertTrue(profile["fingerprint"])

    def test_a_namespace_write_replaces_rather_than_merges(self):
        from fastapi.testclient import TestClient

        team = self.crew("shop")
        agent = self.agent(team["id"], "wren", role="writer", skills=["writing"])
        client = TestClient(create_api(self.app))
        client.put(f"/agents/{agent['id']}/profile/instructions/voice",
                   json={"value": {"tone": "plain"}, "origin": "owner"})
        client.put(f"/agents/{agent['id']}/profile/instructions/voice",
                   json={"value": {"tone": "warm"}, "origin": "owner"})
        stored = client.get(f"/agents/{agent['id']}/profile").json()
        self.assertEqual(stored["namespaces"]["instructions"]["voice"], {"tone": "warm"})
        self.assertTrue(client.get(f"/agents/{agent['id']}/consistency").json()["ok"])


if __name__ == "__main__":
    unittest.main()


class PagePayloadTests(AppCase):
    """a route that exists is not a route whose answer the page can read."""

    def test_every_view_reads_a_key_its_endpoint_actually_sends(self):
        """the bug this exists for: the guardrails panel read {summary, limits} while the
        endpoint answered {guardrails, refused_today}, so it rendered "no limits" forever
        and the pause button, the only way to stand a limit down from here, was never on
        screen. a route check cannot see that, and neither can a person who has never had
        a limit to pause. so the keys are checked against live responses."""
        from fastapi.testclient import TestClient

        api = TestClient(create_api(self.app))
        team = self.crew()
        agent = self.agent(team["id"], "wren", role="writer")
        self.app.guardrails.set(team["id"], "open_cards", 3, actor="owner")
        self.app.kanban.create_task(team["id"], "draft", column="ready")

        contracts = {
            "guardrails": f"/crews/{team['id']}/guardrails",
            "grants": f"/agents/{agent['id']}/grants",
            "context": f"/agents/{agent['id']}/context",
            "sidecars": f"/agents/{agent['id']}/sidecars",
            "workflows": f"/crews/{team['id']}/workflows",
        }
        for name, path in contracts.items():
            payload = api.get(path).json()
            self.assertIsInstance(payload, dict, name)
            # the page has to name at least one key the endpoint sends, or it is reading
            # a shape nobody returns
            self.assertTrue(
                any(f'data.{key}' in POV_PAGE or f'{key}:' in POV_PAGE for key in payload),
                f"{name}: the page reads no key that {path} returns ({sorted(payload)})",
            )

    def test_the_guardrails_panel_reads_the_limits_the_api_sends(self):
        self.assertIn("data.guardrails", POV_PAGE)
        self.assertIn("refused_today", POV_PAGE)
        self.assertNotIn("data.limits", POV_PAGE)


if __name__ == "__main__":
    unittest.main()
