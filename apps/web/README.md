# CreditLens web

Bun 1.3.10 builds the browser application from HTML, TypeScript, and CSS. TypeScript 5.9.3 is the only development dependency. No browser runtime framework is required for the initial workspace.

From this directory:

```sh
bun install --frozen-lockfile
bun run typecheck
bun run build
bun run dev
```

Open the localhost address printed by the development server. Production static files are written to `dist/`. Build output and installed dependencies are ignored by Git.

The scaffold verifies browser initialization only. Backend integration, managed authentication, borrower selection, evidence, and underwriting packets are subsequent chunks. It contains no evaluation results or real borrower data.

The browser is untrusted. The backend must resolve tenant, borrower permissions, and ACLs. API responses must be rendered as text. No secrets may be embedded in the browser bundle.
