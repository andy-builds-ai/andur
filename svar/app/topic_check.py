"""The check of a question and of an answer for the denied topics, by the topic model in the
region."""

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import aws
import citations
import clients
import config
import errors
import llm
import log
import topic_model

#: What the topic check reads before the question and before the answer. Read alone, short
#: questions and answers about the business's own invoices were refused as tax questions ("Wie
#: viel Skonto darf ich abziehen, und bis wann muss ich dafür zahlen?") by the topic guardrail
#: this check replaced; after these words every harmless question and answer of the topic
#: probe's sets but one passed, and every one to refuse was refused, in three runs
#: (docs/decisions.md, 06.10.2026). The wording moves verdicts like a topic's definition does:
#: measure a change with `python -m svar.scripts.topic_probe`.
QUESTION_FRAME = "Customer to business: "
ANSWER_FRAME = "Answer from the business to a customer: "

#: The cause a topic check by the model gives when its reply names no topic and not "none":
#: empty, without the JSON, or a topic of its own. Such a first call is replaced by one more,
#: and the check refuses when that one gives no verdict either (`check_topics`).
UNREADABLE_VERDICT = (
    "the topic model gave no readable verdict",
    "das Themen-Modell hat kein lesbares Urteil geliefert",
)

#: What a failed topic check says, by the side it checked.
TOPIC_CHECK_FAILED = {
    "INPUT": (
        "The question could not be checked for the denied topics: {cause}.",
        "Die Frage konnte nicht auf die gesperrten Themen geprüft werden: {cause}.",
    ),
    "OUTPUT": (
        "The answer could not be checked for the denied topics: {cause}.",
        "Die Antwort konnte nicht auf die gesperrten Themen geprüft werden: {cause}.",
    ),
}


def question_for_topics(question: str) -> str:
    """The question as the topic check reads it: after `QUESTION_FRAME`."""
    return QUESTION_FRAME + question


def answer_for_topics(answer: str) -> str:
    """The answer as the topic check reads it: without its citations, after
    `ANSWER_FRAME`; empty when nothing but citations is left."""
    text = citations.without_citations(answer)
    return ANSWER_FRAME + text if text else ""


def check_question_topics(runtime: Any, settings: config.Settings, question: str) -> dict[str, Any]:
    """The denied topics the question asks about, and the topic model's tokens.

    Checked as INPUT after `QUESTION_FRAME`, before the model is asked. The answer guardrail on
    the model call reads no topic. A failed check answers nothing: the question is not asked
    unchecked.
    """
    return check_topics(runtime, settings, "INPUT", question_for_topics(question))


def check_answer_topics(runtime: Any, settings: config.Settings, answer: str) -> dict[str, Any]:
    """The denied topics the answer strays into, checked without its citations, and the topic
    model's tokens.

    The answer guardrail on the model call does not read the answer's topics: in the answer as
    the model writes it, `Private lives` withheld short harmless answers for their citations
    alone (docs/decisions.md, 04.10.2026). A text of citations alone has no words to check and
    costs no call. A failed check withholds the answer: it is not given unchecked.
    """
    text = answer_for_topics(answer)
    if not text:
        return {"topics": []}
    return check_topics(runtime, settings, "OUTPUT", text)


def topic_client(settings: config.Settings) -> Any:
    """The topic model's client."""
    return clients.client("topic-model", settings.region)


def topic_check_failed(source: str, cause: str, german_cause: str) -> errors.TopicCheckError:
    """The error of a topic check that cannot say the text is free of denied topics."""
    english, german = TOPIC_CHECK_FAILED[source]
    error = errors.TopicCheckError(english.format(cause=cause), german.format(cause=german_cause))
    # A verdict the model gave but nobody can read follows from the text it read, which the
    # caller writes: such a question is not given back (`refund_question`).
    error.unreadable = (cause, german_cause) == UNREADABLE_VERDICT
    return error


def check_topics(runtime: Any, settings: config.Settings, source: str, text: str) -> dict[str, Any]:
    """The denied topics the topic model finds in ``text``, read as ``source``, and its tokens.

    The first call decides when it names none. A topic it names is asked again by
    `topic_model.CONFIRMING_CALLS` calls at once, and stands unless every one of them names
    none; one that fails or gives no verdict keeps it. gpt-oss-120b refused a harmless question
    now and then, as a single call (07.10.2026). A first call that fails or gives no verdict is
    replaced by one more call, which decides alone: a topic it names stands, since the time of
    the check (`aws.TOPIC_MODEL_ROUNDS_PER_CHECK`) leaves no round to confirm it; if it fails or
    gives no verdict as well, the check refuses: what cannot be checked is not let through."""
    model_id = settings.topic_model_id
    tokens = {"input_tokens": 0, "output_tokens": 0}

    def ask(call: int) -> tuple[str | None, BaseException | None, dict[str, int]]:
        """One call: its verdict or None, its error if it failed, its tokens."""
        try:
            response = topic_model.ask(runtime, model_id, text)
        except aws.botocore_errors() as exc:
            log.log_event(
                "topic-model-failed", source=source, call=call, detail=aws.error_detail(exc)
            )
            return None, exc, {}
        named = topic_model.verdict(topic_model.reply_text(response))
        if named is None:
            log.log_event(
                "topic-model-unreadable",
                source=source,
                call=call,
                stop_reason=response.get("stopReason"),
            )
        return named, None, llm.read_usage(response)

    def counted(*calls: tuple[str | None, BaseException | None, dict[str, int]]) -> None:
        for _, _, usage in calls:
            for kind, count in usage.items():
                tokens[kind] += count

    def verdict(named: str) -> dict[str, Any]:
        return {
            "topics": [] if named == topic_model.NO_TOPIC else [f"topic:{named}"],
            "tokens": tokens,
        }

    first = ask(1)
    counted(first)
    named = first[0]
    if named is None:
        replacement = ask(2)
        counted(replacement)
        named, error, _ = replacement
        if named is not None:
            return verdict(named)
        if error is not None:
            cause = aws.error_cause(error)
            # A timeout of the first call stays in the chain: it may have run, and been paid for.
            raise topic_check_failed(source, cause, cause) from (
                first[1] if aws.timed_out(first[1]) else error
            )
        # From the first call's error, if it had one: a timeout there keeps the question
        # counted (`refund_question`), as it would have without the call in its place.
        raise topic_check_failed(source, *UNREADABLE_VERDICT) from first[1]
    if named == topic_model.NO_TOPIC or not topic_model.CONFIRMING_CALLS:
        return verdict(named)
    numbers = range(2, 2 + topic_model.CONFIRMING_CALLS)
    with ThreadPoolExecutor(max_workers=len(numbers)) as pool:
        confirming = list(pool.map(ask, numbers))
    counted(*confirming)
    if all(answer == topic_model.NO_TOPIC for answer, _, _ in confirming):
        log.log_event("topic-model-unconfirmed", source=source, topic=named)
        return verdict(topic_model.NO_TOPIC)
    return verdict(named)


def topic_tokens(*checks: Mapping[str, Any]) -> dict[str, int]:
    """The topic model's tokens over the checks of one request; 0 for a check of citations
    alone, which calls no model."""
    return {
        kind: sum(check.get("tokens", {}).get(kind, 0) for check in checks)
        for kind in ("input_tokens", "output_tokens")
    }
