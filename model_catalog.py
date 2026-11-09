"""
model_catalog.py
Static catalog of OpenCode Zen free models with limits and capabilities.
Override at runtime via BRIDGE_MODEL_CATALOG_JSON or MODEL_CATALOG_JSON.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

CATALOG: Dict[str, Dict[str, Any]] = {
    "x-preview-f-free": {
        "display_name": "Ox Alpha Free",
        "context_window": 1_000_000,
        "max_output": 131_072,
        "capabilities": ["chat", "reasoning", "tool", "function_calling"],
        "vision": False,
        "reasoning": True,
        "api": "chat",
    },
    "muse-spark-1.2-contributor-free": {
        "display_name": "Muse Spark 1.2 Free",
        "context_window": 1_000_000,
        "max_output": 131_072,
        "capabilities": ["chat", "reasoning", "tool", "function_calling"],
        "vision": False,
        "reasoning": True,
        "api": "responses",
    },
    "mimo-v2.5-free": {
        "display_name": "MiMo V2.5 Free",
        "context_window": 200_000,
        "max_output": 32_768,
        "capabilities": ["chat", "reasoning", "tool", "function_calling", "vision"],
        "vision": True,
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
    "hy3-free": {
        "display_name": "Hy3 Free",
        "context_window": 190_000,
        "max_output": 65_536,
        "capabilities": ["chat", "reasoning", "tool", "function_calling"],
        "vision": False,
        "reasoning": True,
        "api": "chat",
    },
    "nemotron-3-ultra-free": {
        "display_name": "Nemotron 3 Ultra Free",
        "context_window": 1_000_000,
        "max_output": 131_072,
        "capabilities": ["chat", "reasoning", "tool", "function_calling"],
        "vision": False,
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
    "big-pickle": {
        "display_name": "Big Pickle",
        "context_window": 200_000,
        "max_output": 32_768,
        "capabilities": ["chat", "reasoning", "tool", "function_calling"],
        "vision": False,
        "reasoning": True,
        "api": "chat",
    },
    "laguna-s-2.1-free": {
        "display_name": "Laguna S 2.1 Free",
        "context_window": 262_144,
        "max_output": 32_768,
        "capabilities": ["chat", "reasoning", "tool", "function_calling"],
        "vision": False,
        "reasoning": True,
        "api": "chat",
    },
    "space-bunny-free": {
        "display_name": "Space Bunny Free",
        "context_window": 200_000,
        "max_output": 65_536,
        "capabilities": ["chat", "reasoning", "tool", "function_calling", "vision"],
        "vision": True,
        "reasoning": True,
        "api": "chat",
    },
    "kimi-k2.5-free": {
        "display_name": "Kimi K2.5 Free",
        "context_window": 256_000,
        "max_output": 65_536,
        "capabilities": ["chat", "reasoning", "tool", "function_calling", "vision"],
        "vision": True,
        "reasoning": True,
        "api": "chat",
    },
    "minimax-m3-free": {
        "display_name": "MiniMax M3 Free",
        "context_window": 200_000,
        "max_output": 65_536,
        "capabilities": ["chat", "reasoning", "tool", "function_calling", "vision"],
        "vision": True,
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
    "glm-5.3-flash-free": {
        "display_name": "GLM 5.3 Flash Free",
        "context_window": 256_000,
        "max_output": 65_536,
        "capabilities": ["chat", "reasoning", "tool", "function_calling"],
        "vision": False,
        "reasoning": True,
        "api": "chat",
    },
    "minimax-m2.5-free": {
        "display_name": "MiniMax M2.5 Free",
        "context_window": 200_000,
        "max_output": 32_768,
        "capabilities": ["chat", "reasoning", "tool", "function_calling"],
        "vision": False,
        "reasoning": True,
        "api": "chat",
    },
    "mimo-v2-pro-free": {
        "display_name": "MiMo V2 Pro Free",
        "context_window": 200_000,
        "max_output": 32_768,
        "capabilities": ["chat", "reasoning", "tool", "function_calling"],
        "vision": False,
        "reasoning": True,
        "api": "chat",
    },
    "nemotron-3-super-free": {
        "display_name": "Nemotron 3 Super Free",
        "context_window": 262_144,
        "max_output": 262_144,
        "capabilities": ["chat", "reasoning", "tool", "function_calling"],
        "vision": False,
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
    path = _catalog_path()
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return None

def _merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = {**base, **override}
    caps = list(merged.get("capabilities") or [])
    if merged.get("vision") and "vision" not in caps:
        caps.append("vision")
    merged["capabilities"] = caps
    return merged

def get_model_meta(model_id: str) -> Dict[str, Any]:
    bare = model_id.split("/")[-1] if "/" in model_id else model_id
    override = _load_override()
    catalog = {**CATALOG, **override} if override else CATALOG
    entry = catalog.get(bare) or catalog.get(model_id) or {}
    return _merge(DEFAULT_META, entry)

def list_models() -> List[Dict[str, Any]]:
    override = _load_override()
    catalog = {**CATALOG, **override} if override else CATALOG
    out: List[Dict[str, Any]] = []
    for model_id, entry in catalog.items():
        meta = _merge(DEFAULT_META, entry)
        meta = {**meta, "id": model_id}
        out.append(meta)
    return out
