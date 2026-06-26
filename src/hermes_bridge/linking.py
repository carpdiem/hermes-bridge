from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Iterable

from .config import AgentConfig, BridgeConfig
from .errors import BridgeError


def _same_path(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except FileNotFoundError:
        return False


def wrapper_text(target: Path, agent_key: str) -> str:
    target_s = str(target).replace("'", "'\\''")
    agent_s = agent_key.replace("'", "'\\''")
    return f"#!/usr/bin/env sh\nexec '{target_s}' '{agent_s}' \"$@\"\n"


def link_agent(agent: AgentConfig, bin_dir: Path, target: Path, *, mode: str = "symlink", force: bool = False) -> str:
    if mode not in {"symlink", "wrapper"}:
        raise BridgeError("link mode must be 'symlink' or 'wrapper'")
    bin_dir.mkdir(parents=True, exist_ok=True)
    dest = bin_dir / agent.command
    existing = shutil.which(agent.command)
    if existing and Path(existing) != dest and not force:
        raise BridgeError(f"refusing to shadow existing command {agent.command!r} at {existing}; use --force if intended")
    if dest.exists() or dest.is_symlink():
        if dest.is_symlink() and mode == "symlink" and _same_path(dest, target):
            return f"ok: {dest} -> {target}"
        if not force:
            raise BridgeError(f"refusing to replace existing path: {dest}; use --force")
        if dest.is_dir() and not dest.is_symlink():
            raise BridgeError(f"refusing to replace directory: {dest}")
        dest.unlink()
    if mode == "symlink":
        os.symlink(target, dest)
        return f"linked: {dest} -> {target}"
    dest.write_text(wrapper_text(target, agent.key))
    dest.chmod(0o755)
    return f"wrote wrapper: {dest} -> {target} {agent.key}"


def link_core(bin_dir: Path, target: Path, *, force: bool = False) -> str:
    """Install the canonical hermes-bridge command itself."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    dest = bin_dir / "hermes-bridge"
    if dest.exists() or dest.is_symlink():
        if dest.is_symlink() and _same_path(dest, target):
            return f"ok: {dest} -> {target}"
        if not force:
            raise BridgeError(f"refusing to replace existing path: {dest}; use --force")
        if dest.is_dir() and not dest.is_symlink():
            raise BridgeError(f"refusing to replace directory: {dest}")
        dest.unlink()
    os.symlink(target, dest)
    return f"linked: {dest} -> {target}"


def unlink_agent(agent: AgentConfig, bin_dir: Path, *, force: bool = False) -> str:
    dest = bin_dir / agent.command
    if not dest.exists() and not dest.is_symlink():
        return f"absent: {dest}"
    if dest.is_dir() and not dest.is_symlink():
        raise BridgeError(f"refusing to remove directory: {dest}")
    if not force and not dest.is_symlink():
        text = dest.read_text(errors="ignore")[:200] if dest.is_file() else ""
        if "hermes-bridge" not in text:
            raise BridgeError(f"refusing to remove non-hermes-bridge file: {dest}; use --force")
    dest.unlink()
    return f"removed: {dest}"


def select_agents(config: BridgeConfig, names: Iterable[str], all_agents: bool) -> list[AgentConfig]:
    if all_agents:
        return config.agents()
    out = []
    for name in names:
        agent = config.agent_for_token(name)
        if not agent:
            raise BridgeError(f"unknown agent: {name}")
        out.append(agent)
    if not out:
        raise BridgeError("specify an agent or --all")
    return out
