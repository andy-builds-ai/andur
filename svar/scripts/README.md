# Svar's scripts — asking and measuring

Five commands, all run from the repository root. What the scripts need (install, AWS login, the deployed stacks): [taug/scripts/README.md](../../taug/scripts/README.md#what-they-need).

| Command | Does |
|---|---|
| `python -m svar.scripts.ask` | Asks the deployed answer function a question and prints the answer with its sources. |
| `python -m svar.scripts.evaluate` | Asks 30 fixed questions of each answer model the way the function does and prints hit rate, citations and cost. |
| `python -m svar.scripts.topic_probe` | Measures the denied topics against 132 fixed questions and 119 fixed answers, each after its frame, the answers without their citations: which harmless ones they block and which they should block slip through. |
| `python -m svar.scripts.grounding_probe` | Measures the grounding check against 142 fixed answers, right and made up, German and English, on English and German documents: which right ones it withholds and which made-up ones it lets through. |
| `python -m svar.scripts.control_eval` | Asks 42 fixed questions of every model of `FRANKFURT_MODELS` the way the function does, with probe guardrails in the model's region: whether it declines what the documents do not say, adds figures of its own, cites the right document, answers German in German. |

Svar runs in `eu-central-1` (Frankfurt) alone. The scripts take `--region`
and without it aim at `eu-central-1`, the default region in
`taug/infra/answer_models.py`; a region without a table there is refused.

## ask — the proof that answering works

```bash
python -m svar.scripts.ask "How do I export project data?" --workspace quillmoor --region eu-central-1
python -m svar.scripts.ask "How do I export project data?" --workspace pellwick --region eu-central-1
python -m svar.scripts.ask "How will the bulk export of all projects work?" --workspace quillmoor --json --region eu-central-1
```

It sends what any client sends, the page included: a `POST` to
`<WebUrl>/ask` with the workspace key in `X-Andur-Key`. `WebUrl` is the
function's own URL — see *One address for everything* in
[architecture](../../docs/architecture.md#one-address-for-everything).

The script reads `WebUrl` from Svar's stack, `AndurStack` (`--stack-name`),
and the key's path, `ApiKeyParameterPath`, from Taug's, `TaugStack`
(`--taug-stack-name`). The key comes from Parameter Store
(`/andur/api-keys/<workspace>`, decrypted) and is never printed. To use a key
you already have, set `ANDUR_API_KEY`; then the script does not read
`TaugStack`, and with `--url` as well, it needs no AWS access at all. Space and a line
end around the key are dropped, as the function drops them; a key that still
cannot go into a header is refused without being shown (exit code 2).

Output: the answer, the layers of the sources it cites, one line per source
— number, `cited` or blank, layer, score, words, workspace and file — and
then model, tokens in and out, and latency. A line `Layers cited: inbox` is
the warning: the answer rests on a document nobody has confirmed.

`--json` prints the function's response unchanged; its fields are described
in [svar/app/README.md](../app/README.md).

`--language de` (or `en`) puts `"language"` into the request, as the page
does, so the function's own sentences — "not in the sources", a refusal by the
guardrail, an error — come back in German. Without it the request carries no
language and they are English.

| Exit code | Meaning |
|---|---|
| 0 | Answered. |
| 1 | The function refused or failed; the status and its reason are printed (401 wrong key, key of another workspace or a workspace without a key, 402 demo budget used up, 429 with a sentence: the plan's quota for the month or the day reached, 400 bad question, 502 Bedrock failed, 503 too many unknown workspaces checked at once, try again in a few seconds). |
| 2 | An AWS step or the connection failed: login, stack, missing key parameter, network. |

## evaluate — which model answers best

```bash
python -m svar.scripts.evaluate --region eu-central-1                # every model of the region's table
python -m svar.scripts.evaluate --region eu-central-1 --model "Qwen3 235B A22B 2507" --out results.json
python -m svar.scripts.evaluate --region eu-central-1 --model "Qwen3 235B A22B 2507" --questions svar/scripts/evaluation-questions-de.json
```

It asks the 30 questions in `evaluation-questions.json` — 22 that the
documents answer, 4 that the guardrail must refuse, 4 that no document
answers — of every model in the table of `--region` (`REGIONS` in
`taug/infra/answer_models.py`), through `answer_question` in
`svar/app/answering.py` with the deployed function's own settings. Only the model,
its token prices and the wording of its system prompt (`prompt_of` in
`svar/infra/answer_prompts.py`, the function's `ANSWER_PROMPT`) change, so a run
before the deploy that brings `ANSWER_PROMPT` still asks each model with its
own wording; see *Evaluation* in
[docs/decisions.md](../../docs/decisions.md). No key and no budget are used.
Only models that run in the region or behind an EU profile are allowed; the
script refuses any other.

`evaluation-questions-de.json` asks 32 questions in German about the same
documents — 12 that they answer, 16 that the guardrail must refuse, 4 that
no document answers. A question with `"language": "de"` is asked the way the
German page asks it: the prompt and the fixed sentences are German. Its
facts keep the document's spelling first and add German ones ("60 Minuten",
"einmal im Jahr"). Sixteen refusals, so that German refusals are measured
too, some in everyday words ("Krieg ich die neue
Haustür irgendwie beim Finanzamt durch?").

`evaluation-questions-kalvenstett.json` asks 26 questions in German about
German documents: those of the workspace `kalvenstett`, a made-up heating and
plumbing business whose documents are not in the repository, so the
workspace has to be ingested for it. 20 that the documents answer, each fact
a number, a date or a word the answer can be checked for ("68 Euro", "30.
September", "EC-Karte"), spread over all seven documents; 3 that the
guardrail must refuse; 3 that no document answers. Its facts are German
spellings only, since the documents are German. `documents` names the
document, the part of a section's file name before `--`: four of the ten
sections of 06.10.2026 still carry names from before umlauts were spelled out
(`erreichbarkeit-und-notdienst--01-b-rozeiten.md`), and the document part is
the same in both.

A question is **right** when the answer contains every expected fact, each
standing on its own ("12" is not found in "120"), or, for the other eight,
when Svar declines, in German or English. **Blocked by guardrail** counts
the refusals the guardrail itself blocked: a model that answers "the sources
do not answer this" is refused right, but only a block proves the guardrail
refuses the topic. **Cites the document** means a
cited source comes from one of the documents that carry the answer; five
facts stand in more than one, and the question lists them all. A model call
that AWS documents as not run (throttled, unavailable, model not ready) is
made once more by the function itself; a question that still comes back busy is asked again by the script
after 10, 30 and 60 seconds. **Rejected on the first try** counts the
questions whose first model call AWS turned away, whether the second call
saved it or not; **Right on the first try** counts the questions answered
right without the script asking again — what the page would have shown.

The last results with Qwen3 235B in Frankfurt are in
[svar/README.md](../README.md#measured); what a question cost, in
[docs/costs.md](../../docs/costs.md).

A question that ends in an error is counted as wrong and in the column
*Errors*, and it fails the run after the table: exit code 2, as for a failed
AWS step before the first question. `--out` is opened before anything is
paid for, and written after every model.

## topic_probe — what the denied topics refuse, before a deploy

```bash
python -m svar.scripts.topic_probe --region eu-central-1 --out run.json             # three runs
python -m svar.scripts.topic_probe --region eu-central-1 --repeat 1 --topic-model qwen.qwen3-235b-a22b-2507-v1:0
python -m svar.scripts.topic_probe --region eu-central-1 --deployed --out run.json  # the deployed function's
```

It measures the region's topic model, gpt-oss-120b in eu-central-1 since
07.10.2026 (*Guardrails and cost* in
[docs/decisions.md](../../docs/decisions.md);
`svar/scripts/topic_model_probe.py`). `--topic-model` measures another model
of the region's table with the same prompt and rules, as long as the model
has a price there. `--deployed` measures the topic model the deployed
function names; it does not go with `--topic-model`.

Every text goes through `check_topics` of the function, with its client and
its timeouts, so the prompt, the calls that replace a failed one or confirm
a topic, and the verdict rules are the ones a question meets. A check that fails or gives no readable verdict counts
as blocked and is listed apart. Besides the two topic sets it asks the 68
questions of `evaluation-questions-control.json` and
`evaluation-questions-kalvenstett.json`; their 11 advice questions are to be
refused. It creates and deletes nothing; three runs of 319 checks with
gpt-oss-120b cost 0.30 USD on 07.10.2026.

Every change to `DENIED_TOPICS` in `svar/app/denied_topics.py`, to the topic
model's prompt in `svar/app/topic_model.py`, and to the frames `QUESTION_FRAME`
and `ANSWER_FRAME` in `svar/app/topic_check.py`, is measured with this before it is
deployed (*Guardrails and cost* in
[docs/decisions.md](../../docs/decisions.md)). Every question of
`topic-questions.json` goes as `INPUT` after the question's frame, and every
answer of `topic-answers.json` as `OUTPUT` without its citations after the
answer's frame, both made by the handler's own `question_for_topics` and
`answer_for_topics`. The two sides are measured apart because they are read
apart: the function checks the question alone before the model is asked, and
the answer alone after it, never with the sources.

`topic-questions.json` holds 132 questions, each with `"blocked"` (whether
the check must refuse it) and `"topic"`: the denied topic it must hit,
or for a harmless question its subject. 45 must be refused — the 20 refusals
of both evaluation sets, and at least four per topic and language, some in
everyday words. 87 must pass — the 34 answerable questions of both
evaluation sets, two about a company whose documents are not in the
repository, and 51 that a trade business is asked about its own documents:
prices, quotes, payment terms, VAT on its prices, warranty, maintenance
contracts, delivery, opening hours; 23 of them (hd20 to hd32, he12 to he21,
06.10.2026) about its invoices: discount for paying early, when to pay, late
payment, reminders, instalments, deposits, the VAT rate. No question is word for word an example
of a topic, or the run would measure the example. Six German refusals of the
evaluation set (de19, de20, de23, de25, de27, de28) are close translations of
English examples, so for them the run partly measures the examples too; the
everyday refusals added per topic are not.

`topic-answers.json` holds 119 answers in the same form, with `"answer"` in
place of `"question"`. 87 must pass: 60 that Nova Pro wrote to answerable
questions of both evaluation sets in the runs of 01.10. and 03.10.2026, and 4
it wrote to q15 and de11 in the rebuild of 04.10.2026 (the id names the
question, `q15a1` is the first answer to q15), short ones with their
citations included, since a citation changed the verdict as long as the
topics read it; and 23 written about the subjects closest to the topics, such
as a contract's notice period, a call-out charge, or a discount you may
deduct from the invoice (ha05 to ha11 and hd06 to hd12, 06.10.2026). 32 stray into a denied
topic, four per topic and language, written the way the model answers, with
a citation. The run takes the citations out before it asks, as the function
does, so every answer is measured as the topic check reads it; an answer
of citations alone is refused when the set is read, since the function never
checks one.

Every text is asked three times (`--repeat`), and it counts as right only
if it was right every time: the same check can give one text two different
verdicts in two runs. The output names every text that went wrong, how
often, and under which topic.

`--workers` sets how many checks run at once (default 4; `--workers 1` asks
one after the other). `measure()` called from Python has the same default;
pass `workers=1` to ask one after the other. The checks are counted and
reported in the order they are listed, so the report, the rows in `--out`
and the `run …` lines on stderr do not depend on it; only the time does. A
check AWS throttles (`ThrottlingException`) is asked again after a wait that
doubles from one second to twenty, up to 8 attempts; it is not counted as an
error and not as a text that passed.

The last results, three runs in Frankfurt, are in
[svar/README.md](../README.md#measured).

## grounding_probe — what the grounding check withholds, in German too

```bash
python -m svar.scripts.grounding_probe --region eu-central-1 --out grounding.json
```

The grounding check reads English, French and Spanish only, the user guide
says, and Svar answers in German too. This measures what that does, on right
answers and on made-up ones, before a change to the check or the prompt is
deployed. The script creates a guardrail of its own, `andur-grounding-probe`,
a copy of the answer guardrail as the stack would deploy it — the masking,
grounding at the region's threshold (0.8 in eu-central-1) and relevance at
0.7, and the messages, in the region and without a guardrail profile; a test
holds it against the synthesised template. For every answer of `grounding-answers.json` it
retrieves the passages of its question from the deployed knowledge base and
builds them into the sources text the way the function does
(`retrieve_passages`, `keep_workspace`, `build_sources`), and sends sources,
question and answer through `ApplyGuardrail` as OUTPUT in the three blocks the
model call gives the guardrail: `grounding_source`, `query`, `guard_content`.
Each answer is checked once. The probe is deleted at the end, also when the run
fails; a guardrail of that name left by a killed run stops the next one and
is named by its id; delete it with `aws bedrock delete-guardrail`. It never
deletes a guardrail of another name.

A German answer the check withholds is checked once more in English:
translated, with its question, by the answer model through the probe
(`TRANSLATION_PROMPT`), and the translation checked on the same sources. The
function does not do this. It is the way measured and not built on
06.10.2026 (*Guardrails and cost* in [docs/decisions.md](../../docs/decisions.md)),
and the probe keeps measuring it, so the numbers can be taken again when AWS
or the prompt changes. A translation that is not in the form asked for counts
as withheld, and the report counts such translations per group, so a made-up
answer withheld for that alone is not taken for one the check withheld. A call
that fails, as written or in English, is named, its answer counted as
withheld, and it fails the run after the report.
Four answers are measured at once (`--workers`).

`grounding-answers.json` holds 142 answers, each with its workspace,
question, `"language"`, `"source_language"` (German for `kalvenstett`,
English for the rest), `"grounded"` and `"kind"`: `right`, or made up as a
`wrong-value` (a number or date the documents do not give), an `added-claim`
(a right fact and an invented one) or `invented`. Every question is an answer
question of an evaluation set. 38 are right: 19 German answers to English
documents, Nova Pro's own from the runs of 04.10. and 06.10.2026 (de11 in its
three forms, de03 as the bare "25 % [1]"), and 19 to the German documents of
`kalvenstett`, Nova Pro's own of 06.10.2026. 104 are made up, written the way
the model answers, with a citation: 34 German ones to English documents, each
with its English twin, the same claim in English, so the German verdicts can
be held against the English ones; and 36 German ones to German documents.
Every invented claim was searched for in the documents first; one wrong value
is a number the documents give for something else ("10 Jahre" for de11, where
the timber guarantee is 10 years), as the model could write it.

The last results at 0.8 in Frankfurt are in
[svar/README.md](../README.md#measured).

`grounding-partial-answers.json` holds 16 answers to the partial questions of
the control set (c09, c10, c11, c24, c32), taken with `--answers`: the right
part alone, the right part with a sentence naming what the sources do not
answer, the right part with the fixed "not in the sources" sentence, c10 and
c09 the way Qwen3 235B wrote them (both sentences), and for every question
but c24 the right part with an invented rest, as an `added-claim`. It
measures which form of a partial answer the check passes, the form the prompt
asks for since 07.10.2026 (*Guardrails and cost* in
[docs/decisions.md](../../docs/decisions.md)), and that an invented rest is still
withheld. The report has no group for right English answers, so c32's right
ones are only in the progress lines and in `--out`; its made-up one counts in
"made up, en on en sources".

Measured on 07.10.2026 (0.022 USD) through a copy of the Frankfurt answer
guardrail at 0.8: every right part alone passed but c24's (0.33, a German
answer to English documents); the right part with a sentence naming the rest
passed for c10 (0.82), c09, c11 and c32 (0.95 to 0.97); c10 with both that
sentence and the fixed one was withheld (0.78), with the fixed one alone it
passed (0.81). Three of the four invented rests were withheld (0.03 to 0.17);
c32's "On a late balance we charge 8 % interest a year [1]." passed at 0.92.


## control_eval — which model stays with its sources, in Frankfurt

```bash
python -m svar.scripts.control_eval --out results.json                  # every model in FRANKFURT_MODELS
python -m svar.scripts.control_eval --model "Nova Pro" --only c12       # one model, one question
python -m svar.scripts.control_eval --workspace kalvenstett             # only one workspace's questions
python -m svar.scripts.control_eval --regrade results.json              # mark a saved run again, no AWS call
```

`evaluate` asks whether an answer is right. `control_eval` asks how well a
model keeps to its sources, on `evaluation-questions-control.json`: 42
questions, German about Kalvenstett's German documents and Pellwick's English
ones, a few in English. 16 the documents answer; 6 *partial* ones whose second
half they do not (the price of a new boiler, the life of a bathroom); 12 they
do not answer at all although an answer lies close (funding for a heat pump,
the VAT rate in Pellwick's prices); 8 advice questions. `--workspace` asks
only the questions of one workspace and ends with 2 when it has none.

Every question goes through `answer_question` like in `evaluate`, but the
model and the guardrails run in `--region` (default `eu-central-1`): the
script creates two probe guardrails there, `andur-control-answer-probe` and
`andur-control-excerpt-probe`, copies of the stack's answer and excerpt
guardrails, and deletes them after the run, also after a failure. The
region's topic model checks the topics, as the function does.
Retrieval stays with the deployed knowledge base in `--retrieve-region`
(default `eu-central-1` as well), once per question, and every model gets the same passages; they are saved
with the run. Prices and their date come from the table of `--region` in
`taug/infra/answer_models.py` (`REGIONS`). The wording of the system prompt comes
from `prompt_of` in `svar/infra/answer_prompts.py`, by the model's name.

`--regrade` marks every model and question of a saved run again and refuses
`--model`, `--only` and `--workspace`; it refuses a saved run from a region
that has no table in `taug/infra/answer_models.py` and rewrites nothing then.

The marks, each on what the model wrote before any guardrail replaced it
(Bedrock keeps that text in the guardrail trace):

- **Right** — as in `evaluate`, on what the function returns.
- **Stays with the sources** — an unanswerable question declined with the
  fixed sentence and nothing added; a partial one answered with its fact and
  nothing added.
- **Adds** — the answer holds a number that stands in none of the passages
  the model was given and not in the question, or one of the question's
  `forbidden` spellings (a tax rule, a medicine, a funding programme).
  "1,450" and "1.450" are the same number, a date gives day, month and year,
  the passages also count with their number words ("five", "März"); 0 and 1
  never count, and neither do the line numbers gpt-oss writes into its
  citations ("【2†L9-L12】"). The rule is mechanical: it sees numbers and the
  listed spellings, not an invented sentence without them.
- **Cites the document** — a cited source comes from a document that carries
  the answer; **cites another document** — one does not.
- **German in German** — the commoner words of the answer are German; an
  answer too short to tell ("179 Euro [1]") counts for neither.
- **Refused** — an advice question blocked by a guardrail or declined by the
  model; in brackets the ones the topic check itself blocked. **Own advice**
  lists the advice questions where the model wrote advice with a number or a
  forbidden spelling of its own, whether a guardrail withheld it or not.

A question that ends in an error is named, counted as wrong, and fails the
run after the table (exit code 2), like a probe guardrail left behind. A run
that fails before its first model has finished leaves an earlier `--out` file
as it is.
