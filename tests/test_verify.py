import subprocess

import pytest

from scripts import verify

THREE = ["ruff check", "ruff format", "pytest"]


def names(changed: list[str]) -> list[str]:
    return [check.name for check in verify.choose_checks(changed)]


def fake_changes(monkeypatch: pytest.MonkeyPatch, changed: list[str]) -> None:
    monkeypatch.setattr(verify, "changed_files", lambda base="main": changed)


def fake_tools(monkeypatch: pytest.MonkeyPatch, failing: str = "") -> list[list[str]]:
    """Replaces the tools: every program is found, and only ``failing`` exits with 1."""
    ran: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        ran.append(command)
        return subprocess.CompletedProcess(command, int(command[0] == failing), stdout="")

    monkeypatch.setattr(verify.shutil, "which", lambda program: program)
    monkeypatch.setattr(verify.subprocess, "run", run)
    return ran


def test_git_lists_renames_apart_and_paths_unquoted(monkeypatch: pytest.MonkeyPatch) -> None:
    ran = fake_tools(monkeypatch)
    verify.changed_files("main")
    diffs = [command for command in ran if "diff" in command]
    assert len(ran) == 3
    assert len(diffs) == 2
    assert all("--no-renames" in command for command in diffs)
    assert all(command[:3] == ["git", "-c", "core.quotepath=off"] for command in ran)


def test_documents_alone_need_no_synth() -> None:
    assert names(["README.md", "docs/x.md"]) == THREE


def test_a_code_package_adds_synth_last() -> None:
    assert names(["svar/app/handler.py"]) == [*THREE, "cdk synth"]


def test_infra_adds_synth() -> None:
    assert names(["taug/infra/stack.py"]) == [*THREE, "cdk synth"]


def test_a_readme_in_a_code_package_needs_no_synth() -> None:
    assert names(["svar/app/README.md"]) == THREE


def test_status_is_missing_only_for_a_code_package_without_it() -> None:
    assert verify.status_missing(["svar/app/x.py"])
    assert not verify.status_missing(["svar/app/x.py", "docs/status.md"])
    assert not verify.status_missing(["infra/app.py"])


def test_list_names_the_checks_and_runs_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_changes(monkeypatch, ["svar/app/x.py"])
    ran = fake_tools(monkeypatch)
    assert verify.main(["--list"]) == 0
    out = capsys.readouterr().out
    for name in [*THREE, "cdk synth"]:
        assert f"check {name}:" in out
    assert ran == []


def test_one_failing_check_fails_the_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_changes(monkeypatch, ["docs/x.md"])
    ran = fake_tools(monkeypatch, failing="pytest")
    assert verify.main([]) == 1
    out = capsys.readouterr().out
    assert "FAIL pytest (exit 1)" in out
    assert "1 of 3 checks failed" in out
    assert len(ran) == 3


def test_a_code_package_without_status_fails_the_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_changes(monkeypatch, ["svar/app/x.py"])
    fake_tools(monkeypatch)
    assert verify.main([]) == 1
    out = capsys.readouterr().out
    assert "FAIL docs/status.md" in out
    assert "1 of 5 checks failed" in out
