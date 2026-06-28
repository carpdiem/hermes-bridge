from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path
from typing import Optional

from . import __version__
from .config import ConfigError, load_config, resolve_config_path
from .doctor import doctor_agent, doctor_config
from .errors import BridgeError
from .linking import link_agent, link_core, select_agents, unlink_agent
from .tmux import attach, capture, create_session, format_sessions, hermes_tui_command, kill, list_sessions
from .update import execute_agent_update, fleet_status, fleet_update, parse_update_options
from .upload import upload

BASE_COMMANDS = {"hermes-bridge", "hermes-bridge.py", "__main__.py"}
GLOBAL_COMMANDS = {"config", "link", "unlink", "install", "doctor", "fleet", "--help", "-h", "help", "--version", "version"}

USAGE = """Usage:
  hermes-bridge <agent> [command] [args...]
  <agent> [command] [args...]              # when invoked through a symlink/wrapper

Agent commands:
  <agent>                                  Start a new remote Hermes TUI in tmux and attach
  <agent> new [name] [-- HERMES_ARGS...]   Start a named remote Hermes TUI
  <agent> tmux list|browse|attach|capture|kill
  <agent> sessions list|browse|resume|continue
  <agent> update [--dry-run|--yes] [--check] [--backup|--no-backup] [--branch NAME]
  <agent> upload <path> [--attach|--foreground] [--name NAME] [--] [message...]
  <agent> upload-book <path> [--attach|--foreground] [--name NAME] [--] [message...]
  <agent> doctor [--no-remote]

Global commands:
  hermes-bridge doctor [--all|AGENT] [--no-remote]
  hermes-bridge fleet status
  hermes-bridge fleet update (--all|AGENT...) [--dry-run|--yes] [--check] [--backup|--no-backup] [--branch NAME]
  hermes-bridge config path|show|validate
  hermes-bridge link [AGENT|--all] [--mode symlink|wrapper] [--bin-dir DIR] [--target PATH] [--force]
  hermes-bridge unlink [AGENT|--all] [--bin-dir DIR] [--force]
  hermes-bridge install [--mode symlink|wrapper] [--bin-dir DIR] [--target PATH] [--force]
  hermes-bridge --version
"""


def print_usage() -> int:
    print(USAGE.rstrip())
    return 0


def _config_path_from_argv(argv: list[str]) -> tuple[Optional[str], list[str]]:
    out: list[str] = []
    path: Optional[str] = None
    i = 0
    while i < len(argv):
        if argv[i] == "--config":
            if i + 1 >= len(argv):
                raise BridgeError("--config requires a path")
            path = argv[i + 1]
            i += 2
        else:
            out.append(argv[i])
            i += 1
    return path, out


def selected_agent(config, argv: list[str], invoked_as: str):
    invoked = Path(invoked_as).name
    agent = None if invoked in BASE_COMMANDS else config.agent_for_token(invoked)
    if agent:
        return agent, argv
    if argv and argv[0] not in GLOBAL_COMMANDS:
        candidate = config.agent_for_token(argv[0])
        if candidate:
            return candidate, argv[1:]
    return None, argv


def split_hermes_args(argv: list[str]) -> tuple[str, list[str]]:
    name = ""
    rest = list(argv)
    if rest and rest[0] != "--" and not rest[0].startswith("-"):
        name = rest.pop(0)
    if rest and rest[0] == "--":
        rest.pop(0)
    return name, rest


def split_upload_args(agent_command: str, cmd: str, args: list[str]) -> tuple[str, str, list[str]]:
    if cmd == "upload-book":
        if not args:
            raise BridgeError(f"usage: {agent_command} upload-book <path> [options] [message...]")
        return "book", args[0], args[1:]

    if not args:
        raise BridgeError(f"usage: {agent_command} upload <path> [options] [message...]")
    if args[0] in {"file", "book"}:
        if len(args) < 2:
            raise BridgeError(f"usage: {agent_command} upload {args[0]} <path> [options] [message...]")
        return args[0], args[1], args[2:]
    return "file", args[0], args[1:]


def handle_agent(config, agent, argv: list[str]) -> int:
    if not argv:
        return handle_agent(config, agent, ["new"])
    cmd = argv[0]
    args = argv[1:]
    if cmd in {"-h", "--help", "help"}:
        return print_usage()
    if cmd in {"--version", "version"}:
        print(f"hermes-bridge {__version__}")
        return 0
    if cmd in {"doctor", "--doctor"}:
        remote = "--no-remote" not in args
        print(doctor_agent(agent, remote_checks=remote))
        return 0
    if cmd == "new":
        agent.tmux_block()
        name, hermes_args = split_hermes_args(args)
        base = name or agent.tmux_prefix()
        session = create_session(agent, base, hermes_tui_command(agent, hermes_args))
        print(f"Started remote tmux session: {session}")
        print("Detach without stopping Hermes: Ctrl-b then d")
        return attach(agent, session)
    if cmd == "tmux":
        agent.tmux_block()
        sub = args[0] if args else "list"
        subargs = args[1:]
        if sub in {"list", "ls"}:
            print(format_sessions(agent, list_sessions(agent)))
            return 0
        if sub == "browse":
            sessions = list_sessions(agent)
            print(format_sessions(agent, sessions))
            if not sessions:
                return 1
            selector = input("\nAttach to session number/name: ").strip()
            return attach(agent, selector)
        if sub == "attach":
            if not subargs:
                raise BridgeError(f"usage: {agent.command} tmux attach <name-or-number>")
            return attach(agent, subargs[0])
        if sub == "capture":
            if not subargs:
                raise BridgeError(f"usage: {agent.command} tmux capture <name-or-number>")
            print(capture(agent, subargs[0]), end="")
            return 0
        if sub == "kill":
            if not subargs:
                raise BridgeError(f"usage: {agent.command} tmux kill <name-or-number> [--force]")
            force = "--force" in subargs[1:]
            target = kill(agent, subargs[0], force=force)
            print(f"Killed: {target}")
            return 0
        raise BridgeError(f"unknown tmux command: {sub}")
    if cmd == "sessions":
        agent.sessions_block()
        sub = args[0] if args else "browse"
        subargs = args[1:]
        from .remote import Remote
        remote = Remote(agent)
        if sub == "browse":
            session = create_session(agent, f"{agent.tmux_prefix()}-sessions", shlex.join([agent.remote_hermes_cmd, "--tui", "sessions", "browse", *subargs]))
            print(f"Started remote tmux session: {session}")
            return attach(agent, session)
        if sub == "list":
            result = remote.run(shlex.join([agent.remote_hermes_cmd, "sessions", "list", *subargs]), check=True)
            print(result.stdout, end="")
            return 0
        if sub == "resume":
            if not subargs:
                raise BridgeError(f"usage: {agent.command} sessions resume <session-id-or-title> [-- HERMES_ARGS...]")
            session_id = subargs[0]
            extra = subargs[1:]
            if extra and extra[0] == "--":
                extra = extra[1:]
            session = create_session(agent, f"{agent.tmux_prefix()}-resume", hermes_tui_command(agent, ["--resume", session_id, *extra]))
            print(f"Started remote tmux session: {session}")
            return attach(agent, session)
        if sub == "continue":
            name, extra = split_hermes_args(subargs)
            hargs = ["--continue"] + ([name] if name else []) + extra
            session = create_session(agent, f"{agent.tmux_prefix()}-continue", hermes_tui_command(agent, hargs))
            print(f"Started remote tmux session: {session}")
            return attach(agent, session)
        raise BridgeError(f"unknown sessions command: {sub}")
    if cmd == "update":
        agent.tmux_block()
        options, rest = parse_update_options(args)
        if rest:
            raise BridgeError("unknown update option(s): " + " ".join(rest))
        print(execute_agent_update(agent, options))
        return 0
    if cmd in {"upload", "upload-book"}:
        kind, src, rest = split_upload_args(agent.command, cmd, args)
        foreground = False
        attach_flag = False
        task_name = None
        message_parts: list[str] = []
        i = 0
        while i < len(rest):
            part = rest[i]
            if part == "--foreground":
                foreground = True; i += 1
            elif part == "--attach":
                attach_flag = True; i += 1
            elif part == "--name":
                if i + 1 >= len(rest):
                    raise BridgeError("--name requires a value")
                task_name = rest[i + 1]; i += 2
            elif part == "--":
                message_parts = rest[i + 1:]; break
            elif part.startswith("--"):
                raise BridgeError(f"unknown upload option: {part}")
            else:
                message_parts = rest[i:]; break
        print(upload(config, agent, kind, src, " ".join(message_parts), foreground=foreground, attach=attach_flag, task_name=task_name))
        return 0
    raise BridgeError(f"unknown command for {agent.command}: {cmd}")


def _parse_common_options(args: list[str]) -> tuple[dict, list[str]]:
    opts = {"all": False, "force": False, "mode": "symlink", "bin_dir": None, "target": None, "remote": True}
    rest: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--all": opts["all"] = True; i += 1
        elif a == "--force": opts["force"] = True; i += 1
        elif a == "--no-remote": opts["remote"] = False; i += 1
        elif a in {"--mode", "--bin-dir", "--target"}:
            if i + 1 >= len(args):
                raise BridgeError(f"{a} requires a value")
            if a == "--mode":
                opts["mode"] = args[i+1]
            elif a == "--bin-dir":
                opts["bin_dir"] = args[i+1]
            else:
                opts["target"] = args[i+1]
            i += 2
        else: rest.append(a); i += 1
    return opts, rest


def handle_global(config, argv: list[str], invoked_as: str) -> int:
    if not argv or argv[0] in {"-h", "--help", "help"}:
        return print_usage()
    cmd, args = argv[0], argv[1:]
    if cmd in {"--version", "version"}:
        print(f"hermes-bridge {__version__}")
        return 0
    if cmd == "config":
        sub = args[0] if args else "path"
        if sub == "path":
            print(config.path)
        elif sub == "show":
            print(config.path.read_text(), end="")
        elif sub == "validate":
            for line in config.validate():
                print(line)
        else:
            raise BridgeError("usage: hermes-bridge config path|show|validate")
        return 0
    if cmd == "doctor":
        opts, rest = _parse_common_options(args)
        print(doctor_config(config))
        if opts["all"]:
            targets = config.agents()
        elif rest:
            target = config.agent_for_token(rest[0])
            if not target:
                raise BridgeError(f"unknown agent: {rest[0]}")
            targets = [target]
        else:
            targets = []
        for agent in targets:
            print("\n" + doctor_agent(agent, remote_checks=opts["remote"]))
        return 0
    if cmd == "fleet":
        sub = args[0] if args else "status"
        subargs = args[1:]
        if sub == "status":
            print(fleet_status(config))
            return 0
        if sub == "update":
            print(fleet_update(config, subargs))
            return 0
        raise BridgeError("usage: hermes-bridge fleet status|update")
    if cmd in {"link", "install"}:
        opts, rest = _parse_common_options(args)
        all_agents = bool(opts["all"] or cmd == "install")
        agents = select_agents(config, rest, all_agents)
        bin_dir = Path(opts["bin_dir"] or config.local_bin_dir).expanduser()
        target = Path(opts["target"] or sys.argv[0]).expanduser().resolve()
        if cmd == "install":
            print(link_core(bin_dir, target, force=bool(opts["force"])))
        for agent in agents:
            print(link_agent(agent, bin_dir, target, mode=str(opts["mode"]), force=bool(opts["force"])))
        if str(bin_dir) not in os.environ.get("PATH", "").split(os.pathsep):
            print(f"warning: {bin_dir} is not currently on PATH")
        return 0
    if cmd == "unlink":
        opts, rest = _parse_common_options(args)
        agents = select_agents(config, rest, bool(opts["all"]))
        bin_dir = Path(opts["bin_dir"] or config.local_bin_dir).expanduser()
        for agent in agents:
            print(unlink_agent(agent, bin_dir, force=bool(opts["force"])))
        return 0
    raise BridgeError(f"unknown global command: {cmd}")


def main(argv: Optional[list[str]] = None, invoked_as: Optional[str] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    invoked_as = invoked_as or sys.argv[0]
    try:
        config_path, argv = _config_path_from_argv(argv)
        if not argv or argv[0] in {"-h", "--help", "help"}:
            return print_usage()
        if argv[0] in {"--version", "version"}:
            print(f"hermes-bridge {__version__}")
            return 0
        if argv[:2] == ["config", "path"]:
            print(resolve_config_path(config_path))
            return 0
        config = load_config(config_path)
        agent, remaining = selected_agent(config, argv, invoked_as)
        if agent:
            return handle_agent(config, agent, remaining)
        return handle_global(config, remaining, invoked_as)
    except (BridgeError, ConfigError) as exc:
        print(f"hermes-bridge: {exc}", file=sys.stderr)
        return 2


def entrypoint() -> int:
    return main()
