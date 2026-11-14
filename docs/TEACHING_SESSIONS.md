# Teaching Astra from your charts

Lessons are files, and a one-time script turns them into a **trader playbook** that the live bot gives
Astra every hour as reference. The same saved Astra agent that runs live analysis studies them;
there is no second AI and no Telegram involvement.

## 1. Add lessons

In the project's `lessons/` folder, put a chart image and a `.txt` file with the same name. No dates are needed.

```
lessons/001_rejection.png
lessons/001_rejection.txt
```

```
QUESTION: What do you see here? Would you buy or sell?
MY VIEW: Sell. This area rejected 3 times and equal highs sit just above (liquidity).
I only sell after a sweep above the highs and a close back below the area.
```

Two lesson types:

- **Trade lesson** (default): Astra decides blind what it would do (buy / sell / wait, where), then compares with your view and learns the principles you applied.
- **Concept lesson**: put `TYPE: CONCEPT` as the first line. Astra explains your marks blind (why this is an FVG, an OB, an MSS…), then reads your definitions and stores them as DEFINITION principles. Use this for labelled teaching charts; use trade lessons for clean, unlabelled charts.

```
TYPE: CONCEPT
QUESTION: Why is this zone marked as an FVG? Where are its top, bottom and middle?
MY VIEW: The FVG is the gap between the LOW of the candle before the big candle and the HIGH of the candle after it...
```

Other formats also work:
- A text-only lesson: just a `.txt` file.
- A `.txt` without `MY VIEW:`: the whole text is treated as your view.
- An image without a `.txt`: Astra answers the default question, and its answer is recorded without comparison.

## 2. Run the script

```powershell
.\.venv\Scripts\python.exe tools\learn_lessons.py                 # study new/changed lessons
.\.venv\Scripts\python.exe tools\learn_lessons.py --redo 001_rejection
.\.venv\Scripts\python.exe tools\learn_lessons.py --report-only    # rebuild the report, no AI
```

For each new lesson Astra:

1. **Answers blind.** It sees the image and your question, but not your view.
2. **Compares with your view.** It records agree / partial / disagree, classifies the situation, notes what it missed, and writes 1–4 price-free principles you applied.

Then every lesson is merged into a playbook of at most 12 principles. Each principle has a situation, a rule, when it does not apply, and the lessons it came from.

Lessons that are already studied and unchanged are skipped, so re-running costs nothing until you add or edit lessons. Each lesson costs 2 calls, plus 1 call for the playbook.

## 3. Read the outputs (in `lessons/`)

| File | Contents |
|---|---|
| `LESSONS_REPORT.md` | Astra's blind answers, agreement with you (overall and per situation), what it missed, the principles and the playbook |
| `astra_playbook.json` | The principles the live bot uses |
| `astra_lessons.json` | Per-lesson results (used to skip lessons already studied) |

## 4. Live use

At every hourly map the bot loads `astra_playbook.json` (setting `playbook_file`; an empty value disables it) and sends Astra up to 12 principles, capped at 9,000 characters (each rule up to 700 characters). No restart is needed.

Astra is told to apply a principle only when the live structure genuinely matches, never to take prices from it, and to name the principle in its reasoning when it uses one, e.g. "Trader principle P3: wait for the sweep".

Principles guide Astra's reasoning only. They never create, approve or relax a BUY/SELL signal; the 2R, news, calendar, confirmation and broker checks are unchanged.

`tools/astra_dry_run.py` shows whether the playbook is included in the payload.

## 5. If images fail

If your Astra model does not accept images, the report says so for each image lesson. Text-only lessons still work: describe the chart in words in the `.txt` file.
