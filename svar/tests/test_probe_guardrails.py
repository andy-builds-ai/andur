"""The probe guardrails `grounding_probe` and `control_eval` make and delete, without AWS."""

from typing import Any

import pytest

from svar.infra.guardrails import GUARDRAIL_NAME
from svar.scripts import probe_guardrails
from taug.scripts.aws import AwsProblem

PROBE = "andur-test-probe"


class FakeBedrock:
    """The control plane: one probe guardrail at most, and every call written down."""

    def __init__(self, existing: list[dict[str, str]] | None = None, statuses=("READY",)):
        self.guardrails = {g["id"]: g["name"] for g in existing or []}
        self.statuses = list(statuses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_guardrails(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("list_guardrails", kwargs))
        items = [{"id": i, "name": n} for i, n in self.guardrails.items()]
        if "nextToken" not in kwargs:
            return (
                {"guardrails": items[:1], "nextToken": "page-2"}
                if len(items) > 1
                else {"guardrails": items}
            )
        return {"guardrails": items[1:]}

    def create_guardrail(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("create_guardrail", kwargs))
        self.guardrails["probe1"] = kwargs["name"]
        return {"guardrailId": "probe1", "version": "DRAFT"}

    def get_guardrail(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_guardrail", kwargs))
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return {
            "name": self.guardrails[kwargs["guardrailIdentifier"]],
            "status": status,
            "statusReasons": ["broken"],
        }

    def delete_guardrail(self, **kwargs: Any) -> None:
        self.calls.append(("delete_guardrail", kwargs))
        del self.guardrails[kwargs["guardrailIdentifier"]]

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


def test_a_probe_that_is_still_creating_is_waited_for() -> None:
    bedrock = FakeBedrock(statuses=("CREATING", "CREATING", "READY"))
    pauses: list[float] = []
    guardrail_id = probe_guardrails.create_probe(bedrock, {"name": PROBE})
    probe_guardrails.wait_until_ready(bedrock, guardrail_id, sleep=pauses.append, name=PROBE)
    assert pauses == [probe_guardrails.READY_POLL_SECONDS] * 2


def test_a_probe_that_fails_ends_the_run() -> None:
    bedrock = FakeBedrock(statuses=("FAILED",))
    guardrail_id = probe_guardrails.create_probe(bedrock, {"name": PROBE})
    with pytest.raises(AwsProblem, match="failed"):
        probe_guardrails.wait_until_ready(bedrock, guardrail_id, sleep=lambda _: None, name=PROBE)


def test_a_probe_left_by_an_earlier_run_is_refused_before_anything_is_created() -> None:
    """Found on the second page of the listing, too."""
    bedrock = FakeBedrock(
        existing=[{"id": "answer1", "name": GUARDRAIL_NAME}, {"id": "old1", "name": PROBE}]
    )
    with pytest.raises(AwsProblem, match="old1"):
        probe_guardrails.create_probe(bedrock, {"name": PROBE})
    assert "create_guardrail" not in bedrock.names()


def test_only_a_guardrail_named_like_the_probe_is_ever_deleted() -> None:
    bedrock = FakeBedrock(existing=[{"id": "answer1", "name": GUARDRAIL_NAME}])
    with pytest.raises(AwsProblem, match="Refusing to delete"):
        probe_guardrails.delete_probe(bedrock, "answer1", PROBE)
    assert bedrock.guardrails == {"answer1": GUARDRAIL_NAME}
    assert "delete_guardrail" not in bedrock.names()


def test_a_failed_delete_is_reported_not_raised() -> None:
    bedrock = FakeBedrock(existing=[{"id": "answer1", "name": GUARDRAIL_NAME}])
    lines: list[str] = []
    assert not probe_guardrails.delete_reporting(bedrock, "answer1", lines.append, PROBE)
    assert lines and lines[0].startswith("error: Refusing to delete")
    guardrail_id = probe_guardrails.create_probe(bedrock, {"name": PROBE})
    assert probe_guardrails.delete_reporting(bedrock, guardrail_id, lines.append, PROBE)
    assert lines[-1] == f"deleted {PROBE} ({guardrail_id})"
