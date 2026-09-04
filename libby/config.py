from __future__ import annotations

import copy
import json
import os
import pathlib
from typing import Any, Dict, List, Mapping, Optional, Union

try:
    import yaml
except Exception:
    yaml = None


PathLike = Union[str, os.PathLike]


class ConfigError(ValueError):
    """Raised when daemon configuration cannot be selected or validated."""


def _load_json(path: pathlib.Path) -> Dict[str, Any]:
    parsed = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise ConfigError(f"configuration root must be a mapping: {path}")
    return parsed


def _load_yaml(path: pathlib.Path) -> Dict[str, Any]:
    if yaml is None:
        raise RuntimeError(
            "YAML requested but PyYAML is not installed; "
            "install it with `pip install pyyaml`"
        )

    parsed = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(parsed, dict):
        raise ConfigError(f"configuration root must be a mapping: {path}")
    return parsed


def load_config(path: PathLike, *, optional: bool = False) -> Dict[str, Any]:
    """Load a JSON or YAML configuration file.

    A missing file raises ``FileNotFoundError`` unless ``optional``, in which
    case ``{}`` is returned - used by the client's optional cli_config.yaml.
    """

    config_path = pathlib.Path(path)
    if not config_path.exists():
        if optional:
            return {}
        raise FileNotFoundError(f"config file not found: {config_path}")

    extension = config_path.suffix.lower()
    if extension == ".json":
        return _load_json(config_path)
    if extension in (".yml", ".yaml"):
        return _load_yaml(config_path)

    errors: List[Exception] = []
    for loader in (_load_json, _load_yaml):
        try:
            return loader(config_path)
        except Exception as exc:
            errors.append(exc)

    raise ConfigError(
        f"could not parse config file as JSON or YAML: {config_path}"
    ) from errors[-1]


def with_env_overrides(
    config: Mapping[str, Any],
    prefix: str = "LIBBY_",
) -> Dict[str, Any]:
    """Apply top-level environment-variable overrides."""

    result = dict(config)

    def coerce(value: str) -> Any:
        stripped = value.strip()
        lowered = stripped.lower()

        if lowered in ("true", "1", "yes", "on"):
            return True
        if lowered in ("false", "0", "no", "off"):
            return False
        if "," in stripped:
            return [item.strip() for item in stripped.split(",")]

        try:
            if "." in stripped:
                return float(stripped)
            return int(stripped)
        except ValueError:
            return stripped

    for env_name, env_value in os.environ.items():
        if env_name.startswith(prefix):
            key = env_name[len(prefix):].lower()
            result[key] = coerce(env_value)

    return result


def deep_merge(
    base: Mapping[str, Any],
    override: Mapping[str, Any],
) -> Dict[str, Any]:
    """Return a recursive merge without mutating either input."""

    result: Dict[str, Any] = copy.deepcopy(dict(base))

    for key, value in override.items():
        existing = result.get(key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            result[key] = deep_merge(existing, value)
        else:
            result[key] = copy.deepcopy(value)

    return result


def is_subsystem_config(
    config: Mapping[str, Any],
    *,
    daemon_section: str = "daemons",
) -> bool:
    """Return whether a config contains a daemon collection."""

    return daemon_section in config


def list_daemons(
    config: Mapping[str, Any],
    *,
    daemon_section: str = "daemons",
) -> List[str]:
    """List daemon IDs in a subsystem config."""

    daemons = config.get(daemon_section, {})
    if not isinstance(daemons, Mapping):
        raise ConfigError(
            f"{daemon_section!r} must contain a mapping of daemon IDs"
        )
    return list(daemons)


def extract_daemon_config(
    full_config: Mapping[str, Any],
    daemon_id: str,
    *,
    daemon_section: str = "daemons",
) -> Dict[str, Any]:
    """Merge subsystem defaults with one daemon's overrides."""

    daemons = full_config.get(daemon_section, {})
    if not isinstance(daemons, Mapping):
        raise ConfigError(
            f"{daemon_section!r} must contain a mapping of daemon IDs"
        )

    if daemon_id not in daemons:
        raise ConfigError(
            f"daemon {daemon_id!r} is not defined; "
            f"available daemons: {list(daemons)}"
        )

    daemon_override = daemons[daemon_id] or {}
    if not isinstance(daemon_override, Mapping):
        raise ConfigError(
            f"configuration for daemon {daemon_id!r} must be a mapping"
        )

    defaults = {
        key: value
        for key, value in full_config.items()
        if key != daemon_section
    }
    result = deep_merge(defaults, daemon_override)
    result.setdefault("peer_id", daemon_id)
    return result


class DaemonConfigLoader:
    """Load a single-daemon or multi-daemon subsystem config."""

    def __init__(
        self,
        path: PathLike,
        *,
        daemon_section: str = "daemons",
    ) -> None:
        self.path = pathlib.Path(path)
        self.daemon_section = daemon_section
        self._config: Optional[Dict[str, Any]] = None

    @property
    def config(self) -> Dict[str, Any]:
        if self._config is None:
            self._config = load_config(self.path)
        return self._config

    @property
    def is_subsystem(self) -> bool:
        return is_subsystem_config(
            self.config,
            daemon_section=self.daemon_section,
        )

    @property
    def subsystem(self) -> Optional[str]:
        raw = self.config.get("subsystem")
        return str(raw) if raw is not None else None

    @property
    def daemon_ids(self) -> List[str]:
        if self.is_subsystem:
            return list_daemons(
                self.config,
                daemon_section=self.daemon_section,
            )

        peer_id = self.config.get("peer_id", self.path.stem)
        return [str(peer_id)]

    def get_daemon_config(
        self,
        daemon_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if self.is_subsystem:
            if daemon_id is None:
                raise ConfigError(
                    "daemon_id is required for a subsystem config; "
                    f"available daemons: {self.daemon_ids}"
                )
            return extract_daemon_config(
                self.config,
                daemon_id,
                daemon_section=self.daemon_section,
            )

        return copy.deepcopy(self.config)