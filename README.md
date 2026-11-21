# Wailing Wilma

![Wailing Wilma watches the visible ship display, checks fresh readings, and speaks through Wingman.](docs/wilma-warning-flow.svg)

Wailing Wilma watches the ship display you can already see and calls out fresh warnings through Wingman. Ask for a status check, mute a category, or let it monitor while you fly. It sends no game input.

**[Download Wailing Wilma](https://github.com/Fowlplaychiken/Wailing-Wilma/releases/latest)**

> **Early preview:** Loaded in Wingman on Windows. Accuracy across ships and displays, warning timing in combat, and game performance still need representative testing. Keep using the game's instruments and alarms.

## Install

1. Download the `wailing_wilma-*.zip` skill from [Releases](https://github.com/Fowlplaychiken/Wailing-Wilma/releases). Use the ZIP under **Assets**, not the automatic source archive.
2. Quit Wingman. Extract the ZIP into `%APPDATA%\ShipBit\WingmanAI\custom_skills`. The resulting path should end in `custom_skills\wailing_wilma\default_config.yaml`.
3. Open Wingman. In your Wingman's skill settings, make **Wailing Wilma** discoverable. Configure an image-capable model in Wingman, or choose a separate reader in Wilma's settings.
4. Bring Star Citizen to the foreground, show a readable ship display, and say **“Start Wailing Wilma.”** Ask **“Wailing Wilma status”** to see what it can read.

A Windows PC running Star Citizen and Wingman with the v3 skill API is required. Monitoring starts when you ask. Wilma uses Wingman's voice and sends no game input. No tablet, GameGlass, or Core edits are needed.

## Commands

| Say or type | What happens |
| --- | --- |
| “Wailing Wilma status” | Reports the current scene, readings and muted categories. |
| “Mute Wailing Wilma” / “Unmute Wailing Wilma” | Temporarily silences or restores warnings. |
| “Mute the fuel warnings” / “Unmute the fuel warnings” | Changes one warning category for the current session. |
| “Warn me about shields at 35 percent” | Changes the low-shield threshold for the current session. Use zero to restore the saved threshold. |
| “Stop Wailing Wilma” | Stops monitoring. |

Use the skill settings to save category switches and thresholds. Leave **Screen source** set to **Star Citizen** for normal use. **Video playback (test)** reads a foreground Chrome or Firefox window for off-game testing.

## What Wilma can read

| Visible information | Example |
| --- | --- |
| Shield charge by face | “Rear shields depleted” |
| Hull, armor, or component health | “Hull integrity critically low” |
| Explicit fire, heat, or component failure | “Fire warning: Engine room” |
| Observed change in component power | “Power offline: Reactor” |
| Hydrogen fuel, quantum fuel, or oxygen | “Quantum fuel low” |
| Explicit EMP or environmental warning | A brief callout naming the visible condition |

Readings depend on the selected page, camera angle, display legibility, and the reader. Wilma checks fresh observations before warning and limits repeated announcements. Unreadable or hidden information is unknown. Status shows which categories the latest usable frame supplied.

## Choose a vision reader

| Option | Setup |
| --- | --- |
| **Wingman's configured model** | Use an image-capable model in Wingman. This is the default. |
| **OpenRouter** | Select a vision model ID and save the key as `openrouter` in Wingman Secrets. Usage follows your OpenRouter plan. |
| **OpenAI-compatible server** | Enter a vision model ID, the server's `/v1` URL, and the name of an existing Wingman secret if the server needs a key. The server can run on another Windows or Linux machine. |

Wilma captures the foreground client area and sends the image to your selected reader. Wilma does not save captures. A hosted reader receives the image under that provider's terms. Use HTTPS for a server on an untrusted network. Reader loss pauses warnings until a fresh usable response returns.

## Troubleshooting

- **Wilma doesn't appear.** Check the folder path above, fully restart Wingman, and check the profile's skill settings.
- **Wilma is quiet.** Ask for status. Bring Star Citizen to the foreground, show a readable ship display, and check that your selected model supports images.
- **The reader is slow or keeps pausing.** Try a faster vision model or turn off **High-detail visual readings**. Old readings expire before they can trigger a callout.

For source builds, run `python build.py --out <new-folder-outside-this-repository>`. The ZIP is created locally without Wingman, model weights, or keys.

[License](LICENSE) · [Source credits](NOTICE.md)
