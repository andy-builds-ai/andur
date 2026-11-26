---
name: verify
description: Checks a change in this repository before it is committed, by running scripts/verify.py, which picks the checks of AGENTS.md by what changed against main (ruff, pytest, cdk synth when infra or a code package changed, the docs/status.md line). Use before every commit, and when asked to check, test or verify a change.
---

# verify

Run the checks, fix what fails, and report. The script decides which checks run; do not pick
them yourself.

1. Run `python -m scripts.verify` from the repository root, with the project's virtual
   environment first on PATH (`.venv/Scripts` on Windows, `.venv/bin` elsewhere) so `ruff`,
   `pytest` and `python` are the pinned ones. `--list` shows the checks without running them.
2. On `FAIL`, read the lines under it, fix the cause in the code, and run the script again.
   Never change a test or a check to make it pass. After three runs that still fail, stop and
   report what fails and what you tried.
3. Report the last `verify:` line and every check that ran.

Never run `cdk deploy`, `cdk destroy` or a script from the table "Scripts that call AWS" in
AGENTS.md: they change the running stacks or cost money, and only the operator runs them. The
script calls no AWS. Svar runs only as a Lambda function and its address is not in this
repository, so checking the running service end to end is the operator's step after a deploy;
say so when asked whether a change works live.
