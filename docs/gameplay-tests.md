# Gameplay tests

These runs used real gameplay playing at normal speed at 2560 × 1440. Reviewers checked the visible HUD before comparing model replies. The deadline was three seconds per frame. Recognizing the viewpoint, reading a gauge and hearing an alert are measured separately.

The [full comparison data](https://github.com/Fowlplaychiken/Wailing-Wilma/blob/NewRepo/docs/reader-trials.csv) includes local and hosted models, OCR and Jev trials, hardware, available costs, denominators and cases still awaiting a run. Compare rows with the same task and source. A planned run has no score.

## Engineering terminal

At 2:00 in the Hull B recording, a cooler failure was already visible. It remained visible at 28 scheduled checkpoints. A camera move hid the terminal at the last three. [Watch the source footage](https://www.youtube.com/watch?v=pOfxidINFvk&t=120s)

| Reader | Timely correct failure readings | Median age of completed replies | Correct warnings submitted | API charge |
| --- | ---: | ---: | ---: | ---: |
| Tesseract terminal reader · Intel Core i7-10700K | 13/28 (46.4%) | 764 ms | 1 | $0 |
| Warm Qwen3.5-4B Q4_K_M · GeForce RTX 2080 Ti | 0/28 | No reply by deadline | 0 | $0 |

The CPU reader submitted “Cooler failed” once. The fault was already on screen at the start. These figures measure detection of an ongoing fault with a silent speech adapter. They give neither time to first audible warning nor precision across ships. In a separate play of the same recording, hosted Gemini 3.5 Flash-Lite also submitted one correct cooler warning. Its 20 completions cost $0.0189 in total. On a fresh Hermes recording, both tested CPU reader configurations missed the power-plant failure.

![Engineering-terminal OCR results on the development recording and a different ship.](wilma-component-transfer.png)

## Cockpit, vehicle and on foot

![Timeline of all 37 Inkling 2 Mini checks on moving cockpit footage.](inkling-mini-cockpit.svg)

**Inkling 2 Mini via OpenRouter.** Wilma tested the hosted model against 36 seconds of 1440p cockpit gameplay with visible warnings. At one check per second, it returned **0 timely readings in 37 checks**. Thirteen requests reached the three-second deadline, 23 checks occurred while a request was in progress, and one capture was missed. Wilma submitted no warnings. The provider returned no usage on these requests, so cost and recognition accuracy on completed replies are unknown. [Source footage at 14:16](https://www.youtube.com/watch?v=eJoqjCmYYls&t=856s)

Eight reviewed checkpoints in a separate recording show two cockpit views, three on foot and three from an ATLS vehicle. Here the task was identifying the viewpoint. All three readers returned zero accepted instrument readings. [Watch the Aberdeen recording](https://www.youtube.com/watch?v=FtvXIOjns90)

| Reader | Correct viewpoint by deadline | Median completed reply age | Reported API charge for this run |
| --- | ---: | ---: | ---: |
| Gemini 3.5 Flash-Lite | 4/8 (50%) | 1.89 s | $0.0117 for 14 completions |
| Claude Haiku 5.5 | 1/8 (12.5%) | 2.48 s | $0.0032 for six completions; four expired calls had unknown billing |
| CPU terminal OCR | 0/8 | 0.285 s | $0 |

The terminal reader specializes in engineering-console banners and returned unknown on these views. Qwen had no matched run here. The hardware figures above exclude electricity.

## Personal warning false alarms

On a different task, local Qwen3.5-4B read the on-foot HUD for blood loss and high pressure. The original prompt produced six false blood-loss readings in eight quiet Siege frames. Asking the reader to transcribe the visible alert fixed those six errors. With the revised prompt, 21/21 matched checkpoints across three recordings correctly stayed quiet. Two frames differed from the reviewed image, so they were excluded. None of these checkpoints contained either warning. Detection of a real personal emergency still needs a positive test. [Siege of Orison](https://www.youtube.com/watch?v=56xX6cU26lI&t=1626s) · [Project Hyperion](https://www.youtube.com/watch?v=tKAXFuL98UU&t=303s) · [Personal HUD footage](https://www.youtube.com/watch?v=9w8nN3LFOFE&t=437s)
