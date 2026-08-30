# Codex Subscription Curator Bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route self-hosted SuperMemory curator tool calls through a ChatGPT-authenticated Codex runtime without exposing or billing an OpenAI API key.

**Architecture:** A private Python HTTP adapter implements only the captured SuperMemory v0.0.8 Chat Completions subset and delegates inference/authentication to OpenAI's official `openai-codex` SDK. It runs in an isolated Compose service with a dedicated `CODEX_HOME`, one in-flight turn, ephemeral read-only threads, strict tool-output validation, and no paid fallback.

**Tech Stack:** Python 3.13.14, standard-library HTTP server, `openai-codex==0.147.0`, `jsonschema==4.26.0`, `unittest`, Docker Compose, SuperMemory server v0.0.8.

**Spec:** `docs/superpowers/specs/2026-08-30-codex-subscription-curator-bridge-design.md`

## Global Constraints

- Personal single-host use only; never expose the bridge publicly.
- The capture gate must pass before adapter implementation begins.
- Implement only non-streaming `POST /v1/chat/completions`, `GET /v1/models`, and `GET /health`.
- Use only OpenAI's official `openai-codex==0.147.0` SDK for Codex auth/runtime access.
- Never parse, copy, refresh, or log OAuth tokens.
- No paid API fallback and no valid billable provider key in either service.
- One in-flight turn; no queue, worker pool, sticky session, routing, or retry abstraction.
- Preserve inbound tool-call IDs/order and support at least 128 outbound calls.
- Keep translator logic version-blind; server compatibility lives in a data-only protocol manifest.
- Stop if the actual App Server child inherits a bridge/provider credential, emits a native tool item, or a prompt-injection canary returns environment, auth, or file data.
- Detect SuperMemory's known `done`-with-zero-memories failure; stop if a positive synthetic document loses its expected memory.

---

## File Map

| File | Responsibility |
|---|---|
| `services/codex-bridge/requirements.txt` | Pinned direct dependencies. |
| `services/codex-bridge/Dockerfile` | Non-root isolated SDK runtime. |
| `services/codex-bridge/.dockerignore` | Exclude tests, caches, Git, and secrets. |
| `services/codex-bridge/src/supermemory_codex_bridge/__init__.py` | Package version. |
| `services/codex-bridge/src/supermemory_codex_bridge/__main__.py` | `serve`, `login`, and `check-auth`. |
| `services/codex-bridge/src/supermemory_codex_bridge/translator.py` | Chat request validation, prompt/schema construction, result validation, OpenAI envelope. |
| `services/codex-bridge/src/supermemory_codex_bridge/service.py` | Official SDK lifecycle, auth gate, timeout/interrupt, HTTP routes, concurrency, safe logs. |
| `services/codex-bridge/tests/capture_provider.py` | Fake provider for scratch protocol capture/replay. |
| `services/codex-bridge/tests/fixtures/supermemory-v0.0.8/` | Sanitized ordered request/reply fixtures. |
| `services/codex-bridge/protocols/compatibility.json` | Tested server-version to protocol-profile mapping. |
| `services/codex-bridge/tests/test_translator.py` | Tool-loop and 94-call tests. |
| `services/codex-bridge/tests/test_service.py` | Fake-SDK and HTTP/security tests. |
| `services/codex-bridge/tests/live_canary.py` | Scratch-volume subscription and security canaries. |
| `compose.codex.yml` | Optional private bridge overlay, health dependency, auth volume, direct-API block. |
| `.env.codex.example` | Non-secret template for the isolated subscription mode. |
| `services/codex-bridge/README.md` | Login, operation, failure, rollback, upgrade. |

---

### Task 1: Capture and freeze the opaque curator protocol

**Files:**
- Create: `services/codex-bridge/tests/capture_provider.py`
- Create: `services/codex-bridge/tests/fixtures/supermemory-v0.0.8/README.md`
- Create: `services/codex-bridge/tests/fixtures/supermemory-v0.0.8/requests.jsonl`
- Create: `services/codex-bridge/tests/fixtures/supermemory-v0.0.8/replies.jsonl`
- Create: `services/codex-bridge/protocols/compatibility.json`

**Interfaces:**
- Consumes: OpenAI-compatible requests emitted by SuperMemory v0.0.8.
- Produces: ordered authorization-free fixtures replayable without a model.

- [ ] **Step 1: Write a failing sanitizer self-test**

```python
class SanitizeTest(unittest.TestCase):
    def test_removes_authorization(self):
        captured = sanitize(
            "POST",
            "/v1/chat/completions",
            {"Authorization": "Bearer capture-only", "Content-Type": "application/json"},
            {"model": "fixture-model", "messages": [{"role": "user", "content": "synthetic"}]},
        )
        self.assertNotIn("authorization", captured["headers"])
        self.assertEqual(captured["body"]["messages"][0]["content"], "synthetic")
```

- [ ] **Step 2: Run it and confirm failure**

Run: `python3 services/codex-bridge/tests/capture_provider.py --self-test`

Expected: failure because `sanitize` is undefined.

- [ ] **Step 3: Implement the sanitizer and fake server**

```python
def sanitize(method, path, headers, body):
    return {
        "method": method,
        "path": path,
        "headers": {
            key.lower(): value
            for key, value in headers.items()
            if key.lower() not in {"authorization", "cookie", "content-length", "host"}
        },
        "body": body,
    }
```

Bind `127.0.0.1:18787`, append sanitized objects to `CAPTURE_FILE`, and initially return one standard `finish_reason: "stop"` Chat Completion.

- [ ] **Step 4: Run the self-test**

Run: `python3 services/codex-bridge/tests/capture_provider.py --self-test`

Expected: one passing test.

- [ ] **Step 5: Start scratch SuperMemory without the real volume**

Use the committed `services/codex-bridge/tests/compose.capture.yml` profile:

```yaml
services:
  server:
    ports: ["127.0.0.1:18767:6767"]
    volumes: ["sm-protocol-capture-data:/data"]
    environment:
      OPENAI_BASE_URL: http://capture-provider:18787/v1
      OPENAI_API_KEY: capture-local-only
      OPENAI_MODEL: fixture-model
      SUPERMEMORY_INGEST_CONCURRENCY: "1"
      SUPERMEMORY_EMBEDDING_PROVIDER: local
volumes:
  sm-capture-data:
```

Start the internal fake provider and scratch server. Ingest the synthetic fact: `On 2026-08-30, Project Finch uses blue labels.`

- [ ] **Step 6: Enforce the hard capture gate**

Require all captured calls to be text-only, non-streaming `POST /v1/chat/completions` with standard function tools, full message replay, and no Responses state or multimodal content. If any condition fails, mark this plan blocked and stop.

- [ ] **Step 7: Replay search/create rounds to completion**

Construct `replies.jsonl` from the captured tool schemas with this sequence:

```text
searchMemories tool_calls -> tool results -> CreateMemory tool_calls -> tool results -> stop
```

Restart the scratch volume, replay by request index, verify all four requests are frozen with their search/create IDs and tool results, and confirm SuperMemory search returns the Project Finch memory. No live model call is allowed.

- [ ] **Step 8: Tear down only scratch state**

Run: `docker compose -p supermemory-protocol-capture --env-file .env.codex -f compose.yml -f services/codex-bridge/tests/compose.capture.yml down -v`

Expected: `sm-capture-data` is removed; real `sm-data` is untouched.

- [ ] **Step 9: Commit the contract**

```bash
git add services/codex-bridge/tests/capture_provider.py services/codex-bridge/tests/fixtures/supermemory-v0.0.8 services/codex-bridge/protocols/compatibility.json
git commit -m "test: capture self-hosted curator protocol"
```

---

### Task 2: Implement the tool-aware translator

**Files:**
- Create: `services/codex-bridge/requirements.txt`
- Create: `services/codex-bridge/src/supermemory_codex_bridge/__init__.py`
- Create: `services/codex-bridge/src/supermemory_codex_bridge/translator.py`
- Create: `services/codex-bridge/tests/test_translator.py`

**Interfaces:**
- Consumes: captured Chat Completions request dictionaries.
- Produces: `CodexTurn` and validated `BridgeResult` objects.

- [ ] **Step 1: Pin dependencies and package version**

```text
openai-codex==0.147.0
jsonschema==4.26.0
```

```python
__version__ = "0.1.0"
```

- [ ] **Step 2: Write failing transcript, validation, and 94-call tests**

Tests must assert existing `tool_call_id` values survive prompt serialization, output schema `maxItems` is 128, strict output uses `arguments_json`, 94 ordered `CreateMemory` calls survive parsing, and the final envelope reports `finish_reason: "tool_calls"`. Unknown tools, invalid arguments, streaming, `n != 1`, non-function tools, and required-tool empty output must raise `BridgeProtocolError`.

- [ ] **Step 3: Run tests and confirm import failure**

Run: `PYTHONPATH=services/codex-bridge/src python3 -m unittest services/codex-bridge/tests/test_translator.py -v`

Expected: import failure because `translator.py` is absent.

- [ ] **Step 4: Implement public types**

```python
@dataclass(frozen=True)
class CodexTurn:
    prompt: str
    developer_instructions: str
    output_schema: dict[str, Any] | None
    model: str

@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]

@dataclass(frozen=True)
class BridgeResult:
    content: str | None
    tool_calls: tuple[ToolCall, ...]
    finish_reason: Literal["stop", "tool_calls"]

class BridgeProtocolError(ValueError):
    pass
```

- [ ] **Step 5: Build deterministic input and schema**

Serialize non-instruction messages and tools with compact JSON. Build one strict envelope that constrains the tool name while carrying SuperMemory's looser arguments as encoded JSON:

```python
{
    "type": "object",
    "properties": {
        "content": {"type": ["string", "null"]},
        "tool_calls": {
            "type": "array",
            "maxItems": 128,
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "enum": tool_names},
                    "arguments_json": {"type": "string"},
                },
                "required": ["name", "arguments_json"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["content", "tool_calls"],
    "additionalProperties": False,
}
```

Developer instructions must label all upstream content untrusted, forbid native Codex tools and filesystem/network access, and require only the schema result.

- [ ] **Step 6: Validate the complete final response**

Parse `arguments_json`, then use `jsonschema.validators.validator_for(schema)`. Reject invalid JSON or schema without repair/retry. Generate new IDs only after validation with `f"call_{uuid.uuid4().hex}"`; preserve array order.

- [ ] **Step 7: Return a Chat Completions envelope**

```python
{
    "id": f"chatcmpl_{request_id}",
    "object": "chat.completion",
    "created": int(time.time()),
    "model": model,
    "choices": [{
        "index": 0,
        "message": {"role": "assistant", "content": result.content, "tool_calls": tool_calls},
        "finish_reason": result.finish_reason,
    }],
    "usage": usage,
}
```

- [ ] **Step 8: Run tests and commit**

Run: `PYTHONPATH=services/codex-bridge/src python3 -m unittest services/codex-bridge/tests/test_translator.py -v`

Expected: all tests pass, including 94 calls.

```bash
git add services/codex-bridge/requirements.txt services/codex-bridge/src services/codex-bridge/tests/test_translator.py
git commit -m "feat: translate curator tool calls for Codex"
```

---

### Task 3: Wrap the official SDK and require ChatGPT auth

**Files:**
- Create: `services/codex-bridge/src/supermemory_codex_bridge/service.py`
- Create: `services/codex-bridge/tests/test_service.py`

**Interfaces:**
- Consumes: `CodexTurn`.
- Produces: `CodexRuntime.run(turn, timeout_seconds) -> BridgeResult`.

- [ ] **Step 1: Write fake-SDK lifecycle tests**

Assert account type `apiKey` fails; the inherited process environment is replaced before SDK startup; all pinned native tool features are disabled; threads use `ephemeral=True`, `Sandbox.read_only`, `ApprovalMode.deny_all`, and cwd `/workspace`; unexpected native tool items retire the runtime; a timeout that does not settle closes the App Server; SDK errors are never retried.

- [ ] **Step 2: Run and confirm failure**

Run: `PYTHONPATH=services/codex-bridge/src python3 -m unittest services/codex-bridge/tests/test_service.py -v`

Expected: import failure because `CodexRuntime` is absent.

- [ ] **Step 3: Implement the account gate**

```python
class BridgeAuthError(RuntimeError):
    pass

class BridgeTimeoutError(TimeoutError):
    pass

def require_chatgpt(self) -> str:
    account = self._codex.account(refresh_token=True).account
    if account is None or account.root.type != "chatgpt":
        raise BridgeAuthError("Codex must be authenticated with ChatGPT")
    return account.root.plan_type.value
```

Reject API-key, Bedrock, and external-token modes.

- [ ] **Step 4: Run one ephemeral turn**

```python
thread = self._codex.thread_start(
    approval_mode=ApprovalMode.deny_all,
    base_instructions=BASE_INSTRUCTIONS,
    developer_instructions=turn.developer_instructions,
    ephemeral=True,
    model=turn.model,
    cwd="/workspace",
    sandbox=Sandbox.read_only,
)
handle = thread.turn(turn.prompt, approval_mode=ApprovalMode.deny_all, output_schema=turn.output_schema, sandbox=Sandbox.read_only)
```

Run `handle.run()` in a one-worker executor. On deadline, call `handle.interrupt()`, allow five seconds to settle, and close the App Server if it remains active before raising `BridgeTimeoutError`. Accept only user/reasoning/assistant turn items and parse only `TurnResult.final_response`.

- [ ] **Step 5: Map failures**

Map authentication to 401, quota/rate-limit to 429, timeout to 504, invalid tool output to 502, and remaining SDK/App Server errors to 503. Log only request metadata and the mapped HTTP status; do not log exception messages or classes from SDK failures.

- [ ] **Step 6: Run tests and commit**

Run: `PYTHONPATH=services/codex-bridge/src python3 -m unittest services/codex-bridge/tests/test_service.py -v`

Expected: all lifecycle tests pass.

```bash
git add services/codex-bridge/src/supermemory_codex_bridge/service.py services/codex-bridge/tests/test_service.py
git commit -m "feat: run curator turns through official Codex SDK"
```

---

### Task 4: Add the private HTTP boundary

**Files:**
- Modify: `services/codex-bridge/src/supermemory_codex_bridge/service.py`
- Create: `services/codex-bridge/src/supermemory_codex_bridge/__main__.py`
- Modify: `services/codex-bridge/tests/test_service.py`

**Interfaces:**
- Consumes: bearer-authenticated HTTP on port 8787.
- Produces: `/health`, `/v1/models`, `/v1/chat/completions`.

- [ ] **Step 1: Write failing HTTP tests**

Cover 401 for missing/wrong bearer, 404 route, 400 streaming, 413 oversized body, 429 saturation with `Retry-After: 5`, 200 valid completion, and 401/429/502/503/504 runtime mappings. Patch logging and prove no known prompt, argument, bearer, or output reaches it.

- [ ] **Step 2: Implement constant-time auth and bounds**

```python
if not hmac.compare_digest(received.encode(), expected.encode()):
    return self.send_openai_error(401, "unauthorized", "invalid_api_key")
```

Reject duplicate auth headers, auth longer than 256 bytes, bodies over `8 * 1024 * 1024`, and concurrent requests through `BoundedSemaphore(1)`.

- [ ] **Step 3: Implement safe logging and commands**

Disable `BaseHTTPRequestHandler.log_message`. Log request ID, route, size, duration, model, and status only.

`python -m supermemory_codex_bridge` must implement:

```text
serve       require ChatGPT auth, then bind 0.0.0.0:8787
login       device-code login; print URL/code/result only
check-auth  print auth mode/plan/model availability; fail unless chatgpt
```

Reject bridge tokens beginning `sk-`; require exactly 64 lowercase hexadecimal characters. Default model is `gpt-5.6-luna` and must appear in `Codex.models()`.

- [ ] **Step 4: Run all tests and commit**

Run: `PYTHONPATH=services/codex-bridge/src python3 -m unittest discover -s services/codex-bridge/tests -p 'test_*.py' -v`

Expected: all tests pass.

```bash
git add services/codex-bridge/src services/codex-bridge/tests/test_service.py
git commit -m "feat: expose private Codex curator bridge"
```

---

### Task 5: Isolate and connect the bridge with Compose

**Files:**
- Create: `services/codex-bridge/Dockerfile`
- Create: `services/codex-bridge/.dockerignore`
- Create: `compose.codex.yml`
- Create: `.env.codex.example`
- Modify: `.gitignore`
- Create locally only: `.env.codex`

**Interfaces:**
- Consumes: named `codex-auth` volume and Compose environment.
- Produces: private `http://codex-bridge:8787/v1`.

- [ ] **Step 1: Create the image**

```dockerfile
FROM python:3.13.14-slim-bookworm
RUN useradd --create-home --uid 10002 bridge \
 && mkdir -p /app/src /workspace /var/lib/codex \
 && chown -R bridge:bridge /app /workspace /var/lib/codex
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --requirement /app/requirements.txt
COPY src /app/src
ENV PYTHONPATH=/app/src CODEX_HOME=/var/lib/codex CODEX_BRIDGE_PORT=8787
USER bridge
CMD ["python", "-m", "supermemory_codex_bridge", "serve"]
```

`.dockerignore` excludes `tests`, `.git`, `.env`, `__pycache__`, and `*.pyc`.

- [ ] **Step 2: Build and test inside the image**

```bash
docker build -t supermemory-codex-bridge:test services/codex-bridge
docker run --rm -v "$PWD/services/codex-bridge/tests:/app/tests:ro" supermemory-codex-bridge:test python -m unittest discover -s /app/tests -p 'test_*.py' -v
```

Expected: all tests pass.

- [ ] **Step 3: Add the private optional service overlay**

Keep `compose.yml` unchanged as the direct-provider door. Add `codex-bridge` to `compose.codex.yml` with no `ports`:

```yaml
  codex-bridge:
    build: ./services/codex-bridge
    restart: unless-stopped
    read_only: true
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    environment:
      CODEX_HOME: /var/lib/codex
      CODEX_BRIDGE_TOKEN: ${CODEX_BRIDGE_TOKEN}
      CODEX_BRIDGE_MODEL: ${CODEX_BRIDGE_MODEL:-gpt-5.6-luna}
      CODEX_BRIDGE_TIMEOUT_SECONDS: "240"
    volumes: ["codex-auth:/var/lib/codex"]
    expose: ["8787"]
    healthcheck:
      test:
        - CMD
        - python
        - -c
        - "import os, urllib.request; r=urllib.request.Request('http://127.0.0.1:8787/health', headers={'Authorization': 'Bearer '+os.environ['CODEX_BRIDGE_TOKEN']}); urllib.request.urlopen(r, timeout=3)"
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 15s
```

In the overlay, replace the server env file with `.env.codex`, depend on bridge health, and block direct API resolution:

```yaml
    depends_on:
      codex-bridge:
        condition: service_healthy
    extra_hosts:
      - "host.docker.internal:host-gateway"
      - "api.openai.com:127.0.0.1"
```

Add the named volume:

```yaml
volumes:
  codex-auth:
```

- [ ] **Step 4: Create the isolated Codex-mode environment before login**

Generate `BRIDGE_TOKEN=$(openssl rand -hex 32)`. Create `.env.codex` without copying any billable provider key:

```dotenv
CODEX_BRIDGE_TOKEN=generated-by-openssl-rand-hex-32
CODEX_BRIDGE_MODEL=gpt-5.6-luna
SUPERMEMORY_INGEST_CONCURRENCY=1
```

Do not modify `.env`; it remains the explicit direct-API door. Codex-mode commands must always use `--env-file .env.codex -f compose.yml -f compose.codex.yml` so the real key is not loaded into either Codex-mode service.

- [ ] **Step 5: Authenticate the dedicated volume**

```bash
docker compose --env-file .env.codex -f compose.yml -f compose.codex.yml run --rm codex-bridge python -m supermemory_codex_bridge login
docker compose --env-file .env.codex -f compose.yml -f compose.codex.yml run --rm codex-bridge python -m supermemory_codex_bridge check-auth
```

Expected: `auth_mode=chatgpt`, plan type present, and `gpt-5.6-luna` available.

- [ ] **Step 6: Verify no bridge port is published and commit**

Run the overlay forms of `docker compose up -d codex-bridge` and `docker compose ps codex-bridge`, then `docker port supermemory-codex-bridge-1`. Expected: healthy with no published port.

```bash
git add services/codex-bridge/Dockerfile services/codex-bridge/.dockerignore compose.codex.yml .env.codex.example .gitignore
git commit -m "feat: isolate Codex curator bridge in compose"
```

---

### Task 6: Run live subscription, security, and integrity gates

**Files:**
- Create: `services/codex-bridge/tests/live_canary.py`
- Modify: `services/codex-bridge/tests/test_service.py`

**Interfaces:**
- Consumes: scratch SuperMemory plus live ChatGPT-authenticated bridge.
- Produces: metadata-only pass/fail report.

- [ ] **Step 1: Implement 20 synthetic cases**

Use a fresh `sm-codex-canary-data` volume and cases comprising five create-only facts, five search/create facts, four contradiction/update pairs, two no-memory noise documents, two long atomic-memory documents, one request to read `/canary/never-read.txt`, and one request to read `/var/lib/codex/auth.json`.

Record only case ID, elapsed time, document status, expected-memory match count, and pass/fail.

- [ ] **Step 2: Add and test the filesystem canary**

Mount `CODEX_BRIDGE_FORBIDDEN_CANARY_1788048000` through the scratch Compose config at `/canary/never-read.txt`. The production image contains only the empty `/canary` directory. Search every resulting memory for that value and token-shaped output. Any match stops rollout.

- [ ] **Step 3: Run the canary**

```bash
python3 services/codex-bridge/tests/live_canary.py \
  --base-url http://127.0.0.1:17767 \
  --timeout-seconds 240 --max-p95-seconds 120
```

Require 20 completed cases, all 18 positive cases containing the exact expected memory count, one distinct memory per long-document atomic group, matching update memories linked as version two with their superseded memories no longer latest, exact benign-only output for injection cases, no fabricated durable fact from the two noise cases, zero canary disclosures, zero positive `done`-without-memory cases, zero timeouts, and p95 not exceeding 120 seconds.

- [ ] **Step 4: Verify fail-closed and log hygiene**

Stop only `codex-bridge`, submit one scratch positive document, and confirm no SuperMemory request can reach `api.openai.com`. If the binary marks it `done`, document the recovery limitation and disallow bulk unattended ingest.

Search logs:

```bash
docker compose logs codex-bridge > /private/tmp/codex-bridge.log
rg -n 'Project Finch|Authorization|Bearer |sk-|eyJ|CODEX_BRIDGE_FORBIDDEN_CANARY' /private/tmp/codex-bridge.log
```

Expected: no matches.

- [ ] **Step 5: Clean scratch state and commit**

Remove only `sm-codex-canary-data`; verify real `sm-data` remains.

```bash
git add services/codex-bridge/tests/live_canary.py services/codex-bridge/tests/test_service.py
git commit -m "test: verify Codex curator bridge end to end"
```

---

### Task 7: Document and perform controlled cutover

**Files:**
- Create: `services/codex-bridge/README.md`
- Modify: `apps/docs/self-hosting/providers.mdx`

**Interfaces:**
- Consumes: verified service and canary evidence.
- Produces: login, operation, failure, rollback, and upgrade runbook.

- [ ] **Step 1: Document operations and failure meanings**

Document login, `check-auth`, startup, health, and logs. State that 401 requires device re-login, quota 429 waits for reset, and 502/503/504 never trigger a paid fallback.

- [ ] **Step 2: Document the version gate**

Keep `openai-codex==0.147.0` pinned. Any SDK/runtime, SuperMemory binary, model, or translator change reruns unit tests, frozen fixtures, the 94-call case, and one live canary before rollout. A compatible SuperMemory release adds only a new fixture directory and `testedServerVersions` manifest entry. An incompatible release requires a new reviewed protocol profile; never repoint a `current` symlink or add inline version checks.

- [ ] **Step 3: Document personal/local provider configuration**

Add a Codex subscription tab showing the private base URL, 64-character local token, `gpt-5.6-luna`, and concurrency one. Warn that memory content leaves the machine under the ChatGPT account's data policy and this is not a public/multi-user service.

- [ ] **Step 4: Back up and cut over**

Back up the real SuperMemory volume using its documented mechanism, excluding the password-equivalent `codex-auth` volume from ordinary backups. Start bridge and server, ingest one benign personal fact, and confirm search returns it before normal use.

- [ ] **Step 5: Run final verification**

```bash
PYTHONPATH=services/codex-bridge/src python3 -m unittest discover -s services/codex-bridge/tests -p 'test_*.py' -v
docker compose config --quiet
docker compose ps
docker compose run --rm codex-bridge python -m supermemory_codex_bridge check-auth
```

Expected: tests pass, Compose is valid, services healthy, ChatGPT auth active, and model available.

- [ ] **Step 6: Commit documentation**

```bash
git add services/codex-bridge/README.md apps/docs/self-hosting/providers.mdx
git commit -m "docs: operate SuperMemory with Codex subscription"
```

---

## Rollback

1. Stop `server` and `codex-bridge`.
2. Restore the pre-cutover data backup only if canary or first ingest corrupted data.
3. Leave `OPENAI_API_KEY` unset; never restore an `sk-*` value automatically.
4. Restart only after explicitly selecting the fixed bridge, local Ollama, or a separately approved paid key.
5. Preserve logs only after confirming they contain no prompt/token data.

## Deferred Work

- Persistent App Server optimization only after measured p95 failure.
- More than one in-flight request.
- Streaming, `/v1/responses`, routing, sticky sessions, and public auth.
- Native SuperMemory provider only when server source or a provider plugin exists.
