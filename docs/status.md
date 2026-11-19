# Status

What runs where, since when, and what waits for the next deploy. This is the one place for
facts that change with a deploy; every other document links here instead of repeating them.
The operator updates it with every deploy and every measurement run.

## Deployed

| Region | Stacks | Code | Last deploy | What it is |
|---|---|---|---|---|
| `eu-central-1` (Frankfurt) | `TaugStack`, `AndurStack` | `571670c` | 19.11.2026 | The stacks that run, in Frankfurt alone ([decisions.md, 18.11.2026](decisions.md#2026-11-18-frankfurt-alone)). Two deploys that day: the first, from `a273938`, removed the key retirer from `TaugStack` and brought the answer function and the page layer of `AndurStack` to the new code, and one question was asked through the address afterwards; the second took the variable that no code read any more out of the answer function's environment. |

The code is named by its commit on `main`; later commits that change only documents or tests
leave the deployed templates as they are. The stacks themselves are described in
[architecture.md](architecture.md).

## Last measured

| Region | Date | What | Where the numbers are |
|---|---|---|---|
| `eu-central-1` | 11.11.2026 | The rehearsal of a new workspace after the move, with every measurement it runs | [svar/README.md](../svar/README.md#measured) |
| `eu-central-1` | 14.10.2026 | The 42 questions built to tempt a model (`control_eval`), not repeated since | [svar/README.md](../svar/README.md#measured) |
| `eu-central-1` | 21.10.2026 | The models Vard is measured with | [vard/README.md](../vard/README.md) |

## Waiting for the next deploy

Each of these sits in the answer function's code package or in a template, so changing it
changes what AWS gets. They wait for the next deploy that changes the function or the
template anyway.

- Five public definitions in `svar/app/` without a docstring: three in `errors.py`, one each
  in `budget.py` and `plans.py`. `pyproject.toml` exempts `svar/app/` from ruff's docstring
  rules until then; the exemption goes with that deploy.
