from __future__ import annotations

import argparse
import base64
import copy
import functools
import hashlib
import importlib.util
import io
import inspect
import json
import os
import pathlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import warnings
from collections import deque
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

KIT_DIR = Path(__file__).resolve().parents[1]
PRIVATE_REPO_DIR = KIT_DIR.parent
PUBLIC_REPO_DIR = KIT_DIR.parents[1]
if (PRIVATE_REPO_DIR / "skill" / "SKILL.md").is_file():
    REPO_DIR = PRIVATE_REPO_DIR
    SKILL_DIR = REPO_DIR / "skill"
else:
    REPO_DIR = PUBLIC_REPO_DIR
    SKILL_DIR = REPO_DIR / "integrations" / "openclaw"
sys.path.insert(0, str(KIT_DIR))

import helpers  # noqa: E402
import arena_client  # noqa: E402
import agent as heuristic_agent  # noqa: E402
import check as offline_check  # noqa: E402
import decision_context  # noqa: E402
import hermes_agent  # noqa: E402
import match_state  # noqa: E402
import llm_agent  # noqa: E402
import memory  # noqa: E402
import play as turn_player  # noqa: E402
import runner  # noqa: E402
import run_local  # noqa: E402
import setup_local_runner  # noqa: E402
import setup_starter_kit  # noqa: E402


class DecisionContextContractTests(unittest.TestCase):
    @staticmethod
    def v2_context(**turn_overrides):
        context = {
            "version": 2,
            "profile": "stateless",
            "stable": {
                "id": "",
                "game_type": "future_game",
                "rules": {"rules": ["server only"]},
                "strategy": {"objective": "win"},
                "user_preferences": {"risk_profile": "balanced"},
                "message_language": "ko",
            },
            "turn": {
                "status": "playing",
                "is_your_turn": True,
                "game_type": "future_game",
                "match_id": 77,
                "seq": "seq-1",
                "action_window_id": "window-1",
                "turn_deadline": "2026-08-07T08:00:00Z",
                "state_mode": "full",
                "state": {"new_resource": 7},
                "legal_actions": [{"action": "choose", "params": {}}],
            },
            "fallback": {"action": "choose", "params": {}},
        }
        context["turn"].update(turn_overrides)
        context["stable"]["id"] = decision_context.stable_context_id(context)
        return context

    def test_v2_validation_preserves_optional_turn_contract_without_aliasing(self):
        raw = self.v2_context(
            state_removed=["expired_offer"],
            action_rejection={"field": "option", "allowed_values": ["safe"]},
            decision_support={
                "recommended_action": {
                    "action": "choose",
                    "params": {},
                },
            },
        )
        normalized = decision_context.normalize_decision_context(raw)

        self.assertIsNotNone(normalized)
        self.assertEqual(normalized["stable"]["id"], raw["stable"]["id"])
        self.assertEqual(normalized["turn"]["state_removed"], ["expired_offer"])
        self.assertEqual(normalized["turn"]["action_rejection"]["field"], "option")
        self.assertEqual(
            normalized["turn"]["decision_support"]["recommended_action"]["action"],
            "choose",
        )
        self.assertEqual(normalized["fallback"]["action"], "choose")
        normalized["turn"]["state"]["new_resource"] = 99
        normalized["turn"]["decision_support"]["recommended_action"]["action"] = "mutated"
        self.assertEqual(raw["turn"]["state"]["new_resource"], 7)
        self.assertEqual(
            raw["turn"]["decision_support"]["recommended_action"]["action"],
            "choose",
        )

    def test_v2_rejects_forged_id_and_invalid_profile_state_mode_pair(self):
        forged = self.v2_context()
        forged["stable"]["id"] = "dc2-deadbeefdeadbeefdeadbeef"
        self.assertIsNone(decision_context.normalize_decision_context(forged))

        delta_stateless = self.v2_context(state_mode="delta")
        delta_stateless["stable"]["id"] = decision_context.stable_context_id(delta_stateless)
        self.assertIsNone(decision_context.normalize_decision_context(delta_stateless))

        session = self.v2_context(state_mode="delta")
        session["profile"] = "session"
        session["stable"]["id"] = decision_context.stable_context_id(session)
        self.assertIsNotNone(decision_context.normalize_decision_context(session))

    def test_v1_envelope_is_promoted_without_overwriting_server_preferences(self):
        raw = {
            "version": 1,
            "game_type": "future_game",
            "state": {"new_resource": 7},
            "legal_actions": [{"action": "choose", "params": {}}],
            "rules": {"rules": ["v1 server rule"]},
            "user_preferences": {
                "current_strategy_hint": "server-original",
                "current_risk_profile": "careful",
            },
        }
        envelope = {
            "decision_context": raw,
            "status": "playing",
            "is_your_turn": True,
            "match_id": 77,
            "seq": "seq-1",
            "action_window_id": "window-1",
            "strategy_brief": {"objective": "filled-only-because-missing"},
            "agent_preferences": {
                "strategy_hint": "legacy-alias-must-not-overwrite",
                "risk_profile": "aggressive",
            },
        }
        original = copy.deepcopy(raw)

        normalized = decision_context.decision_context_from_envelope(envelope)

        self.assertEqual(raw, original)
        self.assertEqual(normalized["version"], 1)
        self.assertEqual(normalized["profile"], "stateless")
        self.assertTrue(normalized["stable"]["id"].startswith("dc1-"))
        self.assertEqual(normalized["stable"]["rules"], raw["rules"])
        self.assertEqual(normalized["stable"]["strategy"], envelope["strategy_brief"])
        self.assertEqual(normalized["stable"]["user_preferences"], raw["user_preferences"])

    def test_prompt_payload_can_reference_stable_context_by_id_only(self):
        context = self.v2_context()
        context["turn"]["decision_support"] = {
            "recommended_action": {"action": "choose", "params": {}},
        }
        context["turn"]["legal_actions"][0]["hint"] = {
            "server_fallback": {"params": {}},
            "visible": "keep",
        }
        payload = decision_context.context_prompt_payload(context, include_stable=False)

        self.assertEqual(payload["stable"], {"id": context["stable"]["id"]})
        self.assertEqual(payload["turn"]["state"], {"new_resource": 7})
        self.assertEqual(
            payload["turn"]["decision_support"]["recommended_action"]["action"],
            "choose",
        )
        self.assertNotIn("fallback", payload)
        self.assertNotIn("server_fallback", payload["turn"]["legal_actions"][0]["hint"])
        self.assertEqual(payload["turn"]["legal_actions"][0]["hint"]["visible"], "keep")
        self.assertIn("fallback", context)
        self.assertIn("server_fallback", context["turn"]["legal_actions"][0]["hint"])

    def test_non_mapping_decision_support_is_rejected(self):
        context = self.v2_context(decision_support="bad")

        self.assertIsNone(decision_context.normalize_decision_context(context))

    def test_generic_params_contract_covers_current_and_future_actions(self):
        liars = self.v2_context()
        liars["turn"]["legal_actions"] = [{
            "action": "bid",
            "params_schema": {
                "type": "object",
                "required": ["quantity", "face"],
                "properties": {
                    "quantity": {"type": "integer", "minimum": 1, "maximum": 10},
                    "face": {"type": "integer", "minimum": 1, "maximum": 6},
                },
                "additionalProperties": False,
            },
        }]
        self.assertIn(
            "params.quantity must be integer",
            decision_context.validate_action_payload(
                {"action": "bid", "params": {"quantity": "3", "face": 6}}, liars,
            ),
        )
        self.assertIn(
            "params.quantity is above maximum 10",
            decision_context.validate_action_payload(
                {"action": "bid", "params": {"quantity": 11, "face": 6}}, liars,
            ),
        )

        mafia = self.v2_context()
        mafia["turn"]["legal_actions"] = [{
            "action": "vote",
            "params_schema": {
                "type": "object",
                "required": ["target_id"],
                "properties": {"target_id": {"type": "integer", "enum": [2, 3]}},
            },
        }]
        self.assertEqual(
            decision_context.validate_action_payload(
                {"action": "vote", "params": {"target_id": 9}}, mafia,
            ),
            ["params.target_id is not in allowed enum"],
        )

        future = self.v2_context()
        future["turn"]["legal_actions"] = [{
            "action": "allocate",
            "params_schema": {
                "type": "object",
                "required": ["resource_id"],
                "properties": {"resource_id": {"type": "string", "minLength": 1}},
                "additionalProperties": False,
            },
        }]
        self.assertEqual(
            decision_context.validate_action_payload(
                {"action": "allocate", "params": {}}, future,
            ),
            ["params.resource_id is required"],
        )

    def test_schema_enum_canonicalization_is_recursive_and_conservative(self):
        legal = [{
            "action": "submit_adjustments",
            "params_schema": {
                "type": "object",
                "properties": {
                    "orders": {
                        "type": "array",
                        "items": {
                            "oneOf": [
                                {
                                    "type": "object",
                                    "properties": {
                                        "type": {"type": "string", "enum": ["BUILD"]},
                                        "unit_type": {"type": "string", "enum": ["A", "F"]},
                                        "destination": {"type": "string", "enum": ["LON"]},
                                    },
                                    "required": ["type", "unit_type", "destination"],
                                    "additionalProperties": False,
                                },
                                {
                                    "type": "object",
                                    "properties": {
                                        "type": {"type": "string", "enum": ["WAIVE"]},
                                    },
                                    "required": ["type"],
                                    "additionalProperties": False,
                                },
                            ],
                        },
                    },
                    "posture": {
                        "anyOf": [
                            {"type": "string", "enum": ["HOLD"]},
                            {"type": "string", "enum": ["MOVE"]},
                        ],
                    },
                },
                "required": ["orders", "posture"],
                "additionalProperties": False,
            },
        }]
        lower = {
            "action": "submit_adjustments",
            "params": {
                "orders": [{
                    "type": "build",
                    "unit_type": "f",
                    "destination": "lon",
                }],
                "posture": "move",
            },
        }

        canonical = decision_context.canonicalize_action_payload(lower, legal)

        self.assertEqual(canonical, {
            "action": "submit_adjustments",
            "params": {
                "orders": [{
                    "type": "BUILD",
                    "unit_type": "F",
                    "destination": "LON",
                }],
                "posture": "MOVE",
            },
        })
        self.assertEqual(lower["params"]["orders"][0]["type"], "build")
        self.assertEqual(
            decision_context.validate_action_payload(canonical, legal),
            [],
        )

        ambiguous = [{
            "action": "choose",
            "params_schema": {
                "type": "object",
                "properties": {
                    "recipient": {
                        "type": "string",
                        "enum": ["GLOBAL", "global"],
                    },
                },
                "required": ["recipient"],
            },
        }]
        unchanged = decision_context.canonicalize_action_payload(
            {"action": "choose", "params": {"recipient": "Global"}},
            ambiguous,
        )
        self.assertEqual(unchanged["params"]["recipient"], "Global")
        self.assertTrue(
            decision_context.validate_action_payload(unchanged, ambiguous),
        )

        press = [{
            "action": "send_press",
            "params_schema": {
                "type": "object",
                "properties": {
                    "messages": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "to_power": {
                                    "type": "string",
                                    "enum": ["FRANCE", "global"],
                                },
                                "content": {"type": "string"},
                            },
                            "required": ["to_power", "content"],
                        },
                    },
                },
                "required": ["messages"],
            },
        }]
        global_recipient = decision_context.canonicalize_action_payload(
            {
                "action": "send_press",
                "params": {
                    "messages": [{"to_power": "GLOBAL", "content": "Hello"}],
                },
            },
            press,
        )
        self.assertEqual(
            global_recipient["params"]["messages"][0]["to_power"],
            "global",
        )
        self.assertEqual(
            decision_context.validate_action_payload(global_recipient, press),
            [],
        )
        invalid = decision_context.canonicalize_action_payload(
            {
                "action": "send_press",
                "params": {
                    "messages": [{"to_power": "NOWHERE", "content": "Hello"}],
                },
            },
            press,
        )
        self.assertEqual(
            invalid["params"]["messages"][0]["to_power"],
            "NOWHERE",
        )
        self.assertTrue(decision_context.validate_action_payload(invalid, press))

    def test_executable_fallback_requires_current_action_and_valid_params(self):
        context = self.v2_context()
        context["fallback"] = {"action": "choose", "params": {}}
        self.assertEqual(
            decision_context.executable_fallback(context),
            {"action": "choose", "params": {}},
        )
        self.assertIsNone(
            decision_context.executable_fallback(
                context,
                [{"action": "wait", "params": {}}],
            )
        )
        context["fallback"]["params"] = "bad"
        self.assertIsNone(decision_context.executable_fallback(context))


class CodingAgentTurnViewTests(unittest.TestCase):
    def test_codex_turn_view_uses_canonical_server_context_without_raw_duplicates(self):
        context = DecisionContextContractTests.v2_context()
        context["turn"]["legal_actions"] = [{
            "action": "choose",
            "params_schema": {
                "type": "object",
                "required": ["option"],
                "properties": {"option": {"type": "string", "enum": ["safe"]}},
            },
            "hint": {"recommended": "safe"},
        }]
        context["stable"]["id"] = decision_context.stable_context_id(context)
        poll = {
            "status": "playing",
            "is_your_turn": True,
            "game_type": "future_game",
            "match_id": 77,
            "state": {"raw_duplicate": "must-not-win"},
            "legal_actions": [{"action": "legacy"}],
            "game_rules_brief": {"rules": ["legacy"]},
            "decision_context": context,
        }

        view = turn_player._turn_view(poll)

        self.assertEqual(view["decision_context_version"], 2)
        self.assertEqual(view["decision_context_id"], context["stable"]["id"])
        self.assertEqual(view["rules"], {"rules": ["server only"]})
        self.assertEqual(view["state"], {"new_resource": 7})
        self.assertEqual(view["legal_actions"], context["turn"]["legal_actions"])
        self.assertNotIn("fallback", view)
        self.assertNotIn("raw_duplicate", json.dumps(view))

    def test_codex_turn_view_keeps_complete_legacy_snapshot_when_context_is_absent(self):
        poll = {
            "status": "playing",
            "is_your_turn": True,
            "game_type": "future_game",
            "match_id": 88,
            "action_window_id": "window-88",
            "turn_deadline": "2026-08-07T10:00:00Z",
            "state": {"future_resource": 9},
            "legal_actions": [{"action": "allocate", "params": {}}],
            "game_rules_brief": {"rules": ["dynamic"]},
            "strategy_brief": {"objective": "win"},
            "agent_preferences": {"message_language": "ko"},
        }

        view = turn_player._turn_view(poll)

        self.assertEqual(view["state"], {"future_resource": 9})
        self.assertEqual(view["rules"], {"rules": ["dynamic"]})
        self.assertEqual(view["strategy"], {"objective": "win"})
        self.assertEqual(view["user_preferences"], {"message_language": "ko"})

    def test_coding_agent_main_plays_without_an_llm_provider_key(self):
        context = DecisionContextContractTests.v2_context()
        emitted = []
        schema = {
            "heartbeat": {
                "body_template": {
                    "status": "idle",
                    "feed_status": "connected",
                },
            },
        }
        poll = {
            "status": "playing",
            "is_your_turn": True,
            "decision_context": context,
        }

        with (
            mock.patch.dict(
                os.environ,
                {"CLAWARENA_CONNECTION_TOKEN": "arena-token"},
                clear=True,
            ),
            mock.patch.object(sys, "argv", ["play.py"]),
            mock.patch.object(turn_player.arena_client, "fetch_schema", return_value=schema),
            mock.patch.object(turn_player.arena_client, "heartbeat") as heartbeat,
            mock.patch.object(turn_player.arena_client, "poll", return_value=(200, poll)) as poll_call,
            mock.patch.object(turn_player, "_emit", side_effect=emitted.append),
        ):
            result = turn_player.main()
            self.assertNotIn("LLM_API_KEY", os.environ)
            self.assertEqual(os.environ["CLAWARENA_BRAIN"], "coding-agent")

        self.assertEqual(result, 0)
        heartbeat.assert_called_once_with("arena-token", schema)
        poll_call.assert_called_once_with("arena-token", wait=0)
        self.assertEqual(emitted[0]["decision_context_version"], 2)
        self.assertTrue(emitted[0]["is_your_turn"])

    def test_save_token_only_exits_before_heartbeat_or_matchmaking(self):
        emitted = []
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(sys, "argv", ["play.py", "--save-token"]),
            mock.patch.object(turn_player, "_resolve_token", return_value="arena-token") as resolve,
            mock.patch.object(turn_player.arena_client, "fetch_schema") as fetch_schema,
            mock.patch.object(turn_player.arena_client, "poll") as poll,
            mock.patch.object(turn_player, "_emit", side_effect=emitted.append),
        ):
            result = turn_player.main()

        self.assertEqual(result, 0)
        resolve.assert_called_once_with(run_local.DEFAULT_ARENA_BASE, save=True)
        fetch_schema.assert_not_called()
        poll.assert_not_called()
        self.assertEqual(emitted, [{
            "ok": True,
            "status": "token_saved",
            "next": (
                "Ask the owner to approve the first live match, then run "
                "python3 play.py --wait 30."
            ),
        }])


class ProtocolTests(unittest.TestCase):
    def test_vegas_invalid_model_face_is_normalized_to_best_current_legal_face(self):
        state = {
            "game_type": "las_vegas",
            "_action_window_id": "6b117efc81045514",
        }
        legal_actions = [{
            "action": "place",
            "hint": {
                "faces_available": [
                    {
                        "face": 2,
                        "dice_you_would_place": 1,
                        "your_dice_already_there": 0,
                        "casino_dice_by_player": {"other": 2},
                        "casino_bills": [30000],
                    },
                    {
                        "face": 6,
                        "dice_you_would_place": 2,
                        "your_dice_already_there": 0,
                        "casino_dice_by_player": {"other": 1},
                        "casino_bills": [80000, 20000],
                    },
                ],
            },
        }]
        reply = json.dumps({
            "action": "place",
            "params": {"face": 5, "message": "Taking the best live option."},
        })

        parsed = llm_agent._parse_action(reply, legal_actions, state)
        provenance = llm_agent._reply_provenance(
            reply, legal_actions, state, finish_reason="stop",
        )

        self.assertEqual(parsed, {
            "action": "place",
            "params": {"face": 6, "message": "Taking the best live option."},
        })
        self.assertEqual(provenance["outcome"], "accepted")
        self.assertEqual(provenance["normalization"], "las_vegas_legal_face")
        self.assertEqual(provenance["contract_problems"], [])

    def test_vegas_string_face_is_canonicalized_without_reselection(self):
        state = {"game_type": "las_vegas"}
        legal_actions = [{
            "action": "place",
            "hint": {"faces_available": [{"face": 3}]},
        }]

        parsed = llm_agent._parse_action(
            '{"action":"place","params":{"face":"3"}}',
            legal_actions,
            state,
        )

        self.assertEqual(parsed, {"action": "place", "params": {"face": 3}})

    def test_monopoly_trade_reply_is_bound_to_current_server_opening(self):
        state = {
            "game_type": "monopoly",
            "_action_window_id": "trade-window-1",
        }
        legal_actions = [
            {
                "action": "propose_trade",
                "hint": {
                    "server_trade_openings": [
                        {
                            "suggested_action": {
                                "action": "propose_trade",
                                "to_agent_id": 22,
                                "offer_cash": 100,
                                "request_cash": 0,
                                "offer_space_ids": [],
                                "request_space_ids": [17],
                            }
                        },
                        {
                            "suggested_action": {
                                "action": "propose_trade",
                                "to_agent_id": 33,
                                "offer_cash": 140,
                                "request_cash": 0,
                                "offer_space_ids": [6],
                                "request_space_ids": [19],
                            }
                        },
                    ]
                },
            },
            {"action": "end_turn", "hint": {}},
        ]
        reply = json.dumps(
            {
                "action": "propose_trade",
                "params": {
                    "to_agent_id": 33,
                    "offer_cash": 120,
                    "request_cash": 0,
                    "offer_space_ids": [6],
                    "request_space_ids": [19],
                    "message": "A balanced exchange that preserves liquidity.",
                },
            }
        )

        parsed = llm_agent._parse_action(reply, legal_actions, state)
        provenance = llm_agent._reply_provenance(
            reply, legal_actions, state, finish_reason="stop",
        )

        self.assertEqual(parsed["action"], "propose_trade")
        self.assertEqual(parsed["params"]["to_agent_id"], 33)
        self.assertEqual(parsed["params"]["offer_cash"], 140)
        self.assertEqual(parsed["params"]["request_space_ids"], [19])
        self.assertEqual(
            parsed["params"]["message"],
            "A balanced exchange that preserves liquidity.",
        )
        self.assertEqual(provenance["outcome"], "accepted")
        self.assertEqual(provenance["normalization"], "server_trade_opening")
        self.assertEqual(provenance["finish_reason"], "stop")
        self.assertNotIn("A balanced exchange", json.dumps(provenance))

    def test_monopoly_34_missing_opening_replies_become_one_shot_non_trade(self):
        """Reproduce the complete match-1187 exact-opening fallback category."""
        state = {"game_type": "monopoly", "_action_window_id": "trade-window-empty"}
        legal_actions = [
            {"action": "roll", "hint": {}},
            {"action": "propose_trade", "hint": {"server_trade_openings": []}},
        ]

        for index in range(34):
            reply = json.dumps({
                "action": "propose_trade",
                "params": {
                    "to_agent_id": 200 + index,
                    "offer_cash": index,
                    "offer_space_ids": [],
                    "request_cash": 0,
                    "request_space_ids": [1 + index],
                },
            })
            parsed = llm_agent._parse_action(reply, legal_actions, state)
            provenance = llm_agent._reply_provenance(
                reply, legal_actions, state, finish_reason="stop",
            )

            self.assertEqual(parsed, {"action": "roll", "params": {}})
            self.assertEqual(provenance["outcome"], "accepted")
            self.assertEqual(provenance["normalized_action"], "roll")
            self.assertEqual(
                provenance["normalization"],
                "server_trade_opening_unavailable_nontrade",
            )
            self.assertEqual(provenance["contract_problems"], [])

    def test_monopoly_trade_opening_canonicalizes_string_ids_and_stale_terms(self):
        state = {"game_type": "monopoly"}
        legal_actions = [{
            "action": "propose_trade",
            "hint": {"server_trade_openings": [{
                "suggested_action": {
                    "action": "propose_trade",
                    "to_agent_id": "22",
                    "offer_cash": "75",
                    "offer_space_ids": ["6"],
                    "offer_jail_cards": "0",
                    "request_cash": "0",
                    "request_space_ids": ["19"],
                    "request_jail_cards": "0",
                },
            }]},
        }, {"action": "end_turn", "hint": {}}]

        parsed = llm_agent._parse_action(json.dumps({
            "action": "propose_trade",
            "params": {
                "to_agent_id": 99,
                "offer_cash": 1,
                "offer_space_ids": [39],
                "request_cash": 500,
                "request_space_ids": [1],
            },
        }), legal_actions, state)

        self.assertEqual(parsed, {
            "action": "propose_trade",
            "params": {
                "to_agent_id": 22,
                "offer_cash": 75,
                "request_cash": 0,
                "offer_jail_cards": 0,
                "request_jail_cards": 0,
                "offer_space_ids": [6],
                "request_space_ids": [19],
            },
        })

    def test_monopoly_invalid_one_sided_server_opening_uses_non_trade(self):
        state = {"game_type": "monopoly"}
        legal_actions = [{
            "action": "propose_trade",
            "hint": {"server_trade_openings": [{
                "suggested_action": {
                    "action": "propose_trade",
                    "to_agent_id": 22,
                    "offer_cash": 0,
                    "offer_space_ids": [],
                    "request_cash": 0,
                    "request_space_ids": [19],
                },
            }]},
        }, {"action": "end_turn", "hint": {}}]

        parsed = llm_agent._parse_action(
            '{"action":"propose_trade","params":{"to_agent_id":22}}',
            legal_actions,
            state,
        )

        self.assertEqual(parsed, {"action": "end_turn", "params": {}})

    def test_monopoly_manage_batch_binds_to_current_server_plan_without_reinference(self):
        """Reproduce match-1188 window c571b3bde3214abb deterministically."""
        state = {
            "game_type": "monopoly",
            "_action_window_id": "c571b3bde3214abb",
        }
        legal_actions = [
            {
                "action": "manage_batch",
                "hint": {
                    "server_manage_batch": {
                        "params": {
                            "operations": [
                                {"action": "build_house", "space_id": "11", "count": "1"},
                                {"action": "build_house", "space_id": 13},
                            ],
                        },
                    },
                },
            },
            {"action": "end_turn", "hint": {}},
        ]
        stale_reply = json.dumps({
            "action": "manage_batch",
            "params": {
                "operations": [
                    {"action": "build_house", "space_id": 14, "count": 8},
                    {"action": "mortgage", "space_id": 99},
                ],
                "message": "One current atomic plan.",
            },
        })

        parsed = llm_agent._parse_action(stale_reply, legal_actions, state)
        provenance = llm_agent._reply_provenance(
            stale_reply,
            legal_actions,
            state,
            finish_reason="stop",
        )

        self.assertEqual(parsed, {
            "action": "manage_batch",
            "params": {
                "operations": [
                    {"action": "build_house", "space_id": 11, "count": 1},
                    {"action": "build_house", "space_id": 13, "count": 1},
                ],
                "message": "One current atomic plan.",
            },
        })
        self.assertEqual(provenance["outcome"], "accepted")
        self.assertEqual(provenance["normalization"], "server_manage_batch")
        self.assertEqual(provenance["contract_problems"], [])

    def test_monopoly_manage_batch_without_current_plan_uses_non_batch_action(self):
        state = {"game_type": "monopoly", "_action_window_id": "empty-batch"}
        legal_actions = [
            {"action": "manage_batch", "hint": {"server_manage_batch": {"params": {"operations": []}}}},
            {"action": "roll", "hint": {}},
        ]
        reply = '{"action":"manage_batch","params":{"operations":[]}}'

        parsed = llm_agent._parse_action(reply, legal_actions, state)
        provenance = llm_agent._reply_provenance(
            reply,
            legal_actions,
            state,
            finish_reason="stop",
        )

        self.assertEqual(parsed, {"action": "roll", "params": {}})
        self.assertEqual(provenance["outcome"], "accepted")
        self.assertEqual(
            provenance["normalization"],
            "server_manage_batch_unavailable_nonbatch",
        )

    def test_monopoly_server_manage_batch_rejects_non_management_binding(self):
        hint = {
            "server_manage_batch": {
                "params": {
                    "operations": [{"action": "propose_trade", "space_id": 11}],
                },
            },
        }

        self.assertIsNone(helpers.server_manage_batch_params(hint))

    def test_monopoly_heuristic_path_never_uses_uncurated_trade_advice(self):
        state = {
            "game_type": "monopoly",
            "heuristic_advice": {
                "recommended_action": {
                    "action": "propose_trade",
                    "to_agent_id": 22,
                    "offer_cash": 1,
                    "request_space_ids": [19],
                },
            },
        }
        legal_actions = [
            {"action": "roll", "hint": {}},
            {"action": "propose_trade", "hint": {"server_trade_openings": []}},
        ]

        self.assertEqual(
            heuristic_agent.decide(state, legal_actions),
            {"action": "roll", "params": {}},
        )

    def test_low_level_client_never_reads_the_legacy_global_token_implicitly(self):
        with (
            mock.patch.dict(os.environ, {"CLAWARENA_CONNECTION_TOKEN": ""}),
            mock.patch.object(arena_client, "TOKEN_PATH", None),
            self.assertRaisesRegex(SystemExit, "Use run_local.py"),
        ):
            arena_client.connection_token()

    def test_distributed_release_versions_are_internally_consistent(self):
        # Every file the installers will try to download must exist in the one
        # directory Django serves. A name in the manifest with no file behind it
        # used to arrive as an HTML 404 page written to disk under a .py name.
        distributed_files = {
            *setup_local_runner.KIT_FILES,
            *setup_starter_kit.CORE_FILES,
            *setup_starter_kit.USER_FILES,
            *(f"fixtures/{name}" for name in setup_starter_kit.FIXTURE_FILES),
            *(f"strategy/{name}" for name in setup_starter_kit.STRATEGY_FILES),
            "setup_local_runner.py",
        }
        for name in sorted(distributed_files):
            self.assertTrue((KIT_DIR / name).is_file(), name)

        skill_md = (SKILL_DIR / "SKILL.md").read_text()
        skill_version = re.search(r"^version:\s*([^\s]+)", skill_md, re.MULTILINE)
        self.assertIsNotNone(skill_version)
        # The ClawHub/OpenClaw skill and the Hermes/BYO server kit have separate
        # release lifecycles. Keep each artifact internally coherent without
        # forcing an unrelated skill-only patch to bump distributed kit bytes.
        self.assertEqual(
            json.loads((SKILL_DIR / "package.json").read_text())["version"],
            skill_version.group(1),
        )
        # The backend test lane mounts backend/ AS the repo root, so the tree
        # above kit/ is not always the checkout layout. Look in both places
        # rather than fail on a path shape that says nothing about the code.
        schema_path = next(
            (
                path
                for path in (
                    REPO_DIR / "backend" / "apps" / "agents" / "agent_schema.py",
                    REPO_DIR / "apps" / "agents" / "agent_schema.py",
                )
                if path.is_file()
            ),
            None,
        )
        if schema_path is not None:
            agent_schema = schema_path.read_text()
            schema_version = re.search(
                r'^STARTER_KIT_VERSION\s*=\s*"([^"]+)"',
                agent_schema,
                re.MULTILINE,
            )
            self.assertIsNotNone(schema_version)
            self.assertEqual(schema_version.group(1), arena_client.CLIENT_VERSION)
        else:
            manifest = json.loads((REPO_DIR / "releases" / "manifest.json").read_text())
            # The Starter Kit and the OpenClaw skill are versioned
            # independently and no longer move together: production serves kit
            # 5.13.72 alongside skill 5.13.49. What must hold is that the kit
            # entry matches this client, and that the two OpenClaw artifacts
            # agree with each other — the same rule scripts/release_manifest.py
            # enforces.
            self.assertEqual(
                manifest["versions"]["starter_kit"],
                arena_client.CLIENT_VERSION,
            )
            self.assertEqual(
                manifest["versions"]["openclaw_skill"],
                manifest["versions"]["openclaw_package"],
            )
            openapi = json.loads((REPO_DIR / "openapi" / "agent-api-v1.json").read_text())
            self.assertEqual(openapi["info"]["version"], arena_client.CLIENT_VERSION)

    def test_managed_hermes_defaults_to_low_reasoning(self):
        # managed_runtimes/ is server-side operational plumbing and is
        # deliberately outside the public boundary, so this file is absent in
        # the published copy of this suite. Skip rather than fail.
        entrypoint_path = REPO_DIR / "managed_runtimes" / "managed_hermes_entrypoint.sh"
        if not entrypoint_path.is_file():
            self.skipTest("managed_runtimes/ is not part of the public distribution")
        entrypoint = entrypoint_path.read_text()

        self.assertIn("HERMES_GAMEPLAY_REASONING_EFFORT=low", entrypoint)
        self.assertIn("HERMES_GAMEPLAY_THINKING_MODE=enabled", entrypoint)
        self.assertNotIn("HERMES_GAMEPLAY_REASONING_EFFORT:-", entrypoint)
        self.assertNotIn("HERMES_GAMEPLAY_THINKING_MODE:-", entrypoint)
        self.assertIn('"reasoning_effort": "low"', entrypoint)
        self.assertNotIn('"reasoning_effort": "none"', entrypoint)

    @unittest.skipUnless(
        shutil.which("curl"),
        "curl transport integration runs in the host test lane; runtime images use clawarena-runtime-smoke",
    )
    def test_documented_multi_file_curl_globs_are_shell_safe(self):
        documented_files = sorted(
            path
            for root in (
                KIT_DIR,
                REPO_DIR / "frontend",
                REPO_DIR / "docs",
                REPO_DIR / "integrations",
            )
            if root.exists()
            for path in root.rglob("*")
            if path.is_file() and path.suffix in {".md", ".tsx"}
        )
        for path in documented_files:
            for line_number, line in enumerate(path.read_text().splitlines(), start=1):
                if "curl -fsSLO" not in line or "{" not in line:
                    continue
                self.assertRegex(
                    line,
                    r"curl -fsSLO\s+['\"]",
                    f"{path}:{line_number} must quote the URL glob so curl saves every file",
                )

        class QuietHandler(SimpleHTTPRequestHandler):
            def log_message(self, _format, *_args):
                return

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "serve"
            kit_root = root / "kit"
            download_root = Path(tmp) / "download"
            kit_root.mkdir(parents=True)
            download_root.mkdir()
            for name in ("one.py", "two.py", "three.py"):
                (kit_root / name).write_text(name)

            handler = functools.partial(QuietHandler, directory=str(root))
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                origin = f"http://127.0.0.1:{server.server_port}"
                command = f"curl -fsSLO '{origin}/kit/{{one.py,two.py,three.py}}'"
                result = subprocess.run(
                    ["/bin/sh", "-c", command],
                    cwd=download_root,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                self.assertEqual(
                    sorted(path.name for path in download_root.iterdir()),
                    ["one.py", "three.py", "two.py"],
                )
                self.assertEqual(result.stdout, "")
            finally:
                server.shutdown()
                server.server_close()

    def test_starter_installer_preserves_user_code_and_stages_upstream(self):
        def fetch(url, target):
            relative = url.split("/kit/", 1)[1]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((KIT_DIR / relative).read_bytes())

        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "bot"
            first = setup_starter_kit.install(
                origin="https://arena.example",
                destination=destination,
                run_checks=True,
                fetch=fetch,
            )
            self.assertEqual(first["status"], "installed")
            self.assertEqual(first["read_first"], "README.md")
            self.assertEqual(first["checks"], ["check.py", "mock_arena.py"])
            self.assertIn("play.py --save-token", first["next"])
            self.assertIn("no provider key", first["next"])
            self.assertNotIn("run_local.py", first["next"])
            for relative in (
                "fixtures/diplomacy_movement.json",
                "fixtures/diplomacy_negotiation.json",
                "fixtures/diplomacy_retreat.json",
                "fixtures/diplomacy_adjustment.json",
                "strategy/diplomacy.md",
            ):
                self.assertEqual(
                    (destination / relative).read_bytes(),
                    (KIT_DIR / relative).read_bytes(),
                    relative,
                )
            custom_agent = (destination / "agent.py").read_text() + "\nCUSTOM = True\n"
            (destination / "agent.py").write_text(custom_agent)

            second = setup_starter_kit.install(
                origin="https://arena.example",
                destination=destination,
                run_checks=False,
                fetch=fetch,
            )

            self.assertEqual(second["status"], "updated")
            self.assertEqual((destination / "agent.py").read_text(), custom_agent)
            self.assertEqual(
                (destination / "agent.py.upstream").read_bytes(),
                (KIT_DIR / "agent.py").read_bytes(),
            )
            self.assertIn("agent.py", second["preserved_user_files"])
            self.assertIn("agent.py.upstream", second["upstream_copies"])
            self.assertIn(".clawarena/", (destination / ".gitignore").read_text())
            self.assertEqual(
                (destination / ".clawarena" / "arena_base").read_text().strip(),
                "https://arena.example/api/v1",
            )
            # The builder kit deliberately omits the managed shared policy.
            # It must consume the server deadline without inheriting the hosted
            # fleet's 105s/165s inference cap.
            self.assertFalse((destination / "decision_policy.py").exists())
            import_check = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import runner; "
                        "assert runner.DEFAULT_DECISION_CAP_SECONDS is None; "
                        "assert runner.DIPLOMACY_DECISION_CAP_SECONDS is None; "
                        "budget = runner.shared_decision_budget("
                        "{'turn_deadline':'1970-01-01T00:02:00+00:00'}, "
                        "clock=lambda: 0.0); "
                        "assert budget['configured_seconds'] is None; "
                        "assert budget['effective_seconds'] == 120.0; "
                        "assert budget['policy'] == 'server_deadline_only'"
                    ),
                ],
                cwd=destination,
                env={
                    **os.environ,
                    "PYTHONPATH": str(destination),
                    "CLAWARENA_DECISION_MAX_SECONDS": "",
                    "CLAWARENA_DIPLOMACY_DECISION_MAX_SECONDS": "",
                    "CLAWARENA_SUBMIT_RESERVE_SECONDS": "0",
                },
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(
                import_check.returncode,
                0,
                import_check.stderr or import_check.stdout,
            )

    def test_starter_installer_download_failure_leaves_existing_files_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "bot"
            destination.mkdir()
            existing = destination / "runner.py"
            existing.write_text("ORIGINAL\n")

            with self.assertRaisesRegex(RuntimeError, "network down"):
                setup_starter_kit.install(
                    origin="https://arena.example",
                    destination=destination,
                    run_checks=False,
                    fetch=lambda _url, _target: (_ for _ in ()).throw(RuntimeError("network down")),
                )

            self.assertEqual(existing.read_text(), "ORIGINAL\n")

    def test_starter_installer_namespaces_legacy_state_before_switching_arenas(self):
        def fetch(url, target):
            relative = url.split("/kit/", 1)[1]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((KIT_DIR / relative).read_bytes())

        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "bot"
            legacy = destination / ".clawarena"
            legacy.mkdir(parents=True)
            (legacy / "token").write_text("prod-token\n")
            (legacy / "arena_base").write_text("https://prod.example/api/v1\n")

            result = setup_starter_kit.install(
                origin="https://test.example",
                destination=destination,
                run_checks=False,
                fetch=fetch,
            )

            target = (
                legacy
                / "instances"
                / setup_starter_kit._arena_scope("https://prod.example/api/v1")
                / "starter-kit"
            )
            self.assertEqual((target / "token").read_text().strip(), "prod-token")
            self.assertEqual(
                json.loads((target / setup_starter_kit.STATE_OWNER_FILENAME).read_text())["arena_base"],
                "https://prod.example/api/v1",
            )
            self.assertEqual(
                (legacy / "arena_base").read_text().strip(),
                "https://test.example/api/v1",
            )
            self.assertEqual(result["migrated_state"], str(target))

    def test_starter_default_state_is_isolated_by_arena(self):
        root = Path("/tmp/clawarena-bot")
        prod = run_local._default_state_dir(root, "https://prod.example/api/v1")
        test = run_local._default_state_dir(root, "https://test.example/api/v1")

        self.assertNotEqual(prod, test)
        self.assertEqual(prod.parts[-1], "starter-kit")

    def test_private_starter_launcher_saves_only_the_arena_token(self):
        args = argparse.Namespace(
            arena_base="https://arena.example/api/v1",
            model="",
            llm_base_url="",
            use_gateway=False,
            no_save_token=False,
        )
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".clawarena"
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch.object(
                    run_local,
                    "_private_prompt",
                    side_effect=["arena-token", "provider-key"],
                ),
                mock.patch.object(
                    run_local,
                    "_text_prompt",
                    side_effect=["model-x", "https://llm.example/v1"],
                ),
            ):
                env = run_local._runner_environment(args, state_dir)

            token_path = state_dir / "token"
            self.assertEqual(token_path.read_text().strip(), "arena-token")
            self.assertEqual(stat.S_IMODE(token_path.stat().st_mode), 0o600)
            self.assertEqual(env["LLM_API_KEY"], "provider-key")
            self.assertEqual(env["LLM_MODEL"], "model-x")
            self.assertFalse(any("provider-key" in path.read_text(errors="ignore") for path in state_dir.rglob("*") if path.is_file()))

    def test_private_starter_launcher_recommends_deepseek_for_a_new_key(self):
        args = argparse.Namespace(
            arena_base="https://arena.example/api/v1",
            model="",
            llm_base_url="",
            use_gateway=False,
            no_save_token=True,
        )
        prompts = []

        def accept_default(label, default):
            prompts.append((label, default))
            return default

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".clawarena"
            with (
                mock.patch.dict(os.environ, {"CLAWARENA_CONNECTION_TOKEN": "arena-token"}, clear=True),
                mock.patch.object(run_local, "_private_prompt", return_value="provider-key"),
                mock.patch.object(run_local, "_text_prompt", side_effect=accept_default),
            ):
                env = run_local._runner_environment(args, state_dir)

        self.assertEqual(env["LLM_MODEL"], "deepseek-v4-flash")
        self.assertEqual(env["LLM_BASE_URL"], "https://api.deepseek.com/v1")
        self.assertEqual(
            prompts,
            [
                ("Model id (DeepSeek recommended)", "deepseek-v4-flash"),
                ("OpenAI-compatible base URL", "https://api.deepseek.com/v1"),
            ],
        )

    def test_existing_key_only_environment_keeps_legacy_openai_defaults(self):
        args = argparse.Namespace(
            arena_base="https://arena.example/api/v1",
            model="",
            llm_base_url="",
            use_gateway=False,
            no_save_token=True,
        )
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".clawarena"
            with mock.patch.dict(
                os.environ,
                {
                    "CLAWARENA_CONNECTION_TOKEN": "arena-token",
                    "LLM_API_KEY": "existing-openai-key",
                },
                clear=True,
            ):
                env = run_local._runner_environment(args, state_dir)

        self.assertEqual(env["LLM_MODEL"], run_local.DEFAULT_MODEL)
        self.assertEqual(env["LLM_BASE_URL"], run_local.DEFAULT_LLM_BASE)

    def test_private_starter_launcher_keeps_one_match_as_the_default(self):
        args = argparse.Namespace(
            continuous=False,
            matches=1,
            dry_run=False,
            preflight_only=False,
        )
        command = run_local._runner_command(args, Path("/tmp/runner.py"))
        self.assertEqual(command[-2:], ["--matches", "1"])

    def test_private_starter_launcher_uses_the_installer_arena_origin(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".clawarena"
            state_dir.mkdir()
            (state_dir / "arena_base").write_text("https://test.example/api/v1\n")

            self.assertEqual(
                run_local._arena_base("", state_dir),
                "https://test.example/api/v1",
            )
            self.assertEqual(
                run_local._arena_base("https://override.example/api/v1", state_dir),
                "https://override.example/api/v1",
            )

    def test_heartbeat_reports_neutral_client_version_without_skill_identity(self):
        schema = {
            "heartbeat": {
                "body_template": {
                    "status": "idle",
                    "feed_status": "connected",
                    "skill_slug": "ai-clawarena",
                    "skill_version": "5.12.1",
                    "watcher_protocol_version": 3,
                },
            },
        }
        with (
            mock.patch.dict(os.environ, {"CLAWARENA_BRAIN": "hermes"}),
            mock.patch.object(arena_client, "request", return_value=(200, {})) as request_call,
        ):
            self.assertEqual(arena_client.heartbeat("token", schema), 200)

        payload = request_call.call_args.kwargs["payload"]
        self.assertEqual(payload["client"], "clawarena-kit")
        self.assertEqual(payload["brain"], "hermes")
        self.assertEqual(payload["client_version"], arena_client.CLIENT_VERSION)
        self.assertNotIn("skill_slug", payload)
        self.assertNotIn("skill_version", payload)
        self.assertNotIn("watcher_protocol_version", payload)

    def test_coding_agent_heartbeat_is_distinct_from_keyed_llm_runner(self):
        schema = {
            "heartbeat": {
                "body_template": {
                    "status": "idle",
                    "feed_status": "connected",
                },
            },
        }
        with mock.patch.dict(
            os.environ,
            {"CLAWARENA_BRAIN": "coding-agent"},
            clear=True,
        ):
            payload = arena_client._heartbeat_body(schema)

        self.assertEqual(payload["client"], "clawarena-kit")
        self.assertEqual(payload["brain"], "coding-agent")

    def test_heartbeat_with_response_can_ack_restart_without_losing_identity(self):
        schema = {
            "heartbeat": {
                "body_template": {
                    "status": "idle",
                    "feed_status": "connected",
                    "skill_slug": "ai-clawarena",
                    "skill_version": "old",
                },
            },
        }
        response = {"agent_preferences": {"watcher_restart_ack_at": "2026-08-01T10:00:01Z"}}
        with (
            mock.patch.dict(os.environ, {"CLAWARENA_BRAIN": "hermes"}),
            mock.patch.object(arena_client, "request", return_value=(200, response)) as request_call,
        ):
            self.assertEqual(
                arena_client.heartbeat_with_response("token", schema, restart_ack=True),
                (200, response),
            )

        payload = request_call.call_args.kwargs["payload"]
        self.assertTrue(payload["restart_ack"])
        self.assertEqual(payload["client"], "clawarena-kit")
        self.assertEqual(payload["brain"], "hermes")
        self.assertNotIn("skill_slug", payload)

    def test_runner_acknowledges_pending_restart_before_exec(self):
        requested = "2026-08-01T10:00:00+00:00"
        with (
            mock.patch.object(
                runner.arena_client,
                "heartbeat_with_response",
                return_value=(200, {
                    "agent_preferences": {
                        "watcher_restart_requested_at": requested,
                        "watcher_restart_ack_at": "2026-08-01T10:00:01+00:00",
                    },
                }),
            ) as heartbeat,
            mock.patch.object(runner.os, "execv") as execv,
            mock.patch.object(sys, "argv", ["runner.py"]),
        ):
            runner._ack_and_restart("token", {}, {
                "agent_preferences": {"watcher_restart_requested_at": requested},
            })

        heartbeat.assert_called_once_with("token", {}, restart_ack=True)
        execv.assert_called_once()
        self.assertEqual(execv.call_args.args[1][-1], str(Path(runner.__file__).resolve()))

    def test_runner_does_not_exec_when_restart_ack_is_not_persisted(self):
        requested = "2026-08-01T10:00:00+00:00"
        with (
            mock.patch.object(
                runner.arena_client,
                "heartbeat_with_response",
                return_value=(200, {
                    "agent_preferences": {"watcher_restart_requested_at": requested},
                }),
            ),
            mock.patch.object(runner.os, "execv") as execv,
        ):
            self.assertFalse(runner._ack_and_restart("token", {}, {
                "agent_preferences": {"watcher_restart_requested_at": requested},
            }))
        execv.assert_not_called()

    def test_first_poll_can_request_an_automatic_context_resync(self):
        with mock.patch.object(arena_client, "request", return_value=(200, {})) as request_call:
            arena_client.poll("token", wait=30, resync=True, context_id="runner-1")

        self.assertEqual(
            request_call.call_args.args[1],
            "/agents/game/?wait=30&snapshot=full&consume_preferences=1"
            "&decision_context_version=2&decision_context_profile=stateless"
            "&consume_history=1&resync=1&context_id=runner-1",
        )

    def test_runner_preserves_server_v1_stable_values_and_applies_window_backoff(self):
        server_context = {
            "version": 1,
            "game_type": "future_game",
            "state": {"resource": 7},
            "legal_actions": [
                {"action": "choose", "params": {}},
                {"action": "wait", "params": {}},
            ],
            "rules": {"rules": ["server rule"]},
            "strategy": {"objective": "server objective"},
            "user_preferences": {
                "current_strategy_hint": "server hint",
                "current_risk_profile": "careful",
            },
        }
        poll = {
            "decision_context": server_context,
            "game_type": "future_game",
            "status": "playing",
            "is_your_turn": True,
        }
        cached_state = {
            "game_rules_brief": {"rules": ["stale cached rule"]},
            "strategy_brief": {"objective": "stale cached objective"},
            "user_preferences": {
                "strategy_hint": "legacy alias",
                "risk_profile": "aggressive",
            },
        }
        usable = [{"action": "wait", "params": {}}]
        rejection = {"rejected_action": "choose", "message": "try another"}

        canonical = runner._decision_context_for_turn(
            poll, cached_state, usable, rejection,
        )

        self.assertEqual(canonical["stable"]["rules"], server_context["rules"])
        self.assertEqual(canonical["stable"]["strategy"], server_context["strategy"])
        self.assertEqual(
            canonical["stable"]["user_preferences"],
            server_context["user_preferences"],
        )
        self.assertEqual(canonical["turn"]["legal_actions"], usable)
        self.assertEqual(canonical["turn"]["action_rejection"], rejection)
        self.assertEqual(len(server_context["legal_actions"]), 2)

    def test_llm_preflight_uses_resolved_model_and_rejects_empty_reply(self):
        with (
            mock.patch.object(
                llm_agent,
                "_llm_config",
                return_value=("https://llm.example/v1", "key", "model-x"),
            ),
            mock.patch.object(
                llm_agent,
                "_chat_request",
                return_value="CLAWARENA_READY",
            ) as chat,
        ):
            self.assertEqual(
                llm_agent.preflight(),
                "model-x via https://llm.example/v1",
            )

        self.assertEqual(chat.call_args.kwargs["max_tokens"], llm_agent.LLM_PREFLIGHT_MAX_TOKENS)
        self.assertFalse(chat.call_args.kwargs["structured_json"])

        with (
            mock.patch.object(
                llm_agent,
                "_llm_config",
                return_value=("https://llm.example/v1", "key", "model-x"),
            ),
            mock.patch.object(llm_agent, "_chat_request", return_value=""),
            self.assertRaisesRegex(RuntimeError, "empty completion"),
        ):
            llm_agent.preflight()

    def test_llm_preflight_discovers_openai_compatible_context_metadata(self):
        with (
            mock.patch.object(
                llm_agent,
                "_llm_config",
                return_value=("https://llm.example/v1", "key", "model-x"),
            ),
            mock.patch.object(
                llm_agent,
                "_chat_request",
                return_value={
                    "text": "CLAWARENA_READY",
                    "prompt_tokens": 12,
                    "finish_reason": "stop",
                },
            ),
            mock.patch.object(
                llm_agent,
                "_model_context_window",
                return_value=1_000_000,
            ) as discover,
        ):
            llm_agent.preflight()

        discover.assert_called_once_with(
            "https://llm.example/v1",
            "key",
            "model-x",
            discover=True,
        )

    def test_action_key_changes_with_payload_but_not_key_order(self):
        first = {"action": "bid", "params": {"quantity": 2, "face": 3}}
        reordered = {"params": {"face": 3, "quantity": 2}, "action": "bid"}
        corrected = {"action": "bid", "params": {"quantity": 3, "face": 3}}

        self.assertEqual(
            helpers.action_idempotency_key("7:state", first),
            helpers.action_idempotency_key("7:state", reordered),
        )
        self.assertNotEqual(
            helpers.action_idempotency_key("7:state", first),
            helpers.action_idempotency_key("7:state", corrected),
        )

    def test_action_parser_returns_memo_without_persisting_anything(self):
        """The parser hands the memo back; it must not write it anywhere.

        The memo used to be persisted to a per-match file. That file is gone --
        the session transcript carries the match now -- and the memo's only
        remaining consumer is the optional per-turn report. The parser was never
        allowed to write, and the point of this test is that it still doesn't.
        """

        move = llm_agent._parse_action(
            '{"action":"bid","params":{"quantity":2},"memo":"private read"}',
            [{"action": "bid"}],
            {"game_type": "liars_dice"},
        )

        self.assertEqual(move["memo"], "private read")
        self.assertFalse(
            [name for name in dir(memory) if name.startswith("record_")],
            "the match move/memo log is gone; nothing should write one",
        )

    def test_hermes_programmatic_parser_keeps_only_final_json(self):
        stdout = "\n".join([
            f"Unknown toolset: {hermes_agent.HERMES_NO_TOOLS_SENTINEL}",
            'Reasoning candidate: {"action":"bid","params":{"quantity":99}}',
            '{"action":"challenge","params":{}}',
        ])

        cleaned = hermes_agent._extract_programmatic_reply(stdout)

        self.assertEqual(cleaned, '{"action":"challenge","params":{}}')
        self.assertNotIn("Reasoning", cleaned)


class StarterSessionTests(unittest.TestCase):
    def setUp(self):
        llm_agent._reset_session()

    def tearDown(self):
        llm_agent._reset_session()

    @staticmethod
    def state(*, chat_log=None, phase="discuss", moves=None):
        return {
            "game_type": "mafia",
            "phase": phase,
            "day_number": 1,
            "my_agent_id": 12,
            "my_name": "starter-test",
            "my_role": "citizen",
            "chat_log": list(chat_log or []),
            "game_rules_brief": {"rules": ["Use the current legal actions."]},
            "strategy_brief": {"role": "citizen", "guidance": ["Track claims."]},
            "user_preferences": {"risk_profile": "balanced"},
            "message_language": "ko",
            "my_memory": {
                "note": "stay consistent",
                "my_recent_moves": list(moves or []),
                "my_private_reads": [],
            },
        }

    @staticmethod
    def legal():
        return [{"action": "chat", "params": {"message": "string"}}]

    def test_first_turn_puts_stable_match_context_before_full_state(self):
        with mock.patch.object(llm_agent.memory, "current_match_id", return_value=41):
            messages, pending = llm_agent._prepare_conversation(self.state(), self.legal())

        self.assertEqual(pending["mode"], "full")
        self.assertEqual([message["role"] for message in messages], ["system", "user"])
        match_text, baseline_text = messages[1]["content"].split("\n\nSTATE_BASELINE:\n", 1)
        context = json.loads(match_text.removeprefix("MATCH_CONTEXT:\n"))
        baseline = json.loads(baseline_text)
        self.assertEqual(context["game_type"], "mafia")
        self.assertEqual(context["identity"]["my_role"], "citizen")
        self.assertEqual(context["message_language"], "ko")
        self.assertNotIn("game_rules_brief", baseline["state"])
        # my_memory is gone from every window and from the file it was read
        # from. The board must not smuggle it back either: the board builder
        # keeps every key it does not recognise, which is exactly how it
        # returned the first time it was "removed".
        self.assertNotIn("my_memory", baseline["state"])
        self.assertNotIn("my_memory", json.dumps(baseline))
        self.assertLess(
            messages[1]["content"].index("game_rules_brief"),
            messages[1]["content"].index("identity"),
        )

    def test_followup_appends_to_exact_transcript_and_sends_only_delta(self):
        first_state = self.state(chat_log=[{"message": "one"}])
        second_state = self.state(
            chat_log=[{"message": "one"}, {"message": "two"}],
            phase="vote",
            moves=[{"action": "chat", "params": {"message": "first"}}],
        )
        with mock.patch.object(llm_agent.memory, "current_match_id", return_value=41):
            first_messages, first_pending = llm_agent._prepare_conversation(
                first_state, self.legal())
            llm_agent._commit_conversation(
                first_pending,
                '{"action":"chat","params":{"message":"first"}}',
            )
            second_messages, second_pending = llm_agent._prepare_conversation(
                second_state, self.legal())

        self.assertEqual(second_pending["mode"], "delta")
        self.assertEqual(second_messages[:2], first_messages)
        self.assertEqual(second_messages[2]["role"], "assistant")
        self.assertEqual(second_messages[3]["role"], "user")
        update = json.loads(
            second_messages[3]["content"].removeprefix("TURN_UPDATE:\n")
        )
        self.assertEqual(update["state_delta"]["phase"], "vote")
        self.assertEqual(
            update["state_delta"]["chat_log"],
            {"_appended": [{"message": "two"}]},
        )
        self.assertEqual(update["context_delta"], {})
        self.assertNotIn("game_rules_brief", update["state_delta"])
        self.assertNotIn("my_memory", update)
        self.assertNotIn("my_memory_delta", update)

    def test_diplomacy_context_epoch_does_not_restart_the_session(self):
        """A server context rebase must not throw away the cached prefix.

        The epoch is a server-side rebase marker; the rebased turn still arrives
        as a full board that the client diff handles. Resetting on it discarded
        the accumulated transcript on roughly every third diplomacy turn, which
        is the whole cost the session mode exists to avoid.
        """

        legal = [{"action": "send_press", "params": {"messages": "array"}}]

        def diplomacy_state(phase, epoch):
            return {
                "game_type": "diplomacy",
                "phase": phase,
                "phase_key": phase,
                "phase_type": "negotiation",
                "decision_context_epoch": epoch,
                "power": "ENGLAND",
                "my_memory": {"my_recent_moves": []},
            }

        with mock.patch.object(llm_agent.memory, "current_match_id", return_value=88):
            _first, first_pending = llm_agent._prepare_conversation(
                diplomacy_state("S1902M-N1", "S1902M"), legal,
            )
            llm_agent._commit_conversation(
                first_pending,
                '{"action":"send_press","params":{"messages":[]}}',
            )
            second, second_pending = llm_agent._prepare_conversation(
                diplomacy_state("S1902M-N2", "S1902M"), legal,
            )
            llm_agent._commit_conversation(
                second_pending,
                '{"action":"send_press","params":{"messages":[]}}',
            )
            next_season, next_pending = llm_agent._prepare_conversation(
                diplomacy_state("F1902M-N1", "F1902M"), legal,
            )

        self.assertEqual(second_pending["mode"], "delta")
        self.assertIn("TURN_UPDATE:\n", second[-1]["content"])
        self.assertEqual(next_pending["mode"], "delta")
        self.assertIn("TURN_UPDATE:\n", next_season[-1]["content"])
        self.assertEqual(next_pending["prior_turn_count"], 2)

    def test_token_pressure_rebuilds_a_full_authoritative_baseline(self):
        with mock.patch.object(llm_agent.memory, "current_match_id", return_value=41):
            messages, pending = llm_agent._prepare_conversation(self.state(), self.legal())
            llm_agent._commit_conversation(pending, '{"action":"chat","params":{}}')
            messages, pending = llm_agent._prepare_conversation(
                self.state(),
                self.legal(),
                context_window=100,
            )

        self.assertEqual(pending["mode"], "compacted")
        self.assertEqual([message["role"] for message in messages], ["system", "user"])
        self.assertIn("STATE_BASELINE:\n", messages[1]["content"])
        self.assertNotIn("TURN_UPDATE:\n", messages[1]["content"])

    def test_byte_stable_tool_survives_the_delta_transport(self):
        """A transport flag must not silently switch the tool back to dynamic.

        The gate used to require the wire profile be "stateless". Under the delta
        transport the server answers "session" and the materializer hands the
        brain the same complete board, so the payload is identical -- but the
        byte-stable tool turned off. Measured on Claw Vegas as tools_bytes going
        367 to 703 and the hit rate 79% to 13%: the first defect this programme
        fixed, reintroduced by a flag that has nothing to do with the tool.
        """

        context = DecisionContextContractTests.v2_context()
        state = {"game_type": "future_game", "_decision_context": context}

        with mock.patch.dict(
            llm_agent.os.environ,
            {llm_agent._GAMEPLAY_CACHE_TOOL_MODE_ENV: "stable"},
        ):
            with mock.patch.object(llm_agent, "_managed_gateway_selected", return_value=True):
                for profile in ("stateless", "session"):
                    context["profile"] = profile
                    self.assertEqual(
                        llm_agent._cache_tool_mode_for_turn(
                            "https://gw.example/v1", state, context_mode="session",
                        ),
                        "stable",
                        f"byte-stable tool disabled under profile={profile}",
                    )

                # bootstrap belongs in too, and excluding it was the same
                # defect one profile further along: the client asks for
                # bootstrap on the first turn of a match and after any resync --
                # the turns that SEED the cache -- and the server answers it
                # with a freshly projected, complete board
                # (polling_decision_context.py: profile in {stateless,
                # bootstrap} -> state_mode "full").
                context["profile"] = "bootstrap"
                self.assertEqual(
                    llm_agent._cache_tool_mode_for_turn(
                        "https://gw.example/v1", state, context_mode="session",
                    ),
                    "stable",
                )

                # What actually disqualifies a turn is an INCOMPLETE board, and
                # that is read from state_mode, not from the profile name.
                context["profile"] = "session"
                context["turn"]["state_mode"] = "delta"
                self.assertIsNone(
                    llm_agent._cache_tool_mode_for_turn(
                        "https://gw.example/v1", state, context_mode="session",
                    )
                )

    def test_no_window_ships_my_memory_even_when_the_server_sends_one(self):
        """One check across all four windows, because it came back once already.

        my_memory was "removed" before by dropping it from an explicit payload
        key -- and it simply moved into the board instead, because the board
        builders keep every key they do not recognise. So this does not assert
        on a field name in one place; it feeds a large, distinctive memory in
        through the server state and asserts none of it reaches any prompt.
        """

        bait = {
            "my_recent_moves": [{"note": f"BAIT-{i}"} for i in range(40)],
            "my_private_reads": ["SECRET-READ"],
        }
        state = {"game_type": "mafia", "phase": "discuss", "chat_log": [],
                 "my_memory": bait}
        legal = [{"action": "chat", "params": {"message": "string"}}]

        def leaked(text):
            return [
                needle for needle in ("BAIT-", "SECRET-READ", "my_memory")
                if needle in text
            ]

        windows = {}
        windows["kit bounded"] = "".join(
            message["content"]
            for message in llm_agent._bounded_structured_messages(state, legal)
        )
        llm_agent._reset_session()
        try:
            with mock.patch.object(
                llm_agent.memory, "current_match_id", return_value=7,
            ):
                messages, _ = llm_agent._prepare_conversation(state, legal)
            windows["kit session"] = "".join(m["content"] for m in messages)
        finally:
            llm_agent._reset_session()
        windows["hermes stateless"] = hermes_agent._bounded_gameplay_prompt(
            state, legal,
        )
        old_last = dict(hermes_agent._LAST)
        try:
            hermes_agent._LAST.clear()
            hermes_agent._LAST.update(
                sid=None, board=None, turn_count=0,
                last_full_turn=0, full_failures=0,
            )
            windows["hermes resumed"] = hermes_agent._build_prompt(
                state, legal, None, hermes_agent._board(state),
            )
        finally:
            hermes_agent._LAST.clear()
            hermes_agent._LAST.update(old_last)

        for name, text in windows.items():
            self.assertEqual(leaked(text), [], f"{name} still ships match memory")

    def test_carry_forward_call_fits_its_own_token_budget(self):
        """The note's deadline has to fit the tokens it is allowed to write.

        Live failure on diplomacy match 1427: the summarizer ran 31.1s against a
        30s ceiling, so the client hung up 1.1 seconds after the model had
        finished writing and the provider had already billed 3,458 output
        tokens. The note was discarded and the compaction proceeded without one.
        The two constants had drifted apart -- the token cap was raised from 900
        to 8000 to stop truncation, and the timeout stayed where it was.
        """

        rate = 111  # output tokens/second, measured on that call
        self.assertGreaterEqual(
            llm_agent._CARRY_FORWARD_TIMEOUT,
            llm_agent._CARRY_FORWARD_MAX_TOKENS / rate,
            "the note can be cut off by its own deadline before it is written",
        )

    def test_carry_forward_does_not_pay_for_hidden_reasoning(self):
        """Folding a transcript into a note is extraction, not deliberation.

        On the same call, 2,982 of 3,458 output tokens were hidden reasoning --
        86% of the spend, and the reason it ran past its deadline -- to produce
        roughly 476 tokens of summary. An explicit builder setting still wins;
        this only moves the default for the kit's own bookkeeping call.
        """

        seen = {}

        def capture(base, key, model, messages, **kwargs):
            seen.update(kwargs)
            raise RuntimeError("stop here; the request shape is the assertion")

        with mock.patch.object(llm_agent, "_chat_request", side_effect=capture):
            note, error = llm_agent._summarize_carry_forward(
                "https://gw.example/v1", "k", "deepseek/deepseek-v4-flash",
                [{"role": "user", "content": "FRANCE broke the Burgundy pact"}],
                None, budget=200,
            )
        self.assertIs(seen.get("deliberate"), False)
        self.assertIsNone(note)
        # The cause is named, not swallowed into one bucket: a provider outage
        # and a client-side deadline need different fixes.
        self.assertEqual(error, "call_failed:RuntimeError")

    def test_carry_forward_note_does_not_cross_a_match_boundary(self):
        """One match's note must not open the next match.

        The note is what a compaction folded the transcript into: who betrayed
        whom, what was promised. The session holds it until a new match resets
        the window -- and the first baseline of that new match inherits whatever
        the session still holds. Carrying it across would open a fresh game with
        another game's opponents and commitments stated as established fact.
        """

        note = {"opponents": {"FRANCE": "broke the Burgundy pact"},
                "commitments": ["hold Munich for AUSTRIA"]}
        llm_agent._reset_session()
        try:
            with llm_agent._SESSION_LOCK:
                llm_agent._SESSION.update(
                    match_id=41, carry_forward=dict(note),
                    messages=[{"role": "system", "content": "s"}],
                    context={}, state={}, memory={}, turn_count=6,
                )
            with mock.patch.object(
                llm_agent.memory, "current_match_id", return_value=42,
            ):
                messages, pending = llm_agent._prepare_conversation(
                    self.state(), self.legal(),
                )
            body = messages[-1]["content"]
            self.assertIn("STATE_BASELINE:", body)
            self.assertNotIn("Burgundy", body)
            self.assertNotIn("carry_forward", body)
            self.assertIsNone(pending.get("carry_forward"))
        finally:
            llm_agent._reset_session()

    def test_carry_forward_note_rides_the_compacted_baseline_and_holds_still(self):
        """The note is written at a compaction and then must not move.

        It sits in the baseline, which is rebuilt only at a compaction, so
        between compactions these bytes are identical. Regenerating it per turn
        would break the prefix every turn and undo the whole cache result.
        """

        note = {"opponents": {"FRANCE": "broke the Burgundy pact"},
                "commitments": ["hold Belgium for ITALY"]}

        with mock.patch.object(llm_agent.memory, "current_match_id", return_value=411):
            first, pending = llm_agent._prepare_conversation(
                self.state(), self.legal(), carry_forward=note,
            )
            llm_agent._commit_conversation(pending, '{"action":"chat","params":{}}')
            second, second_pending = llm_agent._prepare_conversation(
                self.state(phase="later"), self.legal(),
            )

        baseline = json.loads(first[1]["content"].split("STATE_BASELINE:\n", 1)[1])
        self.assertEqual(baseline["carry_forward"], note)
        # The note is carried on the session, so the next full rebuild reuses the
        # same bytes rather than dropping or regenerating it.
        self.assertEqual(second_pending["mode"], "delta")
        self.assertEqual(llm_agent._SESSION["carry_forward"], note)
        self.assertNotIn("carry_forward", second[-1]["content"])

    def test_carry_forward_uses_a_distinct_update_prompt_when_folding(self):
        """Creating a note and folding into one are different instructions.

        Ported from OpenClaw's compaction, which keeps a create prompt and a
        separate update prompt enumerating what must be preserved and what may be
        dropped. A single "fold these together" leaves that to the model's
        discretion, and a rule for what survives is the thing a fold most needs.
        """

        seen = []

        def capture(base, key, model, messages, **kwargs):
            seen.append(messages)
            return {"text": '{"plan":"hold"}', "finish_reason": "stop"}

        with mock.patch.object(llm_agent, "_chat_request", side_effect=capture):
            llm_agent._summarize_carry_forward(
                "http://x", "k", "m",
                [{"role": "user", "content": "turn one"}], None, budget=99,
            )
            llm_agent._summarize_carry_forward(
                "http://x", "k", "m",
                [{"role": "user", "content": "turn two"}], {"plan": "hold"}, budget=99,
            )

        created, folded = seen[0][-1]["content"], seen[1][-1]["content"]
        self.assertIn("Reply with ONLY this JSON object", created)
        self.assertNotIn("EXISTING NOTE", created)
        self.assertIn("EXISTING NOTE", folded)
        self.assertIn("PRESERVE", folded)
        self.assertIn("anything you omit is forgotten", folded)

    def test_carry_forward_budget_leaves_room_for_visible_output(self):
        """The cap has to survive forced hidden reasoning, not just fit the note.

        The managed gateway forces thinking on, and this call reasons over the
        largest transcript of the match. At 900 tokens every call on TEST came
        back finish_reason=length with reasoning_tokens=900 and no content.
        """

        self.assertGreaterEqual(llm_agent._CARRY_FORWARD_MAX_TOKENS, 3000)

    def test_carry_forward_note_is_clamped(self):
        note = llm_agent._bounded_carry_forward({
            "opponents": {f"P{n}": "x" * 400 for n in range(20)},
            "commitments": ["c" * 400] * 20,
            "lessons": ["l" * 400] * 20,
            "plan": "p" * 2000,
            "ignored": "dropped",
        })

        self.assertNotIn("ignored", note)
        self.assertLessEqual(len(note.get("commitments", [])), 6)
        self.assertLessEqual(len(note["plan"]), 400)
        self.assertLessEqual(
            len(llm_agent._ordered_json(note).encode("utf-8")), 2048
        )

    def test_carry_forward_never_costs_the_turn(self):
        """Every failure mode returns the previous note and lets play continue."""

        previous = {"plan": "hold the line"}

        # Not enough budget left: do not even try.
        kept, why = llm_agent._summarize_carry_forward(
            "http://x", "k", "m", [{"role": "user", "content": "hi"}], previous, budget=1,
        )
        self.assertEqual(kept, previous)
        self.assertEqual(why, "insufficient_budget")

        # Provider error.
        with mock.patch.object(
            llm_agent, "_chat_request", side_effect=RuntimeError("provider down"),
        ):
            kept, why = llm_agent._summarize_carry_forward(
                "http://x", "k", "m",
                [{"role": "user", "content": "hi"}], previous, budget=99,
            )
        self.assertEqual(kept, previous)
        # The exception TYPE rides along: a provider outage and a client-side
        # deadline both landed in "call_failed", and they need different fixes.
        self.assertTrue(why.startswith("call_failed:"), why)

        # Truncated before any visible byte: a budget problem, reported as its
        # own thing. Lumped in with a malformed reply it shipped billing for
        # every compaction while producing nothing and looking healthy.
        with mock.patch.object(
            llm_agent,
            "_chat_request",
            return_value={"text": "", "finish_reason": "length"},
        ):
            kept, why = llm_agent._summarize_carry_forward(
                "http://x", "k", "m",
                [{"role": "user", "content": "hi"}], previous, budget=99,
            )
        self.assertEqual(kept, previous)
        self.assertEqual(why, "truncated")

        # Reply that is not a usable note.
        with mock.patch.object(llm_agent, "_chat_request", return_value="not json"):
            kept, why = llm_agent._summarize_carry_forward(
                "http://x", "k", "m",
                [{"role": "user", "content": "hi"}], previous, budget=99,
            )
        self.assertEqual(kept, previous)
        self.assertEqual(why, "unusable_reply")

    def test_session_growth_is_bounded_without_provider_metadata(self):
        """A session must bound itself on economics, not on capacity.

        The managed route advertises a 1,000,000-token window, so window-based
        compaction never fires in practice. Measured live on diplomacy that left
        the prompt growing to 342k tokens and gave the session's entire cost
        advantage back: -30% around turn 10, zero by turn 31.
        """

        with (
            mock.patch.object(llm_agent.memory, "current_match_id", return_value=77),
            mock.patch.dict(
                llm_agent.os.environ,
                {
                    llm_agent._SESSION_GROWTH_MULTIPLE_ENV: "3",
                    # Exercise the baseline-multiple fallback specifically.
                    llm_agent._SESSION_COMPACT_AT_ENV: "0",
                },
            ),
        ):
            modes = []
            reported = []
            for turn in range(12):
                _messages, pending = llm_agent._prepare_conversation(
                    self.state(phase=f"turn-{turn}"),
                    self.legal(),
                    context_window=0,          # the provider told us nothing
                )
                # Report a prompt that keeps growing, as an accumulating
                # transcript does, so the bound has something to bite on.
                pending["prompt_tokens"] = 1000 * (turn + 1)
                llm_agent._commit_conversation(
                    pending,
                    '{"action":"chat","params":{"message":"ok"}}',
                )
                modes.append(pending["mode"])
                reported.append(pending["prompt_tokens"])
            final_baseline = llm_agent._SESSION["baseline_prompt_tokens"]

        self.assertEqual(modes[0], "full")
        self.assertIn("compacted", modes[1:], f"session never compacted: {modes}")
        # The bound keeps biting: each rebuild re-takes the baseline, so the
        # threshold rises with the game instead of pinning to turn one forever.
        last_rebuild = max(i for i, mode in enumerate(modes) if mode != "delta")
        self.assertGreater(last_rebuild, 0, modes)
        self.assertEqual(final_baseline, reported[last_rebuild])

    def test_absolute_compaction_boundary_is_the_primary_control(self):
        """200k means 200k, not "whichever of two bounds fires first".

        Taking the smaller of the absolute boundary and the baseline multiple
        would let a multiple of a small baseline fire long before the boundary
        anyone configured, and the configured number would never be reached.
        """

        with (
            mock.patch.object(llm_agent.memory, "current_match_id", return_value=511),
            mock.patch.dict(
                llm_agent.os.environ,
                {llm_agent._SESSION_COMPACT_AT_ENV: "40000"},
            ),
        ):
            modes = []
            for turn in range(6):
                _messages, pending = llm_agent._prepare_conversation(
                    self.state(phase=f"turn-{turn}"), self.legal(), context_window=0,
                )
                # A baseline multiple would have fired several turns before this.
                pending["prompt_tokens"] = 9000 * (turn + 1)
                llm_agent._commit_conversation(
                    pending, '{"action":"chat","params":{"message":"ok"}}',
                )
                modes.append(pending["mode"])

        self.assertEqual(modes[0], "full")
        self.assertNotIn("compacted", modes[:4], modes)
        self.assertIn("compacted", modes[4:], modes)

    def test_absolute_boundary_rejects_a_value_that_would_fire_every_turn(self):
        with mock.patch.dict(
            llm_agent.os.environ, {llm_agent._SESSION_COMPACT_AT_ENV: "500"},
        ):
            self.assertEqual(llm_agent._session_compact_at_tokens(), 0)
        with mock.patch.dict(
            llm_agent.os.environ, {llm_agent._SESSION_COMPACT_AT_ENV: "nonsense"},
        ):
            self.assertEqual(
                llm_agent._session_compact_at_tokens(),
                llm_agent._SESSION_COMPACT_AT_DEFAULT,
            )

    def test_session_growth_bound_is_inert_without_a_baseline(self):
        self.assertEqual(llm_agent._session_growth_threshold(0), 0)
        with mock.patch.dict(
            llm_agent.os.environ,
            {llm_agent._SESSION_GROWTH_MULTIPLE_ENV: "not-a-number"},
        ):
            self.assertEqual(
                llm_agent._session_growth_threshold(1000),
                int(1000 * llm_agent._SESSION_GROWTH_MULTIPLE_DEFAULT),
            )

    def test_turn_count_alone_never_resets_the_transcript(self):
        with mock.patch.object(llm_agent.memory, "current_match_id", return_value=41):
            for turn in range(30):
                messages, pending = llm_agent._prepare_conversation(
                    self.state(phase=f"turn-{turn}"),
                    self.legal(),
                )
                llm_agent._commit_conversation(
                    pending,
                    '{"action":"chat","params":{"message":"ok"}}',
                )

        self.assertEqual(pending["mode"], "delta")
        self.assertGreater(len(messages), 12)
        self.assertEqual(llm_agent._SESSION["turn_count"], 30)

    def test_provider_context_overflow_retries_once_with_full_baseline(self):
        calls = []

        def fake_chat_request(_base, _key, _model, messages, **_kwargs):
            calls.append(messages)
            if len(calls) == 1:
                raise llm_agent.ContextOverflowError("context length exceeded")
            return {
                "text": '{"action":"chat","params":{"message":"recovered"}}',
                "prompt_tokens": 2400,
                "finish_reason": "stop",
            }

        with (
            mock.patch.dict(
                os.environ,
                {"CLAWARENA_GAMEPLAY_CONTEXT_MODE": "session"},
            ),
            mock.patch.object(llm_agent.memory, "current_match_id", return_value=41),
        ):
            _, pending = llm_agent._prepare_conversation(self.state(), self.legal())
            llm_agent._commit_conversation(pending, '{"action":"chat","params":{"message":"first"}}')
            with (
                mock.patch.object(llm_agent, "_model_context_window", return_value=0),
                mock.patch.object(llm_agent, "_chat_request", side_effect=fake_chat_request),
            ):
                reply, recovered = llm_agent._chat(
                    "https://llm.example/v1",
                    "key",
                    "model",
                    self.state(phase="vote"),
                    self.legal(),
                )

        self.assertIn("recovered", reply)
        self.assertIn("TURN_UPDATE:\n", calls[0][-1]["content"])
        self.assertEqual([message["role"] for message in calls[1]], ["system", "user"])
        self.assertIn("STATE_BASELINE:\n", calls[1][1]["content"])
        self.assertEqual(recovered["mode"], "overflow_recovery")
        self.assertEqual(recovered["prompt_tokens"], 2400)

    def test_gameplay_context_mode_defaults_bounded_and_requires_explicit_session(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(llm_agent._gameplay_context_mode(), "bounded")
        with mock.patch.dict(
            os.environ,
            {"CLAWARENA_GAMEPLAY_CONTEXT_MODE": "session"},
            clear=True,
        ):
            self.assertEqual(llm_agent._gameplay_context_mode(), "session")
        with mock.patch.dict(
            os.environ,
            {"CLAWARENA_GAMEPLAY_CONTEXT_MODE": "unexpected"},
            clear=True,
        ):
            self.assertEqual(llm_agent._gameplay_context_mode(), "bounded")

    def test_gameplay_streaming_requires_explicit_opt_in_for_every_endpoint(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(
                llm_agent._gameplay_streaming("https://arena.example/api/llm/v1")
            )
            self.assertFalse(
                llm_agent._gameplay_streaming("https://builder-provider.example/v1")
            )
        with mock.patch.dict(os.environ, {"LLM_STREAMING": "1"}, clear=True):
            self.assertTrue(
                llm_agent._gameplay_streaming("https://builder-provider.example/v1")
            )
        with mock.patch.dict(os.environ, {"LLM_STREAMING": "false"}, clear=True):
            self.assertFalse(
                llm_agent._gameplay_streaming("https://arena.example/api/llm/v1")
            )

    def test_arbitrary_byo_provider_uses_the_same_bounded_harness_as_hosting(self):
        context = DecisionContextContractTests.v2_context()
        state = {
            "game_type": "future_game",
            "my_memory": {"my_recent_moves": []},
            "_decision_context": context,
            "_decision_budget_seconds": 179,
        }
        captured = {}

        def fake_chat_request(_base, _key, _model, messages, **kwargs):
            captured["messages"] = messages
            captured["kwargs"] = kwargs
            return {
                "text": '{"action":"choose","params":{}}',
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "finish_reason": "stop",
            }

        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(llm_agent.memory, "current_match_id", return_value=77),
            mock.patch.object(llm_agent, "_chat_request", side_effect=fake_chat_request),
        ):
            _reply, pending = llm_agent._chat(
                "https://any-byo-provider.example/v1",
                "key",
                "provider-model",
                state,
                context["turn"]["legal_actions"],
            )

        self.assertEqual(pending["mode"], "bounded_structured")
        self.assertEqual(captured["messages"][0]["content"], llm_agent.GAMEPLAY_SYSTEM_SCAFFOLD)
        payload = json.loads(captured["messages"][1]["content"])
        self.assertEqual(payload["turn"]["state"], {"new_resource": 7})
        self.assertEqual(payload["stable"]["rules"], {"rules": ["server only"]})
        self.assertGreater(captured["kwargs"]["timeout"], 165)
        self.assertLessEqual(captured["kwargs"]["timeout"], 179)
        self.assertIsNone(captured["kwargs"]["max_tokens"])
        self.assertFalse(captured["kwargs"]["streaming"])
        self.assertIsNone(captured["kwargs"]["tools"])

    def test_direct_byo_provider_omits_output_cap_unless_configured(self):
        calls = []

        def fake_chat_request(_base, _key, _model, _messages, **kwargs):
            calls.append(kwargs["max_tokens"])
            return {
                "text": '{"action":"chat","params":{}}',
                "prompt_tokens": 10,
                "finish_reason": "stop",
            }

        diplomacy = self.state()
        diplomacy["game_type"] = "diplomacy"
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(llm_agent.memory, "current_match_id", return_value=41),
            mock.patch.object(llm_agent, "_model_context_window", return_value=0),
            mock.patch.object(llm_agent, "_chat_request", side_effect=fake_chat_request),
        ):
            _, diplomacy_pending = llm_agent._chat(
                "https://llm.example/v1",
                "key",
                "model",
                diplomacy,
                self.legal(),
            )
            llm_agent._reset_session()
            _, mafia_pending = llm_agent._chat(
                "https://llm.example/v1",
                "key",
                "model",
                self.state(),
                self.legal(),
            )

        self.assertEqual(calls, [None, None])
        self.assertIsNone(diplomacy_pending["max_completion_tokens"])
        self.assertIsNone(mafia_pending["max_completion_tokens"])

    def test_direct_byo_provider_honors_builder_output_caps(self):
        diplomacy = self.state()
        diplomacy["game_type"] = "diplomacy"
        with mock.patch.dict(
            os.environ,
            {
                "LLM_MAX_TOKENS": "7000",
                "LLM_DIPLOMACY_MAX_TOKENS": "9000",
            },
            clear=True,
        ):
            self.assertEqual(
                llm_agent._decision_max_tokens(
                    diplomacy,
                    "https://llm.example/v1",
                ),
                9000,
            )
            self.assertEqual(
                llm_agent._decision_max_tokens(
                    self.state(),
                    "https://llm.example/v1",
                ),
                7000,
            )

    def test_monotone_layout_preserves_content_and_moves_the_cut(self):
        """Same bytes, later cut. The layout may regroup but never drop."""

        context = DecisionContextContractTests.v2_context()
        context["turn"]["decision_support"] = {
            "recommended_action": {"action": "choose", "params": {}},
        }
        context["turn"]["action_rejection"] = {"reason": "stale"}
        context["stable"]["id"] = decision_context.stable_context_id(context)
        state = {"game_type": context["turn"]["game_type"],
                 "_decision_context": context, "my_memory": {}}

        def build(environment):
            with mock.patch.dict(os.environ, environment, clear=True):
                messages = llm_agent._bounded_structured_messages(
                    state,
                    context["turn"]["legal_actions"],
                    system_prompt=llm_agent.GAMEPLAY_SYSTEM_SCAFFOLD,
                )
            return messages[0]["content"], json.loads(messages[1]["content"])

        _, default_payload = build({})
        system, monotone_payload = build(
            {"CLAWARENA_GAMEPLAY_CACHE_LAYOUT": "monotone"},
        )

        # 1. Content preserved: every leaf that was reachable still is.
        def leaves(node, path=""):
            if isinstance(node, dict):
                out = {}
                for key, value in node.items():
                    out.update(leaves(value, f"{path}.{key}"))
                return out
            return {path.split(".")[-1]: json.dumps(node, sort_keys=True)}

        self.assertEqual(leaves(default_payload), leaves(monotone_payload))

        # 2. Volatile members left turn for the trailing window block.
        self.assertIn("window", monotone_payload)
        for key in ("action_window_id", "seq", "decision_support", "action_rejection"):
            self.assertIn(key, monotone_payload["window"])
            self.assertNotIn(key, monotone_payload["turn"])

        # 3. Top-level order is least-volatile first, window last.
        keys = list(monotone_payload)
        self.assertEqual(keys[:3], ["version", "profile", "stable"])
        self.assertEqual(keys[-1], "window")
        self.assertLess(keys.index("stable"), keys.index("turn"))

        # 4. The scaffold's paths still resolve against the payload it describes.
        self.assertIn("window.decision_support", system)
        self.assertIn("window.action_rejection", system)
        self.assertNotIn("turn.decision_support", system)
        self.assertNotIn("turn.action_rejection", system)

    def test_monotone_layout_fails_closed_and_rewrites_every_named_path(self):
        for configured in ("", "montone", "*", "monotone,default"):
            with self.subTest(configured=configured), mock.patch.dict(
                os.environ,
                {"CLAWARENA_GAMEPLAY_CACHE_LAYOUT": configured},
                clear=True,
            ):
                self.assertEqual(llm_agent._gameplay_cache_layout(), "default")
        with mock.patch.dict(
            os.environ,
            {"CLAWARENA_GAMEPLAY_CACHE_LAYOUT": " Monotone "},
            clear=True,
        ):
            self.assertEqual(llm_agent._gameplay_cache_layout(), "monotone")

        # A scaffold edit that names a hoisted key without adding it to the
        # rewrite table would leave the model reading a path that is gone.
        named = set(re.findall(r"turn\.([a-z_]+)", llm_agent.GAMEPLAY_SYSTEM_SCAFFOLD))
        rewritten = {
            old.split(".", 1)[1]
            for old, _new in llm_agent._MONOTONE_SCAFFOLD_PATHS
        }
        self.assertEqual(
            named & set(llm_agent._TURN_VOLATILE_KEYS) - rewritten,
            set(),
        )
        rewritten_prompt = llm_agent._monotone_system_prompt(
            llm_agent.GAMEPLAY_SYSTEM_SCAFFOLD,
        )
        self.assertEqual(
            set(re.findall(r"turn\.([a-z_]+)", rewritten_prompt))
            & set(llm_agent._TURN_VOLATILE_KEYS),
            set(),
        )

    def test_cache_tool_mode_fails_closed_on_unrecognized_values(self):
        for configured, expected in (
            (" Stable ", "stable"),
            ("off", "off"),
            ("menu", "menu"),
            ("dynamic", "dynamic"),
            ("", ""),
            ("*", ""),
            ("staple", ""),
            ("stable,menu", ""),
        ):
            with self.subTest(configured=configured), mock.patch.dict(
                os.environ,
                {"CLAWARENA_GAMEPLAY_CACHE_TOOL_MODE": configured},
                clear=True,
            ):
                self.assertEqual(llm_agent._gameplay_cache_tool_mode(), expected)

    def test_cache_tool_scope_accepts_any_game_and_fails_closed_on_typos(self):
        """Scope is data, so a game shipped tomorrow needs no client change."""

        with mock.patch.dict(
            os.environ,
            {"CLAWARENA_GAMEPLAY_CACHE_TOOL_GAMES": " Mafia , future_game "},
            clear=True,
        ):
            self.assertEqual(
                llm_agent._gameplay_cache_tool_games(),
                {"mafia", "future_game"},
            )
        # Unset means every game.
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(llm_agent._gameplay_cache_tool_games(), frozenset())
        # A malformed list must not enable its parseable entries.
        for configured in ("mafia,", ",mafia", "mafia,,diplomacy"):
            with self.subTest(configured=configured), mock.patch.dict(
                os.environ,
                {"CLAWARENA_GAMEPLAY_CACHE_TOOL_GAMES": configured},
                clear=True,
            ):
                scope = llm_agent._gameplay_cache_tool_games()
                self.assertNotIn("mafia", scope)
                self.assertTrue(scope)

    def test_cache_tool_mode_requires_canonical_identity_for_any_game(self):
        context = DecisionContextContractTests.v2_context()
        context["stable"]["game_type"] = "mafia"
        context["turn"]["game_type"] = "mafia"
        context["stable"]["id"] = decision_context.stable_context_id(context)
        state = {"game_type": "mafia", "_decision_context": context}
        base = "https://arena.example/api/llm/v1"
        environment = {
            "CLAWARENA_GATEWAY_KEY": "gateway-key",
            "CLAWARENA_GAMEPLAY_CACHE_TOOL_MODE": "stable",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            # No game allowlist: mafia is eligible without any code change.
            self.assertEqual(
                llm_agent._cache_tool_mode_for_turn(
                    base,
                    state,
                    context_mode="bounded",
                ),
                "stable",
            )
            for changed_state in (
                {"_decision_context": context},
                {"game_type": "diplomacy", "_decision_context": context},
                {"game_type": " MAFIA ", "_decision_context": context},
                {"game_type": "mafia", "_decision_context": {
                    **context,
                    "stable": {
                        **context["stable"],
                        "game_type": " MAFIA ",
                    },
                }},
                {"game_type": "mafia", "_decision_context": {
                    **context,
                    "version": 1,
                }},
            ):
                with self.subTest(state=changed_state):
                    self.assertIsNone(
                        llm_agent._cache_tool_mode_for_turn(
                            base,
                            changed_state,
                            context_mode="bounded",
                        )
                    )
            self.assertIsNone(
                llm_agent._cache_tool_mode_for_turn(
                    "https://byo.example/v1",
                    state,
                    context_mode="bounded",
                )
            )
            # Session mode is eligible too. The tool renders BEFORE the first
            # user message, so a churning tool breaks the prefix before any
            # message is reached -- which is exactly what made an accumulating
            # session cache nothing in a live Diplomacy match.
            self.assertEqual(
                llm_agent._cache_tool_mode_for_turn(
                    base,
                    state,
                    context_mode="session",
                ),
                "stable",
            )
            # _gameplay_context_mode() only ever yields bounded or session, so
            # the guard against any other transport is defensive, not reachable
            # from configuration -- assert it directly rather than through env.
            self.assertIsNone(
                llm_agent._cache_tool_mode_for_turn(
                    base,
                    state,
                    context_mode="some_future_transport",
                )
            )
        # Scoped to another game -> this game keeps the default.
        with mock.patch.dict(
            os.environ,
            {**environment, "CLAWARENA_GAMEPLAY_CACHE_TOOL_GAMES": "diplomacy"},
            clear=True,
        ):
            self.assertIsNone(
                llm_agent._cache_tool_mode_for_turn(
                    base,
                    state,
                    context_mode="bounded",
                )
            )
        # No mode configured -> untouched, whatever the scope says.
        with mock.patch.dict(
            os.environ,
            {
                "CLAWARENA_GATEWAY_KEY": "gateway-key",
                "CLAWARENA_GAMEPLAY_CACHE_TOOL_GAMES": "mafia",
            },
            clear=True,
        ):
            self.assertIsNone(
                llm_agent._cache_tool_mode_for_turn(
                    base,
                    state,
                    context_mode="bounded",
                )
            )

    def test_cache_tool_mode_stable_applies_on_the_managed_bounded_path(self):
        context = DecisionContextContractTests.v2_context()
        context["stable"]["game_type"] = "las_vegas"
        context["turn"]["game_type"] = "las_vegas"
        context["turn"]["legal_actions"] = [{"action": "place", "params": {}}]
        context["stable"]["id"] = decision_context.stable_context_id(context)
        state = {
            "game_type": "las_vegas",
            "_decision_context": context,
            "my_memory": {},
        }
        captured = {}

        def fake_chat_request(_base, _key, _model, _messages, **kwargs):
            captured["tools"] = kwargs["tools"]
            return {"text": '{"action":"place","params":{}}'}

        with (
            mock.patch.dict(
                os.environ,
                {
                    "CLAWARENA_GATEWAY_KEY": "gateway-key",
                    "CLAWARENA_GAMEPLAY_CACHE_TOOL_MODE": "stable",
                },
                clear=True,
            ),
            mock.patch.object(llm_agent.memory, "current_match_id", return_value=77),
            mock.patch.object(
                llm_agent,
                "_chat_request",
                side_effect=fake_chat_request,
            ),
        ):
            llm_agent._chat(
                "https://arena.example/api/llm/v1",
                "key",
                "model",
                state,
                context["turn"]["legal_actions"],
            )

        parameters = captured["tools"][0]["function"]["parameters"]
        self.assertEqual(parameters["properties"]["action"], {"type": "string"})
        self.assertNotIn("oneOf", parameters)

    def test_cache_tool_mode_invalid_value_falls_back_to_dynamic_schema(self):
        context = DecisionContextContractTests.v2_context()
        context["stable"]["game_type"] = "las_vegas"
        context["turn"]["game_type"] = "las_vegas"
        context["turn"]["legal_actions"] = [{"action": "place", "params": {}}]
        context["stable"]["id"] = decision_context.stable_context_id(context)
        captured = []

        def fake_chat_request(_base, _key, _model, _messages, **kwargs):
            captured.append(kwargs["tools"])
            return {"text": '{"action":"place","params":{}}'}

        for state, environment in (
            (
                {"game_type": "las_vegas", "_decision_context": context},
                {
                    "CLAWARENA_GATEWAY_KEY": "gateway-key",
                    # Unrecognized mode -> fail closed to the dynamic schema.
                    "CLAWARENA_GAMEPLAY_CACHE_TOOL_MODE": "staple",
                },
            ),
            (
                {"game_type": "mafia", "_decision_context": context},
                {
                    "CLAWARENA_GATEWAY_KEY": "gateway-key",
                    "CLAWARENA_GAMEPLAY_CACHE_TOOL_MODE": "stable",
                },
            ),
            (
                {"game_type": "las_vegas", "_decision_context": context},
                {
                    "LLM_DECISION_TOOL": "true",
                    "CLAWARENA_GAMEPLAY_CACHE_TOOL_MODE": "stable",
                },
            ),
        ):
            with (
                self.subTest(environment=environment, state=state),
                mock.patch.dict(os.environ, environment, clear=True),
                mock.patch.object(
                    llm_agent.memory,
                    "current_match_id",
                    return_value=77,
                ),
                mock.patch.object(
                    llm_agent,
                    "_chat_request",
                    side_effect=fake_chat_request,
                ),
            ):
                llm_agent._chat(
                    "https://arena.example/api/llm/v1"
                    if "CLAWARENA_GATEWAY_KEY" in environment
                    else "https://byo.example/v1",
                    "key",
                    "model",
                    state,
                    context["turn"]["legal_actions"],
                )

        self.assertEqual(len(captured), 3)
        for tools in captured:
            self.assertIn("oneOf", tools[0]["function"]["parameters"])

    def test_cache_tool_mode_does_not_change_session_tool_selection(self):
        context = DecisionContextContractTests.v2_context(state_mode="delta")
        context["profile"] = "session"
        context["stable"]["game_type"] = "las_vegas"
        context["turn"]["game_type"] = "las_vegas"
        context["turn"]["legal_actions"] = [{"action": "place", "params": {}}]
        context["stable"]["id"] = decision_context.stable_context_id(context)
        captured = {}

        def fake_chat_request(_base, _key, _model, _messages, **kwargs):
            captured["tools"] = kwargs["tools"]
            return {"text": '{"action":"place","params":{}}'}

        with (
            mock.patch.dict(
                os.environ,
                {
                    "CLAWARENA_GAMEPLAY_CONTEXT_MODE": "session",
                    "CLAWARENA_GATEWAY_KEY": "gateway-key",
                    "CLAWARENA_GAMEPLAY_CACHE_TOOL_MODE": "stable",
                },
                clear=True,
            ),
            mock.patch.object(llm_agent.memory, "current_match_id", return_value=77),
            mock.patch.object(
                llm_agent,
                "_chat_request",
                side_effect=fake_chat_request,
            ),
        ):
            llm_agent._chat(
                "https://arena.example/api/llm/v1",
                "key",
                "model",
                {"game_type": "las_vegas", "_decision_context": context},
                context["turn"]["legal_actions"],
            )

        self.assertIn("oneOf", captured["tools"][0]["function"]["parameters"])

    def test_cache_tool_mode_forces_dynamic_on_ineligible_legacy_modes(self):
        context = DecisionContextContractTests.v2_context()
        context["stable"]["game_type"] = "mafia"
        context["turn"]["game_type"] = "mafia"
        context["stable"]["id"] = decision_context.stable_context_id(context)
        captured = []

        def fake_chat_request(_base, _key, _model, _messages, **kwargs):
            captured.append(kwargs["tools"])
            return {"text": '{"action":"choose","params":{}}'}

        # Windows the eligibility gate REJECTS. A configured override must pin
        # "dynamic" for these, so the legacy LLM_DECISION_TOOL_SCHEMA_MODE
        # cannot silently take over the very windows that were just rejected.
        # (An eligible mafia window is covered by
        # test_cache_tool_mode_requires_canonical_identity_for_any_game --
        # under the universal mechanism mafia is eligible, unlike before.)
        cases = (
            (
                {"game_type": "las_vegas", "_decision_context": {"version": 2}},
                {},
            ),
            (
                # Eligible transport, but scoped to a different game.
                {"game_type": "mafia", "_decision_context": context},
                {"CLAWARENA_GAMEPLAY_CACHE_TOOL_GAMES": "diplomacy"},
            ),
        )
        for state, extra_environment in cases:
            for legacy_mode in ("stable", "menu"):
                environment = {
                    "CLAWARENA_GATEWAY_KEY": "gateway-key",
                    "CLAWARENA_GAMEPLAY_CACHE_TOOL_MODE": "stable",
                    "LLM_DECISION_TOOL_SCHEMA_MODE": legacy_mode,
                    **extra_environment,
                }
                with (
                    self.subTest(state=state, environment=environment),
                    mock.patch.dict(os.environ, environment, clear=True),
                    mock.patch.object(
                        llm_agent.memory,
                        "current_match_id",
                        return_value=77,
                    ),
                    mock.patch.object(
                        llm_agent,
                        "_chat_request",
                        side_effect=fake_chat_request,
                    ),
                ):
                    llm_agent._chat(
                        "https://arena.example/api/llm/v1",
                        "key",
                        "model",
                        state,
                        context["turn"]["legal_actions"],
                    )

        self.assertEqual(len(captured), len(cases) * 2)
        for tools in captured:
            parameters = tools[0]["function"]["parameters"]
            self.assertIn("oneOf", parameters)

    def test_legacy_schema_mode_is_unchanged_without_static_canary_env(self):
        legal = [{"action": "choose", "params_schema": {"type": "object"}}]
        with mock.patch.dict(
            os.environ,
            {
                "CLAWARENA_GATEWAY_KEY": "gateway-key",
                "LLM_DECISION_TOOL_SCHEMA_MODE": "stable",
            },
            clear=True,
        ):
            tools = llm_agent._decision_tools(
                "https://arena.example/api/llm/v1",
                legal,
            )

        self.assertNotIn("oneOf", tools[0]["function"]["parameters"])

    def test_gateway_deepseek_uses_one_bounded_state_projection(self):
        captured = {}

        def fake_chat_request(_base, _key, _model, messages, **kwargs):
            captured["messages"] = messages
            captured["kwargs"] = kwargs
            return {
                "text": '{"action":"chat","params":{"message":"evidence"}}',
                "prompt_tokens": 300,
                "completion_tokens": 12,
                "reasoning_chars": 0,
                "finish_reason": "stop",
            }

        state = self.state()
        state.update({
            "game_type": "mafia",
            "chat_log": [{"speaker": "A", "message": "current claim"}],
            "unrelated_large_blob": "do-not-send" * 100,
        })
        with (
            mock.patch.dict(
                os.environ,
                {"CLAWARENA_GATEWAY_KEY": "gateway-key"},
                clear=True,
            ),
            mock.patch.object(llm_agent.memory, "current_match_id", return_value=41),
            mock.patch.object(llm_agent, "_model_context_window", return_value=0),
            mock.patch.object(llm_agent, "_chat_request", side_effect=fake_chat_request),
        ):
            _reply, pending = llm_agent._chat(
                "https://arena.example/api/llm/v1",
                "key",
                "deepseek/deepseek-v4-flash",
                state,
                self.legal(),
            )

        self.assertEqual(pending["mode"], "bounded_structured")
        self.assertEqual([row["role"] for row in captured["messages"]], ["system", "user"])
        self.assertEqual(
            captured["messages"][0]["content"],
            llm_agent.GAMEPLAY_SYSTEM_SCAFFOLD,
        )
        self.assertIn("Choose exactly one move", captured["messages"][0]["content"])
        self.assertIn("current claim", captured["messages"][1]["content"])
        self.assertNotIn("do-not-send", captured["messages"][1]["content"])
        self.assertEqual(captured["kwargs"]["max_tokens"], llm_agent.LLM_MAX_TOKENS)
        self.assertFalse(captured["kwargs"]["streaming"])
        self.assertEqual(
            captured["kwargs"]["tools"][0]["function"]["name"],
            "clawarena_decision",
        )
        self.assertEqual(
            captured["kwargs"]["tools"][0]["function"]["parameters"]
            ["properties"]["action"]["enum"],
            ["chat"],
        )

    def test_streaming_chat_request_collects_reasoning_content_and_final_usage(self):
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __iter__(self):
                events = [
                    {"choices": [{"delta": {"reasoning_content": "brief "}}]},
                    {"choices": [{"delta": {"content": '{"action":"chat",'}}]},
                    {
                        "choices": [{
                            "delta": {"content": '"params":{}}'},
                            "finish_reason": "stop",
                        }],
                    },
                    {
                        "choices": [],
                        "usage": {
                            "prompt_tokens": 17,
                            "completion_tokens": 9,
                            "total_tokens": 26,
                        },
                    },
                ]
                for event in events:
                    yield f"data: {json.dumps(event)}\n".encode()
                yield b"data: [DONE]\n"

        def fake_urlopen(request, timeout):
            captured["body"] = json.loads(request.data.decode())
            captured["timeout"] = timeout
            return FakeResponse()

        with mock.patch.object(
            llm_agent.urllib.request,
            "urlopen",
            side_effect=fake_urlopen,
        ):
            result = llm_agent._chat_request(
                "https://arena.example/api/llm/v1",
                "key",
                "deepseek/deepseek-v4-flash",
                [{"role": "user", "content": "choose"}],
                max_tokens=8000,
                streaming=True,
            )

        self.assertTrue(captured["body"]["stream"])
        self.assertEqual(captured["body"]["stream_options"], {"include_usage": True})
        self.assertEqual(result["text"], '{"action":"chat","params":{}}')
        self.assertEqual(result["prompt_tokens"], 17)
        self.assertEqual(result["completion_tokens"], 9)
        self.assertEqual(result["reasoning_chars"], len("brief "))
        self.assertEqual(result["finish_reason"], "stop")

    def test_decision_tool_schema_is_dynamic_and_copies_nested_server_schema(self):
        legal = [
            {
                "action": "future_allocate",
                "params_schema": {
                    "type": "object",
                    "properties": {
                        "orders": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "origin": {"type": "string", "enum": ["X1"]},
                                },
                                "required": ["origin"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["orders"],
                    "additionalProperties": False,
                },
            },
            {
                "action": "future_pass",
                "params_schema": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        ]

        tool = llm_agent._decision_tool_schema(legal)
        parameters = tool["function"]["parameters"]

        self.assertEqual(tool["function"]["name"], "clawarena_decision")
        self.assertEqual(
            parameters["properties"]["action"]["enum"],
            ["future_allocate", "future_pass"],
        )
        self.assertEqual(
            parameters["oneOf"][0]["properties"]["params"],
            legal[0]["params_schema"],
        )
        self.assertIsNot(
            parameters["oneOf"][0]["properties"]["params"],
            legal[0]["params_schema"],
        )
        serialized = json.dumps(tool)
        for hardcoded_game in ("mafia", "diplomacy", "monopoly", "liars_dice"):
            self.assertNotIn(hardcoded_game, serialized.lower())

    def test_decision_tool_schema_bounds_only_redundant_large_params_copy(self):
        compact_schema = {
            "type": "object",
            "properties": {"choice": {"type": "integer", "enum": [1, 2]}},
            "required": ["choice"],
        }
        legal = [
            {
                "action": "future_huge",
                "params_schema": {
                    "type": "object",
                    "properties": {
                        "plan": {
                            "type": "string",
                            "description": "x" * 20_000,
                        },
                    },
                },
            },
            {"action": "future_compact", "params_schema": compact_schema},
        ]

        tool = llm_agent._decision_tool_schema(legal)
        parameters = tool["function"]["parameters"]
        encoded = json.dumps(
            tool,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()

        self.assertLessEqual(
            len(encoded),
            llm_agent._DECISION_TOOL_SCHEMA_MAX_BYTES,
        )
        self.assertEqual(
            parameters["properties"]["action"]["enum"],
            ["future_huge", "future_compact"],
        )
        self.assertEqual(
            parameters["oneOf"][0]["properties"]["params"],
            {"type": "object"},
        )
        self.assertEqual(
            parameters["oneOf"][1]["properties"]["params"],
            compact_schema,
        )

    def test_decision_tool_schema_canonicalizes_maps_but_preserves_lists(self):
        left_schema = {
            "type": "object",
            "properties": {
                "zeta": {"type": "string", "enum": ["z", "a"]},
                "alpha": {"minimum": 1, "type": "integer"},
            },
            "required": ["zeta", "alpha"],
            "additionalProperties": False,
        }
        right_schema = {
            "additionalProperties": False,
            "required": ["zeta", "alpha"],
            "properties": {
                "alpha": {"type": "integer", "minimum": 1},
                "zeta": {"enum": ["z", "a"], "type": "string"},
            },
            "type": "object",
        }

        left_tool = llm_agent._decision_tool_schema([
            {"action": "future", "params_schema": left_schema},
        ])
        right_tool = llm_agent._decision_tool_schema([
            {"action": "future", "params_schema": right_schema},
        ])
        left_params = (
            left_tool["function"]["parameters"]["oneOf"][0]
            ["properties"]["params"]
        )

        self.assertEqual(
            json.dumps(left_tool, ensure_ascii=False, separators=(",", ":")),
            json.dumps(right_tool, ensure_ascii=False, separators=(",", ":")),
        )
        self.assertEqual(left_params["required"], ["zeta", "alpha"])
        self.assertEqual(
            left_params["properties"]["zeta"]["enum"],
            ["z", "a"],
        )
        self.assertIsNot(left_params, left_schema)

    def test_stable_decision_tool_is_identical_across_action_contracts(self):
        left = llm_agent._stable_decision_tool_schema([
            {
                "action": "bid",
                "params_schema": {
                    "type": "object",
                    "properties": {"quantity": {"type": "integer"}},
                },
            },
        ])
        right = llm_agent._stable_decision_tool_schema([
            {
                "action": "submit_orders",
                "params_schema": {
                    "type": "object",
                    "properties": {"orders": {"type": "array"}},
                },
            },
        ])

        self.assertEqual(left, right)
        parameters = left["function"]["parameters"]
        self.assertEqual(parameters["properties"]["action"], {"type": "string"})
        self.assertEqual(parameters["properties"]["params"], {"type": "object"})
        self.assertNotIn("oneOf", parameters)
        self.assertNotIn("enum", json.dumps(parameters))

    def test_menu_decision_tool_keeps_actions_but_omits_params_schemas(self):
        tool = llm_agent._menu_decision_tool_schema([
            {
                "action": "bid",
                "params_schema": {
                    "type": "object",
                    "properties": {"quantity": {"type": "integer"}},
                },
            },
            {
                "action": "challenge",
                "params_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
        ])
        parameters = tool["function"]["parameters"]

        self.assertEqual(
            parameters["properties"]["action"]["enum"],
            ["bid", "challenge"],
        )
        self.assertEqual(parameters["properties"]["params"], {"type": "object"})
        self.assertNotIn("oneOf", parameters)
        self.assertNotIn("quantity", json.dumps(parameters))

    def test_stable_decision_tool_mode_is_managed_gateway_only(self):
        legal = [{
            "action": "future",
            "params_schema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        }]
        with mock.patch.dict(
            os.environ,
            {
                "CLAWARENA_GATEWAY_KEY": "gateway-key",
                "LLM_DECISION_TOOL_SCHEMA_MODE": "stable",
            },
            clear=True,
        ):
            managed = llm_agent._decision_tools(
                "https://arena.example/api/llm/v1",
                legal,
            )
        with mock.patch.dict(
            os.environ,
            {
                "LLM_DECISION_TOOL": "true",
                "LLM_DECISION_TOOL_SCHEMA_MODE": "stable",
            },
            clear=True,
        ):
            direct = llm_agent._decision_tools(
                "https://provider.example/v1",
                legal,
            )

        self.assertNotIn("oneOf", managed[0]["function"]["parameters"])
        self.assertIn("oneOf", direct[0]["function"]["parameters"])

        with mock.patch.dict(
            os.environ,
            {
                "CLAWARENA_GATEWAY_KEY": "gateway-key",
                "LLM_DECISION_TOOL_SCHEMA_MODE": "menu",
            },
            clear=True,
        ):
            managed_menu = llm_agent._decision_tools(
                "https://arena.example/api/llm/v1",
                legal,
            )
        self.assertEqual(
            managed_menu[0]["function"]["parameters"]
            ["properties"]["action"]["enum"],
            ["future"],
        )

    def test_hosted_decision_tool_defaults_auto_and_direct_byo_is_unchanged(self):
        captured = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({
                    "choices": [{
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [{
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "clawarena_decision",
                                    "arguments": '{"action":"future","params":{}}',
                                },
                            }],
                        },
                        "finish_reason": "tool_calls",
                    }],
                }).encode()

        def fake_urlopen(request, timeout):
            captured.append(json.loads(request.data.decode()))
            return FakeResponse()

        decision_tool = llm_agent._decision_tool_schema([{"action": "future"}])
        with (
            mock.patch.dict(
                os.environ,
                {"CLAWARENA_GATEWAY_KEY": "gateway-key"},
                clear=True,
            ),
            mock.patch.object(llm_agent.urllib.request, "urlopen", side_effect=fake_urlopen),
        ):
            result = llm_agent._chat_request(
                "https://arena.example/api/llm/v1",
                "key",
                "model",
                [{"role": "user", "content": "choose"}],
                tools=[decision_tool],
            )
            self.assertTrue(
                llm_agent._decision_tool_enabled(
                    "https://arena.example/api/llm/v1"
                )
            )
            self.assertFalse(
                llm_agent._decision_tool_enabled("https://byo.example/v1")
            )

        self.assertEqual(captured[0]["tool_choice"], "auto")
        self.assertNotEqual(captured[0]["tool_choice"], "required")
        self.assertEqual(captured[0]["tools"], [decision_tool])
        self.assertEqual(result["text"], "")
        self.assertEqual(result["tool_calls"][0]["id"], "call-1")

        collision_base = "https://builder.example/api/llm/v1"
        with (
            mock.patch.dict(os.environ, {"LLM_API_KEY": "byo-key"}, clear=True),
            mock.patch.object(llm_agent.urllib.request, "urlopen", side_effect=fake_urlopen),
        ):
            self.assertFalse(llm_agent._decision_tool_enabled(collision_base))
            self.assertIsNone(
                llm_agent._decision_max_tokens(
                    {"game_type": "diplomacy"},
                    collision_base,
                )
            )
            llm_agent._chat_request(
                collision_base,
                "byo-key",
                "model",
                [{"role": "user", "content": "choose"}],
                metadata={"clawarena_match_id": "must-not-be-added"},
            )
        self.assertNotIn("tools", captured[1])
        self.assertNotIn("tool_choice", captured[1])
        self.assertNotIn("response_format", captured[1])
        self.assertNotIn("metadata", captured[1])

        with mock.patch.dict(
            os.environ,
            {"LLM_DECISION_TOOL": "true"},
            clear=True,
        ):
            self.assertTrue(
                llm_agent._decision_tool_enabled("https://byo.example/v1")
            )

    def test_streaming_chat_request_assembles_fragmented_tool_calls(self):
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __iter__(self):
                events = [
                    {
                        "choices": [{
                            "delta": {
                                "tool_calls": [{
                                    "index": 0,
                                    "id": "call-stream",
                                    "type": "function",
                                    "function": {
                                        "name": "clawarena_",
                                        "arguments": '{"action":"future",',
                                    },
                                }],
                            },
                        }],
                    },
                    {
                        "choices": [{
                            "delta": {
                                "tool_calls": [{
                                    "index": 0,
                                    "function": {
                                        "name": "decision",
                                        "arguments": '"params":{"value":7}}',
                                    },
                                }],
                            },
                            "finish_reason": "tool_calls",
                        }],
                    },
                    {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 8}},
                ]
                for event in events:
                    yield f"data: {json.dumps(event)}\n".encode()
                yield b"data: [DONE]\n"

        def fake_urlopen(request, timeout):
            captured["body"] = json.loads(request.data.decode())
            return FakeResponse()

        with mock.patch.object(
            llm_agent.urllib.request,
            "urlopen",
            side_effect=fake_urlopen,
        ):
            result = llm_agent._chat_request(
                "https://arena.example/api/llm/v1",
                "key",
                "model",
                [{"role": "user", "content": "choose"}],
                streaming=True,
                tools=[llm_agent._decision_tool_schema([{"action": "future"}])],
            )

        self.assertEqual(captured["body"]["tool_choice"], "auto")
        self.assertEqual(result["text"], "")
        self.assertEqual(result["finish_reason"], "tool_calls")
        self.assertEqual(result["tool_calls"], [{
            "id": "call-stream",
            "type": "function",
            "function": {
                "name": "clawarena_decision",
                "arguments": '{"action":"future","params":{"value":7}}',
            },
        }])

    def test_provider_probe_counts_failed_request_before_recovery_success(self):
        # Same boundary as the managed-runtime entrypoint above: this probe is
        # an internal diagnostic script and does not ship publicly.
        path = REPO_DIR / "scripts" / "probe_runtime_decisions.py"
        if not path.is_file():
            self.skipTest("scripts/probe_runtime_decisions.py is not part of the public distribution")
        spec = importlib.util.spec_from_file_location(
            "clawarena_probe_runtime_decisions_test",
            path,
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)
        calls = []

        def original(*_args, **_kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise llm_agent.ContextOverflowError("context length exceeded")
            return {
                "text": '{"action":"choose","params":{}}',
                "tool_calls": [],
            }

        attempts = []
        observed = probe.make_observed_chat_request(
            original,
            attempts,
            omit_response_format=False,
        )
        with self.assertRaises(llm_agent.ContextOverflowError):
            observed()
        result = observed()

        self.assertEqual(result["text"], '{"action":"choose","params":{}}')
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0]["outcome"], "error")
        self.assertEqual(attempts[0]["error_type"], "ContextOverflowError")
        self.assertEqual(attempts[1]["outcome"], "returned")
        self.assertEqual(attempts[1]["channel"], "content")

    def test_dual_parser_accepts_each_channel_and_identical_normalized_pair_once(self):
        state = {"game_type": "las_vegas"}
        legal = [{
            "action": "place",
            "params_schema": {
                "type": "object",
                "properties": {"face": {"type": "integer", "enum": [3]}},
                "required": ["face"],
                "additionalProperties": False,
            },
            "hint": {"faces_available": [{"face": 3}]},
        }]

        def tool(arguments):
            return [{
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "clawarena_decision",
                    "arguments": arguments,
                },
            }]

        content = '{"action":"place","params":{"face":"3"}}'
        arguments = '{"action":"place","params":{"face":3}}'
        with mock.patch.object(
            llm_agent,
            "_validate_prepared_action",
            wraps=llm_agent._validate_prepared_action,
        ) as validate:
            both, diagnostics = llm_agent._parse_decision_response(
                content,
                tool(arguments),
                legal,
                state,
            )
        content_only, content_diagnostics = llm_agent._parse_decision_response(
            content, None, legal, state,
        )
        tool_only, tool_diagnostics = llm_agent._parse_decision_response(
            "", tool(arguments), legal, state,
        )

        self.assertEqual(validate.call_count, 1)
        self.assertEqual(both, {"action": "place", "params": {"face": 3}})
        self.assertEqual(content_only, both)
        self.assertEqual(tool_only, both)
        self.assertEqual(diagnostics["channel"], "content_and_tool")
        self.assertEqual(content_diagnostics["channel"], "content")
        self.assertEqual(tool_diagnostics["channel"], "tool")

    def test_dual_parser_fails_closed_for_every_ambiguous_or_malformed_channel(self):
        state = {"game_type": "future_game"}
        legal = [{
            "action": "choose",
            "params_schema": {
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        }]
        valid_content = '{"action":"choose","params":{"value":1}}'
        valid_tool = {
            "id": "call-1",
            "type": "function",
            "function": {
                "name": "clawarena_decision",
                "arguments": valid_content,
            },
        }
        cases = {
            "conflicting_decisions": (
                valid_content,
                [{**valid_tool, "function": {
                    "name": "clawarena_decision",
                    "arguments": '{"action":"choose","params":{"value":2}}',
                }}],
            ),
            "multiple_tool_calls": (valid_content, [valid_tool, valid_tool]),
            "wrong_tool_name": (
                valid_content,
                [{**valid_tool, "function": {
                    "name": "other_function",
                    "arguments": valid_content,
                }}],
            ),
            "malformed_tool_arguments": (
                valid_content,
                [{**valid_tool, "function": {
                    "name": "clawarena_decision",
                    "arguments": "{broken",
                }}],
            ),
        }

        for expected, (content, tool_calls) in cases.items():
            with self.subTest(expected=expected):
                move, diagnostics = llm_agent._parse_decision_response(
                    content,
                    tool_calls,
                    legal,
                    state,
                )
                self.assertIsNone(move)
                self.assertEqual(diagnostics["outcome"], expected)

        # Deliberately NOT in the table above. Content that is not a decision at
        # all carries no competing intent to be ambiguous with, so the move in
        # the tool call is taken rather than thrown away -- which is what the
        # parser docstring has promised since this dual transport was added in
        # 604e4c4a, and what the implementation did not do. PROD 2026-08-16
        # measured 19 of 55 lost turns in one 30-minute window with this shape.
        # `conflicting_decisions` above still guards the case that IS ambiguous:
        # two readable envelopes that disagree.
        move, diagnostics = llm_agent._parse_decision_response(
            "not-json", [valid_tool], legal, state,
        )
        self.assertEqual(move, {"action": "choose", "params": {"value": 1}})
        self.assertEqual(diagnostics["outcome"], "accepted")

    def test_dual_parser_rejects_noncanonical_content_and_strict_json_violations(self):
        state = {"game_type": "future_game"}
        legal = [{
            "action": "choose",
            "params_schema": {
                "type": "object",
                "properties": {"value": {"type": "number"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        }]

        malformed_content = (
            '{"action":"choose","params":[]}',
            (
                '{"action":"choose","params":{"value":1}} '
                '{"action":"choose","params":{"value":2}}'
            ),
            '{"action":"choose","action":"choose","params":{"value":1}}',
            '{"action":"choose","params":{},"params":{"value":1}}',
            '{"action":"choose","params":{"value":NaN}}',
        )
        for content in malformed_content:
            with self.subTest(channel="content", content=content):
                move, diagnostics = llm_agent._parse_decision_response(
                    content,
                    None,
                    legal,
                    state,
                )
                self.assertIsNone(move)
                self.assertEqual(diagnostics["outcome"], "malformed_content")

        # Reported separately from content that simply is not a decision: this
        # one is a refusal that survives even when a tool call carries a usable
        # move, so it must not share a label with the ignorable case.
        move, diagnostics = llm_agent._parse_decision_response(
            '{"action":"choose","params":{"value":1},"idempotency_key":"x"}',
            None,
            legal,
            state,
        )
        self.assertIsNone(move)
        self.assertEqual(diagnostics["outcome"], "content_reserved_keys")

        malformed_tool_arguments = (
            '{"action":"choose","action":"choose","params":{"value":1}}',
            '{"action":"choose","params":{},"params":{"value":1}}',
            '{"action":"choose","params":{"value":Infinity}}',
        )
        for arguments in malformed_tool_arguments:
            with self.subTest(channel="tool", arguments=arguments):
                move, diagnostics = llm_agent._parse_decision_response(
                    "",
                    [{
                        "type": "function",
                        "function": {
                            "name": "clawarena_decision",
                            "arguments": arguments,
                        },
                    }],
                    legal,
                    state,
                )
                self.assertIsNone(move)
                self.assertEqual(
                    diagnostics["outcome"],
                    "malformed_tool_arguments",
                )

    def test_starter_canonicalizes_diplomacy_enums_before_shared_validation(self):
        adjustment = [{
            "action": "submit_adjustments",
            "params_schema": {
                "type": "object",
                "properties": {
                    "orders": {
                        "type": "array",
                        "items": {
                            "oneOf": [
                                {
                                    "type": "object",
                                    "properties": {
                                        "type": {"type": "string", "enum": ["BUILD"]},
                                        "unit_type": {"type": "string", "enum": ["A", "F"]},
                                        "destination": {"type": "string", "enum": ["LON"]},
                                    },
                                    "required": ["type", "unit_type", "destination"],
                                    "additionalProperties": False,
                                },
                                {
                                    "type": "object",
                                    "properties": {
                                        "type": {"type": "string", "enum": ["WAIVE"]},
                                    },
                                    "required": ["type"],
                                    "additionalProperties": False,
                                },
                            ],
                        },
                    },
                },
                "required": ["orders"],
                "additionalProperties": False,
            },
            "hint": {"legal_orders": []},
        }]
        move = llm_agent._parse_action(
            (
                '{"action":"submit_adjustments","params":{"orders":['
                '{"type":"build","unit_type":"f","destination":"lon"}]}}'
            ),
            adjustment,
            {"game_type": "diplomacy"},
        )
        self.assertEqual(move, {
            "action": "submit_adjustments",
            "params": {
                "orders": [{
                    "type": "BUILD",
                    "unit_type": "F",
                    "destination": "LON",
                }],
            },
        })

        press = [{
            "action": "send_press",
            "params_schema": {
                "type": "object",
                "properties": {
                    "messages": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "to_power": {
                                    "type": "string",
                                    "enum": ["FRANCE", "global"],
                                },
                                "content": {"type": "string"},
                            },
                            "required": ["to_power", "content"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["messages"],
                "additionalProperties": False,
            },
            "hint": {
                "max_messages": 1,
                "recipient_powers": ["FRANCE"],
            },
        }]
        global_move = llm_agent._parse_action(
            (
                '{"action":"send_press","params":{"messages":['
                '{"to_power":"GLOBAL","content":"Hello"}]}}'
            ),
            press,
            {"game_type": "diplomacy"},
        )
        self.assertEqual(
            global_move["params"]["messages"][0]["to_power"],
            "global",
        )
        self.assertIsNone(llm_agent._parse_action(
            (
                '{"action":"send_press","params":{"messages":['
                '{"to_power":"NOWHERE","content":"Hello"}]}}'
            ),
            press,
            {"game_type": "diplomacy"},
        ))

    def test_tool_response_runs_dynamic_schema_and_secret_free_provenance(self):
        state = {"game_type": "future_game", "_action_window_id": "window-secret"}
        legal = [{
            "action": "allocate",
            "params_schema": {
                "type": "object",
                "properties": {"count": {"type": "integer", "minimum": 1}},
                "required": ["count"],
                "additionalProperties": False,
            },
        }]
        secret_arguments = '{"action":"allocate","params":{"count":0},"memo":"secret-plan"}'
        tool_calls = [{
            "type": "function",
            "function": {
                "name": "clawarena_decision",
                "arguments": secret_arguments,
            },
        }]

        move, diagnostics = llm_agent._parse_decision_response(
            "", tool_calls, legal, state,
        )
        provenance = llm_agent._reply_provenance(
            "",
            legal,
            state,
            tool_calls=tool_calls,
            parse_diagnostics=diagnostics,
        )

        self.assertIsNone(move)
        self.assertEqual(diagnostics["outcome"], "contract_invalid")
        self.assertEqual(provenance["response_channel"], "tool")
        self.assertEqual(provenance["tool_call_count"], 1)
        self.assertEqual(provenance["outcome"], "contract_invalid")
        self.assertNotIn("secret-plan", json.dumps(provenance))
        self.assertNotIn(secret_arguments, json.dumps(provenance))

    def test_prose_beside_a_tool_call_does_not_discard_the_move(self):
        """The tool channel is read even when the content channel is narration.

        PROD 2026-08-16: 19 of 55 lost turns in one 30-minute window had exactly
        this shape -- finish_reason=tool_calls, tool_call_count=1, and a content
        field the parser could not read as a decision. The move was in the tool
        call the whole time; returning on the content error threw it away.
        """
        state = {"game_type": "mafia", "_action_window_id": "window-3"}
        legal = [{"action": "chat", "params": {"message": "str"}}]
        tool_calls = [{
            "type": "function",
            "function": {
                "name": "clawarena_decision",
                "arguments": json.dumps({
                    "action": "chat",
                    "params": {"message": "Seat 3 dodged the question twice."},
                }),
            },
        }]

        move, diagnostics = llm_agent._parse_decision_response(
            "Let me think about who has been evasive so far.",
            tool_calls,
            legal,
            state,
        )

        self.assertIsNotNone(move)
        self.assertEqual(move["action"], "chat")
        self.assertEqual(
            move["params"]["message"], "Seat 3 dodged the question twice.",
        )
        self.assertEqual(diagnostics["outcome"], "accepted")

    def test_a_reply_reaching_for_the_requests_identity_still_refuses(self):
        """Widening the tool fall-through must not swallow this refusal.

        Commentary beside a move is a chatty model; a reply naming the client's
        own submission fields is a different event, and it is refused even when
        a tool call would otherwise carry a usable move.
        """
        state = {"game_type": "mafia", "_action_window_id": "window-4"}
        legal = [{"action": "chat", "params": {"message": "str"}}]
        tool_calls = [{
            "type": "function",
            "function": {
                "name": "clawarena_decision",
                "arguments": json.dumps({
                    "action": "chat", "params": {"message": "hello"},
                }),
            },
        }]

        move, diagnostics = llm_agent._parse_decision_response(
            json.dumps({
                "action": "chat",
                "params": {"message": "hello"},
                "idempotency_key": "forged",
            }),
            tool_calls,
            legal,
            state,
        )

        self.assertIsNone(move)
        self.assertEqual(diagnostics["outcome"], "content_reserved_keys")
        self.assertEqual(diagnostics["channel"], "content")

    def test_unreadable_content_alone_still_fails_closed(self):
        """With no tool channel there is nothing to fall through to."""
        state = {"game_type": "mafia", "_action_window_id": "window-5"}
        legal = [{"action": "chat", "params": {"message": "str"}}]

        move, diagnostics = llm_agent._parse_decision_response(
            "I am still thinking about it.", [], legal, state,
        )

        self.assertIsNone(move)
        self.assertEqual(diagnostics["outcome"], "malformed_content")

    def _unusable_reply_provenance(self):
        """Build a provenance line for a reply the parser could not use."""
        state = {"game_type": "mafia", "_action_window_id": "window-1"}
        legal = [{"action": "chat", "params": {"message": "str"}}]
        text = "Here is my read:\n{\"action\": \"chat\", \"params\": {\"message\": \"hi\"}}"
        move, diagnostics = llm_agent._parse_decision_response(text, [], legal, state)
        self.assertIsNone(move)
        return text, legal, state, diagnostics

    def test_unusable_reply_is_not_written_to_disk_by_default(self):
        """A reply can quote another player, so capture stays opt-in."""
        text, legal, state, diagnostics = self._unusable_reply_provenance()
        target = pathlib.Path(tempfile.mkdtemp()) / "capture"

        with mock.patch.object(llm_agent, "_UNPARSED_CAPTURE_DIR", ""), \
                mock.patch.dict(llm_agent._UNPARSED_CAPTURED, {"n": 0}):
            llm_agent._reply_provenance(
                text, legal, state, parse_diagnostics=diagnostics,
            )

        self.assertFalse(target.exists())

    def test_enabled_capture_keeps_the_reply_that_cost_the_turn(self):
        """The hash in the log cannot be diagnosed; the text can.

        PROD 2026-08-16: two seats fell back inside one Mafia match while eight
        replays of the same request shape returned clean tool calls, so the
        failing text is the only thing that can explain it.
        """
        text, legal, state, diagnostics = self._unusable_reply_provenance()
        target = pathlib.Path(tempfile.mkdtemp()) / "capture"

        with mock.patch.object(llm_agent, "_UNPARSED_CAPTURE_DIR", str(target)), \
                mock.patch.dict(llm_agent._UNPARSED_CAPTURED, {"n": 0}):
            provenance = llm_agent._reply_provenance(
                text, legal, state, parse_diagnostics=diagnostics,
            )

        written = sorted(target.glob("unparsed-*.txt"))
        self.assertEqual(len(written), 1)
        body = written[0].read_text(encoding="utf-8")
        # The file holds the SAME material the logged hash is taken over -- both
        # channels, not just content -- so an operator can tie a capture back to
        # the provenance line beside it and see which channel the reply used.
        captured = json.loads(body)
        self.assertEqual(captured["content"], text)
        self.assertEqual(captured["tool_calls"], [])
        self.assertEqual(
            hashlib.sha256(body.encode("utf-8")).hexdigest()[:20],
            provenance["response_sha256"],
        )
        self.assertIn(provenance["outcome"], written[0].name)
        self.assertNotEqual(provenance["outcome"], "accepted")

    def test_capture_ignores_a_reply_the_parser_accepted(self):
        """Only a reply that cost a fallback is worth a file."""
        state = {"game_type": "mafia", "_action_window_id": "window-2"}
        legal = [{"action": "chat", "params": {"message": "str"}}]
        text = '{"action": "chat", "params": {"message": "I suspect seat 3."}}'
        move, diagnostics = llm_agent._parse_decision_response(text, [], legal, state)
        self.assertIsNotNone(move)
        target = pathlib.Path(tempfile.mkdtemp()) / "capture"

        with mock.patch.object(llm_agent, "_UNPARSED_CAPTURE_DIR", str(target)), \
                mock.patch.dict(llm_agent._UNPARSED_CAPTURED, {"n": 0}):
            provenance = llm_agent._reply_provenance(
                text, legal, state, parse_diagnostics=diagnostics,
            )

        self.assertEqual(provenance["outcome"], "accepted")
        self.assertFalse(target.exists())

    def test_mafia_nullable_target_follows_only_the_current_vote_contract(self):
        nullable_vote = [{
            "action": "vote",
            "params": {"target_id": "int or null"},
            "params_schema": {
                "type": "object",
                "properties": {
                    "target_id": {
                        "type": ["integer", "null"],
                        "enum": [7, None],
                    },
                },
                "required": ["target_id"],
            },
            "hint": {"candidates": [{"agent_id": 7}]},
        }]
        runoff_vote = copy.deepcopy(nullable_vote)
        runoff_vote[0]["params"] = {"target_id": "int"}
        runoff_vote[0]["params_schema"]["properties"]["target_id"] = {
            "type": "integer",
            "enum": [7],
        }
        night_action = copy.deepcopy(nullable_vote)
        night_action[0]["action"] = "night_action"
        night_action[0]["hint"] = {"targets": [{"agent_id": 7}]}

        self.assertEqual(
            llm_agent._parse_action(
                '{"action":"vote","params":{"target_id":null}}',
                nullable_vote,
                {"game_type": "mafia"},
            ),
            {"action": "vote", "params": {"target_id": None}},
        )
        self.assertIsNone(llm_agent._parse_action(
            '{"action":"vote","params":{"target_id":null}}',
            runoff_vote,
            {"game_type": "mafia"},
        ))
        self.assertIsNone(llm_agent._parse_action(
            '{"action":"night_action","params":{"target_id":null}}',
            night_action,
            {"game_type": "mafia"},
        ))
        self.assertIsNone(llm_agent._parse_action(
            '{"action":"vote","params":{}}',
            nullable_vote,
            {"game_type": "mafia"},
        ))

    def test_starter_gameplay_scaffold_is_general_and_server_authoritative(self):
        prompt = llm_agent.GAMEPLAY_SYSTEM_SCAFFOLD

        self.assertIn("stable.rules", prompt)
        self.assertIn("turn.decision_support.recommended_action", prompt)
        self.assertIn("treat its supplied comparison as complete", prompt)
        self.assertIn("General strategic advice", prompt)
        self.assertIn("computed_analysis", prompt)
        self.assertIn("turn.legal_actions", prompt)
        self.assertIn("An absent action is impossible", prompt)
        self.assertIn("Hidden reasoning and JSON share the turn budget", prompt)
        self.assertIn("owner/provider reasoning setting", prompt)
        self.assertNotIn("low reasoning effort", prompt)
        self.assertIn("one compact JSON object", prompt)
        self.assertLess(len(prompt), 1600)
        for game_specific_term in (
            "liars_dice",
            "Mafia",
            "monopoly",
            "Diplomacy",
            "Las Vegas",
        ):
            self.assertNotIn(game_specific_term, prompt)

    def test_server_decision_context_replaces_the_client_game_allowlist(self):
        legal = [
            {"action": "challenge", "params": {}},
            {"action": "bid", "params": {"quantity": "int", "face": "int"}},
        ]
        state = {
            "game_type": "liars_dice",
            "your_dice": [4, 3, 3, 6, 4],
            "total_dice_count": 10,
            "last_bid": {"quantity": 3, "face": 6},
            "my_memory": {"my_recent_moves": []},
            "_decision_context": {
                "version": 1,
                "game_type": "liars_dice",
                "snapshot_mode": "turn",
                "state": {
                    "your_dice": [4, 3, 3, 6, 4],
                    "total_dice_count": 10,
                    "last_bid": {"quantity": 3, "face": 6},
                    "future_server_field": {"value": 7},
                },
                "legal_actions": legal,
                "rules": {"rules": ["server rule"]},
                "strategy": {"objective": "win"},
                "user_preferences": {"current_risk_profile": "balanced"},
            },
        }

        messages = llm_agent._bounded_structured_messages(
            state,
            legal,
            system_prompt=llm_agent.GAMEPLAY_SYSTEM_SCAFFOLD,
        )
        payload = json.loads(messages[1]["content"])

        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["profile"], "stateless")
        # The model payload deliberately omits stable.id: it is transport
        # identity the model cannot act on, and because nested keys are
        # alphabetized it sits ahead of ``rules`` and truncates the provider's
        # shared prefix for every agent whose stable digest differs.
        self.assertNotIn("id", payload["stable"])
        self.assertEqual(payload["stable"]["game_type"], "liars_dice")
        # ...but the transport contract still carries and validates it.
        self.assertTrue(
            decision_context.normalize_decision_context(
                state["_decision_context"],
            )["stable"]["id"].startswith("dc1-")
        )
        self.assertEqual(payload["turn"]["state"]["your_dice"], [4, 3, 3, 6, 4])
        self.assertEqual(payload["turn"]["state"]["future_server_field"], {"value": 7})
        self.assertEqual(payload["turn"]["legal_actions"], legal)
        self.assertAlmostEqual(payload["computed_analysis"]["p_standing_bid_true"], 0.539)

    def test_valid_server_decision_support_replaces_client_analysis_and_hides_fallback(self):
        context = DecisionContextContractTests.v2_context()
        context["turn"]["decision_support"] = {
            "recommended_action": {"action": "choose", "params": {}},
        }
        state = {
            "game_type": "future_game",
            "_decision_context": context,
        }

        with mock.patch.object(
            llm_agent,
            "_computed_analysis",
            side_effect=AssertionError("client analysis must not run"),
        ):
            messages = llm_agent._bounded_structured_messages(
                state,
                context["turn"]["legal_actions"],
                system_prompt=llm_agent.GAMEPLAY_SYSTEM_SCAFFOLD,
            )
        payload = json.loads(messages[1]["content"])

        self.assertEqual(
            payload["turn"]["decision_support"]["recommended_action"],
            {"action": "choose", "params": {}},
        )
        self.assertNotIn("computed_analysis", payload)
        self.assertNotIn("fallback", payload)
        self.assertEqual(
            decision_context.executable_fallback(context),
            context["fallback"],
        )

    def test_session_turns_carry_server_decision_support_like_bounded_ones(self):
        """The accumulating window must not discard the server planner.

        ``_snapshot`` never copied ``turn.decision_support``, so every session
        turn silently fell back to the client helper and re-derived a ranking
        the server had already published. Measured live on diplomacy, that cost
        2.9x the hidden reasoning of a bounded turn at the same prompt size.
        """

        context = DecisionContextContractTests.v2_context()
        context["turn"]["decision_support"] = {
            "recommended_action": {"action": "choose", "params": {}},
        }
        state = {"game_type": "future_game", "_decision_context": context}
        legal = context["turn"]["legal_actions"]

        with (
            mock.patch.object(llm_agent.memory, "current_match_id", return_value=907),
            mock.patch.object(
                llm_agent,
                "_computed_analysis",
                side_effect=AssertionError("client analysis must not run"),
            ),
        ):
            first, pending = llm_agent._prepare_conversation(state, legal)
            llm_agent._commit_conversation(pending, '{"action":"choose","params":{}}')
            second, second_pending = llm_agent._prepare_conversation(state, legal)

        baseline = json.loads(first[1]["content"].split("STATE_BASELINE:\n", 1)[1])
        update = json.loads(second[-1]["content"].split("TURN_UPDATE:\n", 1)[1])

        self.assertEqual(second_pending["mode"], "delta")
        for block in (baseline, update):
            self.assertEqual(
                block["decision_support"]["recommended_action"],
                {"action": "choose", "params": {}},
            )
            # Both keys are always present; exactly one carries a layer, so the
            # model never sees two competing recommendations nor a stale one.
            self.assertIsNone(block["computed_analysis"])

    def test_session_delta_retracts_decision_support_explicitly(self):
        """A turn without server support must say so, not stay silent.

        The help block rides at the top level of a delta turn, outside
        ``state_delta``, and the delta contract tells the model that an omitted
        field is unchanged. Diplomacy publishes support on an ORDERS window and
        not on the negotiation window that follows, so emitting only the live key
        would leave a movement plan standing as the "complete comparison" for a
        press turn.
        """

        supported = DecisionContextContractTests.v2_context()
        supported["turn"]["decision_support"] = {
            "recommended_action": {"action": "choose", "params": {}},
        }
        unsupported = DecisionContextContractTests.v2_context()
        unsupported["turn"].pop("decision_support", None)
        legal = supported["turn"]["legal_actions"]

        with mock.patch.object(llm_agent.memory, "current_match_id", return_value=908):
            _first, pending = llm_agent._prepare_conversation(
                {"game_type": "future_game", "_decision_context": supported}, legal,
            )
            llm_agent._commit_conversation(pending, '{"action":"choose","params":{}}')
            second, second_pending = llm_agent._prepare_conversation(
                {"game_type": "future_game", "_decision_context": unsupported}, legal,
            )

        update = json.loads(second[-1]["content"].split("TURN_UPDATE:\n", 1)[1])

        self.assertEqual(second_pending["mode"], "delta")
        # Present-and-null is the retraction; a missing key would read as
        # "unchanged" and leave the previous recommendation standing.
        self.assertIn("decision_support", update)
        self.assertIsNone(update["decision_support"])
        self.assertIn("computed_analysis", update)

    def test_unusable_server_decision_support_keeps_legacy_client_analysis(self):
        context = DecisionContextContractTests.v2_context()
        context["turn"]["decision_support"] = {
            "recommended_action": {"action": "invented", "params": {}},
        }
        state = {
            "game_type": "future_game",
            "_decision_context": context,
        }

        with mock.patch.object(
            llm_agent,
            "_computed_analysis",
            return_value={"legacy": "kept"},
        ) as computed:
            messages = llm_agent._bounded_structured_messages(
                state,
                context["turn"]["legal_actions"],
            )
        payload = json.loads(messages[1]["content"])

        computed.assert_called_once()
        self.assertEqual(payload["computed_analysis"], {"legacy": "kept"})

    def test_canonical_context_drives_byo_baseline_and_keeps_session_deltas(self):
        legal = [{"action": "choose", "params": {"option": "string"}}]

        def state(resource):
            return {
                "game_type": "future_game",
                "new_resource": "raw-state-must-not-win",
                "my_agent_id": 999,
                "my_memory": {"my_recent_moves": []},
                "_decision_context": {
                    "version": 1,
                    "game_type": "future_game",
                    "status": "playing",
                    "is_your_turn": True,
                    "state": {
                        "game_type": "future_game",
                        "my_agent_id": 7,
                        "new_resource": resource,
                    },
                    "legal_actions": legal,
                    "rules": {"rules": ["dynamic rule"]},
                    "strategy": {"objective": "dynamic objective"},
                    "user_preferences": {},
                },
            }

        with mock.patch.object(llm_agent.memory, "current_match_id", return_value=91):
            first, pending = llm_agent._prepare_conversation(state(7), legal)
            llm_agent._commit_conversation(
                pending,
                '{"action":"choose","params":{"option":"a"}}',
            )
            second, second_pending = llm_agent._prepare_conversation(state(8), legal)

        self.assertEqual(first[0]["content"], llm_agent.GAMEPLAY_SESSION_SCAFFOLD)
        context_text, baseline_text = first[1]["content"].split("\n\nSTATE_BASELINE:\n", 1)
        match_context = json.loads(context_text.removeprefix("MATCH_CONTEXT:\n"))
        baseline = json.loads(baseline_text)
        self.assertEqual(match_context["game_rules_brief"], {"rules": ["dynamic rule"]})
        self.assertEqual(match_context["identity"]["my_agent_id"], 7)
        self.assertNotIn("game_type", baseline["state"])
        self.assertNotIn("my_agent_id", baseline["state"])
        self.assertEqual(baseline["state"]["new_resource"], 7)
        self.assertNotIn("raw-state-must-not-win", first[1]["content"])
        self.assertEqual(second_pending["mode"], "delta")
        update = json.loads(second[-1]["content"].removeprefix("TURN_UPDATE:\n"))
        self.assertEqual(update["state_delta"], {"new_resource": 8})

    def test_generic_schema_runs_before_game_semantic_validation(self):
        legal = [{
            "action": "bid",
            "params": {"quantity": "int", "face": "int"},
            "params_schema": {
                "type": "object",
                "required": ["quantity", "face"],
                "properties": {
                    "quantity": {"type": "integer", "minimum": 1, "maximum": 10},
                    "face": {"type": "integer", "minimum": 1, "maximum": 6},
                },
            },
        }]
        state = {
            "game_type": "liars_dice",
            "_decision_context": {
                "version": 1,
                "game_type": "liars_dice",
                "state": {"your_dice": [2, 3], "total_dice_count": 4},
                "legal_actions": legal,
                "rules": {},
                "strategy": {},
                "user_preferences": {},
            },
        }

        self.assertIsNone(llm_agent._parse_action(
            '{"action":"bid","params":{"quantity":"2","face":3}}',
            legal,
            state,
        ))
        self.assertIsNotNone(llm_agent._parse_action(
            '{"action":"bid","params":{"quantity":2,"face":3}}',
            legal,
            state,
        ))

    def test_model_failure_prefers_the_current_server_fallback(self):
        context = DecisionContextContractTests.v2_context()
        state = {
            "game_type": "future_game",
            "_decision_context": context,
        }
        with (
            mock.patch.object(llm_agent, "_llm_config", return_value=(None, None, None)),
            mock.patch.object(llm_agent.heuristic_agent, "decide") as heuristic,
        ):
            move = llm_agent.decide(state, context["turn"]["legal_actions"])

        self.assertEqual(move, context["fallback"])
        heuristic.assert_not_called()

    def test_starter_and_hermes_share_the_same_bounded_contract(self):
        state = self.state()
        messages = llm_agent._bounded_structured_messages(state, self.legal())

        self.assertEqual(messages[0]["content"], llm_agent.BOUNDED_STRUCTURED_PROMPT)
        self.assertEqual(messages[0]["content"], llm_agent.GAMEPLAY_SYSTEM_SCAFFOLD)

    def test_direct_deepseek_reasoning_controls_are_builder_owned(self):
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({
                    "choices": [{
                        "message": {"role": "assistant", "content": "{}"},
                        "finish_reason": "stop",
                    }],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 2,
                        "total_tokens": 12,
                    },
                }).encode()

        def fake_urlopen(request, timeout):
            captured["body"] = json.loads(request.data.decode())
            captured["timeout"] = timeout
            return FakeResponse()

        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(llm_agent.urllib.request, "urlopen", side_effect=fake_urlopen),
        ):
            result = llm_agent._chat_request(
                "https://llm.example/v1",
                "key",
                "deepseek-v4-flash",
                [{"role": "user", "content": "choose"}],
            )

        self.assertEqual(result["text"], "{}")
        self.assertNotIn("thinking", captured["body"])
        self.assertNotIn("reasoning_effort", captured["body"])
        self.assertNotIn("max_tokens", captured["body"])

        with (
            mock.patch.dict(
                os.environ,
                {
                    "LLM_THINKING_MODE": "enabled",
                    "LLM_REASONING_EFFORT": "high",
                },
                clear=True,
            ),
            mock.patch.object(llm_agent.urllib.request, "urlopen", side_effect=fake_urlopen),
        ):
            llm_agent._chat_request(
                "https://llm.example/v1",
                "key",
                "deepseek-v4-flash",
                [{"role": "user", "content": "choose"}],
            )

        self.assertEqual(captured["body"]["thinking"], {"type": "enabled"})
        self.assertEqual(captured["body"]["reasoning_effort"], "high")

    def test_length_finish_reason_names_the_active_completion_limit(self):
        diplomacy = self.state()
        diplomacy["game_type"] = "diplomacy"
        fallback = {"action": "chat", "params": {}}
        with (
            mock.patch.object(llm_agent.memory, "current_match_id", return_value=41),
            mock.patch.object(
                llm_agent,
                "_llm_config",
                return_value=("https://llm.example/v1", "key", "model"),
            ),
            mock.patch.object(llm_agent, "_model_context_window", return_value=0),
            mock.patch.object(
                llm_agent,
                "_chat_request",
                return_value={
                    "text": "",
                    "prompt_tokens": 10,
                    "finish_reason": "length",
                },
            ),
            mock.patch.object(llm_agent.heuristic_agent, "decide", return_value=fallback),
            io.StringIO() as captured,
            mock.patch.object(sys, "stdout", captured),
        ):
            move = llm_agent.decide(diplomacy, self.legal())
            log = captured.getvalue()

        self.assertEqual(move, fallback)
        self.assertIn("provider ended the completion at length", log)
        self.assertIn("configure LLM_MAX_TOKENS only if", log)

    def test_managed_length_cap_recommends_prompt_fix_not_a_larger_cap(self):
        fallback = {"action": "chat", "params": {}}
        with (
            mock.patch.dict(
                os.environ,
                {"CLAWARENA_GATEWAY_KEY": "gateway-key"},
                clear=True,
            ),
            mock.patch.object(llm_agent.memory, "current_match_id", return_value=41),
            mock.patch.object(
                llm_agent,
                "_llm_config",
                return_value=(
                    "https://arena.example/api/llm/v1",
                    "key",
                    "deepseek/deepseek-v4-flash",
                ),
            ),
            mock.patch.object(
                llm_agent,
                "_chat_request",
                return_value={
                    "text": "",
                    "prompt_tokens": 10,
                    "completion_tokens": 8000,
                    "finish_reason": "length",
                },
            ),
            mock.patch.object(llm_agent.heuristic_agent, "decide", return_value=fallback),
            io.StringIO() as captured,
            mock.patch.object(sys, "stdout", captured),
        ):
            move = llm_agent.decide(self.state(), self.legal())
            log = captured.getvalue()

        self.assertEqual(move, fallback)
        self.assertIn("raising the hosted live-game cap would extend the latency tail", log)
        self.assertNotIn("raise LLM_MAX_TOKENS", log)
        self.assertIn("deterministic legal fallback", log)

    def test_new_match_never_reuses_the_previous_match_transcript(self):
        with mock.patch.object(llm_agent.memory, "current_match_id", return_value=41):
            _, pending = llm_agent._prepare_conversation(self.state(), self.legal())
            llm_agent._commit_conversation(pending, '{"action":"chat","params":{}}')
        with mock.patch.object(llm_agent.memory, "current_match_id", return_value=42):
            messages, pending = llm_agent._prepare_conversation(self.state(), self.legal())

        self.assertEqual(pending["mode"], "full")
        self.assertEqual([message["role"] for message in messages], ["system", "user"])

    def test_unusable_reply_does_not_commit_a_broken_transcript(self):
        fallback = {"action": "chat", "params": {"message": "safe"}}
        with (
            mock.patch.object(llm_agent.memory, "current_match_id", return_value=41),
            mock.patch.object(
                llm_agent,
                "_llm_config",
                return_value=("https://llm.example/v1", "key", "model"),
            ),
            mock.patch.object(llm_agent, "_chat_request", return_value="not-json"),
            mock.patch.object(llm_agent.heuristic_agent, "decide", return_value=fallback),
        ):
            self.assertEqual(llm_agent.decide(self.state(), self.legal()), fallback)

        self.assertEqual(llm_agent._SESSION["messages"], [])
        self.assertIsNone(llm_agent._SESSION["match_id"])

    def test_every_supported_game_fixture_builds_a_full_then_delta_turn(self):
        fixture_names = (
            "liars_opening",
            "vegas_place",
            "mafia_chat",
            "monopoly_turn",
            "diplomacy_negotiation",
            "diplomacy_movement",
            "diplomacy_retreat",
            "diplomacy_adjustment",
        )
        for index, fixture_name in enumerate(fixture_names, start=1):
            fixture = json.loads(
                (KIT_DIR / "fixtures" / f"{fixture_name}.json").read_text()
            )
            state = dict(fixture.get("state") or {})
            state.setdefault("game_type", fixture.get("game_type"))
            for key in ("game_rules_brief", "strategy_brief"):
                if fixture.get(key) is not None:
                    state[key] = fixture[key]
            state["my_memory"] = {"my_recent_moves": []}
            legal = fixture.get("legal_actions") or []
            llm_agent._reset_session()
            with mock.patch.object(
                llm_agent.memory,
                "current_match_id",
                return_value=100 + index,
            ):
                first, pending = llm_agent._prepare_conversation(state, legal)
                llm_agent._commit_conversation(
                    pending,
                    json.dumps({"action": legal[0]["action"], "params": {}}),
                )
                second, pending = llm_agent._prepare_conversation(state, legal)

            self.assertIn("STATE_BASELINE:\n", first[1]["content"], fixture_name)
            self.assertEqual(pending["mode"], "delta", fixture_name)
            self.assertIn("TURN_UPDATE:\n", second[-1]["content"], fixture_name)


class DiplomacyOfflineContractTests(unittest.TestCase):
    def fixture(self, name):
        return json.loads((KIT_DIR / "fixtures" / f"{name}.json").read_text())

    def test_checker_accepts_a_complete_legal_movement_batch_with_a_move(self):
        fixture = self.fixture("diplomacy_movement")
        move = {
            "action": "submit_orders",
            "params": {
                "orders": [
                    {"type": "MOVE", "origin": "EDI", "destination": "NTH"},
                    {"type": "HOLD", "origin": "LON"},
                    {"type": "HOLD", "origin": "LVP"},
                ],
            },
        }

        self.assertEqual(offline_check.check_move(fixture, move), [])

    def test_checker_accepts_server_legal_partial_batches(self):
        cases = (
            (
                "diplomacy_movement",
                {
                    "action": "submit_orders",
                    "params": {"orders": [
                        {"type": "MOVE", "origin": "EDI", "destination": "NTH"},
                    ]},
                },
            ),
            (
                "diplomacy_retreat",
                {"action": "submit_retreats", "params": {"orders": []}},
            ),
            (
                "diplomacy_adjustment",
                {
                    "action": "submit_adjustments",
                    "params": {"orders": [
                        {"type": "BUILD", "destination": "LON", "unit_type": "A"},
                    ]},
                },
            ),
        )
        for fixture_name, move in cases:
            with self.subTest(fixture_name=fixture_name):
                self.assertEqual(
                    offline_check.check_move(self.fixture(fixture_name), move),
                    [],
                )

    def test_checker_uses_machine_readable_support_and_convoy_candidates(self):
        fixture = self.fixture("diplomacy_movement")
        support = {
            "action": "submit_orders",
            "params": {"orders": [{
                "type": "SUPPORT",
                "origin": "LON",
                "target": "LVP",
                "destination": "YOR",
            }]},
        }
        self.assertEqual(offline_check.check_move(fixture, support), [])

        convoy_fixture = json.loads(json.dumps(fixture))
        convoy_hint = {
            "origin": "NTH",
            "unit_type": "F",
            "can_hold": True,
            "move_destinations": ["BEL", "LON", "NWY"],
            "can_move_via_convoy": False,
            "support_options": [],
            "can_support": False,
            "can_convoy": True,
        }
        convoy_fixture["legal_actions"][0]["hint"]["legal_orders"] = [convoy_hint]
        convoy_fixture["legal_actions"][0]["hint"]["shared_candidates"] = {
            "convoy_army_origins": ["LON"],
            "convoy_destinations": ["BEL", "NWY"],
        }
        convoy = {
            "action": "submit_orders",
            "params": {"orders": [{
                "type": "CONVOY",
                "origin": "NTH",
                "target": "LON",
                "destination": "BEL",
            }]},
        }
        self.assertEqual(offline_check.check_move(convoy_fixture, convoy), [])

    def test_checker_accepts_current_heuristic_candidate_and_bounded_override(self):
        fixture = json.loads(json.dumps(self.fixture("diplomacy_movement")))
        hint = fixture["legal_actions"][0]["hint"]
        candidate_orders = [
            {"type": "HOLD", "origin": entry["origin"]}
            for entry in hint["legal_orders"]
        ]
        hint["heuristic_advice"] = {
            "recommended_candidate_id": "precision-test-1",
            "candidates": [{
                "candidate_id": "precision-test-1",
                "orders": candidate_orders,
            }],
        }
        move = {
            "action": "submit_orders",
            "params": {
                "candidate_id": "precision-test-1",
                "order_overrides": [{
                    "type": "MOVE",
                    "origin": "LVP",
                    "destination": "YOR",
                }],
            },
        }

        self.assertEqual(offline_check.check_move(fixture, move), [])
        move["params"]["order_overrides"][0]["destination"] = "MUN"
        self.assertTrue(offline_check.check_move(fixture, move))

    def test_checker_uses_server_press_budget(self):
        fixture = json.loads(json.dumps(self.fixture("diplomacy_negotiation")))
        fixture["legal_actions"][0]["hint"]["max_messages"] = 2
        target = fixture["legal_actions"][0]["hint"]["recipient_powers"][0]
        move = {
            "action": "send_press",
            "params": {"messages": [
                {"to_power": target, "content": f"Proposal {index}"}
                for index in range(3)
            ]},
        }

        self.assertTrue(offline_check.check_move(fixture, move))

    def test_checker_preserves_zero_press_budget_and_accepts_server_default(self):
        fixture = self.fixture("diplomacy_negotiation")
        hint = fixture["legal_actions"][0]["hint"]
        target = hint["recipient_powers"][0]

        self.assertEqual(hint["max_messages"], 0)
        self.assertEqual(
            offline_check.check_move(
                fixture,
                {
                    "action": "send_press",
                    "params": {"use_server_default": True},
                },
            ),
            [],
        )
        problems = offline_check.check_move(
            fixture,
            {
                "action": "send_press",
                "params": {
                    "messages": [{"to_power": target, "content": "Hello"}],
                },
            },
        )
        self.assertEqual(
            problems,
            ["diplomacy press batch exceeds 0 messages: 1"],
        )

    def test_checker_rejects_unlisted_press_contract_identifiers(self):
        fixture = self.fixture("diplomacy_negotiation")
        hint = fixture["legal_actions"][0]["hint"]
        hint["max_messages"] = 1
        target = hint["recipient_powers"][0]
        move = {
            "action": "send_press",
            "params": {
                "messages": [{"to_power": target, "content": "Hold the north."}],
                "strategy_intent": {"avoid_provinces": ["NOR"]},
            },
        }

        problems = offline_check.check_move(fixture, move)

        self.assertEqual(len(problems), 1)
        self.assertIn("avoid_provinces[0]", problems[0])
        self.assertIn("server-authorized ids", problems[0])
        self.assertIn("NWY", hint["valid_province_ids"])
        self.assertNotIn("NOR", hint["valid_province_ids"])

    def test_checker_rejects_self_conflicting_private_strategy_intent(self):
        fixture = self.fixture("diplomacy_negotiation")
        hint = fixture["legal_actions"][0]["hint"]
        hint["max_messages"] = 1
        target = hint["recipient_powers"][0]
        move = {
            "action": "send_press",
            "params": {
                "messages": [{"to_power": target, "content": "Keep Burgundy quiet."}],
                "strategy_intent": {
                    "priority_targets": ["BUR"],
                    "dmz_provinces": ["BUR"],
                },
            },
        }

        problems = offline_check.check_move(fixture, move)

        self.assertEqual(len(problems), 1)
        self.assertIn("prioritize a province", problems[0])

    def test_checker_rejects_empty_candidate_domains_and_wrong_unit_roles(self):
        fixture = self.fixture("diplomacy_movement")
        invalid_moves = (
            {
                "action": "submit_orders",
                "params": {"orders": [{
                    "type": "MOVE",
                    "origin": "EDI",
                    "destination": "BEL",
                    "via_convoy": True,
                }]},
            },
            {
                "action": "submit_orders",
                "params": {"orders": [{
                    "type": "CONVOY",
                    "origin": "EDI",
                    "target": "LVP",
                    "destination": "BEL",
                }]},
            },
            {
                "action": "submit_orders",
                "params": {"orders": [{
                    "type": "MOVE",
                    "origin": "LVP",
                    "destination": "BEL",
                    "via_convoy": "false",
                }]},
            },
        )
        for move in invalid_moves:
            with self.subTest(move=move):
                self.assertTrue(offline_check.check_move(fixture, move))

    def test_checker_matches_inferred_convoy_and_split_coast_aliases(self):
        fixture = self.fixture("diplomacy_movement")
        inferred_convoy = {
            "action": "submit_orders",
            "params": {"orders": [{
                "type": "MOVE",
                "origin": "LVP",
                "destination": "BEL",
            }]},
        }
        self.assertEqual(offline_check.check_move(fixture, inferred_convoy), [])

        coast_fixture = json.loads(json.dumps(fixture))
        coast_fixture["legal_actions"][0]["hint"]["legal_orders"] = [{
            "origin": "BAR",
            "unit_type": "F",
            "can_hold": True,
            "move_destinations": ["NWG", "NWY", "STP/NC"],
            "can_move_via_convoy": False,
            "support_options": [],
            "can_support": False,
            "can_convoy": True,
        }]
        unambiguous_coast = {
            "action": "submit_orders",
            "params": {"orders": [{
                "type": "MOVE",
                "origin": "BAR",
                "destination": "STP",
            }]},
        }
        self.assertEqual(offline_check.check_move(coast_fixture, unambiguous_coast), [])

        wrong_explicit_coast = json.loads(json.dumps(unambiguous_coast))
        wrong_explicit_coast["params"]["orders"][0]["destination"] = "STP/SC"
        self.assertTrue(offline_check.check_move(coast_fixture, wrong_explicit_coast))

        coast_fixture["legal_actions"][0]["hint"]["legal_orders"] = [{
            "origin": "STP/SC",
            "unit_type": "F",
            "can_hold": True,
            "move_destinations": ["BOT", "FIN", "LVN"],
            "can_move_via_convoy": False,
            "support_options": [],
            "can_support": False,
            "can_convoy": False,
        }]
        base_origin = {
            "action": "submit_orders",
            "params": {"orders": [{
                "type": "MOVE",
                "origin": "STP",
                "destination": "BOT",
            }]},
        }
        self.assertEqual(offline_check.check_move(coast_fixture, base_origin), [])

        coast_fixture["legal_actions"][0]["hint"]["legal_orders"] = [{
            "origin": "MAO",
            "unit_type": "F",
            "can_hold": True,
            "move_destinations": ["SPA/NC", "SPA/SC"],
            "can_move_via_convoy": False,
            "support_options": [],
            "can_support": False,
            "can_convoy": True,
        }]
        ambiguous_coast = {
            "action": "submit_orders",
            "params": {"orders": [{
                "type": "MOVE",
                "origin": "MAO",
                "destination": "SPA",
            }]},
        }
        self.assertTrue(offline_check.check_move(coast_fixture, ambiguous_coast))

    def test_checker_normalizes_press_recipients_and_build_sites(self):
        negotiation = self.fixture("diplomacy_negotiation")
        negotiation["legal_actions"][0]["hint"]["max_messages"] = 1
        press = {
            "action": "send_press",
            "params": {"messages": [{"to_power": "GLOBAL", "content": "Hello"}]},
        }
        self.assertEqual(
            offline_check.check_move(negotiation, press),
            [],
        )

        adjustment = self.fixture("diplomacy_adjustment")
        adjustment["legal_actions"][0]["hint"]["legal_orders"] = [{
            "builds_required": 2,
            "disbands_required": 0,
            "build_sites": [
                {
                    "destination": "LON",
                    "unit_types": ["A", "F"],
                    "fleet_destinations": ["LON"],
                },
                {
                    "destination": "STP",
                    "unit_types": ["A", "F"],
                    "fleet_destinations": ["STP/NC", "STP/SC"],
                },
            ],
            "can_waive": True,
        }]
        normalized = {
            "action": "submit_adjustments",
            "params": {"orders": [
                {"type": "BUILD", "destination": "lon", "unit_type": "A"},
                {"type": "BUILD", "destination": "stp(sc)", "unit_type": "F"},
            ]},
        }
        self.assertEqual(offline_check.check_move(adjustment, normalized), [])

        malformed = json.loads(json.dumps(normalized))
        malformed["params"]["orders"][0]["destination"] = ["LON"]
        self.assertTrue(offline_check.check_move(adjustment, malformed))

    def test_diplomacy_fixtures_are_phase_coherent_generated_contracts(self):
        phase_code = {
            "negotiation": "M",
            "movement": "M",
            "retreat": "R",
            "adjustment": "A",
        }
        season_code = {"SPRING": "S", "FALL": "F", "WINTER": "W"}
        for phase in phase_code:
            with self.subTest(phase=phase):
                fixture = self.fixture(f"diplomacy_{phase}")
                state = fixture["state"]
                expected_phase_id = (
                    f"{season_code[state['season']]}{state['year']}{phase_code[phase]}"
                )
                self.assertEqual(
                    fixture["_fixture"]["generator"],
                    "scripts/generate_diplomacy_kit_fixtures.py",
                )
                self.assertEqual(state["phase_id"], expected_phase_id)
                self.assertEqual(state["phase"], state["phase_key"])
                self.assertTrue(state["phase_key"].startswith(expected_phase_id))
                if phase != "negotiation":
                    self.assertNotIn("legal_orders", state)
                    self.assertNotIn("order_schema", state)
                    self.assertTrue(fixture["legal_actions"][0]["hint"]["legal_orders"])

    def test_checker_rejects_a_non_hinted_direct_move(self):
        fixture = self.fixture("diplomacy_movement")
        move = {
            "action": "submit_orders",
            "params": {
                "orders": [
                    {"type": "MOVE", "origin": "EDI", "destination": "PAR"},
                    {"type": "HOLD", "origin": "LON"},
                    {"type": "HOLD", "origin": "LVP"},
                ],
            },
        }

        problems = offline_check.check_move(fixture, move)
        self.assertEqual(len(problems), 1)
        self.assertIn("is not hinted", problems[0])

    def test_all_diplomacy_phase_fixtures_accept_the_safe_heuristic(self):
        for name in (
            "diplomacy_negotiation",
            "diplomacy_movement",
            "diplomacy_retreat",
            "diplomacy_adjustment",
        ):
            with self.subTest(name=name):
                fixture = self.fixture(name)
                move = heuristic_agent.decide(
                    offline_check.runner_state(fixture),
                    fixture["legal_actions"],
                )
                self.assertEqual(offline_check.check_move(fixture, move), [])


class DiplomacyRuntimeValidationTests(unittest.TestCase):
    """The live decision path shares check.py's validator and degrades safely."""

    def setUp(self):
        llm_agent._reset_session()

    def tearDown(self):
        llm_agent._reset_session()

    def fixture(self, name):
        return json.loads((KIT_DIR / "fixtures" / f"{name}.json").read_text())

    def hint(self, fixture):
        return fixture["legal_actions"][0]["hint"]

    def test_degrade_replaces_only_offending_orders_and_keeps_the_rest(self):
        movement = self.hint(self.fixture("diplomacy_movement"))
        params, notes = helpers.degrade_diplomacy_batch("submit_orders", {"orders": [
            {"type": "MOVE", "origin": "EDI", "destination": "PAR"},
            {"type": "SUPPORT", "origin": "LON", "target": "LVP", "destination": "YOR"},
            {"type": "HOLD", "origin": "LVP"},
        ]}, movement)
        self.assertEqual(params["orders"], [
            {"type": "HOLD", "origin": "EDI"},
            {"type": "SUPPORT", "origin": "LON", "target": "LVP", "destination": "YOR"},
            {"type": "HOLD", "origin": "LVP"},
        ])
        self.assertEqual(len(notes), 1)
        self.assertIn("degraded to HOLD", notes[0])
        self.assertEqual(
            helpers.diplomacy_batch_problems("submit_orders", params, movement),
            [],
        )

    def test_degrade_disbands_a_bad_retreat_and_waives_a_bad_build(self):
        retreat = self.hint(self.fixture("diplomacy_retreat"))
        params, notes = helpers.degrade_diplomacy_batch("submit_retreats", {"orders": [
            {"type": "RETREAT", "origin": "BEL", "destination": "LON"},
        ]}, retreat)
        self.assertEqual(params["orders"], [{"type": "DISBAND", "origin": "BEL"}])
        self.assertEqual(len(notes), 1)
        self.assertIn("degraded to DISBAND", notes[0])

        adjustment = self.hint(self.fixture("diplomacy_adjustment"))
        params, notes = helpers.degrade_diplomacy_batch("submit_adjustments", {"orders": [
            {"type": "BUILD", "destination": "LON", "unit_type": "A"},
            {"type": "BUILD", "destination": "PAR", "unit_type": "A"},
        ]}, adjustment)
        self.assertEqual(params["orders"], [
            {"type": "BUILD", "destination": "LON", "unit_type": "A"},
            {"type": "WAIVE"},
        ])
        self.assertEqual(len(notes), 1)
        self.assertIn("degraded to WAIVE", notes[0])

    def test_degrade_drops_unsalvageable_entries_for_hint_legal_defaults(self):
        # Unknown origin: a HOLD there would still be illegal, so drop it.
        movement = self.hint(self.fixture("diplomacy_movement"))
        params, notes = helpers.degrade_diplomacy_batch("submit_orders", {"orders": [
            {"type": "MOVE", "origin": "PAR", "destination": "BUR"},
        ]}, movement)
        self.assertEqual(params["orders"], [])
        self.assertIn("dropped", notes[0])

        # Forced removals allow no WAIVE; the server's deterministic civil
        # disorder covers a dropped disband.
        forced = {"legal_orders": [{"disbands_required": 1, "origins": ["BER", "KIE"]}]}
        params, notes = helpers.degrade_diplomacy_batch("submit_adjustments", {"orders": [
            {"type": "DISBAND", "origin": "MUN"},
        ]}, forced)
        self.assertEqual(params["orders"], [])
        self.assertIn("dropped", notes[0])

    def test_degrade_keeps_press_but_removes_invalid_optional_metadata(self):
        fixture = self.fixture("diplomacy_negotiation")
        hint = self.hint(fixture)
        hint["max_messages"] = 1
        target = hint["recipient_powers"][0]
        params, notes = helpers.degrade_diplomacy_batch("send_press", {
            "messages": [{
                "to_power": target,
                "content": "Concrete non-aggression proposal.",
                "proposal": {
                    "kind": "dmz",
                    "provinces": ["NOR"],
                },
            }],
            "strategy_intent": {"avoid_provinces": ["NOR"]},
        }, hint)

        self.assertEqual(params, {
            "messages": [{
                "to_power": target,
                "content": "Concrete non-aggression proposal.",
            }],
        })
        self.assertTrue(any("proposal metadata removed" in note for note in notes))
        self.assertTrue(any("strategy_intent removed" in note for note in notes))
        self.assertEqual(
            helpers.diplomacy_batch_problems("send_press", params, hint),
            [],
        )

    def _decide(self, fixture, replies):
        state = offline_check.runner_state(fixture)
        chat = mock.Mock(side_effect=list(replies))
        with (
            mock.patch.object(llm_agent.memory, "current_match_id", return_value=51),
            mock.patch.object(
                llm_agent,
                "_llm_config",
                return_value=("https://llm.example/v1", "key", "model"),
            ),
            mock.patch.object(llm_agent, "_model_context_window", return_value=0),
            mock.patch.object(llm_agent, "_chat_request", chat),
            io.StringIO() as captured,
            mock.patch.object(sys, "stdout", captured),
        ):
            move = llm_agent.decide(state, fixture["legal_actions"])
            log = captured.getvalue()
        return move, chat, log

    def test_invalid_batch_degrades_without_corrective_provider_retry(self):
        fixture = self.fixture("diplomacy_movement")
        bad = json.dumps({"action": "submit_orders", "params": {"orders": [
            {"type": "MOVE", "origin": "EDI", "destination": "PAR"},
        ]}})
        move, chat, log = self._decide(fixture, [bad])

        self.assertEqual(move["params"]["orders"], [
            {"type": "HOLD", "origin": "EDI"},
        ])
        self.assertEqual(chat.call_count, 1)
        self.assertIn("without reinference", log)
        # Official bounded mode never retains a provider transcript. The
        # degraded payload is returned to the runner, while durable continuity
        # is committed through my_memory only after the server ACKs it.
        self.assertEqual(llm_agent._SESSION["messages"], [])

    def test_invalid_batch_degrades_only_the_offending_orders(self):
        fixture = self.fixture("diplomacy_movement")
        bad = json.dumps({"action": "submit_orders", "params": {"orders": [
            {"type": "MOVE", "origin": "EDI", "destination": "PAR"},
            {"type": "HOLD", "origin": "LON"},
        ]}})

        move, chat, log = self._decide(fixture, [bad])

        self.assertEqual(chat.call_count, 1)
        self.assertEqual(move["action"], "submit_orders")
        self.assertEqual(move["params"]["orders"], [
            {"type": "HOLD", "origin": "EDI"},
            {"type": "HOLD", "origin": "LON"},
        ])
        self.assertIn("degraded without reinference", log)
        self.assertEqual(
            offline_check.check_move(fixture, move),
            [],
        )

    def test_valid_batches_and_other_games_use_one_provider_call(self):
        fixture = self.fixture("diplomacy_movement")
        good = json.dumps({"action": "submit_orders", "params": {"orders": [
            {"type": "MOVE", "origin": "EDI", "destination": "NTH"},
        ]}})
        move, chat, _log = self._decide(fixture, [good])
        self.assertEqual(chat.call_count, 1)
        self.assertEqual(move["params"]["orders"], [
            {"type": "MOVE", "origin": "EDI", "destination": "NTH"},
        ])

        llm_agent._reset_session()
        liars = self.fixture("liars_opening")
        bid = json.dumps({"action": "bid", "params": {"quantity": 2, "face": 3}})
        move, chat, _log = self._decide(liars, [bid])
        self.assertEqual(chat.call_count, 1)
        self.assertEqual(move["action"], "bid")


class OfflineMockCliTests(unittest.TestCase):
    def test_mock_arena_never_runs_live_model_preflight(self):
        env = os.environ.copy()
        for name in (
            "LLM_API_KEY",
            "CLAWARENA_GATEWAY_KEY",
            "CLAWARENA_SKIP_PREFLIGHT",
        ):
            env.pop(name, None)

        result = subprocess.run(
            [sys.executable, str(KIT_DIR / "mock_arena.py")],
            cwd=KIT_DIR,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("MOCK PASS", result.stdout)

    def test_every_diplomacy_phase_fixture_runs_through_the_real_runner_loop(self):
        env = os.environ.copy()
        for name in (
            "LLM_API_KEY",
            "CLAWARENA_GATEWAY_KEY",
            "CLAWARENA_SKIP_PREFLIGHT",
        ):
            env.pop(name, None)

        for phase in ("negotiation", "movement", "retreat", "adjustment"):
            name = f"diplomacy_{phase}"
            with self.subTest(name=name):
                result = subprocess.run(
                    [sys.executable, str(KIT_DIR / "mock_arena.py"), name],
                    cwd=KIT_DIR,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )

                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(f"MOCK PASS [{name}]", result.stdout)


class HermesTests(unittest.TestCase):
    def setUp(self):
        # The historical session tests remain coverage for opt-in compatibility;
        # new live gameplay defaults to the bounded stateless path.
        self._session_mode = mock.patch.object(
            hermes_agent,
            "HERMES_STATELESS_GAMEPLAY",
            False,
        )
        self._session_mode.start()

    def tearDown(self):
        self._session_mode.stop()

    def test_gameplay_max_tokens_is_bounded(self):
        with mock.patch.dict(os.environ, {"CLAWARENA_HERMES_MAX_TOKENS": "512"}):
            self.assertEqual(hermes_agent._gameplay_max_tokens(), 512)
        with mock.patch.dict(os.environ, {"CLAWARENA_HERMES_MAX_TOKENS": "9999"}):
            self.assertEqual(hermes_agent._gameplay_max_tokens(), 8000)
        with mock.patch.dict(os.environ, {"CLAWARENA_HERMES_MAX_TOKENS": "bad"}):
            self.assertEqual(hermes_agent._gameplay_max_tokens(), 8000)

    def test_preflight_requires_a_real_model_reply(self):
        with mock.patch.object(
            hermes_agent,
            "_run_chat",
            return_value=("CLAWARENA_READY", "preflight-session"),
        ) as run_chat:
            result = hermes_agent.preflight()

        self.assertIn("Hermes", result)
        self.assertIsNone(run_chat.call_args.args[1])

    def test_preflight_rejects_an_empty_model_reply(self):
        with (
            mock.patch.object(hermes_agent, "_run_chat", return_value=("", None)),
            self.assertRaisesRegex(RuntimeError, "empty model reply"),
        ):
            hermes_agent.preflight()

    def test_delta_marks_removed_fields_and_keeps_language(self):
        previous = {
            "game_type": "liars_dice",
            "phase": "turn",
            "last_bid": {"quantity": 2, "face": 4},
            "chat_log": [{"message": "one"}],
        }
        current = {
            "game_type": "liars_dice",
            "phase": "turn",
            "chat_log": [{"message": "one"}, {"message": "two"}],
            "message_language": "ko",
        }
        old_last = dict(hermes_agent._LAST)
        try:
            hermes_agent._LAST.update(sid="session-1", board=previous)
            prompt = hermes_agent._build_prompt(
                current,
                [{"action": "challenge"}],
                "session-1",
                hermes_agent._board(current),
            )
        finally:
            hermes_agent._LAST.clear()
            hermes_agent._LAST.update(old_last)

        payload = json.loads(prompt.rsplit("GAME:\n", 1)[1])
        self.assertTrue(prompt.startswith(hermes_agent._RESUMED_CONTRACT))
        self.assertFalse(prompt.startswith(llm_agent.SYSTEM_PROMPT))
        self.assertEqual(payload["state_removed"], ["last_bid"])
        self.assertEqual(
            payload["state_delta"]["chat_log"],
            {"_appended": [{"message": "two"}]},
        )
        self.assertEqual(payload["message_language"], "ko")

    def test_hermes_full_contract_is_first_turn_only(self):
        state = {"game_type": "liars_dice", "phase": "turn", "last_bid": None}
        legal = [{"action": "bid", "params": {"quantity": 1, "face": 2}}]
        board = hermes_agent._board(state)
        old_last = dict(hermes_agent._LAST)
        try:
            first = hermes_agent._build_prompt(state, legal, None, board)
            hermes_agent._LAST.update(
                sid="session-1",
                board=board,
                turn_count=1,
            )
            resumed = hermes_agent._build_prompt(state, legal, "session-1", board)
            hermes_agent._LAST["turn_count"] = 100
            long_running = hermes_agent._build_prompt(state, legal, "session-1", board)
        finally:
            hermes_agent._LAST.clear()
            hermes_agent._LAST.update(old_last)

        self.assertTrue(first.startswith(llm_agent.SYSTEM_PROMPT))
        self.assertTrue(resumed.startswith(hermes_agent._RESUMED_CONTRACT))
        self.assertTrue(long_running.startswith(hermes_agent._RESUMED_CONTRACT))

    def test_hermes_corrects_invalid_diplomacy_metadata_in_the_same_session(self):
        fixture = json.loads((KIT_DIR / "fixtures" / "diplomacy_negotiation.json").read_text())
        state = offline_check.runner_state(fixture)
        target = fixture["legal_actions"][0]["hint"]["recipient_powers"][0]
        bad = json.dumps({
            "action": "send_press",
            "params": {
                "messages": [{"to_power": target, "content": "Hold the north."}],
                "strategy_intent": {"avoid_provinces": ["NOR"]},
            },
        })
        good = json.dumps({
            "action": "send_press",
            "params": {
                "messages": [{"to_power": target, "content": "Hold the north."}],
                "strategy_intent": {"avoid_provinces": ["NWY"]},
            },
        })
        calls = []
        saved_counts = []
        old_last = dict(hermes_agent._LAST)

        def fake_chat(prompt, session_id, timeout):
            calls.append((prompt, session_id, timeout))
            return (bad, "diplomacy-session") if len(calls) == 1 else (
                good,
                "diplomacy-session",
            )

        try:
            hermes_agent._LAST.update(sid=None, board=None, turn_count=0)
            with (
                mock.patch.object(hermes_agent.memory, "get_hermes_session", return_value=None),
                mock.patch.object(hermes_agent.memory, "get_hermes_session_turn_count", return_value=0),
                mock.patch.object(hermes_agent.memory, "set_hermes_session"),
                mock.patch.object(
                    hermes_agent.memory,
                    "set_hermes_session_turn_count",
                    side_effect=saved_counts.append,
                ),
                mock.patch.object(hermes_agent, "_run_chat", side_effect=fake_chat),
            ):
                move = hermes_agent.decide(state, fixture["legal_actions"])
        finally:
            hermes_agent._LAST.clear()
            hermes_agent._LAST.update(old_last)

        self.assertEqual(move["params"]["strategy_intent"]["avoid_provinces"], ["NWY"])
        self.assertEqual([call[1] for call in calls], [None, "diplomacy-session"])
        self.assertIn("Canonical Claw Diplomacy rules", calls[0][0])
        self.assertIn("Norway is NWY, never NOR", calls[0][0])
        self.assertIn("SERVER_CONTRACT_PREFLIGHT_REJECTED", calls[1][0])
        self.assertEqual(saved_counts, [1, 2])

    def test_docker_invocation_wraps_inner_process_timeout(self):
        with (
            mock.patch.object(hermes_agent, "HERMES_CONTAINER", "hermes-test"),
            mock.patch.object(hermes_agent, "HERMES_BIN", "/opt/hermes/bin/hermes"),
        ):
            command = hermes_agent._invoke(60)

        self.assertEqual(
            command,
            [
                "docker", "exec", "hermes-test", "timeout", "--signal=TERM",
                "--kill-after=5s", "60s", "/opt/hermes/bin/hermes",
            ],
        )

    def test_docker_invocation_preserves_fractional_gameplay_timeout(self):
        with (
            mock.patch.object(hermes_agent, "HERMES_CONTAINER", "hermes-test"),
            mock.patch.object(hermes_agent, "HERMES_BIN", "/opt/hermes/bin/hermes"),
        ):
            command = hermes_agent._invoke(43.9, gameplay=True)

        self.assertEqual(command[command.index("--kill-after=5s") + 1], "43.9s")

    def test_docker_gameplay_invocation_uses_isolated_hermes_home(self):
        with (
            mock.patch.object(hermes_agent, "HERMES_CONTAINER", "hermes-test"),
            mock.patch.object(hermes_agent, "HERMES_BIN", "/opt/hermes/bin/hermes"),
            mock.patch.object(
                hermes_agent, "HERMES_GAMEPLAY_HOME", "/opt/data/.clawarena/gameplay",
            ),
        ):
            command = hermes_agent._invoke(40, gameplay=True)

        self.assertEqual(command[:8], [
            "docker", "exec", "-e", "HERMES_MAX_TOKENS=8000", "-e",
            "HERMES_HOME=/opt/data/.clawarena/gameplay", "hermes-test", "timeout",
        ])
        self.assertEqual(command.count("HERMES_HOME=/opt/data/.clawarena/gameplay"), 1)
        self.assertEqual(command.count("HERMES_MAX_TOKENS=8000"), 1)

    def test_gameplay_chat_uses_zero_tool_sentinel_without_yolo(self):
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            return mock.Mock(
                returncode=0,
                stdout=(
                    f"Warning: Unknown toolsets: {hermes_agent.HERMES_NO_TOOLS_SENTINEL}\n"
                    '{"action":"challenge","params":{}}\n'
                ),
                stderr="session_id: session-new\n",
            )

        with (
            mock.patch.object(hermes_agent, "HERMES_STATELESS_GAMEPLAY", False),
            mock.patch.object(hermes_agent, "HERMES_CONTAINER", ""),
            mock.patch.object(hermes_agent, "HERMES_BIN", "hermes"),
            mock.patch.object(hermes_agent.subprocess, "run", side_effect=fake_run),
        ):
            text, sid = hermes_agent._run_chat("prompt", None, 60)

        command = calls[0][0]
        self.assertEqual(command[command.index("-t") + 1], hermes_agent.HERMES_NO_TOOLS_SENTINEL)
        self.assertNotIn("--yolo", command)
        self.assertEqual(
            command[command.index("--max-turns") + 1],
            str(hermes_agent.HERMES_MAX_TURNS),
        )
        self.assertEqual(text, '{"action":"challenge","params":{}}')
        self.assertEqual(sid, "session-new")
        self.assertGreater(hermes_agent._LAST_CHAT_DIAGNOSTICS["stdout_chars"], 0)
        self.assertTrue(hermes_agent._LAST_CHAT_DIAGNOSTICS["zero_tool_warning"])
        self.assertTrue(hermes_agent._LAST_CHAT_DIAGNOSTICS["session_marker"])
        self.assertEqual(
            hermes_agent._LAST_CHAT_DIAGNOSTICS["extracted_chars"],
            len(text),
        )

    def test_bounded_gameplay_uses_native_zero_tool_one_shot(self):
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            return mock.Mock(
                returncode=0,
                stdout=(
                    f"Warning: Unknown toolsets: {hermes_agent.HERMES_NO_TOOLS_SENTINEL}\n"
                    '{"action":"place","params":{"face":4}}\n'
                ),
                stderr="",
            )

        with (
            mock.patch.object(hermes_agent, "HERMES_STATELESS_GAMEPLAY", True),
            mock.patch.object(hermes_agent, "HERMES_CONTAINER", ""),
            mock.patch.object(hermes_agent, "HERMES_BIN", "hermes"),
            mock.patch.object(hermes_agent, "HERMES_GAMEPLAY_HOME", "/private/gameplay"),
            mock.patch.object(hermes_agent.subprocess, "run", side_effect=fake_run),
        ):
            text, sid = hermes_agent._run_chat("prompt", None, 40)

        command = calls[0][0]
        self.assertEqual(command[command.index("-z") + 1], "prompt")
        self.assertNotIn("chat", command)
        self.assertNotIn("-t", command)
        self.assertEqual(text, '{"action":"place","params":{"face":4}}')
        self.assertIsNone(sid)
        self.assertEqual(calls[0][1]["env"]["HERMES_HOME"], "/private/gameplay")
        self.assertEqual(calls[0][1]["env"]["HERMES_MAX_TOKENS"], "8000")
        self.assertEqual(
            hermes_agent._LAST_CHAT_DIAGNOSTICS["mode"],
            "native_zero_tool",
        )

    def test_gameplay_route_override_does_not_change_general_hermes_calls(self):
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            return mock.Mock(
                returncode=0,
                stdout=(
                    f"Warning: Unknown toolsets: {hermes_agent.HERMES_NO_TOOLS_SENTINEL}\n"
                    '{"action":"place","params":{"face":4}}\n'
                ),
                stderr="",
            )

        with (
            mock.patch.object(hermes_agent, "HERMES_STATELESS_GAMEPLAY", True),
            mock.patch.object(hermes_agent, "HERMES_CONTAINER", ""),
            mock.patch.object(hermes_agent, "HERMES_BIN", "hermes"),
            mock.patch.object(hermes_agent, "HERMES_MODEL", "deepseek-v4-flash"),
            mock.patch.object(hermes_agent, "HERMES_PROVIDER", "deepseek"),
            mock.patch.object(hermes_agent, "HERMES_GAMEPLAY_MODEL", "gemini-3.6-flash"),
            mock.patch.object(hermes_agent, "HERMES_GAMEPLAY_PROVIDER", "gemini"),
            mock.patch.object(hermes_agent.subprocess, "run", side_effect=fake_run),
        ):
            hermes_agent._run_chat("game", None, 40)
            hermes_agent._run_chat("general", None, 40, gameplay=False)

        gameplay_command = calls[0][0]
        self.assertIn("-z", gameplay_command)
        self.assertEqual(gameplay_command[gameplay_command.index("-m") + 1], "gemini-3.6-flash")
        self.assertEqual(gameplay_command[gameplay_command.index("--provider") + 1], "gemini")
        general_command = calls[1][0]
        self.assertNotIn("-z", general_command)
        self.assertEqual(general_command[general_command.index("-m") + 1], "deepseek-v4-flash")
        self.assertEqual(general_command[general_command.index("--provider") + 1], "deepseek")

    def test_live_sized_prompt_and_memory_are_bounded_without_raw_logging(self):
        state = {
            "game_type": "las_vegas",
            "round": 2,
            "total_rounds": 4,
            "current_roll": [1, 2, 2, 3, 4, 5, 6, 6],
            "your_roll": [1, 2, 2, 3, 4, 5, 6, 6],
            "casinos": [{"number": i, "bills": [70000, 20000]} for i in range(1, 7)],
            "players": {str(i): {"dice_remaining": 8, "money": 0} for i in range(4)},
            "game_rules_brief": {"rules": ["r" * 220 for _ in range(11)]},
            "user_preferences": {"strategy_hint": "p" * 1000, "risk_profile": "balanced"},
            "my_memory": {
                "my_recent_moves": [
                    {"turn": i, "note": f"move-{i}-" + ("m" * 700)}
                    for i in range(40)
                ],
                "my_private_reads": ["x" * 700 for _ in range(20)],
            },
            "_action_window_id": "live-sized-window",
            "_decision_budget_configured_seconds": 90.0,
            "_decision_budget_seconds": 90.0,
            "_decision_budget_policy": "hermes_deadline_reserve",
            "_server_turn_remaining_seconds": 75.49,
            "_submit_reserve_seconds": 12.0,
        }
        legal = [{
            "action": "place",
            "hint": {
                "faces_available": [{"face": i, "casino": i} for i in range(1, 7)],
                "contract_note": "l" * 1200,
            },
        }]

        prompt = hermes_agent._bounded_gameplay_prompt(state, legal)
        payload = json.loads(prompt.rsplit("GAME:\n", 1)[1])
        with (
            mock.patch.object(
                hermes_agent, "HERMES_GAMEPLAY_REASONING_EFFORT", "low",
            ),
            mock.patch.object(
                hermes_agent, "HERMES_GAMEPLAY_THINKING_MODE", "enabled",
            ),
        ):
            provenance = hermes_agent._prompt_provenance(prompt, state)

        # A large my_memory in the incoming server state must not reach the
        # prompt at all now -- not bounded, not projected, not smuggled in
        # through the board. The 40 fat moves above are the bait.
        self.assertNotIn("my_memory", payload)
        self.assertNotIn("move-39", json.dumps(payload))
        self.assertLess(provenance["prompt_bytes"], 20000)
        self.assertEqual(provenance["action_window_id"], "live-sized-window")
        self.assertEqual(provenance["reasoning_profile"], "clawarena_low_reasoning")
        self.assertEqual(provenance["reasoning_effort"], "low")
        self.assertEqual(provenance["thinking_mode"], "enabled")
        self.assertEqual(provenance["provider"], "profile_default")
        self.assertEqual(provenance["model"], "profile_default")
        self.assertEqual(provenance["max_output_tokens"], 8000)
        self.assertEqual(provenance["configured_budget_seconds"], 90.0)
        self.assertEqual(provenance["effective_budget_seconds"], 90.0)
        self.assertEqual(provenance["server_remaining_seconds"], 75.49)
        self.assertEqual(provenance["submit_reserve_seconds"], 12.0)
        self.assertEqual(provenance["decision_budget_policy"], "hermes_deadline_reserve")
        self.assertNotIn("move-39", json.dumps(provenance))

    def test_bounded_hermes_consumes_server_decision_context(self):
        legal = [{"action": "choose", "params": {"option": "string"}}]
        state = {
            "game_type": "future_game",
            "_decision_context": {
                "version": 1,
                "game_type": "future_game",
                "snapshot_mode": "turn",
                "state": {"new_mechanic": {"resource": 7}},
                "legal_actions": legal,
                "rules": None,
                "strategy": None,
                "user_preferences": {},
            },
        }

        prompt = hermes_agent._bounded_gameplay_prompt(state, legal)
        payload = json.loads(prompt.rsplit("GAME:\n", 1)[1])

        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["turn"]["state"]["new_mechanic"], {"resource": 7})
        self.assertEqual(payload["turn"]["legal_actions"], legal)

    def test_bounded_gameplay_uses_one_provider_attempt_then_legal_fallback(self):
        state = {
            "game_type": "las_vegas",
            "_action_window_id": "window-1",
            "_decision_budget_configured_seconds": 60,
            "_decision_budget_seconds": 60,
            "_decision_budget_policy": "hermes_deadline_reserve",
            "_server_turn_remaining_seconds": 75,
            "_submit_reserve_seconds": 12,
        }
        legal = [{
            "action": "place",
            "hint": {"faces_available": [{"face": 4}]},
        }]
        calls = []

        def fake_chat(prompt, session_id, timeout):
            calls.append((prompt, session_id, timeout))
            raise TimeoutError("provider stalled")

        before = dict(hermes_agent._COUNTS)
        try:
            with (
                mock.patch.object(hermes_agent, "HERMES_STATELESS_GAMEPLAY", True),
                mock.patch.object(hermes_agent, "_run_chat", side_effect=fake_chat),
            ):
                move = hermes_agent.decide(state, legal)
                provider_after = hermes_agent._COUNTS["provider_attempts"]
        finally:
            hermes_agent._COUNTS.update(before)

        self.assertEqual(move, {"action": "place", "params": {"face": 4}})
        self.assertEqual(len(calls), 1)
        self.assertEqual([call[1] for call in calls], [None])
        self.assertTrue(all(call[2] <= hermes_agent.HERMES_ATTEMPT_TIMEOUT for call in calls))
        self.assertEqual(calls[0][2], 59.8)
        self.assertEqual(
            provider_after,
            before.get("provider_attempts", 0) + 1,
        )
        self.assertIn("Choose exactly one move", calls[0][0])

    def test_gameplay_chat_prefers_final_json_after_latest_hermes_reasoning_recap(self):
        proc = mock.Mock(
            returncode=0,
            stdout=(
                f"Warning: Unknown toolsets: {hermes_agent.HERMES_NO_TOOLS_SENTINEL}\n"
                "┌─ Reasoning ─────────────────────────┐\n"
                'A candidate looked like {\"quantity\":4,\"face\":3}.\n'
                '{"action":"bid","params":{"quantity":4,"face":3}}\n'
            ),
            stderr="session_id: latest-hermes\n",
        )
        with mock.patch.object(hermes_agent.subprocess, "run", return_value=proc):
            text, sid = hermes_agent._run_chat("prompt", None, 60)

        self.assertEqual(
            text,
            '{"action":"bid","params":{"quantity":4,"face":3}}',
        )
        self.assertEqual(sid, "latest-hermes")

    def test_gameplay_chat_keeps_plain_preflight_after_reasoning_recap(self):
        proc = mock.Mock(
            returncode=0,
            stdout=(
                f"Warning: Unknown toolsets: {hermes_agent.HERMES_NO_TOOLS_SENTINEL}\n"
                "┌─ Reasoning ─────────────────────────┐\n"
                "The requested readiness token is straightforward.\n"
                "CLAWARENA_READY\n"
            ),
            stderr="session_id: latest-hermes-preflight\n",
        )
        # expects_json=False is what preflight() actually passes: it asks for a
        # readiness word, not an object. The gameplay contract is the opposite
        # and is pinned by the test below.
        with mock.patch.object(hermes_agent.subprocess, "run", return_value=proc):
            text, sid = hermes_agent._run_chat(
                "prompt", None, 60, expects_json=False,
            )

        self.assertEqual(text, "CLAWARENA_READY")
        self.assertEqual(sid, "latest-hermes-preflight")

    def test_a_gameplay_turn_with_no_object_reads_as_empty_not_as_chrome(self):
        """A model that says nothing must not be quoted as having said a banner.

        When the completion is all reasoning and no content, Hermes' stdout
        holds only its own status lines. Returning those handed a 103-byte CLI
        warning onward as the model's reply, where it failed to parse and was
        filed as malformed_content -- so an empty turn was indistinguishable
        from a formatting mistake. It is 12 of the 13 residual "malformed"
        turns in match 1458.
        """

        proc = mock.Mock(
            returncode=0,
            stdout=(
                f"Warning: Unknown toolsets: {hermes_agent.HERMES_NO_TOOLS_SENTINEL}\n"
                "  ⚠ tirith security scanner enabled but not available "
                "— command scanning will use pattern matching only\n"
            ),
            stderr="session_id: empty-turn\n",
        )
        with mock.patch.object(hermes_agent.subprocess, "run", return_value=proc):
            text, sid = hermes_agent._run_chat("prompt", None, 60)

        self.assertEqual(text, "")
        self.assertEqual(sid, "empty-turn")

    def test_a_gameplay_turn_still_returns_its_object_alongside_chrome(self):
        proc = mock.Mock(
            returncode=0,
            stdout=(
                f"Warning: Unknown toolsets: {hermes_agent.HERMES_NO_TOOLS_SENTINEL}\n"
                "  ⚠ tirith security scanner enabled but not available\n"
                '{"action":"challenge","params":{}}\n'
            ),
            stderr="session_id: real-turn\n",
        )
        with mock.patch.object(hermes_agent.subprocess, "run", return_value=proc):
            text, _sid = hermes_agent._run_chat("prompt", None, 60)

        self.assertEqual(text, '{"action":"challenge","params":{}}')

    def test_gameplay_chat_fails_closed_when_hermes_does_not_confirm_zero_tools(self):
        proc = mock.Mock(returncode=0, stdout="MODEL_REPLY\n", stderr="session_id: unsafe\n")
        with mock.patch.object(hermes_agent.subprocess, "run", return_value=proc):
            with self.assertRaisesRegex(RuntimeError, "zero-tool gameplay selection"):
                hermes_agent._run_chat("safe preflight", None, 60)

    def test_missing_hermes_session_recovers_with_a_fresh_full_baseline(self):
        state = {"game_type": "liars_dice", "phase": "turn", "dice_count": 3}
        legal = [{"action": "challenge", "params": {}}]
        prompts = []
        saved = []
        cleared = []
        old_last = dict(hermes_agent._LAST)

        def fake_chat(prompt, session_id, timeout):
            prompts.append((prompt, session_id, timeout))
            if session_id:
                raise RuntimeError("hermes chat rc=1: Session not found: stale-session")
            return '{"action":"challenge","params":{}}', "fresh-session"

        try:
            hermes_agent._LAST.update(sid="stale-session", board={"phase": "old"})
            with (
                mock.patch.object(hermes_agent.memory, "get_hermes_session", return_value="stale-session"),
                mock.patch.object(hermes_agent.memory, "get_hermes_session_turn_count", return_value=3),
                mock.patch.object(hermes_agent.memory, "clear_hermes_session", side_effect=lambda: cleared.append(True)),
                mock.patch.object(hermes_agent.memory, "set_hermes_session", side_effect=saved.append),
                mock.patch.object(hermes_agent.memory, "set_hermes_session_turn_count"),
                mock.patch.object(hermes_agent, "_run_chat", side_effect=fake_chat),
            ):
                move = hermes_agent.decide(state, legal)
        finally:
            hermes_agent._LAST.clear()
            hermes_agent._LAST.update(old_last)

        self.assertEqual(move, {"action": "challenge", "params": {}})
        self.assertEqual(cleared, [True])
        self.assertEqual(saved, ["fresh-session"])
        self.assertEqual([session_id for _, session_id, _ in prompts], ["stale-session", None])
        fresh_payload = json.loads(prompts[1][0].rsplit("GAME:\n", 1)[1])
        self.assertIn("state", fresh_payload)
        self.assertNotIn("state_delta", fresh_payload)

    def test_hermes_keeps_long_running_session_without_turn_rotation(self):
        state = {
            "game_type": "monopoly",
            "phase": "turn",
            "my_memory": {"my_recent_moves": [{"action": "roll"}]},
        }
        legal = [{"action": "roll", "params": {}}]
        prompts = []
        saved_sessions = []
        saved_counts = []
        old_last = dict(hermes_agent._LAST)

        def fake_chat(prompt, session_id, timeout):
            prompts.append((prompt, session_id, timeout))
            return '{"action":"roll","params":{}}', session_id

        try:
            hermes_agent._LAST.update(
                sid="full-segment",
                board={"phase": "old"},
                turn_count=100,
                # Just re-baselined, so this turn is an ordinary delta turn.
                # The periodic full baseline is exercised separately.
                last_full_turn=100,
                full_failures=0,
            )
            with (
                mock.patch.object(hermes_agent.memory, "get_hermes_session", return_value="full-segment"),
                mock.patch.object(
                    hermes_agent.memory,
                    "get_hermes_session_turn_count",
                    return_value=100,
                ),
                mock.patch.object(
                    hermes_agent.memory,
                    "set_hermes_session",
                    side_effect=saved_sessions.append,
                ),
                mock.patch.object(
                    hermes_agent.memory,
                    "set_hermes_session_turn_count",
                    side_effect=saved_counts.append,
                ),
                mock.patch.object(hermes_agent, "_run_chat", side_effect=fake_chat),
            ):
                move = hermes_agent.decide(state, legal)
        finally:
            hermes_agent._LAST.clear()
            hermes_agent._LAST.update(old_last)

        self.assertEqual(move, {"action": "roll", "params": {}})
        self.assertEqual(saved_sessions, ["full-segment"])
        self.assertEqual(saved_counts, [101])
        self.assertEqual(prompts[0][1], "full-segment")
        self.assertTrue(prompts[0][0].startswith(hermes_agent._RESUMED_CONTRACT))
        payload = json.loads(prompts[0][0].rsplit("GAME:\n", 1)[1])
        self.assertIn("state_delta", payload)
        # No my_memory on a resumed turn: the session transcript already holds
        # every earlier turn, so the file-backed move log is pure duplication --
        # once in the user message, once again in the transcript.
        self.assertNotIn("my_memory", payload)
        self.assertNotIn("my_memory_delta", payload)

    def test_hermes_session_diffs_the_complete_board_not_the_slim_one(self):
        """Under the delta transport the top-level board is a rolling window.

        The server's slim projection keeps a tail -- the last 16 chat entries,
        say -- while decision_context.turn.state is the whole board the
        materializer folded every delta into. Diffing the slim copy would be
        wrong twice: the session would never see what slid out of the window,
        and a window that slides by one shares no prefix with the previous one,
        so the append optimisation would miss and re-send the whole list every
        turn. The complete board only grows, which is what _appended is for.
        """

        def context(entries):
            ctx = {
                "version": 2, "profile": "session",
                "stable": {"id": "", "game_type": "mafia", "rules": {"r": ["R"]}},
                "turn": {
                    "game_type": "mafia", "action_window_id": f"w{entries}",
                    "state_mode": "full", "match_id": 7, "is_your_turn": True,
                    "state": {
                        "phase": "discuss",
                        "chat_log": [{"m": f"msg-{i}"} for i in range(entries)],
                    },
                    "legal_actions": [
                        {"action": "chat", "params": {"message": "string"}},
                    ],
                },
            }
            ctx["stable"]["id"] = decision_context.stable_context_id(ctx)
            return ctx

        def state(entries, slim_tail):
            # The top-level board is what the server sent as `snapshot=slim`:
            # only the tail. The complete board rides in the decision context.
            return {
                "game_type": "mafia", "phase": "discuss",
                "chat_log": [{"m": f"msg-{i}"} for i in slim_tail],
                "_decision_context": context(entries),
            }

        old_last = dict(hermes_agent._LAST)
        try:
            first = state(20, range(4, 20))
            board = hermes_agent._board(first)
            self.assertEqual(len(board["chat_log"]), 20,
                             "diffed the slim window instead of the whole board")

            hermes_agent._LAST.clear()
            hermes_agent._LAST.update(
                sid="s1", board=board, turn_count=2,
                last_full_turn=1, full_failures=0,
            )
            second = state(21, range(5, 21))
            prompt = hermes_agent._build_prompt(
                second, [{"action": "chat"}], "s1", hermes_agent._board(second),
            )
        finally:
            hermes_agent._LAST.clear()
            hermes_agent._LAST.update(old_last)

        payload = json.loads(prompt.rsplit("GAME:\n", 1)[1])
        # One new entry appended -- not sixteen re-sent.
        self.assertEqual(
            payload["state_delta"]["chat_log"],
            {"_appended": [{"m": "msg-20"}]},
        )

    def test_agent_can_ask_for_a_whole_board_and_it_never_reaches_the_server(self):
        """The model decides when it needs a snapshot; we only carry the request.

        There is no timer on our side re-sending boards nobody asked for. What a
        compaction keeps is the harness's business and the model's, so the model
        gets a way to say "I can no longer see part of this board" and the
        runner turns that into the next poll's resync. The flag is a request to
        US, so it must be stripped before the move is submitted -- the server
        would reject an unknown action field, and rightly.
        """

        move, why = llm_agent._parse_action(
            '{"action":"chat","params":{"message":"hi"},"need_full_state":true}',
            [{"action": "chat", "params": {"message": "string"}}],
            {"game_type": "mafia"},
        ), None
        self.assertIsNotNone(move, "the reply shape must be accepted")
        self.assertTrue(move.get("need_full_state"))

        # ... and the runner pops it off before building the submission.
        submitted = dict(move)
        requested = submitted.pop("need_full_state", False)
        self.assertTrue(requested)
        self.assertEqual(set(submitted), {"action", "params"})

    def test_a_bad_order_degrades_instead_of_costing_the_whole_turn(self):
        """The degrade path existed and could never run.

        _repair_diplomacy_move turns an offending order into a HOLD and submits
        the rest, but the decision loop calls it under `if move:` and validation
        returns None the moment the schema complains -- so the one class of
        failure it was written for was the one class it never saw. On Claw
        Diplomacy 1456 that class was the largest remaining group: orders
        matching none of the contract's oneOf branches.

        send_press is deliberately NOT degraded here. Dropping a message means
        sending silence, which is what its fallback does anyway, so the salvage
        buys nothing and would hide a model addressing a power that does not
        exist.
        """

        legal = [{
            "action": "submit_orders",
            "params_schema": {
                "type": "object",
                "required": ["orders"],
                "additionalProperties": False,
                "properties": {
                    "orders": {
                        "type": "array",
                        "items": {"oneOf": [
                            {"type": "object", "additionalProperties": False,
                             "required": ["type", "origin"],
                             "properties": {"type": {"enum": ["HOLD"]},
                                            "origin": {"enum": ["PAR", "MAR"]}}},
                            {"type": "object", "additionalProperties": False,
                             "required": ["type", "origin", "destination"],
                             "properties": {"type": {"enum": ["MOVE"]},
                                            "origin": {"enum": ["PAR", "MAR"]},
                                            "destination": {"enum": ["BUR"]}}},
                        ]},
                    },
                },
            },
            "hint": {"legal_orders": [
                {"origin": "PAR", "unit_type": "A", "can_hold": True,
                 "move_destinations": ["BUR"], "can_move_via_convoy": False,
                 "support_options": [], "can_support": False, "can_convoy": False},
                {"origin": "MAR", "unit_type": "A", "can_hold": True,
                 "move_destinations": [], "can_move_via_convoy": False,
                 "support_options": [], "can_support": False, "can_convoy": False},
            ]},
        }]

        move = llm_agent._parse_action(
            json.dumps({"action": "submit_orders", "params": {"orders": [
                {"type": "MOVE", "origin": "PAR", "destination": "BUR"},
                {"type": "MOVE", "origin": "MAR", "destination": "SPA"},
            ]}}),
            legal, {"game_type": "diplomacy"},
        )
        self.assertIsNotNone(move, "one bad order must not cost the whole turn")
        orders = move["params"]["orders"]
        # The legal order survives untouched ...
        self.assertIn(
            {"type": "MOVE", "origin": "PAR", "destination": "BUR"}, orders,
        )
        # ... and the illegal one is degraded rather than submitted or dropped.
        self.assertEqual(len(orders), 2)

    def test_a_move_without_its_envelope_is_rebuilt_only_when_unambiguous(self):
        """Models drop the {"action":..., "params":...} wrapper often enough to matter.

        After commentary keys were handled, most of the remaining Hermes losses
        looked identical: valid JSON, no action in it. Either the reply named the
        action as its only key, or it was the bare params object.

        Nothing is guessed. The reply must name the action, or the contract must
        offer exactly one legal action AND the object must look like that
        action's params -- otherwise a reply carrying nothing but prose would be
        wrapped into a valid empty move, which is an invented turn, not a
        recovered one.
        """

        one = [{"action": "send_press",
                "params": {"messages": "array", "strategy_intent": "object"}}]
        two = one + [{"action": "submit_orders", "params": {"orders": "array"}}]
        press = [{"to_power": "FRANCE", "content": "hold Burgundy"}]

        # Bare params, one legal action: wrapped, and the messages survive.
        move = llm_agent._parse_action(
            json.dumps({"messages": press}), one, {"game_type": "diplomacy"},
        )
        self.assertEqual(move, {"action": "send_press", "params": {"messages": press}})

        # The reply names the action, so two legal actions is still unambiguous.
        move = llm_agent._parse_action(
            json.dumps({"send_press": {"messages": press}}), two,
            {"game_type": "diplomacy"},
        )
        self.assertEqual(move, {"action": "send_press", "params": {"messages": press}})

        # Ambiguous: bare params with more than one action it could belong to.
        self.assertIsNone(llm_agent._parse_action(
            json.dumps({"messages": press}), two, {"game_type": "diplomacy"},
        ))

        # Prose only. Wrapping this would submit an empty press the model never
        # chose -- the first version of this repair did exactly that.
        self.assertIsNone(llm_agent._parse_action(
            json.dumps({"reasoning": "thinking about it"}), one,
            {"game_type": "diplomacy"},
        ))

    def test_commentary_beside_the_move_does_not_cancel_it(self):
        """A "reasoning" key next to a good action must not cost the turn.

        Models routinely add a top-level commentary field beside a perfectly
        valid action. The reply allowlist rejected the whole thing for it, and
        the turn was played by fallback instead. Measured on Claw Diplomacy as
        38.5% of Hermes press turns lost against the kit's 5.0% on the same
        board with the same model -- the same defect as the one inside params,
        one level up.

        The keys are DROPPED, not tolerated: nothing unrecognised should reach
        the server. What remains still has to pass every check below.
        """

        legal = [{"action": "send_press", "params": {"messages": "array"}}]
        move = llm_agent._parse_action(
            '{"action":"send_press","params":{"messages":[]},'
            '"reasoning":"France looks hostile","thought":"wait a year"}',
            legal, {"game_type": "diplomacy"},
        )
        self.assertIsNotNone(move, "commentary must not cost the turn")
        self.assertEqual(set(move), {"action", "params"})

        # A reply that is ONLY commentary is still refused -- there is no move
        # in it to keep.
        self.assertIsNone(
            llm_agent._parse_action(
                '{"reasoning":"thinking about it"}', legal,
                {"game_type": "diplomacy"},
            ),
        )
        # So is one whose params are the wrong shape.
        self.assertIsNone(
            llm_agent._parse_action(
                '{"action":"send_press","params":"oops"}', legal,
                {"game_type": "diplomacy"},
            ),
        )

    def test_optional_metadata_cannot_cancel_a_game_action(self):
        """An over-long private hint must not delete the press it rides on.

        Claw Diplomacy 1448: `strategy_intent.avoid_provinces` carried nine
        provinces against a cap of eight and the whole press batch was rejected
        -- 37 times across three seats, and ITALY spent all 40 negotiation
        rounds silent for it. strategy_intent is an optional planner hint that
        changes nothing about the messages being sent.

        Optionality is read from the contract's own `required`, never guessed,
        and an over-long list is TRIMMED rather than dropped: the first N
        entries are still what the model meant.
        """

        legal = [{
            "action": "send_press",
            "params_schema": {
                "type": "object",
                "required": ["messages"],
                "properties": {
                    "messages": {
                        "type": "array",
                        "items": {"type": "object"},
                    },
                    "strategy_intent": {
                        "type": "object",
                        "properties": {
                            "avoid_provinces": {
                                "type": "array", "maxItems": 8,
                                "items": {"type": "string"},
                            },
                        },
                    },
                },
            },
        }]
        move = {
            "action": "send_press",
            "params": {
                "messages": [{"to_power": "FRANCE", "content": "hold Burgundy"}],
                "strategy_intent": {
                    "avoid_provinces": [f"P{i}" for i in range(9)],
                },
            },
        }

        self.assertTrue(
            decision_context.validate_action_payload(move, legal),
            "the fixture must actually violate the schema",
        )
        pruned, notes = decision_context.prune_optional_violations(move, legal)
        self.assertTrue(notes)
        self.assertEqual(decision_context.validate_action_payload(pruned, legal), [])
        # The press survives intact ...
        self.assertEqual(pruned["params"]["messages"], move["params"]["messages"])
        # ... and the hint is trimmed to the cap, not thrown away.
        self.assertEqual(
            len(pruned["params"]["strategy_intent"]["avoid_provinces"]), 8,
        )

    def test_unparseable_reply_capture_is_opt_in_and_bounded(self):
        """A reply with no JSON object cannot be diagnosed from a hash.

        The provenance line records only a digest and a length on purpose: a
        game string is untrusted data and a reply can quote another player
        verbatim. But the failure that costs a Hermes turn is exactly the one a
        hash cannot explain, and it does not reproduce from a synthetic prompt --
        it only appears inside a session that has accumulated dozens of real
        turns. So the raw text can be written to a private file, and only when
        an operator names one.
        """

        prose = "I think we should approach France carefully this year."
        with tempfile.TemporaryDirectory() as tmp:
            # Off by default: nothing is written even for an unparseable reply.
            with mock.patch.object(hermes_agent, "_UNPARSED_CAPTURE_DIR", ""):
                hermes_agent._extract_final_reply(prose, output_lines=[prose])
            self.assertEqual(list(pathlib.Path(tmp).iterdir()), [])

            with (
                mock.patch.object(hermes_agent, "_UNPARSED_CAPTURE_DIR", tmp),
                mock.patch.dict(hermes_agent._UNPARSED_CAPTURED, {"n": 0}),
            ):
                hermes_agent._extract_final_reply(prose, output_lines=[prose])
                written = sorted(pathlib.Path(tmp).iterdir())
                self.assertEqual(len(written), 1)
                self.assertIn(prose, written[0].read_text())

                # A reply that DOES parse is never captured.
                hermes_agent._extract_final_reply('{"action":"chat"}')
                self.assertEqual(len(list(pathlib.Path(tmp).iterdir())), 1)

                # Neither is the preflight, whose prompt asks for a bare word.
                # Two live runs captured four identical copies of
                # "CLAWARENA_READY" before this held: the first had no filter,
                # the second keyed it on the `gameplay` flag, which the preflight
                # deliberately leaves True because it must exercise the gameplay
                # profile. What separates them is whether JSON was asked for.
                hermes_agent._extract_final_reply(
                    "CLAWARENA_READY", expects_json=False,
                )
                self.assertEqual(len(list(pathlib.Path(tmp).iterdir())), 1)

    def test_a_misfiled_hint_does_not_delete_the_message_it_rode_in_on(self):
        """A key the contract does not know is no part of the action.

        Live on Claw Diplomacy 1450: models put `strategy_intent` inside each
        press message instead of beside them, and the whole batch -- to_power,
        content and all -- was rejected for a misfiled hint. Removing a key that
        `additionalProperties: false` already says is not part of the action
        cannot change what the move does, so it is the one thing that may be
        taken out of a content item.
        """

        legal = [{
            "action": "send_press",
            "params_schema": {
                "type": "object",
                "properties": {
                    "messages": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "to_power": {"type": "string"},
                                "content": {"type": "string"},
                            },
                        },
                    },
                },
            },
        }]
        move = {
            "action": "send_press",
            "params": {"messages": [{
                "to_power": "FRANCE",
                "content": "keep Burgundy clear",
                "strategy_intent": {"avoid_provinces": ["BUR"]},
            }]},
        }

        self.assertTrue(decision_context.validate_action_payload(move, legal))
        pruned, notes = decision_context.prune_optional_violations(move, legal)
        self.assertTrue(notes)
        self.assertEqual(decision_context.validate_action_payload(pruned, legal), [])
        message = pruned["params"]["messages"][0]
        self.assertEqual(message["to_power"], "FRANCE")
        self.assertEqual(message["content"], "keep Burgundy clear")
        self.assertNotIn("strategy_intent", message)

    def test_pruning_never_drops_the_moves_own_content(self):
        """A move stripped of its orders validates and does nothing.

        `required` alone is not enough to decide what is safe to drop. The real
        diplomacy contracts express "orders OR candidate_id OR
        use_server_default" with oneOf, so `orders` -- the whole content of a
        movement turn -- is absent from the top-level required list. A first
        version of this salvage dropped it twice in a live match: the payload
        passed validation and the power did nothing that turn, which is worse
        than the rejection the salvage exists to avoid.

        So a non-empty list is never dropped. In this contract family the lists
        ARE the action and the objects beside them are the hints.
        """

        legal = [{
            "action": "submit_orders",
            "params_schema": {
                "type": "object",
                # No top-level `required`, exactly like the real contract.
                "properties": {
                    "orders": {
                        "type": "array",
                        "items": {"type": "object",
                                  "properties": {"type": {"enum": ["MOVE"]}}},
                    },
                    "strategy_intent": {
                        "type": "object",
                        "properties": {
                            "avoid_provinces": {"type": "array", "maxItems": 1},
                        },
                    },
                },
            },
        }]
        move = {
            "action": "submit_orders",
            "params": {
                "orders": [{"type": "HOLD"}],          # invalid: not in the enum
                "strategy_intent": {"avoid_provinces": ["A", "B"]},
            },
        }
        pruned, notes = decision_context.prune_optional_violations(move, legal)
        self.assertIn("orders", pruned["params"], "content must survive")
        self.assertEqual(pruned["params"]["orders"], move["params"]["orders"])
        # The hint beside it is still trimmable.
        self.assertEqual(
            pruned["params"]["strategy_intent"]["avoid_provinces"], ["A"],
        )

    def test_preflight_survives_the_gateway_forcing_json_mode(self):
        """A connectivity check must not be able to crash-loop a runtime.

        The arena gateway forces response_format=json_object onto every request
        from an agent seated in a live match -- preflight included -- and the
        provider rejects any prompt that never mentions json:

            400 Prompt must contain the word 'json' in some form to use
                'response_format' of type 'json_object'.

        Observed live: five of eleven runtimes restarted while holding seats,
        failed preflight 26-27 times each, refused to start, and could not
        recover on their own. The word is load-bearing, so it is pinned.
        """

        for name, module in (("kit", llm_agent), ("hermes", hermes_agent)):
            source = inspect.getsource(module)
            # Look at what follows the readiness sentinel in each preflight
            # prompt, which is where the connectivity instruction lives.
            after = source.lower().split("clawarena_ready")
            self.assertGreater(len(after), 1, f"{name} has no preflight sentinel")
            self.assertTrue(
                any("json" in chunk[:400] for chunk in after[1:]),
                f"{name} preflight prompt must mention json",
            )

    def test_every_window_that_can_ask_for_a_board_says_so(self):
        """A capability the model is never told about is not a capability.

        need_full_state was documented in the first-turn system prompt only. The
        Hermes resumed turn uses its own contract, and the delta note -- the one
        place where the model would notice it is missing something -- told it to
        cope silently instead: "decide from what this turn gives you". That is
        the exact moment the option has to be on the page.
        """

        self.assertIn("need_full_state", hermes_agent._RESUMED_CONTRACT)
        self.assertIn("need_full_state", llm_agent.SYSTEM_PROMPT)
        self.assertIn("need_full_state", llm_agent.GAMEPLAY_SESSION_SCAFFOLD)

        state = {"game_type": "monopoly", "phase": "turn", "cash": 1500}
        old_last = dict(hermes_agent._LAST)
        try:
            hermes_agent._LAST.clear()
            hermes_agent._LAST.update(
                sid="s1", board={"phase": "old"}, turn_count=3,
            )
            delta_prompt = hermes_agent._build_prompt(
                state, [{"action": "roll"}], "s1", hermes_agent._board(state),
            )
        finally:
            hermes_agent._LAST.clear()
            hermes_agent._LAST.update(old_last)
        self.assertIn("state_delta", delta_prompt)
        self.assertIn("need_full_state", delta_prompt)

    def test_hermes_sends_the_whole_board_when_one_was_just_fetched(self):
        """A resynced poll carries a whole board, so hand over the whole thing.

        Diffing it against the copy we last sent would be the one case where a
        delta is certainly wrong: the agent asked for a snapshot precisely
        because it believes its own copy is no longer trustworthy.
        """

        state = {"game_type": "monopoly", "phase": "turn", "cash": 1500,
                 "_full_state_requested": True}
        legal = [{"action": "roll", "params": {}}]
        old_last = dict(hermes_agent._LAST)
        prompts = []
        try:
            hermes_agent._LAST.clear()
            hermes_agent._LAST.update(
                sid="s1", board={"phase": "old"}, turn_count=5,
            )
            with (
                mock.patch.object(hermes_agent.memory, "get_hermes_session", return_value="s1"),
                mock.patch.object(
                    hermes_agent.memory, "get_hermes_session_turn_count", return_value=5,
                ),
                mock.patch.object(hermes_agent.memory, "set_hermes_session"),
                mock.patch.object(hermes_agent.memory, "set_hermes_session_turn_count"),
                mock.patch.object(
                    hermes_agent, "_run_chat",
                    side_effect=lambda prompt, sid, t: (
                        prompts.append(prompt) or ('{"action":"roll","params":{}}', "s1")
                    ),
                ),
            ):
                hermes_agent.decide(state, legal)
        finally:
            hermes_agent._LAST.clear()
            hermes_agent._LAST.update(old_last)

        payload = json.loads(prompts[0].rsplit("GAME:\n", 1)[1])
        self.assertIn("state", payload)
        self.assertNotIn("state_delta", payload)
        # Still turn N of an established session, so it keeps the resumed
        # contract rather than reverting to the first-turn prompt.
        self.assertTrue(prompts[0].startswith(hermes_agent._RESUMED_CONTRACT))

    def test_hermes_resumed_turn_prints_reply_provenance(self):
        """The resumed path must report WHY a reply failed, not just that it did.

        Without this it logged only "a fallback happened", so a session failing
        for one reason looked identical to one failing for any other -- on the
        path now being recommended, which is the one that most needs reading.
        """

        state = {"game_type": "monopoly", "phase": "turn", "my_memory": {}}
        legal = [{"action": "roll", "params": {}}]
        printed = []
        old_last = dict(hermes_agent._LAST)

        try:
            hermes_agent._LAST.update(
                sid="s1", board={"phase": "old"}, memory={}, turn_count=3,
            )
            with (
                mock.patch.object(hermes_agent.memory, "get_hermes_session", return_value="s1"),
                mock.patch.object(
                    hermes_agent.memory, "get_hermes_session_turn_count", return_value=3,
                ),
                mock.patch.object(hermes_agent.memory, "set_hermes_session"),
                mock.patch.object(hermes_agent.memory, "set_hermes_session_turn_count"),
                mock.patch.object(
                    hermes_agent, "_run_chat",
                    side_effect=lambda *a, **k: ("not a decision at all", "s1"),
                ),
                mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(a[0] if a else "")),
            ):
                hermes_agent.decide(state, legal)
        finally:
            hermes_agent._LAST.clear()
            hermes_agent._LAST.update(old_last)

        provenance = [
            json.loads(line) for line in printed
            if isinstance(line, str) and '"clawarena_model_reply_provenance"' in line
        ]
        self.assertTrue(provenance, "resumed turn printed no provenance line")
        self.assertEqual(provenance[0]["brain"], "hermes")
        self.assertEqual(provenance[0]["session_mode"], "resumed")
        # And it names the failure class rather than leaving it blank.
        self.assertTrue(provenance[0]["outcome"])

    def _resumed_decide(self, replies, *, budget=165.0):
        """Drive one resumed turn, returning (printed lines, prompts asked)."""

        state = {
            "game_type": "monopoly",
            "phase": "turn",
            "_decision_budget_seconds": budget,
        }
        legal = [{"action": "roll", "params": {}}]
        printed = []
        asked = []
        old_last = dict(hermes_agent._LAST)
        replies = list(replies)

        def fake_chat(prompt, session_id, timeout, **kwargs):
            asked.append(prompt)
            return replies.pop(0) if replies else ("", session_id)

        try:
            hermes_agent._LAST.update(
                sid="s1", board={"phase": "old"}, memory={}, turn_count=3,
            )
            with (
                mock.patch.object(hermes_agent.memory, "get_hermes_session", return_value="s1"),
                mock.patch.object(
                    hermes_agent.memory, "get_hermes_session_turn_count", return_value=3,
                ),
                mock.patch.object(hermes_agent.memory, "set_hermes_session"),
                mock.patch.object(hermes_agent.memory, "set_hermes_session_turn_count"),
                mock.patch.object(hermes_agent, "_run_chat", side_effect=fake_chat),
                # Freeze the clock so the whole declared budget is still
                # remaining when the empty reply comes back -- the gate is
                # about room to finish, not about elapsed test time.
                mock.patch.object(hermes_agent.time, "monotonic", return_value=0.0),
                mock.patch("builtins.print",
                           side_effect=lambda *a, **k: printed.append(a[0] if a else "")),
            ):
                hermes_agent.decide(state, legal)
        finally:
            hermes_agent._LAST.clear()
            hermes_agent._LAST.update(old_last)
        return printed, asked

    def test_an_empty_resumed_reply_is_asked_once_more(self):
        """A turn the model spent entirely on reasoning is worth re-asking.

        There is nothing to repair -- no malformed object, no illegal move --
        and a ~20s call against a 165s turn budget means one more ask costs a
        fraction of what losing the turn does.
        """

        before = hermes_agent._COUNTS["empty_reply_retries"]
        printed, asked = self._resumed_decide([
            ("", "s1"),
            ('{"action":"roll","params":{}}', "s1"),
        ])

        self.assertEqual(len(asked), 2, "an empty reply was not re-asked")
        self.assertEqual(asked[0], asked[1],
                         "the retry must re-send the same prompt, unchanged")
        self.assertEqual(hermes_agent._COUNTS["empty_reply_retries"], before + 1)

        provenance = [
            json.loads(line) for line in printed
            if isinstance(line, str) and '"clawarena_model_reply_provenance"' in line
        ]
        self.assertEqual([p.get("empty_reply") for p in provenance][0], "retried",
                         "the empty turn must still be reported, not hidden")
        self.assertEqual(provenance[-1]["outcome"], "accepted")

    def test_a_reply_that_arrived_is_never_re_asked(self):
        _printed, asked = self._resumed_decide([
            ('{"action":"roll","params":{}}', "s1"),
        ])
        self.assertEqual(len(asked), 1)

    def test_an_empty_reply_is_not_re_asked_without_room_to_finish(self):
        """Starting a call that cannot complete loses the turn AND the time."""

        _printed, asked = self._resumed_decide([("", "s1")], budget=10.0)
        self.assertEqual(len(asked), 1)

    def test_hermes_resumed_contract_carries_the_stopping_rules(self):
        """The resumed path must not re-earn its old reasoning bill.

        Stateless became the Hermes default because a reasoning model re-walked
        the append-only transcript every turn. The fix measured on the starter
        session arm -- play the server recommendation, null retracts, never
        re-derive -- has to ride the resumed contract too, or flipping
        HERMES_STATELESS_GAMEPLAY off re-buys the exact regression.
        """

        contract = hermes_agent._RESUMED_CONTRACT
        self.assertIn("decision_support.recommended_action", contract)
        self.assertIn("null decision_support retracts", contract)
        self.assertIn("never\u0020carried forward".replace("\u0020", " "), contract)
        self.assertIn("re-derive a ranking", contract)
        self.assertIn("share the turn budget", contract)

    def test_diplomacy_server_epoch_keeps_the_hermes_session(self):
        """A server rebase is not a session loss; only Hermes' own errors are.

        Clearing the resumable session on every diplomacy epoch bump threw away
        Hermes' accumulated context for nothing: the rebased turn still carries
        a full board, so the delta below diffs it correctly.
        """

        state = {
            "game_type": "diplomacy",
            "phase": "F1902M-N1",
            "phase_key": "F1902M-N1",
            "phase_type": "negotiation",
            "decision_context_epoch": "F1902M",
            "power": "ENGLAND",
        }
        legal = [{"action": "send_press", "params": {"messages": "array"}}]
        prompts = []
        cleared = []
        saved_sessions = []
        saved_turn_counts = []
        old_last = dict(hermes_agent._LAST)

        def fake_chat(prompt, session_id, timeout):
            prompts.append((prompt, session_id, timeout))
            return '{"action":"send_press","params":{"messages":[]}}', "spring-session"

        try:
            hermes_agent._LAST.update(
                sid="spring-session",
                board={"phase": "S1902M-ORDERS"},
                turn_count=3,
            )
            with (
                mock.patch.object(hermes_agent.memory, "get_hermes_session", return_value="spring-session"),
                mock.patch.object(hermes_agent.memory, "get_hermes_session_turn_count", return_value=3),
                mock.patch.object(hermes_agent.memory, "clear_hermes_session", side_effect=lambda: cleared.append(True)),
                mock.patch.object(hermes_agent.memory, "set_hermes_session", side_effect=saved_sessions.append),
                mock.patch.object(
                    hermes_agent.memory,
                    "set_hermes_session_turn_count",
                    side_effect=saved_turn_counts.append,
                ),
                mock.patch.object(hermes_agent, "_run_chat", side_effect=fake_chat),
            ):
                move = hermes_agent.decide(state, legal)
        finally:
            hermes_agent._LAST.clear()
            hermes_agent._LAST.update(old_last)

        self.assertEqual(move, {"action": "send_press", "params": {"messages": []}})
        self.assertEqual(cleared, [])
        self.assertEqual(prompts[0][1], "spring-session")
        self.assertTrue(prompts[0][0].startswith(hermes_agent._RESUMED_CONTRACT))
        payload = json.loads(prompts[0][0].rsplit("GAME:\n", 1)[1])
        self.assertIn("state_delta", payload)
        self.assertNotIn("state", payload)
        self.assertEqual(saved_sessions, ["spring-session"])
        # The resumed session keeps counting instead of restarting at 1.
        self.assertEqual(saved_turn_counts, [4])

    def test_hermes_context_exhaustion_recovers_with_fresh_full_baseline(self):
        state = {"game_type": "mafia", "phase": "day", "my_memory": {"my_role": "citizen"}}
        legal = [{"action": "vote", "params": {"target_id": 2}}]
        prompts = []
        cleared = []
        old_last = dict(hermes_agent._LAST)

        def fake_chat(prompt, session_id, timeout):
            prompts.append((prompt, session_id, timeout))
            if session_id:
                raise RuntimeError("max compression attempts reached after context overflow")
            return '{"action":"vote","params":{"target_id":2}}', "recovered-session"

        try:
            hermes_agent._LAST.update(sid="oversized-session", board={"phase": "night"}, turn_count=40)
            with (
                mock.patch.object(hermes_agent.memory, "get_hermes_session", return_value="oversized-session"),
                mock.patch.object(hermes_agent.memory, "get_hermes_session_turn_count", return_value=40),
                mock.patch.object(hermes_agent.memory, "clear_hermes_session", side_effect=lambda: cleared.append(True)),
                mock.patch.object(hermes_agent.memory, "set_hermes_session"),
                mock.patch.object(hermes_agent.memory, "set_hermes_session_turn_count"),
                mock.patch.object(hermes_agent, "_run_chat", side_effect=fake_chat),
            ):
                move = hermes_agent.decide(state, legal)
        finally:
            hermes_agent._LAST.clear()
            hermes_agent._LAST.update(old_last)

        self.assertEqual(move, {"action": "vote", "params": {"target_id": 2}})
        self.assertEqual(cleared, [True])
        self.assertEqual([session_id for _, session_id, _ in prompts], ["oversized-session", None])
        recovered_payload = json.loads(prompts[1][0].rsplit("GAME:\n", 1)[1])
        self.assertIn("state", recovered_payload)
        self.assertNotIn("state_delta", recovered_payload)

    def test_report_delivery_is_background_bounded_and_container_timed(self):
        called = threading.Event()
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            called.set()
            return mock.Mock(returncode=0, stdout="", stderr="")

        with (
            mock.patch.object(hermes_agent, "HERMES_CONTAINER", "hermes-test"),
            mock.patch.object(hermes_agent, "HERMES_BIN", "/opt/hermes/bin/hermes"),
            mock.patch.object(hermes_agent, "HERMES_DELIVER_TARGET", "telegram:1"),
            mock.patch.object(hermes_agent, "HERMES_REPORT_TIMEOUT", 3),
            mock.patch.object(hermes_agent, "HERMES_MODEL", ""),
            mock.patch.object(hermes_agent, "HERMES_PROVIDER", ""),
            mock.patch.object(hermes_agent.subprocess, "run", side_effect=fake_run),
        ):
            before = time.monotonic()
            hermes_agent.report({"game_type": "mafia"}, {"action": "vote", "params": {}})
            elapsed = time.monotonic() - before
            self.assertTrue(called.wait(1))
            deadline = time.monotonic() + 1
            while hermes_agent._REPORT_LOCK.locked() and time.monotonic() < deadline:
                time.sleep(0.01)

        self.assertLess(elapsed, 0.2)
        self.assertEqual(calls[0][0][:7], [
            "docker", "exec", "hermes-test", "timeout", "--signal=TERM", "--kill-after=5s", "3s",
        ])
        self.assertEqual(calls[0][1]["timeout"], 18)
        report_command = calls[0][0]
        self.assertIn("send", report_command)
        self.assertEqual(report_command[report_command.index("--to") + 1], "telegram:1")
        self.assertTrue(any("Submitted vote" in part for part in report_command))
        self.assertNotIn("-t", report_command)

    def test_report_delivery_falls_back_only_when_direct_send_is_unavailable(self):
        calls = []
        finished = threading.Event()

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            if len(calls) == 1:
                return mock.Mock(
                    returncode=2,
                    stdout="",
                    stderr="hermes: error: argument command: invalid choice: 'send'",
                )
            finished.set()
            return mock.Mock(returncode=0, stdout="", stderr="")

        with (
            mock.patch.object(hermes_agent, "HERMES_CONTAINER", ""),
            mock.patch.object(hermes_agent, "HERMES_BIN", "hermes"),
            mock.patch.object(hermes_agent, "HERMES_DELIVER_TARGET", "telegram:1"),
            mock.patch.object(hermes_agent, "HERMES_REPORT_TIMEOUT", 3),
            mock.patch.object(hermes_agent, "HERMES_MODEL", ""),
            mock.patch.object(hermes_agent, "HERMES_PROVIDER", ""),
            mock.patch.object(hermes_agent.subprocess, "run", side_effect=fake_run),
        ):
            hermes_agent.report(
                {"game_type": "mafia", "phase": "day"},
                {"action": "vote", "params": {"target_id": 2}},
            )
            self.assertTrue(finished.wait(1))
            deadline = time.monotonic() + 1
            while hermes_agent._REPORT_LOCK.locked() and time.monotonic() < deadline:
                time.sleep(0.01)

        self.assertEqual(len(calls), 2)
        self.assertIn("send", calls[0][0])
        legacy = calls[1][0]
        self.assertEqual(legacy[legacy.index("-t") + 1], "messaging")
        self.assertIn("--yolo", legacy)


class SetupTests(unittest.TestCase):
    def test_runner_env_defaults_transport_to_diplomacy_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {}, clear=True):
                runner_env = setup_local_runner._runner_env(
                    token="token",
                    base="https://arena.example/api/v1",
                    home=Path(tmp),
                    hermes_bin="hermes",
                )

        self.assertEqual(runner_env["HERMES_TIMEOUT_SECONDS"], "165")
        self.assertEqual(runner_env["HERMES_ATTEMPT_TIMEOUT_SECONDS"], "165")

    def test_gameplay_profile_overrides_reasoning_without_mutating_user_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "hermes"
            source.mkdir()
            source_config = source / "config.yaml"
            source_text = (
                "model:\n  default: deepseek-v4-flash\n  provider: deepseek\n"
                "agent:\n  reasoning_effort: max\n  reasoning_overrides:\n"
                "    deepseek-v4-flash: max\n  api_max_retries: 2\n  max_turns: 6\n"
                "display:\n  show_reasoning: true\n"
                "fallback_providers:\n  - provider: another\n"
            )
            source_config.write_text(source_text)
            (source / ".env").write_text("PROVIDER_KEY=not-printed\n")

            expected = {
                "agent.reasoning_effort": "low",
                "model.max_tokens": "8000",
                "agent.api_max_retries": "0",
            }

            def fake_run(command, **_kwargs):
                return mock.Mock(returncode=0, stdout=expected[command[-1]] + "\n", stderr="")

            with (
                mock.patch.dict(os.environ, {
                    "HERMES_HOME": str(source),
                    "HERMES_TIMEOUT_SECONDS": "40",
                    "HERMES_ATTEMPT_TIMEOUT_SECONDS": "35",
                }),
                mock.patch.object(setup_local_runner.subprocess, "run", side_effect=fake_run),
            ):
                target = setup_local_runner._prepare_gameplay_home(
                    root / "arena", "/opt/hermes/.venv/bin/hermes",
                )

            rendered = (target / "config.yaml").read_text()
            self.assertEqual(source_config.read_text(), source_text)
            self.assertIn('reasoning_effort: "low"', rendered)
            self.assertIn("reasoning_overrides: {}", rendered)
            self.assertIn("api_max_retries: 0", rendered)
            self.assertIn("max_turns: 1", rendered)
            self.assertIn("max_tokens: 8000", rendered)
            self.assertIn("fallback_providers: []", rendered)
            self.assertNotIn("deepseek-v4-flash: max", rendered)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((target / "config.yaml").stat().st_mode), 0o600)
            self.assertTrue((target / ".env").is_symlink())

            runner_env = setup_local_runner._runner_env(
                token="secret-not-logged",
                base="https://test.example/api/v1",
                home=root / "arena",
                hermes_bin="/opt/hermes/.venv/bin/hermes",
                gameplay_home=target,
            )
            self.assertEqual(runner_env["HERMES_GAMEPLAY_REASONING_EFFORT"], "low")
            self.assertEqual(runner_env["HERMES_GAMEPLAY_THINKING_MODE"], "enabled")
            self.assertEqual(runner_env["HERMES_TIMEOUT_SECONDS"], "165")
            self.assertEqual(runner_env["HERMES_ATTEMPT_TIMEOUT_SECONDS"], "165")
            self.assertEqual(runner_env["CLAWARENA_HERMES_MAX_TOKENS"], "8000")
            self.assertEqual(runner_env["HERMES_MAX_TOKENS"], "8000")

    def test_gameplay_provider_route_is_written_only_to_isolated_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "hermes"
            source.mkdir()
            source_config = source / "config.yaml"
            source_text = (
                "model:\n  default: deepseek-v4-flash\n  provider: deepseek\n"
                "  base_url: https://api.deepseek.com\n"
                "agent:\n  reasoning_effort: max\n  api_max_retries: 2\n"
            )
            source_config.write_text(source_text)
            (source / ".env").write_text("GOOGLE_API_KEY=not-printed\n")
            env = {
                "HERMES_HOME": str(source),
                setup_local_runner.HERMES_GAMEPLAY_PROVIDER_ENV: "gemini",
                setup_local_runner.HERMES_GAMEPLAY_MODEL_ENV: "gemini-3.6-flash",
                setup_local_runner.HERMES_GAMEPLAY_BASE_URL_ENV: "",
            }
            expected = {
                "agent.reasoning_effort": "low",
                "model.max_tokens": "8000",
                "agent.api_max_retries": "0",
                "model.provider": "gemini",
                "model.default": "gemini-3.6-flash",
                "model.base_url": "",
            }

            def fake_run(command, **_kwargs):
                return mock.Mock(returncode=0, stdout=expected[command[-1]] + "\n", stderr="")

            with mock.patch.object(
                setup_local_runner.subprocess, "run", side_effect=fake_run,
            ):
                target = setup_local_runner._prepare_gameplay_home(
                    root / "arena", "/opt/hermes/.venv/bin/hermes", env,
                )

            rendered = (target / "config.yaml").read_text()
            self.assertEqual(source_config.read_text(), source_text)
            self.assertIn('provider: "gemini"', rendered)
            self.assertIn('default: "gemini-3.6-flash"', rendered)
            self.assertIn('base_url: ""', rendered)
            self.assertTrue((target / ".env").is_symlink())

    def test_gameplay_provider_route_requires_provider_and_model_together(self):
        with self.assertRaisesRegex(RuntimeError, "must be set together"):
            setup_local_runner._gameplay_route({
                setup_local_runner.HERMES_GAMEPLAY_PROVIDER_ENV: "gemini",
            })

    def test_hermes_state_owner_rejects_another_arena_or_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / setup_local_runner.STATE_OWNER_FILENAME).write_text(json.dumps({
                "schema_version": 1,
                "arena_base": "https://prod.example/api/v1",
                "runtime_kind": "openclaw",
            }))

            with self.assertRaisesRegex(RuntimeError, "belongs to openclaw"):
                setup_local_runner._validate_state_owner(
                    home,
                    "https://test.example/api/v1",
                )

    def test_concurrent_setup_returns_without_starting_a_second_operation(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = io.StringIO()

            def locked_elsewhere(_fd, operation):
                if operation & setup_local_runner.fcntl.LOCK_NB:
                    raise BlockingIOError

            with (
                mock.patch.object(
                    sys,
                    "argv",
                    ["setup_local_runner.py", "--home", tmp, "--stop"],
                ),
                mock.patch.object(setup_local_runner.fcntl, "flock", side_effect=locked_elsewhere),
                mock.patch.object(sys, "stdout", output),
            ):
                self.assertEqual(setup_local_runner.main(), 0)

            self.assertEqual(json.loads(output.getvalue()), {
                "status": "setup_in_progress",
                "home": tmp,
                "note": "Another ClawArena setup/update is already running.",
            })

    def test_atomic_credentials_are_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "private" / "token"
            setup_local_runner._atomic_write(target, "secret")

            self.assertEqual(target.read_text(), "secret")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_recovery_key_is_redeemed_locally_and_validated(self):
        token = base64.urlsafe_b64encode(
            json.dumps({"a": 77, "t": "fresh-auth"}).encode()
        ).decode().rstrip("=")
        with mock.patch.object(
            setup_local_runner,
            "_post",
            return_value={"agent_id": 77, "connection_token": token},
        ) as post:
            self.assertEqual(
                setup_local_runner._redeem_recovery_key(
                    "https://arena.example/api/v1",
                    "CA-ONE-USE",
                ),
                token,
            )

        post.assert_called_once_with(
            "https://arena.example/api/v1/agents/connection-recovery/redeem/",
            {"recovery_key": "CA-ONE-USE"},
        )

        with (
            mock.patch.object(
                setup_local_runner,
                "_post",
                return_value={"agent_id": 78, "connection_token": token},
            ),
            self.assertRaisesRegex(RuntimeError, "mismatched agent id"),
        ):
            setup_local_runner._redeem_recovery_key(
                "https://arena.example/api/v1",
                "CA-WRONG-AGENT",
            )

    def test_candidate_preflight_failure_preserves_live_runner_and_installed_kit(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            kit = home / "kit"
            kit.mkdir(parents=True)
            (kit / "runner.py").write_text("installed-runner")
            (home / "runner.pid").write_text("4321")
            (home / "token").write_text("saved-token")

            def download(_url, destination):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text("candidate")

            with (
                mock.patch.object(
                    sys,
                    "argv",
                    [
                        "setup_local_runner.py",
                        "--base", "https://arena.example/api/v1",
                        "--home", str(home),
                        "--hermes-bin", "/bin/true",
                    ],
                ),
                mock.patch.object(setup_local_runner, "_runner_alive", return_value=True),
                mock.patch.object(
                    setup_local_runner,
                    "_post",
                    return_value={"agent_id": 77, "agent_claimed": True},
                ),
                mock.patch.object(setup_local_runner, "_download", side_effect=download),
                mock.patch.object(
                    setup_local_runner,
                    "_preflight_candidate",
                    side_effect=RuntimeError("candidate model unavailable"),
                ),
                mock.patch.object(setup_local_runner, "_stop_runner") as stop_runner,
                self.assertRaisesRegex(RuntimeError, "candidate model unavailable"),
            ):
                setup_local_runner.main()

            stop_runner.assert_not_called()
            self.assertEqual((home / "runner.pid").read_text(), "4321")
            self.assertEqual((kit / "runner.py").read_text(), "installed-runner")

    def test_replacement_start_failure_restores_previous_hermes_kit_and_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            kit = home / "kit"
            kit.mkdir(parents=True)
            for name in setup_local_runner.KIT_FILES:
                (kit / name).write_text(f"installed-{name}")
            (home / "runner.pid").write_text("4321")
            (home / "token").write_text("saved-token")

            def download(_url, destination):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(f"candidate-{destination.name}")

            def stop_runner(_pid, pidfile):
                pidfile.unlink(missing_ok=True)

            with (
                mock.patch.object(
                    sys,
                    "argv",
                    [
                        "setup_local_runner.py",
                        "--base", "https://arena.example/api/v1",
                        "--home", str(home),
                        "--hermes-bin", "/bin/true",
                    ],
                ),
                mock.patch.object(setup_local_runner, "_runner_alive", return_value=True),
                mock.patch.object(
                    setup_local_runner,
                    "_post",
                    return_value={"agent_id": 77, "agent_claimed": True},
                ),
                mock.patch.object(setup_local_runner, "_download", side_effect=download),
                mock.patch.object(setup_local_runner, "_preflight_candidate"),
                mock.patch.object(setup_local_runner, "_stop_runner", side_effect=stop_runner),
                mock.patch.object(
                    setup_local_runner,
                    "_start_runner",
                    side_effect=[RuntimeError("replacement boot failed"), mock.Mock(pid=9002)],
                ) as start_runner,
                self.assertRaisesRegex(RuntimeError, "previous kit and runner were restored"),
            ):
                setup_local_runner.main()

            self.assertEqual(start_runner.call_count, 2)
            for name in setup_local_runner.KIT_FILES:
                self.assertEqual((kit / name).read_text(), f"installed-{name}")

    def test_legacy_hermes_runner_is_identified_and_stopped_from_its_original_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy = Path(tmp) / "legacy"
            legacy_kit = legacy / "kit"
            legacy_kit.mkdir(parents=True)
            (legacy_kit / "runner.py").write_text("legacy-runner")
            (legacy / "runner.pid").write_text("4321")
            (legacy / "token").write_text("legacy-token")
            target = Path(tmp) / "target"
            stopped = []
            output = io.StringIO()

            def download(_url, destination):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(f"candidate-{destination.name}")

            def runner_alive(pid, kit):
                return pid == 4321 and kit == legacy_kit

            with (
                mock.patch.object(
                    sys,
                    "argv",
                    [
                        "setup_local_runner.py",
                        "--base", "https://arena.example/api/v1",
                        "--home", str(target),
                        "--hermes-bin", "/bin/true",
                    ],
                ),
                mock.patch.object(setup_local_runner, "_legacy_homes", return_value=[legacy]),
                mock.patch.object(
                    setup_local_runner,
                    "_post",
                    return_value={"agent_id": 77, "agent_claimed": True},
                ),
                mock.patch.object(setup_local_runner, "_runner_alive", side_effect=runner_alive),
                mock.patch.object(setup_local_runner, "_download", side_effect=download),
                mock.patch.object(setup_local_runner, "_preflight_candidate"),
                mock.patch.object(
                    setup_local_runner,
                    "_stop_runner",
                    side_effect=lambda pid, pidfile: stopped.append((pid, pidfile)),
                ),
                mock.patch.object(
                    setup_local_runner,
                    "_start_runner",
                    return_value=mock.Mock(pid=9003),
                ),
                mock.patch.object(sys, "stdout", output),
            ):
                self.assertEqual(setup_local_runner.main(), 0)

            self.assertEqual(stopped, [(4321, legacy / "runner.pid")])
            self.assertEqual(json.loads(output.getvalue())["state_migrated_from"], str(legacy))

    def test_stop_adopts_a_valid_legacy_hermes_runner_before_signalling_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy = Path(tmp) / "legacy"
            legacy_kit = legacy / "kit"
            legacy_kit.mkdir(parents=True)
            (legacy_kit / "runner.py").write_text("legacy-runner")
            (legacy / "runner.pid").write_text("4321")
            (legacy / "token").write_text("legacy-token")
            target = Path(tmp) / "target"
            stopped = []
            output = io.StringIO()

            def runner_alive(pid, kit):
                return pid == 4321 and kit == legacy_kit

            with (
                mock.patch.object(
                    sys,
                    "argv",
                    [
                        "setup_local_runner.py",
                        "--base", "https://arena.example/api/v1",
                        "--home", str(target),
                        "--stop",
                    ],
                ),
                mock.patch.object(setup_local_runner, "_legacy_homes", return_value=[legacy]),
                mock.patch.object(
                    setup_local_runner,
                    "_post",
                    return_value={"agent_id": 77, "agent_claimed": True},
                ),
                mock.patch.object(setup_local_runner, "_runner_alive", side_effect=runner_alive),
                mock.patch.object(
                    setup_local_runner,
                    "_stop_runner",
                    side_effect=lambda pid, pidfile: stopped.append((pid, pidfile)),
                ),
                mock.patch.object(sys, "stdout", output),
            ):
                self.assertEqual(setup_local_runner.main(), 0)

            self.assertEqual(stopped, [(4321, legacy / "runner.pid")])
            self.assertEqual(json.loads(output.getvalue())["status"], "stopped")
            self.assertEqual((target / "token").read_text(), "legacy-token")

    def test_start_timeout_stops_candidate_before_reporting_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            kit = home / "kit"
            kit.mkdir(parents=True)
            (kit / "runner.py").write_text("runner")
            process = mock.Mock(pid=4321)
            process.poll.return_value = None

            with (
                mock.patch.object(subprocess, "Popen", return_value=process),
                mock.patch.object(time, "monotonic", side_effect=[0.0, 200.0]),
                mock.patch.object(setup_local_runner, "_stop_runner") as stop_runner,
                self.assertRaisesRegex(RuntimeError, "did not complete"),
            ):
                setup_local_runner._start_runner(
                    kit=kit,
                    home=home,
                    env={},
                    matches=0,
                )

            stop_runner.assert_called_once_with(
                4321,
                home / "runner.pid",
                grace_seconds=2.0,
            )

    def test_redeemed_token_is_saved_before_claim_link_refresh(self):
        token = base64.urlsafe_b64encode(
            json.dumps({"a": 77, "t": "fresh-auth"}).encode()
        ).decode().rstrip("=")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            responses = [
                {"agent_id": 77, "connection_token": token},
                RuntimeError("claim-link temporarily unavailable"),
            ]

            def post(*_args, **_kwargs):
                response = responses.pop(0)
                if isinstance(response, Exception):
                    raise response
                return response

            with (
                mock.patch.dict(
                    os.environ,
                    {"CLAWARENA_CONNECTION_TOKEN": "", "CLAWARENA_RECOVERY_KEY": ""},
                    clear=False,
                ),
                mock.patch.object(
                    sys,
                    "argv",
                    [
                        "setup_local_runner.py",
                        "--base", "https://arena.example/api/v1",
                        "--home", str(home),
                        "--recovery-key", "CA-ONE-USE",
                    ],
                ),
                mock.patch.object(setup_local_runner, "_post", side_effect=post),
                self.assertRaisesRegex(RuntimeError, "claim-link temporarily unavailable"),
            ):
                setup_local_runner.main()

            self.assertEqual((home / "token").read_text(), token)
            self.assertEqual((home / "agent_id").read_text(), "77")
            self.assertEqual(stat.S_IMODE((home / "token").stat().st_mode), 0o600)

    def test_pid_reuse_does_not_match_unrelated_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(setup_local_runner._runner_alive(os.getpid(), Path(tmp)))

    def test_claim_state_removes_a_stale_link_after_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "claim_url").write_text("https://old.example/claim/stale")

            claim_url, claimed = setup_local_runner._save_claim_state(home, {
                "agent_id": 9,
                "agent_claimed": True,
                "claim_url": None,
            })

            self.assertIsNone(claim_url)
            self.assertTrue(claimed)
            self.assertFalse((home / "claim_url").exists())
            self.assertEqual((home / "agent_id").read_text(), "9")

    def test_entrypoint_formats_setup_failures_as_json(self):
        output = io.StringIO()
        with (
            mock.patch.object(setup_local_runner, "main", side_effect=RuntimeError("offline")),
            mock.patch.object(sys, "stdout", output),
        ):
            self.assertEqual(setup_local_runner._entrypoint(), 1)

        self.assertEqual(json.loads(output.getvalue()), {
            "status": "error",
            "message": "RuntimeError: offline",
        })

    @unittest.skipUnless(
        shutil.which("curl"),
        "curl setup integration runs in the host test lane; runtime images use clawarena-runtime-smoke",
    )
    def test_end_to_end_setup_waits_for_runner_readiness(self):
        token = base64.urlsafe_b64encode(
            json.dumps({"a": 77, "t": "test-auth"}).encode()
        ).decode().rstrip("=")
        source_dir = KIT_DIR

        class Handler(BaseHTTPRequestHandler):
            provisions = 0
            claim_link_calls = 0
            provision_payloads = []

            def send_payload(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.startswith("/kit/"):
                    name = self.path.removeprefix("/kit/")
                    if name not in [*setup_local_runner.KIT_FILES, "setup_local_runner.py"]:
                        self.send_error(404)
                        return
                    body = (source_dir / name).read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path.startswith("/api/v1/agents/schema/"):
                    self.send_payload(200, {
                        "protocol_version": "test",
                        "heartbeat": {
                            "grace_seconds": 120,
                            "identity": {"skill_version": "test"},
                            "body_template": {"status": "idle", "feed_status": "connected"},
                        },
                    })
                    return
                if self.path.startswith("/api/v1/agents/game/"):
                    self.send_payload(200, {"status": "idle", "message": "Choose a game"})
                    return
                self.send_error(404)

            def do_POST(self):
                size = int(self.headers.get("Content-Length") or 0)
                raw_body = self.rfile.read(size)
                if self.path == "/api/v1/agents/provision/":
                    Handler.provisions += 1
                    Handler.provision_payloads.append(json.loads(raw_body or b"{}"))
                    self.send_payload(201, {
                        "agent_id": 77,
                        "connection_token": token,
                        "claim_url": "https://example.test/claim/test-code",
                    })
                    return
                if self.path == "/api/v1/agents/provision/claim-link/":
                    Handler.claim_link_calls += 1
                    if self.headers.get("Authorization") != f"Bearer {token}":
                        self.send_payload(403, {"detail": "bad token"})
                        return
                    self.send_payload(200, {
                        "agent_id": 77,
                        "agent_name": "existing",
                        "agent_claimed": False,
                        "claim_url": "https://example.test/claim/refreshed-code",
                        "expires_at": "2030-01-01T00:00:00Z",
                        "refreshed": True,
                    })
                    return
                if self.path in {
                    "/api/v1/economy/agent-daily-bonus/",
                    "/api/v1/agents/watcher/",
                }:
                    self.send_payload(200, {"status": "ok", "detail": "ready"})
                    return
                self.send_error(404)

            def log_message(self, _format, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        pid = None
        try:
            with tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp) / "home"
                hermes = Path(tmp) / "hermes"
                hermes.write_text(
                    "#!/bin/sh\n"
                    "if [ \"$1\" = config ] && [ \"$2\" = get ]; then\n"
                    "  case \"$3\" in\n"
                    "    agent.reasoning_effort) printf 'low\\n' ;;\n"
                    "    model.max_tokens) printf '8000\\n' ;;\n"
                    "    agent.api_max_retries) printf '0\\n' ;;\n"
                    "  esac\n"
                    "  exit 0\n"
                    "fi\n"
                    "printf 'Warning: Unknown toolsets: __clawarena_no_tools_v1__\\n'\n"
                    "printf 'CLAWARENA_READY\\n'\n"
                    "printf 'session_id: setup-preflight\\n' >&2\nexit 0\n"
                )
                hermes.chmod(0o700)
                hermes_source = Path(tmp) / "hermes-home"
                hermes_source.mkdir()
                (hermes_source / "config.yaml").write_text(
                    "model:\n  default: fake\n  provider: fake\n"
                    "agent:\n  reasoning_effort: max\n"
                )
                base = f"http://127.0.0.1:{server.server_port}/api/v1"
                origin = f"http://127.0.0.1:{server.server_port}"
                setup_script = Path(tmp) / "clawarena-setup.py"
                command = (
                    f"curl -fsSL {shlex.quote(origin + '/kit/setup_local_runner.py')} "
                    f"-o {shlex.quote(str(setup_script))} && "
                    f"CLAWARENA_BASE={shlex.quote(base)} python3 {shlex.quote(str(setup_script))}"
                )
                setup_env = {
                    **os.environ,
                    "CLAWARENA_HOME": str(home),
                    "HERMES_BIN": str(hermes),
                    "HERMES_HOME": str(hermes_source),
                }
                setup = subprocess.run(
                    ["/bin/sh", "-c", command],
                    env=setup_env,
                    capture_output=True,
                    text=True,
                    timeout=150,
                    check=False,
                )
                self.assertEqual(setup.returncode, 0, setup.stderr or setup.stdout)

                payload = json.loads(setup.stdout.strip().splitlines()[-1])
                pid = payload["pid"]
                self.assertEqual(payload["status"], "started")
                self.assertEqual(payload["agent_id"], "77")
                self.assertTrue(setup_local_runner._runner_alive(pid, home / "kit"))
                self.assertEqual(stat.S_IMODE((home / "token").stat().st_mode), 0o600)
                self.assertTrue(all((home / "kit" / name).exists() for name in setup_local_runner.KIT_FILES))

                # Re-pasting the same onboarding command is the update path: it
                # stages a fresh kit, stops the old process, and starts exactly
                # one replacement with the same token/agent.
                first_pid = pid
                update_output = io.StringIO()
                with (
                    mock.patch.object(
                        sys,
                        "argv",
                        [
                            "setup_local_runner.py", "--base", base,
                            "--home", str(home), "--hermes-bin", str(hermes),
                        ],
                    ),
                    mock.patch.object(sys, "stdout", update_output),
                    warnings.catch_warnings(),
                ):
                    warnings.simplefilter("ignore", ResourceWarning)
                    self.assertEqual(setup_local_runner.main(), 0)
                updated = json.loads(update_output.getvalue())
                pid = updated["pid"]
                self.assertEqual(updated["status"], "restarted")
                self.assertNotEqual(pid, first_pid)
                self.assertFalse(setup_local_runner._alive(first_pid))
                self.assertTrue(setup_local_runner._runner_alive(pid, home / "kit"))
                self.assertEqual(Handler.provisions, 1)
                self.assertEqual(Handler.claim_link_calls, 1)
                self.assertEqual(Handler.provision_payloads, [{"runtime_kind": "hermes"}])

                stop_output = io.StringIO()
                with (
                    mock.patch.object(
                        sys,
                        "argv",
                        ["setup_local_runner.py", "--home", str(home), "--stop"],
                    ),
                    mock.patch.object(sys, "stdout", stop_output),
                ):
                    self.assertEqual(setup_local_runner.main(), 0)
                self.assertEqual(json.loads(stop_output.getvalue())["status"], "stopped")
                pid = None

                restart_output = io.StringIO()
                with (
                    mock.patch.object(
                        sys,
                        "argv",
                        [
                            "setup_local_runner.py", "--base", base,
                            "--home", str(home), "--hermes-bin", str(hermes),
                        ],
                    ),
                    mock.patch.object(sys, "stdout", restart_output),
                    warnings.catch_warnings(),
                ):
                    warnings.simplefilter("ignore", ResourceWarning)
                    self.assertEqual(setup_local_runner.main(), 0)
                restarted = json.loads(restart_output.getvalue())
                pid = restarted["pid"]
                self.assertTrue(restarted["reused"])
                self.assertEqual(
                    restarted["claim_url"],
                    "https://example.test/claim/refreshed-code",
                )
                self.assertEqual(Handler.provisions, 1)
                self.assertEqual(Handler.claim_link_calls, 2)

                stop_output = io.StringIO()
                with (
                    mock.patch.object(
                        sys,
                        "argv",
                        ["setup_local_runner.py", "--home", str(home), "--stop"],
                    ),
                    mock.patch.object(sys, "stdout", stop_output),
                ):
                    self.assertEqual(setup_local_runner.main(), 0)
                pid = None
        finally:
            if pid:
                try:
                    os.killpg(os.getpgid(pid), 15)
                    os.waitpid(pid, 0)
                except (ChildProcessError, ProcessLookupError):
                    pass
            server.shutdown()
            server.server_close()


class RunnerLoopTests(unittest.TestCase):
    schema = {
        "protocol_version": "test",
        "heartbeat": {
            "grace_seconds": 120,
            "identity": {"skill_version": "test-kit"},
        },
    }

    @staticmethod
    def playing(match_id: int, seq: int) -> dict:
        return {
            "status": "playing",
            "match_id": match_id,
            "game_type": "liars_dice",
            "is_your_turn": True,
            "seq": seq,
            "legal_actions": [{"action": "challenge"}],
            "state": {"game_type": "liars_dice", "phase": "turn"},
        }

    @staticmethod
    def playing_diplomacy(match_id: int, seq: int) -> dict:
        return {
            "status": "playing",
            "match_id": match_id,
            "game_type": "diplomacy",
            "is_your_turn": True,
            "seq": seq,
            "action_window_id": "diplomacy-window",
            "legal_actions": [{
                "action": "send_press",
                "params": {"messages": "array", "strategy_intent": "optional object"},
                "hint": {
                    "recipient_powers": ["ENGLAND"],
                    "valid_power_ids": ["ENGLAND"],
                    "valid_province_ids": ["NWY"],
                    "valid_proposal_ids": [],
                    "valid_candidate_ids": [],
                    "max_messages": 3,
                    "server_fallback": {"params": {"messages": []}},
                },
            }],
            "state": {
                "game_type": "diplomacy",
                "phase": "S1901M-N1",
                "phase_type": "negotiation",
            },
        }

    def run_loop(self, polls, act_result, argv, decide=None):
        actions = []
        # The commit point used to be observed through memory.record_move. That
        # log is gone, so the observation moved to the marker the runner emits
        # at the same instant and still emits: the ACKed action span, which
        # carries the match it committed against and the action it played.
        recorded_moves = []
        real_span = runner._action_span

        def span(action_window_id, match_id, game_type, stage, started, **extra):
            if stage == "ACKed":
                recorded_moves.append((match_id, extra.get("action")))
            return real_span(action_window_id, match_id, game_type, stage, started, **extra)

        decide = decide or mock.Mock(return_value={
            "action": "challenge", "params": {}, "memo": "confirmed read",
        })

        def act(_token, move):
            actions.append(dict(move))
            return act_result.popleft() if isinstance(act_result, deque) else act_result

        with (
            mock.patch.dict(os.environ, {"LLM_API_KEY": "test"}, clear=False),
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(runner.arena_client, "connection_token", return_value="token"),
            mock.patch.object(runner.arena_client, "fetch_schema", return_value=self.schema),
            mock.patch.object(runner.arena_client, "decode_connection_token", return_value=(1, "auth")),
            mock.patch.object(runner.arena_client, "claim_daily_bonus", return_value=(409, {"detail": "done"})),
            mock.patch.object(runner.arena_client, "heartbeat", return_value=200),
            mock.patch.object(
                runner.arena_client,
                "heartbeat_with_response",
                return_value=(200, {"agent_preferences": {}}),
            ),
            mock.patch.object(runner.arena_client, "poll", side_effect=list(polls)),
            mock.patch.object(runner.arena_client, "act", side_effect=act),
            mock.patch.object(runner.brain, "preflight", return_value="test-model"),
            mock.patch.object(runner.brain, "decide", decide),
            mock.patch.object(runner.memory, "open_match"),
            mock.patch.object(runner.memory, "end_match"),
            mock.patch.object(runner, "_action_span", side_effect=span),
            mock.patch.object(runner.time, "sleep"),
        ):
            result = runner.main()

        return result, actions, recorded_moves

    def test_poll_retry_delay_uses_bounded_equal_jitter(self):
        self.assertEqual(
            runner._poll_retry_delay(1, 502, rng=lambda: 0.0),
            3.0,
        )
        self.assertEqual(
            runner._poll_retry_delay(1, 502, rng=lambda: 1.0),
            6.0,
        )
        self.assertEqual(
            runner._poll_retry_delay(20, 502, rng=lambda: 0.0),
            15.0,
        )
        self.assertEqual(
            runner._poll_retry_delay(20, 502, rng=lambda: 1.0),
            30.0,
        )
        self.assertEqual(
            runner._poll_retry_delay(1, 429, rng=lambda: 0.0),
            10.0,
        )

    def test_queue_status_message_prefers_matchmaking_operation_notice(self):
        poll = {
            "status": "waiting",
            "message": "Waiting for match assignment...",
            "matchmaking": {
                "mode": "draining",
                "accepting_new_matches": False,
                "message": "Arena update in progress. Live matches continue.",
            },
        }

        self.assertEqual(
            runner._queue_status_message(poll),
            "Arena update in progress. Live matches continue.",
        )

    def test_queue_status_message_keeps_legacy_message_when_open(self):
        poll = {
            "status": "waiting",
            "message": "Waiting for match assignment...",
            "matchmaking": {
                "mode": "open",
                "accepting_new_matches": True,
            },
        }

        self.assertEqual(
            runner._queue_status_message(poll),
            "Waiting for match assignment...",
        )

    def test_hermes_budget_uses_server_reserve_instead_of_remaining_fraction(self):
        poll = {"turn_deadline": "1970-01-01T00:01:15.490000+00:00"}
        with (
            mock.patch.dict(os.environ, {"CLAWARENA_BRAIN": "hermes"}),
            mock.patch.object(runner.time, "time", return_value=0.0),
        ):
            budget = runner._decision_budget(poll)

        self.assertEqual(budget["configured_seconds"], 105.0)
        # 105 is not reachable at this deadline: the server reserve wins,
        # which is the policy this test exists to pin.
        self.assertAlmostEqual(budget["effective_seconds"], 63.49)
        self.assertAlmostEqual(budget["server_remaining_seconds"], 75.49)
        self.assertEqual(budget["submit_reserve_seconds"], 12.0)
        self.assertEqual(budget["policy"], "deadline_submit_reserve")

    def test_diplomacy_budget_leaves_fifteen_seconds(self):
        poll = {
            "game_type": "diplomacy",
            "turn_deadline": "1970-01-01T00:03:00+00:00",
        }
        with (
            mock.patch.dict(os.environ, {"CLAWARENA_BRAIN": "starter"}),
            mock.patch.object(runner.time, "time", return_value=0.0),
        ):
            budget = runner._decision_budget(poll)

        self.assertEqual(budget["configured_seconds"], 165.0)
        self.assertEqual(budget["effective_seconds"], 165.0)
        self.assertEqual(
            budget["server_remaining_seconds"] - budget["effective_seconds"],
            15.0,
        )

    def test_hermes_budget_shrinks_only_to_preserve_server_submit_reserve(self):
        poll = {"turn_deadline": "1970-01-01T00:00:50+00:00"}
        with (
            mock.patch.dict(os.environ, {"CLAWARENA_BRAIN": "hermes"}),
            mock.patch.object(runner.time, "time", return_value=0.0),
        ):
            budget = runner._decision_budget(poll)

        self.assertEqual(budget["effective_seconds"], 38.0)
        self.assertEqual(
            budget["server_remaining_seconds"] - budget["effective_seconds"],
            12.0,
        )

    def test_default_budget_spends_the_window_minus_the_submit_reserve(self):
        """The old policy took `remaining * 0.45` on top of a window the server
        had already bounded, to keep room for a retry inside the same turn. A
        reasoning call runs 20-40s, so the half held back starved the first
        attempt and the retry could not have fitted anyway."""
        poll = {"turn_deadline": "1970-01-01T00:01:21.333333+00:00"}
        with (
            mock.patch.dict(os.environ, {"CLAWARENA_BRAIN": "starter"}),
            mock.patch.object(runner.time, "time", return_value=0.0),
        ):
            budget = runner._decision_budget(poll)

        self.assertAlmostEqual(budget["effective_seconds"], 73.33, places=2)
        self.assertEqual(
            budget["server_remaining_seconds"] - budget["effective_seconds"],
            8.0,
        )
        self.assertEqual(budget["policy"], "deadline_submit_reserve")

    def test_poll_retry_backoff_resets_after_success(self):
        retry_attempts = []

        def retry_delay(failure_count, status_code):
            retry_attempts.append((failure_count, status_code))
            return 0.01

        with mock.patch.object(runner, "_poll_retry_delay", side_effect=retry_delay):
            result, actions, moves = self.run_loop(
                [
                    (502, {"detail": "deploying"}),
                    (404, {"detail": "router replacing service"}),
                    (200, {"status": "idle", "message": "Waiting"}),
                    (503, {"detail": "warming"}),
                    (401, {"detail": "stop"}),
                ],
                (200, {"status": "ok"}),
                ["runner.py"],
            )

        self.assertEqual(result, 1)
        self.assertEqual(retry_attempts, [(1, 502), (2, 404), (1, 503)])
        self.assertEqual(actions, [])
        self.assertEqual(moves, [])

    def test_model_preflight_failure_stops_before_arena_connection(self):
        connection_token = mock.Mock(return_value="token")
        with (
            mock.patch.dict(os.environ, {"LLM_API_KEY": "test"}, clear=False),
            mock.patch.object(sys, "argv", ["runner.py"]),
            mock.patch.object(runner.brain, "preflight", side_effect=RuntimeError("bad key")),
            mock.patch.object(runner.arena_client, "connection_token", connection_token),
        ):
            result = runner.main()

        self.assertEqual(result, 2)
        connection_token.assert_not_called()

    def test_preflight_only_never_polls_or_joins_a_match(self):
        result, actions, moves = self.run_loop(
            [],
            (200, {"status": "ok"}),
            ["runner.py", "--preflight-only"],
        )

        self.assertEqual(result, 0)
        self.assertEqual(actions, [])
        self.assertEqual(moves, [])

    def test_stale_finished_projection_does_not_consume_match_limit(self):
        polls = deque([
            (200, {"status": "finished", "match_id": 999}),
            (200, self.playing(1000, 1)),
            (200, {"status": "finished", "match_id": 1000}),
        ])

        result, actions, moves = self.run_loop(
            polls,
            (200, {"status": "ok", "ack_type": "ok"}),
            ["runner.py", "--matches", "1"],
        )

        self.assertEqual(result, 0)
        self.assertEqual(len(actions), 1)
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0][0], 1000)

    def test_match_limit_finishes_already_assigned_next_match(self):
        polls = deque([
            (200, self.playing(101, 1)),
            (200, self.playing(102, 2)),
            (200, {"status": "finished", "match_id": 102}),
        ])

        result, actions, moves = self.run_loop(
            polls,
            (200, {"status": "ok", "ack_type": "ok"}),
            ["runner.py", "--matches", "1"],
        )

        self.assertEqual(result, 0)
        self.assertEqual(len(actions), 2)
        self.assertEqual(len(moves), 2)
        self.assertEqual([m[0] for m in moves], [101, 102])

    def test_rejected_action_does_not_commit_move_or_memo(self):
        polls = deque([
            (200, self.playing(201, 1)),
            (401, {"status": "error"}),
        ])

        result, _actions, moves = self.run_loop(
            polls,
            (409, {"status": "error", "code": "idempotency_key_reused"}),
            ["runner.py"],
        )

        self.assertEqual(result, 1)
        self.assertEqual(moves, [])

    def test_explicit_already_queued_code_commits_ack(self):
        polls = deque([
            (200, self.playing(301, 1)),
            (401, {"status": "error"}),
        ])

        result, _actions, moves = self.run_loop(
            polls,
            (409, {"status": "error", "code": "action_already_queued"}),
            ["runner.py"],
        )

        self.assertEqual(result, 1)
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0][0], 301)

    def test_lost_ack_retries_exact_payload_without_second_decision(self):
        polls = deque([
            (200, self.playing(401, 1)),
            (401, {"status": "error"}),
        ])
        act_results = deque([
            (0, {"status": "error", "message": "connection reset"}),
            (200, {"status": "ok", "ack_type": "cached_ack"}),
        ])

        result, actions, moves = self.run_loop(
            polls,
            act_results,
            ["runner.py"],
        )

        self.assertEqual(result, 1)
        self.assertEqual(len(actions), 2)
        self.assertEqual(actions[0], actions[1])
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0][0], 401)

    def test_changed_seq_does_not_repeat_same_action_window(self):
        first = self.playing(501, 1)
        first["action_window_id"] = "stable-window"
        noisy_update = self.playing(501, 2)
        noisy_update["action_window_id"] = "stable-window"
        polls = deque([
            (200, first),
            (200, noisy_update),
            (200, {"status": "finished", "match_id": 501}),
        ])

        result, actions, moves = self.run_loop(
            polls,
            (200, {"status": "ok", "ack_type": "ok"}),
            ["runner.py", "--matches", "1"],
        )

        self.assertEqual(result, 0)
        self.assertEqual(len(actions), 1)
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0][0], 501)

    def test_turn_updating_409_replays_cached_decision_without_second_inference(self):
        first = self.playing(511, 1)
        first["action_window_id"] = "stable-window"
        second = self.playing(511, 2)
        second["action_window_id"] = "stable-window"
        decide = mock.Mock(return_value={
            "action": "challenge",
            "params": {},
            "memo": "one decision",
        })
        result, actions, moves = self.run_loop(
            deque([
                (200, first),
                (200, second),
                (200, {"status": "finished", "match_id": 511}),
            ]),
            deque([
                (409, {"status": "error", "message": "The turn is updating; poll and retry"}),
                (200, {"status": "ok", "ack_type": "las_vegas_action_ack"}),
            ]),
            ["runner.py", "--matches", "1"],
            decide=decide,
        )

        self.assertEqual(result, 0)
        self.assertEqual(decide.call_count, 1)
        self.assertEqual(len(actions), 2)
        self.assertEqual(actions[0], actions[1])
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0][0], 511)

    def test_redeploy_308_replays_cached_decision_without_second_inference(self):
        first = self.playing(512, 1)
        first["action_window_id"] = "deploy-window"
        second = self.playing(512, 2)
        second["action_window_id"] = "deploy-window"
        decide = mock.Mock(return_value={
            "action": "challenge",
            "params": {},
            "memo": "one deploy-safe decision",
        })

        result, actions, moves = self.run_loop(
            deque([
                (200, first),
                (200, second),
                (200, {"status": "finished", "match_id": 512}),
            ]),
            deque([
                (308, {"status": "error", "message": "unparsable response body"}),
                (308, {"status": "error", "message": "unparsable response body"}),
                (200, {"status": "ok", "ack_type": "las_vegas_action_ack"}),
            ]),
            ["runner.py", "--matches", "1"],
            decide=decide,
        )

        self.assertEqual(result, 0)
        self.assertEqual(decide.call_count, 1)
        self.assertEqual(len(actions), 3)
        self.assertEqual(actions[0], actions[1])
        self.assertEqual(actions[1], actions[2])
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0][0], 512)

    def test_diplomacy_server_rejection_is_injected_for_one_corrective_turn(self):
        first = self.playing_diplomacy(601, 1)
        second = self.playing_diplomacy(601, 2)
        decide = mock.Mock(side_effect=[
            {
                "action": "send_press",
                "params": {
                    "messages": [],
                    "strategy_intent": {"avoid_provinces": ["NOR"]},
                },
            },
            {
                "action": "send_press",
                "params": {
                    "messages": [],
                    "strategy_intent": {"avoid_provinces": ["NWY"]},
                },
            },
        ])
        act_results = deque([
            (400, {
                "status": "error",
                "code": "unknown_diplomacy_province",
                "message": "strategy_intent.avoid_provinces[0] is not a known province",
                "field": "strategy_intent.avoid_provinces[0]",
                "invalid_value": "NOR",
                "allowed_values": ["NWY"],
            }),
            (200, {"status": "ok", "ack_type": "diplomacy_action_ack"}),
        ])

        result, actions, moves = self.run_loop(
            deque([
                (200, first),
                (200, second),
                (200, {"status": "finished", "match_id": 601}),
            ]),
            act_results,
            ["runner.py", "--matches", "1"],
            decide=decide,
        )

        self.assertEqual(result, 0)
        self.assertEqual(decide.call_count, 2)
        feedback = decide.call_args_list[1].args[0]["action_rejection"]
        self.assertEqual(feedback["field"], "strategy_intent.avoid_provinces[0]")
        self.assertEqual(feedback["invalid_value"], "NOR")
        self.assertEqual(feedback["allowed_values"], ["NWY"])
        self.assertEqual(len(actions), 2)
        self.assertEqual(len(moves), 1)

    def test_second_diplomacy_rejection_uses_server_fallback_without_third_model_call(self):
        decide = mock.Mock(return_value={
            "action": "send_press",
            "params": {
                "messages": [],
                "strategy_intent": {"avoid_provinces": ["NOR"]},
            },
        })
        rejection = (400, {
            "status": "error",
            "code": "unknown_diplomacy_province",
            "message": "strategy_intent.avoid_provinces[0] is not a known province",
            "field": "strategy_intent.avoid_provinces[0]",
            "invalid_value": "NOR",
            "allowed_values": ["NWY"],
        })

        result, actions, moves = self.run_loop(
            deque([
                (200, self.playing_diplomacy(701, 1)),
                (200, self.playing_diplomacy(701, 2)),
                (200, self.playing_diplomacy(701, 3)),
                (200, {"status": "finished", "match_id": 701}),
            ]),
            deque([
                rejection,
                rejection,
                (200, {"status": "ok", "ack_type": "diplomacy_action_ack"}),
            ]),
            ["runner.py", "--matches", "1"],
            decide=decide,
        )

        self.assertEqual(result, 0)
        self.assertEqual(decide.call_count, 2)
        self.assertEqual(len(actions), 3)
        self.assertEqual(actions[-1]["params"], {"messages": []})
        self.assertEqual(len(moves), 1)

    def test_non_correctable_diplomacy_conflict_waits_for_server_state_change(self):
        decide = mock.Mock(return_value={
            "action": "send_press",
            "params": {"messages": []},
        })

        result, actions, moves = self.run_loop(
            deque([
                (200, self.playing_diplomacy(801, 1)),
                (200, self.playing_diplomacy(801, 2)),
                (200, {"status": "finished", "match_id": 801}),
            ]),
            (409, {"status": "error", "code": "stale_phase"}),
            ["runner.py", "--matches", "1"],
            decide=decide,
        )

        self.assertEqual(result, 0)
        self.assertEqual(decide.call_count, 1)
        self.assertEqual(len(actions), 1)
        self.assertEqual(moves, [])


class DistributionParityTests(unittest.TestCase):
    def test_there_is_no_second_copy_of_the_kit(self):
        """The mirror under Next's public directory must stay gone.

        It existed to serve the kit at ``/kit/<file>`` and had to be re-copied
        by hand after every edit. It drifted — a release behind for a day, and
        missing ``report_sink.py`` entirely — so Django now serves the one
        canonical directory. Anything that recreates it (a fixture generator
        writing two targets, a helpful re-copy) brings back the drift, so the
        absence is the assertion.
        """

        mirror = REPO_DIR / "frontend" / "public" / "kit"
        # Ignore __pycache__: a worktree that predates the removal keeps stale
        # build output there, which is residue rather than a second copy.
        resurrected = sorted(
            str(path.relative_to(mirror))
            for path in mirror.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        ) if mirror.exists() else []
        self.assertEqual(
            resurrected,
            [],
            "frontend/public/kit is back; /kit is served from kit/ by Django",
        )


if __name__ == "__main__":
    unittest.main()


class MatchStateTests(unittest.TestCase):
    """The transport-side board materializer."""

    def turn(self, *, mode, state, removed=(), seq=0, checksum=None):
        payload = {"state_mode": mode, "state": state, "state_removed": list(removed)}
        if seq:
            payload["state_seq"] = seq
        if checksum is not None:
            payload["state_checksum"] = checksum
        return payload

    def test_full_then_delta_rebuilds_the_complete_board(self):
        held = match_state.MatchState()
        base = {"phase": "day", "chat": [{"m": "one"}], "dropme": 1}

        first = held.ingest(
            self.turn(mode="full", state=base, seq=1, checksum=match_state.checksum(base)),
            match_id=9, game_type="mafia",
        )
        self.assertEqual(first, base)
        self.assertEqual(held.ack(), 1)

        after = {"phase": "night", "chat": [{"m": "one"}, {"m": "two"}]}
        second = held.ingest(
            self.turn(
                mode="delta",
                state={"phase": "night", "chat": {"_appended": [{"m": "two"}]}},
                removed=["dropme"],
                seq=2,
                checksum=match_state.checksum(after),
            ),
            match_id=9, game_type="mafia",
        )
        self.assertEqual(second, after)
        self.assertEqual(held.ack(), 2)

    def test_literal_escape_is_not_treated_as_an_append(self):
        held = match_state.MatchState()
        held.ingest(self.turn(mode="full", state={"advice": ["old"]}, seq=1),
                    match_id=9, game_type="mafia")

        board = held.ingest(
            self.turn(
                mode="delta",
                state={"advice": {"_literal": {"_appended": ["verbatim"]}}},
                seq=2,
            ),
            match_id=9, game_type="mafia",
        )

        self.assertEqual(board, {"advice": {"_appended": ["verbatim"]}})

    def test_checksum_mismatch_demands_a_fresh_baseline(self):
        held = match_state.MatchState()
        held.ingest(self.turn(mode="full", state={"n": 1}, seq=1), match_id=9, game_type="mafia")

        board = held.ingest(
            self.turn(mode="delta", state={"n": 2}, seq=2, checksum="not-the-real-one"),
            match_id=9, game_type="mafia",
        )

        self.assertIsNone(board)
        self.assertIn("checksum", held.last_error)
        # And it forgets the board rather than carrying a suspect one forward.
        self.assertIsNone(held.ack())

    def test_delta_without_a_baseline_demands_one(self):
        held = match_state.MatchState()

        board = held.ingest(
            self.turn(mode="delta", state={"n": 2}, seq=5),
            match_id=9, game_type="mafia",
        )

        self.assertIsNone(board)
        self.assertIn("no baseline", held.last_error)

    def test_append_to_a_key_we_do_not_hold_is_refused(self):
        held = match_state.MatchState()
        held.ingest(self.turn(mode="full", state={"n": 1}, seq=1), match_id=9, game_type="mafia")

        board = held.ingest(
            self.turn(mode="delta", state={"chat": {"_appended": [{"m": "x"}]}}, seq=2),
            match_id=9, game_type="mafia",
        )

        self.assertIsNone(board)
        self.assertIn("append", held.last_error)

    def test_a_new_match_starts_from_nothing(self):
        held = match_state.MatchState()
        held.ingest(self.turn(mode="full", state={"n": 1}, seq=1), match_id=9, game_type="mafia")

        board = held.ingest(
            self.turn(mode="delta", state={"n": 2}, seq=2),
            match_id=10, game_type="mafia",
        )

        self.assertIsNone(board)
        self.assertIn("no baseline", held.last_error)


class HermesMaxTurnsTests(unittest.TestCase):
    """The agent loop needs somewhere to go when a turn produces no message."""

    def _command_for(self, max_turns):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return mock.Mock(
                returncode=0,
                stdout=(
                    f"Warning: Unknown toolsets: {hermes_agent.HERMES_NO_TOOLS_SENTINEL}\n"
                    '{"action":"challenge","params":{}}\n'
                ),
                stderr="session_id: s\n",
            )

        with (
            mock.patch.object(hermes_agent, "HERMES_STATELESS_GAMEPLAY", False),
            mock.patch.object(hermes_agent, "HERMES_CONTAINER", ""),
            mock.patch.object(hermes_agent, "HERMES_BIN", "hermes"),
            mock.patch.object(hermes_agent, "HERMES_MAX_TURNS", max_turns),
            mock.patch.object(hermes_agent.subprocess, "run", side_effect=fake_run),
        ):
            hermes_agent._run_chat("prompt", None, 60)
        return calls[0]

    def test_the_default_opens_the_whole_recovery_ladder(self):
        """1 initial call + 2 thinking-prefill + 3 empty-content retries.

        Anything less leaves a rung Hermes would have used unreachable, and a
        cap of 1 -- the value this shipped with -- left all six unreachable.
        """

        self.assertGreaterEqual(hermes_agent.HERMES_MAX_TURNS, 6)

    def test_the_limit_is_what_reaches_the_command(self):
        for limit in (1, 2, 4):
            with self.subTest(limit=limit):
                command = self._command_for(limit)
                self.assertEqual(
                    command[command.index("--max-turns") + 1], str(limit),
                )

    def test_a_nonsense_setting_cannot_disable_the_turn(self):
        """0 or a negative would leave the loop no turn at all."""

        for raw in ("0", "-3", "", "not a number"):
            with self.subTest(raw=raw):
                with mock.patch.dict(os.environ, {"HERMES_MAX_TURNS": raw}):
                    module = importlib.reload(hermes_agent)
                    try:
                        self.assertGreaterEqual(module.HERMES_MAX_TURNS, 1)
                    finally:
                        importlib.reload(hermes_agent)
