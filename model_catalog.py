"""
model_catalog.py

Static metadata for the models exposed by the default `opencode` provider
(verified against `opencode models` on opencode v2.2.5), plus a generic
fallback for any other model id.

Override at runtime with BRIDGE_MODEL_CATALOG_JSON (or MODEL_CATALOG_JSON),
pointing at a JSON file shaped like:

    {
      "my-model-free": {
        "display_name": "My Model",
        "context_window": 200000,
        "max_output": 32768,
        "capabilities": ["chat", "tool", "function_calling"],
        "vision": false,
        "reasoning": true,
        "api": "chat"
      }
    }

Unknown model ids still work: they receive DEFAULT_META. That is intentional -
the bridge must never reject a model just because it is missing from this file.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

# Metadata for opencode/* free models. Limits are conservative when the exact
# upstream value is not published; the model itself remains fully usable.
CATALOG: Dict[str, Dict[str, Any]] = {
    "beam-free": {
        "display_name": "Beam Free",
        "context_window": 200_000,
        "max_output": 32_768,
        "capabilities": ["chat", "reasoning", "tool", "function_calling"],
        "vision": False,
        "reasoning": True,
        "api": "chat",
    },
    "big-pickle": {
        "display_name": "Big Pickle",
        "context_window": 200_000,
        "max_output": 32_768,
        "capabilities": ["chat", "reasoning", "tool", "function_calling"],
        "vision": False,
        "reasoning": True,
        "api": "chat",
    },
    "crouton-alpha-free": {
        "display_name": "Crouton Alpha Free",
        "context_window": 200_000,
        "max_output": 32_768,
        "capabilities": ["chat", "reasoning", "tool", "function_calling"],
        "vision": False,
        "reasoning": True,
        "api": "chat",
    },
    "deepseek-v4.1-flash-free": {
        "display_name": "DeepSeek V4.1 Flash Free",
        "context_window": 1_000_000,
        "max_output": 128_000,
        "capabilities": ["chat", "reasoning", "tool", "function_calling"],
        "vision": False,
        "reasoning": True,
        "api": "chat",
    },
    "flint-40b-free": {
        "display_name": "Flint 40B Free",
        "context_window": 200_000,
        "max_output": 32_768,
        "capabilities": ["chat", "reasoning", "tool", "function_calling"],
        "vision": False,
        "reasoning": True,
        "api": "chat",
    },
    "glm-5.3-flash-free": {
        "display_name": "GLM 5.3 Flash Free",
        "context_window": 256_000,
        "max_output": 65_536,
        "capabilities": ["chat", "reasoning", "tool", "function_calling"],
        "vision": False,
        "reasoning": True,
        "api": "chat",
    },
    "iceman-free": {
        "display_name": "Iceman Free",
        "context_window": 200_000,
        "max_output": 32_768,
        "capabilities": ["chat", "reasoning", "tool", "function_calling"],
        "vision": False,
        "reasoning": True,
        "api": "chat",
    },
    "inkling-2-small-free": {
        "display_name": "Inkling 2 Small Free",
        "context_window": 200_000,
        "max_output": 32_768,
        "capabilities": ["chat", "reasoning", "tool", "function_calling"],
        "vision": False,
        "reasoning": True,
        "api": "chat",
    },
    "longcat-2.5-free": {
        "display_name": "LongCat 2.5 Free",
        "context_window": 200_000,
        "max_output": 32_768,
        "capabilities": ["chat", "reasoning", "tool", "function_calling"],
        "vision": False,
        "reasoning": True,
        "api": "chat",
    },
    "longcat-2.5-preview-free": {
        "display_name": "LongCat 2.5 Preview Free",
        "context_window": 200_000,
        "max_output": 32_768,
        "capabilities": ["chat", "reasoning", "tool", "function_calling"],
        "vision": False,
        "reasoning": True,
        "api": "chat",
    },
    "mimo-v2.6-flash-free": {
        "display_name": "MiMo V2.6 Flash Free",
        "context_window": 200_000,
        "max_output": 32_768,
        "capabilities": ["chat", "reasoning", "tool", "function_calling", "vision"],
        "vision": True,
        "reasoning": True,
        "api": "chat",
    },
    "nemotron-3.5-lightning-free": {
        "display_name": "Nemotron 3.5 Lightning Free",
        "context_window": 262_144,
        "max_output": 262_144,
        "capabilities": ["chat", "reasoning", "tool", "function_calling"],
        "vision": False,
        "reasoning": True,
        "api": "chat",
    },
    "nemotron-3.5-super-vl-free": {
        "display_name": "Nemotron 3.5 Super VL Free",
        "context_window": 262_144,
        "max_output": 131_072,
        "capabilities": ["chat", "reasoning", "tool", "function_calling", "vision"],
        "vision": True,
        "reasoning": True,
        "api": "chat",
    },
}

DEFAULT_META: Dict[str, Any] = {
    "display_name": None,
    "context_window": 200_000,
    "max_output": 32_768,
    "capabilities": ["chat", "tool", "function_calling"],
    "vision": False,
    "reasoning": False,
    "api": "chat",
}


def _catalog_path() -> str:
    return (
        os.getenv("BRIDGE_MODEL_CATALOG_JSON", "").strip()
        or os.getenv("MODEL_CATALOG_JSON", "").strip()
    )


def _load_override() -> Optional[Dict[str, Dict[str, Any]]]:
    """Load the optional catalog override, ignoring malformed entries."""
    path = _catalog_path()
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    clean: Dict[str, Dict[str, Any]] = {}
    for key, value in data.items():
        if isinstance(key, str) and isinstance(value, dict):
            clean[key] = value
    return clean or None


def _merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(override, dict):
        override = {}
    merged = {**base, **override}

    capabilities = merged.get("capabilities")
    if not isinstance(capabilities, list):
        capabilities = list(base.get("capabilities") or [])
    else:
        capabilities = [str(c) for c in capabilities]
    if merged.get("vision") and "vision" not in capabilities:
        capabilities.append("vision")

    merged["capabilities"] = capabilities
    try:
        merged["context_window"] = int(merged.get("context_window", base["context_window"]))
    except (TypeError, ValueError):
        merged["context_window"] = int(base["context_window"])
    try:
        merged["max_output"] = int(merged.get("max_output", base["max_output"]))
    except (TypeError, ValueError):
        merged["max_output"] = int(base["max_output"])
    return merged


def _merged_catalog() -> Dict[str, Dict[str, Any]]:
    override = _load_override()
    return {**CATALOG, **override} if override else dict(CATALOG)


def get_model_meta(model_id: str) -> Dict[str, Any]:
    bare = model_id.split("/")[-1] if "/" in model_id else model_id
    catalog = _merged_catalog()
    entry = catalog.get(bare) or catalog.get(model_id) or {}
    return _merge(DEFAULT_META, entry)


def list_models() -> List[Dict[str, Any]]:
    catalog = _merged_catalog()
    out: List[Dict[str, Any]] = []
    for model_id, entry in catalog.items():
        meta = _merge(DEFAULT_META, entry)
        meta["id"] = model_id
        out.append(meta)
    return out
