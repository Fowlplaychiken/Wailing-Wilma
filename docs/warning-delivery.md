# How quickly does Wilma start speaking an incapacitation warning?

With one-second checks, the first speech-bearing audio reached the test computer within three seconds of the visible warning in both recordings. With two-second checks, both recordings met five seconds. These times end when the computer receives speech data. Playback through a headset or speaker remains unmeasured.

| Recording | Time between checks | Exact matches to previously reviewed frames | HUD onset to warning submission | HUD onset to speech data |
| --- | ---: | ---: | ---: | ---: |
| Seismic Data | 2 s | 4/4 | 3.636–3.949 s | 4.574–4.934 s |
| Seismic Data | 1 s | 5/7 | 1.673–1.976 s | 2.001–2.367 s |
| Security Data | 2 s | 4/4 | 2.392–4.452 s | 2.596–4.733 s |
| Security Data | 1 s | 4/7 | 0.390–2.450 s | 0.624–2.731 s |

Each run submitted “You are incapacitated” once while the player’s own banner was visible. Across the four short windows there were four correct submissions, zero false submissions and zero duplicates. The two recordings came from different play sessions by the same creator. Both had previously been used for development.

- **Seismic Data.** The banner was absent at 28:52.500 and visible at 28:52.750. At both check intervals, one quiet and three positive frames matched the primary labels. The one-second run also matched a separately reviewed frame at 28:53 inside the warning interval. Two later frames had no previous reference hash. Both readings that triggered the faster warning matched reviewed pixels. [Original Seismic Data gameplay](https://www.youtube.com/watch?v=prbwG5SAzg8&t=1732s)
- **Security Data.** The banner was absent at 24:33 and visible at 24:35. This two-second uncertainty makes the three-second result indeterminate for the slower run. Each run matched one quiet and three positive frames. The one-second run also read three previously unlabelled frames. Its first confirming reading at 24:34 was one of those three. The next reading, which triggered speech, matched the reviewed 24:35 image. [Original Security Data gameplay](https://www.youtube.com/watch?v=q2YFjZ6XW4s&t=1473s)

## What the clock measures

The original player ran muted at 1× and 1920 × 1080. Wilma 0.1.80 used its high-detail JPEG client, optional CPU terminal OCR reader, three-second frame freshness limit and two-confirmation policy. On confirmation, its released warning-submission method sent that warning text through a test adapter to the configured Pocket TTS Jane voice at 1.08×. The signed timing difference between machines was bounded by seven round trips before and seven after each run, allowing for both 15.625 ms Windows clocks and a 2 ms browser presentation allowance.

The receiving computer examined 24 kHz mono PCM in memory. The end of the interval is the arrival of the second consecutive 10 ms block above −40 dBFS RMS. This excludes leading silence and very small nonzero samples. The original Wingman playback queue and sound device were outside the experiment.

On Seismic Data, the speech request itself took 0.922–0.984 s to reach that threshold during the two-second run and 0.329–0.391 s during the one-second run. Faster confirmation and variation in synthesis time both contributed to the difference. On Security Data, the corresponding provider times were 0.204–0.266 s and 0.219–0.281 s. The results cover one pass per interval on each recording.

