from __future__ import annotations

import shutil
import shlex

from . import __version__
from .config import AgentConfig, BridgeConfig
from .remote import Remote
from .tmux import list_sessions


def _tmux_invocation(agent: AgentConfig) -> str:
    pieces = [agent.remote_tmux_cmd()]
    socket_path = agent.tmux_socket_path()
    if socket_path:
        pieces.extend(["-S", socket_path])
    return " ".join(shlex.quote(piece) for piece in pieces)


def doctor_agent(agent: AgentConfig, *, remote_checks: bool = True) -> str:
    lines = [f"{agent.display_name} ({agent.key})", "-" * (len(agent.display_name) + len(agent.key) + 3)]
    lines.append(f"hermes_bridge_version: {__version__}")
    lines.append(f"command: {agent.command}")
    lines.append(f"ssh_alias: {agent.ssh_alias}")
    lines.append(f"remote_hermes_cmd: {agent.remote_hermes_cmd}")
    lines.append(f"remote_term: {agent.remote_term}")
    lines.append(f"remote_path: {agent.remote_path}")
    if agent.capability_enabled("tmux"):
        socket = agent.tmux_socket_path() or "<default>"
        style_overrides = ",".join(agent.tmux_style_overrides().keys()) or "<default>"
        lines.append(f"tmux: enabled prefix={agent.tmux_prefix()} cmd={agent.remote_tmux_cmd()} socket={socket} geometry={agent.tmux_geometry()[0]}x{agent.tmux_geometry()[1]} style_overrides={style_overrides}")
    else:
        lines.append("tmux: disabled")
    lines.append(f"sessions: {'enabled' if agent.capability_enabled('sessions') else 'disabled'}")
    upload = agent.block("upload")
    if upload:
        enabled = [k for k, v in upload.items() if isinstance(v, dict) and v.get("enabled")]
        lines.append(f"upload: {', '.join(enabled) if enabled else 'disabled'}")
    else:
        lines.append("upload: disabled")
    if remote_checks:
        remote = Remote(agent)
        cmd = f"echo user=$(whoami); echo host=$(hostname); echo hermes=$({shlex.quote(agent.remote_hermes_cmd)} --version 2>/dev/null || true)"
        if agent.capability_enabled("tmux"):
            tmux = _tmux_invocation(agent)
            cmd += f"; echo tmux_configured_cmd={shlex.quote(agent.remote_tmux_cmd())}"
            cmd += f"; echo tmux_resolved=$(command -v {shlex.quote(agent.remote_tmux_cmd())} 2>/dev/null || true)"
            cmd += f"; echo tmux=$({tmux} -V 2>/dev/null || true)"
            cmd += f"; echo tmux_socket=$({tmux} display-message -p '#{{socket_path}}' 2>/dev/null || true)"
            cmd += f"; echo tmux_sessions=$({tmux} list-sessions -F '#{{session_name}}' 2>/dev/null | tr '\\n' ',' | sed 's/,$//' || true)"
        result = remote.run(cmd)
        lines.append("remote:")
        if result.returncode == 0:
            lines.extend(f"  {line}" for line in result.stdout.splitlines())
            if agent.capability_enabled("tmux"):
                try:
                    sessions = list_sessions(agent, remote)
                    lines.append(f"  live_tmux_sessions={len(sessions)}")
                except Exception as exc:
                    lines.append(f"  live_tmux_sessions_error={exc}")
        else:
            lines.append(f"  ERROR exit={result.returncode} stderr={result.stderr.strip()}")
    return "\n".join(lines)


def doctor_config(config: BridgeConfig) -> str:
    lines = ["hermes-bridge doctor", "====================", f"hermes_bridge_version: {__version__}", f"config: {config.path}"]
    lines.append(f"local ssh: {shutil.which('ssh') or '<missing>'}")
    lines.append(f"local bin dir: {config.local_bin_dir}")
    lines.append(f"templates dir: {config.templates_dir}")
    lines.append("")
    lines.extend(config.validate())
    return "\n".join(lines)
