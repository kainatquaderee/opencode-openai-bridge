#!/usr/bin/env python3
"""
opencode-openai-bridge (v0.6.0)

OpenAI-compatible HTTP server that shells out to the real `opencode` CLI
and translates its NDJSON event stream into Chat Completions responses.

This is the **bridge-only** release. All rate-limit bypass functionality
(VPN/proxy/Tor egress rotation, retry-on-429, block ledgers, the former
`unlimited/` package) has been removed. If OpenCode returns a free-tier
limit error, the bridge surfaces it to the caller. See README "Ethical stance".

Env var namespace: BRIDGE_* is preferred to avoid collisions with other
tools that hijack PORT / HOST / API_KEY (e.g. OmniRoute exports PORT=20128).

  BRIDGE_PORT / BRIDGE_HOST / BRIDGE_API_KEY   <- preferred (namespaced)
  PORT / HOST / API_KEY                        <- legacy fallbacks

v0.6.0 highlights
-----------------
- Bypass removed entirely (no `unlimited/`, no `BRIDGE_BYPASS_*`, no retries).
- `error_message` from NDJSON `error` events is preserved in
  `HTTPException.detail` so callers see the real 429 body, not the useless
  `"opencode exited 1: "` string that opencode emits (it writes provider
  errors to stdout as JSON and exits 1 with empty stderr).
- Streaming preserves true per-token output on success. On upstream error,
  the stream emits a **valid** `chat.completion.chunk` carrying the error
  text as content, followed by a finish chunk and `[DONE]`. No bare
  `{"error": ...}` SSE events — strict clients like OmniRoute accept this.
- Subprocess uses `stdin=DEVNULL` so a TTY-launched uvicorn cannot stall
  `opencode run` waiting for input.
- Pre-flight validation of prompt + binary in `chat_completions` so bad
  requests and missing-binary cases return proper HTTP 4xx/5xx status
  codes *before* the SSE stream starts.
- Pydantic models ignore unknown OpenAI fields (`extra = "ignore"`) so
  clients that send `response_format`, `parallel_tool_calls`, etc. do not
  trigger validation errors.

Run:
    python server.py
    uvicorn server:app --host 0.0.0.0 --port 4097
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional

# Optional .env loading so `python server.py` picks up .env without shell magic
try:
    from dotenv import load_dotenv

    _dotenv_path = Path(__file__).parent / ".env"
    if _dotenv_path.exists():
        load_dotenv(_dotenv_path)
except ImportError:
    pass

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from model_catalog import get_model_meta, list_models

# ---------------------------------------------------------------------------
# Config (namespaced preferred; legacy fallback so old .env keeps working)
# ---------------------------------------------------------------------------
def _env(name: str, default: str = "") -> str:
    """BRIDGE_FOO first, then FOO, then default."""
    return os.getenv(f"BRIDGE_{name}") or os.getenv(name) or default

def _env_bool(name: str, default: bool = False) -> bool:
    return _env(name, "true" if default else "false").lower() in ("1", "true", "yes", "on")

def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default

def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default

OPENCODE_BIN = _env("OPENCODE_BIN", "opencode")
OPENCODE_ATTACH = _env("OPENCODE_ATTACH").strip()
OPENCODE_AUTO = _env_bool("OPENCODE_AUTO", True)
OPENCODE_WORKDIR = _env("OPENCODE_WORKDIR").strip()
OPENCODE_TIMEOUT = _env_float("OPENCODE_TIMEOUT", 300.0)
API_KEY = _env("API_KEY").strip()
INCLUDE_REASONING = _env_bool("INCLUDE_REASONING", False)
INCLUDE_TOOL_CALLS = _env_bool("INCLUDE_TOOL_CALLS", False)
HOST = _env("HOST", "0.0.0.0")
PORT = _env_int("PORT", 4097)

_MODELS_OVERRIDE = [m.strip() for m in _env("MODELS").split(",") if m.strip()]

# Linux ARG_MAX is typically ~2 MB; keep prompt under 1.5 MB for headroom
MAX_PROMPT_BYTES = 1_500_000

# ---------------------------------------------------------------------------
# Pydantic models — extra fields ignored so unknown OpenAI parameters
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

    model: str
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
    capabilities: List[str] = []
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
# Helpers
# ---------------------------------------------------------------------------
def _flatten_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for p in content:
            if isinstance(p, dict):
                if p.get("type") == "text":
                    parts.append(str(p.get("text", "")))
                elif "text" in p:
                    parts.append(str(p["text"]))
            elif isinstance(p, str):
                parts.append(p)
        return "\n".join(parts)
    return str(content)

def normalize_model(model: str) -> str:
    model = (model or "").strip()
    if not model:
        raise HTTPException(status_code=400, detail="model is required")
    return model if "/" in model else f"opencode/{model}"

def build_prompt(messages: List[ChatMessage]) -> str:
    system_lines: List[str] = []
    convo: List[str] = []
    for msg in messages:
        text = _flatten_content(msg.content).strip()
        if not text:
            continue
        role = (msg.role or "user").lower()
        if role == "system":
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

def _prepare_prompt(req: ChatCompletionRequest) -> str:
    """Build and validate the prompt. Raises HTTPException for bad input."""
    prompt = build_prompt(req.messages)
    if not prompt:
        raise HTTPException(status_code=400, detail="No usable text in messages")
    if "\x00" in prompt:
        raise HTTPException(status_code=400, detail="Prompt contains NUL bytes")
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Prompt exceeds {MAX_PROMPT_BYTES} bytes (argv limit)",
        )
    return prompt

def build_command(req: ChatCompletionRequest, prompt: str) -> List[str]:
    cmd = [
        OPENCODE_BIN,
        "run",
        "--format", "json",
        "--model", normalize_model(req.model),
    ]
    if OPENCODE_AUTO:
        cmd.append("--auto")
    if OPENCODE_ATTACH:
        cmd.extend(["--attach", OPENCODE_ATTACH])
    workdir = req.opencode_dir or OPENCODE_WORKDIR
    if workdir:
        cmd.extend(["--dir", workdir])
    if req.opencode_agent:
        cmd.extend(["--agent", req.opencode_agent])
    if req.opencode_session:
        cmd.extend(["--session", req.opencode_session])
    if INCLUDE_REASONING:
        cmd.append("--thinking")
    cmd.append(prompt)
    return cmd

def _sse(obj: Dict[str, Any]) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0

def _auth_ok(request: Request) -> bool:
    if not API_KEY:
        return True
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() == API_KEY
    return request.headers.get("x-api-key", "") == API_KEY

# ---------------------------------------------------------------------------
# Subprocess event pump
# ---------------------------------------------------------------------------
async def _iter_opencode_events(
    cmd: List[str],
    timeout: float,
) -> AsyncGenerator[Dict[str, Any], None]:
    if not shutil.which(OPENCODE_BIN):
        raise RuntimeError(
            f"opencode binary not found: {OPENCODE_BIN!r}. "
            "Install it or set BRIDGE_OPENCODE_BIN."
        )

    env = {**os.environ, "NO_COLOR": "1", "FORCE_COLOR": "0"}

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        limit=10 * 1024 * 1024,
    )

    stderr_chunks: List[bytes] = []

    async def drain() -> None:
        assert proc.stderr is not None
        try:
            while True:
                c = await proc.stderr.read(4096)
                if not c:
                    break
                stderr_chunks.append(c)
        except asyncio.CancelledError:
            raise

    stderr_task = asyncio.create_task(drain())
    deadline = time.monotonic() + timeout

    try:
        assert proc.stdout is not None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"opencode run timed out after {timeout}s")
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
            except asyncio.TimeoutError:
                raise TimeoutError(f"opencode run timed out after {timeout}s")
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                yield json.loads(text)
            except json.JSONDecodeError:
                continue
    finally:
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
        try:
            await asyncio.wait_for(stderr_task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            stderr_task.cancel()

    if proc.returncode not in (0, None):
        err = b"".join(stderr_chunks).decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"opencode exited {proc.returncode}: {err[:2000]}")

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
    pid = str(part.get("id") or "__default__")
    by_part = state.setdefault("text_by_part", {})
    prev = by_part.get(pid, "")
    delta = text[len(prev):] if text.startswith(prev) else text
    by_part[pid] = text
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
    pid = str(part.get("id") or "__reasoning__")
    by_part = state.setdefault("reasoning_by_part", {})
    prev = by_part.get(pid, "")
    delta = text[len(prev):] if text.startswith(prev) else text
    by_part[pid] = text
    return delta or None

def _extract_tool_call(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if event.get("type") != "tool_use":
        return None
    part = event.get("part") or {}
    st = part.get("state") or {}
    return {
        "id": str(part.get("callID") or part.get("id") or f"call_{uuid.uuid4().hex[:8]}"),
        "type": "function",
        "function": {
            "name": str(part.get("tool") or "unknown"),
            "arguments": json.dumps(st.get("input") or {}, ensure_ascii=False),
        },
        "output": st.get("output"),
        "title": st.get("title"),
    }

def _finish_reason_from(event: Dict[str, Any]) -> Optional[str]:
    if event.get("type") != "step_finish":
        return None
    reason = (event.get("part") or {}).get("reason")
    if reason in ("stop", "end_turn"):
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
    except Exception:
        return str(payload)

def _session_id_from(event: Dict[str, Any]) -> Optional[str]:
    sid = event.get("sessionID")
    return sid if isinstance(sid, str) and sid else None

# ---------------------------------------------------------------------------
# One-shot completion (non-streaming)
# ---------------------------------------------------------------------------
async def _run_collect_once(req: ChatCompletionRequest, prompt: str) -> Dict[str, Any]:
    cmd = build_command(req, prompt)
    state: Dict[str, Any] = {"full_text": "", "text_by_part": {}}
    tool_calls: List[Dict[str, Any]] = []
    finish_reason = "stop"
    error_message: Optional[str] = None
    session_id: Optional[str] = None

    try:
        async for event in _iter_opencode_events(cmd, OPENCODE_TIMEOUT):
            if not session_id:
                session_id = _session_id_from(event)
            _extract_text(event, state)
            _extract_reasoning(event, state)
            tc = _extract_tool_call(event)
            if tc:
                tool_calls.append(tc)
            fr = _finish_reason_from(event)
            if fr:
                finish_reason = fr
            err = _error_text(event)
            if err:
                error_message = err
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc))
    except RuntimeError as exc:
        # v0.6.0 fix: opencode writes provider errors on **stdout** as NDJSON
        # and exits 1 with empty stderr. `error_message` is the parsed JSON
        # error event; if present, prefer it over the empty-stderr message.
        detail = f"opencode error: {error_message}" if error_message else str(exc)
        raise HTTPException(status_code=502, detail=detail)

    content = state.get("full_text", "")
    if error_message and not content:
        raise HTTPException(status_code=502, detail=f"opencode error: {error_message}")

    message: Dict[str, Any] = {"role": "assistant", "content": content}
    if INCLUDE_REASONING and state.get("reasoning_by_part"):
        message["reasoning_content"] = "".join(state["reasoning_by_part"].values())
    if INCLUDE_TOOL_CALLS and tool_calls:
        message["tool_calls"] = [
            {k: v for k, v in tc.items() if k in ("id", "type", "function")}
            for tc in tool_calls
        ]

    usage = {
        "prompt_tokens": _estimate_tokens(prompt),
        "completion_tokens": _estimate_tokens(content),
        "total_tokens": _estimate_tokens(prompt) + _estimate_tokens(content),
    }

    opencode_meta: Dict[str, Any] = {
        "tool_calls": tool_calls,
        "session_id": session_id or req.opencode_session,
    }
    if error_message:
        # Partial content + error — surface the error as a warning so callers
        # who check it can see what happened, without discarding the content.
        opencode_meta["error"] = error_message

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": normalize_model(req.model),
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason,
        }],
        "usage": usage,
        "opencode": opencode_meta,
    }

async def _collect_completion(req: ChatCompletionRequest) -> Dict[str, Any]:
    prompt = _prepare_prompt(req)
    if not shutil.which(OPENCODE_BIN):
        raise HTTPException(
            status_code=503,
            detail=f"opencode binary not found: {OPENCODE_BIN!r}",
        )
    return await _run_collect_once(req, prompt)

# ---------------------------------------------------------------------------
# Streaming — native per-token output on success, valid chunk-shaped error
# on failure. No bare `{"error": ...}` events in a 200 SSE stream.
# ---------------------------------------------------------------------------
async def _stream_native(
    req: ChatCompletionRequest, prompt: str
) -> AsyncGenerator[str, None]:
    cmd = build_command(req, prompt)
    state: Dict[str, Any] = {"full_text": "", "text_by_part": {}}
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    model_name = normalize_model(req.model)
    finish_reason: Optional[str] = None
    role_sent = False
    content_sent = False
    tool_calls: List[Dict[str, Any]] = []
    error_message: Optional[str] = None

    def _usage() -> Dict[str, int]:
        full = state.get("full_text", "")
        return {
            "prompt_tokens": _estimate_tokens(prompt),
            "completion_tokens": _estimate_tokens(full),
            "total_tokens": _estimate_tokens(prompt) + _estimate_tokens(full),
        }

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

    try:
        async for event in _iter_opencode_events(cmd, OPENCODE_TIMEOUT):
            if not role_sent:
                role_sent = True
                yield chunk({"role": "assistant", "content": ""})

            delta = _extract_text(event, state)
            if delta:
                content_sent = True
                yield chunk({"content": delta})
                continue

            rdelta = _extract_reasoning(event, state)
            if rdelta and INCLUDE_REASONING:
                content_sent = True
                yield chunk({"reasoning_content": rdelta})
                continue

            tc = _extract_tool_call(event)
            if tc:
                tool_calls.append(tc)
                if INCLUDE_TOOL_CALLS:
                    content_sent = True
                    yield chunk({"tool_calls": [{
                        "index": len(tool_calls) - 1,
                        "id": tc["id"],
                        "type": "function",
                        "function": tc["function"],
                    }]})
                continue

            fr = _finish_reason_from(event)
            if fr:
                finish_reason = fr
                continue

            err = _error_text(event)
            if err:
                error_message = err
    except TimeoutError as exc:
        msg = str(exc)
        if not role_sent:
            yield chunk({"role": "assistant", "content": ""})
        if content_sent:
            yield chunk({"content": f"\n\n[opencode error] {msg}"},
                        finish="stop", extra={"usage": _usage()})
        else:
            yield chunk({"content": f"[opencode error] {msg}"})
            yield chunk({}, finish="stop", extra={"usage": _usage()})
        yield "data: [DONE]\n\n"
        return
    except RuntimeError as exc:
        msg = f"opencode error: {error_message}" if error_message else str(exc)
        if not role_sent:
            yield chunk({"role": "assistant", "content": ""})
        if content_sent:
            yield chunk({"content": f"\n\n[{msg}]"},
                        finish="stop", extra={"usage": _usage()})
        else:
            yield chunk({"content": f"[{msg}]"})
            yield chunk({}, finish="stop", extra={"usage": _usage()})
        yield "data: [DONE]\n\n"
        return

    # Stream completed without a RuntimeError. If opencode emitted an error
    # event but still exited 0 (rare), surface the error and finish cleanly.
    if error_message:
        if not role_sent:
            yield chunk({"role": "assistant", "content": ""})
        if content_sent:
            yield chunk({"content": f"\n\n[opencode error] {error_message}"},
                        finish="stop", extra={"usage": _usage()})
        else:
            yield chunk({"content": f"[opencode error] {error_message}"})
            yield chunk({}, finish="stop", extra={"usage": _usage()})
    else:
        if not role_sent:
            yield chunk({"role": "assistant", "content": ""})
        yield chunk({}, finish=finish_reason or "stop", extra={"usage": _usage()})
    yield "data: [DONE]\n\n"

# ---------------------------------------------------------------------------
# Model cards
# ---------------------------------------------------------------------------
def _model_card(model_id: str, meta: Dict[str, Any]) -> ModelCard:
    return ModelCard(
        id=model_id,
        metadata=ModelMetadata(
            display_name=meta.get("display_name"),
            capabilities=meta.get("capabilities", []),
            limits=ModelLimits(
                context_window=meta.get("context_window", 200_000),
                max_output=meta.get("max_output", 32_768),
            ),
            vision=bool(meta.get("vision")),
            reasoning=bool(meta.get("reasoning")),
            api=meta.get("api", "chat"),
        ),
    )

# ---------------------------------------------------------------------------
# Lifespan (no background tasks in v0.6.0)
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    yield

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(
    title="opencode-openai-bridge",
    version="0.6.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def root() -> Dict[str, Any]:
    return {
        "name": "opencode-openai-bridge",
        "version": "0.6.0",
        "endpoints": [
            "/v1/chat/completions",
            "/v1/models",
            "/v1/models/{model_id}",
            "/health",
        ],
        "opencode_bin": OPENCODE_BIN,
        "attach": OPENCODE_ATTACH or None,
        "auto_approve": OPENCODE_AUTO,
        "bind": {"host": HOST, "port": PORT},
    }

@app.get("/health")
async def health() -> Dict[str, Any]:
    ok = shutil.which(OPENCODE_BIN) is not None
    return {
        "status": "ok" if ok else "degraded",
        "opencode_available": ok,
        "opencode_bin": OPENCODE_BIN,
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

    if req.stream:
        # Pre-flight so prompt errors and missing-binary surface as proper
        # HTTP 4xx/5xx responses *before* the SSE stream starts.
        prompt = _prepare_prompt(req)
        if not shutil.which(OPENCODE_BIN):
            raise HTTPException(
                status_code=503,
                detail=f"opencode binary not found: {OPENCODE_BIN!r}",
            )
        return StreamingResponse(
            _stream_native(req, prompt),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    return JSONResponse(await _collect_completion(req))

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host=HOST, port=PORT, reload=False, log_level="info")
