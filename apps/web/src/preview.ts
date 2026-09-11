import { parseRecordings, type RecordedExample } from "./preview-contracts";
import { element, renderPacket, renderSource } from "./render";

/** Static element lookup fails visibly if the shipped HTML and controller do not match. */
function required<T extends HTMLElement>(id: string): T { const node = document.getElementById(id); if (!node) throw new Error(`Missing preview element: ${id}`); return node as T; }

/** The preview fetches one same-origin recording file and never sends borrower questions or tokens. */
async function initialize(): Promise<void> {
  const status = required("request-status"); const error = required("request-error"); const selector = required<HTMLSelectElement>("example"); const packet = required("packet"); const dialog = required<HTMLDialogElement>("source-dialog"); let selected: RecordedExample | undefined;
  required("close-source").addEventListener("click", () => dialog.close());
  packet.addEventListener("click", event => {
    const target = event.target instanceof Element ? event.target.closest<HTMLButtonElement>("button[data-chunk-id]") : null;
    const source = target?.dataset.chunkId ? selected?.sources.get(target.dataset.chunkId) : undefined;
    if (!source) return;
    required("source-title").textContent = source.title; required("source-content").replaceChildren(element("p", "field-note", "This source response was recorded locally. Opening it does not make an authorization or API request."), renderSource(source)); dialog.showModal();
  });
  try {
    const response = await fetch("./recordings.json", { credentials: "omit", redirect: "error" });
    if (!response.ok) throw new Error("The recording file could not be loaded.");
    const body = await response.text(); if (body.length > 4_000_000) throw new Error("The recording file is too large.");
    const recordings = await parseRecordings(JSON.parse(body));
    required("capture-provenance").textContent = `Captured ${recordings.capturedAt} · API source ${recordings.sourceRevision.slice(0, 12)} · Request, response, and source hashes verified in this viewer.`;
    selector.replaceChildren(...recordings.examples.map(example => { const option = element("option", "", example.title); option.value = example.id; return option; }));
    /** Selection only changes which immutable captured packet is displayed. */
    function show(): void {
      selected = recordings.examples.find(example => example.id === selector.value); if (!selected) return;
      if (dialog.open) dialog.close();
      packet.replaceChildren(renderPacket(selected.packet, selected.request, selected.borrowerName, selected.elapsedMs, true));
      const provenance = element("details", "trace-card"); provenance.append(element("summary", "", "Recorded HTTP body hashes"), element("p", "field-note", `Request SHA-256: ${selected.requestHash}`), element("p", "field-note", `Response SHA-256: ${selected.responseHash}`)); packet.append(provenance);
      status.textContent = `Viewing recorded example: ${selected.title}. No live request was made.`;
    }
    selector.addEventListener("change", show); selector.disabled = false; show();
  } catch { error.hidden = false; error.textContent = "The recorded examples could not be verified. No packet is displayed. Reload the preview or inspect the published capture files."; status.textContent = "Recording verification failed."; }
}

void initialize();
