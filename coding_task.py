#!/usr/bin/env python3
"""
coding_task.py - what a real coding request costs on a CPU-only laptop.

    python coding_task.py --models qwen3.5:4b --out coding-task.json
    python coding_task.py --selftest

WHY THIS EXISTS, SEPARATELY FROM typed_decode.py. That file asks eight trivia
questions with one-token answers, which is the right shape for measuring a
decoding constraint and the wrong shape for answering "is this usable". A person
evaluating "free Claude Code" is not asking for an HTTP status code. They are
asking for a function, and they are going to sit and watch it arrive.

So: three prompts of the kind people actually send a coding agent, and the
number that decides everything - how long you wait.

WHAT IS AND IS NOT MEASURED. Wall time, tokens, and tokens/sec are measured.
Whether the code is CORRECT is checked only for task 1, where a deterministic
assertion is possible. Tasks 2 and 3 are recorded verbatim for human reading -
this script does not grade prose, and a script that pretended to would be worse
than one that admits it.

Same discipline as the other harnesses: warm before timing, temperature 0, fixed
seed, medians not means, model unloaded at the end.
"""

import argparse
import json
import os
import re
import shutil
import statistics as st
import subprocess
import sys
import threading
import time
import urllib.request

API = "http://127.0.0.1:11434"
SEED = 7
TIMEOUT = 900
FLOOR_MB = 900

TASKS = [
    {
        "id": "parse_duration",
        "prompt": (
            "Write a Python function `parse_duration(s)` that converts strings like "
            "'1h30m', '45s', '2h', '90m' into an integer number of seconds. "
            "Return only the function, no explanation."
        ),
        # Deterministic check: extract the function and actually run it.
        "cases": [("1h30m", 5400), ("45s", 45), ("2h", 7200), ("90m", 5400)],
    },
    {
        "id": "fix_bug",
        "prompt": (
            "This Python function is meant to return the second largest unique number "
            "in a list, or None if there isn't one. It has a bug. Return the corrected "
            "function only, no explanation.\n\n"
            "def second_largest(nums):\n"
            "    nums = sorted(nums, reverse=True)\n"
            "    return nums[1]\n"
        ),
        "cases": None,
    },
    {
        "id": "explain_regex",
        "prompt": (
            "Explain what this regex matches, in two sentences, plainly: "
            r"^(?:[a-z0-9!#$%&'*+/=?^_`{|}~-]+)@(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$"
        ),
        "cases": None,
    },
]


def mem_available_mb():
    import psutil
    return psutil.virtual_memory().available / 1024 / 1024


def require_ollama():
    """Fail with one readable line instead of a urllib traceback.

    The first run of this script died mid-benchmark with RemoteDisconnected,
    because an Ollama updater (OllamaSetup.exe) had shut the server down between
    the warm-up call and the first real one. That is a one-sentence problem and
    it printed twenty lines of stack, which is how a trivial fault gets mistaken
    for a broken harness.
    """
    try:
        urllib.request.urlopen(API + "/api/tags", timeout=10).read()
    except Exception as e:
        sys.exit(f"Ollama is not reachable at {API} ({type(e).__name__}).\n"
                 f"Start it, or wait for any running OllamaSetup.exe to finish, "
                 f"then re-run.")


def gen(model, prompt, num_predict=1024):
    body = {"model": model, "prompt": prompt, "stream": False, "think": False,
            "options": {"temperature": 0, "seed": SEED, "num_predict": num_predict}}
    req = urllib.request.Request(API + "/api/generate",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    r = json.load(urllib.request.urlopen(req, timeout=TIMEOUT))
    return r, time.perf_counter() - t0


def build_identity(model):
    """Which build of `model` this run measured.

    Added 7 Oct. coding-task.json originally recorded the model NAME only. Ollama then
    republished the qwen3.5:4b tag (3 layers / 3.39 GB -> 5 layers / 3.32 GB) between the
    run and the video, and the file could not say whether the re-pulled model was the one
    it had measured. A tag is not an identity; the digest is.
    """
    def get(path):
        return json.loads(urllib.request.urlopen(API + path, timeout=10).read())
    tag = next((m for m in get("/api/tags").get("models", [])
                if m.get("name") == model or m.get("model") == model), None)
    return {"digest": tag.get("digest") if tag else None,
            "size_bytes": tag.get("size") if tag else None,
            "modified_at": tag.get("modified_at") if tag else None,
            "ollama_version": get("/api/version").get("version")}


def unload(model):
    try:
        req = urllib.request.Request(
            API + "/api/generate",
            data=json.dumps({"model": model, "keep_alive": 0}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=60).read()
    except Exception as e:
        print(f"  (unload failed: {e})", flush=True)


def extract_code(text):
    """Pull the first fenced block, or the whole reply if it isn't fenced."""
    m = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.S)
    return (m.group(1) if m else text).strip()


def check_parse_duration(code, cases):
    """Run the model's function against known cases in a subprocess.

    Subprocess, not exec(), because this is model-generated code and it should
    not be able to touch this process. It still runs on this machine - the
    prompt asks for a pure string-to-int function and the cases are fixed, but
    that is a judgement about likelihood, not a sandbox.
    """
    harness = code + "\n\nimport json,sys\n" + \
        f"print(json.dumps([parse_duration(c) for c,_ in {cases!r}]))\n"
    try:
        p = subprocess.run([sys.executable, "-c", harness],
                           capture_output=True, text=True, timeout=20)
        if p.returncode != 0:
            return False, (p.stderr.strip().splitlines() or ["error"])[-1][:120]
        got = json.loads(p.stdout.strip().splitlines()[-1])
        want = [v for _, v in cases]
        return got == want, f"got {got}, want {want}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:120]


def selftest():
    good = "def parse_duration(s):\n    import re\n    t={'h':3600,'m':60,'s':1}\n" \
           "    return sum(int(n)*t[u] for n,u in re.findall(r'(\\d+)([hms])', s))\n"
    ok, why = check_parse_duration(good, TASKS[0]["cases"])
    assert ok, f"known-good function failed the checker: {why}"
    bad = "def parse_duration(s):\n    return 0\n"
    ok2, _ = check_parse_duration(bad, TASKS[0]["cases"])
    assert not ok2, "checker passed a function that returns 0 for everything"
    assert extract_code("```python\nx=1\n```") == "x=1"
    assert extract_code("no fence here") == "no fence here"
    print("selftest ok (checker accepts a correct function, rejects a wrong one)")


def enable_vt():
    """Windows consoles ignore ESC sequences until ENABLE_VIRTUAL_TERMINAL_PROCESSING is set;
    the 7 Oct take printed no clock at all because nothing set it. No-op elsewhere."""
    if os.name != "nt":
        return
    import ctypes
    k = ctypes.windll.kernel32
    h, mode = k.GetStdHandle(-11), ctypes.c_ulong()
    ok = k.GetConsoleMode(h, ctypes.byref(mode))
    set_ok = ok and k.SetConsoleMode(h, mode.value | 0x0004)


class LiveClock:
    """A real-time clock pinned to the top-right corner of the console (VT escape codes).

    The video's point in this scene is the WAIT, not the text, so the clock has to be real:
    it is driven by perf_counter while the request is in flight, never faked or sped up.
    All terminal writes go through one lock so the clock thread cannot interleave its
    escape sequences into the middle of a printed line.
    """
    lock = threading.Lock()

    def __init__(self):
        self.t0 = time.perf_counter()
        self.frozen = None
        self.cols = shutil.get_terminal_size((100, 30)).columns
        enable_vt()

    def elapsed(self):
        return self.frozen if self.frozen is not None else time.perf_counter() - self.t0

    def tick(self):
        text = f" clock {self.elapsed():5.1f} s "
        with LiveClock.lock:
            sys.stdout.write(f"\x1b7\x1b[1;{max(1, self.cols - len(text))}H\x1b[7m{text}\x1b[0m\x1b8")
            sys.stdout.flush()

    def freeze(self):
        self.frozen = time.perf_counter() - self.t0
        self.tick()


def emit(s="", end="\n"):
    with LiveClock.lock:
        sys.stdout.write(s + end)
        sys.stdout.flush()


def live_gen(model, prompt, clock):
    box = {}

    def work():
        try:
            box["res"] = gen(model, prompt)
        except Exception as e:      # surface it on the main thread instead of hanging the clock
            box["err"] = e
    th = threading.Thread(target=work)
    th.start()
    while th.is_alive():
        clock.tick()
        time.sleep(0.1)
    th.join()
    clock.tick()
    if "err" in box:
        raise box["err"]
    return box["res"]


def run_live(models, runs, out):
    """The benchmark, but shown. Same API call, settings and statistics as main(); the only
    differences are DISPLAY and ORDER: run-major in the order the narration says them (fix a
    bug, explain a regex, write a function), where main() is task-major. Replies are
    deterministic (temperature 0, fixed seed), so the order cannot change what is said;
    it can move timings by a fraction of a second, which is why the artifact records it."""
    os.system("")                                   # enables VT escape handling on Windows consoles
    order = ["fix_bug", "explain_regex", "parse_duration"]
    by_id = {t["id"]: t for t in TASKS}
    results = []
    for model in models:
        ident = build_identity(model)
        emit(f"{model}   digest {ident['digest'][:12]}   Ollama {ident['ollama_version']}   16 GB, CPU only")
        emit("warming up the model ...", end=" ")
        t0 = time.perf_counter()
        gen(model, "warm", num_predict=1)
        emit(f"{time.perf_counter() - t0:.1f} s")
        time.sleep(1)

        data = {tid: {"walls": [], "toks": [], "tps": [], "oks": [], "replies": []} for tid in order}
        for run in range(1, runs + 1):
            if run > 1:
                emit("\x1b[2J\x1b[H", end="")       # a clean screen per pass keeps the rows readable
            emit(f"== run {run} of {runs} " + "=" * 50)
            clock = LiveClock()
            run_total = 0.0
            for i, tid in enumerate(order, 1):
                t = by_id[tid]
                if run == 1:
                    first = t["prompt"].split("\n")[0]
                    emit(f"[{i}/3] {tid}")
                    emit(f"  > {first[:88]}{'...' if len(first) > 88 else ''}")
                else:
                    emit(f"[{i}/3] {tid}", end="  ")
                r, wall = live_gen(model, t["prompt"], clock)
                text = r.get("response", "")
                ev, ed = r.get("eval_count") or 0, r.get("eval_duration") or 1
                d = data[tid]
                d["walls"].append(wall); d["toks"].append(ev); d["tps"].append(ev / (ed / 1e9)); d["replies"].append(text)
                if t["cases"]:
                    ok, _ = check_parse_duration(extract_code(text), t["cases"])
                    d["oks"].append(ok)
                run_total += wall
                if run == 1:
                    body = [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("```")]
                    for ln in body[:3]:
                        emit(f"  < {ln[:90]}")
                    if len(body) > 3:
                        emit(f"  < ... ({len(body)} lines)")
                    emit(f"  {wall:5.2f} s   {ev} tokens   {ev / (ed / 1e9):.1f} tok/s")
                else:
                    emit(f"{wall:5.2f} s")
            clock.freeze()
            emit(f"  run {run} total: {run_total:.2f} s")
            time.sleep(2.5 if run == 1 else 0.5)

        rows = []
        for t in TASKS:                              # artifact keeps TASKS order, as main() writes it
            d = data[t["id"]]
            rows.append({"task": t["id"],
                         "median_wall_s": round(st.median(d["walls"]), 2),
                         "median_tokens": st.median(d["toks"]),
                         "median_tok_per_s": round(st.median(d["tps"]), 2),
                         "correct": (f"{sum(d['oks'])}/{len(d['oks'])}" if d["oks"] else "not auto-graded"),
                         "replies": [x[:900] for x in d["replies"]]})
        total = sum(r["median_wall_s"] for r in rows)
        emit("\x1b[2J\x1b[H", end="")
        emit(f"== median of {runs} runs " + "=" * 45)
        for tid in order:
            r = next(x for x in rows if x["task"] == tid)
            extra = f"   fully correct in {r['correct']} runs" if tid == "parse_duration" else ""
            emit(f"  {tid:<15} {r['median_wall_s']:6.2f} s{extra}")
        emit(f"  {'TOTAL':<15} {total:6.2f} s   for 3 requests")
        emit(f"  {model}  digest {ident['digest'][:12]}  Ollama {ident['ollama_version']}")
        results.append({"model": model, "build": ident, "runs_per_task": runs,
                        "total_median_wall_s": round(total, 2), "tasks": rows,
                        "live": True, "order": "run-major, in narration order: " + ", ".join(order)})
        unload(model)

    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"api": API, "seed": SEED, "temperature": 0, "think": False,
                   "note": "live capture of the benchmark: same call and settings, run-major order; "
                           "warm before timing; medians not means; only parse_duration is auto-graded",
                   "results": results}, fh, indent=1)
    emit(f"\nwrote {out}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="*", default=["qwen3.5:4b"])
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--out", default="coding-task.json")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--live", action="store_true",
                    help="show the run with a real-time clock (this is what the video films)")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    require_ollama()
    if a.live:
        return run_live(a.models, a.runs, a.out)

    results = []
    for model in a.models:
        avail = mem_available_mb()
        print(f"\n{model}  warming up ... ({avail:.0f} MB available)", flush=True)
        gen(model, "warm", num_predict=1)
        time.sleep(1)

        rows = []
        for t in TASKS:
            walls, toks, tps, oks, replies = [], [], [], [], []
            for _ in range(a.runs):
                r, wall = gen(model, t["prompt"])
                text = r.get("response", "")
                ev, ed = r.get("eval_count") or 0, r.get("eval_duration") or 1
                walls.append(wall); toks.append(ev); tps.append(ev / (ed / 1e9))
                replies.append(text)
                if t["cases"]:
                    ok, _ = check_parse_duration(extract_code(text), t["cases"])
                    oks.append(ok)
            row = {"task": t["id"],
                   "median_wall_s": round(st.median(walls), 2),
                   "median_tokens": st.median(toks),
                   "median_tok_per_s": round(st.median(tps), 2),
                   "correct": (f"{sum(oks)}/{len(oks)}" if oks else "not auto-graded"),
                   "replies": [x[:900] for x in replies]}
            rows.append(row)
            print(f"  {t['id']:16} {row['median_wall_s']:>7.2f}s  "
                  f"{row['median_tokens']:>5.0f} tok  {row['median_tok_per_s']:>6.2f} tok/s  "
                  f"{row['correct']}", flush=True)

        total = sum(r["median_wall_s"] for r in rows)
        print(f"  {'TOTAL':16} {total:>7.2f}s for {len(rows)} requests")
        results.append({"model": model, "build": build_identity(model),
                        "runs_per_task": a.runs,
                        "total_median_wall_s": round(total, 2), "tasks": rows})
        unload(model)

    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump({"api": API, "seed": SEED, "temperature": 0, "think": False,
                   "note": "warm before timing; medians not means; only parse_duration "
                           "is auto-graded, the rest are recorded for human reading",
                   "results": results}, fh, indent=1)
    print(f"\nwrote {a.out}")


if __name__ == "__main__":
    main()
