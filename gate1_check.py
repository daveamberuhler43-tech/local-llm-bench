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
import re
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
    ("6.09",  BATTERY, "mistral:7b",  "tok_per_s_median",     2, "7b tok/s"),
    ("1.55",  BATTERY, "mistral:7b",  "ttft_median_s",        2, "7b TTFT"),
    ("24.9",  BATTERY, "mistral:7b",  "watts_total",          1, "7b W tot"),
    ("11.7",  BATTERY, "mistral:7b",  "marginal_watts",       1, "7b W net"),
    ("4802",  BATTERY, "mistral:7b",  "model_resident_mb",    0, "7b resident"),
    ("2.4",   BATTERY, "mistral:7b",  "throttle_pct",         1, "7b throttle"),
    ("45.9",  BATTERY, "mistral:7b",  "hours_per_mtok",       1, "7b hrs/Mtok"),
]

# Energy cost is DERIVED, not stored: the runs were executed with a local tariff
# in MYR, and everything published is USD only. So the check recomputes it from the
# measured kWh rather than trusting the currency field in the run file.
# Derived, never rounded first: rounding the tariff to $0.1092 before multiplying
# moves the 3B figure from $0.0234 to $0.0235 and the published number stops
# matching the arithmetic.
TARIFF_USD = 0.4443 / 4.07  # TNB domestic 44.43 sen/kWh, 4.07 MYR/USD (xe.com 2026-09-12)
DERIVED = [("0.0234", BATTERY, "llama3.2:3b", "3b $/Mtok"),
           ("0.0587", BATTERY, "mistral:7b",  "7b $/Mtok")]

# Figures the tool REFUSED to produce. If a run ever fills one of these in, the
# README's empty cell becomes a lie and this must fail.
REFUSED = [(LONG_1B, "llama3.2:1b", "marginal_watts"),
           (LONG_1B, "llama3.2:1b", "energy_cost_per_mtok")]


# ---------------------------------------------------------------------------
# Video 003 figures: the RAM-pressure section.
#
# Same rule as above - every number in that section names the field it came from.
# The ladder is checked row by row rather than as a summary, because the claim
# being made is "the score never moved", and a summary cannot show that.
# ---------------------------------------------------------------------------

QUANT = "bench-quant.json"          # the two extra quantisations
DISK = "model-disk-sizes.json"      # GET /api/tags, sizes in bytes
V003 = "video-003-data.json"        # lineup + the 12-step pressure ladder

# (model, on-disk MB, resident MB, gap MB) - resident is cross-checked against the run
DISK_VS_RESIDENT = [
    ("llama3.2:3b-instruct-q8_0",  3263, 3780, 517),
    ("mistral:7b-instruct-q3_K_M", 3357, 3987, 630),
    ("mistral:7b",                 4170, 4802, 632),
]

# Loading llama3.2:3b: available before, available after, resident, cost
LOAD_COST = (4477, 1603, 2443, 2874)

# The 1B ladder, in order: available MB, tok/s, swap delta MB, quiz score
LADDER = [
    (4614, 24.82,   0, 3),
    (2868, 24.47,   0, 3),
    (2172, 23.24,   0, 3),
    (1696, 18.93,   0, 3),
    (1324, 16.25, 441, 3),
    (1122, 18.13,  18, 3),
]

SEVEN_B_AVAILABLE_MB = 837   # loading mistral:7b with zero ballast added


def rows(path, key="results"):
    return json.load(open(path, encoding="utf-8"))[key]


def check_v003_sources(problems):
    """video-003-data.json is a convenience view. Prove it still equals its sources.

    Nothing in the README reads the raw files directly, so a hand-edit here would be
    invisible to every other check in this file - the same shape of defect this whole
    script was written for.
    """
    v003 = json.load(open(V003, encoding="utf-8"))

    raw = json.load(open("ram-pressure-all.json", encoding="utf-8"))["runs"]
    if len(raw) != len(v003["pressure"]):
        problems.append(f"{V003} has {len(v003['pressure'])} pressure rows, "
                        f"ram-pressure-all.json has {len(raw)}")
    for i, (r, p) in enumerate(zip(raw, v003["pressure"])):
        drift = {k: (p[k], r.get(k)) for k in p if p[k] != r.get(k)}
        if drift:
            problems.append(f"{V003} pressure row {i} drifted from the run: {drift}")

    src = {}
    for path in (LONG_1B, BATTERY, QUANT):
        for r in rows(path):
            src.setdefault(r["model"], (path, r))
    fields = [("resident_mb", "model_resident_mb"), ("tok_per_s", "tok_per_s_median"),
              ("ttft_s", "ttft_median_s"), ("hours_per_mtok", "hours_per_mtok"),
              ("throttle_pct", "throttle_pct")]
    for m in v003["lineup"]:
        if m["model"] not in src:
            problems.append(f"{V003} lineup has {m['model']}, no run file produced it")
            continue
        path, r = src[m["model"]]
        for here, there in fields:
            if round(float(m[here]), 2) != round(float(r[there]), 2):
                problems.append(f"{V003} {m['model']}.{here} {m[here]} vs {path}:{there} {r[there]}")


def check_v003(readme, problems):
    disk = {m["model"]: m for m in rows(DISK, "models")}
    v003 = json.load(open(V003, encoding="utf-8"))
    lineup = {r["model"]: r for r in v003["lineup"]}
    ladder = [r for r in v003["pressure"] if r["model"] == "llama3.2:1b"]
    quant = {r["model"]: r for r in rows(QUANT)}

    def shown(text, label):
        if text not in readme:
            problems.append(f"{label}: {text} is not in README")

    for model, mb, resident, gap in DISK_VS_RESIDENT:
        if disk[model]["disk_mb"] != mb:
            problems.append(f"{model} disk: README {mb} vs {DISK} {disk[model]['disk_mb']}")
        if lineup[model]["resident_mb"] != resident:
            problems.append(f"{model} resident: README {resident} vs {V003} {lineup[model]['resident_mb']}")
        if resident - mb != gap:
            problems.append(f"{model} gap: README {gap} vs {resident} - {mb} = {resident - mb}")
        shown(f"{mb} MB", f"{model} disk")
        shown(f"{gap} MB", f"{model} gap")

    before, after, resident, cost = LOAD_COST
    load = next(r for r in v003["pressure"] if r["model"] == "llama3.2:3b")
    if (load["available_before_mb"], load["available_after_mb"]) != (before, after):
        problems.append(f"3b load: README {before}->{after} vs {V003} "
                        f"{load['available_before_mb']}->{load['available_after_mb']}")
    if load["model_resident_mb"] != resident:
        problems.append(f"3b load resident: README {resident} vs {V003} {load['model_resident_mb']}")
    if before - after != cost:
        problems.append(f"3b load cost: README {cost} vs {before} - {after} = {before - after}")
    shown(f"{cost} MB gone", "3b load cost")

    # The ladder is parsed back OUT of the README rather than compared to a constant
    # here. A constant that matches the JSON proves nothing about what was actually
    # published: a mutation test showed a wrong quiz column in the table sailing
    # through while every check passed.
    printed = re.findall(r"^\s*(\d+) MB\s+([\d.]+)\s+(-?\d+) MB\s+(\d+)/8\s*$",
                         readme, re.M)
    if len(printed) != len(LADDER):
        problems.append(f"ladder: README prints {len(printed)} rows, expected {len(LADDER)}")
    if len(ladder) != len(LADDER):
        problems.append(f"ladder: {V003} has {len(ladder)} rows, expected {len(LADDER)}")
    for i, (avail, tps, swap, quiz) in enumerate(LADDER):
        r = ladder[i]
        got = (r["available_before_mb"], round(r["tok_per_s"], 2), r["swap_delta_mb"], r["quiz_score"])
        if got != (avail, tps, swap, quiz):
            problems.append(f"ladder row {i}: expected {(avail, tps, swap, quiz)} vs {V003} {got}")
        if i < len(printed):
            a, t, s, q = printed[i]
            pub = (int(a), float(t), int(s), int(q))
            if pub != got:
                problems.append(f"ladder row {i}: README prints {pub} vs {V003} {got}")

    # "24.82 -> 16.25 is a 35% loss" - derived, and it is the video's whole hook
    top, bottom = LADDER[0][1], LADDER[4][1]
    loss = round((1 - bottom / top) * 100)
    if loss != 35:
        problems.append(f"speed loss: README 35% vs 1 - {bottom}/{top} = {loss}%")

    # "no score moved by a single question" across all 12 measurements
    for model, score in (("llama3.2:1b", 3), ("llama3.2:3b", 6), ("mistral:7b", 7)):
        got = {r["quiz_score"] for r in v003["pressure"] if r["model"] == model}
        if got != {score}:
            problems.append(f"{model} scores moved under pressure: {sorted(got)}, README says {score}/8")
    n = len(v003["pressure"])
    if n != 12:
        problems.append(f"README says 12 measurements, {V003} has {n}")

    seven = [r for r in v003["pressure"] if r["model"] == "mistral:7b" and r["ballast_mb"] == 0]
    if min(r["available_before_mb"] for r in seven) != SEVEN_B_AVAILABLE_MB:
        problems.append(f"7b headroom: README {SEVEN_B_AVAILABLE_MB} vs {V003} "
                        f"{min(r['available_before_mb'] for r in seven)}")
    shown(f"{SEVEN_B_AVAILABLE_MB} MB available", "7b headroom")

    # the recommendation: 1022 MB less, 38% faster, identical score
    big, small = lineup["mistral:7b"], lineup["llama3.2:3b-instruct-q8_0"]
    if big["resident_mb"] - small["resident_mb"] != 1022:
        problems.append(f"saving: README 1022 vs {big['resident_mb']} - {small['resident_mb']}")
    faster = round((small["tok_per_s"] / big["tok_per_s"] - 1) * 100)
    if faster != 38:
        problems.append(f"faster: README 38% vs {small['tok_per_s']}/{big['tok_per_s']} = {faster}%")
    if big["quiz"] != small["quiz"]:
        problems.append(f"identical score: {big['quiz']} vs {small['quiz']}")
    shown("1022 MB less, 38% faster", "recommendation")

    # the caveat that undercuts the q3: it throttles 27.0% against the q4's 2.4%
    for model, path, pct in (("mistral:7b-instruct-q3_K_M", QUANT, 27.0),
                             ("mistral:7b", BATTERY, 2.4)):
        actual = {r["model"]: r for r in rows(path)}[model]["throttle_pct"]
        if round(float(actual), 1) != pct:
            problems.append(f"{model} throttle: README {pct} vs {path} {actual}")
    shown("27.0%", "q3 throttle")
    shown("2.4%", "q4 throttle")
    return 6 + len(LADDER) * 2 + 12


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

    for shown, path, model, label in DERIVED:
        kwh = runs[path][model]["kwh_per_mtok"]
        actual = kwh * TARIFF_USD
        if round(actual, 4) != round(float(shown), 4):
            problems.append(f"{label}: README {shown} vs {kwh} kWh x ${TARIFF_USD:.6f} = {actual:.4f}")
        elif shown not in readme:
            problems.append(f"{label}: {shown} is not in README")

    for name, v in rates["rates"].items():
        usd = f"{v['output_per_mtok']:.2f}"
        if usd not in readme:
            problems.append(f"rate {name}: ${usd} not in README")

    for path, model, field in REFUSED:
        if runs[path][model][field] is not None:
            problems.append(f"{model}:{field} is populated, but README shows it as refused")

    check_v003_sources(problems)
    extra = check_v003(readme, problems)

    for p in problems:
        print("FAIL", p)
    print(f"GATE 1: {len(CLAIMS)} figures + {len(DERIVED)} derived + "
          f"{len(rates['rates'])} rates + {len(REFUSED)} refusals + "
          f"{extra} RAM-pressure checks, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
