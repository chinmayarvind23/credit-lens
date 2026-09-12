import { parseBorrowers, parseChunk, parsePacket } from "./contracts";
import type { BorrowerList, Chunk, Packet, QueryRequest } from "./contracts";
import { demoDirectory } from "./demo-directory";
import type { DirectoryConfig } from "./demo-directory";

interface Pending { resolve: (value: unknown) => void; reject: (reason: Error) => void }

/** Execute the same Python contracts locally; this public synthetic mode has no server or tokens. */
export class BrowserApi {
  private worker: Worker | undefined;
  private ready: Promise<void> | undefined;
  private sequence = 0;
  private pending = new Map<number, Pending>();
  /** Startup progress belongs in the existing accessible status area. */
  constructor(private readonly progress: (message: string) => void, private readonly directory?: DirectoryConfig) {}
  /** Browser demos cannot accept managed production credentials. */
  setToken(token: string): void { if (token.trim()) throw new Error("This public browser demo does not accept credentials."); }
  /** Retry startup through the normal borrower refresh action after a failed download. */
  async borrowers(signal: AbortSignal): Promise<BorrowerList> {
    const local = parseBorrowers(await this.request("borrowers", {}, signal));
    return demoDirectory(local, this.directory, signal);
  }
  /** Decimal calculation stays in Python and each query creates a fresh session audit. */
  async query(request: QueryRequest, signal: AbortSignal): Promise<Packet> { return parsePacket(await this.request("query", { request }, signal)); }
  /** Source lookup uses the same borrower/date filters as the completed packet. */
  async evidence(chunkId: string, request: QueryRequest, signal: AbortSignal): Promise<Chunk> { return parseChunk(await this.request("evidence", { chunk_id: chunkId, request }, signal)); }
  /** A worker failure rejects all callers and permits a fresh engine on explicit refresh. */
  private fail(error: Error): void {
    this.worker?.terminate(); this.worker = undefined; this.ready = undefined;
    for (const item of this.pending.values()) item.reject(error);
    this.pending.clear();
  }
  /** Separate cold-start and query deadlines keep the main UI responsive during WASM loading. */
  private start(): Promise<void> {
    if (this.ready) return this.ready;
    const worker = new Worker(new URL("./worker.js", document.baseURI)); this.worker = worker;
    this.ready = new Promise<void>((resolve, reject) => {
      const timeout = setTimeout(() => finish(new Error("Loading took too long. Refresh borrowers to retry.")), 120_000);
      /** Dispose a failed interpreter and surface a bounded, recoverable initialization error. */
      const finish = (error?: Error): void => { clearTimeout(timeout); if (error) { this.fail(error); reject(error); } else resolve(); };
      worker.onerror = () => finish(new Error("The browser evidence engine stopped. Refresh borrowers to restart."));
      worker.onmessage = (event: MessageEvent) => {
        const data = event.data;
        if (data.type === "progress") this.progress(data.message);
        else if (data.type === "ready") finish();
        else if (data.type === "fatal") finish(new Error(data.message));
        else {
          const item = this.pending.get(data.id); if (!item) return;
          this.pending.delete(data.id);
          if (data.type === "result") item.resolve(data.value); else item.reject(new Error(data.message));
        }
      };
    });
    return this.ready;
  }
  /** Cancellation discards stale results; no new request is submitted after an aborted startup. */
  private async request(operation: string, payload: object, signal: AbortSignal): Promise<unknown> {
    if (signal.aborted) throw new DOMException("Cancelled", "AbortError");
    await this.start();
    if (signal.aborted) throw new DOMException("Cancelled", "AbortError");
    const id = ++this.sequence;
    return new Promise((resolve, reject) => {
      /** Remove listeners and request bookkeeping regardless of success, failure or cancellation. */
      const cleanup = (): void => { clearTimeout(timeout); signal.removeEventListener("abort", cancel); this.pending.delete(id); };
      /** Discard a superseded result without writing query data into persistent browser storage. */
      const cancel = (): void => { cleanup(); reject(new DOMException("Cancelled", "AbortError")); };
      const timeout = setTimeout(() => { cleanup(); this.fail(new Error("The evidence engine timed out. Refresh borrowers to restart.")); reject(new Error("The query took too long. Refresh borrowers to restart.")); }, 30_000);
      signal.addEventListener("abort", cancel, { once: true });
      this.pending.set(id, { resolve: value => { cleanup(); resolve(value); }, reject: error => { cleanup(); reject(error); } });
      this.worker?.postMessage({ id, operation, payload });
    });
  }
}
