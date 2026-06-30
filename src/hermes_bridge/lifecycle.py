from __future__ import annotations

import json
import os
import shlex
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Optional

from .config import AgentConfig
from .errors import BridgeError
from .remote import Remote
from .tmux import create_session_exact, hermes_tui_command, list_sessions


@dataclass(frozen=True)
class LifecycleOptions:
    dry_run: bool = False
    replace: bool = False
    snapshot: str = "current"
    stop_timeout: int = 45


@dataclass(frozen=True)
class TmuxTuiInventory:
    name: str
    attached: bool
    created: str
    pane_pids: tuple[int, ...]
    tui_pids: tuple[int, ...]
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
class LifecyclePlan:
    agent: AgentConfig
    sessions: tuple[TmuxTuiInventory, ...]
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def safe_to_apply(self) -> bool:
        return not self.blockers


@dataclass(frozen=True)
class SnapshotSession:
    name: str
    resume_session_id: str = ""


@dataclass(frozen=True)
class Snapshot:
    version: int
    agent_command: str
    agent_display_name: str
    created_at: int
    sessions: tuple[SnapshotSession, ...]
    warnings: tuple[str, ...] = ()


_INVENTORY_SCRIPT = r'''
import json
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
    tui_pids = set()
    tui_detected = False

    for pane_pid in pane_pids:
        for pid in [pane_pid] + descendants(pane_pid):
            cmd = command_for(pid)
            low = cmd.lower()
            is_node_tui = "node" in low and "ui-tui" in low
            is_gateway = "tui_gateway" in low
            if ("hermes" in low and "--tui" in low) or is_node_tui or is_gateway:
                tui_detected = True
            if is_node_tui:
                tui_pids.add(pid)
            active = active_file_for(pid)
            if active:
                tui_detected = True
                active_files.add(active)
                if is_node_tui:
                    tui_pids.add(pid)
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
        "tui_pids": sorted(tui_pids),
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


def snapshot_dir() -> Path:
    base = os.environ.get("HERMES_BRIDGE_SNAPSHOT_DIR")
    if base:
        return Path(base).expanduser()
    return Path.home() / ".cache" / "hermes-bridge" / "snapshots"


def snapshot_path(agent: AgentConfig, name: str = "current") -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in name.strip()) or "current"
    return snapshot_dir() / agent.command / f"{safe}.json"


def inventory_agent(agent: AgentConfig, remote: Optional[Remote] = None) -> tuple[TmuxTuiInventory, ...]:
    agent.tmux_block()
    remote = remote or Remote(agent)
    result = remote.run(_inventory_command(agent), check=True)
    try:
        raw = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise BridgeError(f"could not parse remote lifecycle inventory for {agent.display_name}: {exc}") from exc
    sessions = []
    for item in raw:
        sessions.append(
            TmuxTuiInventory(
                name=str(item.get("name") or ""),
                attached=bool(item.get("attached")),
                created=str(item.get("created") or ""),
                pane_pids=tuple(int(x) for x in item.get("pane_pids") or []),
                tui_pids=tuple(int(x) for x in item.get("tui_pids") or []),
                tui_detected=bool(item.get("tui_detected")),
                active_session_files=tuple(str(x) for x in item.get("active_session_files") or []),
                session_ids=tuple(str(x) for x in item.get("session_ids") or []),
                active_file_errors=tuple(str(x) for x in item.get("active_file_errors") or []),
            )
        )
    return tuple(sessions)


def build_lifecycle_plan(agent: AgentConfig, remote: Optional[Remote] = None) -> LifecyclePlan:
    sessions = inventory_agent(agent, remote)
    blockers: list[str] = []
    warnings: list[str] = []
    for session in sessions:
        if session.attached:
            blockers.append(f"{session.name}: attached tmux session; detach before dehydrating")
        if not session.tui_detected:
            blockers.append(f"{session.name}: no Hermes TUI process detected; refusing to manage it")
        if session.ambiguous:
            blockers.append(
                f"{session.name}: multiple live Hermes session IDs detected ({', '.join(session.session_ids)}); refusing to guess"
            )
        if session.active_file_errors:
            warnings.append(f"{session.name}: active-session file warning: " + "; ".join(session.active_file_errors))
        if session.blank_or_unknown:
            warnings.append(f"{session.name}: no live Hermes session ID found; will recreate as a blank TUI")
    return LifecyclePlan(agent=agent, sessions=sessions, blockers=tuple(blockers), warnings=tuple(warnings))


def format_plan(plan: LifecyclePlan, *, include_header: bool = True) -> str:
    lines: list[str] = []
    if include_header:
        lines.append(f"Hermes Bridge lifecycle plan for {plan.agent.display_name}")
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
        lines.append("Plan is safe to dehydrate. Detached sessions will be closed and snapshotted for later rehydrate.")
    return "\n".join(lines)


def _snapshot_from_plan(plan: LifecyclePlan) -> Snapshot:
    return Snapshot(
        version=1,
        agent_command=plan.agent.command,
        agent_display_name=plan.agent.display_name,
        created_at=int(time.time()),
        sessions=tuple(SnapshotSession(name=s.name, resume_session_id=s.resume_session_id) for s in plan.sessions),
        warnings=plan.warnings,
    )


def _snapshot_to_json(snapshot: Snapshot) -> str:
    return json.dumps(asdict(snapshot), indent=2, sort_keys=True) + "\n"


def save_snapshot(snapshot: Snapshot, path: Path, *, replace: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not replace:
        raise BridgeError(f"snapshot already exists: {path}\nUse --replace to overwrite it, or --snapshot NAME for a separate snapshot.")
    tmp_fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
            handle.write(_snapshot_to_json(snapshot))
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def load_snapshot(agent: AgentConfig, name: str = "current") -> tuple[Snapshot, Path]:
    path = snapshot_path(agent, name)
    if not path.exists():
        raise BridgeError(f"no snapshot found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        sessions = tuple(
            SnapshotSession(name=str(item.get("name") or ""), resume_session_id=str(item.get("resume_session_id") or ""))
            for item in raw.get("sessions") or []
        )
        snap = Snapshot(
            version=int(raw.get("version") or 0),
            agent_command=str(raw.get("agent_command") or ""),
            agent_display_name=str(raw.get("agent_display_name") or ""),
            created_at=int(raw.get("created_at") or 0),
            sessions=sessions,
            warnings=tuple(str(x) for x in raw.get("warnings") or []),
        )
    except Exception as exc:
        raise BridgeError(f"could not read snapshot {path}: {exc}") from exc
    if snap.version != 1:
        raise BridgeError(f"unsupported snapshot version {snap.version}: {path}")
    if snap.agent_command and snap.agent_command != agent.command:
        raise BridgeError(f"snapshot belongs to agent {snap.agent_command}, not {agent.command}: {path}")
    return snap, path


def _stop_command(agent: AgentConfig, targets: Iterable[TmuxTuiInventory], timeout: int) -> str:
    tmux = " ".join(shlex.quote(part) for part in _tmux_argv(agent))
    target_list = [(target.name, target.tui_pids) for target in targets]
    if not target_list:
        return ":"
    send_commands = []
    alive_checks = []
    for name, pids in target_list:
        target = shlex.quote(name)
        if pids:
            pid_args = " ".join(str(int(pid)) for pid in pids)
            send_commands.append(f"kill -TERM {pid_args} >/dev/null 2>&1 || true")
        else:
            send_commands.append(
                f"if {tmux} has-session -t {target} >/dev/null 2>&1; then "
                f"{tmux} send-keys -t {target} C-c; sleep 0.1; {tmux} send-keys -t {target} /quit Enter; "
                "fi"
            )
        alive_checks.append(f"if {tmux} has-session -t {target} >/dev/null 2>&1; then alive=\"$alive {name}\"; fi")
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


def parse_lifecycle_options(args: list[str]) -> tuple[LifecycleOptions, list[str]]:
    dry_run = False
    replace = False
    snapshot = "current"
    stop_timeout = 45
    rest: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--dry-run":
            dry_run = True
            i += 1
        elif arg == "--replace":
            replace = True
            i += 1
        elif arg == "--snapshot":
            if i + 1 >= len(args):
                raise BridgeError("--snapshot requires a name")
            snapshot = args[i + 1]
            i += 2
        elif arg == "--stop-timeout":
            if i + 1 >= len(args):
                raise BridgeError("--stop-timeout requires seconds")
            try:
                stop_timeout = int(args[i + 1])
            except ValueError as exc:
                raise BridgeError("--stop-timeout requires an integer") from exc
            i += 2
        else:
            rest.append(arg)
            i += 1
    return LifecycleOptions(dry_run=dry_run, replace=replace, snapshot=snapshot, stop_timeout=stop_timeout), rest


def dehydrate(agent: AgentConfig, options: LifecycleOptions, remote: Optional[Remote] = None) -> str:
    remote = remote or Remote(agent)
    plan = build_lifecycle_plan(agent, remote)
    path = snapshot_path(agent, options.snapshot)
    lines = [format_plan(plan), "", f"Snapshot: {path}"]
    if options.dry_run:
        lines.append("Dry run only; no snapshot written and no sessions stopped.")
        return "\n".join(lines)
    if not plan.safe_to_apply:
        raise BridgeError(format_plan(plan, include_header=False))
    snapshot = _snapshot_from_plan(plan)
    save_snapshot(snapshot, path, replace=options.replace)
    remote.run(_stop_command(agent, plan.sessions, options.stop_timeout), check=True)
    lines.append(f"Wrote snapshot: {path}")
    lines.append("Dehydrated: " + (", ".join(s.name for s in plan.sessions) if plan.sessions else "none"))
    lines.append("Run your manual Hermes update now, then rehydrate with:")
    lines.append(f"  {agent.command} rehydrate" + ("" if options.snapshot == "current" else f" --snapshot {options.snapshot}"))
    return "\n".join(lines)


def rehydrate(agent: AgentConfig, options: LifecycleOptions, remote: Optional[Remote] = None) -> str:
    remote = remote or Remote(agent)
    snapshot, path = load_snapshot(agent, options.snapshot)
    live_names = {session.name for session in list_sessions(agent, remote)}
    conflicts = [session.name for session in snapshot.sessions if session.name in live_names]
    lines = [f"Hermes Bridge rehydrate plan for {agent.display_name}", "", f"Snapshot: {path}"]
    if not snapshot.sessions:
        lines.append("Snapshot contains no sessions.")
    else:
        lines.append("Sessions to recreate:")
        for session in snapshot.sessions:
            mode = f"resume {session.resume_session_id}" if session.resume_session_id else "blank TUI"
            conflict = " CONFLICT: already live" if session.name in conflicts else ""
            lines.append(f"  - {session.name:<32} {mode}{conflict}")
    if conflicts:
        lines.append("")
        lines.append("Blockers:")
        for name in conflicts:
            lines.append(f"  - {name}: tmux session already exists; attach/kill/rename it before rehydrate")
        if not options.dry_run:
            raise BridgeError("\n".join(lines))
    if options.dry_run:
        lines.append("")
        lines.append("Dry run only; no sessions recreated.")
        return "\n".join(lines)
    recreated: list[str] = []
    for session in snapshot.sessions:
        args = ["--resume", session.resume_session_id] if session.resume_session_id else []
        recreated.append(create_session_exact(agent, session.name, hermes_tui_command(agent, args), remote))
    lines.append("")
    lines.append("Rehydrated: " + (", ".join(recreated) if recreated else "none"))
    lines.append("Snapshot kept for safety. Remove it manually when satisfied:")
    lines.append(f"  rm {shlex.quote(str(path))}")
    return "\n".join(lines)
