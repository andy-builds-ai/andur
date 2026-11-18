"""Assertions against the synthesised CloudFormation template.

These tests never call AWS. `Template.from_stack` renders the stack into the
JSON that CloudFormation would receive, and each test asserts one promise
that template has to keep. They catch a broken stack in a second, before a
deploy spends ten minutes finding out.
"""

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import aws  # svar/app/aws.py, imported by its Lambda name (conftest.py)

# Imported, not skipped past. `aws-cdk-lib` is a hard dependency in
# pyproject.toml, and it has to stay one: with an `importorskip` here, an
# environment without the CDK reported "passed" for this whole module — over
# sixty assertions about the stack, IAM and the page gone without
# a red line anywhere. A missing dependency is a broken environment and has
# to look like one.
import aws_cdk as cdk
import budget
import config
import intake
import pytest
import sentences
from aws_cdk.assertions import Match, Template

from svar.app.denied_topics import DENIED_TOPICS
from svar.app.page import ASK_PATH, HSTS_MAX_AGE_DAYS, ROOT_DOCUMENT, SECURITY_HEADERS
from svar.infra.answer_function import (
    ANSWER_FUNCTION_NAME,
    ANSWER_MAX_CONCURRENCY,
    ANSWER_MEMORY_MB,
    ANSWER_MODEL_ID,
    ANSWER_TIMEOUT_SECONDS,
    BUDGET_KEY_ATTRIBUTE,
    BUDGET_TABLE_NAME,
    BUDGET_TTL_ATTRIBUTE,
    DEFAULT_KEEP_DAYS,
    DEFAULT_QUESTION_LIMIT,
    DEFAULT_WINDOW_HOURS,
    NUMBER_OF_RESULTS,
    PRICE_GROUNDING_PER_1K_UNITS,
    PRICE_INPUT_PER_1K_TOKENS,
    PRICE_OUTPUT_PER_1K_TOKENS,
    PRICE_PII_PER_1K_UNITS,
    PRICE_TOPIC_PER_1K_UNITS,
    WEB_FOLDER,
)
from svar.infra.guardrails import (
    BLOCKED_INPUT_MESSAGE,
    BLOCKED_OUTPUT_MESSAGE,
    EXCERPT_GUARDRAIL_NAME,
    GUARDRAIL_NAME,
    MASKED_PII_TYPES,
    RELEVANCE_THRESHOLD,
)
from svar.infra.stack import SvarStack
from svar.scripts import ask, grounding_probe
from taug.infra import answer_models
from taug.infra.answer_models import FRANKFURT_PRICE_DATE, answer_model
from taug.infra.keys import API_KEY_PARAMETER_PATH
from taug.infra.knowledge_base import (
    CHUNK_MAX_TOKENS,
    CHUNK_OVERLAP_PERCENT,
    DATA_SOURCE_NAME,
    DISTANCE_METRIC,
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL_ID,
    KNOWLEDGE_BASE_NAME,
    NON_FILTERABLE_METADATA_KEYS,
    NONCURRENT_VERSION_RETENTION_DAYS,
    VECTOR_DATA_TYPE,
    VECTOR_INDEX_NAME,
    WORKSPACE_PREFIX,
    replacement_safe_name,
)
from taug.infra.shared import REPOSITORY_ROOT
from taug.infra.stack import TaugStack

#: The file `cdk deploy` reads its context from. The tests read the same file,
#: so that what is synthesised here is what would be deployed.
CDK_JSON = REPOSITORY_ROOT / "cdk.json"


def deploy_context() -> dict[str, Any]:
    """The context of cdk.json — the feature flags a deploy renders with.

    A flag can change the template: ``@aws-cdk/aws-iam:minimizePolicies``
    merges policy statements, and this module unpacks statements by position
    (``list_bucket, get_object = ...``). Synthesised without the context, the
    tests would be reading a template nobody deploys.
    """
    return json.loads(CDK_JSON.read_text(encoding="utf-8"))["context"]


def test_the_cdk_cli_telemetry_stays_off_for_every_run_from_the_repository() -> None:
    """The key keeps the CDK CLI's telemetry off in every run (``2026-11-17-cdk-telemetry-off``)."""
    assert deploy_context()["cli-telemetry"] is False


#: The region a test stack is built for when it names none: Frankfurt, where Svar runs.
TEST_REGION = "eu-central-1"

#: Frankfurt's row of the model table, and its answer and topic models with their prices there.
FRANKFURT = answer_models.REGIONS[TEST_REGION]
FRANKFURT_MODEL = answer_model(FRANKFURT.answer_model_id, TEST_REGION)
TOPIC_MODEL = answer_model(FRANKFURT.topic_model_id, TEST_REGION)


def andur_stacks(app: cdk.App, construct_id: str, **settings: Any) -> tuple[TaugStack, SvarStack]:
    """Taug's stack and Svar's, wired as infra/app.py wires them for the region of ``env``, or
    for `TEST_REGION` without an ``env``; an ``env`` without a region leaves it open, and Svar's
    stack then counts as `DEFAULT_REGION`'s. Each setting not given, Svar's stack takes from that
    region's table itself, and it refuses a region without one. Svar's gets ``construct_id``
    and ``settings``, Taug's the id with "Taug" after it, and the ``env`` goes to both."""
    env = settings.setdefault("env", cdk.Environment(region=TEST_REGION))
    taug = TaugStack(app, f"{construct_id}Taug", env=env)
    svar = SvarStack(app, construct_id, knowledge_base=taug.knowledge_base, **settings)
    svar.add_stack_dependency(taug, "Svar reads the knowledge base from Taug.")
    return taug, svar


@pytest.fixture(scope="module")
def stacks() -> tuple[TaugStack, SvarStack]:
    return andur_stacks(cdk.App(context=deploy_context()), "TestAndurStack")


@pytest.fixture(scope="module")
def stack(stacks: tuple[TaugStack, SvarStack]) -> SvarStack:
    return stacks[1]


@pytest.fixture(scope="module")
def taug_stack(stacks: tuple[TaugStack, SvarStack]) -> TaugStack:
    return stacks[0]


@pytest.fixture(scope="module")
def taug_template(taug_stack: TaugStack) -> Template:
    return Template.from_stack(taug_stack)


@pytest.fixture(scope="module")
def template(stack: SvarStack) -> Template:
    """One synthesis for the whole module — it is the slow part."""
    return Template.from_stack(stack)


def only_resource(template: Template, resource_type: str) -> Mapping[str, Any]:
    """The single resource of a type; fails loudly if there are more or none."""
    (resource,) = template.find_resources(resource_type).values()
    return resource


def imported(
    taug_stack: TaugStack, taug_template: Template, construct: Any, attribute: str
) -> dict:
    """The import Svar's template holds where it points at one of Taug's resources: CDK
    exports the attribute from Taug's stack and imports it by the export's name."""
    reference = get_att(taug_stack, construct, attribute)
    (name,) = [
        output["Export"]["Name"]
        for output in taug_template.to_json()["Outputs"].values()
        if output["Value"] == reference and "Export" in output
    ]
    return {"Fn::ImportValue": name}


def get_att(stack: Any, construct: Any, attribute: str) -> dict[str, list[str]]:
    """The reference CloudFormation renders when one resource points at another."""
    return {"Fn::GetAtt": [stack.get_logical_id(construct), attribute]}


def resource_of(stack: SvarStack, template: Template, construct: Any) -> Mapping[str, Any]:
    """The template entry of one construct; an L2 construct is looked up by its L1 child."""
    cfn = construct.node.default_child or construct
    return template.to_json()["Resources"][stack.get_logical_id(cfn)]


def role_policies(stack: SvarStack, template: Template, role: Any) -> dict[str, dict[str, Any]]:
    """The inline policies of one role, by policy name."""
    policies = resource_of(stack, template, role)["Properties"]["Policies"]
    return {p["PolicyName"]: p["PolicyDocument"] for p in policies}


def joined_last_part(value: Mapping[str, Any]) -> str:
    """The literal end of an ARN that CloudFormation renders as a Fn::Join."""
    return value["Fn::Join"][1][-1]


# --- Documents bucket ----------------------------------------------------


def documents_bucket(stack: SvarStack, template: Template) -> Mapping[str, Any]:
    """The documents bucket itself, by its construct, not whichever bucket happens to match:
    ``has_resource_properties`` is happy as soon as one resource of the type fits, and the
    documents are the one thing here that cannot be rebuilt from anything else."""
    return resource_of(stack, template, stack.documents_bucket)["Properties"]


def test_documents_bucket_blocks_all_public_access(
    taug_stack: TaugStack, taug_template: Template
) -> None:
    assert documents_bucket(taug_stack, taug_template)["PublicAccessBlockConfiguration"] == {
        "BlockPublicAcls": True,
        "BlockPublicPolicy": True,
        "IgnorePublicAcls": True,
        "RestrictPublicBuckets": True,
    }


def test_documents_bucket_is_versioned(taug_stack: TaugStack, taug_template: Template) -> None:
    assert documents_bucket(taug_stack, taug_template)["VersioningConfiguration"] == {
        "Status": "Enabled"
    }


def test_old_document_versions_expire(taug_stack: TaugStack, taug_template: Template) -> None:
    """Versioning without an expiry would keep every old version forever."""
    (rule,) = documents_bucket(taug_stack, taug_template)["LifecycleConfiguration"]["Rules"]
    assert rule["Status"] == "Enabled"
    assert rule["NoncurrentVersionExpiration"] == {
        "NoncurrentDays": NONCURRENT_VERSION_RETENTION_DAYS
    }


# --- What survives cdk destroy, and what does not ------------------------


def test_documents_and_their_policy_survive_stack_deletion(
    taug_stack: TaugStack, taug_template: Template
) -> None:
    """Documents are the one thing that cannot be rebuilt — nor their SSL-only rule."""
    bucket = resource_of(taug_stack, taug_template, taug_stack.documents_bucket)
    policy = resource_of(taug_stack, taug_template, taug_stack.documents_bucket.policy)
    assert bucket["DeletionPolicy"] == "Retain"
    assert policy["DeletionPolicy"] == "Retain"


def test_the_page_goes_with_the_stack(stack: SvarStack, template: Template) -> None:
    """Every file of its layer is a copy of web/ in the repository, so nothing is lost."""
    layer = resource_of(stack, template, stack.page_layer)
    assert layer.get("DeletionPolicy", "Delete") == "Delete"
    assert template.find_resources("AWS::S3::Bucket") == {}


def test_derived_data_is_deleted_with_the_stack(taug_template: Template) -> None:
    """Vectors are rebuilt from documents, so nothing derived may linger."""
    assert (
        only_resource(taug_template, "AWS::S3Vectors::VectorBucket")["DeletionPolicy"] == "Delete"
    )
    assert only_resource(taug_template, "AWS::S3Vectors::Index")["DeletionPolicy"] == "Delete"

    data_source = only_resource(taug_template, "AWS::Bedrock::DataSource")
    assert data_source["Properties"]["DataDeletionPolicy"] == "DELETE"


# --- Vector store --------------------------------------------------------


def test_vector_index_matches_the_embedding_model(taug_template: Template) -> None:
    """Index and knowledge base must agree on dimension and data type."""
    taug_template.has_resource_properties(
        "AWS::S3Vectors::Index",
        {
            "DataType": "float32",
            "Dimension": EMBEDDING_DIMENSION,
            "DistanceMetric": "cosine",
        },
    )


def test_vector_index_name_carries_its_configuration(taug_template: Template) -> None:
    index = only_resource(taug_template, "AWS::S3Vectors::Index")
    name = index["Properties"]["IndexName"]

    assert name == VECTOR_INDEX_NAME
    assert f"-{EMBEDDING_DIMENSION}-" in name
    assert f"-{DISTANCE_METRIC}-" in name


def test_replacement_safe_name_changes_with_every_setting() -> None:
    """Any change that replaces a resource must also rename it, or the two collide."""
    settings = [
        VECTOR_DATA_TYPE,
        EMBEDDING_DIMENSION,
        DISTANCE_METRIC,
        NON_FILTERABLE_METADATA_KEYS,
    ]
    variants = [
        ["float16", *settings[1:]],
        [settings[0], 512, *settings[2:]],
        [*settings[:2], "euclidean", settings[3]],
        [*settings[:3], [*NON_FILTERABLE_METADATA_KEYS, "author"]],
        [*settings[:3], list(reversed(NON_FILTERABLE_METADATA_KEYS))],
    ]
    names = {replacement_safe_name("andur-index-", s) for s in [settings, *variants]}
    assert len(names) == len(variants) + 1


def test_knowledge_base_and_data_source_names_follow_their_configuration(
    taug_template: Template,
) -> None:
    """A new index replaces the base; the base must not collide with its old self."""
    knowledge_base = only_resource(taug_template, "AWS::Bedrock::KnowledgeBase")["Properties"]
    data_source = only_resource(taug_template, "AWS::Bedrock::DataSource")["Properties"]

    assert knowledge_base["Name"] == KNOWLEDGE_BASE_NAME
    assert data_source["Name"] == DATA_SOURCE_NAME
    assert KNOWLEDGE_BASE_NAME != replacement_safe_name(
        "andur-knowledge-base-",
        ["andur-index-other", EMBEDDING_MODEL_ID, EMBEDDING_DIMENSION, VECTOR_DATA_TYPE],
    )


def test_non_filterable_keys_in_the_template_are_exactly_the_constant(
    taug_template: Template,
) -> None:
    """Not a subset: an extra key here would silently switch off a filter."""
    index = only_resource(taug_template, "AWS::S3Vectors::Index")
    keys = index["Properties"]["MetadataConfiguration"]["NonFilterableMetadataKeys"]
    assert keys == NON_FILTERABLE_METADATA_KEYS


def test_bedrock_metadata_keys_are_not_filterable() -> None:
    """Chunk text and Bedrock's own metadata must not eat the filter budget."""
    assert "AMAZON_BEDROCK_TEXT" in NON_FILTERABLE_METADATA_KEYS
    assert "AMAZON_BEDROCK_METADATA" in NON_FILTERABLE_METADATA_KEYS


def test_workspace_and_layer_stay_filterable() -> None:
    """The two keys retrieval filters on must be absent from the exclusion list."""
    assert "workspace" not in NON_FILTERABLE_METADATA_KEYS
    assert "layer" not in NON_FILTERABLE_METADATA_KEYS


# --- Knowledge base ------------------------------------------------------


def test_knowledge_base_exists_and_uses_s3_vectors(taug_template: Template) -> None:
    taug_template.resource_count_is("AWS::Bedrock::KnowledgeBase", 1)
    taug_template.has_resource_properties(
        "AWS::Bedrock::KnowledgeBase",
        {"StorageConfiguration": Match.object_like({"Type": "S3_VECTORS"})},
    )


def test_embedding_model_is_titan_text_embeddings_v2(taug_template: Template) -> None:
    """The model id is buried in a Fn::Join; assert on the rendered JSON."""
    properties = only_resource(taug_template, "AWS::Bedrock::KnowledgeBase")["Properties"]
    vector_config = properties["KnowledgeBaseConfiguration"]["VectorKnowledgeBaseConfiguration"]

    model_arn_parts = vector_config["EmbeddingModelArn"]["Fn::Join"][1]
    assert f"::foundation-model/{EMBEDDING_MODEL_ID}" in model_arn_parts
    assert EMBEDDING_MODEL_ID == "amazon.titan-embed-text-v2:0"

    embedding_config = vector_config["EmbeddingModelConfiguration"]
    assert embedding_config["BedrockEmbeddingModelConfiguration"] == {
        "Dimensions": EMBEDDING_DIMENSION,
        "EmbeddingDataType": "FLOAT32",
    }


def test_resources_are_wired_by_reference(taug_stack: TaugStack, taug_template: Template) -> None:
    """References, not copied strings: a renamed resource keeps its wiring."""
    knowledge_base = only_resource(taug_template, "AWS::Bedrock::KnowledgeBase")["Properties"]
    assert knowledge_base["StorageConfiguration"]["S3VectorsConfiguration"]["IndexArn"] == get_att(
        taug_stack, taug_stack.vector_index, "IndexArn"
    )
    assert knowledge_base["RoleArn"] == get_att(
        taug_stack, taug_stack.knowledge_base_role.node.default_child, "Arn"
    )

    data_source = only_resource(taug_template, "AWS::Bedrock::DataSource")["Properties"]
    assert data_source["DataSourceConfiguration"]["S3Configuration"]["BucketArn"] == get_att(
        taug_stack, taug_stack.documents_bucket.node.default_child, "Arn"
    )
    assert data_source["KnowledgeBaseId"] == get_att(
        taug_stack, taug_stack.knowledge_base, "KnowledgeBaseId"
    )


# --- Data source ---------------------------------------------------------


def test_data_source_crawls_only_the_workspace_prefix(taug_template: Template) -> None:
    taug_template.has_resource_properties(
        "AWS::Bedrock::DataSource",
        {
            "DataSourceConfiguration": Match.object_like(
                {
                    "Type": "S3",
                    "S3Configuration": Match.object_like({"InclusionPrefixes": [WORKSPACE_PREFIX]}),
                }
            )
        },
    )


def test_chunking_is_fixed_size_as_configured(taug_template: Template) -> None:
    taug_template.has_resource_properties(
        "AWS::Bedrock::DataSource",
        {
            "VectorIngestionConfiguration": {
                "ChunkingConfiguration": {
                    "ChunkingStrategy": "FIXED_SIZE",
                    "FixedSizeChunkingConfiguration": {
                        "MaxTokens": CHUNK_MAX_TOKENS,
                        "OverlapPercentage": CHUNK_OVERLAP_PERCENT,
                    },
                }
            }
        },
    )


# --- Permissions ---------------------------------------------------------


def test_role_may_only_read_our_own_documents_bucket(
    taug_stack: TaugStack, taug_template: Template
) -> None:
    bucket_arn = get_att(taug_stack, taug_stack.documents_bucket.node.default_child, "Arn")
    same_account = {"StringEquals": {"aws:ResourceAccount": {"Ref": "AWS::AccountId"}}}

    policies = role_policies(taug_stack, taug_template, taug_stack.knowledge_base_role)
    list_bucket, get_object = policies["ReadDocuments"]["Statement"]

    assert list_bucket["Action"] == "s3:ListBucket"
    assert list_bucket["Resource"] == bucket_arn
    assert list_bucket["Condition"] == same_account

    assert get_object["Action"] == "s3:GetObject"
    assert get_object["Resource"] == {"Fn::Join": ["", [bucket_arn, "/*"]]}
    assert get_object["Condition"] == same_account


def test_role_may_only_invoke_the_embedding_model(
    taug_stack: TaugStack, taug_template: Template
) -> None:
    policies = role_policies(taug_stack, taug_template, taug_stack.knowledge_base_role)
    list_models, invoke_model = policies["InvokeEmbeddingModel"]["Statement"]

    assert list_models["Action"] == "bedrock:ListFoundationModels"
    assert list_models["Resource"] == "*"

    assert invoke_model["Action"] == "bedrock:InvokeModel"
    model_arn_parts = invoke_model["Resource"]["Fn::Join"][1]
    assert model_arn_parts[-1] == f"::foundation-model/{EMBEDDING_MODEL_ID}"


def all_policy_statements(template: Template) -> list[tuple[str, dict[str, Any]]]:
    """Every statement of every inline role policy and every policy resource, with its owner."""
    statements: list[tuple[str, dict[str, Any]]] = []
    for logical_id, role in template.find_resources("AWS::IAM::Role").items():
        for policy in role["Properties"].get("Policies", []):
            statements += [
                (logical_id, statement) for statement in policy["PolicyDocument"]["Statement"]
            ]
    for logical_id, policy in template.find_resources("AWS::IAM::Policy").items():
        statements += [
            (logical_id, statement)
            for statement in policy["Properties"]["PolicyDocument"]["Statement"]
        ]
    return statements


def test_the_only_wildcard_resources_are_model_listing_and_cache_clearing(
    template: Template, taug_template: Template
) -> None:
    """One action that cannot be scoped; nothing else gets to use that excuse.

    ``bedrock:ListFoundationModels`` takes no resource at all. Nothing in the stacks' own
    roles is on a wildcard.
    """
    wildcard_actions = [
        sorted(action) if isinstance(action, list) else [action]
        for action in (
            statement["Action"]
            for _, statement in [
                *all_policy_statements(template),
                *all_policy_statements(taug_template),
            ]
            if statement["Resource"] == "*"
        )
    ]
    # Sorted, because the order inside one statement is the renderer's:
    # ``@aws-cdk/aws-iam:minimizePolicies`` in cdk.json merges statements and
    # decides for itself in which order the actions end up.
    assert sorted(wildcard_actions) == [["bedrock:ListFoundationModels"]]


def test_only_cdk_s_own_deploy_time_roles_have_a_managed_policy(
    stack: SvarStack, template: Template, taug_stack: TaugStack, taug_template: Template
) -> None:
    """Known exceptions: both belong to functions CDK generates for a deploy.

    One is the key retirer's, which no resource calls any more, the other runs the
    deploy drain. Each gets AWSLambdaBasicExecutionRole from its construct; this stack does
    not create these roles and cannot narrow them without replacing the
    constructs. Every role the stack defines itself carries inline policies
    only, and both of these run during a deploy and never afterwards.
    """

    def framework_role(owner: Any, provider: str) -> Any:
        return owner.node.find_child(provider).node.find_child("framework-onEvent").role

    expected = {
        (owner.stack_name, owner.get_logical_id(role.node.default_child))
        for owner, role in [
            (taug_stack, framework_role(taug_stack, "ApiKeyProvider")),
            (stack, framework_role(stack, "DeployDrainProvider")),
        ]
    }
    with_managed = {
        (owner.stack_name, logical_id): role["Properties"]["ManagedPolicyArns"]
        for owner, owner_template in [(taug_stack, taug_template), (stack, template)]
        for logical_id, role in owner_template.find_resources("AWS::IAM::Role").items()
        if "ManagedPolicyArns" in role["Properties"]
    }
    assert set(with_managed) == expected
    for managed_arns in with_managed.values():
        (managed_arn,) = managed_arns
        assert (
            joined_last_part(managed_arn)
            == ":iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
        )


def test_role_may_only_touch_our_own_vector_index(
    taug_stack: TaugStack, taug_template: Template
) -> None:
    """No wildcard on s3vectors — one index, five actions."""
    (statement,) = role_policies(taug_stack, taug_template, taug_stack.knowledge_base_role)[
        "UseVectorIndex"
    ]["Statement"]
    assert sorted(statement["Action"]) == [
        "s3vectors:DeleteVectors",
        "s3vectors:GetIndex",
        "s3vectors:GetVectors",
        "s3vectors:PutVectors",
        "s3vectors:QueryVectors",
    ]
    assert statement["Resource"] == {"Fn::GetAtt": ["VectorIndex", "IndexArn"]}


def test_role_can_only_be_assumed_by_bedrock_in_this_account(
    taug_stack: TaugStack, taug_template: Template
) -> None:
    """Without the source conditions the role is a confused-deputy hole."""
    role = resource_of(taug_stack, taug_template, taug_stack.knowledge_base_role)
    (statement,) = role["Properties"]["AssumeRolePolicyDocument"]["Statement"]

    assert statement["Principal"] == {"Service": "bedrock.amazonaws.com"}
    assert statement["Condition"]["StringEquals"]["aws:SourceAccount"] == {"Ref": "AWS::AccountId"}

    source_arn_parts = statement["Condition"]["ArnLike"]["aws:SourceArn"]["Fn::Join"][1]
    assert source_arn_parts[-1] == ":knowledge-base/*"
    assert {"Ref": "AWS::AccountId"} in source_arn_parts


def test_the_only_compute_that_runs_between_deploys_is_the_answer_function(
    stack: SvarStack, template: Template, taug_template: Template
) -> None:
    """Five functions, and four of them only ever run during a deploy.

    The answer function, the retired key function and the deploy drain, each of the two
    with the CDK framework that runs it. Nothing else runs, so nothing else can cost money
    while nobody asks.
    """
    template.resource_count_is("AWS::Lambda::Function", 3)
    taug_template.resource_count_is("AWS::Lambda::Function", 2)
    functions = template.find_resources("AWS::Lambda::Function")
    assert stack.get_logical_id(stack.answer_function.node.default_child) in functions


# --- Outputs -------------------------------------------------------------


def test_stacks_publish_what_later_steps_need(template: Template, taug_template: Template) -> None:
    """The scripts address each stack only through its outputs (taug/scripts/aws.py)."""
    assert {
        "KnowledgeBaseId",
        "DataSourceId",
        "DocumentsBucketName",
        "VectorBucketName",
        "VectorIndexArn",
        "KnowledgeBaseRoleArn",
        "ApiKeyParameterPath",
    } <= set(taug_template.find_outputs("*"))
    assert set(template.find_outputs("*")) == {
        "WebUrl",
        "GuardrailVersion",
        "ExcerptGuardrailRef",
        "TopicModelId",
        "BudgetTableName",
    }


def test_vector_bucket_output_is_a_name_not_an_arn(taug_template: Template) -> None:
    """`ref` on a vector bucket yields its ARN — an easy mislabel to make."""
    outputs = taug_template.find_outputs("VectorBucketName")
    rendered = str(outputs["VectorBucketName"]["Value"])
    assert "arn:" not in rendered
    assert "andur-vectors-" in rendered


# --- Answer function (build step 3) --------------------------------------


def test_answer_function_runs_the_handler_on_the_newest_runtime(
    stack: SvarStack, template: Template
) -> None:
    properties = resource_of(stack, template, stack.answer_function)["Properties"]
    assert properties["FunctionName"] == ANSWER_FUNCTION_NAME
    assert properties["Runtime"] == "python3.14"
    assert properties["Handler"] == "handler.handler"
    assert properties["Architectures"] == ["arm64"]
    assert properties["MemorySize"] == ANSWER_MEMORY_MB
    assert properties["Timeout"] == ANSWER_TIMEOUT_SECONDS


def test_timeouts_nest_from_aws_calls_to_function_to_ask_script() -> None:
    """Each layer waits longer than the one inside it.

    A hanging Bedrock call ends as the handler's 502, not as a Lambda timeout;
    the function ends before the 15 minutes a Function URL call may take; and
    the ask script waits longer than the function, so it prints the 502. The
    innermost number counts the key once, because only the requested
    workspace's key is read, at most once per request, and the budget table
    twice, because a wrong item is put right with a second write, and the guardrail
    three times, for the topic checks of the question and the answer and for the
    excerpts.
    """
    assert aws.worst_case_seconds(behind_cloudfront=False) == 190
    assert (
        aws.worst_case_seconds(behind_cloudfront=False)
        < ANSWER_TIMEOUT_SECONDS
        < ask.HTTP_TIMEOUT_SECONDS
        < 900
    )


def test_every_caller_outlasts_the_function() -> None:
    """The function is its own address: the page and svar/scripts/ask.py wait longer than the
    function may run, so the function's own timeout is the limit svar/app/aws.py counts with,
    and the worst case still ends inside it, as a 502 with a reason."""
    assert aws.PAGE_TIMEOUT_SECONDS * 1000 == page_number("REQUEST_TIMEOUT_MS")
    assert aws.FUNCTION_TIMEOUT_SECONDS == ANSWER_TIMEOUT_SECONDS
    assert aws.PAGE_TIMEOUT_SECONDS > ANSWER_TIMEOUT_SECONDS
    assert ask.HTTP_TIMEOUT_SECONDS > ANSWER_TIMEOUT_SECONDS
    assert aws.caller_deadline_seconds(behind_cloudfront=False) == ANSWER_TIMEOUT_SECONDS


def test_answer_function_runs_at_most_a_few_requests_at_once(
    stack: SvarStack, template: Template
) -> None:
    """The hard cap on Lambda time and model calls behind an address anyone may find: five
    questions, and room for one page load beside them."""
    properties = resource_of(stack, template, stack.answer_function)["Properties"]
    assert properties["ReservedConcurrentExecutions"] == ANSWER_MAX_CONCURRENCY + 5 == 10


def test_answer_function_is_configured_only_through_its_environment(
    stack: SvarStack, template: Template, taug_stack: TaugStack, taug_template: Template
) -> None:
    variables = resource_of(stack, template, stack.answer_function)["Properties"]["Environment"][
        "Variables"
    ]
    assert variables == {
        "KNOWLEDGE_BASE_ID": imported(
            taug_stack, taug_template, taug_stack.knowledge_base, "KnowledgeBaseId"
        ),
        "PAGE_FOLDER": "/opt",
        "MODEL_ID": FRANKFURT.answer_model_id,
        "ANSWER_PROMPT": "each-part",
        "REGION": {"Ref": "AWS::Region"},
        "NUMBER_OF_RESULTS": str(NUMBER_OF_RESULTS),
        "API_KEY_PARAMETER_PATH": API_KEY_PARAMETER_PATH,
        "BUDGET_TABLE_NAME": {"Ref": stack.get_logical_id(stack.budget_table.node.default_child)},
        "QUESTION_LIMIT": str(DEFAULT_QUESTION_LIMIT),
        "WINDOW_HOURS": str(DEFAULT_WINDOW_HOURS),
        "KEEP_DAYS": str(DEFAULT_KEEP_DAYS),
        "GUARDRAIL_ID": get_att(stack, stack.guardrail_version, "GuardrailId"),
        "GUARDRAIL_VERSION": get_att(stack, stack.guardrail_version, "Version"),
        "EXCERPT_GUARDRAIL_ID": get_att(stack, stack.excerpt_guardrail_version, "GuardrailId"),
        "EXCERPT_GUARDRAIL_VERSION": get_att(stack, stack.excerpt_guardrail_version, "Version"),
        "TOPIC_MODEL_ID": TOPIC_MODEL.model_id,
        "PRICE_TOPIC_MODEL_INPUT_PER_1K_TOKENS": TOPIC_MODEL.input_per_1k,
        "PRICE_TOPIC_MODEL_OUTPUT_PER_1K_TOKENS": TOPIC_MODEL.output_per_1k,
        "PRICE_INPUT_PER_1K_TOKENS": FRANKFURT_MODEL.input_per_1k,
        "PRICE_OUTPUT_PER_1K_TOKENS": FRANKFURT_MODEL.output_per_1k,
        "PRICE_TOPIC_PER_1K_UNITS": PRICE_TOPIC_PER_1K_UNITS,
        "PRICE_PII_PER_1K_UNITS": PRICE_PII_PER_1K_UNITS,
        "PRICE_GROUNDING_PER_1K_UNITS": PRICE_GROUNDING_PER_1K_UNITS,
        "PRICE_DATE": FRANKFURT_PRICE_DATE,
    }
    assert set(variables) == {
        *config.ENVIRONMENT_VARIABLES.values(),
        *config.TOPIC_MODEL_VARIABLES.values(),
        *config.PAGE_VARIABLES.values(),
        config.ANSWER_PROMPT_VARIABLE,
    }


def test_the_model_id_is_configuration_not_handler_code() -> None:
    for module in (REPOSITORY_ROOT / "svar" / "app").glob("*.py"):
        assert ANSWER_MODEL_ID not in module.read_text(encoding="utf-8"), module.name


def test_answer_role_has_five_policies_on_named_resources(
    stack: SvarStack, template: Template, taug_stack: TaugStack, taug_template: Template
) -> None:
    policies = role_policies(stack, template, stack.answer_role)
    assert set(policies) == {
        "ReadApiKeys",
        "CountQuestionsPerKey",
        "RetrieveFromKnowledgeBase",
        "InvokeAnswerModel",
        "WriteLogs",
    }

    (read_keys,) = policies["ReadApiKeys"]["Statement"]
    assert read_keys["Action"] == "ssm:GetParameter"
    assert joined_last_part(read_keys["Resource"]) == f":parameter{API_KEY_PARAMETER_PATH}/*"

    (count,) = policies["CountQuestionsPerKey"]["Statement"]
    assert count["Action"] == "dynamodb:UpdateItem"
    assert count["Resource"] == get_att(stack, stack.budget_table.node.default_child, "Arn")

    (retrieve,) = policies["RetrieveFromKnowledgeBase"]["Statement"]
    assert retrieve["Action"] == "bedrock:Retrieve"
    assert retrieve["Resource"] == imported(
        taug_stack, taug_template, taug_stack.knowledge_base, "KnowledgeBaseArn"
    )

    # Frankfurt's model runs in the region: invoked directly, only with the answer guardrail.
    statements = {s["Sid"]: s for s in policies["InvokeAnswerModel"]["Statement"]}
    assert set(statements) == {
        "InvokeAnswerModelStatement",
        "ApplyAnswerGuardrail",
        "InvokeTopicModel",
    }
    invoke = statements["InvokeAnswerModelStatement"]
    assert invoke["Action"] == "bedrock:InvokeModel"
    assert joined_last_part(invoke["Resource"]) == (
        f"::foundation-model/{FRANKFURT.answer_model_id}"
    )
    # Only through a numbered version of the answer guardrail: <guardrail arn>:<number>, never
    # the draft. What that allows and refuses, step by step through a deploy:
    # svar/tests/test_deploy_steps.py.
    assert invoke["Condition"] == guarded_by(stack)
    apply_guardrail = statements["ApplyAnswerGuardrail"]
    assert apply_guardrail["Action"] == "bedrock:ApplyGuardrail"
    # The CDK sorts the resources of a statement; compared without order.
    assert sorted(map(json.dumps, apply_guardrail["Resource"])) == sorted(
        map(
            json.dumps,
            [
                get_att(stack, stack.guardrail, "GuardrailArn"),
                get_att(stack, stack.excerpt_guardrail, "GuardrailArn"),
            ],
        )
    )
    topic = statements["InvokeTopicModel"]
    assert topic["Action"] == "bedrock:InvokeModel"
    assert joined_last_part(topic["Resource"]) == (
        f":bedrock:{TEST_REGION}::foundation-model/{FRANKFURT.topic_model_id}"
    )
    assert "Condition" not in topic

    (write_logs,) = policies["WriteLogs"]["Statement"]
    assert sorted(write_logs["Action"]) == ["logs:CreateLogStream", "logs:PutLogEvents"]
    assert write_logs["Resource"] == get_att(
        stack, stack.answer_log_group.node.default_child, "Arn"
    )


REGIONAL_MODEL_ID = "nvidia.nemotron-super-3-120b"


def guarded_by(stack: SvarStack) -> dict[str, Any]:
    """The condition on every statement that opens the answer model."""
    return {
        "ArnLike": {
            "bedrock:GuardrailIdentifier": {
                "Fn::Join": ["", [get_att(stack, stack.guardrail, "GuardrailArn"), ":*"]]
            }
        },
        "StringNotLike": {"bedrock:GuardrailIdentifier": "*:DRAFT"},
    }


def test_a_model_in_the_region_is_invoked_directly_with_its_own_prices() -> None:
    app = cdk.App(context=deploy_context())
    _, stack = andur_stacks(app, "RegionalModelStack", answer_model_id=REGIONAL_MODEL_ID)
    template = Template.from_stack(stack)
    policies = role_policies(stack, template, stack.answer_role)
    statements = {s["Sid"]: s for s in policies["InvokeAnswerModel"]["Statement"]}
    invoke = statements["InvokeAnswerModelStatement"]

    assert invoke["Action"] == "bedrock:InvokeModel"
    assert joined_last_part(invoke["Resource"]) == f"::foundation-model/{REGIONAL_MODEL_ID}"
    assert invoke["Condition"] == guarded_by(stack)
    assert statements["ApplyAnswerGuardrail"]["Action"] == "bedrock:ApplyGuardrail"
    variables = resource_of(stack, template, stack.answer_function)["Properties"]["Environment"]
    assert variables["Variables"]["MODEL_ID"] == REGIONAL_MODEL_ID
    assert variables["Variables"]["PRICE_INPUT_PER_1K_TOKENS"] == "0.00018"
    assert variables["Variables"]["PRICE_OUTPUT_PER_1K_TOKENS"] == "0.00078"


def test_a_model_behind_the_eu_profile_is_invoked_through_it() -> None:
    """An answer model of the table with an ``eu.`` id: the profile needs the guardrail, the
    model behind it needs the profile."""
    profile_model = answer_model("eu.amazon.nova-pro-v1:0")
    app = cdk.App(context=deploy_context())
    _, stack = andur_stacks(app, "ProfileModelStack", answer_model_id=profile_model.model_id)
    template = Template.from_stack(stack)
    policies = role_policies(stack, template, stack.answer_role)
    statements = {s["Sid"]: s for s in policies["InvokeAnswerModel"]["Statement"]}
    profile = statements["InvokeAnswerModelProfile"]
    model = statements["InvokeAnswerModelInEurope"]
    assert profile["Action"] == model["Action"] == "bedrock:InvokeModel"
    assert joined_last_part(profile["Resource"]) == f":inference-profile/{profile_model.model_id}"
    assert model["Resource"] == {
        "Fn::Join": [
            "",
            [
                "arn:",
                {"Ref": "AWS::Partition"},
                ":bedrock:eu-*::foundation-model/" + profile_model.model_id.removeprefix("eu."),
            ],
        ]
    }
    assert model["Condition"] == {
        "StringEquals": {"bedrock:InferenceProfileArn": profile["Resource"]}
    }
    assert profile["Condition"] == guarded_by(stack)


def test_a_model_without_a_price_does_not_synthesise() -> None:
    app = cdk.App(context=deploy_context())
    with pytest.raises(KeyError, match="has no price"):
        andur_stacks(app, "UnpricedModelStack", answer_model_id="eu.unpriced-model-v1:0")


def test_the_default_model_carries_its_own_prices() -> None:
    row = answer_model(ANSWER_MODEL_ID)
    assert (PRICE_INPUT_PER_1K_TOKENS, PRICE_OUTPUT_PER_1K_TOKENS) == (
        row.input_per_1k,
        row.output_per_1k,
    )
    assert (row.name, row.input_per_1k, row.output_per_1k) == (
        "Qwen3 235B A22B 2507",
        "0.00029",
        "0.00116",
    )


def test_account_wide_names_carry_the_region(
    template: Template, taug_stack: TaugStack, taug_template: Template
) -> None:
    """IAM names hold for the whole account: without the region, a stack in another region
    would collide with this one."""
    name = resource_of(taug_stack, taug_template, taug_stack.knowledge_base_role)["Properties"][
        "RoleName"
    ]
    assert {"Ref": "AWS::Region"} in name["Fn::Join"][1], name


def test_answer_role_has_no_managed_policy(stack: SvarStack, template: Template) -> None:
    """The managed basic execution policy would allow writing to every log group."""
    assert "ManagedPolicyArns" not in resource_of(stack, template, stack.answer_role)["Properties"]


def test_the_function_url_is_open_and_allows_no_other_origin(
    stack: SvarStack, template: Template
) -> None:
    """AuthType NONE: the URL is the address, and the key header is the gate. No CORS
    configuration either: the page is served from the same address, so there is no
    cross-origin request to allow."""
    template.resource_count_is("AWS::Lambda::Url", 1)
    url = resource_of(stack, template, stack.function_url)
    assert url["Properties"]["AuthType"] == "NONE"
    assert "Cors" not in url["Properties"]


def test_the_web_address_is_the_function_url(stack: SvarStack, template: Template) -> None:
    value = template.find_outputs("WebUrl")["WebUrl"]["Value"]
    assert value == get_att(stack, stack.function_url.node.default_child, "FunctionUrl")


def test_the_page_says_what_it_may_load_and_who_may_frame_it() -> None:
    """A page that keeps a key in localStorage may not be framed and may not load strangers.

    ``default-src 'none'`` is the floor and every source below it is the
    page's own origin, so a script or a stylesheet added from anywhere else
    does not run. ``frame-ancestors 'none'`` keeps the page out of somebody
    else's frame, where the key field would be theirs to read. The function sets these
    headers on every response, page and answer alike (svar/app/page.py).
    """
    policy = SECURITY_HEADERS["Content-Security-Policy"]
    directives = dict(part.split(" ", 1) for part in policy.split("; "))
    assert directives["default-src"] == "'none'"
    assert directives["frame-ancestors"] == "'none'"
    assert directives["script-src"] == directives["style-src"] == "'self'"
    assert directives["connect-src"] == "'self'"


def test_the_address_may_only_be_reached_over_https_and_not_be_sniffed() -> None:
    assert SECURITY_HEADERS["Strict-Transport-Security"] == (
        f"max-age={HSTS_MAX_AGE_DAYS * 24 * 3600}; includeSubDomains"
    )
    assert SECURITY_HEADERS["X-Content-Type-Options"] == "nosniff"
    assert SECURITY_HEADERS["Referrer-Policy"] == "no-referrer"
    assert SECURITY_HEADERS["X-Frame-Options"] == "DENY"


# --- The page's own files ------------------------------------------------


def web_files() -> list[Path]:
    return sorted(p for p in WEB_FOLDER.iterdir() if p.is_file())


def test_the_page_loads_nothing_from_anywhere_else() -> None:
    """No framework, no font, no script from a server that is not this one."""
    markup = (WEB_FOLDER / ROOT_DOCUMENT).read_text(encoding="utf-8")
    referenced = re.findall(r'(?:src|href)="([^"]*)"', markup)
    assert referenced, "the page should reference its script and its stylesheet"
    names = {path.name for path in web_files()}
    for reference in referenced:
        # A data: URI fetches nothing; everything else must be a file of ours.
        assert reference.startswith("data:") or reference in names, reference
    assert {"app.js", "style.css"} <= set(referenced)


def test_no_file_of_the_page_names_an_address_of_its_own() -> None:
    """The page calls /ask relatively; an address in it would be one to keep in step."""
    for path in web_files():
        if path.suffix == ".md":
            continue
        content = path.read_text(encoding="utf-8")
        for forbidden in ("lambda-url", "amazonaws", "cloudfront.net"):
            assert forbidden not in content, f"{path.name} names {forbidden}"


#: What a workspace key looks like: 43 URL-safe characters from
#: ``secrets.token_urlsafe(32)``. Mixed case and a digit, so that a long
#: ordinary word cannot be mistaken for one.
KEY_SHAPED = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43,}(?![A-Za-z0-9_-])")


def test_no_key_is_in_the_page() -> None:
    for path in web_files():
        for candidate in KEY_SHAPED.findall(path.read_text(encoding="utf-8")):
            looks_like_a_key = (
                any(c.isdigit() for c in candidate)
                and any(c.islower() for c in candidate)
                and any(c.isupper() for c in candidate)
            )
            assert not looks_like_a_key, f"{path.name} carries something key-shaped"


def test_the_page_never_prints_the_key() -> None:
    """Not to the console, and not into the address — the key goes into one header."""
    script = (WEB_FOLDER / "app.js").read_text(encoding="utf-8")
    assert "console." not in script


def page_constant(name: str) -> str:
    """One ``const NAME = "value";`` out of the page's script.

    The page cannot import from Python, so it spells these out. What it
    spells out is what the tests below read back.
    """
    script = (WEB_FOLDER / "app.js").read_text(encoding="utf-8")
    (value,) = re.findall(rf'^const {name} = "([^"]*)";$', script, re.MULTILINE)
    return value


def page_number(name: str) -> int:
    script = (WEB_FOLDER / "app.js").read_text(encoding="utf-8")
    (value,) = re.findall(rf"^const {name} = (\d+);$", script, re.MULTILINE)
    return int(value)


def test_the_page_and_the_stack_mean_the_same_path_and_the_same_headers() -> None:
    """A typo here is a green suite and a page that reaches the wrong path.

    The page posts to `ASK_PATH`; the function answers exactly that one path and serves
    its files on every other (svar/app/page.py). Move the path on one side alone and the
    page's POST lands on a file path: the function answers 404 in plain text, and the page,
    which can only read what it gets, tells the reader something about their key. The
    headers are the same kind of agreement one layer further in.
    """
    assert page_constant("ASK_PATH") == ASK_PATH
    assert page_constant("KEY_HEADER") == intake.KEY_HEADER
    assert page_constant("BODY_HASH_HEADER") == ask.BODY_HASH_HEADER
    assert page_number("MAX_QUESTION_CHARACTERS") == intake.MAX_QUESTION_CHARACTERS


def test_the_page_asks_once_at_a_time() -> None:
    """Enter submits the form directly, and a disabled button does not stop it.

    A second question while the first is in flight is a second question off
    the same ten, two of the five Lambda places, and an answer that may not
    belong to the question in the field. The guard is a flag the submit
    handler reads before anything else.
    """
    script = (WEB_FOLDER / "app.js").read_text(encoding="utf-8")
    assert "let asking = false;" in script
    assert "if (asking) {" in script


def test_the_page_stops_waiting_by_itself() -> None:
    """Without this a hung connection leaves the button on "Asking…" forever.

    The page waits a little longer than the function may run, so in every ordinary
    case the function's own answer or 502 arrives first and says more. This is the floor
    under it: an abort, a cleared timer, and a sentence.
    """
    script = (WEB_FOLDER / "app.js").read_text(encoding="utf-8")
    assert "new AbortController()" in script
    assert "signal: controller.signal" in script
    assert "clearTimeout(giveUp)" in script
    assert "AbortError" in script
    # Longer than the function may run, so the function's answer is the one that wins.
    assert page_number("REQUEST_TIMEOUT_MS") > ANSWER_TIMEOUT_SECONDS * 1000


# --- Key budgets ---------------------------------------------------------


def budget_table(stack: SvarStack, template: Template) -> Mapping[str, Any]:
    """The rendered table. ``TableV2`` renders as a global table with one replica."""
    return resource_of(stack, template, stack.budget_table)


def test_the_budget_table_is_one_item_per_key_hash(stack: SvarStack, template: Template) -> None:
    template.resource_count_is("AWS::DynamoDB::GlobalTable", 1)
    properties = budget_table(stack, template)["Properties"]
    assert properties["TableName"] == BUDGET_TABLE_NAME
    assert properties["KeySchema"] == [{"AttributeName": BUDGET_KEY_ATTRIBUTE, "KeyType": "HASH"}]
    assert properties["AttributeDefinitions"] == [
        {"AttributeName": BUDGET_KEY_ATTRIBUTE, "AttributeType": "S"}
    ]
    assert properties["Replicas"] == [{"Region": TEST_REGION}]


def test_the_budget_table_is_billed_per_request(stack: SvarStack, template: Template) -> None:
    """A public URL has no rate to provision for, and an idle month must cost nothing."""
    assert budget_table(stack, template)["Properties"]["BillingMode"] == "PAY_PER_REQUEST"


def test_the_budget_table_clears_old_items_by_itself(stack: SvarStack, template: Template) -> None:
    """On the attribute the handler writes — a TTL on any other name would never fire."""
    assert budget_table(stack, template)["Properties"]["TimeToLiveSpecification"] == {
        "AttributeName": BUDGET_TTL_ATTRIBUTE,
        "Enabled": True,
    }
    assert f"{BUDGET_TTL_ATTRIBUTE} = " in budget.BUDGET_UPDATE_EXPRESSION


def test_the_table_and_the_handler_agree_on_the_attribute_names() -> None:
    """The stack creates the table, the handler writes the items; a typo would only show in AWS."""
    assert budget.BUDGET_KEY_ATTRIBUTE == BUDGET_KEY_ATTRIBUTE
    assert budget.BUDGET_TTL_ATTRIBUTE == BUDGET_TTL_ATTRIBUTE


def test_the_defaults_the_stack_hands_over_are_a_budget_the_handler_accepts() -> None:
    """The handler refuses an item that would go before its own window closes."""
    config.load_settings(
        {
            "KNOWLEDGE_BASE_ID": "KB1",
            "MODEL_ID": ANSWER_MODEL_ID,
            "REGION": TEST_REGION,
            "NUMBER_OF_RESULTS": str(NUMBER_OF_RESULTS),
            "API_KEY_PARAMETER_PATH": API_KEY_PARAMETER_PATH,
            "BUDGET_TABLE_NAME": BUDGET_TABLE_NAME,
            "QUESTION_LIMIT": str(DEFAULT_QUESTION_LIMIT),
            "WINDOW_HOURS": str(DEFAULT_WINDOW_HOURS),
            "KEEP_DAYS": str(DEFAULT_KEEP_DAYS),
            "GUARDRAIL_ID": "gr1",
            "GUARDRAIL_VERSION": "1",
            "EXCERPT_GUARDRAIL_ID": "gr2",
            "EXCERPT_GUARDRAIL_VERSION": "1",
            "TOPIC_MODEL_ID": TOPIC_MODEL.model_id,
            "PRICE_TOPIC_MODEL_INPUT_PER_1K_TOKENS": TOPIC_MODEL.input_per_1k,
            "PRICE_TOPIC_MODEL_OUTPUT_PER_1K_TOKENS": TOPIC_MODEL.output_per_1k,
            "PRICE_INPUT_PER_1K_TOKENS": PRICE_INPUT_PER_1K_TOKENS,
            "PRICE_OUTPUT_PER_1K_TOKENS": PRICE_OUTPUT_PER_1K_TOKENS,
            "PRICE_TOPIC_PER_1K_UNITS": PRICE_TOPIC_PER_1K_UNITS,
            "PRICE_PII_PER_1K_UNITS": PRICE_PII_PER_1K_UNITS,
            "PRICE_GROUNDING_PER_1K_UNITS": PRICE_GROUNDING_PER_1K_UNITS,
            "PRICE_DATE": FRANKFURT_PRICE_DATE,
            "PAGE_FOLDER": "/opt",
        }
    )
    assert (DEFAULT_QUESTION_LIMIT, DEFAULT_WINDOW_HOURS) == (10, 48)


# --- Guardrail -----------------------------------------------------------


def test_two_guardrails_each_with_one_version(stack: SvarStack, template: Template) -> None:
    template.resource_count_is("AWS::Bedrock::Guardrail", 2)
    template.resource_count_is("AWS::Bedrock::GuardrailVersion", 2)
    for guardrail, version, name in (
        (stack.guardrail, stack.guardrail_version, GUARDRAIL_NAME),
        (stack.excerpt_guardrail, stack.excerpt_guardrail_version, EXCERPT_GUARDRAIL_NAME),
    ):
        assert resource_of(stack, template, guardrail)["Properties"]["Name"] == name
        assert resource_of(stack, template, version)["Properties"][
            "GuardrailIdentifier"
        ] == get_att(stack, guardrail, "GuardrailId")


def test_the_excerpt_guardrail_masks_the_same_details_and_does_nothing_else(
    stack: SvarStack, template: Template
) -> None:
    """No topic, no grounding: an excerpt about VAT is no tax question, and one price only."""
    properties = resource_of(stack, template, stack.excerpt_guardrail)["Properties"]
    assert "TopicPolicyConfig" not in properties
    assert "ContextualGroundingPolicyConfig" not in properties
    assert "ContentPolicyConfig" not in properties and "WordPolicyConfig" not in properties
    entities = properties["SensitiveInformationPolicyConfig"]["PiiEntitiesConfig"]
    answer_entities = resource_of(stack, template, stack.guardrail)["Properties"][
        "SensitiveInformationPolicyConfig"
    ]["PiiEntitiesConfig"]
    assert entities == answer_entities
    assert 1 <= len(properties["BlockedOutputsMessaging"]) <= 500


def test_the_answer_guardrail_reads_no_topic_and_stays_in_the_region(
    stack: SvarStack, template: Template
) -> None:
    """The topic model reads the topics (svar/app/topic_check.py); the answer guardrail keeps
    masking, grounding and the messages, without a guardrail profile."""
    properties = resource_of(stack, template, stack.guardrail)["Properties"]
    assert set(properties) == {
        "Name",
        "Description",
        "BlockedInputMessaging",
        "BlockedOutputsMessaging",
        "SensitiveInformationPolicyConfig",
        "ContextualGroundingPolicyConfig",
    }
    assert properties["Description"] == (
        "Andur: masks personal details, withholds ungrounded answers."
    )
    assert "guardrail-profile" not in json.dumps(template.to_json())


def pascal_case(value: Any) -> Any:
    """Bedrock's camelCase field names as CloudFormation spells them, all the way down."""
    if isinstance(value, Mapping):
        return {key[0].upper() + key[1:]: pascal_case(item) for key, item in value.items()}
    if isinstance(value, list):
        return [pascal_case(item) for item in value]
    return value


def test_the_grounding_probe_copies_the_answer_guardrail(
    stack: SvarStack, template: Template
) -> None:
    """What the probe checks German answers with is what the function checks them with: no
    topic, as the answer guardrail has none, its masking, its grounding thresholds and its
    messages, in the region."""
    properties = resource_of(stack, template, stack.guardrail)["Properties"]
    probe = grounding_probe.probe_request(TEST_REGION)
    assert "topicPolicyConfig" not in probe and "TopicPolicyConfig" not in properties
    for name in ("sensitiveInformationPolicyConfig", "contextualGroundingPolicyConfig"):
        assert pascal_case(probe[name]) == properties[name[0].upper() + name[1:]], name
    assert "crossRegionConfig" not in probe
    assert probe["blockedInputMessaging"] == properties["BlockedInputMessaging"]
    assert probe["blockedOutputsMessaging"] == properties["BlockedOutputsMessaging"]
    assert probe["name"] == "andur-grounding-probe" != properties["Name"]
    assert set(probe) - {"name", "description"} == {
        key[0].lower() + key[1:] for key in properties if key not in {"Name", "Description"}
    }


def test_topic_definitions_follow_the_user_guides_best_practices() -> None:
    """ "Best practices for creating denied topics" in the Bedrock user guide: a definition
    describes a subject; it is no instruction ("Block all ...") and no negative or exception
    ("All contents except ..."). A name is a noun phrase that does not describe the topic."""
    instruction = re.compile(r"\b(block|deny|refuse|reject|do not answer)\b", re.IGNORECASE)
    exception = re.compile(r"\b(except|unless|other than|apart from|not)\b", re.IGNORECASE)
    for name, definition, examples in DENIED_TOPICS:
        assert not instruction.search(definition), name
        assert not exception.search(definition), name
        assert len(name.split()) <= 3, name
        assert not any(example in definition for example in examples), name
    assert len({name for name, _, _ in DENIED_TOPICS}) == len(DENIED_TOPICS)


def test_frankfurt_is_the_one_region_of_the_model_table() -> None:
    assert set(answer_models.REGIONS) == {"eu-central-1"} == {answer_models.DEFAULT_REGION}


@pytest.mark.parametrize("region", ["eu-west-2", "eu-north-1", "eu-central-2", "il-central-1"])
def test_a_stack_outside_the_regions_of_the_model_table_does_not_synthesise(region: str) -> None:
    """A region without a row has no prices and no topic model."""
    app = cdk.App(context=deploy_context())
    with pytest.raises(KeyError, match="has no models"):
        andur_stacks(app, "OutsideStack", env=cdk.Environment(region=region))


def test_the_guardrail_messages_stay_inside_what_bedrock_accepts() -> None:
    """Too long is no synth error; it is a failed deploy after several minutes."""
    assert 1 <= len(BLOCKED_INPUT_MESSAGE) <= 500
    assert 1 <= len(BLOCKED_OUTPUT_MESSAGE) <= 500


def test_the_handler_says_in_english_what_the_guardrail_says_and_in_german_the_same() -> None:
    """The handler replaces the guardrail's English message with its own sentence in the
    language of the request. The stack's message stays as the fallback, and the two must not
    drift apart: a visitor who asks in English reads the same words either way."""
    assert sentences.QUESTION_REFUSED_ANSWERS["en"] == BLOCKED_INPUT_MESSAGE
    assert sentences.ANSWER_WITHHELD_ANSWERS["en"] == BLOCKED_OUTPUT_MESSAGE
    assert 1 <= len(sentences.QUESTION_REFUSED_ANSWERS["de"]) <= 500
    assert 1 <= len(sentences.ANSWER_WITHHELD_ANSWERS["de"]) <= 500


def test_personal_details_are_masked_in_the_answer_only(
    stack: SvarStack, template: Template
) -> None:
    config = resource_of(stack, template, stack.guardrail)["Properties"][
        "SensitiveInformationPolicyConfig"
    ]
    entities = config["PiiEntitiesConfig"]
    assert (
        [entity["Type"] for entity in entities]
        == list(MASKED_PII_TYPES)
        == ["NAME", "EMAIL", "PHONE", "ADDRESS"]
    )
    for entity in entities:
        assert entity["Action"] == "ANONYMIZE"
        assert (entity["OutputEnabled"], entity["OutputAction"]) == (True, "ANONYMIZE")
        assert entity["InputEnabled"] is False


def test_grounding_and_relevance_are_checked_at_documented_thresholds(
    stack: SvarStack, template: Template
) -> None:
    config = resource_of(stack, template, stack.guardrail)["Properties"][
        "ContextualGroundingPolicyConfig"
    ]
    filters = {check["Type"]: check for check in config["FiltersConfig"]}
    assert set(filters) == {"GROUNDING", "RELEVANCE"}
    assert filters["GROUNDING"]["Threshold"] == FRANKFURT.grounding_threshold == 0.8
    assert filters["RELEVANCE"]["Threshold"] == RELEVANCE_THRESHOLD == 0.7
    for check in filters.values():
        assert 0 <= check["Threshold"] <= 0.99  # 1 blocks everything, says the user guide
        assert (check["Action"], check["Enabled"]) == ("BLOCK", True)


def test_a_changed_setting_makes_a_new_guardrail_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """The version's description is create-only, and it carries a digest of the settings.

    Without that, a new threshold would reach the guardrail's draft and never
    the version the function uses.
    """

    def version_description() -> str:
        _, other = andur_stacks(cdk.App(context=deploy_context()), "VersionStack")
        return Template.from_stack(other).to_json()["Resources"][
            other.get_logical_id(other.guardrail_version)
        ]["Properties"]["Description"]

    before = version_description()
    assert before == version_description()
    monkeypatch.setitem(
        answer_models.REGIONS, TEST_REGION, FRANKFURT._replace(grounding_threshold=0.9)
    )
    after = version_description()
    assert after != before
    assert len(after) <= 200


def test_a_changed_output_action_makes_new_versions_of_both_guardrails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The digest covers the built properties, not a summary of them.

    An output action was never in the summary the digest used to be taken
    over: switching masking to blocking changed the guardrail's draft and left
    both versions — the ones the function uses — as they were.
    """
    from aws_cdk import aws_bedrock as bedrock

    from svar.infra import guardrails as stack_guardrails

    def descriptions() -> tuple[str, str]:
        _, other = andur_stacks(cdk.App(context=deploy_context()), "ActionStack")
        resources = Template.from_stack(other).to_json()["Resources"]
        return tuple(
            resources[other.get_logical_id(version)]["Properties"]["Description"]
            for version in (other.guardrail_version, other.excerpt_guardrail_version)
        )

    real_policy = stack_guardrails.masking_policy

    def blocking_policy() -> bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty:
        entities = [
            bedrock.CfnGuardrail.PiiEntityConfigProperty(
                type=entity.type,
                action=entity.action,
                input_enabled=entity.input_enabled,
                input_action=entity.input_action,
                output_enabled=entity.output_enabled,
                output_action="BLOCK",
            )
            for entity in real_policy().pii_entities_config
        ]
        return bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty(
            pii_entities_config=entities
        )

    answer_before, excerpt_before = descriptions()
    monkeypatch.setattr(stack_guardrails, "masking_policy", blocking_policy)
    answer_after, excerpt_after = descriptions()
    assert answer_after != answer_before
    assert excerpt_after != excerpt_before


def test_the_prices_are_ones_the_handler_accepts_and_dated() -> None:
    for value in (
        PRICE_INPUT_PER_1K_TOKENS,
        PRICE_OUTPUT_PER_1K_TOKENS,
        PRICE_TOPIC_PER_1K_UNITS,
        PRICE_PII_PER_1K_UNITS,
        PRICE_GROUNDING_PER_1K_UNITS,
    ):
        config.price("PRICE", value)
    assert config.price_day(FRANKFURT_PRICE_DATE) == FRANKFURT_PRICE_DATE


def test_the_guardrail_version_is_an_output(stack: SvarStack, template: Template) -> None:
    outputs = template.find_outputs("GuardrailVersion")
    (output,) = outputs.values()
    assert output["Value"] == {"Ref": stack.get_logical_id(stack.guardrail_version)}
    (excerpt_output,) = template.find_outputs("ExcerptGuardrailRef").values()
    assert excerpt_output["Value"] == {"Ref": stack.get_logical_id(stack.excerpt_guardrail_version)}
    (topic_output,) = template.find_outputs("TopicModelId").values()
    assert topic_output["Value"] == FRANKFURT.topic_model_id


def test_a_changed_topic_changes_no_guardrail_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The topic model reads `DENIED_TOPICS` (svar/app/topic_model.py); no guardrail does, so a
    changed definition leaves both versions as they are."""
    from svar.app import denied_topics

    def descriptions() -> tuple[str, str]:
        _, other = andur_stacks(cdk.App(context=deploy_context()), "TopicStack")
        resources = Template.from_stack(other).to_json()["Resources"]
        return tuple(
            resources[other.get_logical_id(version)]["Properties"]["Description"]
            for version in (other.guardrail_version, other.excerpt_guardrail_version)
        )

    before = descriptions()
    name, definition, examples = denied_topics.DENIED_TOPICS[0]
    changed = ((name, definition + " Changed.", examples), *denied_topics.DENIED_TOPICS[1:])
    monkeypatch.setattr(denied_topics, "DENIED_TOPICS", changed)
    assert descriptions() == before


def test_no_role_may_do_more_than_count_on_the_budget_table(template: Template) -> None:
    """One action, on one table. Reading or writing items freely is nobody's job here."""
    actions = [
        statement["Action"]
        for _, statement in all_policy_statements(template)
        if "dynamodb" in json.dumps(statement["Action"])
    ]
    assert actions == ["dynamodb:UpdateItem"]


# --- Workspace keys ------------------------------------------------------


#: The logical ids of the key function as deployed before 2026-10-01. In Andur's one stack the
#: Delete of each removed key resource reached the function under these ids. Taug's stack
#: builds it anew under the same ids, as its first content (taug/infra/keys.py); no resource
#: sends a Delete there.
DEPLOYED_KEY_FUNCTION_IDS = {
    "ApiKeyFunctionRoleC17A4D17": "AWS::IAM::Role",
    "ApiKeyFunction7C99575B": "AWS::Lambda::Function",
    "ApiKeyProviderframeworkonEvent9849A943": "AWS::Lambda::Function",
}


def test_the_stack_creates_no_workspace_key(template: Template) -> None:
    """Keys come from `python -m taug.scripts.workspace add`; no workspace is named in the code.
    The one custom resource left is the deploy drain, which waits and touches nothing."""
    custom = sorted(
        resource["Type"]
        for resource in template.to_json()["Resources"].values()
        if resource["Type"].startswith("Custom::")
    )
    assert custom == ["Custom::AndurDeployDrain"]


def test_the_key_function_keeps_its_logical_ids(taug_template: Template) -> None:
    """Built anew in Taug's stack, under the ids it had in Andur's one stack, so the split
    keeps the names the tests of the split (tests/test_split.py) hold."""
    resources = taug_template.to_json()["Resources"]
    for logical_id, resource_type in DEPLOYED_KEY_FUNCTION_IDS.items():
        assert resources[logical_id]["Type"] == resource_type, logical_id


def test_no_role_may_write_or_delete_a_key(template: Template) -> None:
    """The deploy that drops the key resources runs their Delete with this stack's roles.

    None of them can delete a parameter any more, so not even an old copy of the key
    function could take a key with it. Reading keys stays with the answer function alone.
    """
    ssm_actions = sorted(
        action
        for _, statement in all_policy_statements(template)
        for action in (
            statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
        )
        if action.startswith("ssm:")
    )
    assert ssm_actions == ["ssm:GetParameter"]


def test_the_key_function_role_may_only_write_its_log(
    taug_stack: TaugStack, taug_template: Template
) -> None:
    policies = role_policies(
        taug_stack, taug_template, taug_stack.node.find_child("ApiKeyFunctionRole")
    )
    assert set(policies) == {"WriteLogs"}


def test_the_key_function_runs_the_handler_that_deletes_nothing(
    taug_stack: TaugStack, taug_template: Template
) -> None:
    function = resource_of(taug_stack, taug_template, taug_stack.node.find_child("ApiKeyFunction"))
    assert function["Properties"]["Handler"] == "handler.on_event"
    assert "deletes nothing" in function["Properties"]["Description"]


# --- Logs ----------------------------------------------------------------


def test_every_log_group_has_a_retention_and_goes_with_the_stack(
    template: Template, taug_template: Template
) -> None:
    log_groups = template.find_resources("AWS::Logs::LogGroup")
    taug_log_groups = taug_template.find_resources("AWS::Logs::LogGroup")
    assert (len(log_groups), len(taug_log_groups)) == (3, 2)
    for log_group in [*log_groups.values(), *taug_log_groups.values()]:
        assert log_group["Properties"]["RetentionInDays"] == 30
        assert log_group["DeletionPolicy"] == "Delete"


def test_every_function_logs_into_a_log_group_of_the_stack(template: Template) -> None:
    """Otherwise Lambda creates one on first use: no retention, and it outlives cdk destroy."""
    log_group_ids = set(template.find_resources("AWS::Logs::LogGroup"))
    for function in template.find_resources("AWS::Lambda::Function").values():
        assert function["Properties"]["LoggingConfig"]["LogGroup"]["Ref"] in log_group_ids


def test_answer_log_group_has_the_name_lambda_would_give_it(
    stack: SvarStack, template: Template
) -> None:
    properties = resource_of(stack, template, stack.answer_log_group)["Properties"]
    assert properties["LogGroupName"] == f"/aws/lambda/{ANSWER_FUNCTION_NAME}"
