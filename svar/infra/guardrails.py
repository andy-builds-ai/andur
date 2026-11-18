"""The two guardrails: on every answer, and on the sources' excerpts. Both run in the stack's
region; the denied topics are checked by the region's topic model (svar/app/topic_check.py)."""

import hashlib
import json
from typing import TYPE_CHECKING, Any

from aws_cdk import aws_bedrock as bedrock

from taug.infra.answer_models import region_models

if TYPE_CHECKING:
    from svar.infra.stack import SvarStack


GUARDRAIL_NAME = "andur-answer-guardrail"

MASKED_PII_TYPES = ("NAME", "EMAIL", "PHONE", "ADDRESS")

RELEVANCE_THRESHOLD = 0.7


def grounding_threshold_for(region: str) -> float:
    """The grounding threshold of the answer guardrail in ``region``, from `REGIONS`, measured
    for its answer model. The stack and the probes that copy its answer guardrail ask this, so
    they agree."""
    return region_models(region).grounding_threshold


BLOCKED_INPUT_MESSAGE = (
    "Svar answers questions about this company's documents. It does not give legal, tax or "
    "medical advice, and it does not answer questions about people's private lives or public "
    "figures."
)
BLOCKED_OUTPUT_MESSAGE = (
    "Svar withheld this answer: the documents do not carry it, or it strays into advice "
    "Svar does not give."
)

EXCERPT_GUARDRAIL_NAME = "andur-excerpt-guardrail"

#: Required by Bedrock, never shown: masking replaces text, it does not block.
EXCERPT_BLOCKED_MESSAGE = "This excerpt was withheld."


def masking_policy() -> bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty:
    """The masking both guardrails share: `MASKED_PII_TYPES` in the output, never in the input."""
    return bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty(
        pii_entities_config=[
            bedrock.CfnGuardrail.PiiEntityConfigProperty(
                type=pii_type,
                action="ANONYMIZE",
                input_enabled=False,
                input_action="NONE",
                output_enabled=True,
                output_action="ANONYMIZE",
            )
            for pii_type in MASKED_PII_TYPES
        ]
    )


def guardrail_with_version(
    stack: "SvarStack", construct_id: str, label: str, properties: dict[str, Any]
) -> tuple[bedrock.CfnGuardrail, bedrock.CfnGuardrailVersion]:
    """A guardrail from ``properties``, and a numbered version that follows every one of
    them."""
    resolved = json.dumps(stack.resolve(properties), sort_keys=True)
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:12]
    guardrail = bedrock.CfnGuardrail(stack, construct_id, **properties)
    version = bedrock.CfnGuardrailVersion(
        stack,
        f"{construct_id}Version",
        guardrail_identifier=guardrail.attr_guardrail_id,
        description=f"Andur {label} guardrail, settings {digest}",
    )
    return guardrail, version


def create_answer_guardrail(
    stack: "SvarStack",
) -> tuple[bedrock.CfnGuardrail, bedrock.CfnGuardrailVersion]:
    """The guardrail on every answer, and the numbered version the function uses: masking and
    the grounding check, no topics; the topic model reads those."""
    return guardrail_with_version(
        stack,
        "AnswerGuardrail",
        "answer",
        {
            "name": GUARDRAIL_NAME,
            "description": "Andur: masks personal details, withholds ungrounded answers.",
            "blocked_input_messaging": BLOCKED_INPUT_MESSAGE,
            "blocked_outputs_messaging": BLOCKED_OUTPUT_MESSAGE,
            "sensitive_information_policy_config": masking_policy(),
            "contextual_grounding_policy_config": bedrock.CfnGuardrail.ContextualGroundingPolicyConfigProperty(  # noqa: E501
                filters_config=[
                    bedrock.CfnGuardrail.ContextualGroundingFilterConfigProperty(
                        type="GROUNDING",
                        threshold=grounding_threshold_for(stack.price_region),
                        action="BLOCK",
                        enabled=True,
                    ),
                    bedrock.CfnGuardrail.ContextualGroundingFilterConfigProperty(
                        type="RELEVANCE",
                        threshold=RELEVANCE_THRESHOLD,
                        action="BLOCK",
                        enabled=True,
                    ),
                ]
            ),
        },
    )


def create_excerpt_guardrail(
    stack: "SvarStack",
) -> tuple[bedrock.CfnGuardrail, bedrock.CfnGuardrailVersion]:
    """The guardrail that masks the sources' excerpts, and its numbered version."""
    return guardrail_with_version(
        stack,
        "ExcerptGuardrail",
        "excerpt",
        {
            "name": EXCERPT_GUARDRAIL_NAME,
            "description": "Andur: masks personal details in the excerpts of the sources.",
            "blocked_input_messaging": EXCERPT_BLOCKED_MESSAGE,
            "blocked_outputs_messaging": EXCERPT_BLOCKED_MESSAGE,
            "sensitive_information_policy_config": masking_policy(),
        },
    )
