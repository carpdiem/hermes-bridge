import tempfile
import unittest
from pathlib import Path

from hermes_bridge.config import load_config
from hermes_bridge.linking import link_agent, link_core, unlink_agent

CONFIG = """
agents:
  ops:
    command: ops-test-command
    ssh_alias: ops-host
    remote_home: /home/hermes-ops
    remote_hermes_cmd: /home/hermes-ops/.local/bin/hermes
"""

class LinkingTests(unittest.TestCase):
    def test_symlink_link_and_unlink(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.yaml"
            p.write_text(CONFIG)
            target = Path(d) / "hermes-bridge"
            target.write_text("#!/bin/sh\nexit 0\n")
            target.chmod(0o755)
            cfg = load_config(str(p))
            agent = cfg.agent("ops")
            bindir = Path(d) / "bin"
            msg = link_agent(agent, bindir, target)
            self.assertIn("linked", msg)
            self.assertTrue((bindir / "ops-test-command").is_symlink())
            msg = unlink_agent(agent, bindir)
            self.assertIn("removed", msg)

    def test_wrapper_mode(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.yaml"
            p.write_text(CONFIG)
            target = Path(d) / "hermes-bridge"
            target.write_text("#!/bin/sh\nexit 0\n")
            target.chmod(0o755)
            cfg = load_config(str(p))
            agent = cfg.agent("ops")
            bindir = Path(d) / "bin"
            link_agent(agent, bindir, target, mode="wrapper")
            text = (bindir / "ops-test-command").read_text()
            self.assertIn("ops", text)

    def test_link_core(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "hermes-bridge-real"
            target.write_text("#!/bin/sh\nexit 0\n")
            target.chmod(0o755)
            bindir = Path(d) / "bin"
            msg = link_core(bindir, target)
            self.assertIn("linked", msg)
            self.assertTrue((bindir / "hermes-bridge").is_symlink())

if __name__ == "__main__":
    unittest.main()
