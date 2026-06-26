import tempfile
import unittest
from pathlib import Path

from hermes_bridge.config import load_config

SAMPLE = """
defaults:
  remote_term: xterm-256color
  remote_tmux_cmd: tmux
  tmux_geometry: 120x40
  remote_path_prepend:
    - "{remote_home}/.local/bin"
    - "{remote_home}/bin"
    - /opt/homebrew/bin
    - /home/linuxbrew/.linuxbrew/bin
    - /usr/local/bin
agents:
  personal:
    command: personal
    display_name: Personal Agent
    ssh_alias: personal-host
    remote_user: hermes-example
    remote_home: /home/hermes-example
    remote_hermes_cmd: /home/hermes-example/.local/bin/hermes
    tmux:
      enabled: true
      prefix: personal
    sessions:
      enabled: true
    upload:
      file:
        enabled: true
        remote_inbox: /tmp/inbox
        prompt_template: upload.md
"""

class ConfigTests(unittest.TestCase):
    def test_load_and_validate(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.yaml"
            p.write_text(SAMPLE)
            template = Path(d) / "templates" / "upload.md"
            template.parent.mkdir()
            template.write_text("Uploaded {{ filename }}")
            cfg = load_config(str(p))
            self.assertIn("ok: personal -> personal", "\n".join(cfg.validate()))
            agent = cfg.agent("personal")
            self.assertEqual(agent.command, "personal")
            self.assertEqual(agent.tmux_prefix(), "personal")
            self.assertEqual(agent.remote_tmux_cmd(), "tmux")
            self.assertEqual(
                agent.remote_path,
                "/home/hermes-example/.local/bin:/home/hermes-example/bin:/opt/homebrew/bin:/home/linuxbrew/.linuxbrew/bin:/usr/local/bin",
            )
            self.assertTrue(agent.capability_enabled("upload", "file"))

    def test_legacy_remote_path_string_still_works(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.yaml"
            p.write_text(SAMPLE.replace("remote_path_prepend:\n    - \"{remote_home}/.local/bin\"\n    - \"{remote_home}/bin\"\n    - /opt/homebrew/bin\n    - /home/linuxbrew/.linuxbrew/bin\n    - /usr/local/bin", "remote_path: /custom/bin:/usr/bin"))
            template = Path(d) / "templates" / "upload.md"
            template.parent.mkdir()
            template.write_text("Uploaded {{ filename }}")
            cfg = load_config(str(p))
            self.assertEqual(cfg.agent("personal").remote_path, "/custom/bin:/usr/bin")

    def test_validate_rejects_missing_upload_template(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.yaml"
            p.write_text(SAMPLE)
            cfg = load_config(str(p))
            with self.assertRaises(Exception) as ctx:
                cfg.validate()
            self.assertIn("upload template not found", str(ctx.exception))

if __name__ == "__main__":
    unittest.main()
