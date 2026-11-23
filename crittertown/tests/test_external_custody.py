"""whose signature is on an external worker's row.

the hole this closes: add_agent minted a key for every agent, external ones included, and
the audit chain signed their rows with it. so a row attributed to a worker we do not run
carried a signature we made, which reads as testimony and is actually the platform talking
about itself. an external worker now brings its own key, we keep the public half, and its
rows are unattested until it signs them.
"""

from __future__ import annotations

import unittest

from base import AppCase

from comms import identity as keys
from core.errors import Conflict, NotAllowed, ValidationError
from manage import attest


class CustodyTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        team = self.crew()
        self.team = team["id"]
        self.worker = self.app.crews.add_agent(team["id"], "kilo", role="doer",
                                              hosting="external")
        self.keypair = keys.generate()

    def register(self, agent_id=None, pubkey=None):
        return self.app.audit.keys.register_external(
            agent_id or self.worker["id"], pubkey or self.keypair.pubkey)

    def test_an_external_worker_is_never_given_a_key_here(self):
        self.assertIsNone(self.app.audit.keys.for_agent(self.worker["id"]))
        minted = self.app.crews.mint_identity(self.worker["id"])
        self.assertFalse(minted["attested"])
        self.assertEqual(minted["agentid"], "")
        row = self.app.store.one(
            "select * from comms_identities where agent_id = ?", (self.worker["id"],))
        self.assertIsNone(row)

    def test_an_agent_we_run_still_holds_its_own_key(self):
        local = self.app.crews.add_agent(self.team, "wren", role="doer")
        self.assertIsNotNone(self.app.audit.keys.for_agent(local["id"]))
        self.assertEqual(self.app.audit.keys.custody_of(local["id"]), "local")

    def test_a_registered_key_is_public_only(self):
        row = self.register()
        self.assertEqual(row["custody"], "external")
        self.assertEqual(row["seckey"], "")
        self.assertEqual(row["pubkey"], self.keypair.pubkey)

    def test_registration_is_idempotent_and_never_rekeys(self):
        first = self.register()
        again = self.register()
        self.assertEqual(first["pubkey"], again["pubkey"])
        with self.assertRaises(Conflict) as caught:
            self.register(pubkey=keys.generate().pubkey)
        self.assertEqual(caught.exception.details.get("reason"), "key_already_registered")

    def test_a_key_cannot_be_claimed_twice(self):
        self.register()
        second = self.app.crews.add_agent(self.team, "kilo-two", role="doer",
                                          hosting="external")
        with self.assertRaises(Conflict) as caught:
            self.register(second["id"], self.keypair.pubkey)
        self.assertEqual(caught.exception.details.get("reason"), "pubkey_in_use")

    def test_an_agent_we_run_cannot_register_one(self):
        local = self.app.crews.add_agent(self.team, "wren", role="doer")
        with self.assertRaises(ValidationError) as caught:
            self.register(local["id"])
        self.assertEqual(caught.exception.details.get("reason"), "not_external")

    def test_a_malformed_key_is_refused(self):
        with self.assertRaises(ValidationError) as caught:
            self.register(pubkey="not-a-key")
        self.assertEqual(caught.exception.details.get("reason"), "bad_pubkey")

    def test_the_agentid_follows_the_registered_key(self):
        self.register()
        agent = self.app.crews.get_agent(self.worker["id"])
        self.assertEqual(agent["agentid"], attest.agentid_for(self.keypair.pubkey))


class BeatTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        team = self.crew()
        self.team = team["id"]
        self.worker = self.app.crews.add_agent(team["id"], "kilo", role="doer",
                                              hosting="external")
        self.keypair = keys.generate()
        self.app.audit.keys.register_external(self.worker["id"], self.keypair.pubkey)
        self.card = self.app.kanban.create_task(
            team["id"], "do the thing", payload={"kind": "code", "slice": "doer"})
        self.app.kanban.move(self.card["id"], "ready", "owner")

    def sign(self, payload: dict, keypair=None) -> str:
        digest = attest.payload_digest(attest.canonical(payload))
        return (keypair or self.keypair).sign(bytes.fromhex(digest))

    def beat(self, **kwargs):
        return self.app.heartbeat.beat(self.worker["id"], key=self.worker["agent_key"], **kwargs)

    def rows(self):
        return self.app.audit.history(self.worker["id"])

    def test_an_unsigned_beat_is_recorded_as_unsigned(self):
        self.beat()
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "heartbeat")
        self.assertFalse(rows[0]["attested"])
        self.assertTrue(self.app.audit.verify(self.worker["id"])["ok"])

    def test_a_signed_beat_is_attested_by_the_worker(self):
        payload = {"at": 1_700_000_000.0, "nonce": "n-1"}
        self.beat(attestation=self.sign(payload), at=payload["at"], nonce=payload["nonce"])
        row = self.rows()[0]
        self.assertTrue(row["attested"])
        self.assertEqual(row["pubkey"], self.keypair.pubkey)
        self.assertEqual(row["signer"], attest.agentid_for(self.keypair.pubkey))
        # the signature covers the payload the worker rebuilt, not our row digest
        self.assertEqual(row["attests"], "payload")
        verdict = self.app.audit.verify(self.worker["id"])
        self.assertTrue(verdict["ok"])
        self.assertEqual(verdict["signed"], 1)

    def test_a_forged_signature_is_refused_and_writes_nothing(self):
        payload = {"at": 1_700_000_000.0, "nonce": "n-1"}
        with self.assertRaises(ValidationError) as caught:
            self.beat(attestation=self.sign(payload, keys.generate()),
                      at=payload["at"], nonce=payload["nonce"])
        self.assertEqual(caught.exception.details.get("reason"), "signature_invalid")
        self.assertEqual(self.rows(), [])

    def test_a_signed_beat_without_a_time_is_refused(self):
        payload = {"at": 0.0, "nonce": "n-1"}
        with self.assertRaises(ValidationError) as caught:
            self.beat(attestation=self.sign(payload), nonce="n-1")
        self.assertEqual(caught.exception.details.get("reason"), "beat_signature_needs_at")

    def test_a_beat_signed_before_registration_does_not_count(self):
        newcomer = self.app.crews.add_agent(self.team, "kilo-two", role="doer",
                                            hosting="external")
        payload = {"at": 1_700_000_000.0, "nonce": "n-2"}
        with self.assertRaises(ValidationError) as caught:
            self.app.heartbeat.beat(newcomer["id"], key=newcomer["agent_key"],
                                    attestation=self.sign(payload),
                                    at=payload["at"], nonce=payload["nonce"])
        self.assertEqual(caught.exception.details.get("reason"), "no_registered_key")

    def test_the_bundle_names_the_key_and_verifies_without_the_database(self):
        payload = {"at": 1_700_000_000.0, "nonce": "n-1"}
        self.beat(attestation=self.sign(payload), at=payload["at"], nonce=payload["nonce"])
        bundle = self.app.audit.export(self.worker["id"])
        self.assertEqual(bundle["pubkey"], self.keypair.pubkey)
        self.assertEqual(bundle["agentid"], attest.agentid_for(self.keypair.pubkey))
        self.assertTrue(bundle["verification"]["ok"])
        self.assertEqual(bundle["verification"]["signed"], 1)
        # the same bundle checked by the offline verifier alone
        self.assertTrue(attest.verify_entries(bundle["entries"])["ok"])

    def test_editing_a_signed_row_breaks_the_chain(self):
        payload = {"at": 1_700_000_000.0, "nonce": "n-1"}
        self.beat(attestation=self.sign(payload), at=payload["at"], nonce=payload["nonce"])
        with self.app.store.tx() as c:
            c.execute("update agent_audit set payload = ? where agent_id = ?",
                      ('{"at": 1.0}', self.worker["id"]))
        self.assertFalse(self.app.audit.verify(self.worker["id"])["ok"])


if __name__ == "__main__":
    unittest.main()
