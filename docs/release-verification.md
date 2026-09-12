# Clean release verification

The application at commit `1f3efce` was installed into a fresh private directory
and virtual environment. No original node_modules or project virtual environment
was copied. The Python and Bun lockfiles were used unchanged.

| Check | Result |
| --- | --- |
| Base Python dependency installation | Passed with `uv sync --locked` |
| Frontend dependency installation | Passed with Bun's frozen lockfile |
| Frontend build and TypeScript check | Passed |
| Frontend tests | 32 passed |
| Documented Python suite with queue, retrieval and observability extras | 438 passed, one optional neural integration test skipped |
| Ruff lint/format and strict source mypy | Passed across 44 source files |
| Actual loopback HTTP | Readiness, built frontend, borrower list, DSCR packet, exact cited source and unauthorized borrower rejection passed |
| Working tree after verification | Clean |

The initial ZIP archive had no Git metadata, so ten provenance tests failed at
`git rev-parse HEAD`. Fetching the exact original commit into that directory and
populating its index, without changing source files, resolved those failures.
Use the README's Git clone instructions when running evaluation and provenance
checks. A bare archive is sufficient to run the core app, but not those checks.

A test run without the documented optional test dependencies also failed while
importing observability. The default application installation remains lightweight;
use the explicit extras listed under Checks and reproduction for the full suite.
Both failed attempts were retained with the successful verification evidence.

The HTTP test launched its own server, confirmed imports came from the fresh
checkout, exercised real TCP requests and stopped its owned process tree. These
checks do not claim a fresh neural-model download, managed cloud integration or
hosted CI execution. No paid service was called.

The public Hugging Face browser release was verified separately through actual
browser interactions, including offline queries and source inspection after
startup. Server test results do not replace that browser evidence. The full
population semantic run is also separate and remains incomplete until its final
journals and field ledger have been reconciled.

Raw installation logs, test results, source path, HTTP output and historical
failures are retained outside the repository in the owner's CreditLens resources.
