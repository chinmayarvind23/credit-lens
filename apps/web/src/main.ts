import { CreditLensApi, apiBase } from "./api";
import type { Borrower, Chunk, Packet, QueryRequest } from "./contracts";
import { element, renderPacket, renderSource } from "./render";

/** Resolve static document anchors once and fail visibly if template and controller drift. */
function required<T extends HTMLElement>(selector: string): T {
  const node = document.querySelector<T>(selector);
  if (!node) throw new Error(`CreditLens document contract is missing ${selector}.`);
  return node;
}

const api = new CreditLensApi(apiBase(window.location));
const borrowerSelect = required<HTMLSelectElement>("#borrower");
const effectiveDate = required<HTMLInputElement>("#effective-date");
const question = required<HTMLTextAreaElement>("#question");
const runButton = required<HTMLButtonElement>("#run-query");
const cancelButton = required<HTMLButtonElement>("#cancel-query");
const status = required("#request-status");
const errorBanner = required("#request-error");
const packetContainer = required("#packet");
const emptyState = required("#empty-state");
const dialog = required<HTMLDialogElement>("#source-dialog");
const sourceContent = required("#source-content");
let borrowers: Borrower[] = [];
let scopeController: AbortController | undefined;
let queryController: AbortController | undefined;
let sourceController: AbortController | undefined;
let completed: { packet: Packet; request: QueryRequest } | undefined;

/** A local calendar date avoids changing policy scope for users west or east of UTC midnight. */
function today(): string {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
}

/** Unknown failures get a useful generic message without dumping payloads, tokens, or stack traces into the UI. */
function showError(error: unknown): void {
  errorBanner.textContent = error instanceof Error ? error.message : "The request could not be completed. Please try again.";
  errorBanner.hidden = false;
}

/** Busy state changes preserve scope controls so changing a borrower can cancel an in-flight query. */
function setBusy(busy: boolean): void {
  runButton.disabled = busy || borrowerSelect.disabled || !borrowerSelect.value;
  runButton.textContent = busy ? "Preparing evidence…" : "Build evidence packet ↗";
  cancelButton.hidden = !busy;
  packetContainer.setAttribute("aria-busy", String(busy));
  status.classList.toggle("is-loading", busy);
}

/** Clearing a snapshot also cancels source inspection, preventing stale evidence after identity or scope changes. */
function clearPacket(): void {
  queryController?.abort(); queryController = undefined;
  sourceController?.abort(); sourceController = undefined;
  completed = undefined;
  packetContainer.replaceChildren(); packetContainer.hidden = true; emptyState.hidden = false;
  sourceContent.replaceChildren(); if (dialog.open) dialog.close();
  errorBanner.hidden = true; status.textContent = ""; setBusy(false);
}

/** Industry is descriptive only; all real permission checks remain server-side. */
function updateBorrowerDescription(): void {
  let selected: Borrower | undefined;
  for (const borrower of borrowers) if (borrower.borrower_id === borrowerSelect.value) selected = borrower;
  required("#borrower-industry").textContent = selected ? `${selected.industry} · ${selected.borrower_id}` : "No authorized borrower selected.";
}

/** Refresh invalidates all old evidence before loading the current identity's authorized borrower list. */
async function loadBorrowers(): Promise<void> {
  clearPacket(); scopeController?.abort();
  const controller = new AbortController(); scopeController = controller;
  borrowers = []; borrowerSelect.disabled = true;
  borrowerSelect.replaceChildren(new Option("Loading authorized borrowers…", ""));
  setBusy(false); status.textContent = "Loading authorized borrower scope…";
  required("#environment").textContent = "Connecting";
  required("#environment-notice").textContent = "Checking the service environment and authorized borrower scope.";
  try {
    const result = await api.borrowers(controller.signal);
    if (scopeController !== controller || controller.signal.aborted) return;
    borrowers = result.borrowers;
    borrowerSelect.replaceChildren();
    for (const borrower of borrowers) borrowerSelect.append(new Option(borrower.name, borrower.borrower_id));
    if (!borrowers.length) borrowerSelect.append(new Option("No authorized borrowers", ""));
    borrowerSelect.disabled = !borrowers.length;
    required("#environment").textContent = result.mode === "demo" ? "Synthetic demo" : "Production";
    required("#environment-notice").textContent = result.mode === "demo"
      ? "SYNTHETIC DEMO · Fictional borrowers and policy documents. Review outputs demonstrate the workflow; they are not real lending advice."
      : "PRODUCTION · Access is restricted to your server-authorized borrower scope. Review all cited evidence before making a lending decision.";
    status.textContent = borrowers.length ? "Authorized borrower scope loaded. Ready for your question." : "No borrowers are available to this identity. Contact your administrator.";
    updateBorrowerDescription(); setBusy(false);
  } catch (error) {
    if (scopeController !== controller || controller.signal.aborted) return;
    borrowerSelect.replaceChildren(new Option("Connection unavailable", ""));
    required("#environment").textContent = "Not connected";
    required("#environment-notice").textContent = "Environment unverified. Connect to the service before reviewing evidence.";
    status.textContent = "Borrower scope could not be loaded."; updateBorrowerDescription(); showError(error);
  }
}

/** Submission freezes the selected context, validates the returned borrower, and suppresses superseded responses. */
async function submitQuery(event: SubmitEvent): Promise<void> {
  event.preventDefault();
  if (!effectiveDate.reportValidity()) return;
  const request: QueryRequest = { borrower_id: borrowerSelect.value, question: question.value.trim(), effective_at: effectiveDate.value };
  if (!request.borrower_id || request.question.length < 3 || request.question.length > 2000) { showError(new Error("Select a borrower and enter a question between 3 and 2,000 characters.")); return; }
  let borrowerName = request.borrower_id;
  for (const borrower of borrowers) if (borrower.borrower_id === request.borrower_id) borrowerName = borrower.name;
  clearPacket(); const controller = new AbortController(); queryController = controller;
  const started = performance.now(); setBusy(true);
  status.textContent = "Preparing authorized evidence, calculations, and citations…";
  try {
    const packet = await api.query(request, controller.signal);
    if (queryController !== controller || controller.signal.aborted) return;
    if (packet.borrower_id !== request.borrower_id) throw new Error("The service returned a mismatched borrower. This result was discarded.");
    completed = { packet, request };
    packetContainer.replaceChildren(renderPacket(packet, request, borrowerName, performance.now() - started));
    packetContainer.hidden = false; emptyState.hidden = true;
    status.textContent = packet.abstained ? "Evidence packet ready. The service abstained from a supported assessment." : "Evidence packet ready for your review.";
  } catch (error) {
    if (queryController !== controller || controller.signal.aborted) return;
    status.textContent = "No evidence packet was accepted."; showError(error);
  } finally {
    if (queryController === controller) { queryController = undefined; setBusy(false); }
  }
}

/** Source provenance must match the completed packet even after a separate, freshly authorized evidence request. */
function sameSource(actual: Chunk, expected: Chunk): boolean {
  return actual.chunk_id === expected.chunk_id && actual.document_id === expected.document_id && actual.document_version === expected.document_version && actual.page === expected.page && actual.content_hash === expected.content_hash;
}

/** Delegation supports dynamically rendered citation buttons while every source fetch stays bound to a snapshot. */
async function inspectCitation(event: MouseEvent): Promise<void> {
  const target = event.target instanceof Element ? event.target.closest<HTMLButtonElement>("button[data-chunk-id]") : null;
  const snapshot = completed; const chunkId = target?.dataset.chunkId;
  if (!snapshot || !chunkId) return;
  let expected: Chunk | undefined;
  for (const chunk of snapshot.packet.evidence) if (chunk.chunk_id === chunkId) expected = chunk;
  sourceController?.abort(); const controller = new AbortController(); sourceController = controller;
  required("#source-title").textContent = "Evidence page";
  sourceContent.replaceChildren(element("p", "field-note", "Checking access and loading the cited source…"));
  if (!dialog.open) dialog.showModal();
  try {
    if (!expected) throw new Error("This citation does not reference a retrieved source in this packet.");
    const source = await api.evidence(chunkId, snapshot.request, controller.signal);
    if (sourceController !== controller || controller.signal.aborted || completed !== snapshot) return;
    if (!sameSource(source, expected)) throw new Error("Source provenance changed. Run the question again to obtain a current packet.");
    required("#source-title").textContent = source.title;
    sourceContent.replaceChildren(renderSource(source));
  } catch (error) {
    if (sourceController !== controller || controller.signal.aborted || completed !== snapshot) return;
    sourceContent.replaceChildren(element("p", "error-banner", error instanceof Error ? error.message : "Source inspection failed."));
  }
}

/** Scope changes erase previous packets before allowing the next question. */
function changeScope(): void { clearPacket(); updateBorrowerDescription(); status.textContent = "Scope changed. Ask a question for the selected borrower and policy date."; }
/** Example questions edit the prompt only; users explicitly submit each evidence request. */
function chooseSuggestion(event: MouseEvent): void {
  const button = event.target instanceof Element ? event.target.closest<HTMLButtonElement>("[data-question]") : null;
  if (!button?.dataset.question) return;
  question.value = button.dataset.question; updateQuestionLength(); question.focus();
}
/** Text length feedback follows actual input without sending draft questions to telemetry. */
function updateQuestionLength(): void { required("#question-length").textContent = `${question.value.length.toLocaleString()} / 2,000`; }
/** Changing identity clears all old scope and removes the token from the form after applying it in memory. */
function applyConnection(): void {
  clearPacket(); const token = required<HTMLInputElement>("#access-token"); api.setToken(token.value); token.value = ""; void loadBorrowers();
}
/** Cancellation removes the in-flight result and remains safe even if the server finishes after abort. */
function cancelQuery(): void { clearPacket(); status.textContent = "Request cancelled. No new packet was accepted."; }
/** Native dialog close and Escape both cancel its source fetch and remove the source text. */
function clearSource(): void { sourceController?.abort(); sourceController = undefined; sourceContent.replaceChildren(); }
/** Close through the native dialog API to restore focus to the citation that opened it. */
function closeSource(): void { dialog.close(); }
/** An explicit refresh gives users a recovery action for connectivity and permission changes. */
function refreshScope(): void { void loadBorrowers(); }

effectiveDate.value = today();
required<HTMLFormElement>("#query-form").addEventListener("submit", submitQuery);
borrowerSelect.addEventListener("change", changeScope);
effectiveDate.addEventListener("change", changeScope);
question.addEventListener("input", updateQuestionLength);
required(".suggestions").addEventListener("click", chooseSuggestion);
required("#connect").addEventListener("click", applyConnection);
required("#refresh-scope").addEventListener("click", refreshScope);
cancelButton.addEventListener("click", cancelQuery);
packetContainer.addEventListener("click", inspectCitation);
required("#close-source").addEventListener("click", closeSource);
dialog.addEventListener("close", clearSource);
void loadBorrowers();
