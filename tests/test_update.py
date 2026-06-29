import unittest

from hermes_bridge.config import AgentConfig, load_config
from hermes_bridge.errors import BridgeError
from hermes_bridge.remote import CommandResult
from hermes_bridge.update import (
    _stop_command,
    build_update_plan,
    execute_agent_update,
    fleet_update,
    inventory_agent,
    parse_update_options,
)


def agent():
    return AgentConfig(
        key="ops",
        raw={
            "command": "ops",
            "display_name": "Ops",
            "ssh_alias": "ops-host",
            "remote_home": "/home/hermes-ops",
            "remote_hermes_cmd": "/home/hermes-ops/.local/bin/hermes",
            "tmux": {"enabled": True, "prefix": "ops"},
        },
        defaults={"remote_tmux_cmd": "tmux", "tmux_geometry": "120x40"},
    )


class FakeRemote:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def run(self, command, *, tty=False, check=False, capture=True):
        self.calls.append({"command": command, "tty": tty, "check": check, "capture": capture})
        response = self.responses.pop(0)
        if check and response.returncode != 0:
            raise BridgeError(response.stderr or response.stdout or "failed")
        return response


def inventory_json(*items):
    import json

    return json.dumps(list(items)) + "\n"


class UpdateTests(unittest.TestCase):
    def test_inventory_parses_live_session_ids(self):
        remote = FakeRemote([
            CommandResult(0, inventory_json({
                "name": "ops-plan",
                "attached": False,
                "created": "100",
                "pane_pids": [12],
                "tui_detected": True,
                "active_session_files": ["/tmp/active.json"],
                "session_ids": ["20260627_abc"],
                "active_file_errors": [],
            }), "")
        ])
        sessions = inventory_agent(agent(), remote)
        self.assertEqual(sessions[0].name, "ops-plan")
        self.assertEqual(sessions[0].resume_session_id, "20260627_abc")
        self.assertIn("HERMES_TUI_ACTIVE_SESSION_FILE", remote.calls[0]["command"])

    def test_plan_blocks_attached_sessions(self):
        remote = FakeRemote([
            CommandResult(0, inventory_json({
                "name": "ops-plan",
                "attached": True,
                "created": "100",
                "pane_pids": [12],
                "tui_detected": True,
                "active_session_files": ["/tmp/active.json"],
                "session_ids": ["20260627_abc"],
                "active_file_errors": [],
            }), "")
        ])
        plan = build_update_plan(agent(), remote)
        self.assertFalse(plan.safe_to_apply)
        self.assertIn("attached tmux session", plan.blockers[0])

    def test_dry_run_does_not_stop_or_update(self):
        opts, _ = parse_update_options(["--dry-run"])
        remote = FakeRemote([
            CommandResult(0, inventory_json({
                "name": "ops-plan",
                "attached": False,
                "created": "100",
                "pane_pids": [12],
                "tui_detected": True,
                "active_session_files": ["/tmp/active.json"],
                "session_ids": ["20260627_abc"],
                "active_file_errors": [],
            }), "")
        ])
        out = execute_agent_update(agent(), opts, remote)
        self.assertIn("Dry run only", out)
        self.assertEqual(len(remote.calls), 1)

    def test_yes_stops_updates_and_rehydrates_with_resume_id(self):
        opts, _ = parse_update_options(["--yes", "--no-backup"])
        remote = FakeRemote([
            CommandResult(0, inventory_json({
                "name": "ops-plan",
                "attached": False,
                "created": "100",
                "pane_pids": [12],
                "tui_detected": True,
                "active_session_files": ["/tmp/active.json"],
                "session_ids": ["20260627_abc"],
                "active_file_errors": [],
            }), ""),
            CommandResult(0, "", ""),
            CommandResult(0, "updated\n", ""),
            CommandResult(0, "ops-plan\n", ""),
        ])
        out = execute_agent_update(agent(), opts, remote)
        self.assertIn("Hermes update completed", out)
        self.assertIn("/exit", remote.calls[1]["command"])
        self.assertIn("update --no-backup --yes", remote.calls[2]["command"])
        self.assertIn("--resume 20260627_abc", remote.calls[3]["command"])
        self.assertIn("name=ops-plan", remote.calls[3]["command"])

    def test_stop_command_does_not_depend_on_shell_word_splitting(self):
        command = _stop_command(agent(), ["ops-one", "ops-two"], 45)
        self.assertNotIn("for name in $names", command)
        self.assertNotIn("names=", command)
        self.assertIn("send-keys -t ops-one /exit Enter", command)
        self.assertIn("send-keys -t ops-two /exit Enter", command)
        self.assertIn("has-session -t ops-one", command)
        self.assertIn("has-session -t ops-two", command)

    def test_check_runs_update_check_without_inventory(self):
        opts, _ = parse_update_options(["--check"])
        remote = FakeRemote([CommandResult(0, "already current\n", "")])
        out = execute_agent_update(agent(), opts, remote)
        self.assertIn("already current", out)
        self.assertEqual(len(remote.calls), 1)
        self.assertIn("update --check", remote.calls[0]["command"])

    def test_fleet_update_requires_explicit_selection(self):
        import tempfile
        from pathlib import Path

        config = """
agents:
  ops:
    command: ops
    ssh_alias: ops-host
    remote_home: /home/hermes-ops
    remote_hermes_cmd: /home/hermes-ops/.local/bin/hermes
    tmux:
      enabled: true
      prefix: ops
"""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.yaml"
            p.write_text(config)
            cfg = load_config(str(p))
            with self.assertRaisesRegex(BridgeError, "requires --all"):
                fleet_update(cfg, ["--dry-run"])


if __name__ == "__main__":
    unittest.main()
