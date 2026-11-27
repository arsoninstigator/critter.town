"""the crew's order, and a trail that can be checked without us.

a per-agent chain answers "was this agent's own story edited". it cannot answer "did these
two agents' rows happen in this order", because moving a row from one chain to the other
breaks neither chain. that is the hole the crew stream closes: one head per crew, advanced
in the same transaction as the row, so the interleaving is checkable too.

and the export is jsonl because the point of an attestation is that somebody else can
check it. a format that needs our parser to read is a format that needs our word.
"""

from __future__ import annotations

import json
import unittest

from crittertown.tests.base import AppCase
from core.errors import ValidationError
from manage import audit as audit_mod


class CrewOrderTests(AppCase):
    def _crew_with_two(self):
        team = self.crew()
        one = self.agent(team["id"], "one")
        two = self.agent(team["id"], "two")
        return team, one, two

    def test_two_agents_share_one_sequence(self):
        team, one, two = self._crew_with_two()
        self.app.audit.record(team["id"], one["id"], "task.claimed", target_kind="task")
        self.app.audit.record(team["id"], two["id"], "task.claimed", target_kind="task")
        self.app.audit.record(team["id"], one["id"], "task.done", target_kind="task")
        verdict = self.app.audit.verify_crew(team["id"])
        self.assertTrue(verdict["ok"], verdict)
        self.assertEqual(verdict["entries"], 3)
        self.assertTrue(verdict["head_agrees"])

    def test_the_crew_head_matches_the_last_row(self):
        team, one, two = self._crew_with_two()
        first = self.app.audit.record(team["id"], one["id"], "task.claimed")
        second = self.app.audit.record(team["id"], two["id"], "task.claimed")
        head = self.app.store.one(
            "select seq, hash from crew_audit_heads where team_id = ?", (team["id"],))
        self.assertEqual(int(head["seq"]), 2)
        self.assertEqual(head["hash"], second["team_hash"])
        self.assertEqual(second["team_prev"], first["team_hash"])

    def test_editing_a_row_breaks_the_crew_chain(self):
        team, one, two = self._crew_with_two()
        self.app.audit.record(team["id"], one["id"], "task.claimed", payload={"n": 1})
        self.app.audit.record(team["id"], two["id"], "task.claimed", payload={"n": 2})
        with self.app.store.tx() as c:
            c.execute("update agent_audit set hash = 'f' * 64 where agent_id = ?", (one["id"],))
        verdict = self.app.audit.verify_crew(team["id"])
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason"], "crew_hash_mismatch")

    def test_a_row_moved_into_another_agent_s_chain_is_caught(self):
        team, one, two = self._crew_with_two()
        self.app.audit.record(team["id"], one["id"], "task.claimed")
        self.app.audit.record(team["id"], two["id"], "task.done")
        # the kind of edit a per-agent chain cannot see: attribute two's row to one
        with self.app.store.tx() as c:
            c.execute("update agent_audit set agent_id = ? where team_seq = 2", (one["id"],))
        self.assertFalse(self.app.audit.verify_crew(team["id"])["ok"])

    def test_a_gap_in_the_crew_sequence_is_named(self):
        team, one, two = self._crew_with_two()
        for _ in range(3):
            self.app.audit.record(team["id"], one["id"], "task.claimed")
        with self.app.store.tx() as c:
            c.execute("delete from agent_audit where team_seq = 2")
        verdict = self.app.audit.verify_crew(team["id"])
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason"], "crew_sequence_gap")
        self.assertEqual(verdict["broken_at_team_seq"], 2)

    def test_rows_written_before_the_crew_stream_are_reported_not_broken(self):
        team, one, _two = self._crew_with_two()
        self.app.audit.record(team["id"], one["id"], "task.claimed")
        with self.app.store.tx() as c:
            # a pre-v26 row: on its agent's chain, outside the crew sequence
            c.execute(
                """insert into agent_audit (id, team_id, agent_id, seq, action, actor_kind,
                   actor_id, target_kind, target_id, payload, prev_hash, hash, created_date)
                   values ('aud_old', ?, ?, 99, 'legacy.thing', 'agent', ?, '', '', '{}',
                           'x', 'y', 1)""", (team["id"], one["id"], one["id"]))
        verdict = self.app.audit.verify_crew(team["id"])
        self.assertTrue(verdict["ok"], verdict)
        self.assertEqual(verdict["pre_crew_rows"], 1)


class BundleTests(AppCase):
    def _bundle(self):
        team = self.crew()
        one = self.agent(team["id"], "one")
        two = self.agent(team["id"], "two")
        self.app.audit.record(team["id"], one["id"], "task.claimed", payload={"n": 1})
        self.app.audit.record(team["id"], two["id"], "task.done", payload={"n": 2})
        self.app.audit.record(team["id"], one["id"], "task.reviewed", payload={"n": 3})
        return self.app.audit.export_team(team["id"])

    def test_the_export_answers_both_questions(self):
        bundle = self._bundle()
        self.assertEqual(bundle["kind"], "crittertown.crew-attestation")
        self.assertTrue(bundle["verification"]["crew"]["ok"])
        self.assertTrue(bundle["verification"]["bundle"]["ok"])
        self.assertEqual(len(bundle["verification"]["agents"]), 2)

    def test_jsonl_round_trips(self):
        bundle = self._bundle()
        text = self.app.audit.to_jsonl(bundle["entries"])
        self.assertEqual(len(text.strip().splitlines()), 3)
        back = self.app.audit.from_jsonl(text)
        self.assertTrue(self.app.audit.verify_bundle(back)["ok"])

    def test_a_dropped_line_is_a_gap_not_a_smaller_trail(self):
        bundle = self._bundle()
        lines = [json.loads(line) for line in
                 self.app.audit.to_jsonl(bundle["entries"]).strip().splitlines()]
        del lines[1]
        verdict = self.app.audit.verify_bundle(lines)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason"], "crew_sequence_gap")

    def test_an_edited_action_fails_the_agent_chain(self):
        bundle = self._bundle()
        lines = [json.loads(line) for line in
                 self.app.audit.to_jsonl(bundle["entries"]).strip().splitlines()]
        lines[0]["action"] = "task.approved"
        verdict = self.app.audit.verify_bundle(lines)
        self.assertFalse(verdict["ok"])
        self.assertTrue(verdict["crew_ok"])
        self.assertEqual(verdict["reason"], "agent_chain_broken")

    def test_bad_jsonl_names_the_line(self):
        with self.assertRaises(ValidationError) as caught:
            self.app.audit.from_jsonl('{"ok": true}\nnot json\n')
        self.assertEqual(caught.exception.details.get("reason"), "bad_jsonl")
        self.assertIn("line 2", str(caught.exception))

    def test_the_bundle_carries_a_worker_signature_it_did_not_make(self):
        team = self.crew()
        worker = self.app.crews.add_agent(team["id"], "outside", hosting="external")
        from comms import identity as keys
        from manage import attest
        keypair = keys.generate()
        self.app.audit.keys.register_external(worker["id"], keypair.pubkey)
        payload = {"at": 1700000000.0, "nonce": "n-1"}
        signature = keypair.sign(bytes.fromhex(
            attest.payload_digest(audit_mod._canonical(payload))))
        self.app.audit.record(team["id"], worker["id"], "heartbeat", payload=payload,
                              attestation=signature)
        bundle = self.app.audit.export_team(team["id"])
        entry = bundle["entries"][0]
        self.assertEqual(entry["attests"], "payload")
        self.assertTrue(entry["attested"])
        self.assertTrue(bundle["verification"]["bundle"]["ok"], bundle["verification"])
        # and the signature is the worker's, not ours: we hold no secret for it
        self.assertEqual(self.app.audit.keys.for_agent(worker["id"]), None)


class RouteTests(AppCase):
    def test_jsonl_downloads_and_verifies_from_the_text_alone(self):
        from fastapi.testclient import TestClient
        from api.routes import create_api
        team = self.crew()
        one = self.agent(team["id"], "one")
        self.app.audit.record(team["id"], one["id"], "task.claimed")
        client = TestClient(create_api(self.app))
        response = client.get(f"/crews/{team['id']}/audit.jsonl")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"].split(";")[0],
                         "application/x-ndjson")
        verdict = client.post(f"/crews/{team['id']}/audit/verify",
                              json={"jsonl": response.text}).json()
        self.assertTrue(verdict["ok"], verdict)


if __name__ == "__main__":
    unittest.main()
