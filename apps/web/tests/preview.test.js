import { expect, test } from "bun:test";
import { parseRecordings, verifiedBody } from "../src/preview-contracts";

/** Fixture hashes represent test bytes only and never enter the public example artifact. */
async function raw(value) {
  const body = JSON.stringify(value); const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(body));
  return { body, sha256: Array.from(new Uint8Array(bytes), byte => byte.toString(16).padStart(2, "0")).join("") };
}
/** A minimal abstention has no source content, making data-boundary mutations explicit. */
async function fixture() {
  const request = { borrower_id: "borrower-001", effective_at: "2026-06-01", question: "Review debt coverage" };
  const packet = { request_id: "test-request", borrower_id: "borrower-001", borrower_summary: [], calculated_metrics: [], applicable_policy: [], policy_disposition: "INSUFFICIENT_EVIDENCE", missing_documents: ["relevant evidence"], exceptions: [], contradictions: [], recommended_next_actions: ["Request missing evidence"], questions_for_underwriter: [], abstained: true, evidence: [], stages: [], provider_mode: "local-extractive", corpus_version: "test", latency_ms: 1, cache_hit: false, cost_usd: null };
  const example = { id: "sample", title: "Test abstention", borrower_name: "Synthetic test borrower", request: { method: "POST", path: "/api/v1/query", ...await raw(request) }, response: { status: 200, elapsed_ms: 2, ...await raw(packet) }, sources: [] };
  return { schema_version: 1, mode: "recorded-synthetic-api", captured_at: "2026-09-11T00:00:00Z", source_revision: "a".repeat(40), examples: Array.from({ length: 5 }, (_, i) => ({ ...structuredClone(example), id: String(i) })) };
}

/** Successful parsing preserves the actual provider and recorded timing without a live API call. */
test("verifies an explicit recorded example set", async () => { const data = await parseRecordings(await fixture()); expect(data.examples).toHaveLength(5); expect(data.examples[0].elapsedMs).toBe(2); expect(data.examples[0].packet.abstained).toBe(true); });
/** Reformatting or altering raw response bytes must not pass the original capture digest. */
test("rejects changed recorded bytes", async () => { const data = await fixture(); data.examples[0].response.body += " "; await expect(parseRecordings(data)).rejects.toThrow("hash does not match"); });
/** A valid digest cannot hide a request/packet borrower mismatch. */
test("rejects mismatched recorded scope", async () => { const data = await fixture(); Object.assign(data.examples[0].request, await raw({ borrower_id: "borrower-002", effective_at: "2026-06-01", question: "Review debt coverage" })); await expect(parseRecordings(data)).rejects.toThrow("public synthetic scope"); });
/** Unknown providers cannot be presented as approved public synthetic examples. */
test("rejects a non-demo provider", async () => { const data = await fixture(); const packet = JSON.parse(data.examples[0].response.body); packet.provider_mode = "production-provider"; Object.assign(data.examples[0].response, await raw(packet)); await expect(parseRecordings(data)).rejects.toThrow("public synthetic scope"); });
/** The static viewer cannot silently ignore source responses beyond the cited packet. */
test("rejects unexpected source records", async () => { const data = await fixture(); data.examples[0].sources.push({}); await expect(parseRecordings(data)).rejects.toThrow("Incomplete recorded sources"); });
/** Body hashes operate on UTF-8 bytes so punctuation and non-ASCII source text remain exact. */
test("verifies exact Unicode response bytes", async () => { const result = await verifiedBody(await raw({ text: "Café · synthetic" })); expect(result.data.text).toBe("Café · synthetic"); });
