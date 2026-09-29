#!/usr/bin/env python3
"""
typed_decode.py - what schema-constrained decoding actually costs on a CPU laptop.

    python typed_decode.py --selftest              # maths + schema wiring, no model
    python typed_decode.py --models llama3.2:3b
    python typed_decode.py --runs 5 --out typed.json

WHY THIS EXISTS. Jev (Typesafe AI, launched Sep 2026) claims millisecond answers
that "cannot hallucinate" because output is constrained to your schema. Everyone
is now asking whether you can get the same thing locally. You partly can - every
llama.cpp-based runtime, Ollama included, can constrain decoding to a JSON schema
today.

WHAT THIS IS NOT. This does not test Jev. Jev's speed comes from a single-pass
typed readout - the upstream llama.cpp PR for it is #29321, "typed decision
readout for models that answer instead of writing", and it is still OPEN, not
merged. Ollama constrains which tokens are legal but still writes every one of
them, scaffolding included. So this measures the thing you can actually run
today, and says plainly that it is a different mechanism.

THREE MODES, same eight questions, same model, same machine:
  free   - plain prompt, model writes whatever it wants
  json   - constrained to {"answer": <string>}: shape guaranteed, content free
  enum   - constrained to one of four literal options: the closest local
           equivalent to what Jev sells

THE MEASUREMENT TRAP THIS AVOIDS. A first pass here showed constrained decoding
7x FASTER, which was wrong: the free run was the first call and paid the cold
model load. Warm and alternating, constrained came out ~6x SLOWER. So: the model
is warmed before timing, modes alternate within every trial so drift hits all
three equally, temperature is 0 with a fixed seed, and the median is reported,
never the mean.
"""

import argparse
import json
import os
import statistics as st
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from local_llm_bench import QUIZ, quiz_answer_ok  # noqa: E402

API = "http://127.0.0.1:11434"
SEED = 7
TIMEOUT = 300
FLOOR_MB = 900            # same floor ram_pressure.py uses; below it Windows struggles

# Measured in video 003 (video-003-data.json). Used only to decide whether a model
# can be loaded safely - never published from here.
RESIDENT_MB = {
    "llama3.2:1b": 1449,
    "llama3.2:3b": 2443,
    "llama3.2:3b-instruct-q8_0": 3780,
    "mistral:7b-instruct-q3_K_M": 3987,
    "mistral:7b": 4802,
    # Measured 2026-09-28 from /api/ps while the run below was warming. Note it
    # is ~400 MB UNDER the 3.4 GB on-disk size, so the disk-size fallback in
    # resident_estimate_mb is conservative here, which is the direction we want.
    "qwen3.5:4b": 2989,
}


def mem_available_mb():
    import psutil
    return psutil.virtual_memory().available / 1024 / 1024


def resident_estimate_mb(model):
    """How much RAM this model will want once loaded.

    Prefers a measured number from RESIDENT_MB. For a model we have never run,
    falls back to its size on disk from /api/tags, which understates resident
    use slightly but is the right order of magnitude.

    This exists because `RESIDENT_MB.get(model)` returns None for an unknown
    model, and the caller's `if need and ...` then skipped the floor check
    entirely - so the one case the guard is for, a model nobody has profiled,
    was the one case it did not cover.
    """
    known = RESIDENT_MB.get(model)
    if known:
        return known, "measured"
    try:
        tags = json.load(urllib.request.urlopen(API + "/api/tags", timeout=30))
        for m in tags.get("models", []):
            if m.get("name") == model:
                return round(m.get("size", 0) / 1024 / 1024), "disk size"
    except Exception as e:
        print(f"  (could not size {model}: {e})", flush=True)
    return None, "unknown"


def unload(model):
    """Ask Ollama to drop the model now, instead of after its idle timeout."""
    try:
        req = urllib.request.Request(
            API + "/api/generate",
            data=json.dumps({"model": model, "keep_alive": 0}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=60).read()
        time.sleep(2)
    except Exception as e:
        print(f"  (unload {model} failed: {e})", flush=True)

# One distractor set per QUIZ question, in the same order. Four options each, and
# the right answer sits in a different slot each time so position cannot be learned.
CHOICES = [
    ["404", "429", "500", "503"],
    ["1", "9", "15", "11"],
    ["53", "80", "443", "123"],
    ["3306", "5432", "1521", "27017"],
    ["30", "62", "64", "126"],
    ["4", "6", "7", "0"],
    ["GET", "PUT", "POST", "DELETE"],
    ["21", "22", "23", "25"],
]

JSON_SCHEMA = {"type": "object",
               "properties": {"answer": {"type": "string"}},
               "required": ["answer"]}


def enum_schema(options):
    return {"type": "object",
            "properties": {"answer": {"type": "string", "enum": list(options)}},
            "required": ["answer"]}


def gen(model, prompt, fmt=None, num_predict=64, think=False):
    """One generation.

    think=False matters more than it looks. qwen3.5:4b is a REASONING model: it
    writes its chain of thought to a separate `thinking` field and leaves
    `response` empty until that finishes. With num_predict=64 it never finishes,
    so the first sweep of this model scored 0/24 in ALL THREE modes - including
    free, which no model had ever failed. That was our harness, not the model.

    Leaving thinking on is also not a fair comparison against the five
    non-reasoning models here: measured on q1, thinking on took 53.55s and 313
    tokens against 0.67s and 4 tokens with it off, and both answered 429
    correctly. That is a reasoning-vs-not difference, not a constrained-decoding
    difference, and it would swamp the effect this file exists to measure.

    Verified harmless on llama3.2:3b and mistral:7b, which have no thinking to
    disable, so it is passed unconditionally rather than kept in a model list.
    """
    body = {"model": model, "prompt": prompt, "stream": False, "think": think,
            "options": {"temperature": 0, "seed": SEED, "num_predict": num_predict}}
    if fmt:
        body["format"] = fmt
    req = urllib.request.Request(API + "/api/generate",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    r = json.load(urllib.request.urlopen(req, timeout=TIMEOUT))
    wall = time.perf_counter() - t0
    return r, wall


def extract(reply, mode):
    """Pull the answer out. Constrained replies are JSON; free ones are prose."""
    if mode == "free":
        return reply
    try:
        return str(json.loads(reply).get("answer", ""))
    except Exception:
        return reply          # malformed despite the schema - worth seeing


def trial(model, i, question, expected, runs):
    modes = {
        "free": None,
        "json": JSON_SCHEMA,
        "enum": enum_schema(CHOICES[i]),
    }
    out = {m: {"wall": [], "tokens": [], "tps": [], "ok": [], "replies": []} for m in modes}
    for _ in range(runs):
        for m, fmt in modes.items():      # alternate inside each run, not in blocks
            r, wall = gen(model, question, fmt)
            ans = extract(r.get("response", ""), m)
            ev, ed = r.get("eval_count") or 0, r.get("eval_duration") or 1
            out[m]["wall"].append(wall)
            out[m]["tokens"].append(ev)
            out[m]["tps"].append(ev / (ed / 1e9))
            out[m]["ok"].append(bool(quiz_answer_ok(ans, expected)))
            out[m]["replies"].append(ans.strip()[:60])
    return out


def selftest():
    assert len(CHOICES) == len(QUIZ), "a choice set per question"
    for (q, expected), opts in zip(QUIZ, CHOICES):
        assert expected in opts, f"correct answer missing from options: {expected}"
        assert len(opts) == len(set(opts)) == 4, f"need 4 distinct options: {opts}"
    # the right answer must not sit in the same slot every time
    slots = {opts.index(e) for (_, e), opts in zip(QUIZ, CHOICES)}
    assert len(slots) > 1, "correct answer is always in the same position"
    assert quiz_answer_ok('{"answer": "429"}', "429")
    assert not quiz_answer_ok('{"answer": "4290"}', "429"), "must not credit a substring"
    assert enum_schema(["a", "b"])["properties"]["answer"]["enum"] == ["a", "b"]
    print(f"selftest ok ({len(QUIZ)} questions, 4 options each, "
          f"correct answer in slots {sorted(slots)})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="*", default=["llama3.2:3b"])
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--out", default="typed-decode.json")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        selftest()
        return

    results = []
    skipped = []
    loaded = None
    for model in a.models:
        # Unload the previous model first. Without this the sweep stacks models
        # and the later ones run under memory pressure the earlier ones never saw,
        # which would make the comparison between them meaningless.
        if loaded:
            unload(loaded)
        avail = mem_available_mb()
        need, src = resident_estimate_mb(model)
        if need and avail - need < FLOOR_MB:
            msg = (f"needs ~{need} MB resident ({src}), only {avail:.0f} MB "
                   f"available (floor {FLOOR_MB} MB)")
            print(f"\n{model}  SKIPPED - {msg}", flush=True)
            skipped.append({"model": model, "reason": msg,
                            "available_mb": round(avail), "resident_mb": need})
            continue

        print(f"\n{model}  warming up ... ({avail:.0f} MB available)", flush=True)
        gen(model, "warm", num_predict=1)
        loaded = model
        time.sleep(1)

        per = {m: {"wall": [], "tokens": [], "tps": [], "ok": []} for m in ("free", "json", "enum")}
        rows = []
        for i, (q, expected) in enumerate(QUIZ):
            t = trial(model, i, q, expected, a.runs)
            rows.append({"question": q, "expected": expected, "options": CHOICES[i],
                         "modes": {m: {k: v for k, v in d.items()} for m, d in t.items()}})
            for m in per:
                per[m]["wall"] += t[m]["wall"]
                per[m]["tokens"] += t[m]["tokens"]
                per[m]["tps"] += t[m]["tps"]
                per[m]["ok"] += t[m]["ok"]
            print(f"  q{i+1} {expected:>5}  " + "  ".join(
                f"{m}:{sum(t[m]['ok'])}/{a.runs}@{st.median(t[m]['wall']):.2f}s" for m in per), flush=True)

        summary = {m: {"median_wall_s": round(st.median(d["wall"]), 3),
                       "median_tokens": st.median(d["tokens"]),
                       "median_tok_per_s": round(st.median(d["tps"]), 2),
                       "correct": sum(d["ok"]), "attempts": len(d["ok"])}
                   for m, d in per.items()}
        results.append({"model": model, "runs_per_question": a.runs,
                        "summary": summary, "detail": rows})

        print(f"\n  {model}")
        print(f"  {'mode':6} {'med wall':>9} {'tokens':>7} {'tok/s':>7} {'correct':>9}")
        for m, s in summary.items():
            print(f"  {m:6} {s['median_wall_s']:>8.2f}s {s['median_tokens']:>7.0f} "
                  f"{s['median_tok_per_s']:>7.2f} {s['correct']:>5}/{s['attempts']}")

    if loaded:
        unload(loaded)

    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump({"api": API, "seed": SEED, "temperature": 0,
                   "note": "warm, modes alternate within each run, medians not means; "
                           "each model unloaded before the next so none runs under "
                           "pressure the others did not see",
                   "results": results, "skipped": skipped}, fh, indent=1)
    print(f"\nwrote {a.out}")
    for s in skipped:
        print(f"  SKIPPED {s['model']}: {s['reason']}")


if __name__ == "__main__":
    main()
