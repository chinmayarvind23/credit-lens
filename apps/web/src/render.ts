import type { Citation, Claim, Chunk, Disposition, Packet, QueryRequest } from "./contracts";

export const dispositionLabels: Record<Disposition, { title: string; description: string; tone: string }> = {
  MEETS_POLICY: { title: "Meets reviewed policy", description: "Review the cited requirements and evidence before making a lending decision.", tone: "green" },
  EXCEPTION_REQUIRED: { title: "Policy exception required", description: "Review the exception and route it through the institution's approval process.", tone: "amber" },
  INSUFFICIENT_EVIDENCE: { title: "More evidence needed", description: "The available sources do not support a complete policy assessment.", tone: "amber" },
  MATERIAL_CONFLICT: { title: "Material conflict to resolve", description: "Sources disagree. Reconcile the conflicting evidence before relying on a conclusion.", tone: "red" },
  HUMAN_JUDGMENT_REQUIRED: { title: "Underwriter judgment required", description: "The evidence requires interpretation by an authorized reviewer.", tone: "amber" },
};

/** The local extractor assesses DSCR only; its banner must not imply a broader policy determination. */
export function packetDisposition(packet: Pick<Packet, "provider_mode" | "policy_disposition">): { title: string; description: string; tone: string } {
  const label = dispositionLabels[packet.policy_disposition];
  if (packet.provider_mode !== "local-extractive") return label;
  if (packet.policy_disposition === "MEETS_POLICY") return { ...label, title: "Meets DSCR threshold", description: "Only DSCR was assessed; review remaining requirements." };
  if (packet.policy_disposition === "EXCEPTION_REQUIRED") return { ...label, title: "DSCR exception required", description: "The assessed DSCR is below the policy threshold. Review the DSCR exception and remaining requirements." };
  return label;
}

/** All dynamic values become literal text, so hostile source text cannot introduce HTML or event handlers. */
export function element<K extends keyof HTMLElementTagNameMap>(tag: K, className = "", text?: string): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag); node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
/** Humanize trace and field labels without altering the underlying identifier stored in the packet. */
export function humanize(value: string): string { return value.replaceAll("_", " "); }
/** Timing precision is display-only and does not imply benchmark or percentile evidence. */
export function duration(ms: number): string { return ms >= 1000 ? `${(ms / 1000).toFixed(2)} s` : `${ms.toFixed(1)} ms`; }
/** Source buttons carry opaque IDs as data, never server-provided URLs or executable markup. */
function citationButton(citation: Citation, sources: Map<string, Chunk>): HTMLButtonElement {
  const source = sources.get(citation.chunk_id);
  const label = `${source?.title ?? citation.document_id} · p. ${citation.page}`;
  const button = element("button", "citation", label); button.type = "button";
  button.dataset.chunkId = citation.chunk_id;
  button.setAttribute("aria-label", `Inspect source: ${label}, version ${citation.document_version}`);
  return button;
}
/** Grouping citations under their exact claim prevents the appearance of support for adjacent uncited text. */
function citationGroup(citations: Citation[], sources: Map<string, Chunk>): HTMLElement {
  const group = element("div", "citations");
  for (const citation of citations) group.append(citationButton(citation, sources));
  return group;
}
/** Claim lists keep provenance adjacent and give empty collections a neutral, scoped description. */
function claimSection(title: string, claims: Claim[], sources: Map<string, Chunk>, empty: string): HTMLElement {
  const section = element("section", "result-card"); const heading = element("div", "panel-heading");
  heading.append(element("h3", "", title), element("span", "count", String(claims.length))); section.append(heading);
  if (!claims.length) section.append(element("p", "empty-note", empty));
  for (const claim of claims) {
    const row = element("div", "claim-row"); row.append(element("p", "claim-text", claim.text), citationGroup(claim.citations, sources)); section.append(row);
  }
  return section;
}
/** Action and gap lists describe only what the service returned, without claiming a complete review. */
function textSection(title: string, items: string[], empty: string, kind = ""): HTMLElement {
  const section = element("section", `result-card ${kind}`); const heading = element("div", "panel-heading");
  heading.append(element("h3", "", title), element("span", "count", String(items.length))); section.append(heading);
  if (!items.length) section.append(element("p", "empty-note", empty));
  else { const list = element("ul", "review-list"); for (const item of items) list.append(element("li", "", item)); section.append(list); }
  return section;
}
/** Render an immutable request snapshot so editing the question or date cannot relabel an older result. */
export function renderPacket(packet: Packet, request: QueryRequest, borrowerName: string, elapsedMs: number): DocumentFragment {
  const fragment = document.createDocumentFragment(); const sources = new Map<string, Chunk>();
  for (const source of packet.evidence) sources.set(source.chunk_id, source);
  const heading = element("div", "result-heading"); const headingCopy = element("div");
  headingCopy.append(element("p", "eyebrow", "Evidence packet"), element("h2", "", borrowerName));
  heading.append(headingCopy, element("span", "subtle", `Policy date ${request.effective_at} · Provider: ${packet.provider_mode}`)); fragment.append(heading);
  fragment.append(element("p", "submitted-question", request.question));
  const disposition = packetDisposition(packet); const banner = element("section", `disposition ${disposition.tone}`);
  banner.append(element("p", "eyebrow", "Policy disposition"), element("h3", "", disposition.title), element("p", "", disposition.description));
  if (packet.abstained) banner.append(element("p", "abstention", "Assessment withheld: the service abstained from a supported conclusion."));
  fragment.append(banner);
  const summary = claimSection("Borrower evidence", packet.borrower_summary, sources, "No borrower summary claims were returned for this question."); fragment.append(summary);
  const metricHeading = element("div", "result-subheading"); metricHeading.append(element("h3", "", "Calculated financial metrics"), element("span", "subtle", "Computed by the service")); fragment.append(metricHeading);
  const metrics = element("div", "metric-grid");
  if (!packet.calculated_metrics.length) metrics.append(element("p", "empty-note", "No financial metrics were returned for this question."));
  for (const metric of packet.calculated_metrics) {
    const card = element("section", "metric-card"); card.append(element("h4", "", humanize(metric.name)));
    const amount = element("p", "metric-value", metric.value); amount.append(element("span", "metric-unit", metric.unit)); card.append(amount);
    card.append(element("p", "field-note", `Inputs: ${metric.source_fields.join(", ")}`), citationGroup(metric.citations, sources)); metrics.append(card);
  }
  fragment.append(metrics);
  const columns = element("div", "result-columns"); const left = element("div", "result-column"); const right = element("div", "result-column");
  left.append(claimSection("Applicable policy", packet.applicable_policy, sources, "No applicable policy claims were returned."), claimSection("Policy exceptions", packet.exceptions, sources, "No policy exceptions were returned for this question."), claimSection("Conflicting evidence", packet.contradictions, sources, "No conflicting claims were returned for this question."));
  right.append(textSection("Missing documents", packet.missing_documents, "No missing documents were reported for this question."), textSection("Recommended next actions", packet.recommended_next_actions, "No next actions were returned.", "next-actions"), textSection("Questions for the underwriter", packet.questions_for_underwriter, "No additional questions were returned."));
  columns.append(left, right); fragment.append(columns);
  const evidenceSection = element("section", "result-card"); const evidenceHeading = element("div", "panel-heading");
  evidenceHeading.append(element("h3", "", "Retrieved evidence"), element("span", "count", String(packet.evidence.length))); evidenceSection.append(evidenceHeading);
  if (!packet.evidence.length) evidenceSection.append(element("p", "empty-note", "No source chunks were returned."));
  for (const source of packet.evidence) {
    const row = element("div", "evidence-row"); const info = element("div");
    info.append(element("h4", "", source.title), element("p", "field-note", `${humanize(source.document_kind)} · ${source.section} · version ${source.document_version}`));
    row.append(info, citationButton(source, sources)); evidenceSection.append(row);
  }
  fragment.append(evidenceSection);
  const trace = element("details", "trace-card"); trace.append(element("summary", "", `Execution trace · ${duration(packet.latency_ms)} service time`));
  const metadata = element("dl", "trace-metadata");
  const fields = [["Provider mode", packet.provider_mode], ["Corpus version", packet.corpus_version], ["Request ID", packet.request_id], ["Service time", duration(packet.latency_ms)], ["Browser round trip", duration(elapsedMs)], ["Cache", packet.cache_hit ? "Hit" : "Miss"], ["Reported request cost", packet.cost_usd === null ? "Not reported" : `$${packet.cost_usd}`]];
  for (const [label, value] of fields) { metadata.append(element("dt", "", label), element("dd", "", value)); }
  trace.append(metadata, element("p", "field-note", "Timings describe this request. They are not benchmark percentiles. Browser round trip includes transfer and response validation."));
  const stages = element("ol", "stage-list");
  for (const stage of packet.stages) { const row = element("li"); row.append(element("span", "", humanize(stage.name)), element("span", "stage-duration", duration(stage.duration_ms))); stages.append(row); }
  if (!packet.stages.length) trace.append(element("p", "empty-note", "No stage timings were returned."));
  trace.append(stages); fragment.append(trace);
  return fragment;
}
/** The drawer exposes literal extracted text and provenance; it never executes or links embedded source instructions. */
export function renderSource(source: Chunk): DocumentFragment {
  const fragment = document.createDocumentFragment();
  fragment.append(element("p", "source-meta", `Page ${source.page} · Version ${source.document_version} · ${humanize(source.document_kind)}`), element("p", "source-section", source.section));
  const dateText = source.valid_to ? `${source.valid_from} to ${source.valid_to} (end exclusive)` : `From ${source.valid_from}`;
  fragment.append(element("p", "field-note", `Effective window: ${dateText}`));
  const pre = element("pre", "source-text", source.text); pre.tabIndex = 0; pre.setAttribute("aria-label", "Extracted source text"); fragment.append(pre);
  const details = element("details", "source-provenance"); details.append(element("summary", "", "Provenance and extraction"));
  const fields = [["Document", source.document_id], ["Chunk", source.chunk_id], ["Content hash", source.content_hash], ["Parser", source.parser_version], ["Chunker", source.chunker_version], ["Character span", `${source.start_char}–${source.end_char}`], ["Extraction confidence", `${(source.extraction_confidence * 100).toFixed(1)}%`]];
  const metadata = element("dl", "trace-metadata"); for (const [label, value] of fields) metadata.append(element("dt", "", label), element("dd", "", value));
  details.append(metadata); fragment.append(details); return fragment;
}
