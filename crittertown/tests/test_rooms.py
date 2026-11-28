"""rooms that follow the org: a channel for a crew, a department, a bloc and a wing, each with the
roster the org gives it and no other. the properties under test are the ones that make a room
trustworthy: it exists once, its members are the org's and not whoever typed a name, a bloc's room ends
with the bloc, and running the reconcile twice changes nothing."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from base import AppCase  # noqa: E402
from core.db import now  # noqa: E402
from core.errors import Conflict, NotAllowed  # noqa: E402
from ingredients import relay  # noqa: E402


class RoomCase(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("shop")
        self.tid = self.team["id"]
        c = self.app.crews
        self.anne = c.add_agent(self.tid, "anne", role="doer")
        self.ben = c.add_agent(self.tid, "ben", role="doer")
        self.cy = c.add_agent(self.tid, "cy", role="reviewer")
        self.dee = c.add_agent(self.tid, "dee", role="doer")
        self.support = self.app.departments.create(
            self.tid, "support", coordinator_agent_id=self.anne["id"], members=[self.dee["id"]])
        self.code = self.app.departments.create(
            self.tid, "code", coordinator_agent_id=self.ben["id"])

    def rooms(self) -> dict:
        self.app.rooms.reconcile(self.tid)
        return {(r["scope"], r["scope_id"]): r for r in self.app.channels.list(self.tid)
                if r["scope"] != "plain"}

    def roster(self, room) -> dict:
        return {m["agent_id"]: m["role"] for m in room["members"]}


class DepartmentRoomTests(RoomCase):
    def test_every_department_gets_a_room_of_its_own(self):
        rooms = self.rooms()
        self.assertIn(("department", self.support["id"]), rooms)
        self.assertIn(("department", self.code["id"]), rooms)

    def test_the_head_leads_and_the_members_are_members(self):
        room = self.rooms()[("department", self.support["id"])]
        self.assertEqual(self.roster(room),
                         {self.anne["id"]: "lead", self.dee["id"]: "member"})

    def test_a_department_room_is_a_dept_kind_channel(self):
        self.assertEqual(self.rooms()[("department", self.code["id"])]["kind"], "dept")

    def test_a_seat_added_to_the_department_appears_in_the_room(self):
        self.rooms()
        self.app.departments.add_member(self.code["id"], self.cy["id"])
        room = self.rooms()[("department", self.code["id"])]
        self.assertEqual(self.roster(room)[self.cy["id"]], "member")

    def test_a_seat_that_left_the_department_leaves_the_room(self):
        self.rooms()
        self.app.departments.remove_member(self.support["id"], self.dee["id"])
        room = self.rooms()[("department", self.support["id"])]
        self.assertNotIn(self.dee["id"], self.roster(room))

    def test_a_new_coordinator_takes_the_lead_and_the_old_one_does_not_keep_it(self):
        self.rooms()
        self.app.departments.update(self.support["id"], coordinator_agent_id=self.cy["id"])
        roster = self.roster(self.rooms()[("department", self.support["id"])])
        self.assertEqual(roster[self.cy["id"]], "lead")
        self.assertNotEqual(roster.get(self.anne["id"]), "lead")


class CrewRoomTests(RoomCase):
    def test_the_crew_has_one_room_with_everyone_in_it(self):
        room = self.rooms()[("crew", self.tid)]
        self.assertEqual(set(self.roster(room)),
                         {self.anne["id"], self.ben["id"], self.cy["id"], self.dee["id"]})

    def test_nobody_leads_the_crew_room(self):
        self.assertNotIn("lead", self.roster(self.rooms()[("crew", self.tid)]).values())

    def test_a_new_agent_is_in_the_crew_room_after_the_next_reconcile(self):
        self.rooms()
        eve = self.app.crews.add_agent(self.tid, "eve", role="doer")
        self.assertIn(eve["id"], self.roster(self.rooms()[("crew", self.tid)]))


class BlocRoomTests(RoomCase):
    def bloc(self, **kw):
        # a seat is lent from the department it sits in, so cy has to sit in one first
        self.app.departments.add_member(self.code["id"], self.cy["id"])
        bloc = self.app.axis.form_bloc(
            self.tid, "launch", "ship the launch", [self.support["id"], self.code["id"]],
            now() + 86400)
        self.app.axis.lend(bloc["id"], self.cy["id"], now() + 3600)
        return bloc

    def test_a_bloc_room_holds_the_seats_it_borrowed(self):
        bloc = self.bloc()
        room = self.rooms()[("bloc", bloc["id"])]
        self.assertEqual(self.roster(room), {self.cy["id"]: "member"})

    def test_closing_the_bloc_closes_its_room_and_empties_it(self):
        bloc = self.bloc()
        self.rooms()
        self.app.axis.close_bloc(bloc["id"])
        room = self.rooms()[("bloc", bloc["id"])]
        self.assertEqual(room["state"], "closed")
        self.assertEqual(self.roster(room), {})

    def test_an_expired_grant_drops_out_of_the_room(self):
        bloc = self.bloc()
        self.rooms()
        with self.app.store.tx() as c:
            c.execute("update org_bloc_grants set expires_at = ? where bloc_id = ?",
                      (now() - 1, bloc["id"]))
        self.assertEqual(self.roster(self.rooms()[("bloc", bloc["id"])]), {})

    def test_a_bloc_that_ended_before_it_was_ever_reconciled_gets_no_room(self):
        bloc = self.bloc()
        self.app.axis.close_bloc(bloc["id"])
        self.assertNotIn(("bloc", bloc["id"]), self.rooms())

    def test_a_closed_room_refuses_a_new_member(self):
        bloc = self.bloc()
        self.rooms()
        self.app.axis.close_bloc(bloc["id"])
        room = self.rooms()[("bloc", bloc["id"])]
        with self.assertRaises(Conflict):
            self.app.channels.join(room["id"], self.ben["id"], _org=True)


class WingRoomTests(RoomCase):
    def test_every_holder_of_a_wing_leads_its_room(self):
        wing = self.app.axis.create_wing(self.tid, "security", "security",
                                         [self.cy["id"], self.dee["id"]])
        room = self.rooms()[("wing", wing["id"])]
        self.assertEqual(self.roster(room), {self.cy["id"]: "lead", self.dee["id"]: "lead"})


class ProjectionTests(RoomCase):
    def test_a_hand_made_join_on_an_org_room_is_refused_and_says_where_to_change_it(self):
        room = self.rooms()[("department", self.support["id"])]
        with self.assertRaises(NotAllowed) as caught:
            self.app.channels.join(room["id"], self.ben["id"])
        self.assertIn("change the department", str(caught.exception))
        self.assertNotIn(self.ben["id"], self.roster(self.app.channels.get(room["id"])))

    def test_a_hand_made_leave_is_refused_too(self):
        room = self.rooms()[("department", self.support["id"])]
        with self.assertRaises(NotAllowed):
            self.app.channels.leave(room["id"], self.dee["id"])

    def test_a_room_someone_opened_by_hand_is_still_theirs_to_edit(self):
        plain = self.app.channels.create(self.tid, "lunch", members=[self.anne["id"]])
        self.app.channels.join(plain["id"], self.ben["id"])
        self.assertIn(self.ben["id"], self.roster(self.app.channels.get(plain["id"])))
        self.assertEqual(plain["scope"], "plain")

    def test_a_reconcile_never_touches_a_hand_made_room(self):
        plain = self.app.channels.create(self.tid, "lunch", members=[self.anne["id"]])
        self.rooms()
        self.assertEqual(self.roster(self.app.channels.get(plain["id"])),
                         {self.anne["id"]: "member"})

    def test_the_org_room_takes_the_next_free_slug_instead_of_a_name_somebody_chose(self):
        self.app.channels.create(self.tid, "support", slug="dept-support")
        room = self.rooms()[("department", self.support["id"])]
        self.assertEqual(room["slug"], "dept-support-2")

    def test_a_second_room_for_one_piece_of_the_org_is_refused_by_the_database(self):
        self.rooms()
        with self.assertRaises(Exception):
            with self.app.store.tx() as c:
                c.execute(
                    """insert into channels (id, team_id, name, slug, scope, scope_id,
                           created_date, updated_date) values ('x', ?, 'dup', 'dup', 'department',
                           ?, 1, 1)""", (self.tid, self.support["id"]))


class ReconcileTests(RoomCase):
    def test_a_second_reconcile_changes_nothing(self):
        first = self.app.rooms.reconcile(self.tid)
        second = self.app.rooms.reconcile(self.tid)
        self.assertGreater(first["applied"], 0)
        self.assertEqual(second["applied"], 0)
        self.assertTrue(second["in_sync"])

    def test_the_plan_reads_without_changing_anything(self):
        before = len(self.app.channels.list(self.tid))
        plan = self.app.rooms.plan(self.tid)
        self.assertFalse(plan["in_sync"])
        self.assertEqual(len(self.app.channels.list(self.tid)), before)

    def test_every_room_is_made_before_anyone_joins_it(self):
        steps = self.app.rooms.plan(self.tid)["steps"]
        for scope, scope_id in {(s["scope"], s["scope_id"]) for s in steps if s["op"] == "create"}:
            create = next(i for i, s in enumerate(steps) if s["op"] == "create"
                          and s["scope_id"] == scope_id)
            joins = [i for i, s in enumerate(steps)
                     if s["op"] == "join" and s.get("scope_id") == scope_id]
            self.assertTrue(all(j > create for j in joins))

    def test_a_deleted_department_closes_its_room(self):
        room = self.rooms()[("department", self.code["id"])]
        with self.app.store.tx() as c:
            c.execute("delete from departments where id = ?", (self.code["id"],))
        after = {r["id"]: r for r in self.app.channels.list(self.tid)}
        self.app.rooms.reconcile(self.tid)
        gone = self.app.channels.get(room["id"])
        self.assertEqual(gone["state"], "closed")
        self.assertEqual(gone["members"], [])
        self.assertIn(room["id"], after)

    def test_a_renamed_department_renames_its_room(self):
        self.rooms()
        self.app.departments.update(self.code["id"], name="platform")
        self.assertEqual(self.rooms()[("department", self.code["id"])]["name"], "platform")


class RoomBoardTests(RoomCase):
    def support_room(self):
        return self.rooms()[("department", self.support["id"])]

    def test_a_member_raises_a_card_on_the_rooms_board(self):
        room = self.support_room()
        card = self.app.channels.create_task(room["id"], "triage", by=self.dee["id"])
        self.assertEqual(card["channel_id"], room["id"])
        stored = self.app.store.one("select channel_id from tasks where id = ?", (card["id"],))
        self.assertEqual(stored["channel_id"], room["id"])
        self.assertEqual(self.app.channels.board(room["id"])["channel"]["id"], room["id"])

    def test_an_outsider_cannot_raise_work_on_a_departments_board(self):
        with self.assertRaises(NotAllowed) as caught:
            self.app.channels.create_task(self.support_room()["id"], "do my thing",
                                          by=self.ben["id"])
        self.assertIn("is not in this department's room", str(caught.exception))

    def test_a_card_on_an_org_room_names_who_raised_it(self):
        from core.errors import ValidationError
        with self.assertRaises(ValidationError):
            self.app.channels.create_task(self.support_room()["id"], "anonymous")

    def test_a_person_may_raise_a_card_in_any_room(self):
        card = self.app.channels.create_task(self.support_room()["id"], "from the owner",
                                             by="owner")
        self.assertEqual(card["channel_id"], self.support_room()["id"])

    def test_a_closed_room_takes_no_new_cards(self):
        self.app.departments.add_member(self.code["id"], self.cy["id"])
        bloc = self.app.axis.form_bloc(self.tid, "launch", "ship it",
                                       [self.support["id"], self.code["id"]], now() + 86400)
        self.app.axis.lend(bloc["id"], self.cy["id"], now() + 3600)
        room = self.rooms()[("bloc", bloc["id"])]
        self.app.axis.close_bloc(bloc["id"])
        self.rooms()
        with self.assertRaises(Conflict):
            self.app.channels.create_task(room["id"], "late work", by="owner")

    def test_a_hand_made_room_stays_open_to_anyone_as_before(self):
        plain = self.app.channels.create(self.tid, "lunch", members=[self.anne["id"]])
        card = self.app.channels.create_task(plain["id"], "pick a place")
        self.assertEqual(card["channel_id"], plain["id"])

    def test_the_board_says_which_piece_of_the_org_it_belongs_to(self):
        board = self.app.channels.board(self.support_room()["id"])
        self.assertEqual(board["channel"]["scope"], "department")
        self.assertEqual(board["channel"]["scope_id"], self.support["id"])

    def test_the_route_takes_the_author(self):
        from fastapi.testclient import TestClient
        from api.routes import create_api
        client = TestClient(create_api(self.app))
        room = self.support_room()
        ok = client.post(f"/channels/{room['id']}/tasks", json={"title": "t", "by": self.dee["id"]})
        self.assertEqual(ok.status_code, 200)
        no = client.post(f"/channels/{room['id']}/tasks", json={"title": "t", "by": self.ben["id"]})
        self.assertEqual(no.status_code, 403)


class RelayRoleTests(RoomCase):
    def test_our_roles_map_onto_the_relays_and_never_to_owner(self):
        self.assertEqual(relay.RELAY_ROLE, {"lead": "admin", "member": "member"})
        self.assertNotIn("owner", relay.RELAY_ROLE.values())

    def test_the_relay_roster_reads_the_mapped_roles(self):
        room = self.rooms()[("department", self.support["id"])]
        got = self.app.rooms.relay_roster(room["id"])
        by = {m["agent_id"]: m["relay_role"] for m in got["members"]}
        self.assertEqual(by, {self.anne["id"]: "admin", self.dee["id"]: "member"})
        self.assertEqual(got["scope"], "department")


class OverviewTests(RoomCase):
    def ov(self, **kw):
        self.app.rooms.reconcile(self.tid)
        return relay.overview(self.app.store, self.tid, **kw)

    def room(self, ov, scope, scope_id):
        return next(r for r in ov["rooms"] if r["scope"] == scope and r["scope_id"] == scope_id)

    def test_the_overview_lists_every_room_with_its_scope_and_roster(self):
        ov = self.ov()
        support = self.room(ov, "department", self.support["id"])
        self.assertEqual({m["name"] for m in support["members"]}, {"anne", "dee"})
        self.assertEqual(ov["rooms"][0]["scope"], "crew")

    def test_cards_on_a_rooms_board_are_counted_by_column(self):
        room = self.rooms()[("department", self.support["id"])]
        self.app.channels.create_task(room["id"], "answer the refund queue", by=self.dee["id"])
        got = self.room(self.ov(), "department", self.support["id"])
        self.assertEqual(got["open_cards"], 1)
        self.assertEqual(sum(got["cards"].values()), 1)

    def test_an_a2a_message_between_two_members_belongs_to_their_room(self):
        self.rooms()
        self.app.a2a.send(self.tid, self.anne["id"], "can you take this", to_agent_id=self.dee["id"], kind="note")
        got = self.room(self.ov(), "department", self.support["id"])
        self.assertEqual(got["a2a"]["count"], 1)
        self.assertEqual(got["a2a"]["recent"][0]["from"], "anne")

    def test_a_message_across_departments_belongs_to_neither_department_room(self):
        self.rooms()
        self.app.a2a.send(self.tid, self.anne["id"], "a private ask", to_agent_id=self.ben["id"], kind="note")
        ov = self.ov()
        self.assertEqual(self.room(ov, "department", self.support["id"])["a2a"]["count"], 0)
        self.assertEqual(self.room(ov, "department", self.code["id"])["a2a"]["count"], 0)
        # both are in the crew room, so that is where it is attributed
        self.assertEqual(self.room(ov, "crew", self.tid)["a2a"]["count"], 1)

    def test_a_message_to_the_whole_crew_belongs_to_no_room(self):
        self.rooms()
        self.app.a2a.send(self.tid, self.anne["id"], "a general remark", to_agent_id=None, kind="note")
        self.assertTrue(all(r["a2a"]["count"] == 0 for r in self.ov()["rooms"]))

    def test_a_message_about_a_card_on_the_board_belongs_to_that_room_even_for_outsiders(self):
        room = self.rooms()[("department", self.support["id"])]
        card = self.app.channels.create_task(room["id"], "refund queue", by=self.anne["id"])
        self.app.a2a.send(self.tid, self.ben["id"], "who owns this", to_agent_id=self.cy["id"], kind="note",
                          task_id=card["id"])
        self.assertEqual(self.room(self.ov(), "department", self.support["id"])["a2a"]["count"], 1)

    def test_pending_review_is_counted_per_room(self):
        self.rooms()
        self.app.a2a.send(self.tid, self.anne["id"], "please review", to_agent_id=self.dee["id"], kind="review_request")
        got = self.room(self.ov(), "department", self.support["id"])
        self.assertEqual(got["a2a"]["pending_review"], 1)

    def test_the_overview_states_the_attribution_rule(self):
        self.assertIn("both its sender and its recipient", self.ov()["attribution"])

    def test_the_routes_serve_the_overview_the_plan_and_the_reconcile(self):
        from fastapi.testclient import TestClient
        from api.routes import create_api
        client = TestClient(create_api(self.app))
        plan = client.get(f"/crews/{self.tid}/rooms/plan").json()
        self.assertFalse(plan["in_sync"])
        done = client.post(f"/crews/{self.tid}/rooms/reconcile").json()
        self.assertGreater(done["applied"], 0)
        ov = client.get(f"/crews/{self.tid}/rooms").json()
        self.assertTrue(any(r["scope"] == "department" for r in ov["rooms"]))
        room = next(r for r in ov["rooms"] if r["scope"] == "department")
        roster = client.get(f"/channels/{room['id']}/relay-roster").json()
        self.assertIn("relay_role", roster["members"][0])


if __name__ == "__main__":
    unittest.main()
