export interface Citation { document_id: string; document_version: string; page: number; chunk_id: string }
export interface Claim { text: string; citations: Citation[] }
export interface SynthesisStatement extends Claim { supporting_quotes: Claim[] }
export interface Synthesis {
  status: "answered" | "refused"; statements: SynthesisStatement[]; refusal_reason: string;
  refusal_category: "none" | "safety" | "input_mismatch" | "insufficient_info";
  model: string; model_digest: string; prompt_version: string; prompt_tokens: number; output_tokens: number;
}
export interface FinancialMetric { name: string; value: string; unit: string; source_fields: string[]; citations: Citation[] }
export interface Borrower { borrower_id: string; name: string; industry: string }
export interface Chunk extends Citation {
  tenant_id: string; borrower_id: string | null; document_kind: string; title: string;
  section: string; text: string; acl_groups: string[]; valid_from: string; valid_to: string | null;
  content_hash: string; parser_version: string; extraction_confidence: number;
  start_char: number; end_char: number; chunker_version: string;
}
export interface Stage { name: string; duration_ms: number }
export const dispositions = ["MEETS_POLICY", "EXCEPTION_REQUIRED", "INSUFFICIENT_EVIDENCE", "MATERIAL_CONFLICT", "HUMAN_JUDGMENT_REQUIRED"] as const;
export type Disposition = typeof dispositions[number];
export interface Packet {
  request_id: string; borrower_id: string; borrower_summary: Claim[]; calculated_metrics: FinancialMetric[];
  applicable_policy: Claim[]; policy_disposition: Disposition; missing_documents: string[];
  exceptions: Claim[]; contradictions: Claim[]; recommended_next_actions: string[];
  questions_for_underwriter: string[]; abstained: boolean; evidence: Chunk[]; stages: Stage[];
  provider_mode: string; corpus_version: string; latency_ms: number; cache_hit: boolean; cost_usd: string | null;
  synthesis: Synthesis | null;
}
export interface BorrowerList { borrowers: Borrower[]; mode: "demo" | "production" }
export interface QueryRequest { borrower_id: string; question: string; effective_at: string }

/** Validate network data at runtime because TypeScript alone cannot establish an HTTP contract. */
function object(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) throw new Error("The API returned an invalid object.");
  return value as Record<string, unknown>;
}
/** Retain server text verbatim; rendering must use text nodes even after shape validation. */
function string(value: unknown): string {
  if (typeof value !== "string") throw new Error("The API returned an invalid text field.");
  return value;
}
/** Nonfinite and negative timing values would create misleading measurements. */
function number(value: unknown): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) throw new Error("The API returned an invalid numeric field.");
  return value;
}
/** Preserve explicit false rather than coercing strings or missing flags to a review state. */
function boolean(value: unknown): boolean {
  if (typeof value !== "boolean") throw new Error("The API returned an invalid status flag.");
  return value;
}
/** Decimal strings preserve backend precision instead of recalculating financial values in JavaScript. */
function decimal(value: unknown): string {
  if (typeof value !== "string" || !/^-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$/.test(value)) throw new Error("The API returned an invalid decimal.");
  return value;
}
/** Nullable provenance fields carry an explicit absence rather than a fabricated default. */
function nullableString(value: unknown): string | null { return value === null ? null : string(value); }
/** Collection validation rejects partial malformed packets before any result enters the view. */
function array<T>(value: unknown, parse: (item: unknown) => T): T[] {
  if (!Array.isArray(value)) throw new Error("The API returned an invalid collection.");
  return value.map(parse);
}
/** Page references must be positive integers so citation buttons cannot invent page zero. */
export function parseCitation(value: unknown): Citation {
  const data = object(value); const page = number(data.page);
  if (!Number.isInteger(page) || page < 1) throw new Error("The API returned an invalid citation page.");
  return { document_id: string(data.document_id), document_version: string(data.document_version), page, chunk_id: string(data.chunk_id) };
}
/** A factual claim without a citation violates the packet contract and is never displayed. */
function parseClaim(value: unknown): Claim {
  const data = object(value); const citations = array(data.citations, parseCitation);
  if (citations.length === 0) throw new Error("The API returned an uncited claim.");
  return { text: string(data.text), citations };
}
/** Generated statements retain their exact supporting quotes without treating the prose as a source. */
function parseSynthesisStatement(value: unknown): SynthesisStatement {
  const data = object(value); const claim = parseClaim(value); const quotes = array(data.supporting_quotes, parseClaim);
  if (!claim.text.trim() || [...claim.text].length > 1200 || claim.citations.length > 8 || !quotes.length || quotes.length > 8 || quotes.some(quote => !quote.text.trim())) throw new Error("The API returned unsupported generated text.");
  return { ...claim, supporting_quotes: quotes };
}
/** Accept legacy packets while rejecting contradictory generation states before displaying any answer. */
function parseSynthesis(value: unknown): Synthesis | null {
  if (value === undefined || value === null) return null;
  const data = object(value);
  if (data.status !== "answered" && data.status !== "refused") throw new Error("The API returned an invalid synthesis status.");
  if (data.refusal_category !== "none" && data.refusal_category !== "safety" && data.refusal_category !== "input_mismatch" && data.refusal_category !== "insufficient_info") throw new Error("The API returned an invalid refusal category.");
  const statements = array(data.statements, parseSynthesisStatement); const reason = string(data.refusal_reason);
  if (statements.length > 6 || [...reason].length > 500 || (data.status === "answered" ? !statements.length || reason !== "" || data.refusal_category !== "none" : statements.length !== 0 || !reason.trim() || data.refusal_category === "none")) throw new Error("The API returned an inconsistent synthesis result.");
  const model = string(data.model); const digest = string(data.model_digest); const version = string(data.prompt_version);
  const promptTokens = number(data.prompt_tokens); const outputTokens = number(data.output_tokens);
  if (!model.trim() || [...model].length > 100 || !/^[a-f0-9]{64}$/.test(digest) || !version.trim() || [...version].length > 100 || !Number.isInteger(promptTokens) || promptTokens > 16384 || !Number.isInteger(outputTokens) || outputTokens > 2048) throw new Error("The API returned invalid synthesis provenance.");
  return { status: data.status, statements, refusal_reason: reason, refusal_category: data.refusal_category,
    model, model_digest: digest, prompt_version: version, prompt_tokens: promptTokens, output_tokens: outputTokens };
}
/** Metrics display only backend-computed values and their cited input fields. */
function parseMetric(value: unknown): FinancialMetric {
  const data = object(value); const citations = array(data.citations, parseCitation);
  if (citations.length === 0) throw new Error("The API returned an uncited metric.");
  return { name: string(data.name), value: decimal(data.value), unit: string(data.unit), source_fields: array(data.source_fields, string), citations };
}
/** Source inspection retains immutable provenance and extraction quality alongside the literal source text. */
export function parseChunk(value: unknown): Chunk {
  const data = object(value); const confidence = number(data.extraction_confidence);
  if (confidence > 1) throw new Error("The API returned an invalid extraction confidence.");
  const start = number(data.start_char); const end = number(data.end_char);
  if (!Number.isInteger(start) || !Number.isInteger(end) || end <= start) throw new Error("The API returned an invalid source span.");
  return { ...parseCitation(value), tenant_id: string(data.tenant_id), borrower_id: nullableString(data.borrower_id),
    document_kind: string(data.document_kind), title: string(data.title), section: string(data.section), text: string(data.text),
    acl_groups: array(data.acl_groups, string), valid_from: string(data.valid_from), valid_to: nullableString(data.valid_to),
    content_hash: string(data.content_hash), parser_version: string(data.parser_version), extraction_confidence: confidence,
    start_char: start, end_char: end, chunker_version: string(data.chunker_version) };
}
/** Accept only the backend's declared modes; a missing mode must never imply a safe demo. */
export function parseBorrowers(value: unknown): BorrowerList {
  const data = object(value);
  if (data.mode !== "demo" && data.mode !== "production") throw new Error("The API returned an unknown environment mode.");
  return { mode: data.mode, borrowers: array(data.borrowers, parseBorrower) };
}
/** Borrower options come from server-authorized summaries rather than client-authored scope. */
function parseBorrower(value: unknown): Borrower {
  const data = object(value); return { borrower_id: string(data.borrower_id), name: string(data.name), industry: string(data.industry) };
}
/** Timing stages expose execution metadata without putting raw request text into diagnostics. */
function parseStage(value: unknown): Stage {
  const data = object(value); return { name: string(data.name), duration_ms: number(data.duration_ms) };
}
/** Reject invalid dispositions and malformed fields as one failed result, avoiding partial success. */
export function parsePacket(value: unknown): Packet {
  const data = object(value); const disposition = string(data.policy_disposition);
  if (!(dispositions as readonly string[]).includes(disposition)) throw new Error("The API returned an unknown policy disposition.");
  const packet: Packet = { request_id: string(data.request_id), borrower_id: string(data.borrower_id),
    borrower_summary: array(data.borrower_summary, parseClaim), calculated_metrics: array(data.calculated_metrics, parseMetric),
    applicable_policy: array(data.applicable_policy, parseClaim), policy_disposition: disposition as Disposition,
    missing_documents: array(data.missing_documents, string), exceptions: array(data.exceptions, parseClaim),
    contradictions: array(data.contradictions, parseClaim), recommended_next_actions: array(data.recommended_next_actions, string),
    questions_for_underwriter: array(data.questions_for_underwriter, string), abstained: boolean(data.abstained),
    evidence: array(data.evidence, parseChunk), stages: array(data.stages, parseStage), provider_mode: string(data.provider_mode),
    corpus_version: string(data.corpus_version), latency_ms: number(data.latency_ms), cache_hit: boolean(data.cache_hit),
    cost_usd: data.cost_usd === null ? null : decimal(data.cost_usd), synthesis: parseSynthesis(data.synthesis) };
  if (packet.abstained && packet.synthesis) throw new Error("The API returned synthesis for an abstained packet.");
  const sources = new Map<string, Chunk>();
  for (const source of packet.evidence) {
    if (sources.has(source.chunk_id)) throw new Error("The API returned duplicate evidence identifiers.");
    sources.set(source.chunk_id, source);
  }
  const statements = packet.synthesis?.statements ?? [];
  for (const claim of [...packet.borrower_summary, ...packet.applicable_policy, ...packet.exceptions, ...packet.contradictions, ...packet.calculated_metrics, ...statements, ...statements.flatMap(statement => statement.supporting_quotes)]) {
    for (const citation of claim.citations) {
      const source = sources.get(citation.chunk_id);
      if (!source || source.document_id !== citation.document_id || source.document_version !== citation.document_version || source.page !== citation.page) {
        throw new Error("The API returned a citation that does not match its retrieved evidence.");
      }
    }
  }
  for (const statement of statements) {
    const cited = new Set(statement.citations.map(citation => citation.chunk_id));
    const supported = new Set<string>();
    for (const quote of statement.supporting_quotes) {
      for (const citation of quote.citations) {
        if (!cited.has(citation.chunk_id) || !sources.get(citation.chunk_id)?.text.includes(quote.text)) throw new Error("The API returned a supporting quote that does not match its cited evidence.");
        supported.add(citation.chunk_id);
      }
    }
    if (supported.size !== cited.size) throw new Error("The API returned generated citations without supporting quotes.");
  }
  return packet;
}
