import tempfile
import unittest
from pathlib import Path

from hermes_bridge.config import load_config
from hermes_bridge.cli import selected_agent

CONFIG = """
agents:
  ops:
    command: ops
    ssh_alias: ops-host
    remote_home: /home/hermes-ops
    remote_hermes_cmd: /home/hermes-ops/.local/bin/hermes
  admin_console:
    command: admin-console
    ssh_alias: admin-console
    remote_home: /home/admin-console
    remote_hermes_cmd: /home/admin-console/.local/bin/hermes
"""

class DispatchTests(unittest.TestCase):
    def test_symlink_invocation_selects_agent(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.yaml"
            p.write_text(CONFIG)
            cfg = load_config(str(p))
            agent, rest = selected_agent(cfg, ["tmux", "list"], "/x/ops")
            self.assertEqual(agent.key, "ops")
            self.assertEqual(rest, ["tmux", "list"])

    def test_explicit_agent_selects_agent(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.yaml"
            p.write_text(CONFIG)
            cfg = load_config(str(p))
            agent, rest = selected_agent(cfg, ["admin-console", "doctor"], "/x/hermes-bridge")
            self.assertEqual(agent.key, "admin_console")
            self.assertEqual(rest, ["doctor"])

if __name__ == "__main__":
    unittest.main()
