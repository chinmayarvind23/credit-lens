# Optional public demo directory

The browser can read the five synthetic borrower labels from Supabase's REST API.
Evidence, questions, financial calculations and session audits stay in the Python
browser worker. The default Hugging Face release uses its bundled directory and
does not require Supabase or make directory requests.

For an operator-owned Supabase project, execute `directory.sql` once using the SQL
editor as an administrator. Use a dedicated synthetic project: this table is
public. The migration enables and forces RLS, grants only three readable columns
to `anon` and `authenticated`, and exposes only published rows. It grants no writes.
The labels intentionally match `build_demo_borrowers()` exactly, including its
synthetic industry assignments.

Before building your browser deployment, add these two tags inside the `<head>`
of `apps/web/index.html` in your own checkout:

```html
<meta name="creditlens-supabase-origin" content="https://YOUR_PROJECT.supabase.co">
<meta name="creditlens-supabase-key" content="sb_publishable_YOUR_PUBLIC_KEY">
```

Use only the publishable key from the project's API-key settings. The client
rejects secret and legacy JWT keys, non-HTTPS origins, URL paths and arbitrary
hosts. Never place a service-role key in browser assets. Review and commit the
public configuration before following the [browser build guide](../huggingface/browser/DEPLOYMENT.md).
The normal build and publication manifest then hash that configuration with the
rest of the app. No resource creation, billing upgrade or paid operation is part
of this integration.

The client sends one credential-free GET with the publishable `apikey` header,
no referrer and no redirects. Responses are bounded to 16 KiB and three seconds.
IDs, names and industries must match the local fixture exactly; stale or changed
rows, missing rows, duplicates, errors and offline access fall back to bundled
metadata. Cancellation propagates. Server mode does not read this configuration.
Changing the directory cannot change the evidence engine's borrower scope.

## Local verification

Start a fresh owned fixture with the pinned PostgreSQL command in the
[PostgreSQL guide](../postgres/README.md), then run:

```powershell
uv run --no-sync python -m infra.supabase.check_permissions --port 15432 --output ../creditlens-supabase-permissions.json
cd apps/web
bun run typecheck
bun test
bun run build
```

The SQL check verifies both roles can read the five canonical rows, cannot read
the publication flag, insert, update, delete, truncate or disable RLS, and cannot
read an unpublished row. It accepts only loopback `creditlens_test` with fixture
credentials and requires an empty public schema. Stop only your owned fixture
when finished. Frontend tests exercise transport, drift, cancellation and timeout.

These checks verify local SQL and client contracts. They do not claim a managed
Supabase deployment or an end-to-end hosted REST check. The free public HF demo
remains self-contained until an operator explicitly configures this path.

Sources: [Supabase API keys](https://supabase.com/docs/guides/getting-started/api-keys),
[REST API](https://supabase.com/docs/guides/api/creating-routes),
[row-level security](https://supabase.com/docs/guides/database/postgres/row-level-security).
