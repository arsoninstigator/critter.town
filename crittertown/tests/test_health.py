"""health that can say no.

a liveness answer that is always true is a decoration. /health says the process is up and
counts rows; /health/ready says the store answers, and fails when it does not, because a
process with a missing database is exactly the case a load balancer must not send work to.
"""

from __future__ import annotations

import unittest

from base import AppCase  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from api.routes import create_api  # noqa: E402


class HealthTests(AppCase):
    def setUp(self):
        super().setUp()
        self.client = TestClient(create_api(self.app))

    def test_health_counts_rows_and_is_public(self):
        team = self.crew("health")
        self.kanban.create_task(team["id"], "a card", column="ready")
        out = self.client.get("/health").json()
        self.assertTrue(out["ok"])
        self.assertEqual(out["counts"]["tasks"], 1)

    def test_readiness_answers_ok_when_the_store_answers(self):
        out = self.client.get("/health/ready")
        self.assertEqual(out.status_code, 200)
        self.assertEqual(out.json(), {"ok": True, "store": "ok"})

    def test_readiness_fails_when_the_store_does_not(self):
        def broken(*_args, **_kwargs):
            raise RuntimeError("database is gone")

        self.app.store.one = broken
        out = self.client.get("/health/ready")
        self.assertEqual(out.status_code, 503)
        self.assertFalse(out.json()["ok"])
        self.assertIn("RuntimeError", out.json()["store"])


if __name__ == "__main__":
    unittest.main()
