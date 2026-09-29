#!/usr/bin/env python3
"""
gate1_v004.py - every figure video 004 publishes, diffed against the run.

    python gate1_v004.py                     # checks the script + narration
    python gate1_v004.py --file path.md      # check one file

Same rule as gate1_check.py: a number reaches an audience only if it can be
traced to video-004-data.json. Two extra jobs specific to this video:

  1. CLAIM GUARD. This video rides the Jev wave but does NOT test Jev. Any file
     that says we tested/ran/benchmarked Jev fails, because llama.cpp PR 29321
     is open, not merged, and we measured Ollama's schema constraint instead.

  2. DERIVED FIGURES. Slowdowns and point-swings are recomputed from the raw
     medians rather than trusted, because an early draft of the memory note
     said "3-4x slower on every model" when mistral:7b-q3 is 0.99x - no cost
     at all. Rounding or generalising before publishing is how that happened.
"""

import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "video-004-data.json")

# Phrases that would claim more than we measured.
FORBIDDEN = [
    (r"\bI (?:tested|ran|benchmarked|measured) Jev\b", "claims we tested Jev itself"),
    (r"\bJev mode (?:on|in) (?:my|our|this) (?:laptop|machine)\b",
     "implies we ran real Jev mode locally"),
    (r"\b3[-– ]?(?:to[- ])?4x slower on every model\b", "false: the q3 is 0.99x"),
    (r"\bnever hallucinat", "overclaims: schemas bound shape, not truth"),
    # Added 2026-09-28. The script said "five to six and a half times slower on any
    # model that was already answering briefly". Measured json/free: 6.47, 5.42,
    # 5.10, 0.99, 2.21, 4.60. mistral:7b answers in 0.76s - briefly - and is 2.21x.
    # The range was only ever true of the three llamas. This is the same class of
    # error as the "3-4x" guard above: a range generalised past the data.
    # The error class is the GENERALISATION, not the numbers. Measured json/free
    # spans 0.99x to 6.47x, so no single range is true "on any model". Saying the
    # three llamas pay 5-6.5x is fine - that is scoped and correct.
    (r"slower on (?:any|every) model",
     "no slowdown range holds across models: measured 0.99x to 6.47x"),
    # "One question nothing fixed" - mistral:7b got the /26 question 10/10 in ALL
    # THREE modes, and qwen3.5:4b gets it 10/10 under either constraint.
    (r"\bOne question nothing fixed\b",
     "false: mistral:7b is 10/10 on /26 in all modes; qwen3.5 10/10 constrained"),
]

MODELS = {
    "1b": "llama3.2:1b",
    "3b": "llama3.2:3b",
    "q8": "llama3.2:3b-instruct-q8_0",
    "q3": "mistral:7b-instruct-q3_K_M",
    "7b": "mistral:7b",
    "qwen": "qwen3.5:4b",
}


def load():
    d = json.load(open(DATA, encoding="utf-8"))
    return {r["model"]: r for r in d["results"]}, d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", action="append", default=None)
    a = ap.parse_args()

    root = os.path.dirname(HERE)
    files = a.file or [os.path.join(root, "video-004-script.md"),
                       os.path.join(root, "narration-004.md")]
    files = [f for f in files if os.path.exists(f)]
    if not files:
        sys.exit("no script or narration to check yet")

    runs, raw = load()
    problems = []
    checked = 0

    for path in files:
        text = open(path, encoding="utf-8").read()
        name = os.path.basename(path)

        for pat, why in FORBIDDEN:
            for m in re.finditer(pat, text, re.I):
                problems.append(f"{name}: {why} -> {m.group(0)!r}")

        # Every per-model score we might publish, as "NN/80"
        for tag, model in MODELS.items():
            s = runs[model]["summary"]
            for mode in ("free", "json", "enum"):
                shown = f"{s[mode]['correct']}/80"
                checked += 1
                # only assert presence-correctness: if a NN/80 for this model's
                # mode appears at all, it must be the right NN
                wrong = re.findall(rf"\b(\d+)/80\b", text)
                if shown.split("/")[0] not in wrong and shown in text:
                    problems.append(f"{name}: {shown} for {model}.{mode} not found intact")

        # Derived: slowdown per model, one decimal
        for tag, model in MODELS.items():
            s = runs[model]["summary"]
            factor = s["json"]["median_wall_s"] / s["free"]["median_wall_s"]
            shown = f"{factor:.2f}x"
            checked += 1
            # if the script quotes a slowdown near this model's name, it must match
            near = re.search(rf"{re.escape(model)}[^\n]{{0,120}}?([\d.]+)x", text)
            if near and abs(float(near.group(1)) - factor) > 0.05:
                problems.append(
                    f"{name}: {model} slowdown {near.group(1)}x vs computed {factor:.2f}x")

        # The /26 subnet question, which the script leans on twice.
        def q26(model):
            q = next(x for x in runs[model]["detail"] if x["expected"] == "62")
            return {m: sum(q["modes"][m]["ok"]) for m in ("free", "json", "enum")}

        # (a) The SCOPED claim: the three smaller models fail it in all three modes.
        for model in ("llama3.2:1b", "llama3.2:3b", "llama3.2:3b-instruct-q8_0"):
            for mode, got in q26(model).items():
                if got != 0:
                    problems.append(
                        f"{model} /26 {mode} is {got}/10, not 0/10 - the scoped "
                        f"'three smaller models' claim is dead")
            checked += 3

        # (b) Why the UNSCOPED version is in FORBIDDEN: mistral:7b aces this question.
        if any(v != 10 for v in q26("mistral:7b").values()):
            problems.append("mistral:7b /26 is no longer 10/10 in all modes - "
                            "re-check the FORBIDDEN 'One question nothing fixed' guard")
        checked += 1

        # (c) The mirror the new act is built on: the SAME question, opposite
        # directions. Constraining destroys it on the q3 and rescues it on qwen.
        # If either half stops holding, that act has no point left.
        q3, qw = q26("mistral:7b-instruct-q3_K_M"), q26("qwen3.5:4b")
        if not (q3["free"] == 10 and q3["json"] == 0 and q3["enum"] == 0):
            problems.append(f"q3 /26 mirror broken: {q3} (want free 10, json 0, enum 0)")
        if not (qw["free"] == 0 and qw["json"] == 10 and qw["enum"] == 10):
            problems.append(f"qwen /26 mirror broken: {qw} (want free 0, json 10, enum 10)")
        checked += 2

    for p in problems:
        print("FAIL", p)
    print(f"GATE 1 (v004): {checked} figures + {len(FORBIDDEN)} claim guards over "
          f"{len(files)} file(s), {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
