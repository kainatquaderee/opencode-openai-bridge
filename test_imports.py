#!/usr/bin/env python3
"""Smoke test for opencode-openai-bridge v0.6.0.

Covers:
    - module imports
    - FastAPI app + routes
    - version string
    - model catalog
    - prompt building (roles, content flattening)
    - _prepare_prompt validation (empty / NUL / valid)
    - _error_text extraction from NDJSON error events
    - error_message propagation present in _run_collect_once source
    - no bypass artifacts (BYPASS_*, _replay_result, _looks_like_limit_error)
    - env precedence (BRIDGE_PORT beats PORT) via subprocess
"""

import importlib
import inspect
import subprocess
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).parent

CHECKS = [
    ("model_catalog", ["CATALOG", "DEFAULT_META", "get_model_meta", "list_models"]),
    ("server", [
        "app", "_run_collect_once", "_stream_native", "_prepare_prompt",
        "_error_text", "build_prompt", "normalize_model",
    ]),
]

def check_module(name, names):
    try:
        mod = importlib.import_module(name)
    except Exception:
        print(f"FAIL  {name}: import failed")
        traceback.print_exc()
        return False
    missing = [n for n in names if not hasattr(mod, n)]
    if missing:
        print(f"FAIL  {name}: missing {missing}")
        return False
    print(f"OK    {name}")
    return True

def main():
    ok = True
    for name, names in CHECKS:
        ok = check_module(name, names) and ok

    # FastAPI app + routes
    try:
        from server import app
        from fastapi import FastAPI
        assert isinstance(app, FastAPI), f"server.app is not FastAPI, got {type(app)}"
        routes = {r.path for r in app.routes}
        for r in ("/health", "/v1/models", "/v1/chat/completions"):
            assert r in routes, f"missing route: {r}"
        print(f"OK    server.app (FastAPI, {len(routes)} routes)")
    except Exception:
        print("FAIL  server.app smoke")
        traceback.print_exc()
        ok = False

    # Version
    try:
        import server
        assert server.app.version == "0.6.0", f"expected 0.6.0, got {server.app.version}"
        print("OK    version (0.6.0)")
    except Exception:
        print("FAIL  version")
        traceback.print_exc()
        ok = False

    # Catalog
    try:
        from model_catalog import list_models, get_model_meta
        models = list_models()
        assert models, "empty catalog"
        assert len(models) == 18, f"expected 18 models, got {len(models)}"
        m = get_model_meta("opencode/deepseek-v4.1-flash-free")
        assert m["context_window"] == 1_000_000
        print(f"OK    catalog ({len(models)} models)")
    except Exception:
        print("FAIL  catalog smoke")
        traceback.print_exc()
        ok = False

    # Prompt building
    try:
        import server
        prompt = server.build_prompt([
            server.ChatMessage(role="system", content="Be terse."),
            server.ChatMessage(role="user", content="Hi"),
            server.ChatMessage(role="assistant", content="Hello"),
            server.ChatMessage(role="user", content="Again"),
        ])
        assert "[system]\nBe terse." in prompt
        assert "[user]\nHi" in prompt
        assert "[assistant]\nHello" in prompt
        assert "[user]\nAgain" in prompt
        print("OK    build_prompt (roles)")
    except Exception:
        print("FAIL  build_prompt")
        traceback.print_exc()
        ok = False

    # _prepare_prompt validation
    try:
        import server
        from fastapi import HTTPException
        # Empty
        try:
            server._prepare_prompt(server.ChatCompletionRequest(model="x", messages=[]))
            raise AssertionError("empty prompt should raise 400")
        except HTTPException as e:
            assert e.status_code == 400, f"expected 400, got {e.status_code}"
        # NUL
        try:
            server._prepare_prompt(server.ChatCompletionRequest(
                model="x", messages=[server.ChatMessage(role="user", content="hi\x00there")]
            ))
            raise AssertionError("NUL prompt should raise 400")
        except HTTPException as e:
            assert e.status_code == 400, f"expected 400, got {e.status_code}"
        # Valid
        p = server._prepare_prompt(server.ChatCompletionRequest(
            model="x", messages=[server.ChatMessage(role="user", content="hi")]
        ))
        assert p == "[user]\nhi", f"unexpected prompt: {p!r}"
        print("OK    _prepare_prompt (empty / NUL / valid)")
    except Exception:
        print("FAIL  _prepare_prompt")
        traceback.print_exc()
        ok = False

    # _error_text extracts from NDJSON error events
    try:
        import server
        ev = {"type": "error", "error": {"type": "provider.free-limit", "message": "x"}}
        text = server._error_text(ev)
        assert text is not None and "provider.free-limit" in text
        assert server._error_text({"type": "text", "part": {"text": "hi"}}) is None
        print("OK    _error_text (NDJSON error extraction)")
    except Exception:
        print("FAIL  _error_text")
        traceback.print_exc()
        ok = False

    # error_message propagation present in _run_collect_once source
    try:
        import server
        src = inspect.getsource(server._run_collect_once)
        assert 'f"opencode error: {error_message}"' in src, \
            "v0.6.0 fix missing: _run_collect_once should prefer error_message"
        print("OK    v0.6.0 fix (error_message propagation in _run_collect_once)")
    except Exception:
        print("FAIL  v0.6.0 fix assertion")
        traceback.print_exc()
        ok = False

    # No bypass artifacts
    try:
        import server
        for name in (
            "BYPASS_ENABLED", "BYPASS_STRATEGY", "BYPASS_SHELL_CMD",
            "BYPASS_MAX_RETRIES", "BYPASS_DRY_RUN", "BYPASS_WATCH_LOG",
            "_BYPASS_STATE", "_ACTIVE_PROXY_ENV",
            "_looks_like_limit_error", "_replay_result",
            "_preflight_bypass", "_record_and_rotate", "_update_proxy_env",
        ):
            assert not hasattr(server, name), f"v0.6.0 must not export {name}"
        print("OK    no bypass artifacts")
    except Exception:
        print("FAIL  bypass artifact check")
        traceback.print_exc()
        ok = False

    # Env precedence via subprocess (avoids in-process reload side effects)
    try:
        import os
        env = {**os.environ, "BRIDGE_PORT": "9999", "PORT": "1111"}
        result = subprocess.run(
            [sys.executable, "-c", "import server; print(server.PORT)"],
            env=env, capture_output=True, text=True, cwd=str(ROOT), timeout=30,
        )
        assert result.returncode == 0, f"subprocess failed: {result.stderr}"
        got = result.stdout.strip()
        assert got == "9999", f"BRIDGE_PORT should win, got {got!r}"
        print("OK    env (BRIDGE_PORT beats PORT)")
    except Exception:
        print("FAIL  env precedence")
        traceback.print_exc()
        ok = False

    print()
    print("ALL GREEN" if ok else "SOME CHECKS FAILED")
    return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main())
