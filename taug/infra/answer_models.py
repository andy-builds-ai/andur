"""Where Andur runs, the models it may be given there, and what their tokens cost.

The stack, `svar/scripts/evaluate.py` and the probes read a region's table through `REGIONS`, so a
model never travels without its price, and never without the price of its region.
"""

from typing import NamedTuple


class AnswerModel(NamedTuple):
    """A model Andur may answer with: its name in reports, its Bedrock id, and its prices."""

    name: str
    model_id: str
    #: USD per 1,000 tokens, standard tier, in eu-central-1, read on `FRANKFURT_PRICE_DATE`.
    input_per_1k: str
    output_per_1k: str


#: The models priced in eu-central-1 (Frankfurt): the models that run in Frankfurt itself, and
#: two behind the EU profile for comparison. AWS Price List API (the source of
#: aws.amazon.com/bedrock/pricing), eu-central-1, standard tier (effective 2026-10-01), read on
#: `FRANKFURT_PRICE_DATE`; an EU profile is billed at the price of the region that calls it.
FRANKFURT_PRICE_DATE = "2026-10-06"

FRANKFURT_MODELS = (
    AnswerModel("gpt-oss-120b", "openai.gpt-oss-120b-1:0", "0.0002", "0.00079"),
    AnswerModel("gpt-oss-20b", "openai.gpt-oss-20b-1:0", "0.00009", "0.0004"),
    AnswerModel("Qwen3 235B A22B 2507", "qwen.qwen3-235b-a22b-2507-v1:0", "0.00029", "0.00116"),
    AnswerModel("Qwen3 32B", "qwen.qwen3-32b-v1:0", "0.0002", "0.00079"),
    AnswerModel("MiniMax M2.5", "minimax.minimax-m2.5", "0.00036", "0.00144"),
    AnswerModel("MiniMax M2.1", "minimax.minimax-m2.1", "0.00036", "0.00144"),
    AnswerModel("Nemotron 3 Super", "nvidia.nemotron-super-3-120b", "0.00018", "0.00078"),
    AnswerModel("GLM 4.7 Flash", "zai.glm-4.7-flash", "0.00008", "0.00048"),
    AnswerModel("Devstral 2 123B", "mistral.devstral-2-123b", "0.00048", "0.0024"),
    AnswerModel("Nova Pro", "eu.amazon.nova-pro-v1:0", "0.00105", "0.0042"),
    AnswerModel("Claude Sonnet 4.6", "eu.anthropic.claude-sonnet-4-6", "0.0033", "0.0165"),
)


class RegionModels(NamedTuple):
    """What a stack in one region answers with, and the prices it counts with there."""

    price_date: str
    models: tuple[AnswerModel, ...]
    answer_model_id: str
    #: The model that checks the denied topics, in the region itself.
    topic_model_id: str
    #: The grounding threshold of the answer guardrail, measured for the region's answer model.
    grounding_threshold: float


#: The regions a stack may be deployed to. `cdk deploy -c region=<region>` and each script's
#: `--region` pick one; without them it is `DEFAULT_REGION`.
REGIONS = {
    # Everything stays in Frankfurt. The Standard topic tier, which reads German, goes only with
    # the EU profile (AWS refused it without one on 06.10.2026), so gpt-oss-120b checks the
    # topics in the region; gpt-oss-20b let advice questions through (docs/decisions.md).
    # CloudFront cannot be kept to Germany, so the function's URL is the address. At 0.8 the
    # grounding check withheld 75 of 104 made-up answers and none of 59 right ones to German
    # documents, Qwen's own among them in three runs; at 0.7, 59 (docs/decisions.md).
    "eu-central-1": RegionModels(
        FRANKFURT_PRICE_DATE,
        FRANKFURT_MODELS,
        "qwen.qwen3-235b-a22b-2507-v1:0",
        "openai.gpt-oss-120b-1:0",
        grounding_threshold=0.8,
    ),
}

#: Frankfurt, where Svar runs (docs/decisions.md).
DEFAULT_REGION = "eu-central-1"


def region_models(region: str) -> RegionModels:
    """The table of a region; a region without one is refused."""
    if region not in REGIONS:
        raise KeyError(
            f"{region} has no models in taug/infra/answer_models.py; "
            f"Andur runs in {', '.join(REGIONS)}."
        )
    return REGIONS[region]


def answer_model(model_id: str, region: str = DEFAULT_REGION) -> AnswerModel:
    """The table row of a model id in a region; a model without a price there is refused."""
    for model in region_models(region).models:
        if model.model_id == model_id:
            return model
    raise KeyError(
        f"{model_id} has no price for {region} in taug/infra/answer_models.py; add it there first."
    )
