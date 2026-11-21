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

![Hosted Inkling cockpit timeline and a comparison with the local model on two matching frames.](inkling-mini-cockpit.svg)

**Inkling 2 Mini via OpenRouter.** Wilma tested the hosted model against 36 seconds of 1440p cockpit gameplay with visible warnings. At one check per second, it returned **0 timely readings in 37 checks**. Thirteen requests reached the three-second deadline, 23 checks occurred while a request was in progress, and one capture was missed. Wilma submitted no warnings. The provider returned no usage on these requests, so cost and recognition accuracy on completed replies are unknown. [Source footage at 14:16](https://www.youtube.com/watch?v=eJoqjCmYYls&t=856s)

Follow-up requests used the same two video times. The captured pixels matched across settings. At 14:27 the player's HUD showed a critical shield warning. At 14:31 the warning strip was blank. The earlier review recorded the video times without saving pixel hashes.

| Reader setting | Critical shield warning | Blank warning strip | Cost for both calls |
| --- | --- | --- | ---: |
| Hosted default reasoning, 15 s limit | Truncated (3.64 s) | Truncated (12.33 s) | $0.00053652 |
| Hosted reasoning off, 15 s limit | Warning missed (2.36 s) | Quiet (1.85 s) | $0.00026492 |
| Hosted reasoning off, 3 s limit | Expired (3.02 s) | Quiet (1.66 s) | At least $0.00031150 |
| Local IQ4_NL, reasoning off, 15 s limit | Expired (15.10 s) | Expired (15.01 s) | $0 API |

The local test used a GeForce RTX 2080 Ti (11 GiB) and a Core i7-10700K. Both requests timed out before the reader returned an answer, so recognition accuracy is unknown. Electricity is excluded from the API cost.

On the hosted service, disabling extra reasoning produced shorter, readable responses. Wilma recognized the cockpit and accepted no instrument readings from either view. The critical warning was missed even on the completed 2.36-second reply. The three-second repeat expired on that same warning. One blank strip cannot establish a false-alarm rate.

Eight reviewed checkpoints in a separate recording show two cockpit views, three on foot and three from an ATLS vehicle. Here the task was identifying the viewpoint. All three readers returned zero accepted instrument readings. [Watch the Aberdeen recording](https://www.youtube.com/watch?v=FtvXIOjns90)

| Reader | Correct viewpoint by deadline | Median completed reply age | Reported API charge for this run |
| --- | ---: | ---: | ---: |
| Gemini 3.5 Flash-Lite | 4/8 (50%) | 1.89 s | $0.0117 for 14 completions |
| Claude Haiku 5.5 | 1/8 (12.5%) | 2.48 s | $0.0032 for six completions; four expired calls had unknown billing |
| CPU terminal OCR | 0/8 | 0.285 s | $0 |

The terminal reader specializes in engineering-console banners and returned unknown on these views. Qwen had no matched run here. The hardware figures above exclude electricity.

## Personal warning false alarms

In version 0.1.80, Wilma can announce **You Are Incapacitated** on foot. In one original 1080p recording, local Qwen3.5-4B Q4_K_M read the banner in all seven reviewed frames from 24:35 through 24:47. All four preceding frames stayed quiet for incapacitation. On an RTX 2080 Ti, all 11 matching frames returned within the 3 s deadline. Median reply age was 1.243 s and the slowest was 1.784 s. Replaying those responses with the default two confirmations offered a single warning at 24:38.747. The run used the stock Wingman encoder, cost $0 in API fees and exercised one previously reviewed incident. Live speech and results on new recordings remain open. [Onyx Facility at 24:35](https://www.youtube.com/watch?v=q2YFjZ6XW4s&t=1475s)

On a different task, local Qwen3.5-4B read the on-foot HUD for blood loss and high pressure. The original prompt produced six false blood-loss readings in eight quiet Siege frames. Asking the reader to transcribe the visible alert fixed those six errors. With the revised prompt, 21/21 matched checkpoints across three recordings correctly stayed quiet. Two frames differed from the reviewed image, so they were excluded. None of these checkpoints contained either warning. Positive detection of blood loss or high pressure still needs a recording with that warning. [Siege of Orison](https://www.youtube.com/watch?v=56xX6cU26lI&t=1626s) · [Project Hyperion](https://www.youtube.com/watch?v=tKAXFuL98UU&t=303s) · [Personal HUD footage](https://www.youtube.com/watch?v=9w8nN3LFOFE&t=437s)
