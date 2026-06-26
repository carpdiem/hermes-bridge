import unittest

from hermes_bridge.config import AgentConfig
from hermes_bridge.errors import BridgeError
from hermes_bridge.remote import CommandResult
from hermes_bridge.tmux import (
    attach,
    create_session,
    list_sessions,
    sanitize_session_piece,
    selector_to_target,
    session_name_for,
)


def agent(socket_path=""):
    tmux = {"enabled": True, "prefix": "ops"}
    if socket_path:
        tmux["socket_path"] = socket_path
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


if __name__ == "__main__":
    unittest.main()
