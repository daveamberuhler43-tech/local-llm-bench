#!/usr/bin/env python3
"""
gate1_check.py - every published figure, diffed against the run that produced it.

Written after a README table silently carried two figures from an earlier, shorter
run than the one it claimed to report. Reading it back did not catch that. Diffing
it did.

Each CLAIM names the exact JSON field a number must come from, so a figure cannot be
transcribed, rounded or remembered into the README without this failing.

    python gate1_check.py          # exit 1 if any figure disagrees with the data
"""

import json
import sys

BATTERY = "bench-battery.json"   # the clean on-battery run: speed + power
LONG_1B = "bench-1b-long.json"   # the 30-run 1B pass: speed only, power refused

# (text in README, source file, model, field, decimal places, label)
CLAIMS = [
    ("19.24", LONG_1B, "llama3.2:1b", "tok_per_s_median",     2, "1b tok/s"),
    ("0.38",  LONG_1B, "llama3.2:1b", "ttft_median_s",        2, "1b TTFT"),
    ("1449",  LONG_1B, "llama3.2:1b", "model_resident_mb",    0, "1b resident"),
    ("9.7",   LONG_1B, "llama3.2:1b", "throttle_pct",         1, "1b throttle"),
    ("15.0",  LONG_1B, "llama3.2:1b", "hours_per_mtok",       1, "1b hrs/Mtok"),
    ("12.86", BATTERY, "llama3.2:3b", "tok_per_s_median",     2, "3b tok/s"),
    ("0.68",  BATTERY, "llama3.2:3b", "ttft_median_s",        2, "3b TTFT"),
    ("23.2",  BATTERY, "llama3.2:3b", "watts_total",          1, "3b W tot"),
    ("10.0",  BATTERY, "llama3.2:3b", "marginal_watts",       1, "3b W net"),
    ("2443",  BATTERY, "llama3.2:3b", "model_resident_mb",    0, "3b resident"),
    ("1.4",   BATTERY, "llama3.2:3b", "throttle_pct",         1, "3b throttle"),
    ("21.5",  BATTERY, "llama3.2:3b", "hours_per_mtok",       1, "3b hrs/Mtok"),
    ("0.095", BATTERY, "llama3.2:3b", "energy_cost_per_mtok", 3, "3b RM/Mtok"),
    ("6.09",  BATTERY, "mistral:7b",  "tok_per_s_median",     2, "7b tok/s"),
    ("1.55",  BATTERY, "mistral:7b",  "ttft_median_s",        2, "7b TTFT"),
    ("24.9",  BATTERY, "mistral:7b",  "watts_total",          1, "7b W tot"),
    ("11.7",  BATTERY, "mistral:7b",  "marginal_watts",       1, "7b W net"),
    ("4802",  BATTERY, "mistral:7b",  "model_resident_mb",    0, "7b resident"),
    ("2.4",   BATTERY, "mistral:7b",  "throttle_pct",         1, "7b throttle"),
    ("45.9",  BATTERY, "mistral:7b",  "hours_per_mtok",       1, "7b hrs/Mtok"),
    ("0.239", BATTERY, "mistral:7b",  "energy_cost_per_mtok", 3, "7b RM/Mtok"),
]

# Figures the tool REFUSED to produce. If a run ever fills one of these in, the
# README's empty cell becomes a lie and this must fail.
REFUSED = [(LONG_1B, "llama3.2:1b", "marginal_watts"),
           (LONG_1B, "llama3.2:1b", "energy_cost_per_mtok")]


def load(path):
    return {r["model"]: r for r in json.load(open(path, encoding="utf-8"))["results"]}


def main():
    readme = open("README.md", encoding="utf-8").read()
    runs = {p: load(p) for p in {BATTERY, LONG_1B}}
    rates = json.load(open("api_rates.json", encoding="utf-8"))
    problems = []

    for shown, path, model, field, dp, label in CLAIMS:
        actual = runs[path][model][field]
        if actual is None:
            problems.append(f"{label}: README shows {shown}, but {field} was refused")
        elif round(float(actual), dp) != round(float(shown), dp):
            problems.append(f"{label}: README {shown} vs {path}:{field} {actual}")
        elif shown not in readme:
            problems.append(f"{label}: {shown} is not in README")

    fx = rates["usd_to_myr"]
    for name, v in rates["rates"].items():
        myr = f"{v['output_per_mtok'] * fx:.2f}"
        if myr not in readme:
            problems.append(f"rate {name}: RM{myr} not in README")

    for path, model, field in REFUSED:
        if runs[path][model][field] is not None:
            problems.append(f"{model}:{field} is populated, but README shows it as refused")

    for p in problems:
        print("FAIL", p)
    print(f"GATE 1: {len(CLAIMS)} figures + {len(rates['rates'])} rates + "
          f"{len(REFUSED)} refusals checked, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
