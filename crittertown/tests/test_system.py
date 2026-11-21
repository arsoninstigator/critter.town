"""the four units above a card: project, claim, strike, crossing.

the properties worth holding: a gate opens on rows and not on an opinion, a synthesis
accounts for every claim it was given, a freeze is a count rather than a mood, and a wall
denies by default.
"""

from __future__ import annotations

import unittest

from base import AppCase  # noqa: E402

from core.errors import ValidationError  # noqa: E402
from work import claims as discourse  # noqa: E402
from work import permeability as walls  # noqa: E402
from work import projects as proj  # noqa: E402
from work import strikes as strikes_rules  # noqa: E402


def project(**over):
    base = {
        "id": "p1", "team_id": "t1", "name": "launch", "phase": "inception",
        "started_at": 0.0, "phase_started_at": 0.0, "objective_ref": "obj1",
        "slices": [{"slice": "recipe", "objective_ref": "obj1"}],
        "unit_cost_ceiling_cents": 900, "unit_price_cents": 1500, "min_margin_pct": 0.3,
        "kill_criteria": ["unit cost stays above the ceiling for a week"], "closed_reason": "",
    }
    base.update(over)
    return base


OBJECTIVES = [{"ref": "obj1", "source": "sql:select count(*) from orders", "self_reported": False}]


class ProjectTests(unittest.TestCase):
    def test_structure_is_checked_before_progress(self):
        faults = {c["id"] for c in proj.validate(project(slices=[], unit_cost_ceiling_cents=0,
                                                         kill_criteria=["it fails"]))}
        self.assertIn("no_slices", faults)
        self.assertIn("no_unit_cost_ceiling", faults)
        self.assertIn("kill_criteria_too_thin", faults)

    def test_price_must_beat_the_ceiling(self):
        faults = {c["id"] for c in proj.validate(project(unit_price_cents=800))}
        self.assertIn("price_not_above_ceiling", faults)

    def test_inception_needs_an_objective_measured_from_outside(self):
        unmet = {c["id"] for c in proj.evaluate(project(), {"objectives": []})}
        self.assertIn("objectives_present", unmet)
        self_measured = [{"ref": "obj1", "source": "self", "self_reported": True}]
        unmet = {c["id"] for c in proj.evaluate(project(), {"objectives": self_measured})}
        self.assertIn("objectives_externally_measured", unmet)
        ok = proj.evaluate(project(), {"objectives": OBJECTIVES})
        self.assertFalse([c for c in ok if not c["ok"]])

    def test_two_objectives_reading_one_source_is_one_number_in_two_hats(self):
        slices = [{"slice": "a", "objective_ref": "obj1"}, {"slice": "b", "objective_ref": "obj2"}]
        objectives = [{"ref": "obj1", "source": "sql:select 1"}, {"ref": "obj2", "source": "sql:select 1"}]
        unmet = {c["id"] for c in proj.evaluate(project(slices=slices), {"objectives": objectives})}
        self.assertIn("sources_distinct", unmet)

    def test_launch_reads_a_live_quote_and_a_margin(self):
        launch = project(phase="launch")
        unmet = {c["id"] for c in proj.evaluate(launch, {"live_quote_cents": 0})}
        self.assertIn("live_quote", unmet)
        thin = {c["id"] for c in proj.evaluate(launch, {"live_quote_cents": 1400,
                                                        "payment_rail_verified": True})}
        self.assertIn("unit_margin", thin)
        fine = proj.evaluate(launch, {"live_quote_cents": 700, "payment_rail_verified": True})
        self.assertFalse([c for c in fine if not c["ok"]])

    def test_operate_needs_a_month_and_daily_samples(self):
        operate = project(phase="operate", phase_started_at=0.0)
        unmet = {c["id"] for c in proj.evaluate(operate, {"now": 5 * 86400, "metric_samples": 3})}
        self.assertIn("operated_a_month", unmet)
        self.assertIn("metric_sampled_daily", unmet)
        ok = proj.evaluate(operate, {"now": 31 * 86400, "metric_samples": 31,
                                     "guardrail_refusals": 0})
        self.assertFalse([c for c in ok if not c["ok"]])

    def test_review_has_two_exits_and_no_third(self):
        review = project(phase="review")
        clean = {"readings_disagree": [], "open_audit_alerts": 0}
        self.assertEqual(proj.advance(review, {**clean, "metric_moved": True})["phase"], "closed")
        self.assertEqual(proj.advance(review, {**clean, "metric_moved": False})["phase"], "killed")
        # a review with no verdict is not a third exit, it is a gate that has not opened
        self.assertEqual(proj.advance(review, clean)["reason"], "gate_unmet")

    def test_a_kill_needs_a_declared_criterion(self):
        with self.assertRaises(ValidationError):
            proj.kill(project(), "the vibe was off", "we did not like how it went")
        with self.assertRaises(ValidationError):
            proj.kill(project(), "unit cost stays above the ceiling for a week", "bad")
        killed = proj.kill(project(), "unit cost stays above the ceiling for a week",
                           "quotes never came under the ceiling")
        self.assertEqual(killed["phase"], "killed")

    def test_a_terminal_project_does_not_advance(self):
        out = proj.advance(project(phase="closed"), {})
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "terminal_phase")


class ClaimTests(unittest.TestCase):
    def test_a_fact_needs_a_reference_and_a_plan_may_not_be_certain(self):
        faults = {f["rule"] for f in discourse.validate_message({
            "id": "m1", "intent": "argue",
            "claims": [{"id": "c1", "kind": "fact", "statement": "it works", "confidence": 0.9},
                       {"id": "c2", "kind": "plan", "statement": "we ship", "confidence": 0.95}]})}
        self.assertIn("fact_without_evidence", faults)
        self.assertIn("plan_stated_as_fact", faults)

    def test_a_message_has_to_say_something(self):
        faults = {f["rule"] for f in discourse.validate_message({"id": "m1", "intent": "x",
                                                                "claims": [], "questions": []})}
        self.assertIn("message_says_nothing", faults)

    def test_opposing_a_claim_nobody_made_is_a_fault(self):
        faults = {f["rule"] for f in discourse.validate_message({
            "id": "m1", "intent": "x",
            "claims": [{"id": "c1", "kind": "inference", "statement": "a", "confidence": 0.6,
                        "opposes": ["ghost"]}]})}
        self.assertIn("opposes_an_absent_claim", faults)

    def test_a_synthesis_accounts_for_every_claim(self):
        branches = [{"claims": [
            {"id": "c1", "kind": "fact", "statement": "a", "evidence": "run:1", "confidence": 0.9, "by": "wren"},
            {"id": "c2", "kind": "inference", "statement": "b", "confidence": 0.7, "by": "sun", "opposes": ["c1"]},
        ]}]
        out = discourse.synthesize(branches)
        self.assertEqual(len(out["kept"]), 0)
        self.assertEqual(len(out["challenged"]), 2)
        self.assertTrue(discourse.is_sound(out))

    def test_dropping_a_sourced_claim_needs_a_source(self):
        branches = [{"claims": [{"id": "c1", "kind": "fact", "statement": "a",
                                 "evidence": "run:1", "confidence": 0.9, "by": "wren"}]}]
        vibes = discourse.synthesize(branches, {"c1": "i disagree"})
        self.assertIn("rebuttal_needs_evidence", {f["rule"] for f in vibes["faults"]})
        sourced = discourse.synthesize(branches, {"c1": "run:2 shows otherwise"})
        self.assertEqual(len(sourced["dropped"]), 1)
        self.assertTrue(discourse.is_sound(sourced))

    def test_agreement_is_not_evidence(self):
        branches = [{"claims": [
            {"id": "c1", "kind": "inference", "statement": "same", "confidence": 0.8, "by": "wren"},
            {"id": "c2", "kind": "inference", "statement": "same", "confidence": 0.8, "by": "sun"},
        ]}]
        out = discourse.synthesize(branches)
        self.assertEqual(sorted(out["agreement_without_evidence"]), ["c1", "c2"])


class StrikeTests(unittest.TestCase):
    def test_two_strikes_freeze_a_seat_and_a_frozen_seat_is_spared(self):
        standing = {"seat": "wren", "strikes": 0, "state": "active", "last_strike": None}
        first = strikes_rules.apply(standing, "guardrail_refused")
        self.assertFalse(first["froze"])
        second = strikes_rules.apply(first, "guardrail_refused")
        self.assertTrue(second["froze"])
        self.assertEqual(second["state"], "frozen")
        third = strikes_rules.apply(second, "guardrail_refused")
        self.assertEqual(third["spared"], "already_frozen")

    def test_burn_has_no_inverse(self):
        burned = {"seat": "wren", "strikes": 9, "state": "burned", "last_strike": None}
        self.assertEqual(strikes_rules.apply(burned, "x")["spared"], "burned")
        self.assertEqual(strikes_rules.clear(burned)["state"], "burned")
        self.assertEqual(strikes_rules.clear({"seat": "w", "strikes": 2, "state": "frozen"})["state"], "active")


class CrossingTests(unittest.TestCase):
    DECLS = [{"id": "c1", "from_scope": "crew-a", "to_scope": "crew-b", "kinds": ["finding", "request"],
              "max_per_hour": 60, "expires_at": 2000.0, "reason": "shared launch work",
              "authorized_by": "owner"}]

    def test_an_unenforceable_crossing_is_refused(self):
        bad = [{"id": "x", "from_scope": "a", "to_scope": "a", "kinds": [], "max_per_hour": 0,
                "expires_at": None, "reason": ""}]
        problems = walls.compile_crossings(bad, ["a"], 0.0)["problems"]
        self.assertEqual(len(problems), 1)
        for rule in ("self_crossing", "no_kinds", "no_rate", "no_expiry", "no_reason"):
            self.assertIn(rule, problems[0]["problem"])

    def test_default_is_deny(self):
        crossings = walls.compile_crossings(self.DECLS, ["crew-a", "crew-b"], 0.0)["crossings"]
        self.assertEqual(walls.resolve(crossings, "crew-b", "crew-a", "finding", 10.0)["reason"], "no_crossing")
        self.assertEqual(walls.resolve(crossings, "crew-a", "crew-b", "receipt", 10.0)["reason"], "kind_not_allowed")
        self.assertEqual(walls.resolve(crossings, "crew-a", "crew-b", "finding", 3000.0)["reason"], "crossing_expired")
        self.assertTrue(walls.resolve(crossings, "crew-a", "crew-b", "finding", 10.0)["ok"])

    def test_the_narrower_door_wins(self):
        wide = {**self.DECLS[0], "id": "wide", "kinds": ["finding", "request", "signal"]}
        narrow = {**self.DECLS[0], "id": "narrow", "kinds": ["finding"]}
        crossings = walls.compile_crossings([wide, narrow], ["crew-a", "crew-b"], 0.0)["crossings"]
        self.assertEqual(walls.resolve(crossings, "crew-a", "crew-b", "finding", 10.0)["crossing"]["id"], "narrow")

    def test_the_bucket_refills_continuously(self):
        crossing = {"max_per_hour": 60}
        burst = walls.burst_for(60)
        spent = walls.consume(crossing, burst, 0.0, 0.0)
        self.assertTrue(spent["allowed"])
        empty = walls.consume(crossing, 0.0, 0.0, 0.0)
        self.assertFalse(empty["allowed"])
        self.assertGreater(empty["retry_after_ms"], 0)
        later = walls.consume(crossing, 0.0, 0.0, 60.0)
        self.assertTrue(later["allowed"])

    def test_size_is_a_door_of_its_own(self):
        decls = [{**self.DECLS[0], "max_bytes": 10}]
        crossings = walls.compile_crossings(decls, ["crew-a", "crew-b"], 0.0)["crossings"]
        out = walls.authorize(crossings, "crew-a", "crew-b", "finding", 10.0, size_bytes=99)
        self.assertEqual(out["reason"], "too_large")


class SystemServiceTests(AppCase):
    def test_a_project_advances_on_rows_and_kills_on_a_declared_criterion(self):
        team = self.crew("projects")
        row = self.app.projects.create(
            team["id"], "launch a recipe",
            [{"slice": "recipe", "objective_ref": "obj1"}], 900,
            ["unit cost stays above the ceiling for a week"], objective_ref="obj1",
            unit_price_cents=1500, min_margin_pct=0.3)
        self.assertEqual(row["phase"], "inception")
        out = self.app.projects.advance(row["id"], {"objectives": OBJECTIVES})
        self.assertEqual(out["project"]["phase"], "build")
        blocked = self.app.projects.advance(row["id"], {})
        self.assertFalse(blocked["ok"])
        self.assertIn("every_slice_started", {c["id"] for c in blocked["unmet"]})
        killed = self.app.projects.kill(row["id"], "unit cost stays above the ceiling for a week",
                                        "quotes never came under the ceiling")
        self.assertEqual(killed["phase"], "killed")

    def test_a_message_with_a_bad_claim_writes_nothing(self):
        team = self.crew("claims")
        with self.assertRaises(ValidationError):
            self.app.claims.post(team["id"], "thread-1", "decide", [
                {"id": "c1", "kind": "fact", "statement": "it works", "confidence": 0.9}])
        self.assertEqual(self.app.claims.thread(team["id"], "thread-1"), [])
        posted = self.app.claims.post(team["id"], "thread-1", "decide", [
            {"id": "c1", "kind": "fact", "statement": "it works", "evidence": "run:1",
             "confidence": 0.9}])
        self.assertEqual(posted["claims"], ["c1"])

    def test_a_frozen_seat_is_not_offered_work(self):
        team = self.crew("strikes")
        agent = self.agent(team["id"], "wren", role="doer")
        self.app.strikes.record(team["id"], agent["id"], "guardrail_refused")
        self.app.strikes.record(team["id"], agent["id"], "guardrail_refused")
        self.assertFalse(self.app.strikes.check(agent["id"])["allowed"])
        cleared = self.app.strikes.clear(team["id"], agent["id"], "owner")
        self.assertEqual(cleared["state"], "active")
        self.assertTrue(self.app.strikes.check(agent["id"])["allowed"])

    def test_crossings_are_declared_then_spent(self):
        team = self.crew("walls")
        other = self.crew("walls-other")
        declared = self.app.crossings.declare(team["id"], [{
            "id": "c1", "from_scope": team["id"], "to_scope": other["id"], "kinds": ["finding"],
            "max_per_hour": 6, "expires_at": 9e12, "reason": "shared launch work",
            "authorized_by": "owner"}])
        self.assertEqual(declared["declared"], 1)
        first = self.app.crossings.authorize(team["id"], team["id"], other["id"], "finding")
        self.assertTrue(first["ok"])
        denied = self.app.crossings.authorize(team["id"], other["id"], team["id"], "finding")
        self.assertEqual(denied["reason"], "no_crossing")
        crossing = self.app.crossings.list(team["id"])[0]
        revoked = self.app.crossings.revoke(crossing["id"], "drain", "owner")
        self.assertFalse(revoked["enabled"])
        self.assertEqual(self.app.crossings.authorize(team["id"], team["id"], other["id"], "finding")["reason"],
                         "crossing_disabled")


if __name__ == "__main__":
    unittest.main()
