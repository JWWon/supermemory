# SuperMemory v0.0.8 curator fixture

Captured from an isolated `supermemory-server` v0.0.8 container using local embeddings, a temporary data volume, and synthetic Project Finch content.

Observed contract:

- `POST /v1/chat/completions`
- non-streaming
- first call: `response_format.type = json_object` for container description
- second and later calls: standard Chat Completions function tools
- full assistant tool calls and `role=tool` results are resent on continuation
- no Responses API state or multimodal content

`requests.jsonl` freezes all four observed requests. `replies.jsonl` replays
container summary, three searches, one `CreateMemory`, and final completion
without a model call. `tests/compose.capture.yml` reproduces the capture on a
fresh volume.

The adapter does not import this directory or branch on `0.0.8`. The data-only
`protocols/compatibility.json` manifest records that these fixtures prove the
`chat-completions-tools-v1` profile for this server release.
