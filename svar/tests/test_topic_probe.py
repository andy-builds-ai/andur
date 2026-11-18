"""The topic probe and its question set, without AWS: what it creates, measures and deletes."""

import json
from pathlib import Path
from typing import Any

import pytest

from svar.app.denied_topics import DENIED_TOPICS
from svar.scripts import evaluate, topic_probe

GERMAN_EVALUATION = evaluate.QUESTIONS_FILE.with_name("evaluation-questions-de.json")

#: The measurement of 01.10.2026 asked two questions about a company whose documents are not in
#: the repository; both were refused after the Standard tier was deployed.
KALVENSTETT_QUESTIONS = {
    "Was kostet die jährliche Wartung meiner Gas-Brennwertheizung, wenn ich einen "
    "Wartungsvertrag habe?",
    "Wie lange habe ich Gewährleistung auf eine Reparatur?",
}


@pytest.fixture(scope="module")
def questions() -> list[dict[str, Any]]:
    return topic_probe.load_probe_questions(topic_probe.QUESTIONS_FILE)


# --- The question set ----------------------------------------------------------------------


def test_every_refusal_of_both_evaluation_sets_must_be_blocked(
    questions: list[dict[str, Any]],
) -> None:
    """The 16 German refusals and the 4 English ones of the measurement of 01.10.2026."""
    by_text = {q["question"]: q for q in questions}
    evaluation = [
        *evaluate.load_questions(evaluate.QUESTIONS_FILE),
        *evaluate.load_questions(GERMAN_EVALUATION),
    ]
    refusals = [q for q in evaluation if q["kind"] == "refuse"]
    assert len(refusals) == 20
    for question in refusals:
        assert by_text[question["question"]]["blocked"] is True, question["id"]


def test_every_answerable_question_of_both_evaluation_sets_must_pass(
    questions: list[dict[str, Any]],
) -> None:
    """22 English and 12 German, and the two Kalvenstett questions of the measurement."""
    by_text = {q["question"]: q for q in questions}
    evaluation = [
        *evaluate.load_questions(evaluate.QUESTIONS_FILE),
        *evaluate.load_questions(GERMAN_EVALUATION),
    ]
    answerable = [q for q in evaluation if q["kind"] == "answer"]
    assert len(answerable) == 34
    for question in answerable:
        found = by_text[question["question"]]
        assert found["blocked"] is False, question["id"]
        assert found["language"] == question.get("language", "en")
    for text in KALVENSTETT_QUESTIONS:
        assert by_text[text]["blocked"] is False


def test_the_set_asks_what_a_trade_business_is_asked_about_its_own_documents(
    questions: list[dict[str, Any]],
) -> None:
    """At least 15 new harmless questions in both languages, on the subjects closest to the
    refused topics: prices, quotes, payment, warranty, maintenance contracts, delivery and
    opening hours."""
    evaluation_texts = {
        q["question"]
        for path in (evaluate.QUESTIONS_FILE, GERMAN_EVALUATION)
        for q in evaluate.load_questions(path)
    }
    new = [
        q
        for q in questions
        if not q["blocked"]
        and q["question"] not in evaluation_texts
        and q["question"] not in KALVENSTETT_QUESTIONS
    ]
    assert len(new) >= 15
    assert {q["language"] for q in new} == {"en", "de"}
    subjects = {q["topic"] for q in new}
    assert {
        "prices",
        "quote",
        "payment",
        "warranty",
        "maintenance contract",
        "delivery",
        "opening hours",
    } <= subjects


def test_every_denied_topic_has_refusals_in_both_languages(
    questions: list[dict[str, Any]],
) -> None:
    for name, _, _ in DENIED_TOPICS:
        for language in topic_probe.LANGUAGES:
            asked = [
                q
                for q in questions
                if q["blocked"] and (q["topic"], q["language"]) == (name, language)
            ]
            assert len(asked) >= 4, (name, language)


def test_no_question_of_the_set_is_an_example_of_a_topic(
    questions: list[dict[str, Any]],
) -> None:
    """A question that is also an example would measure the example, not the topic."""
    examples = {example for _, _, topic_examples in DENIED_TOPICS for example in topic_examples}
    assert not examples & {q["question"] for q in questions}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"language": "fr"}, "unknown language"),
        ({"blocked": "yes"}, "true or false"),
        ({"question": " "}, "non-empty"),
        ({"topic": "prices"}, "names one of"),
        ({"blocked": False}, "names one of"),
    ],
)
def test_a_malformed_question_is_rejected(
    tmp_path: Path, change: dict[str, Any], message: str
) -> None:
    question = {
        "id": "x1",
        "language": "en",
        "blocked": True,
        "topic": DENIED_TOPICS[0][0],
        "question": "Can I sue them?",
    }
    path = tmp_path / "questions.json"
    path.write_text(json.dumps([{**question, **change}]), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        topic_probe.load_probe_questions(path)


def test_an_id_used_twice_is_rejected(tmp_path: Path) -> None:
    question = {
        "id": "x1",
        "language": "de",
        "blocked": False,
        "topic": "prices",
        "question": "Was kostet das?",
    }
    path = tmp_path / "questions.json"
    path.write_text(json.dumps([question, question]), encoding="utf-8")
    with pytest.raises(ValueError, match="used twice"):
        topic_probe.load_probe_questions(path)


# --- The answer set ------------------------------------------------------------------------


@pytest.fixture(scope="module")
def answers() -> list[dict[str, Any]]:
    return topic_probe.load_probe_answers(topic_probe.ANSWERS_FILE)


def test_the_answers_private_lives_withheld_are_in_the_set_as_harmless(
    answers: list[dict[str, Any]],
) -> None:
    """The model's answers to q15 and de11 that `Private lives` withheld on the way out, in
    the rebuild of 04.10.2026, and the forms of the same answers it let through."""
    by_text = {a["answer"]: a for a in answers}
    for text in (
        "Warned [1][2][4]",
        "Warned [1][2][4][6]",
        "Die Garantie gilt für 5 Jahre [1][4].",
        "Die Garantie gilt für 5 Jahre [4].",
    ):
        assert by_text[text]["blocked"] is False, text


def test_every_answer_of_the_evaluation_sets_is_for_an_answerable_question(
    answers: list[dict[str, Any]],
) -> None:
    """An answer named after an evaluation question answers that question, in its language."""
    evaluation = {
        q["id"]: q
        for path in (evaluate.QUESTIONS_FILE, GERMAN_EVALUATION)
        for q in evaluate.load_questions(path)
    }
    named = [a for a in answers if a["id"].split("a")[0] in evaluation]
    assert len(named) >= 60
    for answer in named:
        question = evaluation[answer["id"].split("a")[0]]
        assert (question["kind"], answer["blocked"]) == ("answer", False), answer["id"]
        assert answer["language"] == question.get("language", "en"), answer["id"]


def test_every_denied_topic_has_answers_to_withhold_in_both_languages(
    answers: list[dict[str, Any]],
) -> None:
    for name, _, _ in DENIED_TOPICS:
        for language in topic_probe.LANGUAGES:
            written = [
                a
                for a in answers
                if a["blocked"] and (a["topic"], a["language"]) == (name, language)
            ]
            assert len(written) >= 4, (name, language)


def test_every_answer_is_measured_without_its_citations_as_the_function_checks_it(
    answers: list[dict[str, Any]],
) -> None:
    """The probe cuts the citations with the handler's own function, not a copy of it."""
    app = evaluate.import_app()
    for answer in answers:
        judged = topic_probe.judged_text("OUTPUT", answer["answer"])
        assert judged == app.topic_check.answer_for_topics(answer["answer"]), answer["id"]
        assert "[" not in judged, answer["id"]
    by_id = {a["id"]: a for a in answers}
    assert topic_probe.judged_text("OUTPUT", by_id["q15a1"]["answer"]) == (
        "Answer from the business to a customer: Warned"
    )
    assert topic_probe.judged_text("INPUT", "Was kostet [1]?") == (
        "Customer to business: Was kostet [1]?"
    )


def test_an_answer_of_citations_alone_is_rejected(tmp_path: Path) -> None:
    """The function never checks one, so it would measure nothing."""
    path = tmp_path / "answers.json"
    path.write_text(
        json.dumps(
            [{"id": "x1", "language": "en", "blocked": False, "topic": "prices", "answer": "[1]"}]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="citations alone"):
        topic_probe.load_probe_answers(path)


def test_an_answer_without_its_text_is_rejected(tmp_path: Path) -> None:
    """The answer set reads ``answer``; a question in its place is no answer."""
    path = tmp_path / "answers.json"
    path.write_text(
        json.dumps(
            [{"id": "x1", "language": "en", "blocked": False, "topic": "prices", "question": "?"}]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="answer must be a non-empty text"):
        topic_probe.load_probe_answers(path)


# --- Counting ------------------------------------------------------------------------------


def row(
    name: str, language: str, topic: str, expected: bool, blocked_runs: int, topics: list[str]
) -> dict[str, Any]:
    """One measured text after three runs, as `topic_model_probe.measure` gives it."""
    return {
        "id": name,
        "language": language,
        "topic": topic,
        "source": "INPUT",
        "text": f"Customer to business: {name}?",
        "expected": expected,
        "runs": 3,
        "blocked_runs": blocked_runs,
        "topics": topics,
    }


def test_a_question_is_right_only_if_it_is_right_in_every_run() -> None:
    """A refusal that passes once has slipped through; a harmless question blocked once is a
    false block — the visitor who got it saw a refusal."""
    rows = [
        row("r1", "en", "Legal action", True, 3, ["Legal action"]),
        row("r2", "de", "Legal action", True, 2, ["Legal action"]),
        row("h1", "de", "warranty", False, 1, ["Legal action"]),
        row("h2", "en", "prices", False, 0, []),
    ]
    summary = topic_probe.summarise(rows)
    assert summary["runs"] == 3
    assert summary["slips"]["Legal action (en)"] == {"blocked": 1, "questions": 1, "slipped": []}
    assert summary["slips"]["Legal action (de)"] == {
        "blocked": 0,
        "questions": 1,
        "slipped": ["r2"],
    }
    assert summary["harmless"] == {
        "en": {"blocked": 0, "questions": 1},
        "de": {"blocked": 1, "questions": 1},
    }
    assert summary["false_blocks"] == {"Legal action (de)": ["h1"]}
    assert (summary["slipped"], summary["false_blocked"]) == (1, 1)
    lines = topic_probe.report(summary, rows)
    assert "| r2 | blocked | 2/3 | Legal action | Customer to business: r2? |" in lines
    assert "| h1 | answered | 1/3 | Legal action | Customer to business: h1? |" in lines


def test_a_refusal_under_another_topic_counts_as_blocked_and_is_named() -> None:
    summary = topic_probe.summarise([row("r2", "de", "Tax returns", True, 3, ["Legal action"])])
    assert summary["slips"]["Tax returns (de)"]["blocked"] == 1
    assert summary["other_topic"] == ["r2"]


@pytest.mark.parametrize("argv", [["--repeat", "0"], ["--workers", "0"]])
def test_repeat_and_workers_must_be_at_least_one(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        topic_probe.parse_args(argv)
