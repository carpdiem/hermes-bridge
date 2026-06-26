import unittest

from hermes_bridge.config import AgentConfig
from hermes_bridge.remote import Remote


def agent():
    return AgentConfig(
        key="ops",
        raw={
            "command": "ops",
            "ssh_alias": "ops-host",
            "remote_hermes_cmd": "/Users/ops/.local/bin/hermes",
            "tmux": {"enabled": True, "prefix": "ops"},
        },
        defaults={"remote_tmux_cmd": "tmux", "remote_shell": "zsh"},
    )


class RemoteTests(unittest.TestCase):
    def test_noninteractive_uses_no_tty(self):
        remote = Remote(agent())
        args = remote.ssh_args("echo ok", tty=False)
        self.assertIn("-T", args)
        self.assertNotIn("-tt", args)

    def test_interactive_uses_forced_tty(self):
        remote = Remote(agent())
        args = remote.ssh_args("tmux attach-session -t ops", tty=True)
        self.assertIn("-tt", args)
        self.assertNotIn("-t", args)


if __name__ == "__main__":
    unittest.main()
