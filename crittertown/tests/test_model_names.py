"""a model name is a request, not a promise.

the importers used to put whatever the source said straight into the model column, so a
crewai `llm: gpt-4o, best for writing` became a value a vendor is asked to serve. there is
no allowlist to check against here (the platform talks to an openai-compatible endpoint),
so the only honest check is shape: is this a name at all. these tests hold that line, and
hold the second half of it: a name this deployment does not serve is kept and explained,
never silently dropped, because dropping it would hide the operator's mismatch.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from base import AppCase  # noqa: E402
from core.errors import ValidationError  # noqa: E402
from agents import models  # noqa: E402
from agents.agent_imports import (  # noqa: E402
    adopt, from_a2a, from_claude_code, from_crewai, from_openai,
)


class NameTests(unittest.TestCase):
    def test_a_plain_name_is_kept(self):
        self.assertEqual(models.normalize("gpt-4.1-mini"), "gpt-4.1-mini")
        self.assertEqual(models.normalize("  gpt-4o  "), "gpt-4o")
        self.assertEqual(models.normalize(""), "")

    def test_a_provider_prefix_is_the_same_name(self):
        self.assertEqual(models.normalize("openai/gpt-4o"), "gpt-4o")
        self.assertEqual(models.normalize("anthropic/claude-sonnet-4"), "claude-sonnet-4")
        self.assertEqual(models.normalize("ollama/llama3:8b"), "llama3:8b")

    def test_a_sentence_is_refused_rather_than_trimmed(self):
        # trimming to "gpt-4o" would hide the part the source was telling us
        for raw in ("gpt-4o, best for writing", "gpt 4o", "the fast one", "gpt-4o (recommended)"):
            with self.assertRaises(ValidationError) as caught:
                models.normalize(raw)
            self.assertEqual(caught.exception.details.get("why"), "model_not_a_name", raw)

    def test_a_url_or_path_is_not_a_name(self):
        for raw in ("https://api.example/v1", "~/models/gguf", "/models/x", "a" * 90):
            with self.assertRaises(ValidationError):
                models.normalize(raw)

    def test_the_advisory_says_who_decides(self):
        self.assertIn("deployment decides", models.advisory("gpt-4o", "gpt-4.1-mini"))
        self.assertIn("which is what this deployment serves",
                      models.advisory("gpt-4.1-mini", "gpt-4.1-mini"))
        self.assertIn("deployment decides", models.advisory("gpt-4o", ""))
        self.assertEqual(models.advisory("", "gpt-4.1-mini"), "")


class ReaderTests(unittest.TestCase):
    def test_crewai_llm_keeps_its_name_and_loses_its_prefix(self):
        manifest = from_crewai("a:\n  role: R\n  llm: openai/gpt-4o\n")
        self.assertEqual(manifest.model, "gpt-4o")

    def test_a_sentence_in_the_model_field_is_unmapped_not_adopted(self):
        manifest = from_crewai("a:\n  role: R\n  llm: gpt-4o, best for writing\n")
        self.assertEqual(manifest.model, "")
        self.assertIn("model:gpt-4o, best for writing", manifest.unmapped)
        self.assertTrue(manifest.warnings)

    def test_every_reader_runs_the_same_rule(self):
        card = {"name": "clerk", "description": "files invoices. quickly.",
                "skills": [], "model": "anthropic/claude-sonnet-4"}
        self.assertEqual(from_a2a(card).model, "claude-sonnet-4")
        assistant = {"name": "bot", "instructions": "sort the inbox carefully",
                     "model": "gpt-4.1-mini"}
        self.assertEqual(from_openai(assistant).model, "gpt-4.1-mini")
        subagent = ("---\nname: triage\nmodel: claude-sonnet-4\n---\n"
                    "you triage support mail\n")
        self.assertEqual(from_claude_code(subagent).model, "claude-sonnet-4")


class AdoptionTests(AppCase):
    def test_a_model_this_deployment_does_not_serve_is_kept_and_explained(self):
        team = self.crew("studio")
        result = adopt(self.app, team["id"], from_crewai("a:\n  role: R\n  llm: gpt-4o\n"))
        self.assertEqual(result["agent"]["model"], "gpt-4o")
        self.assertTrue(any("deployment decides" in warning
                            for warning in result["warnings"]))

    def test_a_refused_model_never_reaches_the_row(self):
        team = self.crew("studio")
        result = adopt(self.app, team["id"],
                       from_crewai("a:\n  role: R\n  llm: the fast one\n"))
        self.assertEqual(result["agent"].get("model") or "", "")
        self.assertTrue(any("not imported: model:" in warning
                            for warning in result["warnings"]))

    def test_the_claim_is_recorded_beside_the_import(self):
        team = self.crew("studio")
        result = adopt(self.app, team["id"], from_crewai("a:\n  role: R\n  llm: gpt-4o\n"))
        agent_id = result["agent"]["id"]
        row = self.app.store.one(
            "select value from agent_profile where agent_id = ? and namespace = ? "
            "and key = ?", (agent_id, "instructions", "import-source"))
        self.assertIsNotNone(row)
        self.assertIn("gpt-4o", str(row["value"]))
