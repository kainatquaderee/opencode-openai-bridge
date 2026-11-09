#!/usr/bin/env python3
"""Deterministic smoke test for opencode-openai-bridge v0.7.0.

No network, no OpenCode quota, no subprocess spawning of the CLI. It exercises
the pure helpers that past releases got wrong:

    - module imports and FastAPI route surface
    - version string
    - real 13-model catalog and unknown-model fallback
    - prompt building (system/developer merge, content parts)
    - prompt validation (empty / NUL / oversize)
    - error-status mapping (free-limit -> 429, no-route -> 404, ...)
    - OpenAI error envelope handlers
    - build_command no longer emits --dir or --attach, and never puts the
      prompt in argv
    - real token usage math from step_finish events
    - workdir validation
    - env precedence (BRIDGE_* wins, explicit empty is respected)
    - absence of every bypass-era symbol
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import subprocess
import sys
import traceback
from pathlib import Path

from fastapi import HTTPException

ROOT = Path(__file__).parent

BYPASS_SYMBOLS = (
    "BYPASS_ENABLED", "BYPASS_STRATEGY", "BYPASS_SHELL_CMD",
    "BYPASS_MAX_RETRIES", "BYPASS_DRY_RUN", "BYPASS_WATCH_LOG",
    "_BYPASS_STATE", "_ACTIVE_PROXY_ENV",
    "_looks_like_limit_error", "_replay_result",
    "_preflight_bypass", "_record_and_rotate", "_update_proxy_env",
)

CHECKS = [
    ("model_catalog", ["CATALOG", "DEFAULT_META", "get_model_meta", "list_models"]),
    ("server", [
        "app", "_run_collect_once", "_stream_native", "_prepare_prompt",
        "_error_text", "build_prompt", "normalize_model", "resolve_model",
        "build_command", "resolve_workdir", "error_status_and_message",
        "_accumulate_tokens", "_usage_dict", "_finish_reason_from",
        "_extract_tool_call", "_extract_text", "_http_exception_handler",
        "_validation_exception_handler",
    ]),
]


def check_module(name: str, names: list[str]) -> bool:
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


def expect_http(call, status: int, label: str) -> None:
    try:
        call()
    except HTTPException as exc:
        assert exc.status_code == status, f"{label}: expected {status}, got {exc.status_code}"
        return
    raise AssertionError(f"{label}: expected HTTPException {status}, none raised")


def main() -> int:
    ok = True
    for name, names in CHECKS:
        ok = check_module(name, names) and ok

    import server
    from model_catalog import CATALOG, get_model_meta, list_models

    # --- FastAPI app + routes -------------------------------------------------
    try:
        from fastapi import FastAPI
        assert isinstance(server.app, FastAPI)
        routes = {r.path for r in server.app.routes}
        for r in ("/", "/health", "/v1/models", "/v1/models/{model_id:path}",
                  "/v1/chat/completions"):
            assert r in routes, f"missing route: {r}"
        print(f"OK    app routes ({len(routes)})")
    except Exception:
        print("FAIL  app routes")
        traceback.print_exc()
        ok = False

    # --- Version --------------------------------------------------------------
    try:
        assert server.VERSION == "0.7.0", f"expected 0.7.0, got {server.VERSION}"
        assert server.app.version == "0.7.0"
        print("OK    version (0.7.0)")
    except Exception:
        print("FAIL  version")
        traceback.print_exc()
        ok = False

    # --- Catalog --------------------------------------------------------------
    try:
        models = list_models()
        assert len(models) == len(CATALOG) == 13, f"expected 13 models, got {len(models)}"
        ids = {m["id"] for m in models}
        assert "deepseek-v4.1-flash-free" in ids
        assert "glm-5.3-flash-free" in ids
        deepseek = get_model_meta("opencode/deepseek-v4.1-flash-free")
        assert deepseek["context_window"] == 1_000_000, deepseek
        assert deepseek["max_output"] == 128_000, deepseek
        fallback = get_model_meta("opencode/some-unlisted-model")
        assert fallback["context_window"] == 200_000
        assert "function_calling" in fallback["capabilities"]
        print(f"OK    catalog ({len(models)} real models + unknown fallback)")
    except Exception:
        print("FAIL  catalog")
        traceback.print_exc()
        ok = False

    # --- Prompt building ------------------------------------------------------
    try:
        prompt = server.build_prompt([
            server.ChatMessage(role="system", content="Be terse."),
            server.ChatMessage(role="developer", content="Answer in English."),
            server.ChatMessage(role="user", content="Hi"),
            server.ChatMessage(role="assistant", content="Hello"),
            server.ChatMessage(role="tool", tool_call_id="call_1", content="result"),
            server.ChatMessage(role="user", content=[
                {"type": "text", "text": "part one"},
                {"type": "input_text", "text": "part two"},
            ]),
        ])
        assert prompt.startswith("[system]\nBe terse.\n\nAnswer in English."), prompt
        assert "[user]\nHi" in prompt
        assert "[assistant]\nHello" in prompt
        assert "[tool:call_1]\nresult" in prompt
        assert "part one\npart two" in prompt
        # A message with no usable text is skipped entirely.
        assert server.build_prompt([server.ChatMessage(role="user", content=None)]) == ""
        print("OK    build_prompt (system/developer merge, parts, tool label)")
    except Exception:
        print("FAIL  build_prompt")
        traceback.print_exc()
        ok = False

    # --- Prompt validation ----------------------------------------------------
    try:
        expect_http(
            lambda: server._prepare_prompt(
                server.ChatCompletionRequest(model="x", messages=[])
            ),
            400, "empty prompt",
        )
        expect_http(
            lambda: server._prepare_prompt(server.ChatCompletionRequest(
                model="x",
                messages=[server.ChatMessage(role="user", content="hi\x00there")],
            )),
            400, "NUL prompt",
        )
        # Temporary tiny limit so the test does not allocate 8 MB.
        old_limit = server.MAX_PROMPT_BYTES
        server.MAX_PROMPT_BYTES = 8
        try:
            expect_http(
                lambda: server._prepare_prompt(server.ChatCompletionRequest(
                    model="x",
                    messages=[server.ChatMessage(role="user", content="way too long")],
                )),
                413, "oversize prompt",
            )
        finally:
            server.MAX_PROMPT_BYTES = old_limit
        assert server._prepare_prompt(server.ChatCompletionRequest(
            model="x", messages=[server.ChatMessage(role="user", content="hi")]
        )) == "[user]\nhi"
        print("OK    prepare_prompt (empty / NUL / oversize / valid)")
    except Exception:
        print("FAIL  prepare_prompt")
        traceback.print_exc()
        ok = False

    # --- Error mapping --------------------------------------------------------
    try:
        free_limit = json.dumps({
            "type": "error",
            "error": {
                "type": "provider.free-limit",
                "message": "Free usage limit reached",
                "status": 429,
                "retryAfterMs": 59_000,
            },
        })
        status, message, etype, retry = server.error_status_and_message(free_limit)
        assert status == 429, status
        assert message == "Free usage limit reached", message
        assert etype == "provider.free-limit"
        assert retry == 59, retry

        no_route = json.dumps({
            "type": "error",
            "error": {
                "type": "provider.no-route",
                "message": "Model unavailable: opencode/nope",
            },
        })
        status, message, etype, retry = server.error_status_and_message(no_route)
        assert status == 404, status
        assert "Model unavailable" in message
        assert retry is None

        auth = json.dumps({"error": {"type": "provider.auth", "message": "Unauthorized"}})
        assert server.error_status_and_message(auth)[0] == 401

        unknown = json.dumps({"error": {"type": "provider.weird", "message": "boom"}})
        assert server.error_status_and_message(unknown)[0] == 502

        # A body nested inside response.body is still parsed.
        nested = json.dumps({
            "error": {
                "type": "provider.free-limit",
                "status": 429,
                "response": {"body": json.dumps({"error": {"type": "FreeUsageLimitError"}})},
            },
        })
        assert server.error_status_and_message(nested)[0] == 429
        # Plain text falls back to 502 with the raw text as the message.
        status, message, _, _ = server.error_status_and_message("plain failure")
        assert status == 502 and message == "plain failure"
        print("OK    error_status_and_message (429 / 404 / 401 / 502 / nested)")
    except Exception:
        print("FAIL  error_status_and_message")
        traceback.print_exc()
        ok = False

    # --- _error_text ----------------------------------------------------------
    try:
        ev = {"type": "error", "error": {"type": "provider.free-limit", "message": "x"}}
        text = server._error_text(ev)
        assert text is not None and "provider.free-limit" in text
        assert server._error_text({"type": "text", "part": {"text": "hi"}}) is None
        print("OK    _error_text")
    except Exception:
        print("FAIL  _error_text")
        traceback.print_exc()
        ok = False

    # --- OpenAI error envelope ------------------------------------------------
    try:
        response = asyncio.run(server._http_exception_handler(
            None, HTTPException(status_code=429, detail="Slow down", headers={"Retry-After": "30"})
        ))
        assert response.status_code == 429
        assert response.headers.get("retry-after") == "30"
        body = json.loads(response.body)
        assert body["error"]["type"] == "rate_limit_error"
        assert body["error"]["message"] == "Slow down"
        assert body["error"]["code"] == 429
        assert body["detail"] == "Slow down"

        class FakeValidationError(Exception):
            def errors(self):
                return [{"loc": ("body", "messages"), "msg": "Field required"}]

        response = asyncio.run(server._validation_exception_handler(
            None, FakeValidationError()
        ))
        assert response.status_code == 400
        body = json.loads(response.body)
        assert body["error"]["type"] == "invalid_request_error"
        assert "messages" in body["error"]["message"]
        print("OK    OpenAI error envelope (429 + validation 400)")
    except Exception:
        print("FAIL  error envelope")
        traceback.print_exc()
        ok = False

    # --- Model resolution -----------------------------------------------------
    try:
        assert server.resolve_model(server.ChatCompletionRequest(
            messages=[server.ChatMessage(role="user", content="x")]
        )) == server.DEFAULT_MODEL
        assert server.resolve_model(server.ChatCompletionRequest(
            model="big-pickle", messages=[server.ChatMessage(role="user", content="x")]
        )) == "opencode/big-pickle"
        assert server.resolve_model(server.ChatCompletionRequest(
            model="opencode/glm-5.3-flash-free",
            messages=[server.ChatMessage(role="user", content="x")],
        )) == "opencode/glm-5.3-flash-free"
        expect_http(lambda: server.normalize_model("   "), 400, "blank model")
        print("OK    resolve_model (default + provider prefix)")
    except Exception:
        print("FAIL  resolve_model")
        traceback.print_exc()
        ok = False

    # --- build_command: no dead flags, prompt never in argv -------------------
    try:
        # Keep the test independent of the machine's .env.
        old_server = server.OPENCODE_SERVER
        old_reasoning = server.INCLUDE_REASONING
        server.OPENCODE_SERVER = "http://127.0.0.1:4096"
        server.INCLUDE_REASONING = True
        try:
            cmd = server.build_command(server.ChatCompletionRequest(
                model="opencode/deepseek-v4.1-flash-free",
                messages=[server.ChatMessage(role="user", content="PROMPT_SENTINEL")],
                opencode_agent="build",
                opencode_session="ses_test",
            ))
        finally:
            server.OPENCODE_SERVER = old_server
            server.INCLUDE_REASONING = old_reasoning

        assert "--dir" not in cmd, cmd
        assert "--attach" not in cmd, cmd
        assert "--server" in cmd and "http://127.0.0.1:4096" in cmd, cmd
        assert "--format" in cmd and "json" in cmd, cmd
        assert "--model" in cmd and "opencode/deepseek-v4.1-flash-free" in cmd, cmd
        assert "--agent" in cmd and "build" in cmd, cmd
        assert "--session" in cmd and "ses_test" in cmd, cmd
        assert "--thinking" in cmd, cmd
        assert "PROMPT_SENTINEL" not in cmd, "prompt must travel on stdin, not argv"
        print("OK    build_command (no --dir/--attach, prompt absent from argv)")
    except Exception:
        print("FAIL  build_command")
        traceback.print_exc()
        ok = False

    # --- Workdir validation ---------------------------------------------------
    try:
        assert server.resolve_workdir(server.ChatCompletionRequest(
            messages=[server.ChatMessage(role="user", content="x")]
        )) is None
        assert server.resolve_workdir(server.ChatCompletionRequest(
            messages=[server.ChatMessage(role="user", content="x")],
            opencode_dir=str(ROOT),
        )) == str(ROOT.resolve())
        expect_http(
            lambda: server.resolve_workdir(server.ChatCompletionRequest(
                messages=[server.ChatMessage(role="user", content="x")],
                opencode_dir="/definitely/not/a/real/dir",
            )),
            400, "bad workdir",
        )
        print("OK    resolve_workdir (None / existing / rejected)")
    except Exception:
        print("FAIL  resolve_workdir")
        traceback.print_exc()
        ok = False

    # --- Event extraction + token math ---------------------------------------
    try:
        state = {"full_text": "", "text_by_part": {}}
        first = {"type": "text", "part": {"id": "p1", "text": "Hello"}}
        second = {"type": "text", "part": {"id": "p1", "text": "Hello world"}}
        assert server._extract_text(first, state) == "Hello"
        assert server._extract_text(second, state) == " world"
        assert state["full_text"] == "Hello world"

        server._accumulate_tokens({
            "type": "step_finish",
            "part": {"reason": "tool-calls", "tokens": {
                "input": 100, "output": 7, "reasoning": 3,
                "cache": {"read": 900, "write": 0},
            }},
        }, state)
        server._accumulate_tokens({
            "type": "step_finish",
            "part": {"reason": "stop", "tokens": {
                "input": 50, "output": 11, "reasoning": 2,
                "cache": {"read": 1000, "write": 0},
            }},
        }, state)
        usage = server._usage_dict("[user]\nhi", state)
        assert usage["prompt_tokens"] == 100 + 50 + 900 + 1000, usage
        assert usage["completion_tokens"] == 7 + 11 + 3 + 2, usage
        assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]
        assert usage["prompt_tokens_details"]["cached_tokens"] == 1900
        assert usage["completion_tokens_details"]["reasoning_tokens"] == 5

        assert server._finish_reason_from(
            {"type": "step_finish", "part": {"reason": "tool-calls"}}
        ) == "tool_calls"
        assert server._finish_reason_from(
            {"type": "step_finish", "part": {"reason": "stop"}}
        ) == "stop"
        assert server._finish_reason_from(
            {"type": "step_finish", "part": {"reason": "length"}}
        ) == "length"

        tool = server._extract_tool_call({
            "type": "tool_use",
            "part": {
                "id": "call_1", "tool": "read",
                "state": {"status": "completed", "input": {"path": "a.txt"},
                          "output": "hi", "title": "read"},
            },
        })
        assert tool["id"] == "call_1"
        assert tool["type"] == "function"
        assert tool["function"]["name"] == "read"
        assert json.loads(tool["function"]["arguments"]) == {"path": "a.txt"}
        print("OK    events (text delta, token totals, finish reasons, tool calls)")
    except Exception:
        print("FAIL  events")
        traceback.print_exc()
        ok = False

    # --- No bypass artifacts --------------------------------------------------
    try:
        for name in BYPASS_SYMBOLS:
            assert not hasattr(server, name), f"must not export {name}"
        source = (ROOT / "server.py").read_text(encoding="utf-8")
        assert "retry_on_429" not in source.lower()
        assert "_BYPASS" not in source
        assert "BYPASS_" not in source
        print("OK    no bypass artifacts")
    except Exception:
        print("FAIL  bypass artifact check")
        traceback.print_exc()
        ok = False

    # --- Env precedence (subprocess avoids import-cache side effects) ---------
    try:
        env = {**os.environ, "BRIDGE_PORT": "9999", "PORT": "1111"}
        result = subprocess.run(
            [sys.executable, "-c", "import server; print(server.PORT)"],
            env=env, capture_output=True, text=True, cwd=str(ROOT), timeout=30,
        )
        assert result.returncode == 0, f"subprocess failed: {result.stderr}"
        assert result.stdout.strip() == "9999", result.stdout

        # Explicit empty BRIDGE_API_KEY must beat a legacy API_KEY.
        env = {**os.environ, "BRIDGE_API_KEY": "", "API_KEY": "legacy-secret"}
        result = subprocess.run(
            [sys.executable, "-c", "import server; print(repr(server.API_KEY))"],
            env=env, capture_output=True, text=True, cwd=str(ROOT), timeout=30,
        )
        assert result.returncode == 0, f"subprocess failed: {result.stderr}"
        assert result.stdout.strip() == "''", result.stdout
        print("OK    env precedence (BRIDGE_PORT wins, explicit empty respected)")
    except Exception:
        print("FAIL  env precedence")
        traceback.print_exc()
        ok = False

    print()
    print("ALL GREEN" if ok else "SOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
