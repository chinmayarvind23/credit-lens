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

// Injected transport tests inspect HTTP behavior without pretending to exercise server authorization.
describe("API transport", () => {
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
