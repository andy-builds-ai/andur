"""Picks the checks of AGENTS.md by what changed against a base ref and runs them."""

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass

# Follows the list in AGENTS.md ("A changed byte in a code package changes the template").
CODE_PACKAGES = (
    "svar/app/",
    "taug/__init__.py",
    "taug/access/",
    "svar/infra/deploy_drain/",
    "svar/web/",
)
# Follows the same list in AGENTS.md: with the code packages, what changes a template.
INFRA = ("infra/", "taug/infra/", "svar/infra/")
STATUS = "docs/status.md"
STATUS_SENTENCE = (
    "a code package changed: list the change under waiting for the next deploy in docs/status.md"
)
TAIL_LINES = 20


@dataclass
class Check:
    """One check: the name it is reported by and the command that runs it."""

    name: str
    command: list[str]


def changed_files(base: str = "main") -> list[str]:
    """The files changed against ``base``, committed or not, and the untracked ones."""
    # Unquoted paths, so one with a special character still matches its folder; no rename
    # detection, so a file moved out of a folder is listed under its old path too.
    git = ["git", "-c", "core.quotepath=off"]
    diff = [*git, "diff", "--name-only", "--no-renames"]
    commands = (
        [*diff, f"{base}...HEAD"],
        [*diff, "HEAD"],
        [*git, "ls-files", "--others", "--exclude-standard"],
    )
    found: set[str] = set()
    for command in commands:
        try:
            run = subprocess.run(
                command, capture_output=True, text=True, encoding="utf-8", errors="replace"
            )
        except OSError as error:
            raise RuntimeError(f"{' '.join(command)}: {error}") from error
        if run.returncode != 0:
            reason = (run.stderr.strip().splitlines() or [f"exit {run.returncode}"])[0]
            raise RuntimeError(f"{' '.join(command)}: {reason}")
        found.update(line.replace("\\", "/") for line in run.stdout.splitlines() if line)
    return sorted(found)


def _touches(changed: list[str], prefixes: tuple[str, ...]) -> bool:
    """Whether a changed file other than a README.md lies under one of ``prefixes``."""
    return any(
        name.startswith(prefixes) and name.rsplit("/", 1)[-1] != "README.md" for name in changed
    )


def choose_checks(changed: list[str]) -> list[Check]:
    """The checks for these changed files, in the order they run."""
    checks = [
        Check("ruff check", ["ruff", "check"]),
        Check("ruff format", ["ruff", "format", "--check"]),
        Check("pytest", ["pytest", "-q"]),
    ]
    if _touches(changed, INFRA + CODE_PACKAGES):
        # The command of .github/workflows/ci.yml.
        synth = ["npx", "--yes", "aws-cdk@2.1146.0", "synth", "--quiet", "--no-notices"]
        checks.append(Check("cdk synth", synth))
    return checks


def status_missing(changed: list[str]) -> bool:
    """Whether a code package changed without a change to docs/status.md."""
    return _touches(changed, CODE_PACKAGES) and STATUS not in changed


def _run(check: Check) -> tuple[int, str]:
    """The exit code and the combined output of one check."""
    program = shutil.which(check.command[0])
    if program is None:
        return 127, f"{check.command[0]}: not found"
    run = subprocess.run(
        [program, *check.command[1:]],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return run.returncode, run.stdout or ""


def _say(text: str) -> None:
    """Prints ``text``, replacing what the console's encoding cannot show."""
    encoding = sys.stdout.encoding or "utf-8"
    print(text.encode(encoding, errors="replace").decode(encoding), flush=True)


def _report(name: str, code: int, output: str) -> bool:
    """Prints the result of one check and returns whether it failed."""
    if code == 0:
        _say(f"OK   {name}")
        return False
    _say(f"FAIL {name} (exit {code})")
    for line in output.splitlines()[-TAIL_LINES:]:
        _say(line)
    return True


def main(argv: list[str] | None = None) -> int:
    """Runs the checks and returns 0 if all passed, 1 if one failed, 2 if git failed."""
    parser = argparse.ArgumentParser(prog="python -m scripts.verify", description=__doc__)
    parser.add_argument("--base", default="main", help="the ref to compare with (default: main)")
    parser.add_argument("--list", action="store_true", help="name the checks and run nothing")
    args = parser.parse_args(argv)

    try:
        changed = changed_files(args.base)
    except RuntimeError as error:
        print(f"verify: {error}", file=sys.stderr)
        return 2
    checks = choose_checks(changed)
    _say(f"verify: {len(changed)} changed files against {args.base}")
    for check in checks:
        _say(f"check {check.name}: {' '.join(check.command)}")
    if args.list:
        return 0

    results = []
    if status_missing(changed):
        results.append(_report(STATUS, 1, STATUS_SENTENCE))
    for check in checks:
        results.append(_report(check.name, *_run(check)))

    failed = sum(results)
    if failed:
        _say(f"verify: {failed} of {len(results)} checks failed")
        return 1
    _say(f"verify: all {len(results)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
