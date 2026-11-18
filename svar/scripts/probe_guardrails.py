"""Probe guardrails: made for one measuring run, refused if one is left from a run before, and
deleted by name after it. `grounding_probe` and `control_eval` copy the stack's guardrails into
them; each script names the only guardrails it creates or deletes."""

import time
from collections.abc import Callable, Mapping
from typing import Any

from taug.scripts.aws import AwsProblem, explained

#: How long a new probe guardrail may take to become READY, and how often to look.
READY_TIMEOUT_SECONDS = 120
READY_POLL_SECONDS = 2

#: AWS answers ThrottlingException when more than 25 ApplyGuardrail calls a second arrive (the
#: default of "Each of the other supported Regions" in the Amazon Bedrock quotas,
#: docs.aws.amazon.com/general/latest/gr/bedrock.html; the account's own value is in Service
#: Quotas). Such a call is repeated after a wait that doubles, with jitter, up to a ceiling.
THROTTLE_ATTEMPTS = 8
THROTTLE_WAIT_SECONDS = 1.0
THROTTLE_MAX_WAIT_SECONDS = 20.0


def guardrails_named(bedrock: Any, name: str) -> list[str]:
    """The ids of every guardrail in the region with this name."""
    ids, token = [], None
    while True:
        with explained("Listing the guardrails"):
            page = bedrock.list_guardrails(**({"nextToken": token} if token else {}))
        ids += [g["id"] for g in page.get("guardrails", []) if g.get("name") == name]
        token = page.get("nextToken")
        if not token:
            return ids


def create_probe(bedrock: Any, request: Mapping[str, Any]) -> str:
    """Create the probe guardrail ``request`` describes and return its id; refuse if one of its
    name is left from a run before."""
    name = request["name"]
    left = guardrails_named(bedrock, name)
    if left:
        raise AwsProblem(
            f"A guardrail named {name} already exists ({', '.join(left)}), left by an "
            "earlier run. Delete it first: aws bedrock delete-guardrail "
            f"--guardrail-identifier {left[0]} --region <region>"
        )
    with explained(f"Creating the guardrail {name}"):
        return bedrock.create_guardrail(**request)["guardrailId"]


def wait_until_ready(
    bedrock: Any,
    guardrail_id: str,
    sleep: Callable[[float], None] = time.sleep,
    *,
    name: str,
) -> None:
    """Wait for the draft to be usable; a failed or slow one ends the run."""
    for _ in range(READY_TIMEOUT_SECONDS // READY_POLL_SECONDS):
        with explained(f"Reading the guardrail {name}"):
            guardrail = bedrock.get_guardrail(guardrailIdentifier=guardrail_id)
        status = guardrail["status"]
        if status == "READY":
            return
        if status == "FAILED":
            raise AwsProblem(f"{name} failed: {guardrail.get('statusReasons')}")
        sleep(READY_POLL_SECONDS)
    raise AwsProblem(f"{name} was not ready after {READY_TIMEOUT_SECONDS} seconds")


def delete_probe(bedrock: Any, guardrail_id: str, name: str) -> None:
    """Delete the probe guardrail, after checking by its name that it is the probe."""
    with explained(f"Reading the guardrail {guardrail_id} before deleting it"):
        found = bedrock.get_guardrail(guardrailIdentifier=guardrail_id)["name"]
    if found != name:
        raise AwsProblem(f"Refusing to delete {guardrail_id}: it is {found!r}, not {name}")
    with explained(f"Deleting the guardrail {name}"):
        bedrock.delete_guardrail(guardrailIdentifier=guardrail_id)


def delete_reporting(
    bedrock: Any, guardrail_id: str, progress: Callable[[str], None], name: str
) -> bool:
    """Delete the probe and say whether it is gone. A failed delete is reported, not raised:
    after a failed run it must not hide why the run failed, after a finished run it must not
    cost the report of calls already paid for."""
    try:
        delete_probe(bedrock, guardrail_id, name)
    except AwsProblem as problem:
        progress(f"error: {problem}")
        return False
    progress(f"deleted {name} ({guardrail_id})")
    return True
