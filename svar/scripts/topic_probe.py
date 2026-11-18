"""Measure the denied topics against a fixed question set, before a deploy or after it.

The region's topic model (taug/infra/answer_models.py) reads every question after the function's
`QUESTION_FRAME` and every answer without its citations after its `ANSWER_FRAME`, through the
function's own check (svar/scripts/topic_model_probe.py), the evaluation sets' questions as well;
the run creates and deletes nothing. With ``--deployed`` it measures the deployed function's
topic model, with ``--topic-model`` another model of the region's table.
"""

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from svar.app.denied_topics import DENIED_TOPICS
from svar.scripts.evaluate import import_app
from taug.infra.answer_models import region_models
from taug.scripts import utf8_output
from taug.scripts.aws import REGION

QUESTIONS_FILE = Path(__file__).with_name("topic-questions.json")
ANSWERS_FILE = Path(__file__).with_name("topic-answers.json")

#: What the topic check reads on each side, and the field of a set that holds it.
TEXT_FIELDS = {"INPUT": "question", "OUTPUT": "answer"}

LANGUAGES = ("en", "de")

TOPIC_NAMES = tuple(name for name, _, _ in DENIED_TOPICS)

#: The same topics and question gave different verdicts in two runs (02.10.2026): a
#: question counts as right only if it is right in every one of these runs.
DEFAULT_REPEATS = 3


def load_probe_questions(path: Path) -> list[dict[str, Any]]:
    """The question set, checked: a question to block names the denied topic it must hit, a
    harmless one names its subject, which is no denied topic."""
    return load_probe_set(path, TEXT_FIELDS["INPUT"])


def load_probe_answers(path: Path) -> list[dict[str, Any]]:
    """The answer set, checked the same way: an answer to withhold names the denied topic it
    strays into, a harmless one the subject it answers. An answer of citations alone is
    refused: the function never checks one, so it measures nothing."""
    answers = load_probe_set(path, TEXT_FIELDS["OUTPUT"])
    for answer in answers:
        if not judged_text("OUTPUT", answer["answer"]):
            raise ValueError(f"{answer['id']}: the answer is citations alone")
    return answers


def judged_text(source: str, text: str) -> str:
    """The text the topics judge, made by the function's own functions: a question after its
    frame (`question_for_topics`), an answer without its citations after its frame
    (`answer_for_topics`); empty for an answer of citations alone."""
    app = import_app()
    if source == "OUTPUT":
        return app.topic_check.answer_for_topics(text)
    return app.topic_check.question_for_topics(text)


def load_probe_set(path: Path, field: str) -> list[dict[str, Any]]:
    """A set of texts under ``field``, each with an id, a language, a topic and a verdict."""
    questions = json.loads(path.read_text(encoding="utf-8"))
    seen = set()
    for question in questions:
        name = question["id"]
        if name in seen:
            raise ValueError(f"{name}: the id is used twice")
        seen.add(name)
        if question["language"] not in LANGUAGES:
            raise ValueError(f"{name}: unknown language {question['language']!r}")
        if not isinstance(question["blocked"], bool):
            raise ValueError(f"{name}: blocked must be true or false")
        for key in ("topic", field):
            if not (isinstance(question.get(key), str) and question[key].strip()):
                raise ValueError(f"{name}: {key} must be a non-empty text")
        if question["blocked"] != (question["topic"] in TOPIC_NAMES):
            raise ValueError(
                f"{name}: a text to block names one of {list(TOPIC_NAMES)}, "
                "a harmless one names a subject that is none of them"
            )
    return questions


def always_blocked(row: Mapping[str, Any]) -> bool:
    """Blocked in every run."""
    return row["blocked_runs"] == row["runs"]


def ever_blocked(row: Mapping[str, Any]) -> bool:
    """Blocked in at least one run."""
    return row["blocked_runs"] > 0


def missed(row: Mapping[str, Any]) -> bool:
    """A question to block that passed in any run, or a harmless one blocked in any run."""
    return not always_blocked(row) if row["expected"] else ever_blocked(row)


def summarise(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Slips per denied topic and language, false blocks per firing topic and language. A
    question counts as right only if it was right in every run."""
    others = sorted({r["topic"] for r in rows if r["expected"]} - set(TOPIC_NAMES))
    slips = {
        f"{topic} ({language})": {
            "blocked": sum(always_blocked(r) for r in group),
            "questions": len(group),
            "slipped": [r["id"] for r in group if not always_blocked(r)],
        }
        for topic in (*TOPIC_NAMES, *others)
        for language in LANGUAGES
        if (
            group := [
                r
                for r in rows
                if r["expected"] and r["topic"] == topic and r["language"] == language
            ]
        )
    }
    harmless = {
        language: {
            "blocked": sum(ever_blocked(r) for r in group),
            "questions": len(group),
        }
        for language in LANGUAGES
        if (group := [r for r in rows if not r["expected"] and r["language"] == language])
    }
    false_blocks: dict[str, list[str]] = {}
    for row in rows:
        if ever_blocked(row) and not row["expected"]:
            for topic in row["topics"] or ["(no topic)"]:
                false_blocks.setdefault(f"{topic} ({row['language']})", []).append(row["id"])
    return {
        "runs": max((r["runs"] for r in rows), default=0),
        "slips": slips,
        "harmless": harmless,
        "false_blocks": false_blocks,
        "false_blocked": sum(missed(r) for r in rows if not r["expected"]),
        "slipped": sum(missed(r) for r in rows if r["expected"]),
        "other_topic": [
            r["id"]
            for r in rows
            if r["expected"]
            and ever_blocked(r)
            and r["topic"] in TOPIC_NAMES
            and r["topic"] not in r["topics"]
        ],
    }


def report(summary: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """The run as Markdown: blocked per topic and language, false blocks, every miss."""
    lines = [
        f"Runs per text: {summary['runs']}. Right means right in every run.",
        "",
        "| Denied topic (language) | Blocked | Slipped through |",
        "|---|---|---|",
        *(
            f"| {key} | {s['blocked']}/{s['questions']} | {', '.join(s['slipped']) or '-'} |"
            for key, s in summary["slips"].items()
        ),
        "",
        "| Harmless (language) | Blocked by mistake |",
        "|---|---|",
        *(
            f"| {language} | {h['blocked']}/{h['questions']} |"
            for language, h in summary["harmless"].items()
        ),
        *(f"- by {key}: {', '.join(ids)}" for key, ids in summary["false_blocks"].items()),
    ]
    misses = [r for r in rows if missed(r)]
    if misses:
        lines += ["", "| Id | Expected | Blocked in | By | Text |", "|---|---|---|---|---|"]
        lines += [
            f"| {r['id']} | {'blocked' if r['expected'] else 'answered'} "
            f"| {r['blocked_runs']}/{r['runs']} | {', '.join(r['topics']) or '-'} "
            f"| {r['text']} |"
            for r in misses
        ]
    if summary["other_topic"]:
        lines += ["", f"Blocked, but under another topic: {', '.join(summary['other_topic'])}"]
    return lines


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """The command line: what to measure, how often, how many checks at once and the region."""
    parser = argparse.ArgumentParser(
        prog="python -m svar.scripts.topic_probe",
        description="Measure DENIED_TOPICS against the topic question set.",
    )
    parser.add_argument(
        "--deployed",
        action="store_true",
        help="measure the deployed function's topic model instead of the region's own",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=DEFAULT_REPEATS,
        help=f"ask every question this many times (default: {DEFAULT_REPEATS})",
    )
    parser.add_argument(
        "--workers",
        type=int,
        help="topic checks at once (default: 4); 1 asks one after the other. The result and "
        "its order do not depend on it, and a throttled check is repeated after a wait.",
    )
    parser.add_argument("--questions", type=Path, default=QUESTIONS_FILE)
    parser.add_argument("--answers", type=Path, default=ANSWERS_FILE)
    parser.add_argument("--out", type=Path, help="write every question's result as JSON here")
    parser.add_argument("--region", default=REGION, help=f"default: {REGION}")
    parser.add_argument(
        "--topic-model",
        help="this model of the region's table instead of the region's own topic model",
    )
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    if args.workers is not None and args.workers < 1:
        parser.error("--workers must be at least 1")
    if args.topic_model and args.deployed:
        parser.error("--deployed measures the deployed function's topic model, not --topic-model")
    return args


def main(argv: list[str] | None = None) -> int:
    """Measure the denied topics on the questions and answers with the topic model and print the
    report (`topic_model_probe`)."""
    utf8_output()
    args = parse_args(argv)
    try:
        topic_model_id = args.topic_model or region_models(args.region).topic_model_id
    except KeyError as problem:
        print(f"error: {problem.args[0]}", file=sys.stderr)
        return 2
    from svar.scripts import topic_model_probe

    return topic_model_probe.main(args, topic_model_id)


if __name__ == "__main__":
    sys.exit(main())
