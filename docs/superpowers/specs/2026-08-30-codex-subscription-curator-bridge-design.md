# Codex Subscription Curator Bridge Design

**Status:** Approved for implementation planning

**Date:** 2026-08-30

**Owner:** Jiwoon Won
**Scope:** Personal, single-host, self-hosted SuperMemory only

## Decision

Use OpenAI's official `openai-codex==0.147.0` Python SDK to run Codex through a ChatGPT subscription. Place a narrow OpenAI Chat Completions adapter between the opaque `supermemory-server` binary and that SDK.

Do not depend on `mehdic/codex-proxy`, copy Pi/Hermes OAuth code, call the private ChatGPT Codex backend, or fork unavailable SuperMemory server source. Community projects remain references and comparison fixtures only.

## Why this boundary

The local SuperMemory binary accepts `OPENAI_BASE_URL`, `OPENAI_API_KEY`, and model overrides, but its implementation is not in this checkout. The Dockerfile downloads a pinned binary. The stable seam is the OpenAI-compatible HTTP boundary, not an internal provider interface.

OpenAI officially publishes `codex exec`, the Codex SDK, and App Server. The Python SDK is the smallest supported surface that owns ChatGPT authentication, token refresh, App Server lifecycle, typed events, ephemeral threads, structured output, interruption, model discovery, and a pinned Codex runtime.

SuperMemory's curator is not a one-shot JSON generator. Field reports show a multi-round loop containing `searchMemories` calls followed by large batches of `CreateMemory` calls, including a batch of 94. The adapter must preserve that tool protocol; returning prose or schema-only content is insufficient.

## Goals

- Use the user's ChatGPT/Codex subscription for curator inference.
- Eliminate ordinary OpenAI API-key billing from SuperMemory.
- Preserve tool-call IDs, ordering, arguments, results, and finish reasons.
- Keep Codex authentication inside the official SDK and dedicated `CODEX_HOME`.
- Run privately on the Compose network with no published bridge port.
- Fail closed on unsupported protocol, invalid output, missing ChatGPT auth, quota exhaustion, and saturation.
- Leave local embeddings unchanged.

## Non-goals

- A public or multi-user proxy.
- General `/v1/responses`, embeddings, media, files, batches, or arbitrary OpenAI compatibility.
- Direct OAuth/token handling.
- A paid API fallback.
- Parallel workers, sticky threads, or model routing.
- Repairing unrelated opaque-binary behavior.

## Architecture

```text
client
  -> supermemory-server container
  -> POST /v1/chat/completions
  -> private supermemory-codex-bridge container
  -> official openai-codex Python SDK
  -> SDK-pinned Codex App Server runtime
  -> ChatGPT/Codex subscription
```

The bridge gets a dedicated named volume at `/var/lib/codex` for `CODEX_HOME`; no host home or project directory is mounted. `/workspace` is empty.

The deployment remains a two-way door. Plain `compose.yml` keeps the existing
direct-provider configuration. Adding `compose.codex.yml` switches only the
model transport, replaces the server env file with `.env.codex`, blocks direct
OpenAI API resolution, and adds the private bridge dependency. Neither mode
requires editing curator code.

## Protocol gate

Before implementation, run SuperMemory v0.0.8 against a capture provider with a temporary data volume and synthetic text. Freeze the complete request sequence and replay canned responses until a document completes without a model call.

Implementation proceeds only if the contract is:

- `POST /v1/chat/completions`;
- text-only and non-streaming;
- stateless at HTTP level, with full prior messages resent;
- standard function tools and tool-result messages;
- free of `previous_response_id`, hosted tools, and multimodal dependencies.

Any mismatch blocks this plan and requires a new design review.

## Version compatibility

The bridge core is version-blind. It validates the HTTP request it actually
receives and contains no `if server_version == ...` branches. A data-only
compatibility manifest maps tested SuperMemory releases to protocol profiles
and frozen fixtures.

```text
SuperMemory 0.0.8 -> chat-completions-tools-v1 -> v0.0.8 fixtures
future compatible release -> chat-completions-tools-v1 -> new release fixtures
future incompatible release -> new reviewed protocol profile
```

Upgrading SuperMemory requires a scratch capture and canned replay. If the
normalized wire contract matches `chat-completions-tools-v1`, add the release
to the manifest without touching translator code. If it differs, keep the
existing profile intact and design a new explicit profile. A `current` symlink
is not used for runtime selection because it could silently route an untested
binary to incompatible behavior.

## Translation contract

For each request, the adapter:

1. Separates upstream system/developer instructions from the transcript.
2. Serializes user, assistant, tool-call, and tool-result messages without altering existing IDs or order.
3. Converts function tools into one strict bridge-owned JSON Schema with a tool-name enum, JSON-encoded `arguments_json`, and `maxItems: 128`. This avoids passing SuperMemory's looser function schemas through Codex's stricter structured-output subset. For the unconstrained `json_object` summary call, it omits Codex structured output and validates the returned JSON object locally.
4. Starts a fresh ephemeral Codex thread with fixed instructions, `ApprovalMode.deny_all`, `Sandbox.read_only`, empty cwd, the configured subscription model, and all pinned native tool features disabled.
5. Parses only `TurnResult.final_response`.
6. Parses `arguments_json`, then validates each returned name and argument object against the original tool schema.
7. Generates IDs for new calls, preserves order, and returns `finish_reason: "tool_calls"`; text-only output returns `finish_reason: "stop"`.

When `tool_choice` requires a tool, zero calls is an error. The bridge never converts an expected tool call into prose.

## Authentication and billing

- Start only when `Codex.account()` reports account type `chatgpt`.
- Perform device-code login inside the bridge container.
- Give SuperMemory a random 256-bit local bearer through `OPENAI_API_KEY`; it must not begin with `sk-`.
- Fix `OPENAI_BASE_URL` to `http://codex-bridge:8787/v1`.
- Resolve `api.openai.com` to loopback inside the SuperMemory container.
- Keep no valid billable provider credential in either service.
- Clear the bridge process environment down to an explicit runtime allowlist before the official SDK starts; `CodexConfig.env` alone is insufficient because SDK 0.147.0 overlays it on inherited variables.
- Never fall back automatically.

## Security

- No bridge host port.
- Constant-time bearer comparison.
- 8 MiB request cap.
- Only `/health`, `/v1/models`, and `/v1/chat/completions`.
- One Codex request at a time; concurrent requests get 429.
- Metadata-only logs; never log prompts, arguments, results, auth, stderr, or model output.
- Read-only container root filesystem, no Linux capabilities, and no-new-privileges.
- Read-only, approval-free, ephemeral Codex thread rooted at empty `/workspace`, with every pinned execution/search surface explicitly disabled and per-thread web search configured `disabled`.
- Any turn item other than user, reasoning, or final assistant messages retires the runtime and fails the request.
- A prompt-injection canary must prove Codex cannot return the auth file, environment, or a file outside `/workspace`.

## Failure behavior

| Condition | Result |
|---|---:|
| Wrong bearer | 401 |
| Unsupported field/route/streaming | 400/404 |
| Body over 8 MiB | 413 |
| Busy | 429 with `Retry-After: 5` |
| ChatGPT auth missing | 401 |
| Subscription quota exhausted | 429 |
| Invalid tool output | 502 |
| Codex timeout | 504 after interrupt; retire App Server if it does not settle |
| SDK/App Server unavailable | 503 |

The bridge never retries ambiguous turns. Tests must detect SuperMemory's known `done`-with-zero-memories failure mode instead of trusting document status.

## Acceptance criteria

- Captured v0.0.8 fixtures replay to a completed synthetic document with no model call.
- Tests preserve IDs, ordering, and tool-result messages.
- A 94-call `CreateMemory` response round-trips unchanged.
- Unknown tools and invalid arguments fail.
- Twenty synthetic live ingests cover create-only, search/create, contradiction/update, intentional no-memory, and prompt-injection cases.
- Update canaries require version-two parent linkage; long cases require one distinct memory per atomic fact with no extras; injection cases accept only the exact benign memory; p95 must not exceed 120 seconds.
- A positive fact never passes as `done` with zero expected memories.
- Stopping the bridge cannot trigger a direct OpenAI API request.
- No provider or bridge credential exists in the actual App Server child environment.
- Logs contain no prompts or credentials.
- The filesystem canary cannot be read or returned.
- Version/model changes rerun offline fixtures and a live canary.
- Every supported SuperMemory version appears in the compatibility manifest.

## Hard stops

- Capture does not match the narrow contract.
- IDs or ordering cannot be preserved.
- Codex returns prose where tools are required.
- The 94-call batch fails.
- Codex can return data outside `/workspace`.
- Any valid OpenAI API key remains available to SuperMemory.
- A positive synthetic document silently finishes without its expected memory.
- The adapter starts growing into a generic proxy.

## Sources

- [OpenAI: Codex as a platform](https://developers.openai.com/blog/codex-as-a-platform)
- [Official Codex Python SDK](https://github.com/openai/codex/tree/main/sdk/python)
- [Official Codex App Server](https://learn.chatgpt.com/docs/app-server)
- [SuperMemory tool-batch evidence](https://github.com/supermemoryai/supermemory/issues/1300)
- [SuperMemory zero-memory evidence](https://github.com/supermemoryai/supermemory/issues/1301)
