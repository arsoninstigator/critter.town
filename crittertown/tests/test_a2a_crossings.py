"""a message that leaves the crew is a crossing, not a note.

the properties worth holding: an undeclared pair refuses rather than defaulting to open; a
door declared for findings does not let a request through; the size and the rate are enforced
before the row exists, so a refused message leaves no trace to clean up; and the row that is
written keeps the door it used, because "which door did this cross" is what an audit asks.
"""

from __future__ import annotations

import time
import unittest

from base import AppCase

from core.errors import NotAllowed


class CrossingTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.home = self.crews.create_team("research", review_mode="open")
        self.away = self.crews.create_team("delivery", review_mode="open")
        self.sender = self.agent(self.home["id"], "analyst")
        self.recipient = self.agent(self.away["id"], "builder")
        self.neighbour = self.agent(self.home["id"], "editor")

    # helpers -----------------------------------------------------------
    def door(self, kinds=("finding",), max_per_hour=60, max_bytes=None,
             expires_in=3600, **over) -> dict:
        decl = {"id": "c1", "from_scope": self.home["id"], "to_scope": self.away["id"],
                "kinds": list(kinds), "max_per_hour": max_per_hour,
                "expires_at": time.time() + expires_in,
                "reason": "the research crew shares findings with delivery"}
        if max_bytes is not None:
            decl["max_bytes"] = max_bytes
        decl.update(over)
        return self.app.crossings.declare(self.home["id"], [decl])

    def send(self, body="the numbers moved", kind="note"):
        return self.app.a2a.send(self.home["id"], self.sender["id"], body,
                                 to_agent_id=self.recipient["id"], kind=kind)

    # the default -------------------------------------------------------
    def test_an_undeclared_pair_refuses_and_writes_nothing(self):
        with self.assertRaises(NotAllowed) as caught:
            self.send()
        # the wall names the reason, and a wall with no doors says so
        self.assertIn(caught.exception.details.get("reason"),
                      {"no_crossing", "crossing_not_declared"})
        self.assertEqual(self.app.a2a.log(self.home["id"]), [])

    def test_a_message_inside_the_crew_crosses_nothing(self):
        message = self.app.a2a.send(self.home["id"], self.sender["id"], "a note",
                                    to_agent_id=self.neighbour["id"])
        self.assertEqual(message["crossing_id"], "")
        self.assertEqual(message["to_team_id"], "")

    # a declared door ---------------------------------------------------
    def test_a_declared_door_lets_the_message_through_and_is_recorded(self):
        self.door()
        message = self.send()
        self.assertTrue(message["crossing_id"].startswith("cross_"))
        self.assertEqual(message["to_team_id"], self.away["id"])
        self.assertEqual(message["envelope_kind"], "finding")

    def test_the_door_is_chosen_by_the_message_kind(self):
        self.door(kinds=("finding",))
        with self.assertRaises(NotAllowed) as caught:
            self.send(kind="request")
        self.assertEqual(caught.exception.details.get("reason"), "kind_not_allowed")
        # the same door opened for requests lets the request through
        self.door(kinds=("finding", "request"), id="c2")
        self.assertEqual(self.send(kind="request")["envelope_kind"], "request")

    def test_the_recipient_sees_it_and_the_sending_crew_owns_the_row(self):
        self.door()
        message = self.send()
        self.assertIn(message["id"], [m["id"] for m in self.app.a2a.inbox(self.recipient["id"])])
        self.assertEqual([m["id"] for m in self.app.a2a.log(self.home["id"])], [message["id"]])

    # the limits --------------------------------------------------------
    def test_a_message_larger_than_the_door_allows_is_refused(self):
        self.door(max_bytes=20)
        with self.assertRaises(NotAllowed) as caught:
            self.send(body="x" * 200)
        self.assertEqual(caught.exception.details.get("reason"), "too_large")

    def test_the_rate_is_enforced_before_the_row_exists(self):
        self.door(max_per_hour=6)  # a burst of one, so the second message is early
        self.send()
        with self.assertRaises(NotAllowed) as caught:
            self.send()
        self.assertEqual(caught.exception.details.get("reason"), "rate_limited")
        self.assertEqual(len(self.app.a2a.log(self.home["id"])), 1)

    def test_a_door_that_expired_refuses(self):
        self.door(expires_in=-1)
        with self.assertRaises(NotAllowed) as caught:
            self.send()
        self.assertEqual(caught.exception.details.get("reason"), "crossing_expired")

    def test_a_cut_door_stops_the_next_message(self):
        self.door()
        crossing = self.app.crossings.list(self.home["id"])[0]
        self.app.crossings.revoke(crossing["id"], mode="cut", by="owner")
        with self.assertRaises(NotAllowed) as caught:
            self.send()
        self.assertEqual(caught.exception.details.get("reason"), "crossing_disabled")

    def test_each_message_spends_from_the_door(self):
        self.door(max_per_hour=60)
        before = self.app.crossings.list(self.home["id"])[0]["tokens"]
        self.send()
        after = self.app.crossings.list(self.home["id"])[0]["tokens"]
        self.assertLess(after, before)

    def test_the_send_route_carries_the_refusal(self):
        from fastapi.testclient import TestClient

        from api.routes import create_api
        client = TestClient(create_api(self.app))
        refused = client.post("/a2a", json={
            "team_id": self.home["id"], "from_agent_id": self.sender["id"],
            "to_agent_id": self.recipient["id"], "body": "across the wall"})
        self.assertGreaterEqual(refused.status_code, 400)
        self.door()
        allowed = client.post("/a2a", json={
            "team_id": self.home["id"], "from_agent_id": self.sender["id"],
            "to_agent_id": self.recipient["id"], "body": "across the wall"})
        self.assertEqual(allowed.status_code, 200)
        self.assertTrue(allowed.json()["crossing_id"])
