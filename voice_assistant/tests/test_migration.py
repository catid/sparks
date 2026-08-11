from __future__ import annotations

import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest


MIGRATOR = (
    pathlib.Path(__file__).resolve().parents[1]
    / "scripts"
    / "migrate-legacy-state.py"
)


class LegacyStateMigrationTests(unittest.TestCase):
    def run_migration(self, root: pathlib.Path) -> subprocess.CompletedProcess[str]:
        home = root / "home"
        old_secret = root / "etc" / "cerebrus3-voice" / "gateway.env"
        new_secret = root / "etc" / "cerberus3-voice" / "gateway.env"
        old_asr_env = root / "etc/default/cerebrus3-qwen3-asr"
        new_asr_env = root / "etc/default/cerberus3-qwen3-asr"
        old_bridge_env = root / "etc/default/cerebrus3-voice-bridge"
        new_bridge_env = root / "etc/default/cerberus3-voice-bridge"
        old_state = home / ".local/state/cerebrus-voice/openclaw"
        new_state = home / ".local/state/cerberus-voice/openclaw"
        old_workspace = home / ".local/share/cerebrus-voice/workspace"
        new_workspace = home / ".local/share/cerberus-voice/workspace"
        old_cache = home / ".cache/cerebrus-voice/openclaw"
        new_cache = home / ".cache/cerberus-voice/openclaw"
        old_asr = home / ".cache/cerebrus-voice/qwen3-asr"
        new_asr = home / ".cache/cerberus-voice/qwen3-asr"
        return subprocess.run(
            [
                sys.executable,
                str(MIGRATOR),
                "--legacy-secret",
                str(old_secret),
                "--secret",
                str(new_secret),
                "--legacy-asr-env",
                str(old_asr_env),
                "--asr-env",
                str(new_asr_env),
                "--legacy-bridge-env",
                str(old_bridge_env),
                "--bridge-env",
                str(new_bridge_env),
                "--legacy-state",
                str(old_state),
                "--state",
                str(new_state),
                "--legacy-workspace",
                str(old_workspace),
                "--workspace",
                str(new_workspace),
                "--legacy-cache",
                str(old_cache),
                "--cache",
                str(new_cache),
                "--legacy-asr-cache",
                str(old_asr),
                "--asr-cache",
                str(new_asr),
                "--service-uid",
                str(os.getuid()),
                "--service-gid",
                str(os.getgid()),
                "--secret-uid",
                str(os.getuid()),
                "--secret-gid",
                str(os.getgid()),
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_private_state_is_copied_canonicalized_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            home = root / "home"
            old_secret = root / "etc/cerebrus3-voice/gateway.env"
            old_asr_env = root / "etc/default/cerebrus3-qwen3-asr"
            old_bridge_env = root / "etc/default/cerebrus3-voice-bridge"
            old_state = home / ".local/state/cerebrus-voice/openclaw"
            old_workspace = home / ".local/share/cerebrus-voice/workspace"
            old_cache = home / ".cache/cerebrus-voice/openclaw"
            old_asr = home / ".cache/cerebrus-voice/qwen3-asr"
            for path in (
                old_secret.parent,
                old_asr_env.parent,
                old_state,
                old_workspace,
                old_cache,
                old_asr,
            ):
                path.mkdir(parents=True, mode=0o700)

            private_token = "private-fixture-token-that-must-never-be-printed"
            old_secret.write_text(
                f"OPENCLAW_GATEWAY_TOKEN={private_token}\n"
                f"VOICE_OPENCLAW_TOKEN={private_token}\n",
                encoding="ascii",
            )
            old_secret.chmod(0o600)
            old_asr_env.parent.chmod(0o755)
            old_asr_env.write_text(
                "QWEN_ASR_MAX_AUDIO_SECONDS=29\n", encoding="ascii"
            )
            old_asr_env.chmod(0o600)
            old_bridge_env.write_text("VOICE_ARM_SECONDS=31\n", encoding="ascii")
            old_bridge_env.chmod(0o600)
            old_config = {
                "models": {
                    "providers": {
                        "vllm": {"baseUrl": "http://cerebrus1:8889/v1"}
                    }
                },
                "agents": {
                    "defaults": {"workspace": str(old_workspace)},
                    "custom": "preserved",
                },
            }
            (old_state / "openclaw.json").write_text(
                json.dumps(old_config), encoding="utf-8"
            )
            (old_state / "openclaw.json").chmod(0o600)
            sessions = old_state / "agents/voice/sessions"
            sessions.mkdir(parents=True, mode=0o700)
            legacy_session = old_state / "agents/voice/sessions/session.jsonl"
            near_prefix_session = pathlib.Path(f"{old_state}-backup/session.jsonl")
            (sessions / "sessions.json").write_text(
                json.dumps(
                    {
                        "agent:voice:main": {
                            "sessionFile": str(legacy_session),
                            "displayName": "Private path remains private",
                        },
                        "agent:voice:backup": {
                            "sessionFile": str(near_prefix_session),
                        },
                    }
                ),
                encoding="utf-8",
            )
            (sessions / "turn.trajectory-path.json").write_text(
                json.dumps({"runtimeFile": str(legacy_session)}),
                encoding="utf-8",
            )
            (sessions / "turn.trajectory.jsonl").write_text(
                json.dumps(
                    {
                        "type": "session_start",
                        "data": {
                            "sessionFile": str(legacy_session),
                            "privateText": "do not rewrite cerebrus in content",
                        },
                    }
                )
                + "\n"
                + '{  "type" : "content", "data" : "preserve spacing"  }\n',
                encoding="utf-8",
            )
            private_instruction = "Private custom rule for cerebrus3."
            (old_workspace / "AGENTS.md").write_text(
                private_instruction, encoding="utf-8"
            )
            (old_cache / "cache-marker").write_text("cache", encoding="utf-8")
            (old_asr / "asr-marker").write_text("asr", encoding="utf-8")

            first = self.run_migration(root)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertNotIn(private_token, first.stdout + first.stderr)
            self.assertNotIn(private_instruction, first.stdout + first.stderr)
            self.assertNotIn("QWEN_ASR_MAX_AUDIO_SECONDS", first.stdout + first.stderr)
            self.assertNotIn("VOICE_ARM_SECONDS", first.stdout + first.stderr)

            new_secret = root / "etc/cerberus3-voice/gateway.env"
            new_asr_env = root / "etc/default/cerberus3-qwen3-asr"
            new_bridge_env = root / "etc/default/cerberus3-voice-bridge"
            new_state = home / ".local/state/cerberus-voice/openclaw"
            new_workspace = home / ".local/share/cerberus-voice/workspace"
            self.assertEqual(new_secret.read_text(encoding="ascii"), old_secret.read_text(encoding="ascii"))
            self.assertEqual(stat.S_IMODE(new_secret.stat().st_mode), 0o600)
            self.assertEqual(
                new_asr_env.read_text(encoding="ascii"),
                "QWEN_ASR_MAX_AUDIO_SECONDS=29\n",
            )
            self.assertEqual(
                new_bridge_env.read_text(encoding="ascii"),
                "VOICE_ARM_SECONDS=31\n",
            )
            self.assertEqual(stat.S_IMODE(new_asr_env.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(new_bridge_env.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(new_asr_env.parent.stat().st_mode), 0o755)
            config = json.loads((new_state / "openclaw.json").read_text())
            self.assertEqual(
                config["models"]["providers"]["vllm"]["baseUrl"],
                "http://cerberus1.local:8889/v1",
            )
            self.assertEqual(
                config["agents"]["defaults"]["workspace"], str(new_workspace)
            )
            self.assertEqual(config["agents"]["custom"], "preserved")
            canonical_sessions = new_state / "agents/voice/sessions"
            session_index = json.loads(
                (canonical_sessions / "sessions.json").read_text(encoding="utf-8")
            )
            canonical_session = new_state / "agents/voice/sessions/session.jsonl"
            self.assertEqual(
                session_index["agent:voice:main"]["sessionFile"],
                str(canonical_session),
            )
            self.assertEqual(
                session_index["agent:voice:backup"]["sessionFile"],
                str(near_prefix_session),
            )
            trajectory_path = json.loads(
                (canonical_sessions / "turn.trajectory-path.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(trajectory_path["runtimeFile"], str(canonical_session))
            trajectory_file = canonical_sessions / "turn.trajectory.jsonl"
            trajectory_lines = trajectory_file.read_text(encoding="utf-8").splitlines()
            trajectory = json.loads(trajectory_lines[0])
            self.assertEqual(trajectory["data"]["sessionFile"], str(canonical_session))
            self.assertEqual(
                trajectory["data"]["privateText"],
                "do not rewrite cerebrus in content",
            )
            self.assertEqual(
                trajectory_lines[1],
                '{  "type" : "content", "data" : "preserve spacing"  }',
            )
            self.assertEqual(
                (new_workspace / "AGENTS.md").read_text(encoding="utf-8"),
                "Private custom rule for cerberus3.",
            )
            self.assertEqual(
                json.loads((old_state / "openclaw.json").read_text()), old_config
            )

            session_index_inode = (canonical_sessions / "sessions.json").stat().st_ino
            trajectory_inode = trajectory_file.stat().st_ino
            second = self.run_migration(root)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertNotIn(private_token, second.stdout + second.stderr)
            self.assertEqual(
                json.loads((new_state / "openclaw.json").read_text()), config
            )
            self.assertEqual(
                (canonical_sessions / "sessions.json").stat().st_ino,
                session_index_inode,
            )
            self.assertEqual(trajectory_file.stat().st_ino, trajectory_inode)

    def test_symlinked_legacy_secret_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "token-source"
            source.write_text("private", encoding="ascii")
            source.chmod(0o600)
            legacy = root / "etc/cerebrus3-voice/gateway.env"
            legacy.parent.mkdir(parents=True)
            legacy.symlink_to(source)
            result = self.run_migration(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("expected a regular file", result.stderr)


if __name__ == "__main__":
    unittest.main()
