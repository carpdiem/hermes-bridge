from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from typing import Iterable, Optional

from .config import AgentConfig, BridgeConfig
from .errors import BridgeError, CapabilityError, ConfigError
from .remote import Remote
from .tmux import create_session_exact, hermes_tui_command, list_sessions


@dataclass(frozen=True)
class UpdateOptions:
    dry_run: bool = False
    yes: bool = False
    check: bool = False
    hermes_args: tuple[str, ...] = ()
    stop_timeout: int = 45


@dataclass(frozen=True)
class TmuxTuiInventory:
    name: str
    attached: bool
    created: str
    pane_pids: tuple[int, ...]
    tui_detected: bool
    active_session_files: tuple[str, ...]
    session_ids: tuple[str, ...]
    active_file_errors: tuple[str, ...]

    @property
    def resume_session_id(self) -> str:
        return self.session_ids[0] if len(self.session_ids) == 1 else ""

    @property
    def ambiguous(self) -> bool:
        return len(self.session_ids) > 1

    @property
    def blank_or_unknown(self) -> bool:
        return self.tui_detected and not self.session_ids


@dataclass(frozen=True)
class AgentUpdatePlan:
    agent: AgentConfig
    sessions: tuple[TmuxTuiInventory, ...]
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def safe_to_apply(self) -> bool:
        return not self.blockers


_INVENTORY_SCRIPT = r'''
import json
import os
import subprocess
import sys


def run(args):
    return subprocess.run(args, text=True, capture_output=True)


tmux = json.loads(sys.argv[1])
prefix = sys.argv[2]
fmt = "#{session_name}\t#{session_attached}\t#{session_created}"
proc = run(tmux + ["list-sessions", "-F", fmt])
if proc.returncode != 0:
    msg = (proc.stderr or proc.stdout or "").strip()
    if "no server running" in msg or "failed to connect" in msg or "no sessions" in msg:
        print("[]")
        sys.exit(0)
    print(msg, file=sys.stderr)
    sys.exit(proc.returncode)

sessions = []
for line in proc.stdout.splitlines():
    parts = line.split("\t")
    if len(parts) < 3:
        continue
    name, attached, created = parts[:3]
    if name == prefix or name.startswith(prefix + "-"):
        sessions.append({"name": name, "attached": attached != "0", "created": created})

ps_tree = run(["ps", "axo", "pid=,ppid="])
children = {}
if ps_tree.returncode == 0:
    for line in ps_tree.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        children.setdefault(ppid, []).append(pid)


def descendants(root):
    out = []
    stack = list(children.get(root, []))
    seen = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        out.append(pid)
        stack.extend(children.get(pid, []))
    return out


def command_for(pid):
    proc = run(["ps", "-p", str(pid), "-o", "command="])
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def active_file_for(pid):
    # Deliberately parse remotely and only return this one env var, never the
    # full environment, because Hermes processes often carry secrets.
    proc = run(["ps", "eww", "-p", str(pid)])
    if proc.returncode != 0:
        return ""
    for token in proc.stdout.replace("\n", " ").split():
        if token.startswith("HERMES_TUI_ACTIVE_SESSION_FILE="):
            return token.split("=", 1)[1]
    return ""


def read_session_id(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        value = str(data.get("session_id") or "").strip()
        return value, ""
    except Exception as exc:
        return "", f"{type(exc).__name__}: {exc}"

out = []
for session in sessions:
    panes = run(tmux + ["list-panes", "-t", session["name"], "-F", "#{pane_pid}"])
    pane_pids = []
    if panes.returncode == 0:
        for line in panes.stdout.splitlines():
            try:
                pane_pids.append(int(line.strip()))
            except ValueError:
                pass

    active_files = set()
    session_ids = set()
    active_file_errors = set()
    tui_detected = False

    for pane_pid in pane_pids:
        for pid in [pane_pid] + descendants(pane_pid):
            cmd = command_for(pid)
            low = cmd.lower()
            if ("hermes" in low and "--tui" in low) or "ui-tui" in low or "tui_gateway" in low:
                tui_detected = True
            active = active_file_for(pid)
            if active:
                tui_detected = True
                active_files.add(active)
                sid, err = read_session_id(active)
                if sid:
                    session_ids.add(sid)
                elif err:
                    active_file_errors.add(f"{active}: {err}")

    out.append({
        "name": session["name"],
        "attached": session["attached"],
        "created": session["created"],
        "pane_pids": pane_pids,
        "tui_detected": tui_detected,
        "active_session_files": sorted(active_files),
        "session_ids": sorted(session_ids),
        "active_file_errors": sorted(active_file_errors),
    })

print(json.dumps(out, sort_keys=True))
'''


def _tmux_argv(agent: AgentConfig) -> list[str]:
    parts = shlex.split(agent.remote_tmux_cmd())
    socket_path = agent.tmux_socket_path()
    if socket_path:
        parts.extend(["-S", socket_path])
    return parts


def _inventory_command(agent: AgentConfig) -> str:
    return " ".join(
        [
            "python3",
            "-c",
            shlex.quote(_INVENTORY_SCRIPT),
            shlex.quote(json.dumps(_tmux_argv(agent))),
            shlex.quote(agent.tmux_prefix()),
        ]
    )


def inventory_agent(agent: AgentConfig, remote: Optional[Remote] = None) -> tuple[TmuxTuiInventory, ...]:
    agent.tmux_block()
    remote = remote or Remote(agent)
    result = remote.run(_inventory_command(agent), check=True)
    try:
        raw = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise BridgeError(f"could not parse remote update inventory for {agent.display_name}: {exc}") from exc
    sessions = []
    for item in raw:
        sessions.append(
            TmuxTuiInventory(
                name=str(item.get("name") or ""),
                attached=bool(item.get("attached")),
                created=str(item.get("created") or ""),
                pane_pids=tuple(int(x) for x in item.get("pane_pids") or []),
                tui_detected=bool(item.get("tui_detected")),
                active_session_files=tuple(str(x) for x in item.get("active_session_files") or []),
                session_ids=tuple(str(x) for x in item.get("session_ids") or []),
                active_file_errors=tuple(str(x) for x in item.get("active_file_errors") or []),
            )
        )
    return tuple(sessions)


def build_update_plan(agent: AgentConfig, remote: Optional[Remote] = None) -> AgentUpdatePlan:
    sessions = inventory_agent(agent, remote)
    blockers: list[str] = []
    warnings: list[str] = []
    for session in sessions:
        if session.attached:
            blockers.append(f"{session.name}: attached tmux session; detach before updating")
        if not session.tui_detected:
            blockers.append(f"{session.name}: no Hermes TUI process detected; refusing to manage it as an update target")
        if session.ambiguous:
            blockers.append(
                f"{session.name}: multiple live Hermes session IDs detected ({', '.join(session.session_ids)}); refusing to guess"
            )
        if session.active_file_errors:
            warnings.append(
                f"{session.name}: active-session file warning: " + "; ".join(session.active_file_errors)
            )
        if session.blank_or_unknown:
            warnings.append(f"{session.name}: no live Hermes session ID found; will recreate as a blank TUI")
    return AgentUpdatePlan(agent=agent, sessions=sessions, blockers=tuple(blockers), warnings=tuple(warnings))


def format_plan(plan: AgentUpdatePlan, *, include_header: bool = True) -> str:
    lines: list[str] = []
    if include_header:
        lines.append(f"Hermes Bridge update plan for {plan.agent.display_name}")
        lines.append("")
    if not plan.sessions:
        lines.append(f"No live {plan.agent.display_name} bridge tmux sessions found.")
    else:
        lines.append("Bridge tmux sessions:")
        for session in plan.sessions:
            state = "attached" if session.attached else "detached"
            if session.ambiguous:
                resume = "AMBIGUOUS: " + ", ".join(session.session_ids)
            elif session.resume_session_id:
                resume = f"resume {session.resume_session_id}"
            elif session.tui_detected:
                resume = "blank/unknown TUI"
            else:
                resume = "not a Hermes TUI"
            lines.append(f"  - {session.name:<32} {state:<9} {resume}")
    if plan.blockers:
        lines.append("")
        lines.append("Blockers:")
        for blocker in plan.blockers:
            lines.append(f"  - {blocker}")
    if plan.warnings:
        lines.append("")
        lines.append("Warnings:")
        for warning in plan.warnings:
            lines.append(f"  - {warning}")
    if plan.safe_to_apply:
        lines.append("")
        lines.append("Plan is safe to apply. Detached sessions will be closed, Hermes updated, then tmux sessions recreated.")
    return "\n".join(lines)


def _stop_command(agent: AgentConfig, names: Iterable[str], timeout: int) -> str:
    tmux = " ".join(shlex.quote(part) for part in _tmux_argv(agent))
    name_list = list(names)
    if not name_list:
        return ":"
    # Do not rely on shell word-splitting of a scalar list. zsh preserves
    # ``$names`` as one word by default, which turns several tmux sessions into
    # one invalid target. Emit explicit per-session commands instead.
    send_commands = []
    alive_checks = []
    for name in name_list:
        target = shlex.quote(name)
        send_commands.append(
            f"if {tmux} has-session -t {target} >/dev/null 2>&1; then {tmux} send-keys -t {target} /exit Enter; fi"
        )
        alive_checks.append(
            f"if {tmux} has-session -t {target} >/dev/null 2>&1; then alive=\"$alive {name}\"; fi"
        )
    return (
        "; ".join(send_commands)
        + "; "
        + f"deadline=$(( $(date +%s) + {int(timeout)} )); "
        + "while :; do "
        + "alive=; "
        + "; ".join(alive_checks)
        + "; "
        + "if [ -z \"$alive\" ]; then exit 0; fi; "
        + "if [ $(date +%s) -ge $deadline ]; then printf 'sessions did not exit before timeout:%s\\n' \"$alive\" >&2; exit 91; fi; "
        + "sleep 1; "
        + "done"
    )


def stop_sessions(plan: AgentUpdatePlan, options: UpdateOptions, remote: Optional[Remote] = None) -> None:
    if not plan.safe_to_apply:
        raise BridgeError("update plan has blockers; refusing to stop sessions")
    names = [session.name for session in plan.sessions]
    if not names:
        return
    remote = remote or Remote(plan.agent)
    remote.run(_stop_command(plan.agent, names, options.stop_timeout), check=True)


def run_hermes_update(agent: AgentConfig, options: UpdateOptions, remote: Optional[Remote] = None) -> str:
    remote = remote or Remote(agent)
    args = [agent.remote_hermes_cmd, "update", *options.hermes_args]
    if options.yes and "--yes" not in args and "-y" not in args:
        args.append("--yes")
    result = remote.run(shlex.join(args), check=True)
    return (result.stdout or "") + (result.stderr or "")


def rehydrate_sessions(plan: AgentUpdatePlan, remote: Optional[Remote] = None) -> tuple[str, ...]:
    remote = remote or Remote(plan.agent)
    recreated: list[str] = []
    for session in plan.sessions:
        hermes_args = ["--resume", session.resume_session_id] if session.resume_session_id else []
        command = hermes_tui_command(plan.agent, hermes_args)
        recreated.append(create_session_exact(plan.agent, session.name, command, remote))
    return tuple(recreated)


def execute_agent_update(agent: AgentConfig, options: UpdateOptions, remote: Optional[Remote] = None) -> str:
    remote = remote or Remote(agent)
    if options.check:
        output = run_hermes_update(agent, options, remote)
        return output.rstrip() or f"{agent.display_name}: hermes update --check completed."

    plan = build_update_plan(agent, remote)
    lines = [format_plan(plan)]
    dry_run = options.dry_run or not options.yes
    if dry_run:
        lines.append("")
        lines.append("Dry run only; no sessions stopped and no update run. Re-run with --yes to apply.")
        return "\n".join(lines)
    if not plan.safe_to_apply:
        raise BridgeError(format_plan(plan, include_header=False))

    stopped = bool(plan.sessions)
    lines.append("")
    lines.append("Stopping detached bridge tmux sessions...")
    stop_sessions(plan, options, remote)
    lines.append("Stopped.")

    update_error: Optional[Exception] = None
    try:
        lines.append("Running hermes update...")
        update_output = run_hermes_update(agent, options, remote)
        if update_output.strip():
            lines.append(update_output.rstrip())
        lines.append("Hermes update completed.")
    except Exception as exc:  # rehydrate before surfacing the failure
        update_error = exc
        lines.append(f"Hermes update failed: {exc}")
    finally:
        if stopped:
            lines.append("Recreating bridge tmux sessions...")
            try:
                recreated = rehydrate_sessions(plan, remote)
                lines.append("Recreated: " + (", ".join(recreated) if recreated else "none"))
            except Exception as exc:
                lines.append(f"Rehydrate failed: {exc}")
                if update_error is None:
                    update_error = exc

    if update_error is not None:
        raise BridgeError("\n".join(lines)) from update_error
    return "\n".join(lines)


def parse_update_options(args: list[str]) -> tuple[UpdateOptions, list[str]]:
    dry_run = False
    yes = False
    check = False
    stop_timeout = 45
    hermes_args: list[str] = []
    rest: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--dry-run":
            dry_run = True
            i += 1
        elif arg in {"--yes", "-y"}:
            yes = True
            i += 1
        elif arg == "--check":
            check = True
            hermes_args.append(arg)
            i += 1
        elif arg == "--stop-timeout":
            if i + 1 >= len(args):
                raise BridgeError("--stop-timeout requires seconds")
            try:
                stop_timeout = int(args[i + 1])
            except ValueError as exc:
                raise BridgeError("--stop-timeout requires an integer number of seconds") from exc
            i += 2
        elif arg in {"--backup", "--no-backup", "--force"}:
            hermes_args.append(arg)
            i += 1
        elif arg == "--branch":
            if i + 1 >= len(args):
                raise BridgeError("--branch requires a value")
            hermes_args.extend([arg, args[i + 1]])
            i += 2
        else:
            rest.append(arg)
            i += 1
    if dry_run and yes:
        raise BridgeError("choose either --dry-run or --yes, not both")
    return UpdateOptions(dry_run=dry_run, yes=yes, check=check, hermes_args=tuple(hermes_args), stop_timeout=stop_timeout), rest


def selectable_update_agents(config: BridgeConfig) -> list[AgentConfig]:
    return [agent for agent in config.agents() if agent.capability_enabled("tmux")]


def resolve_fleet_agents(config: BridgeConfig, args: list[str]) -> tuple[list[AgentConfig], list[str]]:
    all_selected = False
    names: list[str] = []
    rest: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--all":
            all_selected = True
            i += 1
        elif arg in {"--branch", "--stop-timeout"}:
            if i + 1 >= len(args):
                raise BridgeError(f"{arg} requires a value")
            rest.extend([arg, args[i + 1]])
            i += 2
        elif arg.startswith("-"):
            rest.append(arg)
            i += 1
        else:
            names.append(arg)
            i += 1
    if all_selected:
        if names:
            raise BridgeError("use either --all or explicit agent names, not both")
        return selectable_update_agents(config), rest
    if not names:
        raise BridgeError("fleet update requires --all or one or more agent names")
    agents: list[AgentConfig] = []
    for name in names:
        agent = config.agent_for_token(name)
        if not agent:
            raise BridgeError(f"unknown agent: {name}")
        try:
            agent.tmux_block()
        except (CapabilityError, ConfigError) as exc:
            raise BridgeError(str(exc)) from exc
        agents.append(agent)
    return agents, rest


def fleet_status(config: BridgeConfig) -> str:
    chunks: list[str] = []
    for agent in selectable_update_agents(config):
        chunks.append(format_plan(build_update_plan(agent)))
    return "\n\n".join(chunks) if chunks else "No tmux-enabled agents configured."


def fleet_update(config: BridgeConfig, args: list[str]) -> str:
    agents, option_args = resolve_fleet_agents(config, args)
    options, rest = parse_update_options(option_args)
    if rest:
        raise BridgeError("unknown update option(s): " + " ".join(rest))
    chunks: list[str] = []
    for agent in agents:
        chunks.append(execute_agent_update(agent, options))
    return "\n\n".join(chunks)
