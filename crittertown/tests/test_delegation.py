"""delegation, money, and the kind of worker an agent is.

the properties worth holding: state crosses a boundary and a summary does not, only the
addressee may answer, a receiver cannot accept work whose precondition does not hold, a
silent offer becomes a row rather than an absence, an unowned part of a split is an alarm,
the budget check and the budget claim are one act, and a kind of worker caps both the seats
it may fill and the slices it may read.
"""

from __future__ import annotations

import unittest

from base import AppCase  # noqa: E402

from core.errors import ValidationError  # noqa: E402
from work import delegation  # noqa: E402


class HandoffRulesTests(unittest.TestCase):
    def offer(self, **over) -> dict:
        base = {"id": "hand_1", "from_agent_id": "a", "to_agent_id": "b",
                "payload_ref": "doc://specs/1", "precondition": "tests pass",
                "timeout_seconds": 600, "state": "offered", "offered_at": 1000.0}
        return {**base, **over}

    def test_a_handoff_without_a_reference_is_refused(self):
        self.assertIn("payload_ref_must_be_a_reference",
                      delegation.validate(self.offer(payload_ref="it is in my head")))

    def test_a_handoff_without_a_precondition_is_refused(self):
        self.assertIn("precondition_required", delegation.validate(self.offer(precondition=" ")))

    def test_self_handoff_is_refused(self):
        self.assertIn("self_handoff", delegation.validate(self.offer(to_agent_id="a")))

    def test_only_the_addressee_may_accept(self):
        with self.assertRaises(ValidationError) as caught:
            delegation.transition(self.offer(), {"type": "accept", "actor": "a",
                                                 "precondition_held": True}, 1001.0)
        self.assertIn("only the addressee may accept", caught.exception.message)

    def test_an_unheld_precondition_is_refused_rather_than_accepted(self):
        with self.assertRaises(ValidationError) as caught:
            delegation.transition(self.offer(), {"type": "accept", "actor": "b",
                                                 "precondition_held": False}, 1001.0)
        self.assertIn("precondition not satisfied", caught.exception.message)

    def test_an_offer_that_timed_out_cannot_be_answered(self):
        with self.assertRaises(ValidationError) as caught:
            delegation.transition(self.offer(), {"type": "accept", "actor": "b",
                                                 "precondition_held": True}, 9999.0)
        self.assertIn("timed out", caught.exception.message)

    def test_a_rejection_needs_a_reason(self):
        with self.assertRaises(ValidationError) as caught:
            delegation.transition(self.offer(), {"type": "reject", "actor": "b", "reason": ""},
                                  1001.0)
        self.assertIn("a_rejection_needs_a_reason", caught.exception.message)

    def test_an_answered_offer_cannot_be_answered_again(self):
        accepted = delegation.transition(self.offer(), {"type": "accept", "actor": "b",
                                                        "precondition_held": True}, 1001.0)
        with self.assertRaises(ValidationError):
            delegation.transition(accepted, {"type": "reject", "actor": "b", "reason": "x"}, 1002.0)

    def test_expiry_is_an_event_not_an_absence(self):
        offers = [self.offer(), self.offer(id="hand_2", offered_at=5000.0)]
        self.assertEqual([o["id"] for o in delegation.open_overdue(offers, 1700.0)], ["hand_1"])

    def test_an_orphan_partition_is_named(self):
        self.assertEqual(delegation.orphan_partitions(["a", "b"], {"a": "wren", "b": ""}), ["b"])

    def test_an_envelope_must_name_its_authority(self):
        problems = delegation.validate_envelope({
            "kind": "finding", "payload_ref": "doc://x", "authority_basis": "",
            "hops": 1, "max_hops": 3})
        self.assertIn("authority_basis_required", problems)

    def test_a_relay_cannot_hop_forever(self):
        problems = delegation.validate_envelope({
            "kind": "signal", "payload_ref": "url://x", "authority_basis": "shift",
            "hops": 4, "max_hops": 3})
        self.assertTrue(any(p.startswith("hop_limit_reached") for p in problems))


class AuthorityTests(unittest.TestCase):
    def test_authority_is_an_intersection_with_a_trace(self):
        from agents import authority

        out = authority.effective_home(
            [{"id": "root", "ceiling": {"tools": ["shell", "git", "stripe"], "spend": 100}},
             {"id": "tier", "ceiling": {"tools": ["shell", "git"], "spend": 40}}],
            {"tools": ["shell", "git", "stripe"], "spend": 50})
        self.assertEqual(out["grants"]["tools"], ["git", "shell"])
        self.assertEqual(out["grants"]["spend"], 40)
        dropped = [entry for entry in out["trace"] if not entry["kept"]]
        self.assertEqual(dropped, [{"tool": "stripe", "kept": False, "dropped_by": "tier"}])

    def test_a_secondment_spends_nothing_unless_the_scope_says_so(self):
        from agents import authority

        out = authority.effective_in_host(
            [{"id": "host", "ceiling": {"tools": ["shell"], "spend": 99}}],
            {"tools": ["shell", "git"], "spend": 50},
            {"scope": {"tools": ["shell", "git"]}})
        self.assertEqual(out["mode"], "host")
        self.assertEqual(out["grants"]["spend"], 0)
        self.assertEqual(out["grants"]["tools"], ["shell"])

    def test_an_elevation_is_refused_with_a_reason(self):
        from agents import authority

        base = {"tools": ["shell"], "spend": 10}
        elevation = {"id": "e1", "issuer": "root", "adds": ["deploy"],
                     "expires_at": "2030-01-01", "max_uses": 1, "used": 0}
        context = {"now": "2026-11-21", "target_chain": ["root"], "day_of_week": 1,
                   "hour_utc": 10,
                   "issuer_ceiling": {"tools": ["shell"], "spend": 10}}
        beyond = authority.apply_elevation(base, elevation, context)
        self.assertEqual(beyond["reason"], "issuer_lacks:deploy")
        missing = authority.apply_elevation(base, {**elevation, "issuer": "nobody"}, context)
        self.assertEqual(missing["reason"], "issuer_not_ancestor")
        used = authority.apply_elevation(base, {**elevation, "used": 1}, context)
        self.assertEqual(used["reason"], "exhausted")

    def test_a_spend_limit_that_nobody_set_refuses_a_cost(self):
        from agents import authority

        self.assertFalse(authority.can_afford({"tools": [], "spend": 0}, 5))
        self.assertTrue(authority.can_afford({"tools": [], "spend": 0}, 0))


class QueueTests(unittest.TestCase):
    def message(self, **over) -> dict:
        base = {"id": "msg_1", "state": "queued", "attempts": 0, "max_attempts": 8,
                "next_attempt_at": 1000.0, "expires_at": 100000.0, "holder": "",
                "locked_until": None}
        return {**base, **over}

    def test_jitter_is_deterministic_and_inside_the_band(self):
        from work import delegation

        first = delegation.jitter_factor("msg_1", 1)
        self.assertEqual(first, delegation.jitter_factor("msg_1", 1))
        self.assertTrue(0.5 <= first < 1.5)

    def test_backoff_grows_and_caps(self):
        from work import delegation

        small = delegation.backoff_seconds("msg_1", 1)
        bigger = delegation.backoff_seconds("msg_1", 5)
        capped = delegation.backoff_seconds("msg_1", 40)
        self.assertLess(small, bigger)
        self.assertLessEqual(capped, delegation.DEFAULT_BACKOFF["cap"] * 1.5)

    def test_a_lease_that_expires_returns_the_message(self):
        from work import delegation

        leased = delegation.lease(self.message(), "wren", 1000.0, ttl=60)
        self.assertEqual(delegation.reclaim(leased, 1030.0)["state"], "leased")
        back = delegation.reclaim(leased, 1070.0)
        self.assertEqual(back["state"], "queued")
        self.assertEqual(back["attempts"], 1)

    def test_a_nack_near_the_ttl_dies_instead_of_retrying_into_it(self):
        from work import delegation

        dying = delegation.nack(self.message(expires_at=1002.0), 1000.0)
        self.assertEqual(dying["state"], "dead")
        self.assertEqual(dying["dead_reason"], "ttl_would_expire_before_retry")

    def test_attempts_exhausted_is_a_dead_letter(self):
        from work import delegation

        out = delegation.nack(self.message(attempts=7, max_attempts=8), 1000.0)
        self.assertEqual((out["state"], out["dead_reason"]), ("dead", "attempts_exhausted"))

    def test_a_route_refuses_size_expiry_and_rate(self):
        from work import delegation

        route = {"expires_at": 5000.0, "max_bytes": 100, "max_per_hour": 2, "used_this_hour": 0}
        self.assertEqual(delegation.check_route(route, 1000.0, size=200),
                         "route_payload_too_large")
        self.assertEqual(delegation.check_route({**route, "used_this_hour": 2}, 1000.0),
                         "route_rate_exhausted")
        self.assertEqual(delegation.check_route(route, 6000.0), "route_expired")
        self.assertEqual(delegation.check_route(route, 1000.0, size=10), "")


class DelegationServiceTests(AppCase):
    def setUp(self):
        super().setUp()
        self.team = self.crew("delegation")
        self.sender = self.agent(self.team["id"], "wren", role="doer")
        self.receiver = self.agent(self.team["id"], "sun", role="doer")
        self.card = self.kanban.create_task(self.team["id"], "ship the deck", column="ready")

    def offer(self, **over):
        args = {"payload_ref": "doc://decks/q4", "precondition": "tests pass on main",
                "timeout_seconds": 600, "task_id": self.card["id"]}
        args.update(over)
        return self.app.delegation.offer(self.team["id"], self.sender["id"],
                                         self.receiver["id"], **args)

    def test_accepting_moves_the_card_to_the_receiver(self):
        handoff = self.offer()
        self.assertFalse(self.kanban.get_task(self.card["id"])["assignee_agent_id"])
        accepted = self.app.delegation.answer(handoff["id"], {
            "type": "accept", "actor": self.receiver["id"], "precondition_held": True})
        self.assertEqual(accepted["state"], "accepted")
        self.assertEqual(self.kanban.get_task(self.card["id"])["assignee_agent_id"],
                         self.receiver["id"])

    def test_a_silent_offer_is_swept_into_a_row(self):
        handoff = self.offer(timeout_seconds=1)
        with self.app.store.tx() as c:
            c.execute("update delegations set offered_at = offered_at - 100 where id = ?",
                      (handoff["id"],))
        swept = self.app.delegation.sweep(self.team["id"])
        self.assertIn(handoff["id"], swept["expired"])
        self.assertEqual(self.app.delegation.get(handoff["id"])["state"], "expired")

    def test_the_list_names_what_is_still_open(self):
        self.offer()
        self.assertEqual(len(self.app.delegation.list(self.team["id"], state="offered")), 1)

    def test_a_breakdown_refuses_a_part_nobody_owns(self):
        out = self.app.delegation.breakdown(self.card["id"], [
            {"title": "lane one", "assignee_agent_id": self.sender["id"]},
            {"title": "lane two"},
        ])
        self.assertEqual(out["orphans"], ["lane two"])
        self.assertEqual(len(out["created"]), 1)
        self.assertIn("orphan_partition:lane two", out["problems"])

    def test_a_breakdown_of_owned_parts_opens_a_barrier(self):
        out = self.app.delegation.breakdown(self.card["id"], [
            {"title": "lane one", "assignee_agent_id": self.sender["id"]},
            {"title": "lane two", "assignee_agent_id": self.receiver["id"]},
        ])
        self.assertEqual(out["orphans"], [])
        self.assertEqual(len(out["created"]), 2)
        self.assertEqual(out["barrier"]["join"], "all")


class BudgetTests(AppCase):
    def setUp(self):
        super().setUp()
        self.team = self.crew("budget")
        self.agent_row = self.agent(self.team["id"], "wren", role="doer")

    def test_a_reservation_of_nothing_reserves_nothing(self):
        with self.assertRaises(ValidationError) as caught:
            self.app.budget.hold(self.team["id"], 0)
        self.assertIn("positive_amount", caught.exception.message)

    def test_the_check_and_the_claim_are_one_act(self):
        self.app.guardrails.set(self.team["id"], "spend_per_day", 1000)
        self.app.budget.hold(self.team["id"], 600, reason="first")
        with self.assertRaises(ValidationError) as caught:
            self.app.budget.hold(self.team["id"], 600, reason="second")
        self.assertIn("committed today", caught.exception.message)

    def test_a_held_reservation_is_not_available_money(self):
        self.app.guardrails.set(self.team["id"], "spend_per_day", 1000)
        self.app.budget.hold(self.team["id"], 700)
        self.assertEqual(self.app.budget.summary(self.team["id"])["available"], 300)

    def test_settling_records_the_bill_and_releasing_returns_it(self):
        self.app.guardrails.set(self.team["id"], "spend_per_day", 1000)
        first = self.app.budget.hold(self.team["id"], 400)
        second = self.app.budget.hold(self.team["id"], 400)
        self.app.budget.settle(first["id"], 250)
        self.app.budget.release(second["id"], reason="never ran")
        summary = self.app.budget.summary(self.team["id"])
        self.assertEqual((summary["spent_today"], summary["held"], summary["available"]),
                         (250, 0, 750))

    def test_an_estimate_that_was_wrong_is_recorded_as_over_spend(self):
        reservation = self.app.budget.hold(self.team["id"], 100)
        settled = self.app.budget.settle(reservation["id"], 180)
        self.assertTrue(settled["over_spend"])
        self.assertEqual(settled["settled_cents"], 180)

    def test_a_reservation_cannot_be_settled_twice(self):
        reservation = self.app.budget.hold(self.team["id"], 100)
        self.app.budget.settle(reservation["id"])
        with self.assertRaises(ValidationError):
            self.app.budget.settle(reservation["id"])

    def test_a_run_reserves_and_settles_its_declared_budget(self):
        self.app.guardrails.set(self.team["id"], "spend_per_day", 1000)
        card = self.kanban.create_task(self.team["id"], "do the thing", column="ready",
                                       payload={"budget_cents": 300, "spent_cents": 220})
        self.app.runner.dispatch(card["id"])
        summary = self.app.budget.summary(self.team["id"])
        self.assertEqual((summary["spent_today"], summary["held"]), (220, 0))


class ArchetypeTests(AppCase):
    def test_an_unknown_kind_is_refused_where_the_agent_is_made(self):
        team = self.crew("kinds-bad")
        with self.assertRaises(ValueError):
            self.app.crews.add_agent(team["id"], "mystery", role="doer", kind="wizard")

    def test_the_four_kinds_of_work_have_their_own_seats(self):
        from agents import archetypes

        self.assertEqual(archetypes.KINDS["harness"]["roles"], ("doer", "reviewer"))
        self.assertEqual(archetypes.KINDS["factory"]["roles"], ("doer", "sampler"))
        self.assertEqual(archetypes.KINDS["autoscientist"]["roles"],
                         ("researcher", "synthesizer"))
        self.assertEqual(archetypes.KINDS["orchestrator"]["roles"], ("integrator",))

    def test_a_harness_agent_cannot_be_a_researcher(self):
        from agents import archetypes

        ok, reason = archetypes.may_fill("harness", "researcher")
        self.assertFalse(ok)
        self.assertEqual(reason, "harness_cannot_fill:researcher")

    def test_a_kind_narrows_the_slices_a_seat_can_read(self):
        from agents import archetypes

        self.assertEqual(
            archetypes.slices_for("harness", "doer",
                                  ["intent", "knowledge", "lessons", "task", "failures"]),
            ["intent", "task", "lessons", "failures"])
        self.assertEqual(
            archetypes.slices_for("orchestrator", "integrator",
                                  ["task", "board", "knowledge", "inbox"]),
            ["task", "board", "knowledge"])

    def test_an_alias_is_the_same_kind(self):
        from agents import archetypes

        self.assertEqual(archetypes.canonical("code"), "harness")
        self.assertEqual(archetypes.canonical("research"), "autoscientist")
        self.assertEqual(archetypes.canonical("dark-factory"), "factory")

    def test_an_autoscientist_takes_the_research_seat_and_not_the_doer_seat(self):
        from work import roster

        team = self.crew("kinds-roster")
        self.app.crews.add_agent(team["id"], "sci", role="researcher", kind="autoscientist")
        agents = [{"id": a["id"], "name": a["name"], "role": a["role"], "kind": a["kind"],
                   "skills": [], "context": None}
                  for a in self.app.crews.list_agents(team["id"])]
        open_question = {"question": "open", "shape": "one", "parts": "coupled",
                         "check": "judgement", "stakes": "reversible", "seats": 2}
        out = roster.form(open_question, agents)
        researcher = [seat for seat in out["seats"] if seat["role"] == "researcher"][0]
        self.assertEqual(researcher["agent"], "sci")
        self.assertEqual(researcher["slices"], ["intent", "knowledge", "task", "inbox"])

        # the same agent is not a candidate for a harness doer seat
        harness = {"question": "known", "shape": "one", "parts": "coupled",
                   "check": "programmatic", "stakes": "reversible", "seats": 2}
        doer = [seat for seat in roster.form(harness, agents)["seats"]
                if seat["role"] == "doer"][0]
        self.assertEqual(doer["agent"], "")


if __name__ == "__main__":
    unittest.main()
