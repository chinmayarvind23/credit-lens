import { parseBorrowers, parseChunk, parsePacket } from "./contracts";
import type { BorrowerList, Chunk, Packet, QueryRequest } from "./contracts";

/** The local Bun development origin alone may target port 8000; deployed assets always use their own origin. */
export function apiBase(location: Pick<Location, "hostname" | "port">): string {
  return location.hostname === "localhost" && location.port === "3000" ? "http://localhost:8000" : "";
}
/** Keep tokens in memory and make retries explicit to avoid replaying expensive underwriting requests. */
export class CreditLensApi {
  private token = "";
  /** Injecting fetch allows failure and cancellation checks without a browser or a live backend. */
  constructor(private readonly base: string, private readonly transport: typeof fetch = fetch) {}
  /** Replacing the token affects subsequent requests; callers clear results before changing identity. */
  setToken(token: string): void { this.token = token.trim(); }
  /** Borrower selection is populated only from the server's current authorized response. */
  async borrowers(signal: AbortSignal): Promise<BorrowerList> {
    return parseBorrowers(await this.request("/api/v1/borrowers", signal));
  }
  /** The wire request contains a borrower selector, question, and date, with no client-provided ACL. */
  async query(request: QueryRequest, signal: AbortSignal): Promise<Packet> {
    return parsePacket(await this.request("/api/v1/query", signal, request));
  }
  /** Reauthorize source inspection server-side with the date and borrower of the completed packet. */
  async evidence(chunkId: string, request: QueryRequest, signal: AbortSignal): Promise<Chunk> {
    const query = new URLSearchParams({ borrower_id: request.borrower_id, effective_at: request.effective_at });
    return parseChunk(await this.request(`/api/v1/evidence/${encodeURIComponent(chunkId)}?${query}`, signal));
  }
  /** Bounded fetch, no-store, and generic errors prevent silent hangs, browser persistence, and reflected server payloads. */
  private async request(path: string, signal: AbortSignal, body?: QueryRequest): Promise<unknown> {
    const controller = new AbortController();
    /** Forward user/scope cancellation to the same controller used for the timeout. */
    const cancel = (): void => controller.abort();
    signal.addEventListener("abort", cancel, { once: true });
    if (signal.aborted) controller.abort();
    let timedOut = false;
    /** An explicit 30-second client deadline bounds even a stalled response body. */
    const timeout = setTimeout((): void => { timedOut = true; controller.abort(); }, 30_000);
    try {
      const headers = new Headers({ Accept: "application/json" });
      if (this.token) headers.set("Authorization", `Bearer ${this.token}`);
      if (body) headers.set("Content-Type", "application/json");
      const options: RequestInit = { method: body ? "POST" : "GET", headers, signal: controller.signal, cache: "no-store", credentials: "omit", redirect: "error" };
      if (body) options.body = JSON.stringify(body);
      const response = await this.transport(this.base + path, options);
      if (!response.ok) {
        if (response.status === 401) throw new Error("Authentication is required or has expired. Update your managed access token in Connection.");
        if (response.status === 403) throw new Error("This identity cannot access the requested borrower or evidence. Refresh your authorized borrower list.");
        if (response.status === 404) throw new Error("The requested borrower or source is no longer available.");
        if (response.status === 429) throw new Error("The service is busy. Wait briefly before trying again.");
        if (response.status === 422) throw new Error("The service could not accept the question or effective date. Check your inputs.");
        throw new Error(`The service could not complete this request (HTTP ${response.status}). Try again when the service is available.`);
      }
      if (!response.headers.get("Content-Type")?.includes("application/json")) throw new Error("The API returned an unexpected response format.");
      try { return await response.json(); }
      catch { throw new Error("The API returned invalid JSON. No result was accepted."); }
    } catch (error) {
      if (timedOut) throw new Error("The request exceeded 30 seconds. No result was accepted. You can try again.");
      if (error instanceof TypeError) throw new Error("Cannot reach the API. Check your connection and that the CreditLens service is running.");
      throw error;
    } finally { clearTimeout(timeout); signal.removeEventListener("abort", cancel); }
  }
}
