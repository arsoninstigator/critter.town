"""what a crew can reach, what that reach costs it, and who accepted the risk.

the properties under test are the three that make a posture reading worth reading: the inventory is
read off rows rather than declared, a finding key survives a recompute so a dismissal attaches to
the thing, and a dismissal expires so a temporary exception stops being permanent. the last one is
tested from both sides: a lapsed acceptance comes back naming the person, and it does not come back
as if nobody had ever looked at it.
"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from base import AppCase  # noqa: E402
from core.db import new_id, now  # noqa: E402
from core.errors import Conflict, NotFound, ValidationError  # noqa: E402
from work import posture as ps  # noqa: E402


class PureTests(unittest.TestCase):
    def test_severities_are_ordered(self):
        self.assertTrue(ps.at_least("high", "medium"))
        self.assertFalse(ps.at_least("low", "medium"))

    def test_an_unknown_severity_is_refused(self):
        with self.assertRaises(ValidationError):
            ps.rank("urgent")

    def test_a_finding_key_names_the_rule_and_the_thing(self):
        found = ps.finding("egress_unbounded", "*", "high", "detail")
        self.assertEqual(found["key"], "egress_unbounded:*")

    def test_a_finding_cannot_be_built_with_a_severity_nobody_defined(self):
        with self.assertRaises(ValidationError):
            ps.finding("rule", "subject", "spicy", "detail")

    def test_scopes_are_read_from_text_or_a_list(self):
        self.assertEqual(ps._scopes('["read", "write"]'), ["read", "write"])
        self.assertEqual(ps._scopes(None), [])


class ServiceTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew()
        self.doer = self.app.crews.add_agent(self.team["id"], "wren", role="doer")

    def rail(self, kind: str, value, agent_id: str = "") -> dict:
        return self.app.guardrails.set(self.team["id"], kind, value, agent_id=agent_id)

    def secret(self, label: str, scopes=None, expires_at=None) -> str:
        secret_id = new_id("cred")
        with self.app.store.tx() as c:
            c.execute(
                """insert into credential_broker (id, team_id, agent_id, label, proxy_url,
                       token_env, scopes, status, issued_by, expires_at, created_date)
                   values (?,?,?,?,'','',?,'active','owner',?,?)""",
                (secret_id, self.team["id"], self.doer["id"], label,
                 json.dumps(scopes or []), expires_at, now()))
        return secret_id

    def keys(self, team_id: str | None = None) -> list[str]:
        return [f["key"] for f in self.app.posture.findings(team_id or self.team["id"])]

    # ---- the inventory is read, not declared ---------------------------
    def test_the_inventory_names_what_the_rails_allow(self):
        self.rail("egress_hosts", ["api.example.com"])
        inventory = self.app.posture.inventory(self.team["id"])
        self.assertEqual([h["host"] for h in inventory["hosts"]], ["api.example.com"])

    def test_a_rail_names_who_it_was_granted_to(self):
        self.rail("egress_hosts", ["api.example.com"], agent_id=self.doer["id"])
        inventory = self.app.posture.inventory(self.team["id"])
        self.assertEqual(inventory["hosts"][0]["allowed_by"], [self.doer["id"]])

    def test_a_revoked_rail_is_not_in_the_inventory(self):
        rail = self.rail("egress_hosts", ["api.example.com"])
        with self.app.store.tx() as c:
            c.execute("update guardrails set status = 'revoked' where id = ?", (rail["id"],))
        self.assertEqual(self.app.posture.inventory(self.team["id"])["hosts"], [])

    def test_a_credential_is_named_by_its_label_and_never_by_its_value(self):
        self.secret("stripe", scopes=["read"])
        inventory = self.app.posture.inventory(self.team["id"])
        self.assertEqual(inventory["secrets"][0]["label"], "stripe")
        self.assertEqual(inventory["secrets"][0]["token_env"], "")

    def test_the_counts_say_how_much_reach_there_is(self):
        self.rail("egress_hosts", ["a.example.com", "b.example.com"])
        self.secret("stripe", scopes=["read"])
        counts = self.app.posture.inventory(self.team["id"])["counts"]
        self.assertEqual(counts["hosts"], 2)
        self.assertEqual(counts["secrets"], 1)
        self.assertEqual(counts["external_agents"], 0)

    # ---- rules ---------------------------------------------------------
    def test_an_open_allowlist_is_a_high_finding(self):
        self.rail("egress_hosts", ["*"])
        found = [f for f in self.app.posture.findings(self.team["id"])
                 if f["rule"] == "egress_unbounded"]
        self.assertEqual(found[0]["severity"], "high")

    def test_a_wildcard_subdomain_is_medium_not_high(self):
        self.rail("egress_hosts", ["*.example.com"])
        found = [f for f in self.app.posture.findings(self.team["id"])
                 if f["rule"] == "egress_wildcard"]
        self.assertEqual(found[0]["severity"], "medium")

    def test_a_named_host_is_not_a_finding(self):
        self.rail("egress_hosts", ["api.example.com"])
        self.assertEqual([f for f in self.keys() if f.startswith("egress_")], [])

    def test_an_expired_credential_is_a_finding_before_a_run_discovers_it(self):
        self.secret("stripe", scopes=["read"], expires_at=now() - 60)
        self.assertIn("secret_expired:stripe", self.keys())

    def test_a_credential_expiring_within_the_week_is_a_low_finding(self):
        self.secret("stripe", scopes=["read"], expires_at=now() + 3 * 86400)
        found = [f for f in self.app.posture.findings(self.team["id"])
                 if f["key"] == "secret_expiring:stripe"]
        self.assertEqual(found[0]["severity"], "low")

    def test_a_credential_with_no_scopes_is_a_finding(self):
        self.secret("stripe")
        self.assertIn("secret_unscoped:stripe", self.keys())

    def test_a_credential_that_reaches_everything_is_a_finding(self):
        self.secret("stripe", scopes=["*"])
        self.assertIn("secret_wide:stripe", self.keys())

    def test_an_external_reviewer_is_high_because_the_signature_cannot_be_checked(self):
        self.app.crews.add_agent(self.team["id"], "kilo", role="reviewer", hosting="external")
        found = [f for f in self.app.posture.findings(self.team["id"])
                 if f["key"] == "external_seat:kilo"]
        self.assertEqual(found[0]["severity"], "high")

    def test_an_external_doer_is_only_worth_noting(self):
        self.app.crews.add_agent(self.team["id"], "kilo", role="doer", hosting="external")
        found = [f for f in self.app.posture.findings(self.team["id"])
                 if f["key"] == "external_agent:kilo"]
        self.assertEqual(found[0]["severity"], "info")

    def test_a_disconnected_connector_is_a_source_that_went_quiet(self):
        with self.app.store.tx() as c:
            c.execute(
                """insert into connectors (id, team_id, kind, name, config, status,
                       created_date) values (?,?,?,?,'{}','stale',?)""",
                (new_id("con"), self.team["id"], "notion", "handbook", now()))
        self.assertIn("connector_down:handbook", self.keys())

    def test_a_skill_that_may_call_any_tool_is_a_grant_nobody_wrote_down(self):
        self.app.skills.create(self.team["id"], name="anything",
                               description="use when a card asks for anything at all", body="x",
                               allowed_tools=["*"])
        self.assertIn("skill_open_tools:anything", self.keys())

    def test_a_crew_with_no_egress_rail_is_told_its_reach_is_the_sandbox_default(self):
        self.assertIn(f"no_egress_declared:{self.team['id']}", self.keys())

    def test_findings_are_ordered_heaviest_first(self):
        self.rail("egress_hosts", ["*", "*.example.com"])
        severities = [ps.rank(f["severity"]) for f in self.app.posture.findings(self.team["id"])]
        self.assertEqual(severities, sorted(severities, reverse=True))

    # ---- dismissals ----------------------------------------------------
    def dismiss(self, key: str, until_hours: float = 24, owner: str = "anne") -> dict:
        return self.app.posture.dismiss(self.team["id"], key, owner, "accepted for the launch",
                                        now() + until_hours * 3600)

    def lapse(self, key: str, owner: str = "anne") -> str:
        """a dismissal written straight to the table with a date that has already passed, which is
        what a lapsed acceptance looks like the morning after: the same row, a date behind it."""
        dismissal_id = new_id("dsm")
        with self.app.store.tx() as c:
            c.execute(
                """insert into posture_dismissals (id, team_id, finding_key, severity, owner,
                       reason, until_date, created_date)
                   values (?,?,?,'high',?,'accepted for the launch',?,?)""",
                (dismissal_id, self.team["id"], key, owner, now() - 60, now() - 86400))
        return dismissal_id

    def test_a_dismissal_needs_a_named_person(self):
        self.rail("egress_hosts", ["*"])
        with self.assertRaises(ValidationError):
            self.app.posture.dismiss(self.team["id"], "egress_unbounded:*", "",
                                     "accepted", now() + 3600)

    def test_a_dismissal_needs_a_reason(self):
        self.rail("egress_hosts", ["*"])
        with self.assertRaises(ValidationError):
            self.app.posture.dismiss(self.team["id"], "egress_unbounded:*", "anne", "",
                                     now() + 3600)

    def test_a_dismissal_needs_an_expiry(self):
        self.rail("egress_hosts", ["*"])
        with self.assertRaises(ValidationError):
            self.app.posture.dismiss(self.team["id"], "egress_unbounded:*", "anne", "why", 0)

    def test_a_dismissal_that_has_already_expired_dismisses_nothing(self):
        self.rail("egress_hosts", ["*"])
        with self.assertRaises(ValidationError):
            self.app.posture.dismiss(self.team["id"], "egress_unbounded:*", "anne", "why",
                                     now() - 60)

    def test_a_dismissal_must_name_a_finding_that_exists(self):
        with self.assertRaises(NotFound):
            self.dismiss("egress_unbounded:nothing.example.com")

    def test_a_dismissed_finding_leaves_the_open_list(self):
        self.rail("egress_hosts", ["*"])
        self.dismiss("egress_unbounded:*")
        reading = self.app.posture.reading(self.team["id"])
        self.assertNotIn("egress_unbounded:*", [f["key"] for f in reading["findings"]])
        self.assertEqual([f["key"] for f in reading["accepted"]], ["egress_unbounded:*"])

    def test_the_accepted_finding_keeps_who_and_until(self):
        self.rail("egress_hosts", ["*"])
        self.dismiss("egress_unbounded:*", owner="anne")
        accepted = self.app.posture.reading(self.team["id"])["accepted"][0]
        self.assertEqual(accepted["dismissed"]["owner"], "anne")
        self.assertEqual(accepted["dismissed"]["reason"], "accepted for the launch")

    def test_a_lapsed_acceptance_comes_back_naming_whoever_gave_it(self):
        self.rail("egress_hosts", ["*"])
        dismissal = self.lapse("egress_unbounded:*")
        reading = self.app.posture.reading(self.team["id"])
        back = [f for f in reading["findings"] if f["key"] == "egress_unbounded:*"]
        self.assertEqual(back[0]["lapsed"]["owner"], "anne")
        self.assertEqual(back[0]["lapsed"]["id"], dismissal)

    def test_an_expired_dismissal_can_be_left_out_of_the_list(self):
        self.rail("egress_hosts", ["*"])
        self.lapse("egress_unbounded:*")
        self.assertEqual(self.app.posture.dismissals(self.team["id"], include_expired=False), [])

    def test_a_dismissal_survives_a_recompute_of_the_inventory(self):
        self.rail("egress_hosts", ["*"])
        self.dismiss("egress_unbounded:*")
        self.rail("egress_hosts", ["api.example.com"], agent_id=self.doer["id"])
        self.assertEqual([f["key"] for f in self.app.posture.reading(self.team["id"])["accepted"]],
                         ["egress_unbounded:*"])

    # ---- what blocks ---------------------------------------------------
    def test_a_crew_that_never_stated_a_threshold_blocks_nothing(self):
        self.rail("egress_hosts", ["*"])
        reading = self.app.posture.reading(self.team["id"])
        self.assertIsNone(reading["block_at"])
        self.assertEqual(reading["blocking"], [])

    def test_a_stated_threshold_blocks_at_that_severity(self):
        self.app.crews.update_team(self.team["id"], orchestration={"posture": {"block_at": "high"}})
        self.rail("egress_hosts", ["*"])
        reading = self.app.posture.reading(self.team["id"])
        self.assertEqual([f["key"] for f in reading["blocking"]], ["egress_unbounded:*"])

    def test_a_threshold_above_the_findings_blocks_nothing(self):
        self.app.crews.update_team(self.team["id"],
                                   orchestration={"posture": {"block_at": "critical"}})
        self.rail("egress_hosts", ["*"])
        self.assertEqual(self.app.posture.reading(self.team["id"])["blocking"], [])

    def test_a_dismissal_lifts_the_block(self):
        self.app.crews.update_team(self.team["id"], orchestration={"posture": {"block_at": "high"}})
        self.rail("egress_hosts", ["*"])
        self.dismiss("egress_unbounded:*")
        self.assertEqual(self.app.posture.reading(self.team["id"])["blocking"], [])

    def test_a_stated_threshold_refuses_the_dispatch_before_a_token_is_spent(self):
        self.app.crews.update_team(self.team["id"], orchestration={"posture": {"block_at": "high"}})
        self.rail("egress_hosts", ["*"])
        task = self.app.kanban.create_task(self.team["id"], "ship it", description="x",
                                           column="ready")
        with self.assertRaises(Conflict) as caught:
            self.app.runner.dispatch(task["id"], max_turns=1)
        self.assertEqual(caught.exception.details["key"], "egress_unbounded:*")

    def test_the_run_records_the_reading_when_the_crew_stated_a_threshold(self):
        self.app.crews.update_team(self.team["id"],
                                   orchestration={"posture": {"block_at": "critical"}})
        self.rail("egress_hosts", ["*"])
        task = self.app.kanban.create_task(self.team["id"], "ship it", description="x",
                                           column="ready")
        out = self.app.runner.dispatch(task["id"], max_turns=1)
        self.assertEqual(out["posture"]["verdict"], "clear")
        self.assertEqual(out["posture"]["block_at"], "critical")

    def test_the_run_says_the_crew_stated_nothing_rather_than_pretending_it_passed(self):
        task = self.app.kanban.create_task(self.team["id"], "ship it", description="x",
                                           column="ready")
        out = self.app.runner.dispatch(task["id"], max_turns=1)
        self.assertEqual(out["posture"]["verdict"], "unset")

    # ---- routes --------------------------------------------------------
    def test_the_routes_answer(self):
        from fastapi.testclient import TestClient
        from api.routes import create_api
        self.rail("egress_hosts", ["*"])
        client = TestClient(create_api(self.app))
        reading = client.get(f"/crews/{self.team['id']}/posture").json()
        self.assertEqual(reading["counts"]["high"], 1)
        inventory = client.get(f"/crews/{self.team['id']}/posture/inventory").json()
        self.assertEqual(inventory["hosts"][0]["host"], "*")
        found = client.get(f"/crews/{self.team['id']}/posture/findings",
                           params={"severity": "high"}).json()
        self.assertEqual(found["count"], 1)
        dismissed = client.post(f"/crews/{self.team['id']}/posture/dismiss",
                                json={"key": "egress_unbounded:*", "owner": "anne",
                                      "reason": "launch week", "until": now() + 86400}).json()
        self.assertEqual(dismissed["owner"], "anne")
        listed = client.get(f"/crews/{self.team['id']}/posture/dismissals").json()
        self.assertEqual(listed["count"], 1)
