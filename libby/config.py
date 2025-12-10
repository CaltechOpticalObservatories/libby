from __future__ import annotations
from typing import Any, Dict, Mapping
import json, os, pathlib

try:
    import yaml
except Exception:
    yaml = None

def _load_json_section(path: pathlib.Path, section: str) -> Dict[str, Any]:
    cfg = json.loads(path.read_text()) or {}
    return cfg.get(section, {})

def _load_yaml_section(path: pathlib.Path, section: str) -> dict:
    cfg = yaml.safe_load(path.read_text()) or {}
    return cfg.get(section, {})

def load_config(path: str | os.PathLike[str], section: str) -> Dict[str, Any]:
    """
    Load config from .json or .yml/.yaml.
    If the extension is missing/unknown, attempt JSON → YAML.
    """
    p = pathlib.Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {p}")
    ext = p.suffix.lower()
    if ext == ".json":
        return _load_json_section(p, section) or {}
    if ext in (".yml", ".yaml"):
        return _load_yaml_section(p, section) or {}
    # Auto-detect
    for fn in (_load_json_section, _load_yaml_section):
        try:
            return fn(p, section) or {}
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
