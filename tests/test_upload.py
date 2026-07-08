import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_bridge.config import AgentConfig, BridgeConfig
from hermes_bridge.remote import CommandResult
from hermes_bridge.upload import expand_upload_sources, upload_many


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
            "upload": {
                "file": {
                    "enabled": True,
                    "remote_inbox": "/home/hermes-ops/Inbox/_Inbox",
                    "prompt_template": "ops-upload-file.md",
                }
            },
        },
        defaults={"remote_tmux_cmd": "tmux", "tmux_geometry": "120x40"},
    )


class FakeRemote:
    instances = []

    def __init__(self, _agent):
        self.agent = _agent
        self.uploads = []
        self.runs = []
        FakeRemote.instances.append(self)

    def stream_stdin_to_remote_file(self, local_bytes, remote_path):
        self.uploads.append((remote_path, local_bytes))

    def run(self, command, *, tty=False, check=False, capture=True):
        self.runs.append({"command": command, "tty": tty, "check": check, "capture": capture})
        return CommandResult(0, "", "")


class UploadTests(unittest.TestCase):
    def setUp(self):
        FakeRemote.instances = []

    def test_expand_upload_sources_expands_sorted_globs_and_dedupes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            b = root / "2026-07-03-b.jpg"
            a = root / "2026-07-03-a.jpg"
            a.write_text("a")
            b.write_text("b")
            paths = expand_upload_sources([str(root / "2026-07-03-*.jpg"), str(a)])
            self.assertEqual(paths, [a, b])

    def test_upload_many_uploads_files_and_manifest_as_single_task(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            templates = root / "templates"
            templates.mkdir()
            (templates / "ops-upload-file.md").write_text(
                "kind={{ upload_kind }}\nremote={{ remote_path }}\nlocal={{ local_path }}\nmessage={{ user_message }}\n"
            )
            config = BridgeConfig(path=root / "config.yaml", raw={"templates_dir": str(templates)})
            first = root / "one.jpg"
            second = root / "two.jpg"
            first.write_bytes(b"one")
            second.write_bytes(b"two")

            with patch("hermes_bridge.upload.Remote", FakeRemote), patch("hermes_bridge.upload.create_session", return_value="ops-upload-file-batch") as fake_create:
                out = upload_many(config, agent(), "file", [str(root / "*.jpg")], "review these", task_name="photos")

            remote = FakeRemote.instances[0]
            uploaded_paths = [item[0] for item in remote.uploads]
            self.assertEqual(len(remote.uploads), 3)
            self.assertTrue(any(path.endswith("--one.jpg") for path in uploaded_paths))
            self.assertTrue(any(path.endswith("--two.jpg") for path in uploaded_paths))
            manifest = [data for path, data in remote.uploads if path.endswith("--batch-upload-manifest.md")][0].decode()
            self.assertIn("File count: 2", manifest)
            self.assertIn("one.jpg", manifest)
            self.assertIn("two.jpg", manifest)
            self.assertIn("review these", fake_create.call_args.args[2])
            self.assertIn("batch-file", fake_create.call_args.args[2])
            self.assertIn("ops-photos", fake_create.call_args.args[1])
            self.assertIn("Uploaded 2 files", out)
            self.assertIn("Manifest:", out)


if __name__ == "__main__":
    unittest.main()
