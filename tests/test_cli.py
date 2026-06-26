import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from hermes_bridge.cli import main

CONFIG = """
agents:
  ops:
    command: ops
    ssh_alias: ops-host
    remote_home: /home/hermes-ops
    remote_hermes_cmd: /home/hermes-ops/.local/bin/hermes
"""

UPLOAD_CONFIG = """
agents:
  ops:
    command: ops
    ssh_alias: ops-host
    remote_home: /home/hermes-ops
    remote_hermes_cmd: /home/hermes-ops/.local/bin/hermes
    upload:
      file:
        enabled: true
        remote_inbox: /home/hermes-ops/Inbox/_Inbox
        prompt_template: ops-upload-file.md
      book:
        enabled: true
        remote_inbox: /home/hermes-ops/Books/_Inbox
        prompt_template: ops-upload-book.md
"""


class CliTests(unittest.TestCase):
    def test_missing_option_value_is_clean_error(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.yaml"
            p.write_text(CONFIG)
            err = io.StringIO()
            with redirect_stderr(err):
                code = main(["--config", str(p), "link", "--mode"], invoked_as="hermes-bridge")
            self.assertEqual(code, 2)
            self.assertIn("--mode requires a value", err.getvalue())

    def test_doctor_unknown_agent_is_error(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.yaml"
            p.write_text(CONFIG)
            err = io.StringIO()
            out = io.StringIO()
            with redirect_stderr(err), redirect_stdout(out):
                code = main(["--config", str(p), "doctor", "missing"], invoked_as="hermes-bridge")
            self.assertEqual(code, 2)
            self.assertIn("unknown agent: missing", err.getvalue())

    def test_agent_symlink_version(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.yaml"
            p.write_text(CONFIG)
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--config", str(p), "--version"], invoked_as="ops")
            self.assertEqual(code, 0)
            self.assertIn("hermes-bridge", out.getvalue())

    def test_doctor_outputs_bridge_version(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.yaml"
            p.write_text(CONFIG)
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--config", str(p), "doctor", "ops", "--no-remote"], invoked_as="hermes-bridge")
            self.assertEqual(code, 0)
            self.assertIn("hermes_bridge_version:", out.getvalue())

    def test_agent_doctor_outputs_bridge_version(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.yaml"
            p.write_text(CONFIG)
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--config", str(p), "doctor", "--no-remote"], invoked_as="ops")
            self.assertEqual(code, 0)
            self.assertIn("hermes_bridge_version:", out.getvalue())

    def test_upload_defaults_to_file_kind(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.yaml"
            p.write_text(UPLOAD_CONFIG)
            out = io.StringIO()
            with patch("hermes_bridge.cli.upload", return_value="uploaded") as fake_upload, redirect_stdout(out):
                code = main(["--config", str(p), "upload", "/tmp/example.txt", "--", "summarize this"], invoked_as="ops")
            self.assertEqual(code, 0)
            _, agent, kind, src, message = fake_upload.call_args.args[:5]
            self.assertEqual(agent.command, "ops")
            self.assertEqual(kind, "file")
            self.assertEqual(src, "/tmp/example.txt")
            self.assertEqual(message, "summarize this")
            self.assertIn("uploaded", out.getvalue())

    def test_legacy_upload_file_form_still_works(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.yaml"
            p.write_text(UPLOAD_CONFIG)
            with patch("hermes_bridge.cli.upload", return_value="uploaded") as fake_upload, redirect_stdout(io.StringIO()):
                code = main(["--config", str(p), "upload", "file", "/tmp/example.txt"], invoked_as="ops")
            self.assertEqual(code, 0)
            self.assertEqual(fake_upload.call_args.args[2:4], ("file", "/tmp/example.txt"))

    def test_upload_book_alias_maps_to_book_kind(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.yaml"
            p.write_text(UPLOAD_CONFIG)
            with patch("hermes_bridge.cli.upload", return_value="uploaded") as fake_upload, redirect_stdout(io.StringIO()):
                code = main(["--config", str(p), "upload-book", "/tmp/book.epub", "extract ideas"], invoked_as="ops")
            self.assertEqual(code, 0)
            self.assertEqual(fake_upload.call_args.args[2:5], ("book", "/tmp/book.epub", "extract ideas"))

    def test_legacy_upload_book_form_still_works(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.yaml"
            p.write_text(UPLOAD_CONFIG)
            with patch("hermes_bridge.cli.upload", return_value="uploaded") as fake_upload, redirect_stdout(io.StringIO()):
                code = main(["--config", str(p), "upload", "book", "/tmp/book.epub"], invoked_as="ops")
            self.assertEqual(code, 0)
            self.assertEqual(fake_upload.call_args.args[2:4], ("book", "/tmp/book.epub"))


if __name__ == "__main__":
    unittest.main()
