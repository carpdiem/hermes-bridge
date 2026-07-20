from __future__ import annotations

import datetime as _dt
import re
import shlex
from dataclasses import dataclass
from typing import Iterable, Optional

from .config import AgentConfig
from .errors import BridgeError
from .remote import Remote


@dataclass(frozen=True)
class TmuxSession:
    name: str
    attached: bool
    created: str


def sanitize_session_piece(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9_.-]+", "-", value).strip("-")
    return value or _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def session_name_for(prefix: str, requested: Optional[str] = None) -> str:
    base = sanitize_session_piece(requested or prefix)
    if base == prefix or base.startswith(prefix + "-"):
        return base
    return f"{prefix}-{base}"


def _tmux(agent: AgentConfig) -> str:
    pieces = [agent.remote_tmux_cmd()]
    socket_path = agent.tmux_socket_path()
    if socket_path:
        pieces.extend(["-S", socket_path])
    return " ".join(shlex.quote(piece) for piece in pieces)


def list_sessions(agent: AgentConfig, remote: Optional[Remote] = None) -> list[TmuxSession]:
    remote = remote or Remote(agent)
    tmux = _tmux(agent)
    prefix = agent.tmux_prefix()
    fmt = "#{session_name}\t#{session_attached}\t#{session_created}"
    # Do not let the local client mistake transport/config failures for "no sessions".
    # Only a real tmux "no server/sessions" condition is normalized to empty output;
    # ssh failures, bad aliases, bad tmux paths, and format errors must surface.
    command = (
        f"set +e; out=$({tmux} list-sessions -F {shlex.quote(fmt)} 2>&1); rc=$?; set -e; "
        "if [ \"$rc\" -eq 0 ]; then printf '%s\\n' \"$out\"; "
        "else case \"$out\" in "
        "*'no server running'*|*'failed to connect'*|*'no sessions'*|*'error connecting to '*'No such file or directory'*) exit 0 ;; "
        "*) printf '%s\\n' \"$out\" >&2; exit \"$rc\" ;; "
        "esac; fi"
    )
    result = remote.run(command)
    if result.returncode != 0:
        msg = result.stderr.strip() or result.stdout.strip() or "unknown remote tmux error"
        raise BridgeError(f"could not list remote tmux sessions for {agent.display_name} via {agent.ssh_alias}: {msg}")
    sessions: list[TmuxSession] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        name, attached, created = parts[:3]
        if name == prefix or name.startswith(prefix + "-"):
            sessions.append(TmuxSession(name=name, attached=attached != "0", created=created))
    return sessions


def format_sessions(agent: AgentConfig, sessions: Iterable[TmuxSession]) -> str:
    items = list(sessions)
    if not items:
        return f"No live {agent.display_name} tmux sessions."
    lines = [f"Live {agent.display_name} tmux sessions:", ""]
    for i, s in enumerate(items, 1):
        state = "attached" if s.attached else "detached"
        created = s.created
        try:
            created = _dt.datetime.fromtimestamp(int(s.created)).strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
        lines.append(f"  {i:2d}. {s.name:<32} {state:<9} created {created}")
    return "\n".join(lines)


def resolve_selector(selector: str, sessions: list[TmuxSession], agent: AgentConfig) -> str:
    if not selector:
        raise BridgeError("missing tmux session name/number")
    if selector.isdigit():
        idx = int(selector)
        if 1 <= idx <= len(sessions):
            return sessions[idx - 1].name
        raise BridgeError(f"no tmux session number: {selector}")
    prefix = agent.tmux_prefix()
    target = selector if selector == prefix or selector.startswith(prefix + "-") else session_name_for(prefix, selector)
    for session in sessions:
        if session.name == target:
            return session.name
    raise BridgeError(f"no live {agent.display_name} tmux session: {selector}")


def hermes_tui_command(agent: AgentConfig, hermes_args: list[str]) -> str:
    return shlex.join([agent.remote_hermes_cmd, "--tui", *hermes_args])


def _style_options(agent: AgentConfig) -> dict[str, str]:
    label = f" {agent.display_name} | #S "
    options = {
        "status-position": "bottom",
        "status-style": "bg=#3c3836,fg=#ebdbb2",
        "status-left-style": "bg=#3c3836,fg=#fabd2f",
        "status-right-style": "bg=#3c3836,fg=#fabd2f",
        "window-status-current-style": "bg=#3c3836,fg=#fabd2f",
        "message-style": "bg=#d79921,fg=#282828,bold",
        "pane-active-border-style": "fg=#d79921",
        "status-left": label,
        "status-right": " %H:%M ",
    }
    options.update(agent.tmux_style_overrides())
    return options


def _style_commands(agent: AgentConfig, session_var: str = '"$name"') -> str:
    tmux = _tmux(agent)
    return "; ".join(
        f"{tmux} set-option -t {session_var} {shlex.quote(k)} {shlex.quote(v)} >/dev/null 2>&1 || true"
        for k, v in _style_options(agent).items()
    )


def create_session(agent: AgentConfig, base_name: str, remote_command: str, remote: Optional[Remote] = None) -> str:
    remote = remote or Remote(agent)
    prefix = agent.tmux_prefix()
    base = session_name_for(prefix, base_name)
    cols, rows = agent.tmux_geometry()
    tmux = _tmux(agent)
    q_base = shlex.quote(base)
    q_cmd = shlex.quote(remote_command)
    style = _style_commands(agent)
    command = (
        f"base={q_base}; name=\"$base\"; i=2; "
        f"while {tmux} has-session -t \"$name\" >/dev/null 2>&1; do name=\"$base-$i\"; i=$((i+1)); done; "
        f"{tmux} new-session -d -s \"$name\" -x {cols} -y {rows} {q_cmd}; "
        "sleep 0.2; "
        f"if ! {tmux} has-session -t \"$name\" >/dev/null 2>&1; then "
        "printf 'tmux session exited immediately: %s\\n' \"$name\" >&2; exit 90; "
        "fi; "
        f"{style}; "
        "printf '%s\\n' \"$name\""
    )
    result = remote.run(command, check=True)
    name = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    if not name:
        raise BridgeError("tmux session was not created")
    return name


def selector_to_target(agent: AgentConfig, selector: str, remote: Optional[Remote] = None) -> str:
    """Resolve a user selector to a tmux target.

    Numeric selectors require a session listing. Named selectors are normalized
    directly so attach/capture/kill do not first depend on a listing round-trip;
    the tmux command itself is then the source of truth.
    """
    if not selector:
        raise BridgeError("missing tmux session name/number")
    if selector.isdigit():
        sessions = list_sessions(agent, remote)
        return resolve_selector(selector, sessions, agent)
    prefix = agent.tmux_prefix()
    return selector if selector == prefix or selector.startswith(prefix + "-") else session_name_for(prefix, selector)


def attach(agent: AgentConfig, selector: str, remote: Optional[Remote] = None) -> int:
    remote = remote or Remote(agent)
    target = selector_to_target(agent, selector, remote)
    return remote.run(f"{_tmux(agent)} attach-session -t {shlex.quote(target)}", tty=True).returncode


def capture(agent: AgentConfig, selector: str, remote: Optional[Remote] = None) -> str:
    remote = remote or Remote(agent)
    target = selector_to_target(agent, selector, remote)
    result = remote.run(f"{_tmux(agent)} capture-pane -p -S -200 -t {shlex.quote(target)}", check=True)
    return result.stdout


def kill(agent: AgentConfig, selector: str, *, force: bool = False, remote: Optional[Remote] = None) -> str:
    remote = remote or Remote(agent)
    target = selector_to_target(agent, selector, remote)
    if not force:
        answer = input(f"Kill remote tmux session '{target}'? Type 'kill' to confirm: ")
        if answer != "kill":
            raise BridgeError("cancelled")
    remote.run(f"{_tmux(agent)} kill-session -t {shlex.quote(target)}", check=True)
    return target
