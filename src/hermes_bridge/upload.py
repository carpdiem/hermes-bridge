from __future__ import annotations

import datetime as _dt
import glob
import os
import re
import shlex
import tempfile
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


def expand_upload_sources(srcs: list[str]) -> list[Path]:
    expanded: list[Path] = []
    for src in srcs:
        raw = os.path.expanduser(src)
        matches = sorted(glob.glob(raw)) if glob.has_magic(raw) else []
        if matches:
            expanded.extend(Path(match) for match in matches)
        else:
            expanded.append(Path(raw))

    deduped: list[Path] = []
    seen: set[str] = set()
    for path in expanded:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return deduped


def _unique_remote_upload_path(remote_inbox: str, local_path: Path, used: set[str]) -> str:
    base = remote_upload_path(remote_inbox, local_path)
    if base not in used:
        used.add(base)
        return base

    base_path = Path(base)
    stem = base_path.stem
    suffix = base_path.suffix
    parent = str(base_path.parent)
    i = 2
    while True:
        candidate = f"{parent}/{stem}-{i}{suffix}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        i += 1


def build_prompt(config: BridgeConfig, agent: AgentConfig, kind: str, local_path: Path, remote_path: str, message: str, mode: str) -> str:
    block = agent.upload_block("book" if kind == "batch-book" else "file" if kind == "batch-file" else kind)
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


def upload_many(config: BridgeConfig, agent: AgentConfig, kind: str, srcs: list[str], message: str, *, foreground: bool = False, attach: bool = False, task_name: Optional[str] = None) -> str:
    if not srcs:
        raise BridgeError(f"{agent.command}: upload requires at least one path")

    paths = expand_upload_sources(srcs)
    if len(paths) == 1:
        return upload(config, agent, kind, str(paths[0]), message, foreground=foreground, attach=attach, task_name=task_name)

    block = agent.upload_block(kind)
    remote_inbox = str(block.get("remote_inbox") or "").format(remote_user=agent.remote_user, remote_home=agent.remote_home)
    if not remote_inbox:
        raise BridgeError(f"{agent.command}: upload.{kind}.remote_inbox is required")

    for path in paths:
        if not path.exists():
            raise BridgeError(f"path does not exist: {path}")
        if not path.is_file():
            raise BridgeError(f"path is not a regular file: {path}")

    remote = Remote(agent)
    used_remote_paths: set[str] = set()
    uploaded: list[tuple[Path, str]] = []
    for path in paths:
        remote_path = _unique_remote_upload_path(remote_inbox, path, used_remote_paths)
        remote.stream_stdin_to_remote_file(path.read_bytes(), remote_path)
        uploaded.append((path, remote_path))

    now = _dt.datetime.now().isoformat(timespec="seconds")
    manifest_lines = [
        f"# {agent.display_name} batch upload manifest",
        "",
        f"Created: {now}",
        f"Upload kind: {kind}",
        f"File count: {len(uploaded)}",
        "",
        "## Files",
        "",
    ]
    for i, (local_path, remote_path) in enumerate(uploaded, 1):
        manifest_lines.extend([
            f"### {i}. {local_path.name}",
            f"- Original local path: `{local_path.resolve()}`",
            f"- Remote path: `{remote_path}`",
            "",
        ])
    manifest_content = "\n".join(manifest_lines).rstrip() + "\n"

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", prefix="hermes-bridge-upload-manifest-", suffix=".md", delete=False) as handle:
        handle.write(manifest_content)
        manifest_local = Path(handle.name)
    try:
        manifest_remote = _unique_remote_upload_path(remote_inbox, Path("batch-upload-manifest.md"), used_remote_paths)
        remote.stream_stdin_to_remote_file(manifest_content.encode("utf-8"), manifest_remote)
        default_message = "Acknowledge this batch upload manifest and suggest the highest-leverage next actions. Do not fully process the files yet unless the filenames make the request obvious."
        prompt = build_prompt(config, agent, f"batch-{kind}", manifest_local, manifest_remote, message.strip() or default_message, "foreground" if foreground else "fire-and-forget")
    finally:
        try:
            manifest_local.unlink()
        except OSError:
            pass

    hermes_cmd = shlex.join([agent.remote_hermes_cmd, "chat", "-q", prompt])
    if foreground:
        code = remote.run(hermes_cmd, tty=True).returncode
        if code != 0:
            raise BridgeError(f"remote Hermes foreground task exited with {code}")
        return f"Uploaded {len(uploaded)} files and ran foreground batch task for {manifest_remote}"

    wrapped = f"{hermes_cmd}; status=$?; printf '\\n[{agent.command} batch upload task exited with status %s]\\n' \"$status\"; printf 'Session preserved for review. Press Enter to close.\\n'; read _; exit \"$status\""
    base = f"{agent.tmux_prefix()}-{task_name}" if task_name else f"{agent.tmux_prefix()}-upload-{kind}-batch"
    session = create_session(agent, base, shlex.join(["/bin/zsh", "-lc", wrapped]), remote=remote)
    if attach:
        remote.run(f"{shlex.quote(agent.remote_tmux_cmd())} attach-session -t {shlex.quote(session)}", tty=True)
    files = "\n".join(f"  - {local} -> {remote_path}" for local, remote_path in uploaded)
    return f"Uploaded {len(uploaded)} files to {agent.ssh_alias}:{remote_inbox}\n{files}\nManifest: {manifest_remote}\nStarted durable remote tmux task: {session}"
