from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

from .errors import ConfigError

_PATTERN = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}")


def render_template_text(text: str, values: Mapping[str, Any]) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise ConfigError(f"template variable not supplied: {key}")
        return str(values[key])
    return _PATTERN.sub(replace, text)


def render_template_file(path: Path, values: Mapping[str, Any]) -> str:
    try:
        text = path.read_text()
    except FileNotFoundError as exc:
        raise ConfigError(f"template not found: {path}") from exc
    return render_template_text(text, values)
