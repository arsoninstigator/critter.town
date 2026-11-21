"""the org chart's moving parts: four seat verbs, a dispatcher, and an objective that asks.

the properties worth holding: freezing releases what the seat was holding, thawing past the
window refuses instead of resuming, burning orphans the children so they finish but never
multiply, dispatch answers "nothing" with a reason and routes by capability rather than by
name, and an objective is only given work when it is behind the clock rather than the target.
"""

from __future__ import annotations

import time
import unittest

from base import AppCase

from core.errors import NotAllowed, ValidationError
from work import dispatch as dispatch_mod
from work import replenish as replenish_mod
from work import seats as seats_mod
from work.replenish import decide, pace


def seat(**over) -> dict:
    base = {"id": "seat_a", "team_id": "t", "name": "wren", "role": "doer", "skills": ["code"],
            "state": "live", "frozen_until": None, "burned_reason": "", "orphaned": False,
            "lineage": "seat_a", "depth": 0, "parent_id": None}
    base.update(over)
    return base


class SeatVerbTests(unittest.TestCase):
    def test_a_live_seat_may_work_and_a_frozen_one_may_not(self):
        self.assertTrue(seats_mod.can_work(seat()))
        self.assertFalse(seats_mod.can_work(seat(state="frozen")))

    def test_freezing_sets_a_window_and_refuses_twice(self):
        out = seats_mod.freeze(seat(), at=1000.0, ttl_ms=1000)
        self.assertTrue(out["ok"])
        self.assertEqual(out["seat"]["state"], "frozen")
        self.assertEqual(out["seat"]["frozen_until"], 1001.0)
        again = seats_mod.freeze(out["seat"], at=1002.0)
        self.assertEqual(again["reason"], "already_frozen")

    def test_a_burned_seat_cannot_be_frozen_or_thawed(self):
        burned = seats_mod.burn(seat(), "replaced by a better model", at=1.0)["seat"]
        self.assertEqual(seats_mod.freeze(burned)["reason"], "seat_is_burned")
        self.assertEqual(seats_mod.thaw(burned)["reason"], "seat_is_burned")

    def test_thawing_inside_the_window_resumes(self):
        frozen = seats_mod.freeze(seat(), at=1000.0, ttl_ms=1000)["seat"]
        out = seats_mod.thaw(frozen, at=1000.5)
        self.assertTrue(out["ok"])
        self.assertEqual(out["seat"]["state"], "live")
        self.assertIsNone(out["seat"]["frozen_until"])

    def test_thawing_past_the_window_refuses_and_names_the_spawn(self):
        frozen = seats_mod.freeze(seat(), at=1000.0, ttl_ms=1000)["seat"]
        out = seats_mod.thaw(frozen, at=5000.0)
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "frozen_window_expired")
        self.assertIn("spawn", out["detail"]["hint"])

    def test_burning_needs_a_reason_of_substance(self):
        self.assertEqual(seats_mod.burn(seat(), "  ")["reason"], "burn_needs_a_reason")
        self.assertEqual(seats_mod.burn(seat(), "x")["reason"], "burn_needs_a_reason")

    def test_duplication_narrows_skills_and_stops_at_the_depth_ceiling(self):
        wide = seat(skills=["code", "design"])
        narrowed = seats_mod.duplicate(wide, {"id": "kid", "skills": ["code"]}, now_ts=1.0)
        self.assertTrue(narrowed["ok"])
        self.assertEqual(narrowed["seat"]["depth"], 1)
        self.assertEqual(narrowed["seat"]["lineage"], "seat_a")
        widened = seats_mod.duplicate(wide, {"id": "kid", "skills": ["code", "design", "ops"]})
        self.assertEqual(widened["reason"], "skill_not_granted")
        deep = seat(depth=3)
        self.assertEqual(seats_mod.duplicate(deep, {"id": "kid"})["reason"], "lineage_too_deep")

    def test_a_frozen_or_orphaned_parent_may_not_spawn(self):
        self.assertEqual(seats_mod.duplicate(seat(state="frozen"), {"id": "kid"})["reason"],
                         "seat_is_frozen")
        self.assertEqual(seats_mod.duplicate(seat(orphaned=True), {"id": "kid"})["reason"],
                         "orphaned_seat_may_not_spawn")

    def test_orphan_only_touches_a_live_seat(self):
        self.assertTrue(seats_mod.orphan(seat())["orphaned"])
        self.assertFalse(seats_mod.orphan(seat(state="frozen"))["orphaned"])


class SeatServiceTests(AppCase):
    def test_freezing_releases_the_lease_it_was_holding(self):
        team = self.crew()
        agent = self.agent(team["id"], "wren")
        card = self.task(team["id"])
        self.app.custody.claim(card["id"], agent["id"], ttl=600)
        frozen = self.app.seats.freeze(agent["id"], "restructuring")
        self.assertEqual(frozen["state"], "frozen")
        self.assertEqual(frozen["released"]["leases_released"], 1)
        self.assertEqual(self.app.custody.held_by(card["id"]), "")

    def test_a_frozen_seat_is_not_offered_a_handoff(self):
        team = self.crew()
        sender = self.agent(team["id"], "sender")
        receiver = self.agent(team["id"], "receiver")
        self.app.seats.freeze(receiver["id"], "on leave")
        with self.assertRaises(NotAllowed):
            self.app.delegation.offer(team["id"], sender["id"], receiver["id"],
                                      "card://" + self.task(team["id"])["id"],
                                      "the card is in review")

    def test_burning_orphans_the_children(self):
        team = self.crew()
        parent = self.agent(team["id"], "parent")
        child = self.app.seats.duplicate(parent["id"], "child", skills=["research"])
        burned = self.app.seats.burn(parent["id"], "replaced")
        self.assertEqual(burned["state"], "burned")
        self.assertEqual(burned["orphaned"], 1)
        self.assertTrue(self.app.seats.get(child["id"])["orphaned"])

    def test_a_frozen_seat_past_its_window_is_retired_by_the_sweep(self):
        team = self.crew()
        agent = self.agent(team["id"], "wren")
        self.app.seats.freeze(agent["id"], "quiet", ttl_days=0.00001)
        out = self.app.seats.sweep_idle(team["id"], at=time.time() + 60)
        self.assertEqual(out["retired"], [agent["id"]])
        self.assertEqual(self.app.seats.get(agent["id"])["state"], "burned")

    def test_the_idle_sweep_is_off_unless_the_crew_declares_it(self):
        team = self.crew()
        agent = self.agent(team["id"], "wren")
        out = self.app.seats.sweep_idle(team["id"], at=time.time() + 86400 * 30)
        self.assertEqual(out["frozen"], [])
        self.assertEqual(self.app.seats.get(agent["id"])["state"], "live")

    def test_the_idle_sweep_freezes_a_quiet_seat_when_declared(self):
        team = self.crew()
        agent = self.agent(team["id"], "wren")
        with self.app.store.tx() as c:
            c.execute("update teams set idle_freeze_days = 7 where id = ?", (team["id"],))
        card = self.task(team["id"])
        self.kanban.assign(card["id"], agent["id"])
        self.kanban.move(card["id"], "in_progress", "system")
        out = self.app.seats.sweep_idle(team["id"], at=time.time() + 86400 * 30)
        self.assertEqual(out["frozen"], [agent["id"]])
        self.assertEqual(self.app.seats.get(agent["id"])["state"], "frozen")


class DispatchTests(unittest.TestCase):
    def card(self, cid: str, kind: str = "code", slice_: str = "", priority: int = 0) -> dict:
        payload = {"kind": kind}
        if slice_:
            payload["slice"] = slice_
        return {"id": cid, "priority": priority, "payload": payload}

    def test_nothing_ready_is_the_quiet_answer(self):
        out = dispatch_mod.next_assignment([], [seat()], set())
        self.assertEqual(out["why"], "nothing_ready")

    def test_a_kind_with_no_evidence_rule_is_unassignable(self):
        out = dispatch_mod.next_assignment([self.card("t1", kind="alchemy")], [seat()], set())
        self.assertEqual(out["why"], "kind_unregistered")
        self.assertEqual(out["kinds"], ["alchemy"])

    def test_a_slice_nobody_holds_has_no_seat(self):
        out = dispatch_mod.next_assignment([self.card("t1", slice_="design")], [seat()], set())
        self.assertEqual(out["why"], "no_seat_for_slice")

    def test_work_is_addressed_by_capability_not_by_name(self):
        other = seat(id="seat_b", skills=["design"])
        out = dispatch_mod.next_assignment([self.card("t1", slice_="design")],
                                           [seat(), other], set())
        self.assertTrue(out["ok"])
        self.assertEqual(out["seat"], "seat_b")

    def test_priority_then_lowest_id_decides(self):
        cards = [self.card("t2", priority=1), self.card("t1", priority=1), self.card("t3", priority=5)]
        out = dispatch_mod.next_assignment(cards, [seat()], set())
        self.assertEqual(out["task_id"], "t3")
        out = dispatch_mod.next_assignment(cards[:2], [seat()], set())
        self.assertEqual(out["task_id"], "t1")

    def test_a_frozen_seat_is_not_in_the_pool(self):
        out = dispatch_mod.next_assignment([self.card("t1")], [seat(state="frozen")], set())
        self.assertEqual(out["why"], "no_seat_for_slice")


class DispatchServiceTests(AppCase):
    def test_dispatch_claims_the_card_it_named(self):
        team = self.crew()
        agent = self.agent(team["id"], "wren", role="doer", skills=["code"])
        card = self.task(team["id"], payload={"kind": "code"})
        out = self.app.dispatch.next(team["id"])
        self.assertTrue(out["ok"])
        self.assertEqual(out["seat"], agent["id"])
        self.assertEqual(self.app.custody.held_by(card["id"]), agent["id"])

    def test_a_leased_card_is_not_handed_out_twice(self):
        team = self.crew()
        self.agent(team["id"], "wren", role="doer", skills=["code"])
        card = self.task(team["id"], payload={"kind": "code"})
        self.app.dispatch.next(team["id"])
        self.assertEqual(self.app.dispatch.next(team["id"])["why"], "nothing_ready")
        self.assertEqual(self.app.custody.held_by(card["id"]) != "", True)


class ReplenishTests(unittest.TestCase):
    def objective(self, **over) -> dict:
        base = {"id": "obj", "team_id": "t", "name": "signups", "baseline": 0.0, "target": 100.0,
                "deadline": 1000.0, "observed_since": 0.0, "kinds": ["code"], "wip_limit": 2,
                "status": "active", "created_date": 0.0}
        base.update(over)
        return base

    def test_pace_compares_the_work_to_the_clock_not_the_target(self):
        shape = pace(self.objective(), 5.0, at=50.0)
        self.assertAlmostEqual(shape["done"], 0.05)
        self.assertAlmostEqual(shape["elapsed"], 0.05)
        self.assertFalse(shape["behind"])

    def test_being_behind_is_the_gap_between_done_and_elapsed(self):
        self.assertTrue(pace(self.objective(), 10.0, at=500.0)["behind"])
        self.assertFalse(pace(self.objective(), 60.0, at=500.0)["behind"])

    def test_no_measurement_is_a_refusal_not_a_pass(self):
        out = decide(self.objective(), [], [], at=10.0)
        self.assertEqual(out["why"], "no_measurement")

    def test_one_card_per_objective(self):
        open_task = {"id": "t1", "objective_id": "obj"}
        out = decide(self.objective(), [{"objective_id": "obj", "value": 1.0, "computed_at": 1.0}],
                     [open_task], at=900.0)
        self.assertEqual(out["why"], "already_queued")

    def test_a_deadline_that_passed_is_a_decision_not_a_target(self):
        out = decide(self.objective(), [{"objective_id": "obj", "value": 1.0, "computed_at": 1.0}],
                     [], at=2000.0)
        self.assertEqual(out["why"], "no_deadline_left")

    def test_a_kind_the_org_cannot_do_is_refused(self):
        out = decide(self.objective(kinds=["alchemy"]),
                     [{"objective_id": "obj", "value": 1.0, "computed_at": 1.0}], [], at=900.0)
        self.assertEqual(out["why"], "kind_unregistered")

    def test_a_retired_objective_is_not_given_work(self):
        out = decide(self.objective(status="retired"), [], [], at=1.0)
        self.assertEqual(out["why"], "objective_retired")


class ReplenishServiceTests(AppCase):
    def test_an_objective_behind_its_clock_gets_one_card_and_then_refuses(self):
        team = self.crew()
        objective = self.app.replenish.create(team["id"], "signups", 0, 100,
                                             time.time() + 86400, ["code"],
                                             observed_since=time.time() - 86400)
        self.app.replenish.measure(objective["id"], 1)
        out = self.app.replenish.replenish(team["id"])
        self.assertEqual(len(out["created"]), 1)
        self.assertEqual(out["created"][0]["payload"]["objective_id"], objective["id"])
        again = self.app.replenish.replenish(team["id"])
        self.assertEqual(again["created"], [])
        self.assertEqual(again["refused"][0]["why"], "already_queued")

    def test_an_on_track_objective_is_left_alone(self):
        team = self.crew()
        objective = self.app.replenish.create(team["id"], "signups", 0, 100,
                                             time.time() + 86400, ["code"],
                                             observed_since=time.time() - 86400)
        self.app.replenish.measure(objective["id"], 99)
        self.assertEqual(self.app.replenish.replenish(team["id"])["refused"][0]["why"], "on_track")

    def test_a_target_equal_to_its_baseline_is_refused(self):
        team = self.crew()
        with self.assertRaises(ValidationError):
            self.app.replenish.create(team["id"], "flat", 5, 5, time.time() + 86400, ["code"])


class WiringTests(AppCase):
    def test_a_guardrail_refusal_records_a_strike(self):
        team = self.crew()
        agent = self.agent(team["id"], "wren")
        card = self.task(team["id"])
        self.kanban.assign(card["id"], agent["id"])
        self.kanban.move(card["id"], "in_progress", "system")
        self.app.guardrails.set(team["id"], "open_cards", 0)
        with self.assertRaises(NotAllowed):
            self.app.guardrails.enforce(team["id"], "dispatch", {},
                                       task=self.kanban.get_task(card["id"]))
        standing = self.app.strikes.standing(agent["id"])
        self.assertGreaterEqual(standing["strikes"], 1)

    def test_a_handoff_leaving_the_crew_needs_a_declared_crossing(self):
        home = self.crew("home")
        other = self.crew("other")
        sender = self.agent(home["id"], "sender")
        receiver = self.agent(other["id"], "receiver")
        card = self.task(home["id"])
        with self.assertRaises(NotAllowed):
            self.app.delegation.offer(home["id"], sender["id"], receiver["id"],
                                      "card://" + card["id"], "the card is in review")

    def test_a_declared_crossing_lets_the_handoff_through(self):
        home = self.crew("home")
        other = self.crew("other")
        sender = self.agent(home["id"], "sender")
        receiver = self.agent(other["id"], "receiver")
        card = self.task(home["id"])
        self.app.crossings.declare(home["id"], [{"id": "door1", "from_scope": home["id"],
                                                    "to_scope": other["id"],
                                                    "kinds": ["request"], "max_per_hour": 10,
                                                     "expires_at": time.time() + 86400,
                                                     "reason": "the review needs a second pair of eyes"}])
        offered = self.app.delegation.offer(home["id"], sender["id"], receiver["id"],
                                           "card://" + card["id"], "the card is in review")
        self.assertEqual(offered["state"], "offered")

    def test_the_brief_carries_an_open_project_and_its_unmet_checks(self):
        team = self.crew()
        self.app.projects.create(team["id"], "launch",
                                 [{"slice": "recipe", "objective_ref": "o"}], 900,
                                 ["unit cost stays above the ceiling for a week"],
                                 objective_ref="o")
        brief = self.app.brief.build(team["id"])
        self.assertEqual(len(brief["projects"]), 1)
        self.assertIn("objectives_present", brief["projects"][0]["unmet"])
