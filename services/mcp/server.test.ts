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
  if (path === "/v4/search") return Response.json({ results: [{ id: "memory", memory: "Synthetic indigo fact" }], total: 1 });
  if (path === "/v4/profile") return Response.json({ profile: { static: ["Synthetic fact"], dynamic: [] } });
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
afterAll(() => { endpoint.stop(true); backend.stop(true); });

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
