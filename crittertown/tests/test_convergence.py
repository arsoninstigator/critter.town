"""a barrier: the record that says whether a fan-out is finished, and who is missing.

the property worth holding is that silence is never a result. three of five lanes
answering must not read like five of five, a blocked lane must carry its reason, and a
result without a reference is a claim.
"""

from __future__ import annotations

import unittest

from base import AppCase

from core.errors import Conflict, NotFound, ValidationError
from work import convergence
from work.convergence import (
    add_report, barrier, cancelled_by_first_success, conflicts, stalled, validate,
)


def plan(**over):
    base = {
        "partitions": {"north": "lane_a", "south": "lane_b"},
        "participants": ["lane_a", "lane_b"],
        "join": "all",
        "quorum": None,
        "reports": [],
        "deadline": 1000.0,
    }
    base.update(over)
    return base


class ValidateTests(unittest.TestCase):
    def test_a_healthy_plan_has_no_problems(self):
        self.assertEqual(validate(plan()), [])

    def test_a_plan_with_no_partitions_is_refused(self):
        self.assertIn("no_partitions", validate(plan(partitions={})))

    def test_one_participant_is_not_a_convergence(self):
        self.assertIn("a_convergence_needs_two_participants",
                      validate(plan(partitions={"north": "lane_a"}, participants=["lane_a"])))

    def test_an_owner_outside_the_participants_is_named(self):
        problems = validate(plan(partitions={"north": "stranger", "south": "lane_b"}))
        self.assertIn("owner_not_a_participant:north:stranger", problems)

    def test_quorum_must_be_a_number_inside_the_partitions(self):
        self.assertIn("quorum_needs_a_number", validate(plan(join="quorum")))
        self.assertIn("quorum_is_more_than_the_partitions",
                      validate(plan(join="quorum", quorum=9)))

    def test_an_unknown_join_is_refused(self):
        self.assertIn("join_must_be_one_of_all|quorum|first_success", validate(plan(join="most")))


class ReportTests(unittest.TestCase):
    def test_a_result_needs_a_reference(self):
        with self.assertRaises(ValidationError):
            add_report(plan(), {"partition": "north", "participant": "lane_a",
                                "outcome": "result"}, 10.0)

    def test_a_blocked_lane_needs_a_reason(self):
        with self.assertRaises(ValidationError):
            add_report(plan(), {"partition": "north", "participant": "lane_a",
                                "outcome": "blocked"}, 10.0)

    def test_a_partition_the_plan_does_not_name_is_refused(self):
        with self.assertRaises(ValidationError):
            add_report(plan(), {"partition": "east", "participant": "lane_a",
                                "outcome": "not_applicable"}, 10.0)

    def test_the_wrong_participant_cannot_report_for_a_partition(self):
        with self.assertRaises(ValidationError):
            add_report(plan(), {"partition": "north", "participant": "lane_b",
                                "outcome": "not_applicable"}, 10.0)

    def test_an_unknown_outcome_is_refused(self):
        with self.assertRaises(ValidationError):
            add_report(plan(), {"partition": "north", "participant": "lane_a",
                                "outcome": "probably"}, 10.0)

    def test_reporting_twice_replaces_the_first_report(self):
        once = add_report(plan(), {"partition": "north", "participant": "lane_a",
                                   "outcome": "blocked", "reason": "waiting on keys"}, 10.0)
        twice = add_report(once, {"partition": "north", "participant": "lane_a",
                                  "outcome": "result", "artifact_ref": "commit:abc"}, 20.0)
        self.assertEqual(len(twice["reports"]), 1)
        self.assertEqual(twice["reports"][0]["outcome"], "result")


class BarrierTests(unittest.TestCase):
    def test_all_waits_for_every_partition(self):
        one = add_report(plan(), {"partition": "north", "participant": "lane_a",
                                  "outcome": "result", "artifact_ref": "commit:a"}, 1.0)
        state = barrier(one)
        self.assertFalse(state["resolved"])
        self.assertEqual(state["missing"], ["south"])

    def test_all_closes_when_the_last_lane_reports_not_applicable(self):
        one = add_report(plan(), {"partition": "north", "participant": "lane_a",
                                  "outcome": "result", "artifact_ref": "commit:a"}, 1.0)
        two = add_report(one, {"partition": "south", "participant": "lane_b",
                               "outcome": "not_applicable"}, 2.0)
        self.assertTrue(barrier(two)["resolved"])

    def test_quorum_counts_results_not_reports(self):
        one = add_report(plan(join="quorum", quorum=1), {"partition": "north",
                         "participant": "lane_a", "outcome": "blocked", "reason": "no"}, 1.0)
        self.assertFalse(barrier(one)["resolved"])

    def test_first_success_closes_on_one_result_and_names_the_losers(self):
        one = add_report(plan(join="first_success"), {"partition": "north",
                         "participant": "lane_a", "outcome": "result",
                         "artifact_ref": "commit:a"}, 1.0)
        self.assertTrue(barrier(one)["resolved"])
        self.assertEqual(cancelled_by_first_success(one), ["south"])

    def test_a_plan_without_a_deadline_never_stalls(self):
        self.assertFalse(stalled(plan(deadline=None), 10_000.0))

    def test_a_missing_deadline_lane_stalls_after_it(self):
        self.assertTrue(stalled(plan(), 1001.0))
        self.assertFalse(stalled(plan(), 999.0))

    def test_two_lanes_that_touch_the_same_surface_are_a_conflict(self):
        one = add_report(plan(), {"partition": "north", "participant": "lane_a",
                                  "outcome": "result", "artifact_ref": "commit:a",
                                  "touches": ["agents/orchestrator.py"]}, 1.0)
        two = add_report(one, {"partition": "south", "participant": "lane_b",
                               "outcome": "result", "artifact_ref": "commit:b",
                               "touches": ["agents/orchestrator.py"]}, 2.0)
        found = conflicts(two)
        self.assertEqual(found, [{"surface": "agents/orchestrator.py",
                                  "partitions": ["north", "south"]}])


class ConvergenceServiceTests(AppCase):
    def setUp(self):
        super().setUp()
        self.team = self.crew("barrier")
        self.agent(self.team["id"], "lane-runner")
        self.parent = self.kanban.create_task(self.team["id"], "ship the launch",
                                              column="ready")
        self.lanes = [self.kanban.create_task(self.team["id"], f"lane {n}",
                                              column="ready", parent_id=self.parent["id"])
                      for n in range(1, 3)]
        self.plan = self.app.convergence.open(
            self.parent["id"], {lane["id"]: lane["id"] for lane in self.lanes},
            [lane["id"] for lane in self.lanes])

    def test_an_impossible_plan_is_refused_before_it_is_stored(self):
        with self.assertRaises(ValidationError) as caught:
            self.app.convergence.open(self.parent["id"], {"only": "one"}, ["one"])
        self.assertIn("problems", caught.exception.details)

    def test_a_report_closes_the_barrier_when_the_last_lane_answers(self):
        first, second = self.lanes
        self.app.convergence.report(self.plan["id"], {
            "partition": first["id"], "participant": first["id"], "outcome": "result",
            "artifact_ref": "commit:a"})
        self.assertEqual(self.app.convergence.get(self.plan["id"])["status"], "open")
        closed = self.app.convergence.report(self.plan["id"], {
            "partition": second["id"], "participant": second["id"], "outcome": "result",
            "artifact_ref": "commit:b"})
        self.assertEqual(closed["status"], "closed")
        self.assertTrue(closed["barrier"]["resolved"])

    def test_a_closed_barrier_refuses_more_reports(self):
        for lane in self.lanes:
            self.app.convergence.report(self.plan["id"], {
                "partition": lane["id"], "participant": lane["id"], "outcome": "result",
                "artifact_ref": "commit:x"})
        with self.assertRaises(Conflict):
            self.app.convergence.report(self.plan["id"], {
                "partition": self.lanes[0]["id"], "participant": self.lanes[0]["id"],
                "outcome": "result", "artifact_ref": "commit:y"})

    def test_check_names_a_stalled_barrier(self):
        self.app.convergence.open(self.parent["id"], {"a": "a", "b": "b"}, ["a", "b"],
                                  deadline_seconds=-1)
        stalled_plan = self.app.convergence.for_task(self.parent["id"])
        checked = self.app.convergence.check(stalled_plan["id"])
        self.assertTrue(checked["stalled"])
        self.assertEqual(checked["status"], "stalled")

    def test_a_missing_convergence_is_not_found(self):
        with self.assertRaises(NotFound):
            self.app.convergence.get("conv_nope")

    def test_the_barrier_closes_a_parent_card_only_when_resolved(self):
        self.app.runner.dispatch(self.parent["id"])
        with self.assertRaises(ValidationError) as caught:
            self.kanban.approve(self.parent["id"], "owner", evidence="run:x")
        self.assertIn("barrier is not closed", caught.exception.message)
        for lane in self.lanes:
            self.app.runner.dispatch(lane["id"])
        self.assertEqual(self.kanban.approve(self.parent["id"], "owner",
                                             evidence="run:x")["column"], "done")


class RouteTests(AppCase):
    def setUp(self):
        super().setUp()
        from api.routes import create_api
        from fastapi.testclient import TestClient

        self.client = TestClient(create_api(self.app))
        self.team = self.crew("routes")
        self.parent = self.kanban.create_task(self.team["id"], "parent", column="ready")
        self.a = self.kanban.create_task(self.team["id"], "a", column="ready")
        self.b = self.kanban.create_task(self.team["id"], "b", column="ready")

    def test_the_four_routes_round_trip_a_barrier(self):
        opened = self.client.post(f"/tasks/{self.parent['id']}/convergence", json={
            "partitions": {self.a["id"]: self.a["id"], self.b["id"]: self.b["id"]},
            "participants": [self.a["id"], self.b["id"]],
        }).json()
        self.assertEqual(opened["join"], "all")
        reported = self.client.post(f"/convergences/{opened['id']}/report", json={
            "partition": self.a["id"], "participant": self.a["id"],
            "outcome": "result", "artifact_ref": "commit:a"}).json()
        self.assertFalse(reported["barrier"]["resolved"])
        self.assertEqual(self.client.get(f"/tasks/{self.parent['id']}/convergence")
                         .json()["id"], opened["id"])
        self.assertFalse(self.client.get(f"/convergences/{opened['id']}")
                         .json()["barrier"]["resolved"])


class FanOutTests(AppCase):
    def test_a_fan_out_opens_one_barrier_with_a_partition_per_lane(self):
        team = self.crew("fan")
        self.agent(team["id"], "runner")
        parent = self.kanban.create_task(team["id"], "launch", column="ready",
                                         payload={"subtasks": [{"title": "one"},
                                                               {"title": "two"},
                                                               {"title": "three"}]})
        lanes = self.app.runner.plan(parent["id"])
        plan = self.app.convergence.for_task(parent["id"])
        self.assertEqual(sorted(plan["partitions"]), sorted(lane["id"] for lane in lanes))
        self.assertEqual(plan["join"], "all")

    def test_a_single_child_is_not_a_convergence(self):
        team = self.crew("solo")
        self.agent(team["id"], "runner")
        parent = self.kanban.create_task(team["id"], "launch", column="ready",
                                         payload={"subtasks": [{"title": "only"}]})
        self.app.runner.plan(parent["id"])
        self.assertIsNone(self.app.convergence.for_task(parent["id"]))

    def test_a_lane_that_runs_reports_a_result_and_one_that_fails_reports_blocked(self):
        team = self.crew("lanes")
        self.agent(team["id"], "runner")
        parent = self.kanban.create_task(team["id"], "launch", column="ready",
                                         payload={"subtasks": [{"title": "one"},
                                                               {"title": "two"}]})
        lanes = self.app.runner.plan(parent["id"])
        plan = self.app.convergence.for_task(parent["id"])
        self.app.runner.dispatch(lanes[0]["id"])
        self.app.runner.model.script = [{"thought": "hand off",
                                         "commands": [],
                                         "message": {"to": "nobody", "kind": "result",
                                                     "body": "x"},
                                         "done": True, "result": {}}]
        try:
            self.app.runner.dispatch(lanes[1]["id"])
        except Exception:  # noqa: BLE001 - the run fails, the report is the point
            pass
        reports = {r["partition"]: r for r in self.app.convergence.get(plan["id"])["reports"]}
        self.assertEqual(reports[lanes[0]["id"]]["outcome"], "result")
        self.assertTrue(reports[lanes[0]["id"]]["artifact_ref"].startswith("run:"))
        self.assertEqual(reports[lanes[1]["id"]]["outcome"], "blocked")
        self.assertTrue(reports[lanes[1]["id"]]["reason"])


if __name__ == "__main__":
    unittest.main()
