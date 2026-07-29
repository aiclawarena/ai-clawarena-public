from __future__ import annotations

import argparse
import base64
import functools
import io
import json
import os
import re
import shlex
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
REPO_DIR = KIT_DIR.parents[1]
sys.path.insert(0, str(KIT_DIR))

import helpers  # noqa: E402
import arena_client  # noqa: E402
import agent as heuristic_agent  # noqa: E402
import check as offline_check  # noqa: E402
import hermes_agent  # noqa: E402
import llm_agent  # noqa: E402
import memory  # noqa: E402
import reflect  # noqa: E402
import runner  # noqa: E402
import run_local  # noqa: E402
import setup_local_runner  # noqa: E402
import setup_starter_kit  # noqa: E402


class ProtocolTests(unittest.TestCase):
    def test_low_level_client_never_reads_the_legacy_global_token_implicitly(self):
        with (
            mock.patch.dict(os.environ, {"CLAWARENA_CONNECTION_TOKEN": ""}),
            mock.patch.object(arena_client, "TOKEN_PATH", None),
            self.assertRaisesRegex(SystemExit, "Use run_local.py"),
        ):
            arena_client.connection_token()

    def test_public_release_files_and_versions_stay_in_sync(self):
        release_files = {
            *setup_local_runner.KIT_FILES,
            *setup_starter_kit.CORE_FILES,
            *setup_starter_kit.USER_FILES,
            *(f"fixtures/{name}" for name in setup_starter_kit.FIXTURE_FILES),
            *(f"strategy/{name}" for name in setup_starter_kit.STRATEGY_FILES),
            "setup_local_runner.py",
        }
        for name in sorted(release_files):
            self.assertTrue((KIT_DIR / name).is_file(), name)

        openclaw_dir = REPO_DIR / "integrations" / "openclaw"
        skill_md = (openclaw_dir / "SKILL.md").read_text()
        skill_version = re.search(r"^version:\s*([^\s]+)", skill_md, re.MULTILINE)
        self.assertIsNotNone(skill_version)
        self.assertEqual(skill_version.group(1), arena_client.CLIENT_VERSION)
        self.assertEqual(
            json.loads((openclaw_dir / "package.json").read_text())["version"],
            arena_client.CLIENT_VERSION,
        )

    def test_documented_multi_file_curl_globs_are_shell_safe(self):
        documented_files = [
            KIT_DIR / "BUILDER.md",
            KIT_DIR / "README.md",
            REPO_DIR / "docs" / "quickstart.md",
            REPO_DIR / "integrations" / "openclaw" / "INSTALL.md",
        ]
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
                run_checks=False,
                fetch=fetch,
            )
            self.assertEqual(first["status"], "installed")
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

    def test_private_starter_launcher_keeps_one_match_as_the_default(self):
        args = argparse.Namespace(
            continuous=False,
            matches=1,
            dry_run=False,
            no_reflect=False,
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

    def test_first_poll_can_request_an_automatic_context_resync(self):
        with mock.patch.object(arena_client, "request", return_value=(200, {})) as request_call:
            arena_client.poll("token", wait=30, resync=True, context_id="runner-1")

        self.assertEqual(
            request_call.call_args.args[1],
            "/agents/game/?wait=30&snapshot=full&consume_preferences=1"
            "&consume_history=1&resync=1&context_id=runner-1",
        )

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

    def test_match_memory_recovery_keeps_all_retained_moves_and_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous_dir = memory.MEMORY_DIR
            previous_archive = memory.ARCHIVE_DIR
            previous_current = dict(memory._current)
            try:
                memory.MEMORY_DIR = Path(tmp)
                memory.ARCHIVE_DIR = Path(tmp) / "archive"
                memory._current.update(match_id=None, data=None)
                memory.begin_turn(77, {"my_role": "citizen"})
                for index in range(20):
                    memory.record_move(77, {"action": "chat", "params": {"message": str(index)}})
                    memory.record_memo(77, f"read-{index}")

                recovered = memory.begin_turn(77, {"my_role": "citizen"})
            finally:
                memory.MEMORY_DIR = previous_dir
                memory.ARCHIVE_DIR = previous_archive
                memory._current.clear()
                memory._current.update(previous_current)

        self.assertEqual(len(recovered["my_recent_moves"]), 20)
        self.assertEqual(len(recovered["my_private_reads"]), 20)

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

    def test_shared_parser_uses_first_complete_object(self):
        text = 'prefix {"strategy_prompt":"keep","reason":"solid"} trailing {"bad":true}'
        self.assertEqual(
            reflect.extract_reflection(text),
            {"strategy_prompt": "keep", "reason": "solid"},
        )

    def test_reflection_payload_trims_only_at_a_complete_thought(self):
        context = {
            "match": {"id": 7, "game_type": "monopoly"},
            "limits": {"strategy_prompt_max_chars": 40},
            "current_strategy_prompt": "",
        }

        payload = reflect.build_save_payload(
            context,
            "Keep cash reserves. This trailing lesson is deliberately too long.",
            "durable lesson",
        )

        self.assertIsNotNone(payload)
        self.assertEqual(payload["strategy_prompt"], "Keep cash reserves.")
        self.assertEqual(
            reflect._truncate_strategy_prompt("alpha beta gamma delta", 12),
            "alpha beta…",
        )

    def test_action_parser_returns_memo_without_writing_memory(self):
        with mock.patch.object(memory, "record_memo") as record_memo:
            move = llm_agent._parse_action(
                '{"action":"bid","params":{"quantity":2},"memo":"private read"}',
                [{"action": "bid"}],
                {"game_type": "liars_dice"},
            )

        self.assertEqual(move["memo"], "private read")
        record_memo.assert_not_called()


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
        self.assertNotIn("my_memory", baseline["state"])
        self.assertEqual(baseline["my_memory"]["my_recent_moves"], [])
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
        self.assertEqual(
            update["my_memory_delta"]["my_recent_moves"],
            {"_appended": [{"action": "chat", "params": {"message": "first"}}]},
        )
        self.assertNotIn("my_memory", update)

    def test_diplomacy_context_epoch_bounds_session_without_splitting_one_season(self):
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
        self.assertEqual(next_pending["mode"], "epoch")
        self.assertEqual([message["role"] for message in next_season], ["system", "user"])
        self.assertIn("STATE_BASELINE:\n", next_season[1]["content"])
        self.assertEqual(next_pending["prior_turn_count"], 0)

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

        with mock.patch.object(llm_agent.memory, "current_match_id", return_value=41):
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

    def test_diplomacy_has_separate_completion_headroom(self):
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

        self.assertEqual(calls, [
            llm_agent.LLM_DIPLOMACY_MAX_TOKENS,
            llm_agent.LLM_MAX_TOKENS,
        ])
        self.assertEqual(
            diplomacy_pending["max_completion_tokens"],
            llm_agent.LLM_DIPLOMACY_MAX_TOKENS,
        )
        self.assertEqual(
            mafia_pending["max_completion_tokens"],
            llm_agent.LLM_MAX_TOKENS,
        )

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
        self.assertIn(
            f"configured {llm_agent.LLM_DIPLOMACY_MAX_TOKENS} token limit",
            log,
        )
        self.assertIn("raise LLM_MAX_TOKENS", log)

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

    def test_checker_rejects_unlisted_press_contract_identifiers(self):
        fixture = self.fixture("diplomacy_negotiation")
        hint = fixture["legal_actions"][0]["hint"]
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
        press = {
            "action": "send_press",
            "params": {"messages": [{"to_power": "GLOBAL", "content": "Hello"}]},
        }
        self.assertEqual(
            offline_check.check_move(self.fixture("diplomacy_negotiation"), press),
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

    def test_invalid_batch_gets_one_corrective_retry_that_can_win(self):
        fixture = self.fixture("diplomacy_movement")
        bad = json.dumps({"action": "submit_orders", "params": {"orders": [
            {"type": "MOVE", "origin": "EDI", "destination": "PAR"},
        ]}})
        good = json.dumps({"action": "submit_orders", "params": {"orders": [
            {"type": "MOVE", "origin": "EDI", "destination": "NTH"},
        ]}})

        move, chat, _log = self._decide(fixture, [bad, good])

        self.assertEqual(move["params"]["orders"], [
            {"type": "MOVE", "origin": "EDI", "destination": "NTH"},
        ])
        self.assertEqual(chat.call_count, 2)
        retry_messages = chat.call_args_list[1].args[3]
        self.assertEqual(retry_messages[-1]["role"], "user")
        self.assertIn("ORDER_VALIDATION_FAILED", retry_messages[-1]["content"])
        self.assertIn("is not hinted", retry_messages[-1]["content"])
        # The committed transcript ends with the accepted retry reply.
        self.assertEqual(llm_agent._SESSION["messages"][-1]["content"], good)

    def test_still_invalid_retry_degrades_only_the_offending_orders(self):
        fixture = self.fixture("diplomacy_movement")
        bad = json.dumps({"action": "submit_orders", "params": {"orders": [
            {"type": "MOVE", "origin": "EDI", "destination": "PAR"},
            {"type": "HOLD", "origin": "LON"},
        ]}})

        move, chat, log = self._decide(fixture, [bad, bad])

        self.assertEqual(chat.call_count, 2)
        self.assertEqual(move["action"], "submit_orders")
        self.assertEqual(move["params"]["orders"], [
            {"type": "HOLD", "origin": "EDI"},
            {"type": "HOLD", "origin": "LON"},
        ])
        self.assertIn("degraded to stay hint-legal", log)
        self.assertEqual(
            offline_check.check_move(fixture, move),
            [],
        )

    def test_valid_batches_and_other_games_never_trigger_a_retry(self):
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
            mock.patch.object(hermes_agent, "HERMES_CONTAINER", ""),
            mock.patch.object(hermes_agent, "HERMES_BIN", "hermes"),
            mock.patch.object(hermes_agent.subprocess, "run", side_effect=fake_run),
        ):
            text, sid = hermes_agent._run_chat("prompt", None, 60)

        command = calls[0][0]
        self.assertEqual(command[command.index("-t") + 1], hermes_agent.HERMES_NO_TOOLS_SENTINEL)
        self.assertNotIn("--yolo", command)
        self.assertEqual(text, '{"action":"challenge","params":{}}')
        self.assertEqual(sid, "session-new")

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
        with mock.patch.object(hermes_agent.subprocess, "run", return_value=proc):
            text, sid = hermes_agent._run_chat("prompt", None, 60)

        self.assertEqual(text, "CLAWARENA_READY")
        self.assertEqual(sid, "latest-hermes-preflight")

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
        self.assertEqual(
            payload["my_memory"],
            {"my_recent_moves": [{"action": "roll"}]},
        )

    def test_diplomacy_server_epoch_starts_fresh_hermes_baseline(self):
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
        saved_epochs = []
        old_last = dict(hermes_agent._LAST)

        def fake_chat(prompt, session_id, timeout):
            prompts.append((prompt, session_id, timeout))
            return '{"action":"send_press","params":{"messages":[]}}', "fall-session"

        try:
            hermes_agent._LAST.update(
                sid="spring-session",
                board={"phase": "S1902M-ORDERS"},
                turn_count=3,
                context_epoch="S1902M",
            )
            with (
                mock.patch.object(hermes_agent.memory, "get_hermes_session", return_value="spring-session"),
                mock.patch.object(hermes_agent.memory, "get_hermes_session_turn_count", return_value=3),
                mock.patch.object(hermes_agent.memory, "get_hermes_context_epoch", return_value="S1902M"),
                mock.patch.object(hermes_agent.memory, "clear_hermes_session", side_effect=lambda: cleared.append(True)),
                mock.patch.object(hermes_agent.memory, "set_hermes_session", side_effect=saved_sessions.append),
                mock.patch.object(hermes_agent.memory, "set_hermes_session_turn_count"),
                mock.patch.object(hermes_agent.memory, "set_hermes_context_epoch", side_effect=saved_epochs.append),
                mock.patch.object(hermes_agent, "_run_chat", side_effect=fake_chat),
            ):
                move = hermes_agent.decide(state, legal)
        finally:
            hermes_agent._LAST.clear()
            hermes_agent._LAST.update(old_last)

        self.assertEqual(move, {"action": "send_press", "params": {"messages": []}})
        self.assertEqual(cleared, [True])
        self.assertEqual(prompts[0][1], None)
        self.assertFalse(prompts[0][0].startswith(hermes_agent._RESUMED_CONTRACT))
        payload = json.loads(prompts[0][0].rsplit("GAME:\n", 1)[1])
        self.assertIn("state", payload)
        self.assertNotIn("state_delta", payload)
        self.assertEqual(saved_sessions, ["fall-session"])
        self.assertEqual(saved_epochs, ["F1902M"])

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


class MemoryTests(unittest.TestCase):
    def test_ack_commits_are_atomic_and_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "memory"
            with (
                mock.patch.object(memory, "MEMORY_DIR", root),
                mock.patch.object(memory, "ARCHIVE_DIR", root / "archive"),
                mock.patch.object(memory, "_current", {"match_id": None, "data": None}),
            ):
                memory.begin_turn(41, {"my_role": "doctor"})
                memory.record_move(41, {"action": "save", "params": {"target_id": 8}})
                memory.record_memo(41, "protect 8 again")
                memory.set_hermes_session("segment-1")
                memory.set_hermes_session_turn_count(12)
                memory.set_hermes_context_epoch("S1902M")

                live = root / "41.json"
                self.assertTrue(live.exists())
                self.assertEqual(stat.S_IMODE(live.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
                self.assertEqual(list(root.glob(".41.json.*")), [])
                self.assertEqual(memory.match_summary(41)["my_memos"], ["protect 8 again"])
                self.assertEqual(memory.get_hermes_session(), "segment-1")
                self.assertEqual(memory.get_hermes_session_turn_count(), 12)
                self.assertEqual(memory.get_hermes_context_epoch(), "S1902M")

                memory.end_match(41)
                self.assertFalse(live.exists())
                self.assertEqual(memory.match_summary(41)["my_role"], "doctor")


class ReflectionWorkerTests(unittest.TestCase):
    def test_submit_does_not_wait_for_slow_reflection(self):
        started = threading.Event()
        release = threading.Event()

        def slow_reflection(*_args, **_kwargs):
            started.set()
            release.wait(2)

        with (
            mock.patch.dict(os.environ, {}, clear=False),
            mock.patch.object(reflect, "maybe_reflect", side_effect=slow_reflection),
        ):
            os.environ.pop("CLAWARENA_NO_REFLECT", None)
            worker = runner.ReflectionWorker("token")
            before = time.monotonic()
            worker.submit(12, {"strategy_self_learning_enabled": True})
            elapsed = time.monotonic() - before
            self.assertTrue(started.wait(1))
            self.assertLess(elapsed, 0.2)
            release.set()
            worker.close(wait=True, timeout=2)


class SetupTests(unittest.TestCase):
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
                mock.patch.object(time, "monotonic", side_effect=[0.0, 100.0]),
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
                    "#!/bin/sh\nprintf 'Warning: Unknown toolsets: __clawarena_no_tools_v1__\\n'\n"
                    "printf 'CLAWARENA_READY\\n'\n"
                    "printf 'session_id: setup-preflight\\n' >&2\nexit 0\n"
                )
                hermes.chmod(0o700)
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
        recorded_moves = []
        recorded_memos = []
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
            mock.patch.object(runner.arena_client, "poll", side_effect=list(polls)),
            mock.patch.object(runner.arena_client, "act", side_effect=act),
            mock.patch.object(runner.brain, "preflight", return_value="test-model"),
            mock.patch.object(runner.brain, "decide", decide),
            mock.patch.object(runner.memory, "begin_turn", return_value={}),
            mock.patch.object(runner.memory, "end_match"),
            mock.patch.object(runner.memory, "record_move", side_effect=lambda *args, **kwargs: recorded_moves.append(args)),
            mock.patch.object(runner.memory, "record_memo", side_effect=lambda *args: recorded_memos.append(args)),
            mock.patch.object(runner.time, "sleep"),
        ):
            result = runner.main()

        return result, actions, recorded_moves, recorded_memos

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

    def test_poll_retry_backoff_resets_after_success(self):
        retry_attempts = []

        def retry_delay(failure_count, status_code):
            retry_attempts.append((failure_count, status_code))
            return 0.01

        with mock.patch.object(runner, "_poll_retry_delay", side_effect=retry_delay):
            result, actions, moves, memos = self.run_loop(
                [
                    (502, {"detail": "deploying"}),
                    (404, {"detail": "router replacing service"}),
                    (200, {"status": "idle", "message": "Waiting"}),
                    (503, {"detail": "warming"}),
                    (401, {"detail": "stop"}),
                ],
                (200, {"status": "ok"}),
                ["runner.py", "--no-reflect"],
            )

        self.assertEqual(result, 1)
        self.assertEqual(retry_attempts, [(1, 502), (2, 404), (1, 503)])
        self.assertEqual(actions, [])
        self.assertEqual(moves, [])
        self.assertEqual(memos, [])

    def test_model_preflight_failure_stops_before_arena_connection(self):
        connection_token = mock.Mock(return_value="token")
        with (
            mock.patch.dict(os.environ, {"LLM_API_KEY": "test"}, clear=False),
            mock.patch.object(sys, "argv", ["runner.py", "--no-reflect"]),
            mock.patch.object(runner.brain, "preflight", side_effect=RuntimeError("bad key")),
            mock.patch.object(runner.arena_client, "connection_token", connection_token),
        ):
            result = runner.main()

        self.assertEqual(result, 2)
        connection_token.assert_not_called()

    def test_preflight_only_never_polls_or_joins_a_match(self):
        result, actions, moves, memos = self.run_loop(
            [],
            (200, {"status": "ok"}),
            ["runner.py", "--preflight-only", "--no-reflect"],
        )

        self.assertEqual(result, 0)
        self.assertEqual(actions, [])
        self.assertEqual(moves, [])
        self.assertEqual(memos, [])

    def test_stale_finished_projection_does_not_consume_match_limit(self):
        polls = deque([
            (200, {"status": "finished", "match_id": 999}),
            (200, self.playing(1000, 1)),
            (200, {"status": "finished", "match_id": 1000}),
        ])

        result, actions, moves, memos = self.run_loop(
            polls,
            (200, {"status": "ok", "ack_type": "ok"}),
            ["runner.py", "--matches", "1", "--no-reflect"],
        )

        self.assertEqual(result, 0)
        self.assertEqual(len(actions), 1)
        self.assertEqual(len(moves), 1)
        self.assertEqual(memos, [(1000, "confirmed read")])

    def test_match_limit_finishes_already_assigned_next_match(self):
        polls = deque([
            (200, self.playing(101, 1)),
            (200, self.playing(102, 2)),
            (200, {"status": "finished", "match_id": 102}),
        ])

        result, actions, moves, memos = self.run_loop(
            polls,
            (200, {"status": "ok", "ack_type": "ok"}),
            ["runner.py", "--matches", "1", "--no-reflect"],
        )

        self.assertEqual(result, 0)
        self.assertEqual(len(actions), 2)
        self.assertEqual(len(moves), 2)
        self.assertEqual(memos, [(101, "confirmed read"), (102, "confirmed read")])

    def test_rejected_action_does_not_commit_move_or_memo(self):
        polls = deque([
            (200, self.playing(201, 1)),
            (401, {"status": "error"}),
        ])

        result, _actions, moves, memos = self.run_loop(
            polls,
            (409, {"status": "error", "code": "idempotency_key_reused"}),
            ["runner.py", "--no-reflect"],
        )

        self.assertEqual(result, 1)
        self.assertEqual(moves, [])
        self.assertEqual(memos, [])

    def test_explicit_already_queued_code_commits_ack(self):
        polls = deque([
            (200, self.playing(301, 1)),
            (401, {"status": "error"}),
        ])

        result, _actions, moves, memos = self.run_loop(
            polls,
            (409, {"status": "error", "code": "action_already_queued"}),
            ["runner.py", "--no-reflect"],
        )

        self.assertEqual(result, 1)
        self.assertEqual(len(moves), 1)
        self.assertEqual(memos, [(301, "confirmed read")])

    def test_lost_ack_retries_exact_payload_without_second_decision(self):
        polls = deque([
            (200, self.playing(401, 1)),
            (401, {"status": "error"}),
        ])
        act_results = deque([
            (0, {"status": "error", "message": "connection reset"}),
            (200, {"status": "ok", "ack_type": "cached_ack"}),
        ])

        result, actions, moves, memos = self.run_loop(
            polls,
            act_results,
            ["runner.py", "--no-reflect"],
        )

        self.assertEqual(result, 1)
        self.assertEqual(len(actions), 2)
        self.assertEqual(actions[0], actions[1])
        self.assertEqual(len(moves), 1)
        self.assertEqual(memos, [(401, "confirmed read")])

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

        result, actions, moves, memos = self.run_loop(
            polls,
            (200, {"status": "ok", "ack_type": "ok"}),
            ["runner.py", "--matches", "1", "--no-reflect"],
        )

        self.assertEqual(result, 0)
        self.assertEqual(len(actions), 1)
        self.assertEqual(len(moves), 1)
        self.assertEqual(memos, [(501, "confirmed read")])

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

        result, actions, moves, _memos = self.run_loop(
            deque([
                (200, first),
                (200, second),
                (200, {"status": "finished", "match_id": 601}),
            ]),
            act_results,
            ["runner.py", "--matches", "1", "--no-reflect"],
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

        result, actions, moves, _memos = self.run_loop(
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
            ["runner.py", "--matches", "1", "--no-reflect"],
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

        result, actions, moves, _memos = self.run_loop(
            deque([
                (200, self.playing_diplomacy(801, 1)),
                (200, self.playing_diplomacy(801, 2)),
                (200, {"status": "finished", "match_id": 801}),
            ]),
            (409, {"status": "error", "code": "stale_phase"}),
            ["runner.py", "--matches", "1", "--no-reflect"],
            decide=decide,
        )

        self.assertEqual(result, 0)
        self.assertEqual(decide.call_count, 1)
        self.assertEqual(len(actions), 1)
        self.assertEqual(moves, [])


if __name__ == "__main__":
    unittest.main()
