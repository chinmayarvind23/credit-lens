import { parseChunk, parsePacket, type Chunk, type Packet, type QueryRequest } from "./contracts";

export interface RecordedExample { id: string; title: string; borrowerName: string; request: QueryRequest; packet: Packet; sources: Map<string, Chunk>; elapsedMs: number; requestHash: string; responseHash: string }
export interface Recordings { capturedAt: string; sourceRevision: string; examples: RecordedExample[] }

/** Capture data is a public artifact, so validate it before assigning any product meaning. */
function record(value: unknown): Record<string, unknown> { if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Invalid recording object"); return value as Record<string, unknown>; }
/** Preserve literal source strings and reject absent provenance rather than inventing defaults. */
function text(value: unknown): string { if (typeof value !== "string") throw new Error("Invalid recording text"); return value; }
/** Hash the original UTF-8 HTTP body, not a reserialized JSON object with different bytes. */
export async function verifiedBody(value: unknown): Promise<{ data: unknown; hash: string }> {
  const item = record(value); const body = text(item.body); const hash = text(item.sha256);
  if (body.length > 2_000_000 || !/^[a-f0-9]{64}$/.test(hash)) throw new Error("Invalid recording digest");
  const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(body));
  const actual = Array.from(new Uint8Array(bytes), byte => byte.toString(16).padStart(2, "0")).join("");
  if (actual !== hash) throw new Error("Recorded response hash does not match");
  return { data: JSON.parse(body) as unknown, hash };
}
/** Read only the original three-field query contract; the preview never submits these requests. */
function requestValue(value: unknown): QueryRequest {
  const data = record(value); const borrower = text(data.borrower_id); const question = text(data.question); const effective = text(data.effective_at);
  if (Object.keys(data).sort().join(",") !== "borrower_id,effective_at,question" || !/^[a-zA-Z0-9_-]{1,80}$/.test(borrower) || !/^\d{4}-\d{2}-\d{2}$/.test(effective) || question.length < 3 || question.length > 2000) throw new Error("Invalid captured request");
  return { borrower_id: borrower, question, effective_at: effective };
}
/** Captured source GETs must match every cited chunk rather than relying on packet copies alone. */
async function sourceValues(value: unknown, packet: Packet): Promise<Map<string, Chunk>> {
  if (!Array.isArray(value) || value.length !== packet.evidence.length) throw new Error("Incomplete recorded sources");
  const sources = new Map<string, Chunk>();
  for (const raw of value) { const source = record(raw); if (source.status !== 200) throw new Error("Source capture failed"); const chunk = parseChunk((await verifiedBody(source)).data); if (sources.has(chunk.chunk_id)) throw new Error("Duplicate recorded source"); sources.set(chunk.chunk_id, chunk); }
  for (const chunk of packet.evidence) if (JSON.stringify(sources.get(chunk.chunk_id)) !== JSON.stringify(chunk)) throw new Error("Recorded source differs from cited evidence");
  return sources;
}
/** The only accepted material is the intentionally published synthetic local provider capture. */
async function exampleValue(value: unknown): Promise<RecordedExample> {
  const data = record(value); const response = record(data.response); const rawRequest = record(data.request);
  if (response.status !== 200 || rawRequest.method !== "POST" || rawRequest.path !== "/api/v1/query") throw new Error("Unexpected capture endpoint");
  const requestBody = await verifiedBody(rawRequest); const responseBody = await verifiedBody(response); const request = requestValue(requestBody.data); const packet = parsePacket(responseBody.data);
  if (packet.provider_mode !== "local-extractive" || packet.borrower_id !== request.borrower_id || packet.evidence.some(chunk => chunk.tenant_id !== "demo-bank" || (chunk.borrower_id !== null && chunk.borrower_id !== request.borrower_id) || chunk.acl_groups.some(group => group !== "underwriting"))) throw new Error("Recording is outside public synthetic scope");
  if (typeof response.elapsed_ms !== "number" || !Number.isFinite(response.elapsed_ms) || response.elapsed_ms < 0) throw new Error("Invalid captured timing");
  return { id: text(data.id), title: text(data.title), borrowerName: text(data.borrower_name), request, packet, sources: await sourceValues(data.sources, packet), elapsedMs: response.elapsed_ms, requestHash: requestBody.hash, responseHash: responseBody.hash };
}
/** Verify the whole small recording set before allowing any example to be selected. */
export async function parseRecordings(value: unknown): Promise<Recordings> {
  const data = record(value); const captured = text(data.captured_at); const revision = text(data.source_revision);
  if (data.schema_version !== 1 || data.mode !== "recorded-synthetic-api" || !Number.isFinite(Date.parse(captured)) || !/^[a-f0-9]{40}$/.test(revision) || !Array.isArray(data.examples) || data.examples.length !== 5) throw new Error("Invalid public recording set");
  const examples = await Promise.all(data.examples.map(exampleValue));
  if (new Set(examples.map(example => example.id)).size !== examples.length) throw new Error("Duplicate recording identifier");
  return { capturedAt: captured, sourceRevision: revision, examples };
}
