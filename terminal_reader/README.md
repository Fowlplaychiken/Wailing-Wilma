# Wilma Terminal OCR

Read engineering console warnings with Tesseract on the Wingman PC or a second computer. The reader recognizes a visible reactor countdown or a named component failure. You can also enable the English incapacitation warning on the player's own suit HUD. Wilma decides whether the reading is fresh and when to announce it. For cockpit shields, ground vehicles or other suit readings, select a general vision model.

## Start the reader

1. Extract the terminal reader ZIP on the computer doing the OCR. Open a terminal in the extracted folder.
2. Use Python 3.11 and install the libraries listed in `requirements.txt`, preferably in a virtual environment.
3. Select your trusted Tesseract 5.5.3 or 5.5.4 installation. Use its executable SHA-256 from your verified install or build record. Place the pinned English recognition file in a directory you own. [English tessdata_fast](https://github.com/tesseract-ocr/tessdata_fast/blob/87416418657359cb625c412a48b6e1d6d41c29bd/eng.traineddata)
4. Run the setup command below once. It reads a generated “42” test image through both Tesseract and Wilma, checks that both finish, and prints a receipt hash. Keep the receipt locally. Repeat after changing the executable or recognition data.
5. Start the reader with the printed hash. Leave the terminal open while using the reader. Stop it with Ctrl+C.

For example, replace the paths and hash with the values for your installation:

```shell
python -m pip install -r requirements.txt
python -m tools.setup_wilma_terminal_ocr --tesseract-executable "path/to/tesseract" --tessdata-directory "path/to/tessdata" --engine-sha256 YOUR_ENGINE_SHA256 --out receipt.json
python -m tools.wilma_terminal_ocr --tesseract-executable "path/to/tesseract" --tessdata-directory "path/to/tessdata" --receipt receipt.json --receipt-sha256 YOUR_RECEIPT_SHA256
```

The default address accepts connections on this computer. For direct access from a trusted LAN, add `--host YOUR_LAN_ADDRESS --allow-lan --api-key-env WILMA_READER_KEY` and save the same key in Wingman Secrets. This starts an HTTP listener. SSH forwarding keeps images and credentials encrypted in transit.

## Connect Wingman

Open **Wailing Wilma → Vision reader** and choose **OpenAI-compatible server**. Set **Separate reader model** to `wilma-terminal-ocr` and **Reader server address** to `http://127.0.0.1:49154/v1` if both apps run on this computer. Turn on **High-detail visual readings**. If using another host, use its configured address and authentication, or forward its loopback port over your existing SSH connection. A loopback address always refers to the computer making the request.

Start visual warnings by voice and look at an engineering console. Ask for **Wilma status** to check the reader. Select **Wingman's configured model** to return to your usual vision model. This setting applies only to Wilma.

## Read your suit's incapacitation warning

With Wilma 0.1.80 or later, add `--personal-incapacitation` to the reader startup command. In Wilma turn on **Incapacitation warnings on foot**, leave **High-detail visual readings** on and turn off **Watch engineering terminals on foot**. When Wilma knows you are out of your seat, it requests the personal HUD. Seated and general requests use the engineering reader.

Reader 0.1.82 adds `--personal-incapacitation bright-text` for pale HUD lettering against uneven terrain. Use it with **High-detail visual readings**. The original grayscale setting is available by omitting `bright-text`. The filter applies only to the suit banner and runs once per image on the OCR computer.

The personal reader looks for the complete “You Are Incapacitated” heading and the adjacent “Time Until Death” timer in the lower-left part of the image. If OCR splits the digits from the label, it also requires the nearby rescue beacon instruction. These details help distinguish the player's HUD from a subtitle. The reader returns only the heading. An obscured heading, a different language, or a shifted HUD can leave the state unknown. An absent banner does not establish recovery or rescue. The optional reader and warning start disabled. Wilma's usual freshness and confirmation settings apply.
