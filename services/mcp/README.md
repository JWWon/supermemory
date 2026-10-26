# Private Supermemory MCP

Small, stateless MCP access to the existing self-hosted Supermemory API. It does
not extract memories, run a curator, maintain an active-space database, or add
another credential store. Supermemory validates the client's existing API key
through `/v3/session`, as in the upstream MCP implementation.

The pinned official MCP SDK serves both 2025 clients and protocol `2026-07-28`.
The implementation follows [upstream MCP tool names](https://github.com/supermemoryai/supermemory/tree/main/apps/mcp), with explicit container scope
and no Cloudflare/OAuth/widget dependencies.

| Tool | Purpose |
|---|---|
| `who_am_i` | Authentication/service identity; excludes session tokens |
| `list_spaces` | Available container names |
| `search_memory` | Scoped semantic memory search |
| `get_profile` | Scoped stable/recent profile and optional search |
| `list_documents` | Scoped document metadata and processing state |
| `get_document` | Document content, checked against the requested container |
| `list_memories` | Scoped extracted memory IDs/content |
| `add_memory` | Queue content for extraction, or forget an exact ID/content |

Memory operations require `containerTag`. Honor explicit repository/company
mappings; use `brain_canonical` only for personal work. Historical containers
remain available through explicit lookup and are not merged or relabeled.
`get_document` limits content to 200,000 characters and reports truncation.
`add_memory(action="forget")` does not perform a similarity-based deletion.
Passing `memoryId` chooses that exact memory; otherwise `content` must match.

## Deployment

The normal Compose stack includes this service on the private container network.
Caddy exposes `/mcp` through the same existing Tailscale HTTPS origin as the REST
API; there is no published MCP container port or additional public listener.

Set `SUPERMEMORY_MCP_URL` in the Compose env file to the public URL, then:

```bash
docker compose --env-file .env.codex -f compose.yml -f compose.codex.yml build mcp
docker compose --env-file .env.codex -f compose.yml -f compose.codex.yml up -d --no-deps mcp
docker compose --env-file .env.codex -f compose.yml -f compose.codex.yml up -d --no-deps --force-recreate caddy
```

Recreating Caddy refreshes its single-file bind mount after Git replaces the
Caddyfile. The existing server and database do not need to restart.

Runtime configuration:

- `SUPERMEMORY_API_URL`: internal REST base URL, normally `http://server:6767`.
- `MCP_PUBLIC_URL`: expected public MCP URL, used for Host/Origin validation.
- `HOST` / `PORT`: local bind address; standalone defaults to loopback / 8788.
- `RERANK_URL` / `RERANK_MODEL`: optional reranker for `search_memory`, normally
  the host's oMLX `http://host.docker.internal:11434/v1/rerank` with
  `bge-reranker-v2-m3`. Remove `RERANK_URL` to return plain search.

### Reranked search

With a reranker configured, `search_memory` asks Supermemory for up to 30
candidates at similarity 0.3, scores them with the cross-encoder, drops scores
below 0.01, and returns the top `limit` with a `rerankScore` on each result and
`reranked: true`. If the reranker fails or takes longer than 8 seconds, it
returns what plain search would have (similarity 0.6 or higher, search order)
with `reranked: false`. Memory text goes only to the configured reranker; the
Supermemory key never does.

Calibrated on 520 Korean and English questions against the personal store:
top-1 recall rose from 57.1% to 71.9% and top-10 from 86.5% to 93.5%, and the
0.01 floor never removed a correct memory. Reranking adds about 0.7 seconds per
search (about 1.75 seconds when the model has to load first).

### Reranked automatic recall

The official Claude and Codex hooks recall through `POST /v4/profile` with
`{containerTag, q}` and read `searchResults.results`. Caddy sends that one route
to this service (`lb_policy first`, with `server:6767` as the fallback upstream,
so recall survives this service being down). With a reranker configured it
forwards the request to the server unchanged for the profile, fetches 30
candidates at similarity 0.3, reranks them, and replaces `searchResults.results`
with the top 15 (each with a `rerankScore`; `similarity` stays the embedding
score because the hooks cut off on it). If the reranker fails or the 2.5 second
deadline passes (the hooks give up at 3 to 4 seconds; a reranker that has to
load takes about 3), the server's own response is returned untouched. Requests
without a query pass through.

Checked on 2026-10-27 with 66 generated questions (33 Korean, 33 English) against
the live store, applying each hook's own cutoff (Claude: similarity 0.55, 15
results; Codex: 0.6, 5 results): Claude top-1 38 to 44, hit@15 55 to 59; Codex
top-1 36 to 42, hit@5 49 to 54. Recall time rose from about 0.3 to 1.4 seconds at
the median while the server was saturated by a reprocessing loop.

Requests require the existing `sm_...` Supermemory API key. Keys are forwarded
only to the fixed first-party API, never to URLs supplied by a tool call;
redirects are rejected. The service does not implement cloud OAuth or MCP Apps.
`/health` is container liveness; authenticated calls also check API availability.

## One lookup entry point per agent

Use one `supermemory` MCP registration pointing to this deployment. Keep one
official automatic recall/capture hook per event. Remove duplicate wrappers,
stale hosted-MCP registrations, and CLI lookup/status skills that duplicate the
MCP tools. Keep repository scope mappings and the official capture helper.
Back up configuration before edits; never copy API keys into Git or messages.

For Codex, prefer native HTTP MCP with `http_headers_helper` reading the existing
`~/.codex/supermemory.json` credential, so key rotation has one source:

```toml
[mcp_servers.supermemory]
url = "https://YOUR-TAILNET-HOST:8443/mcp"
http_headers_helper = """node -e 'const fs=require("node:fs"),p=require("node:path"),os=require("node:os");const c=JSON.parse(fs.readFileSync(p.join(os.homedir(),".codex","supermemory.json"),"utf8"));if(!c.apiKey)process.exit(1);process.stdout.write(JSON.stringify({Authorization:"Bearer "+c.apiKey}));'"""
enabled = true
startup_timeout_sec = 20
tool_timeout_sec = 30
```

The installed official Codex and Claude Code stdio proxies also support
`SUPERMEMORY_MCP_URL`. Set it in that MCP process's environment when using a
proxy; do not edit installed plugin code. Avoid registering both the proxy and
a direct HTTP connection in the same agent.

Adding MCP does not implement missing REST endpoints such as the local binary's
absent `/v4/conversations`. Client session-capture compatibility is separate.

## Verification

```bash
cd services/mcp
bun install --frozen-lockfile --ignore-scripts
bun test
bun run verify                 # authenticated discovery and empty-scope profile
bun run verify -- --legacy    # older protocol, same endpoint
bun run verify -- --write     # isolated write/extract/read/search/forget + cleanup
```

`verify.ts` reads the existing Codex credential file. `SUPERMEMORY_CONFIG` can
select another compatible JSON file; `SUPERMEMORY_API_URL` and
`SUPERMEMORY_MCP_URL` can select the deployment. Except for loopback testing,
both URLs must share an origin. The write test deletes only the document it
created. Never run a destructive test against an existing user memory.

Unit/integration tests use a fake API and actual MCP clients for both protocol
eras. They check authentication, Host/Origin rejection, explicit scope,
cross-container document rejection, and exact forget forwarding. Live checks
verify the released binary's API shapes and real extraction separately.
