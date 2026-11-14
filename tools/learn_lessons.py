#!/usr/bin/env python3
"""Teach Astra from your own charts — one-time script, nothing sent to Telegram.

Put lessons in the `lessons/` folder of the project. One lesson = one chart image plus a text file
with the SAME name:

    lessons/001_rejection.png     your chart (candles + your lines), no dates needed
    lessons/001_rejection.txt     your question and your view, for example:

        QUESTION: What do you see here? Would you buy or sell?
        MY VIEW: Sell. This area rejected 3 times and equal highs sit just above (liquidity).
        I only sell after a sweep above the highs and a close back below the area.

A .txt without "MY VIEW:" is treated entirely as your view. An image without a .txt gets the default
question and Astra's answer is recorded without comparison. Text-only lessons (just a .txt) work too.

For each NEW lesson the SAME Astra agent as the live bot:
  1. answers blind (sees the image + your question, not your view),
  2. compares with your view and extracts the principles you used,
then all lessons are consolidated into the trader playbook.

Outputs (in the lessons folder):
  astra_playbook.json   principles the LIVE bot gives Astra every hour as reference
  LESSONS_REPORT.md     what Astra answered, where it agreed/missed, the playbook — read this
  astra_lessons.json    per-lesson results (already-processed lessons are skipped next time)

Run from the project root (Windows):
  .\\.venv\\Scripts\\python.exe tools\\learn_lessons.py
  .\\.venv\\Scripts\\python.exe tools\\learn_lessons.py --redo 001_rejection    # re-study one lesson
  .\\.venv\\Scripts\\python.exe tools\\learn_lessons.py --report-only            # rebuild report, no AI

The bot reads the playbook at the next hourly map; no restart needed. Delete or edit the playbook
to change what Astra uses. Principles guide Astra's reasoning; they never change the signal checks.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import astra_analysis as AA  # noqa: E402
import astra_anthropic as AN  # noqa: E402
import gold_monitor as M  # noqa: E402
M.load_secrets()

UTC = timezone.utc
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
DEFAULT_QUESTION = "What do you see on this chart? Would you buy, sell or wait, and why?"
DECISIONS = ("BUY", "SELL", "WAIT", "NO_TRADE")

_S, _obj, _enum, _arr = AA._S, AA._obj, AA._enum, AA._arr
BLIND_SCHEMA = _obj({
    "decision": _enum(*DECISIONS), "confidence": _enum("LOW", "MEDIUM", "HIGH"),
    "timeframe_hint": _S, "structure_seen": _S, "key_areas": _arr(_S), "reasoning": _S})
COMPARE_SCHEMA = _obj({
    "trader_decision": _enum(*DECISIONS), "agreement": _enum("AGREE", "PARTIAL", "DISAGREE"),
    "situation": _enum(*AA.SITUATIONS), "structure": _S, "trader_reasons": _arr(_S),
    "principles": _arr(_S), "astra_missed": _S, "astra_note": _S})
CONCEPT_BLIND_SCHEMA = _obj({
    "marks": _arr(_obj({"label": _S, "what_i_see": _S, "why_it_qualifies": _S})), "overall": _S})
CONCEPT_COMPARE_SCHEMA = _obj({
    "agreement": _enum("AGREE", "PARTIAL", "DISAGREE"),
    "definitions": _arr(_obj({"concept": _S, "rule": _S})), "astra_missed": _S, "astra_note": _S})
PLAYBOOK_SCHEMA = _obj({
    "summary": _S,
    "principles": _arr(_obj({"id": _S, "situation": _enum(*AA.SITUATIONS), "rule": _S,
                             "when_not": _S, "lessons": _arr(_S)}))})

TEACHING = """
TEACHING MODE — this overrides the market-map output format for this request only.
Your trader is teaching you with an undated chart image (and/or text). There are no live prices and
no trade is being placed. Answer with the supplied JSON schema only. Write in {language}.
Never treat the chart as the live market; never output signals.
"""
BLIND = """Step 1 (blind): read the chart yourself BEFORE seeing the trader's view. Describe the structure
you see (trend, swings, repeated tests, liquidity, breaks), the key areas (describe them, no exact
prices unless readable on the axis), your decision (BUY / SELL / WAIT / NO_TRADE), confidence and why."""
COMPARE = """Step 2 (learn): compare your blind answer with the trader's view. Classify the situation,
list the trader's reasons, and write 1-4 short, reusable, PRICE-FREE principles the trader applied
(e.g. "Sell a repeatedly rejected resistance only after a sweep of the equal highs and a close back
below"). astra_missed = what your blind reading missed or got wrong (empty if nothing). astra_note =
anything you would respectfully add or question."""
CONCEPT_BLIND = """Step 1 (blind, concept lesson): the trader labelled elements on this chart (e.g. FVG, OB, MSS,
breaker, spring). For EACH label, say what candles/structure you see at that spot and why, in your
understanding, it qualifies as that concept. Do not discuss what happened afterwards or any trade."""
CONCEPT_COMPARE = """Step 2 (learn the trader's definitions): compare your explanation with the trader's. For each
concept write the trader's identification rule as a short, reusable, PRICE-FREE definition that you can
apply to UNLABELLED charts later (what candles, what order, what must be true, what disqualifies it).
astra_missed = where your understanding differed from the trader's. astra_note = respectful questions."""
CONSOLIDATE = """Step 3 (playbook): merge the principles from ALL confirmed lessons into at most 12 clear,
non-duplicated, price-free rules, each tied to a situation, with when_not = when the rule does NOT
apply, and lessons = the lesson ids it came from. Keep the trader's intent; do not invent rules.
Keep the trader's concept definitions as their own principles with situation DEFINITION (one per concept,
merged across lessons). The live bot reads each rule up to 700 characters and each when_not up to 300,
and the whole playbook up to 9,000 characters: stay well inside these limits, and prefer merging related
rules over adding new ones."""


def find_lessons(folder: Path):
    """Pair images and .txt files by name. Returns [(lesson_id, image_path|None, text_path|None)]."""
    found = {}
    for p in sorted(folder.iterdir()):
        if p.name.startswith((".", "astra_", "LESSONS_", "README")) or not p.is_file():
            continue
        suffix = p.suffix.lower()
        if suffix in IMAGE_TYPES:
            found.setdefault(p.stem, [None, None])[0] = p
        elif suffix == ".txt":
            found.setdefault(p.stem, [None, None])[1] = p
    return [(k, v[0], v[1]) for k, v in sorted(found.items())]


def lesson_kind(path: Path | None):
    """TYPE: CONCEPT (teach why something is labelled) or TRADE (default: what would you do)."""
    if path is None:
        return "TRADE"
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.strip().upper().startswith("TYPE:"):
            return "CONCEPT" if "CONCEPT" in line.upper() else "TRADE"
    return "TRADE"


def parse_text(path: Path | None):
    if path is None:
        return DEFAULT_QUESTION, ""
    text = "\n".join(l for l in path.read_text(encoding="utf-8-sig").splitlines()
                     if not l.strip().upper().startswith("TYPE:")).strip()
    upper = text.upper()
    if "MY VIEW:" in upper:
        i = upper.index("MY VIEW:")
        head, view = text[:i], text[i + len("MY VIEW:"):]
        q = head.split(":", 1)[1] if head.upper().strip().startswith("QUESTION:") else head
        return (q.strip() or DEFAULT_QUESTION), view.strip()
    if upper.startswith("QUESTION:"):
        return text.split(":", 1)[1].strip() or DEFAULT_QUESTION, ""
    return DEFAULT_QUESTION, text


def fingerprint(image: Path | None, text: Path | None):
    h = hashlib.sha256()
    for p in (image, text):
        h.update(p.read_bytes() if p else b"-")
    return h.hexdigest()[:16]


def image_part(path: Path):
    if path.stat().st_size > 10_000_000:
        raise ValueError(f"{path.name} is larger than 10 MB")
    data = base64.b64encode(path.read_bytes()).decode()
    return {"type": "input_image", "image_url": f"data:{IMAGE_TYPES[path.suffix.lower()]};base64,{data}"}


def ask(client, saved, schema, step, parts, language, effort, tokens=4000):
    """One structured call to the SAME saved Astra agent, with optional image input."""
    instructions = saved["instructions"] + TEACHING.format(language=language) + "\n" + step
    if saved.get("provider") == "anthropic":
        return AN.lesson_ask(client, saved, schema, instructions, parts, effort, tokens)
    r = client.responses.create(
        model=saved["model"], instructions=instructions, store=False,
        input=[{"role": "user", "content": parts}], reasoning={"effort": effort}, max_output_tokens=tokens,
        text={"format": {"type": "json_schema", "name": "gold_lesson", "strict": True, "schema": schema}})
    if r.status != "completed" or not r.output_text:
        raise ValueError("Incomplete or refused model response")
    result = json.loads(r.output_text)
    M.strict_validate(result, schema)
    usage = getattr(r, "usage", None)
    tokens_used = getattr(usage, "total_tokens", None) if usage is not None else None
    return result, tokens_used


def study_concept(client, saved, lesson_id, image, text, language, effort):
    question, view = parse_text(text)
    parts = [{"type": "input_text", "text": f"Concept lesson {lesson_id}. Trader's question: {question}"}]
    if image:
        parts.append(image_part(image))
    blind, t1 = ask(client, saved, CONCEPT_BLIND_SCHEMA, CONCEPT_BLIND, parts, language, effort)
    record = {"lesson_id": lesson_id, "kind": "CONCEPT", "image": image.name if image else None,
              "text": text.name if text else None, "question": question, "trader_view": view, "blind": blind,
              "studied_at": datetime.now(UTC).isoformat(), "tokens": t1 or 0}
    if view:
        parts2 = parts + [{"type": "input_text", "text": "Your explanation was: " + json.dumps(blind, ensure_ascii=False)
                           + "\nThe trader's explanation of why they marked it: " + view}]
        c, t2 = ask(client, saved, CONCEPT_COMPARE_SCHEMA, CONCEPT_COMPARE, parts2, language, effort)
        c["definitions"] = [d for d in c["definitions"] if d["rule"].strip()][:6]
        c["situation"] = "DEFINITION"
        c["principles"] = [f"{d['concept']}: {d['rule']}" for d in c["definitions"]]
        record["comparison"] = c
        record["tokens"] += t2 or 0
    return record


def study(client, saved, lesson_id, image, text, language, effort):
    if lesson_kind(text) == "CONCEPT":
        return study_concept(client, saved, lesson_id, image, text, language, effort)
    question, view = parse_text(text)
    parts = [{"type": "input_text", "text": f"Lesson {lesson_id}. Trader's question: {question}"}]
    if image:
        parts.append(image_part(image))
    blind, t1 = ask(client, saved, BLIND_SCHEMA, BLIND, parts, language, effort)
    record = {"lesson_id": lesson_id, "kind": "TRADE", "image": image.name if image else None,
              "text": text.name if text else None,
              "question": question, "trader_view": view, "blind": blind, "studied_at": datetime.now(UTC).isoformat(),
              "tokens": t1 or 0}
    if view:
        parts2 = parts + [{"type": "input_text", "text": "Your blind answer was: " + json.dumps(blind, ensure_ascii=False)
                           + "\nThe trader's view: " + view}]
        comparison, t2 = ask(client, saved, COMPARE_SCHEMA, COMPARE, parts2, language, effort)
        comparison["principles"] = [p for p in comparison["principles"] if p.strip()][:4]
        record["comparison"] = comparison
        record["tokens"] += t2 or 0
    return record


def consolidate(client, saved, records, language, effort):
    taught = [{"lesson_id": r["lesson_id"], "situation": r["comparison"]["situation"],
               "principles": r["comparison"]["principles"], "trader_view": r["trader_view"][:600]}
              for r in records if r.get("comparison")]
    if not taught:
        return None, 0
    parts = [{"type": "input_text", "text": json.dumps({"lessons": taught}, ensure_ascii=False)}]
    playbook, t = ask(client, saved, PLAYBOOK_SCHEMA, CONSOLIDATE, parts, language, effort, tokens=5000)
    for i, p in enumerate(playbook["principles"][:12], 1):
        p["id"] = f"P{i}"
    playbook["principles"] = playbook["principles"][:12]
    return playbook, t or 0


def report(records, playbook, folder: Path, errors):
    lines = ["# Astra teaching report", "",
             f"Generated {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')} — {len(records)} lesson(s).", ""]
    compared = [r for r in records if r.get("comparison")]
    if compared:
        agree = sum(r["comparison"]["agreement"] == "AGREE" for r in compared)
        partial = sum(r["comparison"]["agreement"] == "PARTIAL" for r in compared)
        lines += [f"**Blind agreement:** {agree}/{len(compared)} agree, {partial} partial "
                  f"(Astra answered before seeing your view).", ""]
        by = {}
        for r in compared:
            s = r["comparison"]["situation"]
            by.setdefault(s, [0, 0])
            by[s][0] += r["comparison"]["agreement"] == "AGREE"
            by[s][1] += 1
        lines += ["| Situation | Agreed |", "|---|---|"] + [f"| {s} | {a}/{n} |" for s, (a, n) in sorted(by.items())] + [""]
    if playbook:
        lines += ["## Trader playbook (used live by Astra)", "", playbook["summary"], ""]
        for p in playbook["principles"]:
            lines.append(f"- **{p['id']} · {p['situation']}** — {p['rule']}"
                         + (f"  \n  _Not when:_ {p['when_not']}" if p.get("when_not") else "")
                         + (f"  \n  _From:_ {', '.join(p['lessons'])}" if p.get("lessons") else ""))
        lines.append("")
    lines += ["## Lessons", ""]
    for r in records:
        b = r["blind"]
        lines += [f"### {r['lesson_id']}" + (" (concept)" if r.get("kind") == "CONCEPT" else "")
                  + (f" — {r['image']}" if r.get("image") else ""), "",
                  f"**Your question:** {r['question']}", ""]
        if r.get("kind") == "CONCEPT":
            lines.append("**Astra's own explanation (before reading yours):**")
            lines += [f"- **{m['label']}** — sees: {m['what_i_see']}  \n  why it qualifies: {m['why_it_qualifies']}"
                      for m in b["marks"]]
            lines += [f"  \n{b['overall']}", ""]
        else:
            lines += [f"**Astra (blind):** {b['decision']} ({b['confidence']} confidence, {b['timeframe_hint']})  ",
                      f"{b['structure_seen']}  ", f"_Why:_ {b['reasoning']}", ""]
        c = r.get("comparison")
        if c:
            lines += [f"**Your view:** {r['trader_view']}", "",
                      f"**Result:** {c['agreement']} — situation {c['situation']}"]
            if c["astra_missed"]:
                lines.append(f"  \n**Astra missed:** {c['astra_missed']}")
            lines += ["", "**Definitions learned:**" if r.get("kind") == "CONCEPT" else "**Principles learned:**"]
            lines += [f"- {p}" for p in c["principles"]]
            if c["astra_note"]:
                lines += ["", f"_Astra's note:_ {c['astra_note']}"]
        else:
            lines.append("_No 'MY VIEW' given — answer recorded without comparison._")
        lines.append("")
    if errors:
        lines += ["## Problems", ""] + [f"- {e}" for e in errors] + [""]
    (folder / "LESSONS_REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def make_client(c):
    if c.get("analyst_provider") == "anthropic":
        return AN.make_client(c, retries=1)
    from openai import OpenAI
    return OpenAI(timeout=min(c["ai_deadline_seconds"], 360), max_retries=1)


def run(args, client=None):
    c = M.load_config(args.config)
    language = args.language or c["telegram_language"]
    folder = Path(args.folder)
    folder.mkdir(parents=True, exist_ok=True)
    store = folder / "astra_lessons.json"
    done = json.loads(store.read_text(encoding="utf-8-sig")) if store.is_file() else {}   # -sig: a BOM from an editor
    lessons = find_lessons(folder)
    errors, spent = [], 0
    if not lessons:
        print(f"No lessons in {folder}. Add e.g. 001_name.png + 001_name.txt (see --help).")
        return 1
    current = {}
    todo = []
    for lid, image, text in lessons:
        fp = fingerprint(image, text)
        if lid in done and done[lid].get("fingerprint") == fp and lid not in (args.redo or []):
            current[lid] = done[lid]
        else:
            todo.append((lid, image, text, fp))
    playbook_path = folder / "astra_playbook.json"
    playbook = json.loads(playbook_path.read_text(encoding="utf-8-sig")) if playbook_path.is_file() else None
    if not args.report_only and todo:
        client = client or make_client(c)
        saved = (AN.load_analyst(c, M.ROOT) if c.get("analyst_provider") == "anthropic"
                 else M.load_saved_agent(client))
        print(f"Astra model {saved['model']}: studying {len(todo)} new/changed lesson(s), "
              f"{len(current)} unchanged.")
        for lid, image, text, fp in todo:
            try:
                rec = study(client, saved, lid, image, text, language, c["astra_reasoning"])
                rec["fingerprint"] = fp
                current[lid] = rec
                spent += rec["tokens"]
                c_ = rec.get("comparison")
                if rec.get("kind") == "CONCEPT":
                    print(f"  {lid} (concept): {len(rec['blind']['marks'])} mark(s) explained"
                          + (f" | {c_['agreement']} with you | {len(c_['definitions'])} definition(s)" if c_
                             else " | no view to compare"))
                else:
                    print(f"  {lid}: Astra blind {rec['blind']['decision']}"
                          + (f" | you {c_['trader_decision']} | {c_['agreement']}" if c_ else " | no view to compare"))
            except Exception as exc:
                msg = f"{lid}: {type(exc).__name__}: {M.safe_error(exc)[:300]}"
                if image and ("image" in str(exc).lower() or "vision" in str(exc).lower()):
                    msg += " — your Astra model may not accept images; try a text-only lesson."
                errors.append(msg)
                print("  " + msg)
        records = [current[k] for k in sorted(current)]
        try:
            new_pb, t = consolidate(client, saved, records, language, c["astra_reasoning"])
            spent += t
            if new_pb:
                new_pb["updated_at"] = datetime.now(UTC).isoformat()
                new_pb["model"] = saved["model"]
                playbook = new_pb
                M.atomic(playbook_path, playbook)
        except Exception as exc:
            errors.append(f"playbook: {type(exc).__name__}: {M.safe_error(exc)[:300]}")
        M.atomic(store, current)
    elif not todo:
        print("All lessons already studied (use --redo NAME to study one again).")
    records = [current[k] for k in sorted(current)]
    report(records, playbook, folder, errors)
    if spent:
        print(f"Tokens used: {spent:,}")
    print(f"Report: {folder / 'LESSONS_REPORT.md'}")
    if playbook:
        print(f"Playbook ({len(playbook['principles'])} principles) used live from the next hourly map: {playbook_path}")
    return 2 if errors else 0


def main(argv=None, client=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--folder", default=str(ROOT / "lessons"))
    p.add_argument("--config", default=str(ROOT / "config" / "gold_v5_config.json"))
    p.add_argument("--redo", nargs="*", help="lesson names to study again")
    p.add_argument("--report-only", action="store_true", help="rebuild the report, no AI calls")
    p.add_argument("--language", help="override telegram_language")
    args = p.parse_args(argv)
    M.utf8_console()
    if not Path(args.config).is_file():
        args.config = None
    key = AN.analyst_key_name(args.config)
    if client is None and not args.report_only and not os.getenv(key):
        p.error(f"{key} is not set")
    return run(args, client)


if __name__ == "__main__":
    raise SystemExit(main())
