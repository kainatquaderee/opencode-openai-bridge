# opencode-openai-bridge

OpenAI-compatible HTTP server that shells out to the **real OpenCode CLI** and translates its NDJSON event stream into Chat Completions responses.

**Current version: v0.6.0** — bridge-only release. All rate-limit bypass features (VPN/proxy/egress rotation, retry-on-429, block ledgers, `unlimited/` package) have been removed. See [Ethical stance](#ethical-stance).

## Ethical stance

This bridge does **not** attempt to circumvent OpenCode's free-tier rate limits.

The free tier has a per-egress-block daily quota. Historically, some tools rotated the egress IP (VPN, proxy, Tor) to reset the quota. **That is rate-limit circumvention and it violates the provider's terms of service.**

v0.6.0 removes all such functionality. If you hit the free-tier limit, the bridge surfaces the error to your caller. Options:

- Wait until 00:00 UTC for the quota to reset
- Use a paid OpenCode Go subscription
- Configure a different provider in OpenCode
- Use `stream: false` and handle the 502 response in your client

The bridge is a legitimate OpenAI-compatible adapter for your own authenticated `opencode` CLI. Nothing more.

## Quick start

```bash
./start.sh
```

Windows: `start.bat`.

Manual:

```
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python verify_files.py
python test_imports.py
uvicorn server:app --host 0.0.0.0 --port 4097
```

Or via Make:

```
make install check run
```

## Env var namespacing (important)

**All bridge vars use `BRIDGE_*`.** The generic `PORT` / `HOST` / `API_KEY` names are read as *fallbacks only*, and can be hijacked by other tools (OmniRoute exports `PORT=20128`).

| Preferred | Legacy fallback | Effect |
|---|---|---|
| `BRIDGE_PORT` | `PORT` | bind port |
| `BRIDGE_HOST` | `HOST` | bind host |
| `BRIDGE_API_KEY` | `API_KEY` | required bearer token |
| `BRIDGE_OPENCODE_BIN` | `OPENCODE_BIN` | CLI path |

`start.sh` and `start.bat` **never read `PORT`** — only `BRIDGE_PORT`. Even if your shell has OmniRoute's `PORT=20128` exported, the bridge lands on 4097.

## Point OmniRoute at it

| Field ↕▾ | Value ↕▾ |
|---|---|
| −Base URL | `http://localhost:4097/v1` |
| −API Key | anything (or your `BRIDGE_API_KEY`) |
| Model | `opencode/deepseek-v4.1-flash-free` |
⚙

## Try it

```
curl -s http://localhost:4097/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"opencode/deepseek-v4.1-flash-free","messages":[{"role":"user","content":"Say hi"}]}'
```

```
curl -s http://localhost:4097/v1/models | jq '.data[] | {id, ctx: .metadata.limits.context_window}'
curl -s http://localhost:4097/health | jq
```

## Fire test

Run these in order. They prove the bridge works end-to-end on your machine.

```
# 1. Smoke test opencode itself (no bridge involved).
#    If this fails, the bridge cannot help — fix opencode auth/quota first.
opencode run "Respond with exactly: OPENCODE_SMOKE_OK"

# 2. Start the bridge.
./start.sh

# 3. Non-streaming completion.
curl -s http://localhost:4097/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"opencode/deepseek-v4.1-flash-free","messages":[{"role":"user","content":"Say hi in 3 words."}]}' | jq

# 4. Streaming completion. Watch for valid chat.completion.chunk events,
#    then [DONE]. NO bare `data: {"error": ...}` lines.
curl -N -s http://localhost:4097/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"opencode/deepseek-v4.1-flash-free","messages":[{"role":"user","content":"Say hi in 3 words."}],"stream":true}' | cat -A | head -30

# 5. Model catalog.
curl -s http://localhost:4097/v1/models | jq '.data[] | {id, ctx: .metadata.limits.context_window}'

# 6. Health (no `bypass` field in v0.6.0).
curl -s http://localhost:4097/health | jq
```

Expected:

- **Step 1**: `OPENCODE_SMOKE_OK` appears in the output.
- **Step 3**: JSON with `choices[0].message.content` populated (e.g. `"Hi there friend"`).
- **Step 4**: lines like `data: {"id":"chatcmpl-...","object":"chat.completion.chunk",...,"choices":[{"index":0,"delta":{"content":"..."},...}]}`, then `data: [DONE]`.
- **Step 5**: array of 18 models with `ctx` values.
- **Step 6**: `{"status":"ok",...}` — no `bypass` field.

## Streaming behavior

- **Success:** true per-token streaming. Each `text` event from `opencode` becomes a `chat.completion.chunk` with `delta.content`.
- **Upstream error before any content:** the stream emits a role chunk, a content chunk carrying the error text (e.g. `[opencode error: {"type":"provider.free-limit",...}]`), a finish chunk with `finish_reason="stop"`, then `[DONE]`. HTTP status is 200 because SSE has already started.
- **Upstream error after some content:** existing content is preserved; the error text is appended as a final content chunk with `finish_reason="stop"`, then `[DONE]`.
- **Bad prompt / missing binary:** caught by pre-flight in `chat_completions` **before** the stream starts, so callers get proper HTTP `400` / `413` / `503`.

If your client requires strict 4xx/5xx status codes for rate-limit errors, use `stream: false`. The non-streaming path returns HTTP `502` with the full error body.

## Full config

| Variable ↕▾ | Default ↕▾ | Purpose ↕▾ |
|---|---|---|
| −`BRIDGE_OPENCODE_BIN` | `opencode` | CLI path |
| `BRIDGE_OPENCODE_ATTACH` | — | warm `opencode serve` URL |
| `BRIDGE_OPENCODE_AUTO` | `true` | pass `--auto` |
| `BRIDGE_OPENCODE_WORKDIR` | — | `--dir` |
| `BRIDGE_OPENCODE_TIMEOUT` | `300` | per-request timeout |
| `BRIDGE_HOST` | `0.0.0.0` | bind host |
| `BRIDGE_PORT` | `4097` | bind port |
| `BRIDGE_API_KEY` | — | require `Authorization: Bearer` |
| `BRIDGE_INCLUDE_REASONING` | `false` | emit `reasoning_content` |
| `BRIDGE_INCLUDE_TOOL_CALLS` | `false` | surface `tool_calls` |
| `BRIDGE_MODELS` | — | comma list; overrides catalog |
| `BRIDGE_MODEL_CATALOG_JSON` | — | override JSON path |
⚙

Legacy `PORT` / `HOST` / `API_KEY` / `OPENCODE_BIN` / etc. still work as fallbacks but are **strongly discouraged**.

## Endpoints

| Method ↕▾ | Path ↕▾ | Purpose ↕▾ |
|---|---|---|
| −GET | `/` | info + bind |
| GET | `/health` | status |
| GET | `/v1/models` | catalog |
| GET | `/v1/models/{id}` | single model |
| POST | `/v1/chat/completions` | chat |
⚙

## Layout

```
opencode-openai-bridge/
├── server.py             FastAPI server
├── model_catalog.py      Per-model limits/caps
├── requirements.txt
├── .env.example          All vars documented
├── .python-version
├── README.md
├── LICENSE
├── Makefile
├── test_imports.py       Smoke test
├── verify_files.py       File-presence check
├── start.sh              POSIX launcher
└── start.bat             Windows launcher
```

## Safety

Never touches: `opencode.db`, `auth.json`, system identifiers, OpenCode config.

The bridge only spawns `opencode run`; it never mutates OpenCode state.

## Caveats

- **OpenCode is an agent.** Set `BRIDGE_OPENCODE_WORKDIR` to a sandbox if you don't want it touching files.
- **Free-tier quota is per egress block, per UTC day.** If you exhaust it, the bridge surfaces the error and stops. That is intentional.
- **No bypass.** Do not add egress rotation, proxy pools, or retry-on-429 to this bridge.

## License

MIT

</BDS:create_file>

<BDS:create_file fileName="start.bat">

```batch
@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

set MODE=run
if "%~1"=="--check" set MODE=check
if "%~1"=="--reinstall" (
    if exist .venv rmdir /s /q .venv
)

REM Optional .env loading
if exist .env (
    for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
        set "KEY=%%A"
        set "VAL=%%B"
        if not "!KEY!"=="" if not "!KEY:~0,1!"=="#" (
            set "!KEY!=!VAL!"
        )
    )
    echo ==^> loaded .env
)

REM Python check
where python >nul 2>nul
if errorlevel 1 (
    echo ERROR: python not found on PATH.
    exit /b 1
)

REM Virtualenv
if not exist .venv (
    echo ==^> creating .venv
    python -m venv .venv
    if errorlevel 1 exit /b 1
)

call .venv\Scripts\activate.bat

REM Deps hash check via python (portable)
python -c "import hashlib,sys,os; h=hashlib.sha256(open('requirements.txt','rb').read()).hexdigest(); p='.venv/.deps-hash'; c=open(p).read() if os.path.exists(p) else ''; sys.exit(0 if h==c else 1)"
if errorlevel 1 (
    echo ==^> installing deps
    python -m pip install -q --upgrade pip
    python -m pip install -q -r requirements.txt
    python -c "import hashlib,open; h=hashlib.sha256(open('requirements.txt','rb').read()).hexdigest(); open('.venv/.deps-hash','w').write(h)"
)

echo ==^> verifying files
python verify_files.py
if errorlevel 1 exit /b 1

echo ==^> running smoke test
python test_imports.py
if errorlevel 1 exit /b 1

if "%MODE%"=="check" (
    echo.
    echo ==^> check only; not starting server
    exit /b 0
)

if "%BRIDGE_HOST%"=="" set BRIDGE_HOST=0.0.0.0
if "%BRIDGE_PORT%"=="" set BRIDGE_PORT=4097

if not "%PORT%"=="" if not "%PORT%"=="%BRIDGE_PORT%" (
    echo NOTE: shell has PORT=%PORT% ^(ignored^). Using BRIDGE_PORT=%BRIDGE_PORT%.
)

echo.
echo ==^> starting server on %BRIDGE_HOST%:%BRIDGE_PORT%
uvicorn server:app --host %BRIDGE_HOST% --port %BRIDGE_PORT%

