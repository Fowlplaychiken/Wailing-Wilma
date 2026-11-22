# Hosted readers on two ship failures

Gemma 4 26B A4B IT led the 42-model catalog test on the Hull B. It identified the cooler failure within three seconds on **18/38** positive frames. A direct Claude Haiku 5.5 configuration also scored 18/38. A repeat of that configuration scored **19/38**. The earliest eligible warning came 3.350 s after the first reviewed frame, when the cooler fault was already visible. [Hull B recording at 1:50](https://www.youtube.com/watch?v=pOfxidINFvk&t=110s)

On the Hermes, the power plant warning first became legible between 9:25.500 and 9:25.750. Of the six finalists in the first test, only Gemini 3.5 Flash-Lite produced an eligible warning, **11.825–12.076 s** after visible onset. In a follow-up, direct Haiku reading literal terminal text from the stock JPEG improved that to **4.835–5.086 s**. The range crosses five seconds. Both warnings still had the correct visible target when eligible. [Hermes recording at 9:25](https://www.youtube.com/watch?v=6JgvriGCOr4&t=565s)

## Reading the tables

One original 1440p frame was scheduled each second at normal playback speed. **By 3 s** and **By 5 s** count correct named-failure readings against every scheduled positive frame, including busy, expired, missing and unusable checks. **Median hit** is the median capture-to-reply time for correct detections within five seconds. A dash means there was no qualifying result.

**First offer** replays the released Wilma 0.1.80 policy on those recorded replies. It requires two distinct confirming captures, a consistent view and readings less than three seconds old. The figures assume an immediately available speech queue. Actual speech submission and playback were unmeasured. A live three-second request deadline could allow different subsequent captures.

**Quiet replies** counts parseable replies within five seconds on three controls for the Hull B and seven on the Hermes. Four Hermes controls precede the failure and three follow the terminal leaving view. Lack of a reply gives no evidence about false alarms.

**USD** sums provider-reported OpenRouter charges or, for direct Haiku, estimates from returned token usage at the published rates. **Priced / sent** counts calls with cost evidence out of all started calls. When some calls lack prices, their cost is unknown. Zero applies only to the priced calls in that row. [Download the experiment CSV](reader-trials.csv)

## Hermes transfer

All 20 frames matched the reviewed pixels. Thirteen showed the power plant failure. The first six rows repeat the finalists on this ship. The last four vary the Haiku prompt, output schema or JPEG detail after inspection of the first results. No tested configuration offered a wrong or duplicate warning here.

| Model or configuration | By 3 s | By 5 s | Median hit | First offer | USD | Priced / sent | Quiet replies |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Haiku direct: focused, thinking off, 512 | 3/13 (23%) | 3/13 (23%) | 1.637 s | — | $0.00212 | 9/9 | 4/7 |
| Haiku direct: default, 512 | 0/13 (0%) | 0/13 (0%) | — | — | $0.00394 | 6/6 | 1/7 |
| google/gemma-4-26b-a4b-it | 0/13 (0%) | 0/13 (0%) | — | — | $0.00097 | 6/7 | 2/7 |
| google/gemini-2.5-flash-lite | 1/13 (8%) | 1/13 (8%) | 2.248 s | — | $0.00201 | 8/8 | 3/7 |
| amazon/nova-2-lite-v1 | 0/13 (0%) | 1/13 (8%) | 3.662 s | — | $0.00489 | 6/6 | 2/7 |
| google/gemini-3.5-flash-lite | 2/13 (15%) | 2/13 (15%) | 2.635 s | 11.825–12.076 s | $0.00693 | 7/7 | 2/7 |
| Haiku focused JSON, stock JPEG | 3/13 (23%) | 3/13 (23%) | 1.719 s | — | $0.00233 | 9/9 | 3/7 |
| Haiku literal text, stock JPEG | 5/13 (38%) | 5/13 (38%) | 1.621 s | 4.835–5.086 s | $0.00218 | 10/10 | 4/7 |
| Haiku schema + literal text, stock JPEG | 2/13 (15%) | 2/13 (15%) | 2.544 s | — | $0.00175 | 7/7 | 3/7 |
| Haiku schema + literal text, high detail | 5/13 (38%) | 5/13 (38%) | 2.089 s | 7.791–8.042 s | $0.00465 | 8/8 | 3/7 |

The initial focused Haiku read the right target at 9:27, 9:32 and 9:37. Its intervening replies at 9:29 and 9:34 included prose and concluded that the reading was unknown. Each interrupted the two-frame confirmation. The stock-JPEG literal follow-up preserved two consecutive confirmations.

## Hull B catalog models

Each model received the same general prompt, stock JPEG and five-second request deadline. Thirty-eight frames showed the cooler fault and the terminal was out of view on the final three. Of 42 catalog models, seven eventually produced an eligible cooler warning and one, Gemma, did so within five seconds. Only 12 of the 126 scheduled control checks returned a fresh parseable reply within Wilma’s three-second age limit.

| Model or configuration | By 3 s | By 5 s | Median hit | First offer | USD | Priced / sent | Quiet replies |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| google/gemma-4-26b-a4b-it | 18/38 (47%) | 18/38 (47%) | 1.760 s | 4.532 s | $0.00315 | 19/19 | 1/3 |
| google/gemini-2.5-flash-lite | 14/38 (37%) | 14/38 (37%) | 1.774 s | 32.335 s | $0.00382 | 19/19 | 1/3 |
| amazon/nova-2-lite-v1 | 11/38 (29%) | 11/38 (29%) | 2.318 s | 5.954 s | $0.01062 | 16/16 | 1/3 |
| google/gemini-3.1-flash-lite | 11/38 (29%) | 11/38 (29%) | 2.365 s | 27.364 s | $0.00967 | 13/15 | 1/3 |
| google/gemini-3.5-flash-lite | 10/38 (26%) | 11/38 (29%) | 2.268 s | 7.879 s | $0.01617 | 17/17 | 2/3 |
| openai/gpt-5.4-nano | 10/38 (26%) | 10/38 (26%) | 1.953 s | 6.954 s | $0.01270 | 19/19 | 1/3 |
| mistralai/mistral-small-2603 | 9/38 (24%) | 10/38 (26%) | 2.598 s | — | $0.00761 | 13/14 | 1/3 |
| meta-llama/llama-4-scout | 4/38 (11%) | 4/38 (11%) | 1.817 s | — | $0.00896 | 20/20 | 2/3 |
| openai/gpt-6.2-sol | 3/38 (8%) | 9/38 (24%) | 3.619 s | 19.422 s | $0.07658 | 10/11 | 1/3 |
| xiaomi/mimo-v2.6-flash | 2/38 (5%) | 3/38 (8%) | 2.842 s | — | $0.00395 | 7/11 | 0/3 |
| anthropic/claude-haiku-4.5 | 2/38 (5%) | 2/38 (5%) | 2.822 s | — | $0.04557 | 12/12 | 1/3 |
| mistralai/ministral-3b-2512 | 1/38 (3%) | 1/38 (3%) | 2.624 s | — | $0.00442 | 12/12 | 0/3 |
| openai/gpt-6.1-terra | 0/38 (0%) | 8/38 (21%) | 3.781 s | — | $0.03825 | 9/10 | 1/3 |
| google/gemini-4-argon | 0/38 (0%) | 3/38 (8%) | 4.128 s | — | $0.01792 | 3/9 | 0/3 |
| openai/gpt-5.6-luna | 0/38 (0%) | 3/38 (8%) | 4.251 s | — | $0.00400 | 4/9 | 1/3 |
| z-ai/glm-5.3-flash | 0/38 (0%) | 2/38 (5%) | 3.235 s | — | $0.00477 | 8/10 | 0/3 |
| mistralai/ministral-8b-2512 | 0/38 (0%) | 2/38 (5%) | 4.525 s | — | $0.00111 | 2/9 | 0/3 |
| anthropic/claude-haiku-5.5 | 0/38 (0%) | 0/38 (0%) | — | — | $0.00579 | 9/9 | 1/3 |
| bytedance-seed/seed-2-1-turbo | 0/38 (0%) | 0/38 (0%) | — | — | Unknown | 0/9 | 0/3 |
| bytedance-seed/seed-2.0-mini | 0/38 (0%) | 0/38 (0%) | — | — | Unknown | 0/9 | 0/3 |
| deepseek/deepseek-v4.1-flash-1017 | 0/38 (0%) | 0/38 (0%) | — | — | $0.00493 | 9/10 | 0/3 |
| google/gemini-3.8-flash | 0/38 (0%) | 0/38 (0%) | — | — | $0.01563 | 4/9 | 1/3 |
| google/gemini-4-carbon | 0/38 (0%) | 0/38 (0%) | — | — | Unknown | 0/9 | 0/3 |
| google/gemini-4-hydrogen | 0/38 (0%) | 0/38 (0%) | — | — | Unknown | 0/9 | 0/3 |
| google/gemini-4-neon | 0/38 (0%) | 0/38 (0%) | — | — | Unknown | 0/9 | 0/3 |
| google/gemini-omni-1.2-flash | 0/38 (0%) | 0/38 (0%) | — | — | Unknown | 0/9 | 0/3 |
| inclusionai/ling-3.0-flash-vl | 0/38 (0%) | 0/38 (0%) | — | — | $0.00091 | 5/9 | 0/3 |
| inclusionai/ling-3.2-flash:free | 0/38 (0%) | 0/38 (0%) | — | — | $0 | 1/16 | 0/3 |
| meta/muse-ember | 0/38 (0%) | 0/38 (0%) | — | — | $0.00576 | 14/15 | 0/3 |
| minimax/minimax-m3.1-flash | 0/38 (0%) | 0/38 (0%) | — | — | $0.00753 | 11/11 | 0/3 |
| nex-agi/nex-n2.5-mini | 0/38 (0%) | 0/38 (0%) | — | — | $0.00004 | 1/9 | 0/3 |
| nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free | 0/38 (0%) | 0/38 (0%) | — | — | $0 | 3/9 | 0/3 |
| openai/gpt-4.1-nano | 0/38 (0%) | 0/38 (0%) | — | — | $0.00428 | 12/12 | 0/3 |
| openai/gpt-6-luna | 0/38 (0%) | 0/38 (0%) | — | — | $0.00040 | 1/9 | 1/3 |
| qwen/qwen3.8-flash | 0/38 (0%) | 0/38 (0%) | — | — | Unknown | 0/9 | 0/3 |
| qwen/qwen3.8-omni-flash | 0/38 (0%) | 0/38 (0%) | — | — | Unknown | 0/9 | 0/3 |
| stepfun/step-3.7-flash | 0/38 (0%) | 0/38 (0%) | — | — | Unknown | 0/9 | 0/3 |
| tencent/hy4-flash | 0/38 (0%) | 0/38 (0%) | — | — | Unknown | 0/9 | 0/3 |
| thinkingmachines/inkling-2-mini | 0/38 (0%) | 0/38 (0%) | — | — | $0.00137 | 4/9 | 0/3 |
| thinkingmachines/inkling-2-small | 0/38 (0%) | 0/38 (0%) | — | — | $0.01615 | 14/14 | 0/3 |
| x-ai/grok-4.7 | 0/38 (0%) | 0/38 (0%) | — | — | Unknown | 0/9 | 0/3 |
| z-ai/glm-5.3-flashx | 0/38 (0%) | 0/38 (0%) | — | — | $0.00177 | 1/9 | 0/3 |

GPT-5.4 Nano offered the same cooler failure twice, at 6.954 and 19.594 s, after changing its reported view from an engineering terminal to a cockpit. Gemini 3.1 Flash-Lite also offered it twice, at 27.364 and 32.008 s, after changing the reported terminal page and subject. Neither repetition corresponded to a second reviewed failure.

## Hull B direct Haiku settings

These ten runs use Claude Haiku 5.5 through Anthropic. The first six change settings for one model. The last four repeat focused JSON or use literal terminal transcription, a response schema or a high-detail image. These are development runs on the same fault.

| Model or configuration | By 3 s | By 5 s | Median hit | First offer | USD | Priced / sent | Quiet replies |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Haiku direct: default, 512 | 0/38 (0%) | 1/38 (3%) | 3.635 s | — | $0.00713 | 11/11 | 1/3 |
| Haiku direct: default, 1536 | 0/38 (0%) | 6/38 (16%) | 4.694 s | — | $0.00531 | 7/9 | 1/3 |
| Haiku direct: low effort, 512 | 1/38 (3%) | 3/38 (8%) | 3.024 s | — | $0.00743 | 12/12 | 2/3 |
| Haiku direct: thinking off, 512 | 17/38 (45%) | 17/38 (45%) | 1.788 s | 9.835 s | $0.00919 | 20/20 | 2/3 |
| Haiku direct: focused, default, 512 | 8/38 (21%) | 10/38 (26%) | 2.708 s | 17.524 s | $0.00493 | 13/13 | 1/3 |
| Haiku direct: focused, thinking off, 512 | 18/38 (47%) | 18/38 (47%) | 1.683 s | 4.470 s | $0.00466 | 20/20 | 2/3 |
| Haiku focused JSON, stock JPEG | 19/38 (50%) | 19/38 (50%) | 1.484 s | 3.350 s | $0.00489 | 21/21 | 2/3 |
| Haiku literal text, stock JPEG | 17/38 (45%) | 17/38 (45%) | 1.484 s | 3.397 s | $0.00450 | 21/21 | 2/3 |
| Haiku schema + literal text, stock JPEG | 5/38 (13%) | 6/38 (16%) | 2.369 s | 28.935 s | $0.00390 | 16/16 | 1/3 |
| Haiku schema + literal text, high detail | 0/38 (0%) | 0/38 (0%) | — | — | $0.00907 | 16/16 | 1/3 |
