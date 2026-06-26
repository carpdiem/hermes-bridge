from __future__ import annotations

import datetime as _dt
import os
import re
import shlex
from pathlib import Path
from typing import Optional

from .config import AgentConfig, BridgeConfig
from .errors import BridgeError
from .remote import Remote
from .templates import render_template_file
from .tmux import create_session


def _safe_filename(name: str) -> str:
    name = os.path.basename(name).strip() or "upload"
    name = re.sub(r"[\x00-\x1f/]+", "_", name)
    name = re.sub(r"\s+", " ", name)
    return name


def remote_upload_path(remote_inbox: str, local_path: Path) -> str:
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{remote_inbox.rstrip('/')}/{stamp}--{_safe_filename(local_path.name)}"


def build_prompt(config: BridgeConfig, agent: AgentConfig, kind: str, local_path: Path, remote_path: str, message: str, mode: str) -> str:
    block = agent.upload_block(kind)
    template_name = block.get("prompt_template")
    if not template_name:
        raise BridgeError(f"{agent.command}: upload.{kind}.prompt_template is required")
    values = {
        "agent_key": agent.key,
        "agent_command": agent.command,
        "agent_display_name": agent.display_name,
        "docs_prefix": agent.docs_prefix,
        "drive_root": agent.drive_root,
        "upload_kind": kind,
        "filename": Path(remote_path).name,
        "local_path": str(local_path),
        "remote_path": remote_path,
        "user_message": message.strip() or "Acknowledge the upload and suggest the highest-leverage next actions. Do not fully process it yet unless the filename makes the request obvious.",
        "mode": mode,
    }
    return render_template_file(config.templates_dir / str(template_name), values)


def upload(config: BridgeConfig, agent: AgentConfig, kind: str, src: str, message: str, *, foreground: bool = False, attach: bool = False, task_name: Optional[str] = None) -> str:
    block = agent.upload_block(kind)
    local_path = Path(src).expanduser()
    if not local_path.exists():
        raise BridgeError(f"path does not exist: {local_path}")
    if not local_path.is_file():
        raise BridgeError(f"path is not a regular file: {local_path}")
    remote_inbox = str(block.get("remote_inbox") or "").format(remote_user=agent.remote_user, remote_home=agent.remote_home)
    if not remote_inbox:
        raise BridgeError(f"{agent.command}: upload.{kind}.remote_inbox is required")
    remote_path = remote_upload_path(remote_inbox, local_path)
    remote = Remote(agent)
    remote.stream_stdin_to_remote_file(local_path.read_bytes(), remote_path)
    if not message.strip():
        return f"Uploaded {local_path} to {agent.ssh_alias}:{remote_path}"

    mode = "foreground" if foreground else "fire-and-forget"
    prompt = build_prompt(config, agent, kind, local_path.resolve(), remote_path, message, mode)
    hermes_cmd = shlex.join([agent.remote_hermes_cmd, "chat", "-q", prompt])
    if foreground:
        code = remote.run(hermes_cmd, tty=True).returncode
        if code != 0:
            raise BridgeError(f"remote Hermes foreground task exited with {code}")
        return f"Uploaded and ran foreground task for {remote_path}"

    wrapped = f"{hermes_cmd}; status=$?; printf '\\n[{agent.command} upload task exited with status %s]\\n' \"$status\"; printf 'Session preserved for review. Press Enter to close.\\n'; read _; exit \"$status\""
    base = f"{agent.tmux_prefix()}-{task_name}" if task_name else f"{agent.tmux_prefix()}-upload-{kind}"
    session = create_session(agent, base, shlex.join(["/bin/zsh", "-lc", wrapped]), remote=remote)
    if attach:
        remote.run(f"{shlex.quote(agent.remote_tmux_cmd())} attach-session -t {shlex.quote(session)}", tty=True)
    return f"Uploaded {local_path} to {agent.ssh_alias}:{remote_path}\nStarted durable remote tmux task: {session}"
