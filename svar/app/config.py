"""The function's settings: read from its environment and checked, with no network call."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

import errors
import page
import prompts

MAX_RESULTS = 30

MAX_QUESTION_LIMIT = 10_000
MAX_WINDOW_HOURS = 24 * 365
MAX_KEEP_DAYS = 3650

SECONDS_PER_HOUR = 3600
SECONDS_PER_DAY = 24 * SECONDS_PER_HOUR

MAX_PRICE_PER_THOUSAND = Decimal(10)


@dataclass(frozen=True)
class Prices:
    """USD per 1,000 tokens of the answering model and of the topic model, or per 1,000
    guardrail text units."""

    input_tokens: Decimal
    output_tokens: Decimal
    sensitive_information_units: Decimal
    grounding_units: Decimal
    topic_model_input_tokens: Decimal
    topic_model_output_tokens: Decimal


@dataclass(frozen=True)
class Settings:
    """What the stack tells the function through its environment."""

    knowledge_base_id: str
    model_id: str
    region: str
    number_of_results: int
    key_parameter_path: str
    budget_table: str
    question_limit: int
    window_hours: int
    keep_days: int
    guardrail_id: str
    guardrail_version: str
    excerpt_guardrail_id: str
    excerpt_guardrail_version: str
    #: The model that checks the denied topics, in the region.
    topic_model_id: str
    prices: Prices
    price_date: str
    #: The folder of the page's files, which the function serves (page.py).
    page_folder: str
    #: The wording of the system prompt (`prompts.PROMPTS`): the answer model's own, a column of
    #: the table in infra/answer_models.py.
    answer_prompt: str = prompts.DEFAULT_PROMPT


#: Environment variable for each setting. The stack sets all of them.
ENVIRONMENT_VARIABLES = {
    "knowledge_base_id": "KNOWLEDGE_BASE_ID",
    "model_id": "MODEL_ID",
    "region": "REGION",
    "number_of_results": "NUMBER_OF_RESULTS",
    "key_parameter_path": "API_KEY_PARAMETER_PATH",
    "budget_table": "BUDGET_TABLE_NAME",
    "question_limit": "QUESTION_LIMIT",
    "window_hours": "WINDOW_HOURS",
    "keep_days": "KEEP_DAYS",
    "guardrail_id": "GUARDRAIL_ID",
    "guardrail_version": "GUARDRAIL_VERSION",
    "excerpt_guardrail_id": "EXCERPT_GUARDRAIL_ID",
    "excerpt_guardrail_version": "EXCERPT_GUARDRAIL_VERSION",
    "price_input_tokens": "PRICE_INPUT_PER_1K_TOKENS",
    "price_output_tokens": "PRICE_OUTPUT_PER_1K_TOKENS",
    "price_sensitive_information_units": "PRICE_PII_PER_1K_UNITS",
    "price_grounding_units": "PRICE_GROUNDING_PER_1K_UNITS",
    "price_date": "PRICE_DATE",
}

#: The settings of the topic check: the model, in the region, and its prices
#: (infra/answer_models.py).
TOPIC_MODEL_VARIABLES = {
    "topic_model_id": "TOPIC_MODEL_ID",
    "price_topic_model_input_tokens": "PRICE_TOPIC_MODEL_INPUT_PER_1K_TOKENS",
    "price_topic_model_output_tokens": "PRICE_TOPIC_MODEL_OUTPUT_PER_1K_TOKENS",
}

#: Optional: a function deployed before this variable existed keeps `prompts.DEFAULT_PROMPT`, and
#: `scripts/evaluate.py` reads the deployed environment and sets the prompt of each model itself.
ANSWER_PROMPT_VARIABLE = "ANSWER_PROMPT"

#: The folder the function serves the page from.
PAGE_VARIABLES = {"page_folder": page.PAGE_FOLDER_VARIABLE}


def whole_number(variable: str, text: str, lowest: int, highest: int) -> int:
    """A setting that has to be a whole number in a range; the error names the variable."""
    if not (text.isascii() and text.isdigit()) or not lowest <= int(text) <= highest:
        raise errors.ConfigurationError(
            f"{variable} must be a whole number from {lowest} to {highest}, not {text!r}."
        )
    return int(text)


def price(variable: str, text: str) -> Decimal:
    """A price per thousand: a plain decimal from 0 to `MAX_PRICE_PER_THOUSAND`."""
    try:
        value = Decimal(text)
    except InvalidOperation:
        value = None
    if value is None or not value.is_finite() or not Decimal(0) <= value <= MAX_PRICE_PER_THOUSAND:
        raise errors.ConfigurationError(
            f"{variable} must be a price in USD from 0 to {MAX_PRICE_PER_THOUSAND}, not {text!r}."
        )
    return value


def price_day(text: str) -> str:
    """The day the prices were read, as ``YYYY-MM-DD`` and a real date."""
    try:
        day = date.fromisoformat(text)
    except ValueError:
        day = None
    if day is None or day.isoformat() != text:
        raise errors.ConfigurationError(f"PRICE_DATE must be a date as YYYY-MM-DD, not {text!r}.")
    return text


def load_settings(environ: Mapping[str, str]) -> Settings:
    """Read and check the settings; no network call."""
    values: dict[str, str] = {}
    variables = {**ENVIRONMENT_VARIABLES, **TOPIC_MODEL_VARIABLES, **PAGE_VARIABLES}
    for name, variable in variables.items():
        value = environ.get(variable, "").strip()
        if not value:
            raise errors.ConfigurationError(f"The environment variable {variable} is not set.")
        values[name] = value

    number_of_results = whole_number(
        "NUMBER_OF_RESULTS", values["number_of_results"], 1, MAX_RESULTS
    )
    question_limit = whole_number("QUESTION_LIMIT", values["question_limit"], 1, MAX_QUESTION_LIMIT)
    window_hours = whole_number("WINDOW_HOURS", values["window_hours"], 1, MAX_WINDOW_HOURS)
    keep_days = whole_number("KEEP_DAYS", values["keep_days"], 1, MAX_KEEP_DAYS)
    path = values["key_parameter_path"]
    if not path.startswith("/") or path.endswith("/"):
        raise errors.ConfigurationError(
            f"API_KEY_PARAMETER_PATH must start with a slash and not end with one, not {path!r}."
        )
    if keep_days * SECONDS_PER_DAY <= window_hours * SECONDS_PER_HOUR:
        raise errors.ConfigurationError(
            f"KEEP_DAYS ({keep_days}) must outlast WINDOW_HOURS ({window_hours}): a budget "
            "whose item disappears before its window closes is no budget."
        )
    answer_prompt = environ.get(ANSWER_PROMPT_VARIABLE, "").strip() or prompts.DEFAULT_PROMPT
    if answer_prompt not in prompts.PROMPTS:
        raise errors.ConfigurationError(
            f"{ANSWER_PROMPT_VARIABLE} must be one of {', '.join(prompts.PROMPTS)}, "
            f"not {answer_prompt!r}."
        )
    for name in ("guardrail_version", "excerpt_guardrail_version"):
        if not (values[name].isascii() and values[name].isdigit()):
            raise errors.ConfigurationError(
                f"{variables[name]} must be a version number, not {values[name]!r}."
            )
    prices = Prices(
        input_tokens=price("PRICE_INPUT_PER_1K_TOKENS", values["price_input_tokens"]),
        output_tokens=price("PRICE_OUTPUT_PER_1K_TOKENS", values["price_output_tokens"]),
        sensitive_information_units=price(
            "PRICE_PII_PER_1K_UNITS", values["price_sensitive_information_units"]
        ),
        grounding_units=price("PRICE_GROUNDING_PER_1K_UNITS", values["price_grounding_units"]),
        topic_model_input_tokens=price(
            "PRICE_TOPIC_MODEL_INPUT_PER_1K_TOKENS", values["price_topic_model_input_tokens"]
        ),
        topic_model_output_tokens=price(
            "PRICE_TOPIC_MODEL_OUTPUT_PER_1K_TOKENS", values["price_topic_model_output_tokens"]
        ),
    )

    return Settings(
        knowledge_base_id=values["knowledge_base_id"],
        model_id=values["model_id"],
        region=values["region"],
        number_of_results=number_of_results,
        key_parameter_path=values["key_parameter_path"],
        budget_table=values["budget_table"],
        question_limit=question_limit,
        window_hours=window_hours,
        keep_days=keep_days,
        guardrail_id=values["guardrail_id"],
        guardrail_version=values["guardrail_version"],
        excerpt_guardrail_id=values["excerpt_guardrail_id"],
        excerpt_guardrail_version=values["excerpt_guardrail_version"],
        topic_model_id=values["topic_model_id"],
        prices=prices,
        price_date=price_day(values["price_date"]),
        page_folder=values["page_folder"],
        answer_prompt=answer_prompt,
    )
