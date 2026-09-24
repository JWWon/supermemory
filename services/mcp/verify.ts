import { readFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";
import { Client, StreamableHTTPClientTransport } from "@modelcontextprotocol/client";

const config = JSON.parse(await readFile(process.env.SUPERMEMORY_CONFIG ?? join(homedir(), ".codex/supermemory.json"), "utf8"));
const base = new URL(process.env.SUPERMEMORY_API_URL ?? config.baseUrl);
const endpoint = new URL(process.env.SUPERMEMORY_MCP_URL ?? "./mcp", `${base.toString().replace(/\/$/, "")}/`);
if (base.origin !== endpoint.origin && !(base.hostname === "127.0.0.1" && endpoint.hostname === "127.0.0.1"))
  throw new Error("MCP and API must share the configured origin (except loopback tests)");
const key = process.env.SUPERMEMORY_CODEX_API_KEY ?? config.apiKey;
if (!key) throw new Error("Supermemory API key missing");
const headers = { Authorization: `Bearer ${key}`, "Content-Type": "application/json" };
const client = new Client({ name: "supermemory-local-verification", version: "0.1.0" },
  { versionNegotiation: { mode: process.argv.includes("--legacy") ? "legacy" : "auto" } });
await client.connect(new StreamableHTTPClientTransport(endpoint, { requestInit: { headers } }));
const tag = `mcp_verify_${Date.now()}`;
let documentId: string | undefined;
async function call(name: string, args: Record<string, unknown> = {}) {
  const reply = await client.callTool({ name, arguments: args });
  if (reply.isError) throw new Error(`${name}: ${JSON.stringify(reply.content)}`);
  return reply.structuredContent as Record<string, any>;
}
try {
  const tools = await client.listTools();
  const identity = await call("who_am_i");
  if (!identity.user?.id) throw new Error("Missing authenticated identity");
  await call("get_profile", { containerTag: tag });
  console.log(`PASS connected: ${tools.tools.length} tools; protocol ${client.getProtocolEra()}`);
  if (process.argv.includes("--write")) {
    const saved = await call("add_memory", { containerTag: tag, content: "Project Kestrel MCP verification uses vermilion labels." });
    documentId = saved.id;
    if (!documentId) throw new Error("No document ID returned");
    console.log(`Created isolated verification document ${documentId} in ${tag}`);
    const deadline = Date.now() + 180000;
    let found = false;
    while (Date.now() < deadline) {
      const doc = await call("get_document", { containerTag: tag, documentId });
      if (doc.document.status === "failed") throw new Error("Verification extraction failed");
      if (doc.document.status === "done") {
        const matches = await call("search_memory", { containerTag: tag, query: "Project Kestrel vermilion labels" });
        found = JSON.stringify(matches.results).toLowerCase().includes("vermilion");
        if (found) break;
      }
      await Bun.sleep(1000);
    }
    if (!found) throw new Error("Verification memory was not searchable within 180 seconds");
    const crossed = await client.callTool({ name: "get_document", arguments: { containerTag: `${tag}_other`, documentId } });
    if (!crossed.isError) throw new Error("Cross-container document access was not rejected");
    await call("list_documents", { containerTag: tag });
    const memories = await call("list_memories", { containerTag: tag });
    const memory = memories.memoryEntries?.[0];
    if (!memory?.id || !memory?.memory) throw new Error("Memory listing omitted the created entry");
    await call("add_memory", { containerTag: tag, action: "forget", memoryId: memory.id, content: memory.memory });
    console.log("PASS save -> extraction -> search/list/read -> exact forget; cross-container read rejected");
  }
} finally {
  await client.close();
  if (documentId) {
    const response = await fetch(new URL(`/v3/documents/${encodeURIComponent(documentId)}`, base), {
      method: "DELETE", headers, signal: AbortSignal.timeout(15000),
    });
    if (!response.ok && response.status !== 404) throw new Error(`Verification cleanup HTTP ${response.status}`);
    console.log("Removed the isolated verification document.");
  }
}
