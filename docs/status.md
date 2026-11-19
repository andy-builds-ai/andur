# Status

What runs where, since when, and what waits for the next deploy. This is the one place for
facts that change with a deploy; every other document links here instead of repeating them.
The operator updates it with every deploy and every measurement run.

## Deployed

| Region | Stacks | Code | Last deploy | What it is |
|---|---|---|---|---|
| `eu-central-1` (Frankfurt) | `TaugStack`, `AndurStack` | `a273938` | 19.11.2026 | The stacks that run, in Frankfurt alone ([decisions.md, 18.11.2026](decisions.md#2026-11-18-frankfurt-alone)). The deploy removed the key retirer from `TaugStack` and brought the answer function and the page layer of `AndurStack` to this code; one question was asked through the address afterwards. |

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

- `svar/infra/answer_function.py` without `RETIRED_ENVIRONMENT`: the answer function's
  environment loses `PRICE_TOPIC_PER_1K_UNITS`, which no code has read since the deploy of
  19.11.2026. `AndurStack` changes in one place, the function's environment.
- Five public definitions in `svar/app/` without a docstring: three in `errors.py`, one each
  in `budget.py` and `plans.py`. `pyproject.toml` exempts `svar/app/` from ruff's docstring
  rules until then; the exemption goes with that deploy.
