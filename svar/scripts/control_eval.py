"""Measure how well an answer model stays with its sources, the way the function answers.

`svar/scripts/evaluate.py` asks whether an answer is right. This asks more of a
model: whether it declines what the documents do not say even when an answer
lies close (`unanswerable` questions, each a temptation), whether it answers
the documented half of a question and leaves the other half alone (`partial`),
whether it adds a figure or a forbidden statement of its own, whether it
cites the document the answer comes from, whether it answers German in
German, and whether an advice question is refused.

Every question goes through `answer_question` in svar/app/answering.py, with the
prompt, the guardrails and the cost line of the function. Probe guardrails
are created in ``--region`` for the run and deleted after it, copies of the
stack's: the answer guardrail (masking, grounding check) and the excerpt
guardrail (masking). The region's topic model checks the denied topics, as the
function does. Retrieval runs once per question against the knowledge base in
``--retrieve-region``, and every model gets the same passages.

**What counts as added.** A model *adds* when what it wrote, before any
guardrail, holds

- a number that stands in none of the passages it was given and not in the
  question (`invented_numbers`): "1,450" and "1.450" are the same number,
  "07:30" holds 7 and 30, a date holds its day, month and year, and the
  passages also count with their number words ("five" is 5, "März" is 3).
  0 and 1 never count: they stand for "a", "eine", "once";
- or one of the question's ``forbidden`` spellings: what a model would have
  to know from elsewhere (a tax rule, a medicine, a funding programme).

The rule is mechanical and repeatable. It sees numbers and the listed
spellings, not an invented sentence without them.
"""

import argparse
import dataclasses
import json
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from svar.infra.answer_prompts import prompt_of
from svar.infra.guardrails import (
    BLOCKED_INPUT_MESSAGE,
    BLOCKED_OUTPUT_MESSAGE,
    EXCERPT_BLOCKED_MESSAGE,
    MASKED_PII_TYPES,
    RELEVANCE_THRESHOLD,
    grounding_threshold_for,
)
from svar.scripts.control_marks import (
    ModelTexts,
    Passages,
    grade,
    passage_texts,
    summarise,
    table,
)
from svar.scripts.evaluate import (
    ask,
    deployed_environment,
    import_app,
    load_questions,
    stays_in_the_eu,
)
from svar.scripts.probe_guardrails import create_probe, delete_reporting, wait_until_ready
from taug.infra.answer_models import (
    DEFAULT_REGION,
    REGIONS,
    AnswerModel,
    answer_model,
    region_models,
)
from taug.scripts import utf8_output
from taug.scripts.aws import AwsProblem, client

QUESTIONS_FILE = Path(__file__).with_name("evaluation-questions-control.json")

#: The probe guardrails, by the stack guardrail each one copies. The only guardrails this
#: script creates or deletes.
PROBE_NAMES = {
    "answer": "andur-control-answer-probe",
    "excerpt": "andur-control-excerpt-probe",
}

#: The options that pick what a run asks; ``--regrade`` marks every model and question of a
#: saved run again and takes none of them.
SELECTIONS = ("model", "only", "workspace")


def load_control_questions(path: Path) -> list[dict[str, Any]]:
    """The question set, checked like `evaluate`'s, plus ``forbidden`` and ``partial``."""
    questions = load_questions(path)
    seen = set()
    for question in questions:
        name = question["id"]
        if name in seen:
            raise ValueError(f"{name}: the id is used twice")
        seen.add(name)
        forbidden = question.get("forbidden", [])
        if not (isinstance(forbidden, list) and all(isinstance(f, str) and f for f in forbidden)):
            raise ValueError(f"{name}: forbidden must be a list of spellings")
        if question.get("partial", False) not in (True, False):
            raise ValueError(f"{name}: partial must be true or false")
        if question.get("partial") and question["kind"] != "answer":
            raise ValueError(f"{name}: only an answer question can be partial")
    return questions


def probe_requests(region: str = DEFAULT_REGION) -> dict[str, dict[str, Any]]:
    """The CreateGuardrail request of each probe: the stack's two guardrails in ``region``,
    with its grounding threshold, in the region."""
    masking = {
        "piiEntitiesConfig": [
            {
                "type": pii_type,
                "action": "ANONYMIZE",
                "inputEnabled": False,
                "inputAction": "NONE",
                "outputEnabled": True,
                "outputAction": "ANONYMIZE",
            }
            for pii_type in MASKED_PII_TYPES
        ]
    }

    note = "Andur: temporary, svar/scripts/control_eval.py; deleted after the run."
    return {
        "answer": {
            "name": PROBE_NAMES["answer"],
            "description": note,
            "sensitiveInformationPolicyConfig": masking,
            "contextualGroundingPolicyConfig": {
                "filtersConfig": [
                    {
                        "type": "GROUNDING",
                        "threshold": grounding_threshold_for(region),
                        "action": "BLOCK",
                    },
                    {"type": "RELEVANCE", "threshold": RELEVANCE_THRESHOLD, "action": "BLOCK"},
                ]
            },
            "blockedInputMessaging": BLOCKED_INPUT_MESSAGE,
            "blockedOutputsMessaging": BLOCKED_OUTPUT_MESSAGE,
        },
        "excerpt": {
            "name": PROBE_NAMES["excerpt"],
            "description": note,
            "sensitiveInformationPolicyConfig": masking,
            "blockedInputMessaging": EXCERPT_BLOCKED_MESSAGE,
            "blockedOutputsMessaging": EXCERPT_BLOCKED_MESSAGE,
        },
    }


def settings_for(base: Any, model: AnswerModel, region: str, probes: Mapping[str, str]) -> Any:
    """The deployed settings with this model, its prompt wording and its token prices from the
    table of ``region``, the region of the model and the probe guardrails (their drafts). The
    region's topic model checks the topics, at its price there. The guardrail unit prices stay
    the deployed ones."""
    topic_model_id = REGIONS[region].topic_model_id
    topic_model = answer_model(topic_model_id, region)
    prices = dataclasses.replace(
        base.prices,
        input_tokens=Decimal(model.input_per_1k),
        output_tokens=Decimal(model.output_per_1k),
        topic_model_input_tokens=Decimal(topic_model.input_per_1k),
        topic_model_output_tokens=Decimal(topic_model.output_per_1k),
    )
    return dataclasses.replace(
        base,
        model_id=model.model_id,
        answer_prompt=prompt_of(model),
        region=region,
        prices=prices,
        price_date=region_models(region).price_date,
        guardrail_id=probes["answer"],
        guardrail_version="DRAFT",
        topic_model_id=topic_model_id,
        excerpt_guardrail_id=probes["excerpt"],
        excerpt_guardrail_version="DRAFT",
    )


def run_model(
    app: Any,
    settings: Any,
    questions: Sequence[Mapping[str, Any]],
    passages: Passages,
    texts: ModelTexts,
    progress: Callable[[str], None],
) -> list[dict[str, Any]]:
    """Every question for one model: the outcome, what the model wrote, the marks."""
    rows = []
    for question in questions:
        # No model call when retrieval finds nothing or fails: the last question's text must
        # not stand in for this one's.
        texts.last = None
        outcome = ask(app, settings, question)
        body = outcome.get("body")
        results = passages.found.get(passages.key(question["workspace"], question["question"]), [])
        given = passage_texts(app, results, question["workspace"])
        marks = grade(question, body, texts.last, given, app)
        rows.append(
            {
                "id": question["id"],
                "kind": question["kind"],
                "partial": question.get("partial", False),
                "language_asked": question.get("language", "en"),
                **outcome,
                "model_text": texts.last,
                **marks,
            }
        )
        flags = [
            "right" if marks["correct"] else "WRONG",
            *(
                ["added " + ", ".join(marks["invented_numbers"] + marks["forbidden"])]
                * marks["added"]
            ),
            *(["blocked"] * marks["blocked"]),
            *([outcome["error"]] if "error" in outcome else []),
        ]
        progress(f"{settings.model_id} {question['id']}: {'; '.join(flags)}")
    return rows


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """The command line; both regions default to `DEFAULT_REGION`. ``--regrade`` takes none
    of `SELECTIONS`: it marks the whole saved run again."""
    parser = argparse.ArgumentParser(
        prog="python -m svar.scripts.control_eval",
        description="Measure how well answer models stay with their sources.",
    )
    parser.add_argument(
        "--model",
        action="append",
        help="a model name from the table of --region; default: all of them",
    )
    parser.add_argument("--questions", type=Path, default=QUESTIONS_FILE)
    parser.add_argument("--only", action="append", help="ask only this question id (may repeat)")
    parser.add_argument("--workspace", help="ask only the questions of this workspace")
    parser.add_argument("--out", type=Path, help="write every answer and mark as JSON here")
    parser.add_argument(
        "--regrade",
        type=Path,
        help="mark the run saved in this --out file again, with no AWS call, and rewrite it",
    )
    parser.add_argument(
        "--region", default=DEFAULT_REGION, help=f"model and guardrails; default {DEFAULT_REGION}"
    )
    parser.add_argument(
        "--retrieve-region",
        default=DEFAULT_REGION,
        help=f"the deployed function and its knowledge base; default {DEFAULT_REGION}",
    )
    args = parser.parse_args(argv)
    if args.regrade:
        given = [f"--{name}" for name in SELECTIONS if getattr(args, name)]
        if given:
            parser.error(
                f"--regrade marks every model and question of the saved run again; "
                f"{', '.join(given)} would be ignored"
            )
    return args


def regrade(path: Path, questions_file: Path) -> int:
    """Mark a saved run again with the rules as they are now, without a single AWS call: the
    function's response, the model's own text and the passages are all in the file."""
    app = import_app()
    data = json.loads(path.read_text(encoding="utf-8"))
    questions = {q["id"]: q for q in load_control_questions(questions_file)}
    # A run saved before it named its region ran in the default one.
    region = data.get("region", DEFAULT_REGION)
    if region not in REGIONS:
        print(
            f"error: {path} was run in {region}, which has no models in "
            "taug/infra/answer_models.py; nothing was rewritten",
            file=sys.stderr,
        )
        return 2
    by_name = {m.name: m for m in region_models(region).models}
    summaries = []
    for name, rows in data["runs"].items():
        for row in rows:
            question = questions[row["id"]]
            key = Passages.key(question["workspace"], question["question"])
            if row.get("model_text") and key not in data["passages"]:
                print(
                    f"error: no saved passages for {row['id']}: its question changed after the "
                    "run; nothing was rewritten",
                    file=sys.stderr,
                )
                return 2
            given = passage_texts(app, data["passages"].get(key, []), question["workspace"])
            row.update(grade(question, row.get("body"), row.get("model_text"), given, app))
        summaries.append(summarise(by_name[name], rows))
    data["summaries"] = summaries
    data["regraded"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    for line in table(summaries):
        print(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the chosen models through the questions with probe guardrails and print the
    table; with ``--regrade``, mark a saved run again instead."""
    utf8_output()
    args = parse_args(argv)
    if args.regrade:
        return regrade(args.regrade, args.questions)
    outside_regions = [r for r in (args.region, args.retrieve_region) if not r.startswith("eu-")]
    if outside_regions:
        print(f"error: {outside_regions} are not EU regions", file=sys.stderr)
        return 2
    if args.region not in REGIONS:
        print(f"error: {args.region} has no models in taug/infra/answer_models.py", file=sys.stderr)
        return 2
    known = region_models(args.region).models
    models = [m for m in known if not args.model or m.name in args.model]
    unknown = set(args.model or []) - {m.name for m in known}
    if unknown or not models:
        print(
            f"error: unknown model {sorted(unknown)}; known: {[m.name for m in known]}",
            file=sys.stderr,
        )
        return 2
    outside = [m.model_id for m in models if not stays_in_the_eu(m.model_id)]
    if outside:
        print(f"error: these would leave the EU: {outside}", file=sys.stderr)
        return 2
    questions = load_control_questions(args.questions)
    if args.only:
        unknown_ids = set(args.only) - {q["id"] for q in questions}
        if unknown_ids:
            print(f"error: no questions {sorted(unknown_ids)} in {args.questions}", file=sys.stderr)
            return 2
        questions = [q for q in questions if q["id"] in args.only]
    if args.workspace:
        questions = [q for q in questions if q["workspace"] == args.workspace]
        if not questions:
            print(f"error: no questions of {args.workspace} in {args.questions}", file=sys.stderr)
            return 2
    if args.out:
        # A bad path fails now, not after paying; an earlier result there stays as it is. One
        # that goes bad during the run ends it after the model whose result it could not take.
        try:
            with args.out.open("a", encoding="utf-8"):
                pass
        except OSError as problem:
            print(f"error: {args.out} cannot be written: {problem}", file=sys.stderr)
            return 2

    def progress(line: str) -> None:
        print(line, file=sys.stderr)

    app = import_app()
    try:
        base = app.config.load_settings(deployed_environment(args.retrieve_region))
        bedrock = client("bedrock", args.region)
    except (AwsProblem, app.errors.ConfigurationError) as problem:
        print(f"error: {problem}", file=sys.stderr)
        return 2
    passages = Passages(
        app.retrieval.retrieve_passages, client("bedrock-agent-runtime", args.retrieve_region)
    )
    texts = ModelTexts(app.llm.ask_model, app)
    app.retrieval.retrieve_passages, app.llm.ask_model = passages, texts

    probes: dict[str, str] = {}
    runs: dict[str, list[dict[str, Any]]] = {}
    summaries: list[dict[str, Any]] = []
    started = time.strftime("%Y-%m-%dT%H:%M:%S")
    failed: AwsProblem | str | None = None
    try:
        for kind, request in probe_requests(args.region).items():
            probes[kind] = create_probe(bedrock, request)
            progress(f"created {request['name']} ({probes[kind]})")
        for kind, guardrail_id in probes.items():
            wait_until_ready(bedrock, guardrail_id, name=PROBE_NAMES[kind])
        for model in models:
            settings = settings_for(base, model, args.region, probes)
            runs[model.name] = run_model(app, settings, questions, passages, texts, progress)
            summaries.append(summarise(model, runs[model.name]))
            progress(f"{model.name}: {summaries[-1]['cost_total']:.4f} USD")
            if args.out:
                try:
                    write(args.out, args, started, summaries, runs, passages)
                except OSError as problem:
                    # What is paid for stays in the report below; no further model is paid for
                    # when its result could not be saved either.
                    failed = f"{args.out} cannot be written: {problem}"
                    break
    except AwsProblem as problem:
        failed = problem
    finally:
        # A list, not a generator: a failed delete must not skip the others.
        deleted = all(
            [
                delete_reporting(bedrock, guardrail_id, progress, PROBE_NAMES[kind])
                for kind, guardrail_id in probes.items()
            ]
        )
    for line in table(summaries):
        print(line)
    total = sum(s["cost_total"] for s in summaries)
    print(f"\nModel and guardrail cost of the run: {total:.4f} USD (retrieval not counted).")
    if failed is not None:
        print(f"error: {failed}", file=sys.stderr)
        return 2
    failed_calls = sorted({name for summary in summaries for name in summary["errors"]})
    if failed_calls:
        print(f"error: failed calls, counted as wrong: {', '.join(failed_calls)}", file=sys.stderr)
    # The report stands; a failed call or a probe left behind still fails the run.
    return 0 if deleted and not failed_calls else 2


def write(
    path: Path,
    args: argparse.Namespace,
    started: str,
    summaries: Sequence[Mapping[str, Any]],
    runs: Mapping[str, Any],
    passages: Passages,
) -> None:
    """Save the run so far as JSON, with the passages ``--regrade`` needs."""
    path.write_text(
        json.dumps(
            {
                "started": started,
                "region": args.region,
                "retrieve_region": args.retrieve_region,
                "price_date": region_models(args.region).price_date,
                "summaries": summaries,
                "runs": runs,
                "passages": passages.found,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    sys.exit(main())
