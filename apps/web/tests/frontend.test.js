import { describe, expect, test } from "bun:test";
import { parseBorrowers, parseChunk, parsePacket } from "../src/contracts";
import { CreditLensApi, apiBase } from "../src/api";
import { duration, packetDisposition, renderPacket, renderSource } from "../src/render";

/** Small synthetic fixtures exercise the wire shape without presenting evaluation results as measured quality. */
function fixture() {
  const citation = { document_id: "policy-1", document_version: "v1", page: 2, chunk_id: "chunk-1" };
  const chunk = { ...citation, tenant_id: "test-tenant", borrower_id: null, document_kind: "policy", title: "Synthetic policy", section: "Coverage", text: "<img src=x onerror=alert(1)> This is literal source text.", acl_groups: ["underwriter"], valid_from: "2025-01-01", valid_to: null, content_hash: "test-hash", parser_version: "test-parser", extraction_confidence: 1, start_char: 0, end_char: 50, chunker_version: "test-chunker" };
  const claim = { text: "<script>Hostile text must remain literal</script>", citations: [citation] };
  return { request_id: "test-request", borrower_id: "borrower-001", borrower_summary: [claim], calculated_metrics: [{ name: "DSCR", value: "1.2500000000000000001", unit: "x", source_fields: ["net_income", "debt_service"], citations: [citation] }], applicable_policy: [claim], policy_disposition: "MEETS_POLICY", missing_documents: [], exceptions: [], contradictions: [], recommended_next_actions: ["Review cited documents"], questions_for_underwriter: [], abstained: false, evidence: [chunk], stages: [{ name: "citation_validation", duration_ms: 1.2 }], provider_mode: "synthetic-test", corpus_version: "test-v1", latency_ms: 4.5, cache_hit: false, cost_usd: null };
}

/** Real wire fields with synthetic text let UI tests separate generated claims from exact evidence. */
function synthesisFixture() {
  const packet = fixture(); const source = packet.evidence[0];
  const citation = { document_id: source.document_id, document_version: source.document_version, page: source.page, chunk_id: source.chunk_id };
  packet.provider_mode = "ollama-rag";
  packet.synthesis = { status: "answered", statements: [{ text: "<script>Generated interpretation remains text</script>", citations: [citation], supporting_quotes: [{ text: source.text, citations: [citation] }] }], refusal_reason: "", refusal_category: "none", model: "local-test:small", model_digest: "a".repeat(64), prompt_version: "test-prompt-v1", prompt_tokens: 50, output_tokens: 20 };
  return packet;
}

/** Build JSON responses with the same media type used by the FastAPI contract. */
function json(value, status = 200) { return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } }); }

// Shape checks protect the boundary where untyped service responses enter the application.
describe("runtime contracts", () => {
  /** Financial values must survive parsing without IEEE-754 rounding. */
  test("preserves Decimal strings exactly", () => { expect(parsePacket(fixture()).calculated_metrics[0].value).toBe("1.2500000000000000001"); });
  /** Lending decisions are excluded even when an otherwise valid packet contains one. */
  test("rejects an approval disposition", () => { const packet = fixture(); packet.policy_disposition = "APPROVE"; expect(() => parsePacket(packet)).toThrow("unknown policy disposition"); });
  /** Citation enforcement must fail before any uncited claim is displayed. */
  test("rejects claims without citations", () => { const packet = fixture(); packet.borrower_summary[0].citations = []; expect(() => parsePacket(packet)).toThrow("uncited claim"); });
  /** A plausible citation with the wrong page still lacks verified provenance. */
  test("rejects references to the wrong source page", () => { const packet = fixture(); packet.evidence[0].page = 7; expect(() => parsePacket(packet)).toThrow("does not match"); });
  /** Missing retrieval evidence must not be replaced by a superficially valid citation button. */
  test("rejects citations outside the returned evidence", () => { const packet = fixture(); packet.evidence = []; expect(() => parsePacket(packet)).toThrow("does not match"); });
  /** Duplicate chunk identifiers would make source inspection ambiguous. */
  test("rejects duplicate evidence identifiers", () => { const packet = fixture(); packet.evidence.push(packet.evidence[0]); expect(() => parsePacket(packet)).toThrow("duplicate evidence"); });
  /** String status flags must not become truthy review states. */
  test("rejects coerced boolean and invalid latency", () => { const packet = fixture(); packet.abstained = "false"; expect(() => parsePacket(packet)).toThrow("status flag"); packet.abstained = false; packet.latency_ms = -1; expect(() => parsePacket(packet)).toThrow("numeric field"); });
  /** Nullable cost remains unavailable instead of inventing zero-cost evidence. */
  test("preserves absent cost and rejects numeric decimals", () => { const packet = fixture(); expect(parsePacket(packet).cost_usd).toBeNull(); packet.calculated_metrics[0].value = 1.25; expect(() => parsePacket(packet)).toThrow("decimal"); });
  /** Mode validation prevents a response without environment identity from claiming synthetic safety. */
  test("requires explicit environment mode", () => { expect(() => parseBorrowers({ borrowers: [] })).toThrow("environment mode"); expect(parseBorrowers({ borrowers: [], mode: "demo" }).borrowers).toHaveLength(0); });
  /** Source provenance needs positive page numbers, valid confidence, and an ordered character span. */
  test("validates source coordinates and confidence", () => { const source = fixture().evidence[0]; source.end_char = 0; expect(() => parseChunk(source)).toThrow("source span"); source.end_char = 10; source.extraction_confidence = 1.1; expect(() => parseChunk(source)).toThrow("extraction confidence"); });
});

describe("generated synthesis contracts", () => {
  /** Older browser bundles and saved packets remain valid when synthesis was never requested. */
  test("normalizes absent and explicit null synthesis", () => {
    expect(parsePacket(fixture()).synthesis).toBeNull();
    expect(parsePacket({ ...fixture(), synthesis: null }).synthesis).toBeNull();
  });
  /** Model output cannot override the workflow's decision to withhold synthesis. */
  test("rejects synthesis on server-abstained packets", () => {
    const packet = synthesisFixture(); packet.abstained = true;
    expect(() => parsePacket(packet)).toThrow("abstained packet");
    Object.assign(packet.synthesis, { status: "refused", statements: [], refusal_reason: "Unavailable", refusal_category: "insufficient_info" });
    expect(() => parsePacket(packet)).toThrow("abstained packet");
  });
  /** Displayable synthesis preserves provenance and exact quotes instead of recomputing financial values. */
  test("preserves generated text, exact quotes and model provenance", () => {
    const packet = parsePacket(synthesisFixture());
    expect(packet.synthesis.statements[0].supporting_quotes[0].text).toBe(packet.evidence[0].text);
    expect(packet.synthesis.model_digest).toBe("a".repeat(64));
    expect(packet.synthesis.prompt_tokens).toBe(50);
    expect(packet.calculated_metrics[0].value).toBe("1.2500000000000000001");
  });
  /** A model citation cannot create a source that the server omitted from authorized evidence. */
  test("rejects generated statements with foreign citations", () => {
    const packet = synthesisFixture(); packet.synthesis.statements[0].citations = [{ ...packet.borrower_summary[0].citations[0], chunk_id: "foreign" }];
    expect(() => parsePacket(packet)).toThrow("does not match");
  });
  /** Structurally valid source identities cannot excuse a rewritten quote. */
  test("rejects fabricated supporting quotes", () => {
    const packet = synthesisFixture(); packet.synthesis.statements[0].supporting_quotes[0].text = "This sentence is absent from the source.";
    expect(() => parsePacket(packet)).toThrow("supporting quote");
  });
  /** Every displayed statement citation needs a quote, while quotes cannot introduce uncited support. */
  test("rejects disconnected statement citations and quote sources", () => {
    const packet = synthesisFixture(); const second = { ...packet.evidence[0], chunk_id: "chunk-2" }; packet.evidence.push(second);
    const citation = { document_id: second.document_id, document_version: second.document_version, page: second.page, chunk_id: second.chunk_id };
    packet.synthesis.statements[0].citations.push(citation);
    expect(() => parsePacket(packet)).toThrow("without supporting quotes");
    packet.synthesis.statements[0].citations.pop(); packet.synthesis.statements[0].supporting_quotes[0].citations.push(citation);
    expect(() => parsePacket(packet)).toThrow("supporting quote");
  });
  /** Empty support, missing quotes and blank generated text never become accepted interpretations. */
  test("rejects absent generated support", () => {
    const packet = synthesisFixture(); packet.synthesis.statements[0].supporting_quotes = [];
    expect(() => parsePacket(packet)).toThrow("unsupported generated text");
    packet.synthesis.statements = [{ text: " ", citations: [packet.evidence[0]], supporting_quotes: [{ text: packet.evidence[0].text, citations: [packet.evidence[0]] }] }];
    expect(() => parsePacket(packet)).toThrow("unsupported generated text");
  });
  /** Unknown or contradictory synthesis states fail as one packet rather than partial success. */
  test("rejects inconsistent answer and refusal states", () => {
    const packet = synthesisFixture(); packet.synthesis.status = "approved";
    expect(() => parsePacket(packet)).toThrow("synthesis status");
    packet.synthesis.status = "answered"; packet.synthesis.refusal_reason = "Refused";
    expect(() => parsePacket(packet)).toThrow("inconsistent synthesis");
    packet.synthesis.status = "refused"; packet.synthesis.refusal_category = "safety";
    expect(() => parsePacket(packet)).toThrow("inconsistent synthesis");
    packet.synthesis.statements = []; packet.synthesis.refusal_reason = " ";
    expect(() => parsePacket(packet)).toThrow("inconsistent synthesis");
  });
  /** Runtime model provenance must stay explicit and token counts cannot silently round fractional values. */
  test("rejects invalid model identity and generation metadata", () => {
    const packet = synthesisFixture(); packet.synthesis.prompt_tokens = 0.5;
    expect(() => parsePacket(packet)).toThrow("synthesis provenance");
    packet.synthesis.prompt_tokens = 50; packet.synthesis.model_digest = "mutable-tag";
    expect(() => parsePacket(packet)).toThrow("synthesis provenance");
    packet.synthesis.model_digest = "a".repeat(64); packet.synthesis.refusal_category = "unknown";
    expect(() => parsePacket(packet)).toThrow("refusal category");
  });
  /** Generation budgets match the Python wire contract before oversized fields enter the DOM. */
  test("rejects oversized synthesis collections, text and token counts", () => {
    const mutations = [
      synthesis => { synthesis.statements = Array(7).fill(synthesis.statements[0]); },
      synthesis => { synthesis.statements[0].text = "x".repeat(1201); },
      synthesis => { synthesis.statements[0].citations = Array(9).fill(synthesis.statements[0].citations[0]); },
      synthesis => { synthesis.statements[0].supporting_quotes = Array(9).fill(synthesis.statements[0].supporting_quotes[0]); },
      synthesis => { synthesis.model = "x".repeat(101); },
      synthesis => { synthesis.prompt_version = "x".repeat(101); },
      synthesis => { synthesis.prompt_tokens = 16385; },
      synthesis => { synthesis.output_tokens = 2049; },
      synthesis => { Object.assign(synthesis, { status: "refused", statements: [], refusal_category: "safety", refusal_reason: "x".repeat(501) }); },
    ];
    for (const mutate of mutations) { const packet = synthesisFixture(); mutate(packet.synthesis); expect(() => parsePacket(packet)).toThrow(); }
  });
  /** JavaScript UTF-16 code units must not reject a Python-valid count of Unicode characters. */
  test("uses Unicode character limits for generated text", () => {
    const packet = synthesisFixture(); packet.synthesis.statements[0].text = "😀".repeat(1200);
    expect(parsePacket(packet).synthesis.statements[0].text).toBe(packet.synthesis.statements[0].text);
  });
});

// Injected transport tests inspect HTTP behavior without pretending to exercise server authorization.
describe("API transport", () => {
  /** Native Window.fetch cannot use a CreditLensApi instance as its receiver. */
  test("calls the transport without an API instance receiver", async () => {
    /** A regular function exposes the receiver that arrow-function stubs would hide. */
    async function transport() {
      expect(this).toBeUndefined();
      return json({ mode: "demo", borrowers: [] });
    }
    await new CreditLensApi("", transport).borrowers(new AbortController().signal);
  });
  /** Deployed builds cannot redirect bearer tokens to a configurable third-party host. */
  test("uses same origin except the explicit localhost dev port", () => { expect(apiBase({ hostname: "localhost", port: "3000" })).toBe("http://localhost:8000"); expect(apiBase({ hostname: "credit.example", port: "3000" })).toBe(""); });
  /** A request carries exactly the public query fields and keeps bearer credentials out of the body. */
  test("sends bounded scope fields and memory token", async () => {
    let sent;
    /** Capture request metadata for assertions without logging token contents. */
    const transport = async (url, options) => { sent = { url, options }; return json(fixture()); };
    const api = new CreditLensApi("", transport); api.setToken(" test-token ");
    await api.query({ borrower_id: "borrower-001", question: "What is DSCR?", effective_at: "2026-01-01" }, new AbortController().signal);
    expect(sent.url).toBe("/api/v1/query"); expect(sent.options.headers.get("Authorization")).toBe("Bearer test-token");
    expect(Object.keys(JSON.parse(sent.options.body))).toEqual(["borrower_id", "question", "effective_at"]);
    expect(sent.options.cache).toBe("no-store"); expect(sent.options.credentials).toBe("omit"); expect(sent.options.redirect).toBe("error");
  });
  /** Clearing credentials must affect subsequent requests without retaining a previous identity. */
  test("clears token between requests", async () => {
    const headers = [];
    /** Record only headers needed to test replacement behavior. */
    const transport = async (_url, options) => { headers.push(options.headers); return json({ mode: "demo", borrowers: [] }); };
    const api = new CreditLensApi("", transport); api.setToken("old"); await api.borrowers(new AbortController().signal); api.setToken(""); await api.borrowers(new AbortController().signal);
    expect(headers[0].has("Authorization")).toBe(true); expect(headers[1].has("Authorization")).toBe(false);
  });
  /** Server error bodies are intentionally omitted so sensitive payloads cannot be reflected in the view. */
  test("maps access errors without reflecting bodies", async () => {
    /** Return a denied payload containing text that must not reach the error message. */
    const transport = async () => json({ detail: "sensitive diagnostic" }, 403);
    const api = new CreditLensApi("", transport); await expect(api.borrowers(new AbortController().signal)).rejects.toThrow("cannot access");
  });
  /** A frontend fallback document should not be mistaken for an empty successful API response. */
  test("rejects HTML fallback responses", async () => {
    /** Reproduce an incorrectly routed static server response. */
    const transport = async () => new Response("<html>app</html>", { headers: { "Content-Type": "text/html" } });
    await expect(new CreditLensApi("", transport).borrowers(new AbortController().signal)).rejects.toThrow("unexpected response format");
  });
  /** Cancellation reaches the transport and a cancelled response is not retried automatically. */
  test("forwards cancellation without retries", async () => {
    let calls = 0; let transportSignal;
    /** A pending transport ends only when the controller cancels it. */
    const transport = (_url, options) => { calls++; transportSignal = options.signal; return new Promise((resolve, reject) => {
      /** Model fetch's AbortError without requiring a live network socket. */
      options.signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
    }); };
    const controller = new AbortController(); const pending = new CreditLensApi("", transport).borrowers(controller.signal); controller.abort();
    await expect(pending).rejects.toThrow("Aborted"); expect(transportSignal.aborted).toBe(true); expect(calls).toBe(1);
  });
  /** Only generation-bearing requests receive the longer budget; source and identity reads stay bounded. */
  test("uses 240 seconds for queries and 30 seconds for other endpoints", async () => {
    const original = globalThis.setTimeout; const deadlines = [];
    /** Record deadlines while retaining native timer cleanup. */
    globalThis.setTimeout = (callback, delay, ...args) => { deadlines.push(delay); return original(callback, delay, ...args); };
    try {
      const request = { borrower_id: "borrower-001", question: "test", effective_at: "2025-01-01" };
      /** Return each endpoint's normal response shape without external service dependencies. */
      const transport = async (url) => json(url.endsWith("/query") ? fixture() : url.includes("/evidence/") ? fixture().evidence[0] : { mode: "demo", borrowers: [] });
      const api = new CreditLensApi("", transport); const signal = new AbortController().signal;
      await api.query(request, signal); await api.borrowers(signal); await api.evidence("chunk-1", request, signal);
      expect(deadlines).toEqual([240_000, 30_000, 30_000]);
    } finally { globalThis.setTimeout = original; }
  });
  /** The longer query budget must still abort a stalled body and report its actual deadline. */
  test("enforces the query deadline through response body consumption", async () => {
    const original = globalThis.setTimeout; let transportSignal; let calls = 0;
    /** Advance the owned deadline immediately without waiting four minutes in the test. */
    globalThis.setTimeout = (callback, delay, ...args) => { expect(delay).toBe(240_000); return original(callback, 0, ...args); };
    try {
      /** Model a successful header response whose body only ends when fetch is cancelled. */
      const transport = async (_url, options) => { calls++; transportSignal = options.signal; return {
        ok: true, headers: new Headers({ "Content-Type": "application/json" }),
        /** A stalled body rejects on the same signal as the network request. */
        json: () => new Promise((_resolve, reject) => options.signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true })),
      }; };
      await expect(new CreditLensApi("", transport).query({ borrower_id: "borrower-001", question: "test", effective_at: "2025-01-01" }, new AbortController().signal)).rejects.toThrow("exceeded 240 seconds");
      expect(transportSignal.aborted).toBe(true); expect(calls).toBe(1);
    } finally { globalThis.setTimeout = original; }
  });
  /** Scope changes still cancel generation immediately instead of waiting for its larger budget. */
  test("forwards query cancellation without retrying generation", async () => {
    let calls = 0;
    /** Reject the pending query when the caller cancels it. */
    const transport = (_url, options) => { calls++; return new Promise((_resolve, reject) => options.signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true })); };
    const controller = new AbortController();
    const pending = new CreditLensApi("", transport).query({ borrower_id: "borrower-001", question: "test", effective_at: "2025-01-01" }, controller.signal);
    controller.abort(); await expect(pending).rejects.toThrow("Aborted"); expect(calls).toBe(1);
  });
  /** A source lookup preserves the completed request's policy date and borrower. */
  test("encodes source identity and policy scope", async () => {
    let url;
    /** Capture the actual source URL while returning a valid synthetic chunk. */
    const transport = async (value) => { url = value; return json(fixture().evidence[0]); };
    await new CreditLensApi("", transport).evidence("chunk/with space", { borrower_id: "borrower-001", question: "test", effective_at: "2025-01-01" }, new AbortController().signal);
    expect(url).toBe("/api/v1/evidence/chunk%2Fwith%20space?borrower_id=borrower-001&effective_at=2025-01-01");
  });
});

/** This narrow DOM stand-in traps HTML sinks; it verifies rendering logic, not browser layout or accessibility behavior. */
class TextNode {
  /** Preserve only the node operations the renderer owns, keeping the test seam explicit. */
  constructor(tag = "fragment") { this.tagName = tag; this.children = []; this.dataset = {}; this.attributes = {}; this.value = ""; }
  /** Assignments stay literal, exactly as the renderer requires from browser textContent. */
  set textContent(value) { this.value = value; this.children = []; }
  /** Flatten text for assertions while retaining elements for forbidden-tag inspection. */
  get textContent() { return this.value + this.children.map(child => child.textContent).join(""); }
  /** Any HTML sink is a test failure regardless of what the current fixture contains. */
  set innerHTML(_value) { throw new Error("Unsafe HTML sink"); }
  /** Store ordered nodes so packet hierarchy can be inspected. */
  append(...nodes) { this.children.push(...nodes); }
  /** Capture metadata without interpreting it as markup. */
  setAttribute(name, value) { this.attributes[name] = value; }
}

/** Isolate the minimal document replacement to a synchronous rendering call. */
function withDocument(render) {
  const original = globalThis.document;
  globalThis.document = {
    /** Instantiate element stand-ins without interpreting dynamic text. */
    createElement(tag) { return new TextNode(tag); },
    /** A fragment captures all returned nodes for a single packet. */
    createDocumentFragment() { return new TextNode(); },
  };
  try { return render(); } finally { globalThis.document = original; }
}

// These checks protect source literalness and reporting accuracy independently of browser automation availability.
describe("safe packet rendering", () => {
  /** The deterministic local provider only assesses DSCR and must not claim a complete policy assessment. */
  test("scopes local dispositions to DSCR while preserving general provider labels", () => {
    expect(packetDisposition({ provider_mode: "local-extractive", policy_disposition: "MEETS_POLICY" }).title).toBe("Meets DSCR threshold");
    expect(packetDisposition({ provider_mode: "local-extractive", policy_disposition: "EXCEPTION_REQUIRED" }).title).toBe("DSCR exception required");
    expect(packetDisposition({ provider_mode: "managed-provider", policy_disposition: "MEETS_POLICY" }).title).toBe("Meets reviewed policy");
  });
  /** Generated interpretation never expands what the existing deterministic finance check assessed. */
  test("scopes generated and withheld provider dispositions to DSCR", () => {
    for (const mode of ["ollama-rag", "rag-withheld"]) {
      expect(packetDisposition({ provider_mode: mode, policy_disposition: "MEETS_POLICY" }).title).toBe("Meets DSCR threshold");
      expect(packetDisposition({ provider_mode: mode, policy_disposition: "EXCEPTION_REQUIRED" }).title).toBe("DSCR exception required");
    }
  });
  /** Interpretation appears before the deterministic packet but its exact quotes stay separately inspectable. */
  test("renders synthesis first with adjacent citations and literal supporting quotes", () => {
    const packet = parsePacket(synthesisFixture());
    const output = withDocument(() => renderPacket(packet, { borrower_id: packet.borrower_id, question: "Question", effective_at: "2025-01-01" }, "Borrower", 2));
    const synthesisIndex = output.children.findIndex(node => node.className === "result-card generated-synthesis");
    const dispositionIndex = output.children.findIndex(node => node.className.startsWith("disposition "));
    expect(synthesisIndex).toBeGreaterThan(-1); expect(synthesisIndex).toBeLessThan(dispositionIndex);
    const section = output.children[synthesisIndex]; const row = section.children[2];
    expect(section.textContent).toContain("Generated interpretation");
    expect(row.children[0].textContent).toBe(packet.synthesis.statements[0].text);
    expect(row.children[1].children[0].dataset.chunkId).toBe("chunk-1");
    expect(row.children[2].tagName).toBe("details");
    expect(row.children[2].textContent).toContain("Exact supporting quotes");
    expect(row.children[2].children[1].textContent).toBe(packet.evidence[0].text);
    expect(row.children[0].tagName).toBe("p");
    expect(row.children[2].children[1].tagName).toBe("blockquote");
    expect(section.textContent).not.toContain("Verified answer");
    expect(output.textContent).toContain("Meets DSCR threshold");
  });
  /** Refused synthesis stays explicit without presenting a nonexistent generated answer or invented quotes. */
  test("renders a generated refusal before the remaining evidence", () => {
    const packet = synthesisFixture(); Object.assign(packet.synthesis, { status: "refused", statements: [], refusal_reason: "<img src=x> More information is required.", refusal_category: "insufficient_info" });
    const output = withDocument(() => renderPacket(parsePacket(packet), { borrower_id: packet.borrower_id, question: "Question", effective_at: "2025-01-01" }, "Borrower", 2));
    const section = output.children.find(node => node.className === "result-card generated-synthesis");
    expect(section.textContent).toContain("Generated answer withheld");
    expect(section.textContent).toContain(packet.synthesis.refusal_reason);
    expect(section.textContent).not.toContain("Exact supporting quotes");
    expect(section.children[2].tagName).toBe("p");
    expect(output.textContent).toContain("Borrower evidence");
  });
  /** Malicious claims and source instructions must remain visible literal text. */
  test("renders hostile source content without an HTML sink", () => {
    const output = withDocument(() => renderSource(fixture().evidence[0])); expect(output.textContent).toContain("<img src=x onerror=alert(1)>");
    expect(output.children.some(child => child.tagName === "img")).toBe(false);
  });
  /** Financial precision, provider identity, and absent cost must remain honest in the rendered packet. */
  test("renders precision, provider, no reported cost, and request snapshot", () => {
    const output = withDocument(() => renderPacket(parsePacket(fixture()), { borrower_id: "borrower-001", question: "Original question", effective_at: "2025-01-01" }, "Synthetic Borrower", 9));
    expect(output.textContent).toContain("1.2500000000000000001"); expect(output.textContent).toContain("synthetic-test"); expect(output.textContent).toContain("Not reported"); expect(output.textContent).toContain("2025-01-01"); expect(output.textContent).toContain("Original question"); expect(output.textContent).toContain("<script>Hostile text must remain literal</script>");
  });
  /** Abstention must remain explicit even if useful evidence accompanies it. */
  test("renders abstention prominently", () => { const packet = fixture(); packet.abstained = true; packet.policy_disposition = "INSUFFICIENT_EVIDENCE"; const output = withDocument(() => renderPacket(parsePacket(packet), { borrower_id: "borrower-001", question: "Question", effective_at: "2025-01-01" }, "Borrower", 2)); expect(output.textContent).toContain("Assessment withheld"); expect(output.textContent).toContain("More evidence needed"); });
  /** Request time uses stated units rather than suggesting a benchmark percentile. */
  test("formats measured timing units", () => { expect(duration(9.23)).toBe("9.2 ms"); expect(duration(1234)).toBe("1.23 s"); });
});

/** Extend the text-only DOM seam with controller events; this still makes no claim about native browser semantics. */
class ControllerNode extends TextNode {
  /** Minimal state models the controls used by the controller, including select default selection. */
  constructor(tag = "div") { super(tag); this.handlers = new Map(); this.value = ""; this.disabled = false; this.hidden = false; this.open = false; this.classList = { toggle() {} }; }
  /** Store callbacks to trigger exact state transitions without synthesizing computer input. */
  addEventListener(name, callback) { this.handlers.set(name, callback); }
  /** Control values follow the selected option when scope is repopulated. */
  replaceChildren(...children) { this.children = []; this.value = ""; this.append(...children); }
  /** The browser selects the first option by default; other nodes just retain render output. */
  append(...children) { super.append(...children); if (this.tagName === "select" && !this.value && children[0]) this.value = children[0].value; }
  /** Native constraint validation is separately pending browser verification. */
  reportValidity() { return true; }
  /** Closing clears native open state for the cancellation branch. */
  close() { this.open = false; this.handlers.get("close")?.(); }
  /** Dialog display is modeled only for controller completeness, without testing focus traps. */
  showModal() { this.open = true; }
  /** Focus is intentionally outside this isolated state test. */
  focus() {}
}

/** Let the asynchronous fetch and JSON promises complete without adding real-time sleeps. */
async function settle() { for (let i = 0; i < 20; i++) await Promise.resolve(); }

/** A transport that ignores abort reproduces the race that cancellation alone cannot prevent. */
test("a late response cannot overwrite a newer borrower packet", async () => {
  const original = { document: globalThis.document, window: globalThis.window, fetch: globalThis.fetch, Option: globalThis.Option };
  const nodes = new Map(); const pending = [];
  /** Each static selector resolves to one stable node for the controller's lifetime. */
  function node(selector) {
    if (!nodes.has(selector)) nodes.set(selector, new ControllerNode(selector === "#borrower" ? "select" : "div"));
    return nodes.get(selector);
  }
  globalThis.document = {
    /** Static anchor lookup matches the production template contract. */
    querySelector: node,
    documentElement: { dataset: {} },
    /** Result rendering uses the same literal-text seam as the XSS check. */
    createElement(tag) { return new ControllerNode(tag); },
    /** Preserve packet content as one rendered fragment. */
    createDocumentFragment() { return new ControllerNode("fragment"); },
  };
  globalThis.window = { location: { hostname: "localhost", port: "8000" } };
  /** Model native options without introducing an unneeded DOM dependency. */
  globalThis.Option = class extends ControllerNode { constructor(text, value) { super("option"); this.textContent = text; this.value = value; } };
  /** Borrowers resolve immediately while queries remain under explicit test control. */
  globalThis.fetch = async (url, options) => {
    if (url === "/api/v1/borrowers") return json({ mode: "demo", borrowers: [{ borrower_id: "borrower-001", name: "First borrower", industry: "Test" }, { borrower_id: "borrower-002", name: "Second borrower", industry: "Test" }] });
    /** Deliberately ignore abort so the generation guard, rather than transport behavior, protects the view. */
    return new Promise(resolve => pending.push({ resolve, options }));
  };
  try {
    await import("../src/main.ts?state-test"); await settle();
    node("#question").value = "Original question";
    /** Invoke the submitted form contract directly; no OS or browser UI automation is used. */
    const first = node("#query-form").handlers.get("submit")({ preventDefault() {} });
    expect(pending).toHaveLength(1);
    node("#borrower").value = "borrower-002"; node("#borrower").handlers.get("change")();
    expect(pending[0].options.signal.aborted).toBe(true); expect(node("#packet").hidden).toBe(true);
    node("#question").value = "New borrower question";
    const second = node("#query-form").handlers.get("submit")({ preventDefault() {} });
    const latest = fixture(); latest.borrower_id = "borrower-002"; pending[1].resolve(json(latest)); await second;
    expect(node("#packet").textContent).toContain("Second borrower");
    pending[0].resolve(json(fixture())); await first;
    expect(node("#packet").textContent).toContain("Second borrower"); expect(node("#packet").textContent).not.toContain("First borrower");
    node("#effective-date").value = "2025-01-01"; node("#effective-date").handlers.get("change")();
    expect(node("#packet").hidden).toBe(true); expect(node("#packet").textContent).toBe("");
  } finally { Object.assign(globalThis, original); }
});

/** Malformed JSON parsing errors must not echo fragments of potentially sensitive response bodies. */
test("invalid JSON produces a curated error", async () => {
  /** Return a malformed private payload to exercise response parsing, not server authorization. */
  const transport = async () => new Response("sensitive malformed payload", { headers: { "Content-Type": "application/json" } });
  await expect(new CreditLensApi("", transport).borrowers(new AbortController().signal)).rejects.toThrow("The API returned invalid JSON. No result was accepted.");
});

/** Recorded samples must never present captured measurements as a current query. */
test("recorded packets clearly label captured timings", () => {
  const output = withDocument(() => renderPacket(parsePacket(fixture()), { borrower_id: "borrower-001", question: "Recorded question", effective_at: "2025-01-01" }, "Synthetic Borrower", 9, true));
  expect(output.textContent).toContain("Recorded local service time");
  expect(output.textContent).toContain("Recorded API round trip");
});
