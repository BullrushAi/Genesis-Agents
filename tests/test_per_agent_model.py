"""Per-agent model override: precedence tests (stdlib unittest, no network, no model calls).

These tests cover the resolver only. They do not call any provider, do not read live
env, and do not prove a router model id resolves — that is a deploy-time Platform check.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runtime.per_agent_model import (
    override_env_name,
    per_agent_override,
    resolve_agent_model,
    slug_to_env_name,
)

GLOBAL = "anthropic/claude-sonnet-4-5"
BUNDLE = "anthropic/claude-sonnet-4-5"
DEFAULT = "minimax/minimax-m2.5"
OVERRIDE = "M3.1-Flash"


class TestSlugToEnvName(unittest.TestCase):
    def test_knowledge_evidence(self):
        self.assertEqual(
            override_env_name("genesis-knowledge-evidence"),
            "GENESIS_MODEL_OVERRIDE_GENESIS_KNOWLEDGE_EVIDENCE",
        )

    def test_case_and_separators(self):
        self.assertEqual(slug_to_env_name("Genesis-Knowledge_Evidence"), "GENESIS_KNOWLEDGE_EVIDENCE")

    def test_empty_slug(self):
        self.assertEqual(override_env_name(""), "")
        self.assertEqual(override_env_name(None), "")


class TestResolvePrecedence(unittest.TestCase):
    def test_override_beats_global_and_bundle(self):
        env = {"GENESIS_MODEL_OVERRIDE_GENESIS_KNOWLEDGE_EVIDENCE": OVERRIDE}
        model, source = resolve_agent_model(
            "genesis-knowledge-evidence", BUNDLE, GLOBAL, DEFAULT, env=env
        )
        self.assertEqual((model, source), (OVERRIDE, "per_agent_override"))

    def test_other_agent_falls_back_to_global(self):
        env = {"GENESIS_MODEL_OVERRIDE_GENESIS_KNOWLEDGE_EVIDENCE": OVERRIDE}
        model, source = resolve_agent_model(
            "genesis-fin-ap", BUNDLE, GLOBAL, DEFAULT, env=env
        )
        self.assertEqual((model, source), (GLOBAL, "global"))

    def test_no_slug_falls_back_to_global(self):
        model, source = resolve_agent_model(None, BUNDLE, GLOBAL, DEFAULT, env={})
        self.assertEqual((model, source), (GLOBAL, "global"))

    def test_global_auto_falls_back_to_bundle(self):
        model, source = resolve_agent_model(
            "genesis-fin-ap", BUNDLE, "auto", DEFAULT, env={}
        )
        self.assertEqual((model, source), (BUNDLE, "bundle"))

    def test_override_auto_falls_through_to_global(self):
        env = {"GENESIS_MODEL_OVERRIDE_GENESIS_KNOWLEDGE_EVIDENCE": "auto"}
        model, source = resolve_agent_model(
            "genesis-knowledge-evidence", BUNDLE, GLOBAL, DEFAULT, env=env
        )
        self.assertEqual((model, source), (GLOBAL, "global"))

    def test_empty_override_falls_through(self):
        env = {"GENESIS_MODEL_OVERRIDE_GENESIS_KNOWLEDGE_EVIDENCE": "   "}
        model, source = resolve_agent_model(
            "genesis-knowledge-evidence", BUNDLE, GLOBAL, DEFAULT, env=env
        )
        self.assertEqual((model, source), (GLOBAL, "global"))

    def test_no_global_no_bundle_falls_to_default(self):
        model, source = resolve_agent_model(
            "genesis-fin-ap", "", "", DEFAULT, env={}
        )
        self.assertEqual((model, source), (DEFAULT, "default"))

    def test_override_value_passed_verbatim(self):
        # No provider prefix is invented here: the value is the env value, verbatim.
        env = {"GENESIS_MODEL_OVERRIDE_GENESIS_KNOWLEDGE_EVIDENCE": "  M3.1-Flash  "}
        self.assertEqual(
            per_agent_override("genesis-knowledge-evidence", env=env), "M3.1-Flash"
        )


if __name__ == "__main__":
    unittest.main()
