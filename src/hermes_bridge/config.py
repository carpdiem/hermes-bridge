from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .errors import ConfigError, CapabilityError

CONFIG_ENV = "HERMES_BRIDGE_CONFIG"
DEFAULT_CONFIG_REL = Path("hermes-bridge") / "config.yaml"
COMMAND_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
REMOTE_SHELL_RE = re.compile(r"^(/[A-Za-z0-9_./+-]+|[A-Za-z0-9_.+-]+)$")
DEFAULT_REMOTE_PATH_PREPEND = [
    "{remote_home}/.local/bin",
    "{remote_home}/bin",
    "/opt/homebrew/bin",
    "/home/linuxbrew/.linuxbrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
]
TMUX_STYLE_OPTION_NAMES = (
    "status-position",
    "status-style",
    "status-left-style",
    "status-right-style",
    "window-status-current-style",
    "message-style",
    "pane-active-border-style",
    "status-left",
    "status-right",
)
TMUX_STYLE_OPTION_SET = set(TMUX_STYLE_OPTION_NAMES)


def default_config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / DEFAULT_CONFIG_REL


def resolve_config_path(path: Optional[str] = None) -> Path:
    if path:
        return Path(path).expanduser()
    env = os.environ.get(CONFIG_ENV)
    if env:
        return Path(env).expanduser()
    return default_config_path()


def _load_yaml(path: Path) -> Dict[str, Any]:
    try:
        import yaml  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on environment
        raise ConfigError("PyYAML is required. Install with: python3 -m pip install PyYAML") from exc
    try:
        data = yaml.safe_load(path.read_text())
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except Exception as exc:
        raise ConfigError(f"failed to parse config file {path}: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"config root must be a mapping: {path}")
    return data


def deep_get(mapping: Dict[str, Any], path: Iterable[str], default: Any = None) -> Any:
    cur: Any = mapping
    for part in path:
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def validate_command_name(value: str, *, label: str) -> None:
    if not COMMAND_NAME_RE.fullmatch(value) or value in {".", ".."} or "/" in value:
        raise ConfigError(f"invalid {label}: {value!r}; use a basename-like command token")


def validate_remote_shell(value: str, *, label: str) -> None:
    if not REMOTE_SHELL_RE.fullmatch(value):
        raise ConfigError(f"invalid {label}: {value!r}; use a shell basename or absolute path without shell metacharacters")


@dataclass(frozen=True)
class AgentConfig:
    key: str
    raw: Dict[str, Any]
    defaults: Dict[str, Any]

    @property
    def command(self) -> str:
        return str(self.raw.get("command") or self.key)

    @property
    def aliases(self) -> list[str]:
        aliases = self.raw.get("aliases") or []
        if isinstance(aliases, str):
            return [aliases]
        if isinstance(aliases, list):
            return [str(a) for a in aliases]
        return []

    @property
    def display_name(self) -> str:
        return str(self.raw.get("display_name") or self.command)

    @property
    def ssh_alias(self) -> str:
        value = self.raw.get("ssh_alias")
        if not value:
            raise ConfigError(f"agents.{self.key}.ssh_alias is required")
        return str(value)

    @property
    def remote_user(self) -> str:
        return str(self.raw.get("remote_user") or "")

    @property
    def remote_home(self) -> str:
        value = self.raw.get("remote_home")
        if value:
            return str(value)
        if self.remote_user:
            return f"/Users/{self.remote_user}"
        return "~"

    @property
    def remote_shell(self) -> str:
        value = str(self.raw.get("remote_shell") or self.defaults.get("remote_shell") or "bash")
        validate_remote_shell(value, label=f"agents.{self.key}.remote_shell")
        return value

    @property
    def remote_term(self) -> str:
        return str(self.raw.get("remote_term") or self.defaults.get("remote_term") or "xterm-256color")

    def _format_remote_value(self, value: Any) -> str:
        return str(value).format(remote_user=self.remote_user, remote_home=self.remote_home)

    def _format_path_entries(self, value: Any, *, label: str) -> str:
        if isinstance(value, str):
            return self._format_remote_value(value)
        if isinstance(value, list):
            entries = [self._format_remote_value(entry) for entry in value if str(entry)]
            return ":".join(entries)
        raise ConfigError(f"{label} must be a string or list of strings")

    @property
    def remote_path(self) -> str:
        explicit = self.raw.get("remote_path")
        if explicit is None:
            explicit = self.defaults.get("remote_path")
        if explicit is not None:
            return self._format_path_entries(explicit, label=f"agents.{self.key}.remote_path")

        prepend = self.raw.get("remote_path_prepend")
        if prepend is None:
            prepend = self.defaults.get("remote_path_prepend")
        if prepend is None:
            prepend = DEFAULT_REMOTE_PATH_PREPEND
        return self._format_path_entries(prepend, label=f"agents.{self.key}.remote_path_prepend")

    @property
    def remote_hermes_cmd(self) -> str:
        value = self.raw.get("remote_hermes_cmd")
        if not value:
            raise ConfigError(f"agents.{self.key}.remote_hermes_cmd is required")
        return str(value).format(remote_user=self.remote_user, remote_home=self.remote_home)

    @property
    def docs_prefix(self) -> str:
        return str(self.raw.get("docs_prefix") or f"{self.display_name}:")

    @property
    def drive_root(self) -> str:
        return str(self.raw.get("drive_root") or "")

    def block(self, *path: str) -> Dict[str, Any]:
        value = deep_get(self.raw, path, {})
        return value if isinstance(value, dict) else {}

    def capability_enabled(self, *path: str) -> bool:
        block = self.block(*path)
        return bool(block.get("enabled", False))

    def require_capability(self, *path: str) -> Dict[str, Any]:
        block = self.block(*path)
        label = ".".join(path)
        if not block or not bool(block.get("enabled", False)):
            raise CapabilityError(f"{self.command}: capability not configured/enabled: {label}")
        return block

    def tmux_block(self) -> Dict[str, Any]:
        return self.require_capability("tmux")

    def sessions_block(self) -> Dict[str, Any]:
        return self.require_capability("sessions")

    def upload_block(self, kind: str) -> Dict[str, Any]:
        return self.require_capability("upload", kind)

    def tmux_prefix(self) -> str:
        block = self.tmux_block()
        return str(block.get("prefix") or self.command)

    def remote_tmux_cmd(self) -> str:
        block = self.tmux_block()
        value = block.get("remote_tmux_cmd") or self.raw.get("remote_tmux_cmd") or self.defaults.get("remote_tmux_cmd") or "tmux"
        return str(value).format(remote_user=self.remote_user, remote_home=self.remote_home)

    def tmux_socket_path(self) -> str:
        block = self.tmux_block()
        value = block.get("socket_path") or self.raw.get("tmux_socket_path") or self.defaults.get("tmux_socket_path") or ""
        return str(value).format(remote_user=self.remote_user, remote_home=self.remote_home)

    def tmux_geometry(self) -> tuple[int, int]:
        block = self.tmux_block()
        raw = str(block.get("geometry") or self.defaults.get("tmux_geometry") or "120x40")
        try:
            cols_s, rows_s = raw.lower().split("x", 1)
            cols, rows = int(cols_s), int(rows_s)
            if cols < 40 or rows < 10:
                raise ValueError
            return cols, rows
        except Exception as exc:
            raise ConfigError(f"invalid tmux geometry for {self.key}: {raw!r}; expected COLSxROWS") from exc

    def tmux_style_overrides(self) -> Dict[str, str]:
        block = self.tmux_block()
        raw = block.get("style") or {}
        if not raw:
            return {}
        if not isinstance(raw, dict):
            raise ConfigError(f"agents.{self.key}.tmux.style must be a mapping")
        out: Dict[str, str] = {}
        for raw_key, raw_value in raw.items():
            key = str(raw_key).replace("_", "-")
            if key not in TMUX_STYLE_OPTION_SET:
                allowed = ", ".join(TMUX_STYLE_OPTION_NAMES)
                raise ConfigError(f"unsupported tmux.style option for {self.key}: {raw_key!r}; allowed: {allowed}")
            if isinstance(raw_value, (dict, list)):
                raise ConfigError(f"agents.{self.key}.tmux.style.{key} must be a scalar string")
            out[key] = str(raw_value)
        return out


@dataclass(frozen=True)
class BridgeConfig:
    path: Path
    raw: Dict[str, Any]

    @property
    def defaults(self) -> Dict[str, Any]:
        value = self.raw.get("defaults") or {}
        return value if isinstance(value, dict) else {}

    @property
    def agents_raw(self) -> Dict[str, Dict[str, Any]]:
        agents = self.raw.get("agents") or {}
        if not isinstance(agents, dict):
            raise ConfigError("config 'agents' must be a mapping")
        out: Dict[str, Dict[str, Any]] = {}
        for key, value in agents.items():
            if not isinstance(value, dict):
                raise ConfigError(f"agents.{key} must be a mapping")
            out[str(key)] = value
        return out

    @property
    def templates_dir(self) -> Path:
        value = self.raw.get("templates_dir") or self.defaults.get("templates_dir") or "templates"
        p = Path(str(value)).expanduser()
        if not p.is_absolute():
            p = self.path.parent / p
        return p

    @property
    def local_bin_dir(self) -> Path:
        value = self.raw.get("local_bin_dir") or self.defaults.get("local_bin_dir") or "~/.local/bin"
        return Path(str(value)).expanduser()

    def agent(self, key: str) -> AgentConfig:
        agents = self.agents_raw
        if key not in agents:
            raise ConfigError(f"unknown agent: {key}")
        return AgentConfig(key=key, raw=agents[key], defaults=self.defaults)

    def agents(self) -> list[AgentConfig]:
        return [self.agent(k) for k in self.agents_raw.keys()]

    def agent_for_token(self, token: str) -> Optional[AgentConfig]:
        for agent in self.agents():
            if token in {agent.key, agent.command, *agent.aliases}:
                return agent
        return None

    def validate(self) -> list[str]:
        messages: list[str] = []
        seen_commands: dict[str, str] = {}
        if not self.agents_raw:
            raise ConfigError("at least one agent must be configured")
        for agent in self.agents():
            _ = agent.ssh_alias
            _ = agent.remote_hermes_cmd
            _ = agent.remote_shell
            _ = agent.remote_path
            cmd = agent.command
            validate_command_name(cmd, label=f"agents.{agent.key}.command")
            if cmd in seen_commands:
                raise ConfigError(f"duplicate command {cmd!r}: {seen_commands[cmd]} and {agent.key}")
            seen_commands[cmd] = agent.key
            if agent.capability_enabled("tmux"):
                _ = agent.remote_tmux_cmd()
                _ = agent.tmux_geometry()
                _ = agent.tmux_style_overrides()
            upload = agent.block("upload")
            for kind, block in upload.items():
                if isinstance(block, dict) and block.get("enabled"):
                    template = block.get("prompt_template")
                    inbox = block.get("remote_inbox")
                    if not template:
                        raise ConfigError(f"agents.{agent.key}.upload.{kind}.prompt_template is required")
                    if not inbox:
                        raise ConfigError(f"agents.{agent.key}.upload.{kind}.remote_inbox is required")
                    template_path = self.templates_dir / str(template)
                    if not template_path.exists():
                        raise ConfigError(f"upload template not found for {agent.key}.{kind}: {template_path}")
            messages.append(f"ok: {agent.key} -> {agent.command} ({agent.ssh_alias})")
        return messages


def load_config(path: Optional[str] = None) -> BridgeConfig:
    resolved = resolve_config_path(path)
    return BridgeConfig(path=resolved.resolve(), raw=_load_yaml(resolved))
