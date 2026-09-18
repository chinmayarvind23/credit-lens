import { parseBorrowers } from "./contracts";
import type { BorrowerList } from "./contracts";

export interface DirectoryConfig { origin: string; key: string }

/** Operator HTML opts in; reject secret keys and arbitrary destinations before any fetch. */
export function directoryConfig(origin: string, key: string): DirectoryConfig | undefined {
  if (!origin && !key) return undefined;
  if (!/^https:\/\/[a-z0-9-]+\.supabase\.co$/.test(origin) || !/^sb_publishable_[A-Za-z0-9_-]{10,200}$/.test(key)) {
    throw new Error("The demo directory requires a Supabase HTTPS origin and publishable key.");
  }
  return { origin, key };
}

/** A remote directory may reorder synthetic labels but cannot change local evidence identities. */
export async function demoDirectory(local: BorrowerList, config: DirectoryConfig | undefined, signal: AbortSignal, fetcher: typeof fetch = fetch): Promise<BorrowerList> {
  if (signal.aborted) throw new DOMException("Cancelled", "AbortError");
  if (!config || local.mode !== "demo") return local;
  directoryConfig(config.origin, config.key);
  const controller = new AbortController();
  /** Cancellation applies to the optional network operation as well as the Python worker. */
  const cancel = (): void => controller.abort();
  signal.addEventListener("abort", cancel, { once: true });
  const timer = setTimeout(cancel, 3000);
  try {
    const response = await fetcher(`${config.origin}/rest/v1/creditlens_demo_borrowers?select=borrower_id,name,industry&order=borrower_id&limit=6`, {
      headers: { apikey: config.key, Accept: "application/json" }, signal: controller.signal,
      credentials: "omit", redirect: "error", referrerPolicy: "no-referrer",
    });
    if (!response.ok || !response.body) throw new Error("Directory unavailable");
    const reader = response.body.getReader();
    let size = 0; let body = "";
    const decoder = new TextDecoder();
    try {
      while (true) {
        const part = await reader.read();
        if (part.done) break;
        size += part.value.byteLength;
        if (size > 16_384) throw new Error("Directory too large");
        body += decoder.decode(part.value, { stream: true });
      }
      body += decoder.decode();
    } finally { await reader.cancel(); reader.releaseLock(); }
    if (controller.signal.aborted) throw new Error("Directory request cancelled");
    const remote = parseBorrowers({ mode: "demo", borrowers: JSON.parse(body) });
    const known = new Map(local.borrowers.map(b => [b.borrower_id, b]));
    const seen = new Set<string>();
    if (remote.borrowers.length !== known.size) throw new Error("Directory differs from fixture");
    for (const borrower of remote.borrowers) {
      const original = known.get(borrower.borrower_id);
      if (seen.has(borrower.borrower_id) || !original || borrower.name !== original.name || borrower.industry !== original.industry) throw new Error("Directory differs from fixture");
      seen.add(borrower.borrower_id);
    }
    return remote;
  } catch {
    if (signal.aborted) throw new DOMException("Cancelled", "AbortError");
    return local;
  } finally { clearTimeout(timer); signal.removeEventListener("abort", cancel); }
}
