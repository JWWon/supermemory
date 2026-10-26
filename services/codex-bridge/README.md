# SuperMemory Codex subscription bridge

Personal, single-host adapter that keeps SuperMemory's curator loop unchanged
while routing its model calls through OpenAI's official Codex Python SDK and a
ChatGPT subscription.

The supported deployment uses SuperMemory `0.0.8`, protocol
`chat-completions-tools-v1`, `openai-codex` and its bundled CLI `0.156.1`, and
`gpt-6-luna` with `high` reasoning effort. SDK updates require rebuilding the
bridge image; updating Codex on the host does not update this container.

SuperMemory owns extraction, memory-tool execution, graph updates, and retrieval.
The adapter returns model proposals only. It accepts the captured `max_tokens`
and `serviceTier` fields for compatibility, but does not forward equivalent
Codex token limits or service-tier settings. Unsupported protocol shapes return
HTTP 400; this is not a general OpenAI-compatible endpoint.

## Two-way door

Direct provider/API-key mode remains unchanged:

```bash
docker compose up -d
```

Codex subscription mode adds the private overlay:

```bash
docker compose --env-file .env.codex \
  -f compose.yml -f compose.codex.yml up -d
```

The bridge has no published host port. SuperMemory reaches it only at
`http://codex-bridge:8787/v1` on the Compose network.

## Configure

```bash
cp .env.codex.example .env.codex
openssl rand -hex 32
```

Put the generated 64-character lowercase hexadecimal value in
`CODEX_BRIDGE_TOKEN`. Do not put an `sk-*` key or any billable LLM provider key
in `.env.codex`.

Keep the existing embedding settings and dimensions in `.env.codex`. Changing
embedding dimensions for an existing `sm-data` volume is unsafe.

Memory content sent through this mode leaves the host for OpenAI processing
under the authenticated ChatGPT account. Get the data owner's explicit consent
before enabling the overlay on an existing volume.

## Authenticate

The official SDK stores ChatGPT credentials in the dedicated `codex-auth`
volume:

```bash
docker compose --env-file .env.codex \
  -f compose.yml -f compose.codex.yml \
  run --rm --no-deps codex-bridge \
  python -m supermemory_codex_bridge login
```

Open the displayed verification URL and enter the user code. Verify auth and
model availability:

```bash
docker compose --env-file .env.codex \
  -f compose.yml -f compose.codex.yml \
  run --rm --no-deps codex-bridge \
  python -m supermemory_codex_bridge check-auth
```

Expected output includes:

```text
auth_mode=chatgpt
plan_type=<your-chatgpt-plan>
model=gpt-6-luna
```

Never copy `codex-auth` into ordinary project backups. It contains
password-equivalent refresh credentials.

## Back up and cut over

Stop the direct-mode server long enough to archive its data volume consistently:

```bash
mkdir -p backups
docker compose --env-file .env -f compose.yml stop server
docker run --rm --read-only \
  -v supermemory_sm-data-009:/data:ro \
  -v "$PWD/backups:/backup" \
  caddy:2-alpine \
  tar -czf /backup/supermemory-sm-data-pre-codex.tar.gz -C /data .
```

Confirm the archive is non-empty, then start Codex mode:

```bash
tar -tzf backups/supermemory-sm-data-pre-codex.tar.gz | head
docker compose --env-file .env.codex \
  -f compose.yml -f compose.codex.yml \
  up -d --build codex-bridge server
```

If you stop before cutover, restore the unchanged direct-mode server with
`docker compose --env-file .env -f compose.yml start server`.

## Start and inspect

```bash
docker compose --env-file .env.codex \
  -f compose.yml -f compose.codex.yml up -d
docker compose --env-file .env.codex \
  -f compose.yml -f compose.codex.yml ps
docker compose --env-file .env.codex \
  -f compose.yml -f compose.codex.yml logs --since 10m codex-bridge
```

Bridge logs contain only request ID, route, byte count, duration, model, and
status. Prompt text, tool arguments, results, credentials, and SDK stderr must
never appear.

## Errors

| HTTP | Meaning | Action |
|---:|---|---|
| 400 | Request differs from the captured protocol | Stop and recapture the SuperMemory version. |
| 401 | ChatGPT auth missing/expired | Run device login again. |
| 413 | Request exceeds 8 MiB | Do not broaden without a design review. |
| 413 | Prompt over the model's context window (Codex compacts it) or over Codex's 1,048,576-character input limit | The runtime stays up and nothing is retired. The server fails that memory-agent run; a document that keeps growing will keep failing. |
| 429 | Bridge busy or subscription quota reached | Wait for the request/reset; do not re-login for quota. |
| 502 | Codex returned invalid tool JSON | Inspect metadata-only logs and run the fixture suite. |
| 503 | SDK/App Server unavailable | An irrecoverable runtime exits after returning the error; Compose restarts it. |
| 504 | SDK setup or generation exceeded 240 seconds | The runtime is retired and the bridge restarts. Inspect the affected document; do not automatically replay it. |

SuperMemory can mark failed extraction as `done` with zero memories. Do not run
bulk unattended ingestion until the canary suite passes, and verify positive
documents through memory search rather than status alone.

## Tests

```bash
PYTHONPATH=services/codex-bridge/src:services/codex-bridge/tests \
  python3 -m unittest discover -s services/codex-bridge/tests -p 'test_*.py' -v
```

Container test:

```bash
docker compose --env-file .env.codex \
  -f compose.yml -f compose.codex.yml \
  run --rm --no-deps \
  -v "$PWD/services/codex-bridge/tests:/app/tests:ro" \
  codex-bridge python -m unittest discover -s /app/tests -p 'test_*.py' -v
```

### Real-runtime checks without credentials or network

The runtime canary uses the pinned SDK, a synthetic loopback provider, and only
non-secret model metadata from the running bridge. It checks JSON summaries,
tool proposals, the effective model/reasoning settings, current tool controls,
and refusal of native code/shell/patch attempts against a dummy private file.

```bash
docker compose --env-file .env.codex -f compose.yml -f compose.codex.yml \
  exec -T codex-bridge python -c \
  'import json; d=json.load(open("/var/lib/codex/models_cache.json")); print(json.dumps({"models":[m for m in d["models"] if m.get("slug")=="gpt-6-luna"]}))' \
  | docker run --rm -i --network none --read-only --cap-drop ALL \
      --security-opt no-new-privileges:true \
      --tmpfs /tmp:rw,nosuid,nodev,size=128m,mode=1777 \
      -v "$PWD/services/codex-bridge/tests:/app/tests:ro" \
      supermemory-codex-bridge python /app/tests/runtime_canary.py
```

Luna uses Responses Lite: tool registration can be carried in
`tool_namespaces_info` even when there is no ordinary `tools` array. Tests must
inspect that metadata and actual refusal behavior. Model-driven asynchronous
questions and code-mode tools can remain registered; the adapter disables
delegation, sleep, ordinary questions, native shell access, and the code-mode
host and its in-process fallback. A registered tool is not proof of execution.

The complete request deadline includes `thread_start`, `thread.turn`, and
generation. On timeout the official SDK process is closed; there is no separate
interrupt call that can hang outside the deadline. Fatal failures stop the HTTP
server after its error response is flushed, allowing the existing Compose
restart policy to recover without a new supervisor or retry queue.

### Fresh isolated gates

Start the bridge against a disposable SuperMemory volume; do not include the
real `sm-data` volume:

```bash
docker compose -p supermemory-codex-canary --env-file .env.codex \
  -f compose.yml -f compose.codex.yml \
  -f services/codex-bridge/tests/compose.canary.yml \
  up -d --build codex-bridge server

PYTHONPATH=services/codex-bridge/tests \
  python3 services/codex-bridge/tests/live_canary.py \
  --base-url http://127.0.0.1:17767 \
  --timeout-seconds 240 --max-p95-seconds 120
```

The suite must report 20 cases, zero failures, and p95 at or below 120
seconds. It verifies every atomic long-document fact, linkage on each matching
update, superseded memories no longer marked latest, zero memories for noise,
searchability, and exact benign-only output for file/auth injection probes.

This suite evaluates model and SuperMemory behavior as well as integration.
Keep failures classified separately from adapter contract regressions: an exact
memory wording, graph-linkage, or retrieval-quality mismatch does not authorize
changing memory semantics in the adapter. For adapter-only changes, run the
unit/runtime checks and a scratch ingestion/retrieval smoke. Use the full suite
when evaluating a model or server change.

For the fail-closed gate, stop only `codex-bridge`, submit a fresh synthetic
document to port 17767, and poll its returned document ID. It must finish
`failed` with zero memories. Restart the bridge afterward:

```bash
docker compose -p supermemory-codex-canary --env-file .env.codex \
  -f compose.yml -f compose.codex.yml \
  -f services/codex-bridge/tests/compose.canary.yml stop codex-bridge

docker compose -p supermemory-codex-canary --env-file .env.codex \
  -f compose.yml -f compose.codex.yml \
  -f services/codex-bridge/tests/compose.canary.yml start codex-bridge
```

Scan metadata-only logs before cleanup. Any match is a hard failure:

```bash
docker compose -p supermemory-codex-canary --env-file .env.codex \
  -f compose.yml -f compose.codex.yml \
  -f services/codex-bridge/tests/compose.canary.yml logs codex-bridge \
  | rg 'CODEX_BRIDGE_FORBIDDEN_CANARY|eyJ[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9_-]{12,}|\b[0-9a-f]{64}\b'
```

Remove only the disposable stack and its scratch data when finished:

```bash
docker compose -p supermemory-codex-canary --env-file .env.codex \
  -f compose.yml -f compose.codex.yml \
  -f services/codex-bridge/tests/compose.canary.yml down -v
```

### Recapture the opaque server protocol

The capture profile replays the canned four-round response sequence without
calling a model:

```bash
docker compose -p supermemory-protocol-capture --env-file .env.codex \
  -f compose.yml \
  -f services/codex-bridge/tests/compose.capture.yml \
  up -d capture-provider server
```

Submit the documented synthetic Project Finch sentence to port 18767, poll it
to `done`, then copy `/output/requests.jsonl` from the capture-provider
container. It must contain four lines and preserve all search/create call IDs
and tool results. The translator fixture test asserts that transcript. Remove
the scratch project with the same Compose files and `down -v`.

## Upgrade SuperMemory

The bridge core has no SuperMemory version branches. Compatibility lives in
`protocols/compatibility.json` and frozen fixtures.

For every server upgrade:

1. Run the new binary against `tests/capture_provider.py` with a scratch volume.
2. Replay canned summary, search, create, and finish responses without a model.
3. Compare the normalized contract with `chat-completions-tools-v1`.
4. If compatible, add the new server version and fixture path to the manifest.
5. If incompatible, stop and design a new protocol profile; never repoint a
   symlink or add inline version checks.
6. Run unit tests, the ordered 94-call regression, filesystem canaries, and one
   benign live ingest before cutover.

## Switch back

```bash
docker compose --env-file .env.codex \
  -f compose.yml -f compose.codex.yml down
docker compose up -d
```

This reuses the original `.env` direct-provider configuration. Switching modes
does not change SuperMemory's curator implementation or stored data.
