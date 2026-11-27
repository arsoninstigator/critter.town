"""reading crewai and openai agents out of the files they actually ship.

the importer existed and had readers for both, but neither had been run against a real
file, and both were doing the same wrong thing in different places: passing a foreign
tool name straight through as a local permission. crewai ships tool *classes*
(SerperDevTool), openai ships function tools that are json schemas for an api the host
runs. neither exists here, so an imported agent arrived holding names that granted
nothing while looking like they granted something.

and crewai could not be read at all on a default install, because its configs are yaml
and pyyaml is an optional extra: every real file was refused with "install pyyaml". the
subset those files use is small, so `agents/simpleyaml.py` reads it and refuses the rest
by line number.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from base import AppCase  # noqa: E402
from agents import simpleyaml  # noqa: E402
from agents.agent_imports import adopt, from_a2a, from_crewai, from_openai  # noqa: E402
from core.errors import ValidationError  # noqa: E402

CREW_YAML = """
# config/agents.yaml from a real crewai project
researcher:
  role: Senior AI Researcher
  goal: Find the most important model releases each week
  backstory: |
    You have spent a decade reading papers
    nobody else finishes.
  tools:
    - SerperDevTool
    - ScrapeWebsiteTool
    - InternalTickerTool
  llm: gpt-4o
  allow_delegation: false
  max_iter: 25
  memory: true
writer:
  role: Report Writer
  goal: Turn findings into a briefing
  backstory: You write for busy people.
  tools: [FileReadTool]
  llm: claude-sonnet-4
tasks:
  find:
    description: find the releases
    expected_output: a list with sources
    agent: researcher
"""

ASSISTANT = {
    "id": "asst_real", "object": "assistant", "name": "triage bot",
    "instructions": "sort the inbox, escalate anything about money.",
    "model": "gpt-4o",
    "tools": [
        {"type": "function", "function": {"name": "lookup_order",
                                          "parameters": {"type": "object"}}},
        {"type": "function", "function": {"name": "bash"}},
        {"type": "file_search"},
        {"type": "code_interpreter"},
    ],
    "tool_resources": {"file_search": {"vector_store_ids": ["vs_42", "vs_43"]}},
    "response_format": "auto",
    "temperature": 0.2,
}


class YamlSubsetTests(unittest.TestCase):
    def test_a_crewai_config_reads_without_pyyaml(self):
        parsed = simpleyaml.loads(CREW_YAML)
        self.assertEqual(parsed["researcher"]["role"], "Senior AI Researcher")
        self.assertEqual(parsed["researcher"]["tools"],
                         ["SerperDevTool", "ScrapeWebsiteTool", "InternalTickerTool"])
        self.assertIs(parsed["researcher"]["allow_delegation"], False)
        self.assertEqual(parsed["researcher"]["max_iter"], 25)
        self.assertIn("nobody else finishes", parsed["researcher"]["backstory"])

    def test_a_block_scalar_keeps_its_lines_and_a_folded_one_joins_them(self):
        parsed = simpleyaml.loads("a: |\n  one\n  two\nb: >\n  three\n  four\n")
        self.assertEqual(parsed["a"], "one\ntwo")
        self.assertEqual(parsed["b"], "three four")

    def test_lists_of_mappings_and_inline_lists(self):
        parsed = simpleyaml.loads("- name: a\n  port: 1\n- name: b\n- [x, y]\n")
        self.assertEqual(parsed[0], {"name": "a", "port": 1})
        self.assertEqual(parsed[1], {"name": "b"})
        self.assertEqual(parsed[2], ["x", "y"])

    def test_scalars_come_back_as_the_right_type(self):
        parsed = simpleyaml.loads("a: true\nb: no\nc: 3\nd: 1.5\ne: ~\nf: '4'\n")
        self.assertIs(parsed["a"], True)
        self.assertIs(parsed["b"], False)
        self.assertEqual(parsed["c"], 3)
        self.assertEqual(parsed["d"], 1.5)
        self.assertIsNone(parsed["e"])
        self.assertEqual(parsed["f"], "4")

    def test_a_comment_inside_a_quote_is_a_character(self):
        parsed = simpleyaml.loads('a: "the # sign"\nb: value # a comment\n')
        self.assertEqual(parsed["a"], "the # sign")
        self.assertEqual(parsed["b"], "value")

    def test_an_anchor_is_refused_rather_than_read_as_a_string(self):
        with self.assertRaises(ValidationError) as caught:
            simpleyaml.loads("a: &anchor 1\n")
        self.assertEqual(caught.exception.details.get("reason"), "yaml_line_unreadable")
        self.assertEqual(caught.exception.details.get("line"), 1)

    def test_tabs_for_indentation_are_refused_by_line(self):
        with self.assertRaises(ValidationError) as caught:
            simpleyaml.loads("a:\n\tb: 1\n")
        self.assertEqual(caught.exception.details.get("line"), 2)

    def test_a_duplicate_key_is_refused_rather_than_silently_overwritten(self):
        with self.assertRaises(ValidationError) as caught:
            simpleyaml.loads("a: 1\na: 2\n")
        self.assertEqual(caught.exception.details.get("line"), 2)

    def test_a_line_that_belongs_to_nothing_is_refused(self):
        with self.assertRaises(ValidationError):
            simpleyaml.loads("a: 1\nstray line\n")

    def test_flow_mappings_are_refused_with_a_reason(self):
        with self.assertRaises(ValidationError) as caught:
            simpleyaml.loads("a: {b: 1}\n")
        self.assertIn("flow mapping", str(caught.exception))


class CrewaiTests(unittest.TestCase):
    def test_a_real_agents_yaml_becomes_a_manifest(self):
        manifest = from_crewai(CREW_YAML)
        self.assertEqual(manifest.name, "researcher")
        self.assertEqual(manifest.role, "Senior AI Researcher")
        self.assertEqual(manifest.model, "gpt-4o")
        self.assertIn("## goal", manifest.instructions)
        self.assertIn("## backstory", manifest.instructions)

    def test_tool_classes_map_to_ours_and_the_rest_are_reported(self):
        manifest = from_crewai(CREW_YAML)
        self.assertEqual(manifest.tools, ["search", "http"])
        self.assertIn("tool.InternalTickerTool", manifest.unmapped)

    def test_tasks_are_not_imported_as_identity(self):
        manifest = from_crewai(CREW_YAML)
        self.assertIn("tasks", manifest.unmapped)
        self.assertTrue(any("board" in warning for warning in manifest.warnings))
        self.assertNotIn("find the releases", manifest.instructions)

    def test_another_agent_in_the_same_file_is_named_explicitly(self):
        manifest = from_crewai(CREW_YAML, "writer")
        self.assertEqual(manifest.name, "writer")
        self.assertEqual(manifest.tools, ["files"])
        self.assertEqual(manifest.model, "claude-sonnet-4")

    def test_a_yaml_file_with_no_agents_is_refused(self):
        with self.assertRaises(ValidationError):
            from_crewai("just: a mapping\nof: things\n")


class OpenaiTests(unittest.TestCase):
    def test_function_tools_are_reported_not_granted(self):
        manifest = from_openai(ASSISTANT)
        # "bash" is one of our tool names, so it maps; "lookup_order" is somebody's api
        # and is reported instead of granted
        self.assertEqual(manifest.tools, ["shell"])
        self.assertIn("tool.function.lookup_order", manifest.unmapped)
        self.assertIn("tool.file_search", manifest.unmapped)
        self.assertIn("tool.code_interpreter", manifest.unmapped)

    def test_a_function_named_after_one_of_our_tools_maps_to_it(self):
        manifest = from_openai(ASSISTANT)
        self.assertEqual(manifest.tools, ["shell"])

    def test_vector_stores_are_a_warning_because_their_files_did_not_arrive(self):
        manifest = from_openai(ASSISTANT)
        self.assertIn("knowledge.vector_store", manifest.unmapped)
        self.assertTrue(any("vector store" in warning for warning in manifest.warnings))

    def test_tuning_fields_are_named_rather_than_dropped(self):
        manifest = from_openai(ASSISTANT)
        self.assertIn("temperature", manifest.unmapped)
        self.assertIn("response_format", manifest.unmapped)


REAL_CARD = {
    "protocolVersion": "0.2.5",
    "name": "Invoice Reconciler",
    "description": ("Reconciles supplier invoices against purchase orders and flags "
                    "mismatches. Runs nightly on the operator's own infrastructure."),
    "url": "https://agents.example.com/a2a/v1",
    "preferredTransport": "JSONRPC",
    "version": "1.4.0",
    "provider": {"organization": "Example Ltd", "url": "https://example.com"},
    "capabilities": {"streaming": True, "pushNotifications": False},
    "defaultInputModes": ["text/plain"],
    "skills": [
        {"id": "reconcile", "name": "Reconcile invoices",
         "description": "Matches invoices to POs by amount and date.",
         "tags": ["finance"], "examples": ["reconcile November"]},
        {"id": "flag", "name": "Flag mismatches",
         "description": "Lists invoices that cannot be matched."},
    ],
    "securitySchemes": {"apiKey": {"type": "apiKey", "in": "header"}},
    "security": [{"apiKey": []}],
}


class A2aCardTests(unittest.TestCase):
    def test_a_card_is_read_as_the_remote_partys_account(self):
        manifest = from_a2a(REAL_CARD)
        self.assertEqual(manifest.name, "Invoice Reconciler")
        self.assertEqual(
            manifest.role,
            "Reconciles supplier invoices against purchase orders and flags mismatches")
        self.assertIn("not an instruction you were given", manifest.instructions)
        self.assertIn("Runs nightly", manifest.instructions)

    def test_the_remote_skills_never_become_our_skills(self):
        manifest = from_a2a(REAL_CARD)
        self.assertEqual(manifest.skills, [])
        self.assertIn("a2a.skill:reconcile", manifest.unmapped)
        self.assertIn("a2a.skill:flag", manifest.unmapped)

    def test_the_remote_skills_are_kept_as_attributed_knowledge(self):
        manifest = from_a2a(REAL_CARD)
        titles = [entry["title"] for entry in manifest.knowledge]
        self.assertIn("Invoice Reconciler: Reconcile invoices", titles)
        body = manifest.knowledge[0]["text"]
        self.assertIn("the remote agent claims", body)
        self.assertIn("source: https://agents.example.com/a2a/v1", body)

    def test_a_card_carrying_a_credential_is_refused(self):
        card = dict(REAL_CARD, securitySchemes={
            "apiKey": {"type": "apiKey", "value": "sk-live-abcdefghijklmnop"}})
        with self.assertRaises(ValidationError) as caught:
            from_a2a(card)
        self.assertEqual(caught.exception.details.get("reason"), "card_carries_a_secret")

    def test_a_plain_http_endpoint_is_a_warning(self):
        manifest = from_a2a(dict(REAL_CARD, url="http://agents.example.com/a2a"))
        self.assertTrue(any("https" in warning for warning in manifest.warnings))

    def test_the_transport_and_security_are_reported_rather_than_copied(self):
        manifest = from_a2a(REAL_CARD)
        for key in ("card.url", "card.securitySchemes", "card.security",
                    "card.preferredTransport", "card.provider"):
            self.assertIn(key, manifest.unmapped)

    def test_a_card_with_no_description_says_the_prompt_is_empty(self):
        manifest = from_a2a({"name": "bare", "url": "https://x.example/a2a"})
        self.assertEqual(manifest.instructions, "")
        self.assertTrue(any("no system prompt" in warning for warning in manifest.warnings))


class AdoptionTests(AppCase):
    def test_an_imported_crewai_agent_lands_with_mapped_tools_only(self):
        team = self.crew("studio")
        result = adopt(self.app, team["id"], from_crewai(CREW_YAML))
        profile = self.app.profile.profile(result["agent"]["id"])["namespaces"]
        enabled = {name for name, entry in (profile.get("tools") or {}).items()
                   if entry.get("enabled")}
        self.assertEqual(enabled, {"search", "http"})
        self.assertNotIn("InternalTickerTool", enabled)

    def test_the_imported_agent_arrives_with_its_prompt_and_nothing_else(self):
        team = self.crew("studio")
        result = adopt(self.app, team["id"], from_crewai(CREW_YAML))
        stored = self.crews.get_agent(result["agent"]["id"])
        prompt = stored.get("system_prompt") or stored.get("prompt") or ""
        self.assertIn("Senior AI Researcher", prompt)
        self.assertEqual(self.app.kanban.board(team["id"])["columns"][0]["tasks"], [])
