# CreditLens delivery reference

- [Interactive Hugging Face demo](https://huggingface.co/spaces/chinmayarvind/creditlens)
- [Original stack and implementation evidence](stack-evidence.md)
- [Reproducible measurements and limitations](evaluation.md)
- [Architecture](HLD.md) and [implementation contracts](LLD.md)
- [Fresh installation verification](release-verification.md)
- [Concurrent HTTP and shared-authority verification](multi-instance.md)
- [Governed Cortex configuration](../infra/cortex/README.md)
- [Optional AWS operator setup](../infra/aws/README.md)

The public Space uses the static SDK to host an interactive Python browser worker.
Its observed release is `9570614e0a391f5d9cfdd7eabf3ea7bd041c3149`, with runtime
`RUNNING` and no requested compute hardware. The app page was reachable during the
current delivery check. Prior browser verification for this unchanged release
covers desktop/mobile, all five dispositions, exact sources and offline queries
after startup; the actual recording is available through the README GIF.

The latest governed-runtime regression passed 498 tests with one optional neural
integration skipped. This is separate from the earlier fresh-checkout run and
from the retained local neural-retrieval experiments. No new cloud or inference
performance measurements are implied.

For résumé use, retain 3,840 synthetic pages, the 240-question authored suite and
local Recall@10 improvement from 74.05% to 86.54%. Do not present requested semantic
accuracy or cloud-cost targets as measured results. The later full-packet DeepEval
diagnostic was stopped when the user prioritized faster résumé delivery; its raw
outputs and failures remain retained. RAGAS's completed cited-field diagnostic
does not establish whole-answer accuracy.

The project resources directory contains the final résumé bullets, blog,
interview notes, decisions, raw measurements and video. Live managed deployments,
automated OCR execution and broader planned engineering experiments are not
represented as completed by this delivery reference.
