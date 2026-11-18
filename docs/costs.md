# What it costs

Prices for `eu-central-1` (Frankfurt), read 06.10.2026 from the AWS Price
List API, the machine-readable source of the pricing pages
(`taug/infra/answer_models.py`). Order of magnitude, not a quote; the cost
per question below is measured.

**Idle:** cents per month. The documents bucket is a handful of megabytes,
and nothing else is running.

**Per question, measured.** Every answer carries `cost_usd` in its log line
and its response: the tokens of the answer model and of the topic model, and
the guardrail text units for masking and grounding, each times its price
(standard tier; the prices sit in the stack next to their source).

Bedrock charges the model call of an answer the guardrail withheld (user
guide, "How charges are calculated for Amazon Bedrock Guardrails", read
07.10.2026) and reports 0 tokens for it, in the response and in CloudWatch
alike (96 calls of Qwen3 235B in eu-central-1, 11 of them withheld,
07.10.2026). Since then such an answer carries `cost_usd: null`.

In `eu-central-1` on 07.10.2026, Qwen3 235B with the deployed function's
settings, the three question sets of `python -m svar.scripts.evaluate`: 0.0023 USD
per question on average for the English set, 0.0013 for German questions on
English documents and 0.0021 for German questions on German documents,
counting the questions with a known cost; 14 of the 88 were withheld by the
grounding check and carry `cost_usd: null`. Three runs of the topic check by
gpt-oss-120b, 957 checks, cost 0.30 USD. A rehearsal of a new workspace with
all its measurements cost 0.70 USD (at most 0.75), from the cost lines of
the measurements; see
[scripts/README.md](../scripts/README.md#rehearsal--a-new-workspace-set-up-and-measured-step-by-step).

In `eu-central-1` on 14.10.2026, after that day's deploy, the same three sets
with Qwen3 235B: 0.0024 USD per question on average for the English set,
0.0014 for German questions on English documents and 0.0021 for German
questions on German documents, counting the questions with a known cost; 12
of the 88 (3, 8 and 1) carry `cost_usd: null`, answers the answer guardrail
withheld. Three runs of the topic check by gpt-oss-120b, 957 checks,
cost 0.30 USD again.

In `eu-central-1` on 11.11.2026, the rehearsal after Taug's resources moved
into a stack of their own, the same three sets with Qwen3 235B: 0.0024 USD
per question on average for the English set, 0.0014 for German questions on
English documents and 0.0021 for German questions on German documents,
counting the questions with a known cost; 11 of the 88 (3, 7 and 1) carry
`cost_usd: null`. Three runs of the topic check by gpt-oss-120b, 957 checks,
0.30 USD. The craft firm's 21 questions of `control_eval`: 0.033 USD in all
for the 16 with a known cost, 0.0021 USD per question; the grounding check
withheld the other 5, which carry `cost_usd: null`.

**Ten passages since 28.09.2026** (`NUMBER_OF_RESULTS`, was 6; see
[decisions](decisions.md)). The grounding check reads every retrieved
passage as its reference, one unit per 1,000 characters, so its part grows
with the passages, not with the answer. Not in `cost_usd`: the question's
embedding, the vector query and the Lambda time.

The excerpts of the sources pass a second guardrail that masks personal
details, billed in the same PII units. A refused question costs nothing
there: it returns no sources.

gpt-oss-120b in the region checks the question, after its frame, before the
answer model is asked, and the answer without its citations after it; its
tokens are part of `cost_usd`. A question it refuses costs those tokens and
no call of the answer model, and no excerpts. An answer of citations alone
makes no topic check.

**The cap:** the web address is open to anyone who has it, and the answer
function serves the page too, so the stack reserves 10, 5 for questions and
5 more for a page load (`PAGE_LOAD_CONCURRENCY`), and that reservation is the
cap ([operating.md](operating.md#the-region)). Lambda turns the rest away
with HTTP 429 before any code runs. A request without a valid key never reaches the model.
That bounds the rate at which anything can arrive; what one key may spend in
total is its budget — ten questions and 48 hours. A leaked key is a leak of
ten questions, not of an open tap. A key on a plan may spend a month's quota:
6,000 questions on `betrieb` and 15,000 on `firma`.

**Counting it:** one conditional write per question, which is one write unit
on a table billed per request. A question on a plan is two, the refused demo
write and the count. Note that the permanent free tier of
25 write and 25 read units is for provisioned capacity, not for on-demand
tables; this table is on-demand, because a public URL has no rate to
provision for and a throttled counter would refuse honest questions.

**Filling it:** 40 documents of 2,000 words is roughly 110,000 tokens of
embedding (Titan Text Embeddings V2).

**Serving the page:** the answer function serves its five files, so a page
load is up to five calls of the function; they call no model and no
guardrail (`svar/app/page.py`).
