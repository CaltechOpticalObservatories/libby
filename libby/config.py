from __future__ import annotations
from typing import Any, Dict, Mapping
import json, os, pathlib

try:
    import yaml
except Exception:
    yaml = None

def _load_json(p: pathlib.Path) -> Dict[str, Any]:
    return json.loads(p.read_text())

def _load_yaml(p: pathlib.Path) -> Dict[str, Any]:
    if yaml is None:
        raise RuntimeError("YAML requested but PyYAML not installed. `pip install pyyaml`")
    return yaml.safe_load(p.read_text()) or {}

def load_config(path: str | os.PathLike[str], *, optional: bool = False) -> Dict[str, Any]:
    """Load a JSON/YAML config file into a dict.

    The single file→dict loader for libby; daemon and client config readers wrap
    this with their own semantics. A missing file raises ``FileNotFoundError``
    unless ``optional``, in which case ``{}`` is returned. The extension picks
    the parser (.json vs .yml/.yaml); an unknown extension auto-detects JSON→YAML.
    """
    p = pathlib.Path(path)
    if not p.exists():
        if optional:
            return {}
        raise FileNotFoundError(f"Config file not found: {p}")
    data = _parse(p)
    if not isinstance(data, dict):
        raise ValueError(
            f"Config file {p} did not parse to a dict, got {type(data).__name__}"
        )
    return data


def _parse(p: pathlib.Path) -> Any:
    ext = p.suffix.lower()
    if ext == ".json":
        return _load_json(p) or {}
    if ext in (".yml", ".yaml"):
        return _load_yaml(p) or {}
    for parser in (_load_json, _load_yaml):
        try:
            return parser(p) or {}
        except Exception:
            pass
    raise ValueError(f"Could not parse config file as JSON or YAML: {p}")

def with_env_overrides(cfg: Mapping[str, Any], prefix: str = "LIBBY_") -> Dict[str, Any]:
    """
    Uppercase, underscore keys: LIBBY_PEER_ID, LIBBY_BIND, etc.
    Booleans: '1','true','yes' => True ; '0','false','no' => False
    Lists: comma-separated.
    """
    out: Dict[str, Any] = dict(cfg)

    def coerce(v: str) -> Any:
        s = v.strip()
        ls = s.lower()
        if ls in ("true","1","yes","on"): return True
        if ls in ("false","0","no","off"): return False
        if "," in s: return [x.strip() for x in s.split(",")]
        try:
            if "." in s: return float(s)
            return int(s)
        except Exception:
            return s

    for k, v in os.environ.items():
        if not k.startswith(prefix): 
            continue
        key = k[len(prefix):].lower()
        out[key] = coerce(v)
    return out
