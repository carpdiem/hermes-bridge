import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_bridge.config import AgentConfig
from hermes_bridge.errors import BridgeError
from hermes_bridge.lifecycle import (
    LifecycleOptions,
    _stop_command,
    dehydrate,
    inventory_agent,
    rehydrate,
    snapshot_path,
)
from hermes_bridge.remote import CommandResult


def agent() -> AgentConfig:
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
    return json.dumps(list(items)) + "\n"


def live_item(**overrides):
    item = {
        "name": "ops-plan",
        "attached": False,
        "created": "100",
        "pane_pids": [12],
        "tui_pids": [222],
        "tui_detected": True,
        "active_session_files": ["/tmp/active.json"],
        "session_ids": ["20260627_abc"],
        "active_file_errors": [],
    }
    item.update(overrides)
    return item


class LifecycleTests(unittest.TestCase):
    def test_inventory_parses_live_session_ids_without_dumping_env(self):
        remote = FakeRemote([CommandResult(0, inventory_json(live_item()), "")])
        sessions = inventory_agent(agent(), remote)
        self.assertEqual(sessions[0].name, "ops-plan")
        self.assertEqual(sessions[0].resume_session_id, "20260627_abc")
        self.assertIn("HERMES_TUI_ACTIVE_SESSION_FILE", remote.calls[0]["command"])
        self.assertNotIn("printenv", remote.calls[0]["command"])

    def test_dehydrate_dry_run_does_not_write_or_stop(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"HERMES_BRIDGE_SNAPSHOT_DIR": d}):
            remote = FakeRemote([CommandResult(0, inventory_json(live_item()), "")])
            out = dehydrate(agent(), LifecycleOptions(dry_run=True), remote)
            self.assertIn("Dry run only", out)
            self.assertFalse(snapshot_path(agent()).exists())
            self.assertEqual(len(remote.calls), 1)

    def test_dehydrate_writes_snapshot_then_stops_sessions(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"HERMES_BRIDGE_SNAPSHOT_DIR": d}):
            remote = FakeRemote([
                CommandResult(0, inventory_json(live_item()), ""),
                CommandResult(0, "", ""),
            ])
            out = dehydrate(agent(), LifecycleOptions(replace=True), remote)
            path = snapshot_path(agent())
            self.assertTrue(path.exists())
            data = json.loads(path.read_text())
            self.assertEqual(data["sessions"][0]["name"], "ops-plan")
            self.assertEqual(data["sessions"][0]["resume_session_id"], "20260627_abc")
            self.assertIn("kill -TERM 222", remote.calls[1]["command"])
            self.assertIn("Dehydrated: ops-plan", out)

    def test_dehydrate_refuses_to_overwrite_existing_snapshot(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"HERMES_BRIDGE_SNAPSHOT_DIR": d}):
            path = snapshot_path(agent())
            path.parent.mkdir(parents=True)
            path.write_text("{}")
            remote = FakeRemote([CommandResult(0, inventory_json(live_item()), "")])
            with self.assertRaisesRegex(BridgeError, "--replace"):
                dehydrate(agent(), LifecycleOptions(), remote)
            self.assertEqual(len(remote.calls), 1)

    def test_dehydrate_blocks_attached_sessions(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"HERMES_BRIDGE_SNAPSHOT_DIR": d}):
            remote = FakeRemote([CommandResult(0, inventory_json(live_item(attached=True)), "")])
            with self.assertRaisesRegex(BridgeError, "attached tmux session"):
                dehydrate(agent(), LifecycleOptions(replace=True), remote)

    def test_rehydrate_recreates_exact_names_from_snapshot(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"HERMES_BRIDGE_SNAPSHOT_DIR": d}):
            path = snapshot_path(agent())
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({
                "version": 1,
                "agent_command": "ops",
                "agent_display_name": "Ops",
                "created_at": 1,
                "warnings": [],
                "sessions": [{"name": "ops-plan", "resume_session_id": "20260627_abc"}],
            }))
            remote = FakeRemote([
                CommandResult(0, "", ""),
                CommandResult(0, "ops-plan\n", ""),
            ])
            out = rehydrate(agent(), LifecycleOptions(), remote)
            self.assertIn("--resume 20260627_abc", remote.calls[1]["command"])
            self.assertIn("name=ops-plan", remote.calls[1]["command"])
            self.assertIn("Rehydrated: ops-plan", out)

    def test_rehydrate_blocks_existing_live_session_conflicts(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"HERMES_BRIDGE_SNAPSHOT_DIR": d}):
            path = snapshot_path(agent())
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({
                "version": 1,
                "agent_command": "ops",
                "agent_display_name": "Ops",
                "created_at": 1,
                "warnings": [],
                "sessions": [{"name": "ops-plan", "resume_session_id": "20260627_abc"}],
            }))
            remote = FakeRemote([CommandResult(0, "ops-plan\t0\t100\n", "")])
            with self.assertRaisesRegex(BridgeError, "already exists"):
                rehydrate(agent(), LifecycleOptions(), remote)

    def test_stop_command_falls_back_without_shell_word_splitting(self):
        command = _stop_command(agent(), [inventory_agent(agent(), FakeRemote([
            CommandResult(0, inventory_json(live_item(name="ops-one", tui_pids=[])), "")
        ]))[0]], 45)
        self.assertNotIn("for name in $names", command)
        self.assertIn("send-keys -t ops-one C-c", command)
        self.assertIn("send-keys -t ops-one /quit Enter", command)


if __name__ == "__main__":
    unittest.main()
