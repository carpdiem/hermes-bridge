import subprocess
import tempfile
import unittest
from pathlib import Path

from hermes_bridge.config import AgentConfig
from hermes_bridge.errors import BridgeError
from hermes_bridge.remote import CommandResult, Remote
from hermes_bridge.tmux import (
    attach,
    create_session,
    _style_options,
    list_sessions,
    sanitize_session_piece,
    selector_to_target,
    session_name_for,
)


def agent(socket_path="", style=None):
    tmux = {"enabled": True, "prefix": "ops"}
    if socket_path:
        tmux["socket_path"] = socket_path
    if style is not None:
        tmux["style"] = style
    return AgentConfig(
        key="ops",
        raw={
            "command": "ops",
            "display_name": "Ops",
            "ssh_alias": "ops-host",
            "remote_hermes_cmd": "/Users/ops/.local/bin/hermes",
            "tmux": tmux,
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


class LocalRemote(Remote):
    def run(self, command, *, tty=False, check=False, capture=True):
        proc = subprocess.run(["bash", "-lc", command], text=True, capture_output=True)
        result = CommandResult(proc.returncode, proc.stdout, proc.stderr)
        if check and result.returncode != 0:
            raise BridgeError(result.stderr or result.stdout or "failed")
        return result


class TmuxTests(unittest.TestCase):
    def test_sanitize(self):
        self.assertEqual(sanitize_session_piece("Support Workflow!"), "support-workflow")
        self.assertEqual(sanitize_session_piece("ops.foo"), "ops.foo")

    def test_session_name_for(self):
        self.assertEqual(session_name_for("ops", "support"), "ops-support")
        self.assertEqual(session_name_for("ops", "ops-support"), "ops-support")

    def test_list_sessions_parses_prefixed_sessions(self):
        remote = FakeRemote([CommandResult(0, "ops\t0\t100\nops-plan\t1\t101\nother\t0\t102\n", "")])
        sessions = list_sessions(agent(), remote)
        self.assertEqual([s.name for s in sessions], ["ops", "ops-plan"])

    def test_list_sessions_format_uses_real_tabs(self):
        remote = FakeRemote([CommandResult(0, "ops\t0\t100\n", "")])
        list_sessions(agent(), remote)
        command = remote.calls[0]["command"]
        self.assertIn("#{session_name}\t#{session_attached}\t#{session_created}", command)
        self.assertNotIn(r"#{session_name}\t#{session_attached}\t#{session_created}", command)

    def test_list_sessions_surfaces_ssh_failures(self):
        remote = FakeRemote([CommandResult(255, "", "ssh: Could not resolve hostname ops-host")])
        with self.assertRaisesRegex(BridgeError, "could not list remote tmux sessions.*ops-host"):
            list_sessions(agent(), remote)

    def test_list_sessions_normalizes_macos_missing_socket_as_no_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_tmux = Path(tmp) / "tmux"
            fake_tmux.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' 'error connecting to /private/tmp/tmux-503/default (No such file or directory)' >&2\n"
                "exit 1\n"
            )
            fake_tmux.chmod(0o755)
            configured = agent()
            configured = AgentConfig(
                key=configured.key,
                raw=configured.raw,
                defaults={**configured.defaults, "remote_tmux_cmd": str(fake_tmux)},
            )
            self.assertEqual(list_sessions(configured, LocalRemote(configured)), [])

    def test_configured_socket_path_is_used(self):
        remote = FakeRemote([CommandResult(0, "ops\t0\t100\n", "")])
        sessions = list_sessions(agent("/private/tmp/tmux-503/default"), remote)
        self.assertEqual([s.name for s in sessions], ["ops"])
        self.assertIn("-S /private/tmp/tmux-503/default list-sessions", remote.calls[0]["command"])

    def test_named_attach_does_not_list_first(self):
        remote = FakeRemote([CommandResult(0, "", "")])
        self.assertEqual(attach(agent(), "plan", remote), 0)
        self.assertEqual(len(remote.calls), 1)
        self.assertIn("attach-session -t ops-plan", remote.calls[0]["command"])
        self.assertTrue(remote.calls[0]["tty"])

    def test_numeric_selector_uses_list(self):
        remote = FakeRemote([CommandResult(0, "ops\t0\t100\nops-plan\t0\t101\n", "")])
        self.assertEqual(selector_to_target(agent(), "2", remote), "ops-plan")
        self.assertIn("list-sessions", remote.calls[0]["command"])

    def test_create_session_verifies_it_stayed_alive(self):
        remote = FakeRemote([CommandResult(0, "ops\n", "")])
        self.assertEqual(create_session(agent(), "ops", "/Users/ops/.local/bin/hermes --tui", remote), "ops")
        command = remote.calls[0]["command"]
        self.assertIn("new-session -d", command)
        self.assertIn("has-session -t \"$name\"", command)
        self.assertIn("tmux session exited immediately", command)

    def test_default_style_options_preserve_current_palette(self):
        options = _style_options(agent())
        self.assertEqual(options["status-position"], "bottom")
        self.assertEqual(options["status-style"], "bg=#3c3836,fg=#ebdbb2")
        self.assertEqual(options["status-left-style"], "bg=#3c3836,fg=#fabd2f")
        self.assertEqual(options["status-right-style"], "bg=#3c3836,fg=#fabd2f")
        self.assertEqual(options["window-status-current-style"], "bg=#3c3836,fg=#fabd2f")
        self.assertEqual(options["message-style"], "bg=#d79921,fg=#282828,bold")
        self.assertEqual(options["pane-active-border-style"], "fg=#d79921")
        self.assertEqual(options["status-left"], " Ops | #S ")
        self.assertEqual(options["status-right"], " %H:%M ")

    def test_agent_style_overrides_subset_and_accepts_snake_case(self):
        options = _style_options(agent(style={"status-style": "bg=#111111,fg=#eeeeee", "status_right": " #S "}))
        self.assertEqual(options["status-style"], "bg=#111111,fg=#eeeeee")
        self.assertEqual(options["status-right"], " #S ")
        self.assertEqual(options["status-left"], " Ops | #S ")

    def test_create_session_applies_agent_style_overrides(self):
        remote = FakeRemote([CommandResult(0, "ops\n", "")])
        create_session(agent(style={"message-style": "bg=#111111,fg=#eeeeee,bold"}), "ops", "/Users/ops/.local/bin/hermes --tui", remote)
        command = remote.calls[0]["command"]
        self.assertIn("message-style", command)
        self.assertIn("bg=#111111,fg=#eeeeee,bold", command)


if __name__ == "__main__":
    unittest.main()
