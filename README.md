# opencode-openai-bridge

OpenAI-compatible HTTP server that shells out to the **real OpenCode CLI** and
translates its NDJSON event stream into Chat Completions responses.

**Current version: v0.7.0** — bridge-only release. All rate-limit bypass
features (VPN/proxy/egress rotation, retry-on-429, block ledgers, the former
`unlimited/` package) are gone. See [Ethical stance](#ethical-stance).

Verified end-to-end against **opencode v2.2.5**.

## Ethical stance

This bridge does **not** attempt to circumvent OpenCode's free-tier limits.

The free tier has a per-egress-block daily quota. Some tools rotated the egress
IP (VPN, proxy, Tor) to reset it. That is rate-limit circumvention and it
violates the provider's terms of service. This bridge does none of it.

When OpenCode reports a free-tier limit, the bridge surfaces the real error and
stops. If you hit the limit:

- Wait for the quota reset (OpenCode reports `retryAfterMs`; the bridge forwards
  it as a `Retry-After` header on the non-streaming path).
- Use a paid OpenCode plan.
- Configure a different provider in OpenCode's own config.
- Use `stream: false` when your client needs a real HTTP status code.

The bridge is a legitimate OpenAI-compatible adapter for your own authenticated
`opencode` CLI. Nothing more.

## Requirements

- Python 3.9+
- `opencode` CLI installed and authenticated (`opencode auth login` or whatever
  your install uses)
- The model you request must be available to your account (`opencode models`)

The bridge never reads, writes, or migrates OpenCode's database, `auth.json`, or
config. It only spawns `opencode run` and reads its stdout.

## Quick start

```bash
./start.sh
```

Windows:

```bat
start.bat
```

Manual:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python verify_files.py
python test_imports.py
python -m uvicorn server:app --host 0.0.0.0 --port 4097
```

Or with Make:

```bash
make install check run
```

On first start the launcher creates `.venv`, installs dependencies when
`requirements.txt` changes, runs the file check and the smoke test, warns if
`opencode` is not on `PATH`, then starts uvicorn.

## Env var namespacing (important)

**All bridge variables use `BRIDGE_*`.** The generic `PORT` / `HOST` / `API_KEY`
names are read as *fallbacks only*, and other tools can hijack them (OmniRoute
exports `PORT=20128`).

| Preferred | Legacy fallback | Effect |
|---|---|---|
| `BRIDGE_PORT` | `PORT` | bind port |
| `BRIDGE_HOST` | `HOST` | bind host |
| `BRIDGE_API_KEY` | `API_KEY` | required bearer token |
| `BRIDGE_OPENCODE_BIN` | `OPENCODE_BIN` | CLI path |

`start.sh` and `start.bat` **never read `PORT`** — only `BRIDGE_PORT`. Even if
your shell exports OmniRoute's `PORT=20128`, the bridge still binds 4097.

An explicitly set but empty `BRIDGE_*` variable wins over the legacy name. That
lets `BRIDGE_API_KEY=` disable auth even when a legacy `API_KEY` is exported.

## Full config

| Variable | Default | Purpose |
|---|---|---|
| `BRIDGE_OPENCODE_BIN` | `opencode` | CLI executable or absolute path |
| `BRIDGE_OPENCODE_SERVER` | — | attach to a warm `opencode serve` URL (passes `--server`) |
| `BRIDGE_OPENCODE_ATTACH` | — | legacy alias for `BRIDGE_OPENCODE_SERVER` |
| `BRIDGE_OPENCODE_AUTO` | `true` | pass `--auto` so the agent can use tools without prompts |
| `BRIDGE_OPENCODE_WORKDIR` | — | default cwd for the spawned CLI |
| `BRIDGE_OPENCODE_TIMEOUT` | `300` | per-request timeout in seconds (minimum 1) |
| `BRIDGE_DEFAULT_MODEL` | `opencode/deepseek-v4.1-flash-free` | model used when a request omits `model` |
| `BRIDGE_HOST` | `0.0.0.0` | bind host |
| `BRIDGE_PORT` | `4097` | bind port |
| `BRIDGE_API_KEY` | — | require `Authorization: Bearer <key>` (or `X-API-Key`) |
| `BRIDGE_INCLUDE_REASONING` | `false` | request `--thinking` and emit `reasoning_content` |
| `BRIDGE_INCLUDE_TOOL_CALLS` | `false` | expose OpenCode tool calls as OpenAI `tool_calls` |
| `BRIDGE_MODELS` | — | comma list that overrides the built-in catalog |
| `BRIDGE_MODEL_CATALOG_JSON` | — | path to a JSON catalog override |

Legacy `OPENCODE_BIN`, `OPENCODE_AUTO`, `OPENCODE_WORKDIR`,
`OPENCODE_TIMEOUT`, and the other unnamespaced names still work as fallbacks,
but `BRIDGE_*` is strongly preferred.

## How it works

OpenCode v2 changed its CLI surface in ways that broke earlier bridge versions.
The current implementation accounts for all of them:

- **Prompt via stdin, never argv.** `opencode run` accepts the prompt on stdin.
  Passing it as an argv element fails for long conversations because Linux caps
  a single argument at `MAX_ARG_STRLEN` (about 128 KiB). The bridge writes the
  prompt to the child's stdin pipe and closes it.
- **Working directory via `cwd=`.** OpenCode v2 has no `--dir` flag; passing it
  makes the CLI print help and exit 1. The bridge passes the directory to
  `asyncio.create_subprocess_exec(cwd=...)` instead.
- **Attach via `--server`.** The old `--attach` flag no longer exists.
- **Real usage numbers.** The bridge sums `step_finish.part.tokens` across agent
  steps: `prompt_tokens = input + cache.read`, `completion_tokens = output +
  reasoning`. It only estimates from character counts when OpenCode emits no
  token data at all.
- **Honest errors.** OpenCode's `error` events are parsed and mapped to proper
  HTTP status codes and human-readable messages instead of dumping raw JSON at
  the caller.

## Point OmniRoute at it

| Field | Value |
|---|---|
| Base URL | `http://localhost:4097/v1` |
| API Key | anything, or your `BRIDGE_API_KEY` |
| Model | `opencode/deepseek-v4.1-flash-free` |

## Try it

Non-streaming:

```bash
curl -s http://localhost:4097/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "opencode/deepseek-v4.1-flash-free",
    "messages": [{"role": "user", "content": "Say hi"}]
  }' | jq
```

Streaming:

```bash
curl -N http://localhost:4097/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "opencode/deepseek-v4.1-flash-free",
    "messages": [{"role": "user", "content": "Count to five"}],
    "stream": true
  }'
```

Catalog and health:

```bash
curl -s http://localhost:4097/v1/models \
  | jq '.data[] | {id, ctx: .metadata.limits.context_window}'
curl -s http://localhost:4097/health | jq
```

A specific working directory for one request:

```bash
curl -s http://localhost:4097/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "opencode/deepseek-v4.1-flash-free",
    "messages": [{"role": "user", "content": "List the files here"}],
    "opencode_dir": "/tmp/sandbox"
  }' | jq
```

## Model catalog

The built-in catalog matches the free `opencode/*` models reported by
`opencode models` on v2.2.5:

```
beam-free
big-pickle
crouton-alpha-free
deepseek-v4.1-flash-free
flint-40b-free
glm-5.3-flash-free
iceman-free
inkling-2-small-free
longcat-2.5-free
longcat-2.5-preview-free
mimo-v2.6-flash-free
nemotron-3.5-lightning-free
nemotron-3.5-super-vl-free
```

Unknown model ids are still accepted and served with conservative fallback
metadata (200k context / 32k output). The catalog never blocks a model that your
CLI can actually run.

Override the list with `BRIDGE_MODELS`, or override individual entries with
`BRIDGE_MODEL_CATALOG_JSON`:

```json
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
```

## Error handling

Non-streaming failures use a real HTTP status and an OpenAI-style body that also
keeps the FastAPI `detail` field for older clients:

```json
{
  "error": {
    "message": "opencode error: Free usage limit reached (provider.free-limit)",
    "type": "rate_limit_error",
    "param": null,
    "code": 429
  },
  "detail": "opencode error: Free usage limit reached (provider.free-limit)"
}
```

Status mapping:

| Upstream condition | HTTP status |
|---|---|
| `provider.free-limit`, quota, rate limit | `429` |
| auth / permission failure | `401` |
| `provider.no-route`, unknown model, unavailable | `404` |
| bad request, empty messages, NUL, bad `opencode_dir` | `400` |
| prompt too large | `413` |
| `opencode` binary missing | `503` |
| request timeout | `504` |
| anything else from the CLI | `502` |

When OpenCode supplies `retryAfterMs`, the bridge converts it to a `Retry-After`
header in seconds.

## Streaming behavior

- **Success:** each OpenCode `text` part becomes one or more
  `chat.completion.chunk` events with `delta.content`. Reasoning deltas appear
  as `delta.reasoning_content` when `BRIDGE_INCLUDE_REASONING=true`. The final
  chunk carries `finish_reason` and `usage`, followed by `data: [DONE]`.
- **Error before content:** the stream emits a role chunk, a content chunk with
  a human-readable `[opencode error: ...]` message, a finish chunk with
  `finish_reason="stop"` and usage, then `[DONE]`. A 200 SSE stream cannot carry
  a 4xx status after the headers are sent, so the error travels inside a valid
  chunk rather than as a bare `data: {"error": ...}` event.
- **Error after partial content:** the already-streamed content is preserved and
  the error text is appended as a final content chunk.
- **Pre-flight failures** (bad JSON body, empty messages, NUL bytes, oversize
  prompt, missing binary, bad `opencode_dir`) are rejected **before** the stream
  starts, so clients receive a normal `400` / `413` / `503`.

If a client requires strict 4xx/5xx statuses for upstream quota errors, use
`stream: false`.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | name, version, config summary |
| `GET` | `/health` | status, binary availability, model count, uptime |
| `GET` | `/v1/models` | model catalog |
| `GET` | `/v1/models/{id}` | single model card |
| `POST` | `/v1/chat/completions` | chat completions (streaming or not) |

`/v1/models` and `/v1/chat/completions` honor `BRIDGE_API_KEY` when set. `/` and
`/health` stay open so process supervisors can probe the server.

## Request extensions

These optional top-level fields are bridge-specific and ignored by OpenAI
clients that do not send them:

| Field | Effect |
|---|---|
| `opencode_dir` | cwd for this request's `opencode run` process |
| `opencode_agent` | pass `--agent <name>` |
| `opencode_session` | pass `--session <id>` |

Unknown OpenAI fields (`response_format`, `parallel_tool_calls`, `logprobs`,
etc.) are accepted and ignored instead of producing a `422`.

## Fire test

Run these in order. They prove the bridge works end-to-end on your machine.

```bash
# 1. Smoke-test OpenCode itself, with no bridge involved.
#    If this fails, fix OpenCode auth/quota before touching the bridge.
printf 'Reply with exactly: OPENCODE_SMOKE_OK' \
  | opencode run --format json --model opencode/deepseek-v4.1-flash-free

# 2. Start the bridge (leave it running in another terminal).
./start.sh

# 3. Health.
curl -s http://localhost:4097/health | jq

# 4. Catalog.
curl -s http://localhost:4097/v1/models \
  | jq '.data[] | {id, ctx: .metadata.limits.context_window}'

# 5. Non-streaming completion.
curl -s http://localhost:4097/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"opencode/deepseek-v4.1-flash-free","messages":[{"role":"user","content":"Say hi in 3 words."}]}' \
  | jq '{content: .choices[0].message.content, usage, finish: .choices[0].finish_reason}'

# 6. Streaming completion.
curl -N -s http://localhost:4097/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"opencode/deepseek-v4.1-flash-free","messages":[{"role":"user","content":"Say hi in 3 words."}],"stream":true}' \
  | head -20

# 7. Working directory (must NOT produce an "Unrecognized flag: --dir" error).
curl -s http://localhost:4097/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"opencode/deepseek-v4.1-flash-free","messages":[{"role":"user","content":"Reply OK"}],"opencode_dir":"/tmp"}' \
  | jq '.choices[0].message.content'

# 8. Invalid model (must be HTTP 404 with a human message, not raw JSON).
curl -s -o /tmp/bridge_err.json -w '%{http_code}\n' \
  http://localhost:4097/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"opencode/definitely-not-a-model","messages":[{"role":"user","content":"hi"}]}'
cat /tmp/bridge_err.json | jq
```

Expected:

- **Step 1:** `OPENCODE_SMOKE_OK` in the NDJSON output.
- **Step 3:** `"status": "ok"`, `"opencode_available": true`, `"models": 13`,
  and `"opencode_version"` containing your CLI version.
- **Step 4:** the 13 real model ids with context values; deepseek shows
  `1000000`.
- **Step 5:** a non-empty `content`, `usage.prompt_tokens` in the thousands
  (real tokens, not 10), and `finish: "stop"`.
- **Step 6:** `data: {"id":"chatcmpl-...","object":"chat.completion.chunk",...}`
  lines, a final chunk with `finish_reason` and `usage`, then `data: [DONE]`.
  There must be no bare `data: {"error": ...}` line.
- **Step 7:** `"OK"` (or whatever the model replies). No `--dir` error.
- **Step 8:** HTTP `404` and a body whose `error.message` reads
  `opencode error: Model unavailable: opencode/definitely-not-a-model
  (provider.no-route)`.

## Troubleshooting

**`Unrecognized flag: --dir in command opencode run`**
You are running an old bridge. v0.7.0 passes the working directory via
`cwd=` instead. Check `server.py` contains `cwd=cwd` and no `"--dir"`.

**`Unrecognized flag: --attach`**
OpenCode v2 renamed it to `--server`. Set `BRIDGE_OPENCODE_SERVER` (or the
legacy `BRIDGE_OPENCODE_ATTACH`); v0.7.0 emits `--server`.

**HTTP 503 / `opencode binary not found`**
`opencode` is not on `PATH` for the user running the bridge. Set
`BRIDGE_OPENCODE_BIN=/absolute/path/to/opencode`.

**HTTP 429 / `Free usage limit reached`**
The free-tier quota is exhausted. This is expected behavior; the bridge does not
rotate egress or retry to evade it. Wait for the reset, use a paid plan, or
switch providers inside OpenCode.

**HTTP 404 / `Model unavailable`**
Run `opencode models` and use one of the ids it prints.

**Streaming returns 200 but the text says `[opencode error: ...]`**
The upstream error arrived after the SSE headers were already sent. Use
`stream: false` if your client needs the real HTTP status.

**Long conversation fails**
v0.7.0 sends the prompt on stdin, so the old 128 KiB argv limit no longer
applies. The bridge's own safety cap is 8 MB of prompt text, which returns
`413` rather than failing inside the CLI.

## Layout

```
opencode-openai-bridge/
├── server.py             FastAPI server and NDJSON translation
├── model_catalog.py      Per-model limits and capabilities
├── requirements.txt
├── .env.example          All variables documented
├── .python-version
├── README.md
├── LICENSE
├── Makefile
├── test_imports.py       Deterministic smoke test
├── verify_files.py       File-presence check
├── start.sh              POSIX launcher
└── start.bat             Windows launcher
```

## Safety

Never touches: `opencode.db`, `auth.json`, system identifiers, OpenCode config.

The bridge only spawns `opencode run`; it never mutates OpenCode state. It does
not proxy raw API calls, rotate egress, retry quota errors, or install anything
into OpenCode.

## Caveats

- **OpenCode is an agent.** It can read and write files depending on the prompt
  and agent. Set `BRIDGE_OPENCODE_WORKDIR` (or per-request `opencode_dir`) to a
  sandbox if you do not want it touching your project.
- **Free-tier quota is per egress block, per UTC day.** If you exhaust it, the
  bridge surfaces the error and stops. That is intentional.
- **No bypass.** Do not add egress rotation, proxy pools, or retry-on-429 to this
  bridge.
- OpenCode's NDJSON schema is not a public contract. The extractors tolerate
  missing fields, but a future CLI release could change event names. Run
  `test_imports.py` and the fire test after upgrading OpenCode.

## License

MIT
