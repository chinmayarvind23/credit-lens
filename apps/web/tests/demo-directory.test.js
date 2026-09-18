import { expect, test } from "bun:test";
import { directoryConfig, demoDirectory } from "../src/demo-directory";

const local = { mode: "demo", borrowers: [{ borrower_id: "borrower-001", name: "Synthetic", industry: "Test" }] };
const config = { origin: "https://synthetic.supabase.co", key: "sb_publishable_syntheticfixtureonly" };
/** Canned responses exercise only transport contracts; real SQL permissions are checked separately. */
function response(value) { return new Response(JSON.stringify(value)); }

/** Reject credentials that could grant privileged database access before issuing a request. */
test("configuration accepts only publishable keys and exact HTTPS project origins", () => {
  expect(directoryConfig("", "")).toBeUndefined();
  expect(directoryConfig(config.origin, config.key)).toEqual(config);
  for (const key of ["sb_secret_example", "eyJlegacyjwt", ""]) expect(() => directoryConfig(config.origin, key)).toThrow();
  for (const origin of ["http://synthetic.supabase.co", "https://synthetic.supabase.co.evil.test", "https://synthetic.supabase.co/path", "https://u:p@synthetic.supabase.co"]) expect(() => directoryConfig(origin, config.key)).toThrow();
});

/** Default and production paths must never acquire a new public network dependency. */
test("unconfigured and production directories make no network call", async () => {
  const forbidden = () => { throw new Error("unexpected fetch"); };
  expect(await demoDirectory(local, undefined, new AbortController().signal, forbidden)).toBe(local);
  const production = { ...local, mode: "production" };
  expect(await demoDirectory(production, config, new AbortController().signal, forbidden)).toBe(production);
});

/** No question, evidence, token or audit content is included in directory transport. */
test("reads matching labels using one restricted GET", async () => {
  let calls = 0;
  const result = await demoDirectory(local, config, new AbortController().signal, async (url, options) => {
    calls++;
    expect(url).toBe(`${config.origin}/rest/v1/creditlens_demo_borrowers?select=borrower_id,name,industry&order=borrower_id&limit=6`);
    expect(options.credentials).toBe("omit"); expect(options.redirect).toBe("error");
    expect(options.headers).toEqual({ apikey: config.key, Accept: "application/json" });
    expect(options.body).toBeUndefined();
    return response(local.borrowers);
  });
  expect(calls).toBe(1); expect(result).toEqual(local); expect(result).not.toBe(local);
});

/** Corrupt, stale and oversized metadata cannot relabel the Python evidence fixture. */
test("falls back on mismatches, duplicates, errors and oversized bodies", async () => {
  const b = local.borrowers[0];
  for (const value of [[], [{ ...b, name: "Changed" }], [{ ...b, industry: "Changed" }], [{ ...b, borrower_id: "restricted" }], [b, b], {}, [{ ...b, name: "x".repeat(17000) }]]) {
    expect(await demoDirectory(local, config, new AbortController().signal, async () => response(value))).toBe(local);
  }
  expect(await demoDirectory(local, config, new AbortController().signal, async () => new Response("denied", { status: 403 }))).toBe(local);
  expect(await demoDirectory(local, config, new AbortController().signal, async () => { throw new Error("offline"); })).toBe(local);
});

/** A superseded refresh must not render the fallback after user cancellation. */
test("propagates cancellation before and during fetch", async () => {
  const cancelled = new AbortController(); cancelled.abort();
  await expect(demoDirectory(local, config, cancelled.signal)).rejects.toThrow("Cancelled");
  const active = new AbortController();
  await expect(demoDirectory(local, config, active.signal, async () => { active.abort(); throw new Error("aborted"); })).rejects.toThrow("Cancelled");
  const late = new AbortController();
  await expect(demoDirectory(local, config, late.signal, async () => { late.abort(); return response(local.borrowers); })).rejects.toThrow("Cancelled");
});

/** The optional service cannot indefinitely block the usable bundled demo. */
test("falls back when the directory exceeds its deadline", async () => {
  const result = await demoDirectory(local, config, new AbortController().signal, async (_url, options) => new Promise((_resolve, reject) => {
    options.signal.addEventListener("abort", () => reject(new Error("deadline")), { once: true });
  }));
  expect(result).toBe(local);
});
