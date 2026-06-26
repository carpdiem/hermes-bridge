from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import AgentConfig
from .errors import BridgeError


@dataclass
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class Remote:
    def __init__(self, agent: AgentConfig):
        self.agent = agent
        self.ssh_bin = shutil.which("ssh") or "/usr/bin/ssh"

    def prelude(self) -> str:
        path = self.agent.remote_path
        term = self.agent.remote_term
        return f"set -e; export PATH={shlex.quote(path)}:$PATH; export TERM={shlex.quote(term)}; cd ~"

    def shell_command(self, command: str) -> str:
        full = f"{self.prelude()}; {command}"
        return f"{shlex.quote(self.agent.remote_shell)} -lc {shlex.quote(full)}"

    def ssh_args(self, command: str, tty: bool = False) -> list[str]:
        # Use -tt for interactive tmux attach/resume paths. A single -t can be
        # skipped by OpenSSH when the Python wrapper's stdin is not recognized as
        # a terminal; tmux then sees no tty and fails with
        # "open terminal failed: not a terminal".
        return [self.ssh_bin, "-tt" if tty else "-T", self.agent.ssh_alias, self.shell_command(command)]

    def run(self, command: str, *, tty: bool = False, check: bool = False, capture: bool = True) -> CommandResult:
        args = self.ssh_args(command, tty=tty)
        if tty:
            try:
                with open("/dev/tty", "rb", buffering=0) as tty_in, open("/dev/tty", "wb", buffering=0) as tty_out:
                    code = subprocess.call(args, stdin=tty_in, stdout=tty_out, stderr=tty_out)
            except OSError:
                if not os.isatty(0):
                    raise BridgeError("interactive attach requires a local terminal; run this command from a real terminal, not a pipe/background job")
                code = subprocess.call(args)
            if check and code != 0:
                raise BridgeError(f"remote command failed with exit code {code}: {command}")
            return CommandResult(returncode=code)
        proc = subprocess.run(args, text=True, capture_output=capture)
        if check and proc.returncode != 0:
            msg = proc.stderr.strip() or proc.stdout.strip() or command
            raise BridgeError(f"remote command failed ({proc.returncode}): {msg}")
        return CommandResult(proc.returncode, proc.stdout or "", proc.stderr or "")

    def stream_stdin_to_remote_file(self, local_bytes: bytes, remote_path: str) -> None:
        command = f"mkdir -p {shlex.quote(str(Path(remote_path).parent))}; cat > {shlex.quote(remote_path)}"
        args = self.ssh_args(command, tty=False)
        proc = subprocess.run(args, input=local_bytes, capture_output=True)
        if proc.returncode != 0:
            stderr = proc.stderr.decode(errors="replace") if proc.stderr else ""
            raise BridgeError(f"upload failed ({proc.returncode}): {stderr.strip()}")
