---
title: CreditLens
emoji: 📑
colorFrom: green
colorTo: gray
sdk: static
app_file: index.html
pinned: false
---

# CreditLens: interactive lending evidence

Select a fictional borrower, enter a question and inspect the cited source pages.
The Python evidence engine runs in your browser. No account, API key, temporary
tunnel or running owner computer is required. The first load downloads the engine;
later questions run locally. Hosting uses the free Hugging Face Static Space plan.

Try **Calculate debt service coverage and identify policy exceptions** with
Northstar Fabrication, Cedar Freight, Harbor Medical or Prairie Foods. Try
**What is the minimum DSCR?** with different policy dates, or ask an unsupported
question to see the system abstain.

This is an interactive application, with new retrieval, Decimal calculations and
page-level citation checks for each question. It runs the repository's Python
workflow through Pyodide 0.27.7 and uses local BM25 search and quoted evidence.
Optional hybrid retrieval and reranking run separately in the server app.

All distributed documents are public fictional examples. Browser scope filtering
demonstrates the workflow and cannot secure private data from the visitor.
Do not enter confidential borrower information. Questions are processed in the
worker, with session audit records in memory; reloading clears them. DSCR is only
one policy requirement, and this demo does not approve loans or give lending advice.

Source and local/server setup: [credit-lens](https://github.com/chinmayarvind23/credit-lens).
AWS deployment is optional and is documented in that repository; it is not required
to use this Space. No paid inference or cloud services are called by the demo.
