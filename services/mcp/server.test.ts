import { afterAll, expect, test } from "bun:test";
import { Client, StreamableHTTPClientTransport } from "@modelcontextprotocol/client";
import { createApp } from "./server";

const key = "sm_test_only_0123456789abcdef";
const calls: { method: string; path: string; body: any }[] = [];
const backend = Bun.serve({ hostname: "127.0.0.1", port: 0, async fetch(request) {
  const path = new URL(request.url).pathname;
  if (request.headers.get("authorization") !== `Bearer ${key}`)
    return Response.json({ error: "bad key" }, { status: 401 });
  const body = request.method === "GET" ? null : await request.json();
  calls.push({ method: request.method, path, body });
  if (path === "/v3/session") return Response.json({ user: { id: "user", name: "Test" },
    org: { id: "org", name: "Test" }, session: { token: "DO_NOT_EXPOSE" } });
  if (path === "/v3/container-tags/list") return Response.json([{ containerTag: "personal", name: "Personal" }]);
  if (path === "/v4/search") return Response.json({ results: [
    { id: "memory", memory: "Synthetic indigo fact", similarity: 0.9 },
    { id: "violet", memory: "Synthetic violet fact", similarity: 0.7 },
    { id: "noise", memory: "Unrelated note", similarity: 0.4 },
  ], total: 3 });
  if (path === "/v4/profile") return Response.json({ profile: { static: ["Synthetic fact"], dynamic: [] },
    searchResults: { results: [{ id: "noise", memory: "Unrelated note", similarity: 0.9 }], total: 1, timing: 7 } });
  if (path === "/v3/documents/list") return Response.json({ memories: [{ id: "doc" }], pagination: { currentPage: 1 } });
  if (path === "/v4/memories/list") return Response.json({ memoryEntries: [{ id: "memory" }], pagination: { currentPage: 1 } });
  if (path === "/v3/documents/doc") return Response.json({ id: "doc", containerTags: ["personal"], content: "PRIVATE_DOCUMENT" });
  if (path === "/v3/documents") return Response.json({ id: "created" });
  if (path === "/v4/memories" && request.method === "DELETE") return Response.json({ id: "memory", forgotten: true });
  return Response.json({ error: "not found" }, { status: 404 });
} });
const endpoint = Bun.serve({ hostname: "127.0.0.1", port: 0,
  fetch: createApp(backend.url.toString(), "http://127.0.0.1/mcp") });
const url = new URL("/mcp", endpoint.url);

// Fake cross-encoder: prefers "violet", rejects "Unrelated", or fails when told to.
let rerankerDown = false;
let rerankerSlow = false;
const rerankCalls: { authorization: string | null; body: any }[] = [];
const reranker = Bun.serve({ hostname: "127.0.0.1", port: 0, async fetch(request) {
  const body = await request.json();
  rerankCalls.push({ authorization: request.headers.get("authorization"), body });
  if (rerankerSlow) await Bun.sleep(3500);
  if (rerankerDown) return new Response("down", { status: 503 });
  const score = (doc: string) => doc.includes("violet") ? 0.95 : doc.includes("indigo") ? 0.5 : 0.0001;
  return Response.json({ results: body.documents.map((doc: string, index: number) => ({ index, relevance_score: score(doc) })) });
} });
const rerankingEndpoint = Bun.serve({ hostname: "127.0.0.1", port: 0,
  fetch: createApp(backend.url.toString(), "http://127.0.0.1/mcp", new URL("/v1/rerank", reranker.url).toString()) });
afterAll(() => { endpoint.stop(true); rerankingEndpoint.stop(true); reranker.stop(true); backend.stop(true); });

test("search_memory reranks a wider candidate set and falls back to plain search", async () => {
  const client = new Client({ name: "rerank-test", version: "1" });
  await client.connect(new StreamableHTTPClientTransport(new URL("/mcp", rerankingEndpoint.url), {
    requestInit: { headers: { Authorization: `Bearer ${key}` } },
  }));
  try {
    const ranked = (await client.callTool({ name: "search_memory", arguments: { containerTag: "personal", query: "colors", limit: 5 } })).structuredContent as any;
    expect(calls.at(-1)).toEqual({ method: "POST", path: "/v4/search",
      body: { q: "colors", containerTag: "personal", limit: 30, searchMode: "memories", threshold: 0.3 } });
    expect(ranked.reranked).toBe(true);
    expect(ranked.results.map((r: any) => r.id)).toEqual(["violet", "memory"]);
    expect(ranked.results[0].rerankScore).toBe(0.95);
    expect(ranked.total).toBe(2);
    expect(rerankCalls.at(-1)).toEqual({ authorization: null, body: {
      model: "bge-reranker-v2-m3", query: "colors", return_documents: false,
      documents: ["Synthetic indigo fact", "Synthetic violet fact", "Unrelated note"] } });

    rerankerDown = true;
    const plain = (await client.callTool({ name: "search_memory", arguments: { containerTag: "personal", query: "colors" } })).structuredContent as any;
    expect(plain.reranked).toBe(false);
    expect(plain.results.map((r: any) => r.id)).toEqual(["memory", "violet"]);
  } finally { rerankerDown = false; await client.close(); }
});

for (const modern of [false, true]) {
  test(`${modern ? "2026" : "2025"} client: discovery, scoped reads, writes and errors`, async () => {
    const client = new Client({ name: "adapter-test", version: "1" },
      modern ? { versionNegotiation: { mode: { pin: "2026-07-28" } } } : {});
    await client.connect(new StreamableHTTPClientTransport(url, {
      requestInit: { headers: { Authorization: `Bearer ${key}` } },
    }));
    try {
      const tools = await client.listTools();
      expect(tools.tools.map(t => t.name).sort()).toEqual([
        "add_memory", "get_document", "get_profile", "list_documents", "list_memories", "list_spaces", "search_memory", "who_am_i",
      ]);
      const identity = await client.callTool({ name: "who_am_i", arguments: {} });
      expect(identity.isError).not.toBe(true);
      expect(JSON.stringify(identity)).not.toContain("DO_NOT_EXPOSE");
      for (const name of ["search_memory", "get_profile", "list_documents", "list_memories", "get_document"]) {
        const response = await client.callTool({ name, arguments: {
          containerTag: "personal", query: "indigo", documentId: "doc",
        } });
        expect(response.isError).not.toBe(true);
      }
      const missingScope = await client.callTool({ name: "search_memory", arguments: { query: "indigo" } });
      expect(missingScope.isError).toBe(true);
      const crossed = await client.callTool({ name: "get_document", arguments: { documentId: "doc", containerTag: "company" } });
      expect(crossed.isError).toBe(true);
      expect(JSON.stringify(crossed)).not.toContain("PRIVATE_DOCUMENT");
      const saved = await client.callTool({ name: "add_memory", arguments: { content: "Synthetic fact", containerTag: "personal" } });
      expect(saved.structuredContent).toMatchObject({ id: "created", status: "queued" });
      const forgotten = await client.callTool({ name: "add_memory", arguments: {
        content: "Synthetic fact", containerTag: "personal", action: "forget", memoryId: "memory",
      } });
      expect(forgotten.isError).not.toBe(true);
      expect(calls.at(-1)).toEqual({ method: "DELETE", path: "/v4/memories", body: { containerTag: "personal", id: "memory" } });
    } finally { await client.close(); }
  });
}

test("reject missing/invalid auth, foreign origins and hosts", async () => {
  for (const headers of [{}, { Authorization: "Bearer sm_wrong_0123456789abcdef" }]) {
    const response = await fetch(url, { method: "POST", headers });
    expect(response.status).toBe(401);
  }
  const handle = createApp(backend.url.toString(), "http://127.0.0.1/mcp");
  for (const headers of [{ Host: "evil.example" }, { Host: "127.0.0.1", Origin: "https://evil.example" }]) {
    const response = await handle(new Request(url, { headers }));
    expect(response.status).toBe(403);
  }
});

test("/v4/profile recall: reranked results, native profile, and the server's own answer on failure", async () => {
  const recall = (base: URL, body: unknown, authorization = `Bearer ${key}`) => fetch(new URL("/v4/profile", base), {
    method: "POST", headers: { "Content-Type": "application/json", Authorization: authorization }, body: JSON.stringify(body) });
  const native = { profile: { static: ["Synthetic fact"], dynamic: [] },
    searchResults: { results: [{ id: "noise", memory: "Unrelated note", similarity: 0.9 }], total: 1, timing: 7 } };

  const ranked = await (await recall(rerankingEndpoint.url, { containerTag: "personal", q: "colors" })).json() as any;
  expect(ranked.profile).toEqual(native.profile);
  expect(ranked.searchResults.results.map((r: any) => r.id)).toEqual(["violet", "memory"]);
  expect(ranked.searchResults).toMatchObject({ total: 2, timing: 7 });
  expect(calls.filter(c => c.path === "/v4/search").at(-1)?.body).toEqual(
    { q: "colors", containerTag: "personal", limit: 30, searchMode: "memories", threshold: 0.3 });

  rerankerDown = true;
  try { expect(await (await recall(rerankingEndpoint.url, { containerTag: "personal", q: "colors" })).json()).toEqual(native); }
  finally { rerankerDown = false; }

  rerankerSlow = true;
  const started = Date.now();
  try { expect(await (await recall(rerankingEndpoint.url, { containerTag: "personal", q: "colors" })).json()).toEqual(native); }
  finally { rerankerSlow = false; }
  expect(Date.now() - started).toBeLessThan(3200); // gave up at the 2.5 s deadline, not the reranker's 3.5 s

  expect(await (await recall(rerankingEndpoint.url, { containerTag: "personal" })).json()).toEqual(native); // no query: untouched
  expect(await (await recall(endpoint.url, { containerTag: "personal", q: "colors" })).json()).toEqual(native); // no reranker configured
  expect((await recall(rerankingEndpoint.url, { containerTag: "personal", q: "colors" }, "Bearer sm_wrong_0123456789abcdef")).status).toBe(401);
  expect((await fetch(new URL("/v4/profile", rerankingEndpoint.url), { method: "POST" })).status).toBe(401);
});
