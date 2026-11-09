#!/usr/bin/env python3
"""
opencode-openai-bridge (v0.7.0)

OpenAI-compatible HTTP server that shells out to the real `opencode` CLI and
translates its NDJSON event stream into Chat Completions responses.

This is the **bridge-only** release. All rate-limit bypass functionality
(VPN/proxy/Tor egress rotation, retry-on-429, block ledgers, the former
`unlimited/` package) has been removed. If OpenCode returns a free-tier
limit error, the bridge surfaces it to the caller. See README "Ethical stance".

Env var namespace: BRIDGE_* is preferred to avoid collisions with other
tools that hijack PORT / HOST / API_KEY (e.g. OmniRoute exports PORT=20128).

  BRIDGE_PORT / BRIDGE_HOST / BRIDGE_API_KEY   <- preferred (namespaced)
  PORT / HOST / API_KEY                        <- legacy fallbacks

v0.7.0 highlights (verified against opencode v2.2.5)
---------------------------------------------------
- The prompt is written to the child's **stdin**, never argv. This removes the
  Linux MAX_ARG_STRLEN (~128 KiB) failure mode for long conversations.
- `--dir` was removed (opencode v2 has no such flag); the working directory is
  passed to the subprocess via `cwd=`, which is the correct mechanism.
- `--attach` was replaced by `--server` (the v2 flag). `BRIDGE_OPENCODE_ATTACH`
  is still accepted as a legacy alias.
- Real token usage is read from `step_finish.part.tokens` instead of being
  estimated from character counts.
- Upstream error events are mapped to correct HTTP status codes
  (`provider.free-limit` -> 429, `provider.no-route` -> 404, etc.) and
  human-readable messages, with `Retry-After` when the provider supplies it.
- Errors are emitted in the OpenAI `{"error": {...}}` envelope while still
  keeping a top-level `detail` field for older clients.
- Streaming keeps true per-event output on success; on failure it emits a
  valid `chat.completion.chunk` carrying the error text, a finish chunk, and
  `[DONE]`. No bare `{"error": ...}` SSE events.
- Generic exceptions and process-spawn failures are caught, so a stream can
  never die silently mid-flight.
- `stdin` is a pipe now (it carries the prompt) and is always closed; no TTY
  stall is possible.

Run:
    python server.py
    uvicorn server:app --host 0.0.0.0 --port 4097
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

# Optional .env loading so `python server.py` picks up .env without shell magic
try:
    from dotenv import load_dotenv

    _dotenv_path = Path(__file__).parent / ".env"
    if _dotenv_path.exists():
        load_dotenv(_dotenv_path)
except ImportError:  # pragma: no cover - python-dotenv is a normal dependency
    pass

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from model_catalog import get_model_meta, list_models

VERSION = "0.7.0"

logger = logging.getLogger("opencode_bridge")

# ---------------------------------------------------------------------------
# Config (namespaced preferred; legacy fallback so old .env keeps working)
# ---------------------------------------------------------------------------
def _env(name: str, default: str = "") -> str:
    """Return BRIDGE_<name> if it is set (even empty), else <name>, else default."""
    value = os.getenv(f"BRIDGE_{name}")
    if value is not None:
        return value
    value = os.getenv(name)
    if value is not None:
        return value
    return default


def _env_bool(name: str, default: bool = False) -> bool:
    return _env(name, "true" if default else "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)).strip())
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)).strip())
    except (TypeError, ValueError):
        return default


OPENCODE_BIN = _env("OPENCODE_BIN", "opencode").strip() or "opencode"
# v2 uses --server. Keep the old ATTACH name as a documented alias.
OPENCODE_SERVER = (
    _env("OPENCODE_SERVER").strip() or _env("OPENCODE_ATTACH").strip()
)
OPENCODE_AUTO = _env_bool("OPENCODE_AUTO", True)
OPENCODE_WORKDIR = _env("OPENCODE_WORKDIR").strip()
OPENCODE_TIMEOUT = max(1.0, _env_float("OPENCODE_TIMEOUT", 300.0))
DEFAULT_MODEL = _env("DEFAULT_MODEL", "opencode/deepseek-v4.1-flash-free").strip()
API_KEY = _env("API_KEY").strip()
INCLUDE_REASONING = _env_bool("INCLUDE_REASONING", False)
INCLUDE_TOOL_CALLS = _env_bool("INCLUDE_TOOL_CALLS", False)
HOST = _env("HOST", "0.0.0.0").strip() or "0.0.0.0"
PORT = _env_int("PORT", 4097)

_MODELS_OVERRIDE = [m.strip() for m in _env("MODELS").split(",") if m.strip()]

# The prompt travels over stdin, so argv length is irrelevant. Keep a generous
# cap purely to protect the bridge from unbounded memory use.
MAX_PROMPT_BYTES = 8_000_000

# stdout line buffer for the NDJSON pump
STREAM_LIMIT = 10 * 1024 * 1024

_STARTED_AT = time.time()
_OPENCODE_VERSION: Optional[str] = None

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class OpencodeError(RuntimeError):
    """A failure that should become a structured HTTP error."""

    status = 502

    def __init__(self, message: str, *, status: Optional[int] = None) -> None:
        super().__init__(message)
        if status is not None:
            self.status = status


class OpencodeUnavailable(OpencodeError):
    """The binary could not be found or spawned."""

    status = 503


# ---------------------------------------------------------------------------
# Pydantic models - extra fields ignored so unknown OpenAI parameters
# (response_format, parallel_tool_calls, logprobs, etc.) do not 422.
# ---------------------------------------------------------------------------
class ChatMessage(BaseModel):
    model_config = {"extra": "ignore"}

    role: str
    content: Any = None
    name: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    model_config = {"extra": "ignore"}

    model: Optional[str] = None
    messages: List[ChatMessage]
    stream: bool = False
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    max_completion_tokens: Optional[int] = None
    stop: Optional[Any] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None
    user: Optional[str] = None
    opencode_session: Optional[str] = None
    opencode_agent: Optional[str] = None
    opencode_dir: Optional[str] = None


class ModelLimits(BaseModel):
    context_window: int
    max_output: int


class ModelMetadata(BaseModel):
    display_name: Optional[str] = None
    capabilities: List[str] = Field(default_factory=list)
    limits: ModelLimits
    vision: bool = False
    reasoning: bool = False
    api: str = "chat"


class ModelCard(BaseModel):
    id: str
    object: str = "model"
    created: int = Field(default_factory=lambda: int(time.time()))
    owned_by: str = "opencode"
    metadata: ModelMetadata


class ModelList(BaseModel):
    object: str = "list"
    data: List[ModelCard]


# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------
def _flatten_content(content: Any) -> str:
    """Flatten OpenAI message content (string, parts list, or scalar) to text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") in ("text", "input_text") and "text" in part:
                    parts.append(str(part["text"]))
                elif "text" in part:
                    parts.append(str(part["text"]))
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    if isinstance(content, (dict,)):
        return str(content.get("text", content))
    return str(content)


def normalize_model(model: str) -> str:
    model = (model or "").strip()
    if not model:
        raise HTTPException(status_code=400, detail="model is required")
    return model if "/" in model else f"opencode/{model}"


def resolve_model(req: ChatCompletionRequest) -> str:
    return normalize_model(req.model or DEFAULT_MODEL)


def build_prompt(messages: List[ChatMessage]) -> str:
    system_lines: List[str] = []
    convo: List[str] = []
    for msg in messages:
        text = _flatten_content(msg.content).strip()
        if not text:
            continue
        role = (msg.role or "user").strip().lower()
        if role in ("system", "developer"):
            system_lines.append(text)
        elif role == "assistant":
            convo.append(f"[assistant]\n{text}")
        elif role == "tool":
            convo.append(f"[tool:{msg.tool_call_id or 'result'}]\n{text}")
        else:
            convo.append(f"[user]\n{text}")
    parts: List[str] = []
    if system_lines:
        parts.append("[system]\n" + "\n\n".join(system_lines))
    parts.extend(convo)
    return "\n\n".join(parts).strip()


def prepare_prompt(req: ChatCompletionRequest) -> str:
    """Build and validate the prompt. Raises HTTPException for bad input."""
    prompt = build_prompt(req.messages)
    if not prompt:
        raise HTTPException(status_code=400, detail="No usable text in messages")
    if "\x00" in prompt:
        raise HTTPException(status_code=400, detail="Prompt contains NUL bytes")
    encoded_len = len(prompt.encode("utf-8"))
    if encoded_len > MAX_PROMPT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Prompt is {encoded_len} bytes; limit is {MAX_PROMPT_BYTES}. "
                "Shorten the conversation or raise the limit in server.py."
            ),
        )
    return prompt


# Backwards-compatible private alias used by older smoke tests.
_prepare_prompt = prepare_prompt


def resolve_workdir(req: ChatCompletionRequest) -> Optional[str]:
    """Resolve and validate the working directory passed to the child process."""
    workdir = (req.opencode_dir or OPENCODE_WORKDIR or "").strip()
    if not workdir:
        return None
    if not os.path.isdir(workdir):
        raise HTTPException(
            status_code=400,
            detail=f"opencode_dir is not a directory: {workdir!r}",
        )
    return os.path.abspath(workdir)


def build_command(req: ChatCompletionRequest) -> List[str]:
    """Build the `opencode run` argv. The prompt is NOT part of argv."""
    cmd = [
        OPENCODE_BIN,
        "run",
        "--format", "json",
        "--model", resolve_model(req),
    ]
    if OPENCODE_AUTO:
        cmd.append("--auto")
    if OPENCODE_SERVER:
        # opencode v2 flag. v0.6.x used `--attach`, which no longer exists.
        cmd.extend(["--server", OPENCODE_SERVER])
    if req.opencode_agent:
        cmd.extend(["--agent", req.opencode_agent])
    if req.opencode_session:
        cmd.extend(["--session", req.opencode_session])
    if INCLUDE_REASONING:
        cmd.append("--thinking")
    return cmd


def _sse(obj: Dict[str, Any]) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


def _binary_available() -> bool:
    return shutil.which(OPENCODE_BIN) is not None


def _auth_ok(request: Request) -> bool:
    if not API_KEY:
        return True
    auth = request.headers.get("authorization", "")
    supplied = ""
    if auth.lower().startswith("bearer "):
        supplied = auth[7:].strip()
    if not supplied:
        supplied = request.headers.get("x-api-key", "").strip()
    if not supplied:
        return False
    try:
        return secrets.compare_digest(supplied, API_KEY)
    except TypeError:  # pragma: no cover - defensive
        return False


# ---------------------------------------------------------------------------
# Upstream error parsing / status mapping
# ---------------------------------------------------------------------------
def _parse_json_object(raw: Optional[str]) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _iter_error_dicts(data: Dict[str, Any]):
    yield data
    inner = data.get("error")
    if isinstance(inner, dict):
        yield inner
    response = data.get("response")
    if isinstance(response, dict):
        body = response.get("body")
        if isinstance(body, str):
            parsed = _parse_json_object(body)
            if isinstance(parsed, dict):
                yield parsed
                body_err = parsed.get("error")
                if isinstance(body_err, dict):
                    yield body_err


def _status_for_error_type(error_type: Optional[str]) -> int:
    t = (error_type or "").lower()
    if not t:
        return 502
    if any(k in t for k in ("free-limit", "freelimit", "rate", "quota", "too_many")):
        return 429
    if any(k in t for k in ("auth", "unauthor", "permission", "forbidden")):
        return 401
    if any(k in t for k in ("no-route", "not-found", "notfound", "unavailable")):
        return 404
    return 502


def error_status_and_message(
    raw: Optional[str],
) -> Tuple[int, str, Optional[str], Optional[int]]:
    """Return (status, human_message, error_type, retry_after_seconds)."""
    data = _parse_json_object(raw)
    status: Optional[int] = None
    message: Optional[str] = None
    error_type: Optional[str] = None
    retry_after: Optional[int] = None

    if isinstance(data, dict):
        for d in _iter_error_dicts(data):
            candidate_status = d.get("status")
            if (
                status is None
                and isinstance(candidate_status, int)
                and 400 <= candidate_status <= 599
            ):
                status = candidate_status

            candidate_type = d.get("type")
            if (
                error_type is None
                and isinstance(candidate_type, str)
                and candidate_type
                # The outer NDJSON envelope carries type="error"; the real
                # provider type lives on the nested error object.
                and candidate_type not in (
                    "error", "text", "reasoning", "tool_use", "step_start", "step_finish",
                )
            ):
                error_type = candidate_type

            candidate_message = d.get("message")
            if message is None and isinstance(candidate_message, str) and candidate_message.strip():
                message = candidate_message.strip()

            candidate_retry = d.get("retryAfterMs")
            if retry_after is None and isinstance(candidate_retry, (int, float)) and candidate_retry > 0:
                retry_after = max(1, int(candidate_retry / 1000))

    if status is None:
        status = _status_for_error_type(error_type)
    if message is None:
        message = (raw or "").strip() or "opencode error"
    return status, message, error_type, retry_after


def _retry_headers(retry_after: Optional[int]) -> Optional[Dict[str, str]]:
    if not retry_after:
        return None
    return {"Retry-After": str(retry_after)}


# ---------------------------------------------------------------------------
# NDJSON event pump
# ---------------------------------------------------------------------------
async def _iter_opencode_events(
    cmd: List[str],
    prompt: str,
    timeout: float,
    cwd: Optional[str] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """Spawn `opencode run`, feed the prompt on stdin, and yield NDJSON events."""
    if not _binary_available():
        raise OpencodeUnavailable(
            f"opencode binary not found: {OPENCODE_BIN!r}. "
            "Install it or set BRIDGE_OPENCODE_BIN."
        )

    env = {**os.environ, "NO_COLOR": "1", "FORCE_COLOR": "0"}

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd,
            limit=STREAM_LIMIT,
        )
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError) as exc:
        raise OpencodeUnavailable(f"failed to start opencode: {exc}") from exc

    stderr_chunks: List[bytes] = []

    async def drain() -> None:
        assert proc.stderr is not None
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            stderr_chunks.append(chunk)

    stderr_task = asyncio.create_task(drain())

    # Feed the prompt on stdin. This is the only safe way to pass arbitrarily
    # large prompts: Linux caps a single argv element at MAX_ARG_STRLEN (128 KiB).
    try:
        assert proc.stdin is not None
        proc.stdin.write(prompt.encode("utf-8"))
        await proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        # The child exited before reading everything; the exit code/stderr below
        # will carry the real explanation.
        pass
    finally:
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:  # pragma: no cover - defensive
            pass

    deadline = time.monotonic() + timeout
    timed_out = False

    try:
        assert proc.stdout is not None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                raise TimeoutError(f"opencode run timed out after {timeout:g}s")
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
            except asyncio.TimeoutError:
                timed_out = True
                raise TimeoutError(f"opencode run timed out after {timeout:g}s")
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                # opencode occasionally prints non-JSON diagnostics on stdout.
                logger.debug("ignoring non-JSON stdout line: %s", text[:200])
                continue
            if isinstance(event, dict):
                yield event
    finally:
        # Always reap the child. On early close (client disconnect) or timeout the
        # process is killed so no orphan `opencode` survives the request.
        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=0.1 if timed_out else 2.0)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
        try:
            await asyncio.wait_for(stderr_task, timeout=2.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            stderr_task.cancel()
            try:
                await stderr_task
            except BaseException:
                pass

    if proc.returncode not in (0, None):
        err = b"".join(stderr_chunks).decode("utf-8", errors="replace").strip()
        raise OpencodeError(f"opencode exited {proc.returncode}: {err[:2000]}")


# ---------------------------------------------------------------------------
# Event extraction
# ---------------------------------------------------------------------------
def _extract_text(event: Dict[str, Any], state: Dict[str, Any]) -> Optional[str]:
    if event.get("type") != "text":
        return None
    part = event.get("part") or {}
    text = part.get("text")
    if not isinstance(text, str) or not text:
        return None
    part_id = str(part.get("id") or "__default__")
    by_part = state.setdefault("text_by_part", {})
    previous = by_part.get(part_id, "")
    if text.startswith(previous):
        delta = text[len(previous):]
    else:
        delta = text
    by_part[part_id] = text
    if not delta:
        return None
    state["full_text"] = state.get("full_text", "") + delta
    return delta


def _extract_reasoning(event: Dict[str, Any], state: Dict[str, Any]) -> Optional[str]:
    if event.get("type") != "reasoning":
        return None
    part = event.get("part") or {}
    text = part.get("text") or part.get("reasoning")
    if not isinstance(text, str) or not text:
        return None
    part_id = str(part.get("id") or "__reasoning__")
    by_part = state.setdefault("reasoning_by_part", {})
    previous = by_part.get(part_id, "")
    if text.startswith(previous):
        delta = text[len(previous):]
    else:
        delta = text
    by_part[part_id] = text
    return delta or None


def _extract_tool_call(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if event.get("type") != "tool_use":
        return None
    part = event.get("part") or {}
    state = part.get("state") or {}
    call_id = str(part.get("callID") or part.get("id") or f"call_{uuid.uuid4().hex[:8]}")
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": str(part.get("tool") or "unknown"),
            "arguments": json.dumps(state.get("input") or {}, ensure_ascii=False),
        },
        "output": state.get("output"),
        "status": state.get("status"),
        "title": state.get("title"),
    }


def _finish_reason_from(event: Dict[str, Any]) -> Optional[str]:
    if event.get("type") != "step_finish":
        return None
    reason = (event.get("part") or {}).get("reason")
    if reason in ("stop", "end_turn", "end-turn"):
        return "stop"
    if reason in ("tool-calls", "tool_calls"):
        return "tool_calls"
    if reason == "length":
        return "length"
    return None


def _error_text(event: Dict[str, Any]) -> Optional[str]:
    if event.get("type") != "error":
        return None
    payload = event.get("part") or event.get("error") or event
    try:
        return json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return str(payload)


def _session_id_from(event: Dict[str, Any]) -> Optional[str]:
    sid = event.get("sessionID")
    return sid if isinstance(sid, str) and sid else None


def _accumulate_tokens(event: Dict[str, Any], state: Dict[str, Any]) -> None:
    """Sum real token counts from `step_finish.part.tokens` across agent steps."""
    if event.get("type") != "step_finish":
        return
    tokens = (event.get("part") or {}).get("tokens")
    if not isinstance(tokens, dict):
        return
    totals = state.setdefault(
        "tokens",
        {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "steps": 0},
    )

    def as_int(value: Any) -> int:
        return int(value) if isinstance(value, (int, float)) else 0

    totals["input"] += as_int(tokens.get("input"))
    totals["output"] += as_int(tokens.get("output"))
    totals["reasoning"] += as_int(tokens.get("reasoning"))
    cache = tokens.get("cache")
    if isinstance(cache, dict):
        totals["cache_read"] += as_int(cache.get("read"))
    totals["steps"] += 1


def _usage_dict(prompt: str, state: Dict[str, Any]) -> Dict[str, Any]:
    totals = state.get("tokens")
    if isinstance(totals, dict) and (
        totals.get("input") or totals.get("output") or totals.get("cache_read")
    ):
        prompt_tokens = int(totals.get("input", 0)) + int(totals.get("cache_read", 0))
        completion_tokens = int(totals.get("output", 0)) + int(totals.get("reasoning", 0))
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "prompt_tokens_details": {
                "cached_tokens": int(totals.get("cache_read", 0)),
            },
            "completion_tokens_details": {
                "reasoning_tokens": int(totals.get("reasoning", 0)),
            },
        }
    full = state.get("full_text", "")
    prompt_tokens = _estimate_tokens(prompt)
    completion_tokens = _estimate_tokens(full)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


# ---------------------------------------------------------------------------
# One-shot completion (non-streaming)
# ---------------------------------------------------------------------------
async def _run_collect_once(
    req: ChatCompletionRequest,
    prompt: str,
    workdir: Optional[str] = None,
) -> Dict[str, Any]:
    cmd = build_command(req)
    state: Dict[str, Any] = {"full_text": "", "text_by_part": {}}
    tool_calls: Dict[str, Dict[str, Any]] = {}
    finish_reason: Optional[str] = None
    error_message: Optional[str] = None
    session_id: Optional[str] = None

    try:
        async for event in _iter_opencode_events(cmd, prompt, OPENCODE_TIMEOUT, workdir):
            if not session_id:
                session_id = _session_id_from(event)
            _extract_text(event, state)
            _extract_reasoning(event, state)
            tool_call = _extract_tool_call(event)
            if tool_call:
                tool_calls[tool_call["id"]] = tool_call
            finish = _finish_reason_from(event)
            if finish:
                finish_reason = finish
            error = _error_text(event)
            if error:
                error_message = error
            _accumulate_tokens(event, state)
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except OpencodeError as exc:
        if error_message:
            status, message, error_type, retry_after = error_status_and_message(error_message)
            detail = f"opencode error: {message}"
            if error_type:
                detail += f" ({error_type})"
            logger.warning("opencode error status=%s type=%s", status, error_type)
            raise HTTPException(
                status_code=status,
                detail=detail,
                headers=_retry_headers(retry_after),
            ) from exc
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("unexpected bridge error")
        raise HTTPException(status_code=502, detail=f"bridge error: {exc}") from exc

    content = state.get("full_text", "")
    if error_message and not content:
        status, message, error_type, retry_after = error_status_and_message(error_message)
        detail = f"opencode error: {message}"
        if error_type:
            detail += f" ({error_type})"
        raise HTTPException(
            status_code=status,
            detail=detail,
            headers=_retry_headers(retry_after),
        )

    if finish_reason is None:
        finish_reason = "tool_calls" if tool_calls else "stop"

    ordered_tool_calls = list(tool_calls.values())
    message: Dict[str, Any] = {"role": "assistant", "content": content}
    if INCLUDE_REASONING and state.get("reasoning_by_part"):
        message["reasoning_content"] = "".join(state["reasoning_by_part"].values())
    if INCLUDE_TOOL_CALLS and ordered_tool_calls:
        message["tool_calls"] = [
            {k: v for k, v in tc.items() if k in ("id", "type", "function")}
            for tc in ordered_tool_calls
        ]

    opencode_meta: Dict[str, Any] = {
        "tool_calls": ordered_tool_calls,
        "session_id": session_id or req.opencode_session,
        "steps": int((state.get("tokens") or {}).get("steps", 0)),
    }
    if error_message:
        # Partial content plus an upstream error: keep the content but expose
        # the error so callers can inspect it.
        opencode_meta["error"] = error_message

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": resolve_model(req),
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason,
        }],
        "usage": _usage_dict(prompt, state),
        "opencode": opencode_meta,
    }


async def _collect_completion(req: ChatCompletionRequest) -> Dict[str, Any]:
    """Convenience wrapper: prepare everything and run one completion."""
    prompt = prepare_prompt(req)
    workdir = resolve_workdir(req)
    if not _binary_available():
        raise HTTPException(
            status_code=503,
            detail=f"opencode binary not found: {OPENCODE_BIN!r}",
        )
    return await _run_collect_once(req, prompt, workdir)


# ---------------------------------------------------------------------------
# Streaming - native per-event output on success, a valid chunk-shaped error
# on failure. No bare `{"error": ...}` events in a 200 SSE stream.
# ---------------------------------------------------------------------------
async def _stream_native(
    req: ChatCompletionRequest,
    prompt: str,
    workdir: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    cmd = build_command(req)
    state: Dict[str, Any] = {"full_text": "", "text_by_part": {}}
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    model_name = resolve_model(req)
    finish_reason: Optional[str] = None
    role_sent = False
    content_sent = False
    tool_calls: Dict[str, Dict[str, Any]] = {}
    error_message: Optional[str] = None

    def usage() -> Dict[str, Any]:
        return _usage_dict(prompt, state)

    def chunk(
        delta: Dict[str, Any],
        finish: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> str:
        obj: Dict[str, Any] = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model_name,
            "system_fingerprint": None,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        if extra:
            obj.update(extra)
        return _sse(obj)

    def error_tail(message: str) -> List[str]:
        nonlocal role_sent, content_sent
        out: List[str] = []
        if not role_sent:
            role_sent = True
            out.append(chunk({"role": "assistant", "content": ""}))
        if content_sent:
            out.append(chunk(
                {"content": f"\n\n[opencode error: {message}]"},
                finish="stop",
                extra={"usage": usage()},
            ))
        else:
            out.append(chunk({"content": f"[opencode error: {message}]"}))
            out.append(chunk({}, finish="stop", extra={"usage": usage()}))
        out.append("data: [DONE]\n\n")
        return out

    try:
        async for event in _iter_opencode_events(cmd, prompt, OPENCODE_TIMEOUT, workdir):
            # Accumulate first: step_finish events are consumed by the branch
            # below and must not skip their real token counts.
            _accumulate_tokens(event, state)

            if not role_sent:
                role_sent = True
                yield chunk({"role": "assistant", "content": ""})

            delta = _extract_text(event, state)
            if delta:
                content_sent = True
                yield chunk({"content": delta})
                continue

            reasoning_delta = _extract_reasoning(event, state)
            if reasoning_delta and INCLUDE_REASONING:
                content_sent = True
                yield chunk({"reasoning_content": reasoning_delta})
                continue

            tool_call = _extract_tool_call(event)
            if tool_call:
                is_new = tool_call["id"] not in tool_calls
                tool_calls[tool_call["id"]] = tool_call
                if INCLUDE_TOOL_CALLS and is_new:
                    content_sent = True
                    yield chunk({"tool_calls": [{
                        "index": len(tool_calls) - 1,
                        "id": tool_call["id"],
                        "type": "function",
                        "function": tool_call["function"],
                    }]})
                continue

            finish = _finish_reason_from(event)
            if finish:
                finish_reason = finish
                continue

            error = _error_text(event)
            if error:
                error_message = error
    except TimeoutError as exc:
        for item in error_tail(f"timeout: {exc}"):
            yield item
        return
    except OpencodeError as exc:
        if error_message:
            _, message, _, _ = error_status_and_message(error_message)
        else:
            message = str(exc)
        for item in error_tail(message):
            yield item
        return
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("unexpected bridge stream error")
        for item in error_tail(f"bridge error: {exc}"):
            yield item
        return

    # Normal completion (opencode exited 0).
    if error_message:
        _, message, _, _ = error_status_and_message(error_message)
        for item in error_tail(message):
            yield item
        return

    if not role_sent:
        yield chunk({"role": "assistant", "content": ""})
    if finish_reason is None:
        finish_reason = "tool_calls" if tool_calls else "stop"
    yield chunk({}, finish=finish_reason, extra={"usage": usage()})
    yield "data: [DONE]\n\n"


# Backwards-compatible private alias.
_stream_response = _stream_native


# ---------------------------------------------------------------------------
# Model cards
# ---------------------------------------------------------------------------
def _model_card(model_id: str, meta: Dict[str, Any]) -> ModelCard:
    return ModelCard(
        id=model_id,
        metadata=ModelMetadata(
            display_name=meta.get("display_name"),
            capabilities=list(meta.get("capabilities") or []),
            limits=ModelLimits(
                context_window=int(meta.get("context_window", 200_000)),
                max_output=int(meta.get("max_output", 32_768)),
            ),
            vision=bool(meta.get("vision")),
            reasoning=bool(meta.get("reasoning")),
            api=str(meta.get("api", "chat")),
        ),
    )


# ---------------------------------------------------------------------------
# Lifespan: probe the CLI version once, never mutate OpenCode state.
# ---------------------------------------------------------------------------
async def _probe_opencode_version() -> Optional[str]:
    if not _binary_available():
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            OPENCODE_BIN, "--version",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        text = out.decode("utf-8", errors="replace").strip()
        return text or None
    except Exception:  # pragma: no cover - version probe is best-effort
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _OPENCODE_VERSION
    _OPENCODE_VERSION = await _probe_opencode_version()
    if _OPENCODE_VERSION:
        logger.info("opencode CLI detected: %s", _OPENCODE_VERSION)
    else:
        logger.warning("opencode CLI not found or not runnable: %r", OPENCODE_BIN)
    yield


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(
    title="opencode-openai-bridge",
    version=VERSION,
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _error_type_for_status(status: int) -> str:
    if status == 400:
        return "invalid_request_error"
    if status == 401:
        return "authentication_error"
    if status == 403:
        return "permission_error"
    if status == 404:
        return "not_found_error"
    if status == 413:
        return "invalid_request_error"
    if status == 429:
        return "rate_limit_error"
    if status >= 500:
        return "api_error"
    return "invalid_request_error"


@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail
    if isinstance(detail, dict):
        message = detail.get("message") or json.dumps(detail, ensure_ascii=False)
    else:
        message = str(detail)
    body = {
        "error": {
            "message": message,
            "type": _error_type_for_status(exc.status_code),
            "param": None,
            "code": exc.status_code,
        },
        # Keep the FastAPI-native field for clients that read `detail`.
        "detail": detail,
    }
    return JSONResponse(status_code=exc.status_code, content=body, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    parts: List[str] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()))
        parts.append(f"{loc}: {err.get('msg', 'invalid value')}" if loc else str(err.get("msg")))
    message = "; ".join(parts) or "Invalid request body"
    body = {
        "error": {
            "message": message,
            "type": "invalid_request_error",
            "param": None,
            "code": 400,
        },
        "detail": message,
    }
    return JSONResponse(status_code=400, content=body)


@app.get("/")
async def root() -> Dict[str, Any]:
    return {
        "name": "opencode-openai-bridge",
        "version": VERSION,
        "endpoints": [
            "/v1/chat/completions",
            "/v1/models",
            "/v1/models/{model_id}",
            "/health",
        ],
        "opencode_bin": OPENCODE_BIN,
        "opencode_version": _OPENCODE_VERSION,
        "server": OPENCODE_SERVER or None,
        "auto_approve": OPENCODE_AUTO,
        "default_model": DEFAULT_MODEL,
        "bind": {"host": HOST, "port": PORT},
    }


@app.get("/health")
async def health() -> Dict[str, Any]:
    available = _binary_available()
    return {
        "status": "ok" if available else "degraded",
        "version": VERSION,
        "opencode_available": available,
        "opencode_bin": OPENCODE_BIN,
        "opencode_version": _OPENCODE_VERSION,
        "models": len(_MODELS_OVERRIDE) if _MODELS_OVERRIDE else len(list_models()),
        "uptime_seconds": int(time.time() - _STARTED_AT),
        "bind": {"host": HOST, "port": PORT},
    }


@app.get("/v1/models", response_model=ModelList)
async def list_models_endpoint(request: Request) -> ModelList:
    if not _auth_ok(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    if _MODELS_OVERRIDE:
        cards = [_model_card(m, get_model_meta(m)) for m in _MODELS_OVERRIDE]
        return ModelList(data=cards)
    return ModelList(data=[_model_card(m["id"], m) for m in list_models()])


@app.get("/v1/models/{model_id:path}", response_model=ModelCard)
async def get_model_endpoint(model_id: str, request: Request) -> ModelCard:
    if not _auth_ok(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return _model_card(model_id, get_model_meta(model_id))


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest, request: Request):
    if not _auth_ok(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not req.messages:
        raise HTTPException(status_code=400, detail="messages must be a non-empty array")

    # Validate everything before the SSE stream starts so callers get proper
    # HTTP status codes instead of an error hidden inside a 200 stream.
    model = resolve_model(req)
    prompt = prepare_prompt(req)
    workdir = resolve_workdir(req)
    if not _binary_available():
        raise HTTPException(
            status_code=503,
            detail=f"opencode binary not found: {OPENCODE_BIN!r}",
        )

    logger.info(
        "chat request model=%s stream=%s messages=%d prompt_bytes=%d",
        model, req.stream, len(req.messages), len(prompt.encode("utf-8")),
    )

    if req.stream:
        return StreamingResponse(
            _stream_native(req, prompt, workdir),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    return JSONResponse(await _run_collect_once(req, prompt, workdir))


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run("server:app", host=HOST, port=PORT, reload=False, log_level="info")
