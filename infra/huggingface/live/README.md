---
title: CreditLens Live
emoji: 🔎
colorFrom: green
colorTo: gray
sdk: static
app_file: index.html
pinned: false
---

# CreditLens live workbench

Ask a question, select a synthetic borrower and effective date, build an evidence
packet, and inspect its source pages. Each request reaches the live FastAPI
application; this version does not replay recorded answers.

To stay within the no-spending restriction, Hugging Face serves the small web
entry point while the backend runs on the owner's computer through a free HTTPS
Cloudflare Quick Tunnel. The computer, Docker container and tunnel must remain
running. The temporary tunnel address changes when restarted. This is a live
demonstration, not an always-available managed deployment.

Only synthetic lending data is available. Financial checks use the synthetic
policy, and the underwriter retains the final lending decision. Live model-based
generation, production identity integration and semantic quality grading remain
unfinished. No GPU, paid runtime, inference job or persistent HF storage is requested.

[Source code](https://github.com/chinmayarvind23/credit-lens)
