"""Configuration loader with YAML merging and env var interpolation.

Merge order: base.yaml <- channels.yaml <- environment variables
Env vars in YAML: ${VAR_NAME} or ${VAR_NAME:default_value}
"""

from __future__ import annotations

import os
import re
from copy import deepcopy
from pathlib import Path

import yaml

from src.config.schema import AppConfig
from src.core.exceptions import ConfigError

_ENV_VAR_PATTERN = re.compile(r"\$\{(\w+)(?::([^}]*))?\}")


def load_config(config_dir: Path | None = None) -> AppConfig:
    """Load and validate configuration.

    Merges base.yaml with channels.yaml, resolves env vars, and validates.
    """
    if config_dir is None:
        config_dir = Path("config")

    base_path = config_dir / "base.yaml"
    channels_path = config_dir / "channels.yaml"

    if not base_path.exists():
        raise ConfigError(f"Base config not found: {base_path}")

    base = _load_yaml(base_path)

    # Merge channels config if it exists
    if channels_path.exists():
        channels_data = _load_yaml(channels_path)
        if "channels" in channels_data:
            base["channels"] = channels_data["channels"]

    # Merge overlay config if CONFIG_OVERLAY env var is set
    overlay_name = os.environ.get("CONFIG_OVERLAY", "")
    if overlay_name:
        overlay_path = config_dir / f"{overlay_name}.yaml"
        if overlay_path.exists():
            overlay_data = _load_yaml(overlay_path)
            _deep_merge(base, overlay_data)

    resolved = _resolve_env_vars(base)

    try:
        return AppConfig(**resolved)
    except Exception as e:
        raise ConfigError(f"Config validation failed: {e}") from e


def _deep_merge(base: dict, overlay: dict) -> None:
    """Recursively merge overlay into base. Overlay values win.

    For dicts: recurse. For everything else (lists, scalars): replace.
    """
    for key, value in overlay.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def _load_yaml(path: Path) -> dict:
    """Load a YAML file and return as dict."""
    with open(path) as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


def _resolve_env_vars(data: dict | list | str | int | float | bool | None) -> any:
    """Recursively replace ${VAR} and ${VAR:default} with env var values."""
    if isinstance(data, dict):
        return {k: _resolve_env_vars(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [_resolve_env_vars(item) for item in data]
    elif isinstance(data, str):
        return _ENV_VAR_PATTERN.sub(_env_replacer, data)
    return data


def _env_replacer(match: re.Match) -> str:
    """Replace a single ${VAR:default} match."""
    var_name = match.group(1)
    default = match.group(2)
    value = os.environ.get(var_name)
    if value is not None:
        return value
    if default is not None:
        return default
    return match.group(0)
