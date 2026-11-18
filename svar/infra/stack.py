"""Svar's stack, named AndurStack in AWS: its parts wired together, and its outputs.

Taug's stack (taug/infra/stack.py) holds what it stands on."""

from aws_cdk import (
    CfnOutput,
    Stack,
    Token,
    aws_bedrock as bedrock,
)
from constructs import Construct

from svar.app.page import ASK_PATH
from svar.infra.answer_function import (
    ANSWER_FUNCTION_NAME,
    ANSWER_MAX_CONCURRENCY,
    create_answer_function,
    create_answer_role,
    create_budget_table,
    create_deploy_drain,
    create_function_url,
    create_page_layer,
)
from svar.infra.guardrails import create_answer_guardrail, create_excerpt_guardrail
from taug.infra.answer_models import DEFAULT_REGION, answer_model, region_models
from taug.infra.shared import create_log_group

#: The stack's description, the same before and after Taug's documents, vectors and knowledge
#: base move out: a move may change nothing else in the template.
SVAR_DESCRIPTION = "Svar: guardrails, answer function and page, on Taug's knowledge base."


class SvarStack(Stack):
    """Svar: the answer function with its guardrails, budgets and page. Deployed under the
    stack name it always had, AndurStack (infra/app.py)."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        knowledge_base: bedrock.CfnKnowledgeBase,
        answer_model_id: str | None = None,
        answer_max_concurrency: int | None = ANSWER_MAX_CONCURRENCY,
        **kwargs,
    ) -> None:
        """``knowledge_base``: Taug's (taug/infra/stack.py), which the function searches;
        CDK passes its id and ARN between the stacks as an export and an import.
        ``answer_model_id`` None: the answer model of the stack's region's row of `REGIONS`;
        another model must have a price in that row. The topic model and the grounding
        threshold always come from the row; a region without one is refused. The function's
        URL is the one address of page and questions, open, and serves the page too."""
        super().__init__(scope, construct_id, **kwargs)
        # Without a set region the table, and with it the prices, are the default region's;
        # infra/app.py always sets one.
        self.price_region = DEFAULT_REGION if Token.is_unresolved(self.region) else self.region
        self.models = region_models(self.price_region)
        self.answer_model_id = answer_model_id or self.models.answer_model_id
        self.answer_model = answer_model(self.answer_model_id, self.price_region)
        self.topic_model_id = self.models.topic_model_id
        self.topic_model = answer_model(self.topic_model_id, self.price_region)
        self.answer_max_concurrency = answer_max_concurrency

        self.knowledge_base = knowledge_base

        self.budget_table = create_budget_table(self)
        self.answer_log_group = create_log_group(
            self, "AnswerFunctionLogs", f"/aws/lambda/{ANSWER_FUNCTION_NAME}"
        )
        self.guardrail, self.guardrail_version = create_answer_guardrail(self)
        self.excerpt_guardrail, self.excerpt_guardrail_version = create_excerpt_guardrail(self)
        self.answer_role = create_answer_role(
            self,
            self.knowledge_base,
            self.budget_table,
            self.answer_log_group,
            self.guardrail,
            self.excerpt_guardrail,
        )
        self.page_layer = create_page_layer(self)
        self.answer_function = create_answer_function(
            self,
            self.knowledge_base,
            self.budget_table,
            self.answer_role,
            self.answer_log_group,
            self.guardrail_version,
            self.excerpt_guardrail_version,
            self.page_layer,
        )
        self.function_url = create_function_url(self, self.answer_function)
        self.deploy_drain = create_deploy_drain(
            self, self.answer_function, [self.guardrail_version, self.excerpt_guardrail_version]
        )

        self._create_outputs()

    def _create_outputs(self) -> None:
        """Everything a script or a CLI call needs to address this stack."""
        CfnOutput(
            self,
            "WebUrl",
            value=self.function_url.url,
            description=(
                "The page, served by the answer function at its own URL in this region, "
                f"without CloudFront. Questions go to <WebUrl>{ASK_PATH.lstrip('/')} with "
                "the header X-Andur-Key."
            ),
        )
        CfnOutput(
            self,
            "GuardrailVersion",
            value=self.guardrail_version.ref,
            description=(
                "The guardrail on every answer and the version the function uses, "
                "as <id>|<version>."
            ),
        )
        CfnOutput(
            self,
            "ExcerptGuardrailRef",
            value=self.excerpt_guardrail_version.ref,
            description="The guardrail that masks the sources' excerpts, as <id>|<version>.",
        )
        CfnOutput(
            self,
            "TopicModelId",
            value=self.topic_model_id,
            description=(
                "The model that checks the question and the answer for the denied topics, "
                "in this region."
            ),
        )
        CfnOutput(
            self,
            "BudgetTableName",
            value=self.budget_table.table_name,
            description=(
                "One item per key, found by the SHA-256 of the key: questions used, window, expiry."
            ),
        )
