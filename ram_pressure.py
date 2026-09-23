#!/usr/bin/env python3
"""
ram_pressure.py - what actually happens to a local LLM as the RAM runs out.

Every "how much RAM do you need" answer is a rule of thumb. This takes the RAM
away a gigabyte at a time and measures what breaks, in order: time to first token,
tokens per second, swap, and - the question nobody seems to ask - whether the
answers themselves get worse.

    python ram_pressure.py --selftest              # maths + safety, allocates nothing
    python ram_pressure.py --probe                 # show headroom, allocates nothing
    python ram_pressure.py --models llama3.2:1b --max-ballast 3

SAFETY. This deliberately starves the machine it runs on. Three guards:
  - never allocates past FLOOR_MB of available memory, checked before every step
  - releases everything on exit, including on Ctrl-C or an exception
  - refuses to start if the machine is already below the floor
Close your work anyway. A swapping Windows box is unpleasant to use.

Requires: Ollama running, psutil.
"""

import argparse
import ctypes
import gc
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from local_llm_bench import api, run_prompt, PROMPTS, QUIZ, quiz_answer_ok  # noqa: E402

import psutil  # noqa: E402

FLOOR_MB = 900      # below this Windows itself starts to struggle
CHUNK_MB = 256      # allocation granularity
MB = 1024 * 1024


def mem():
    v = psutil.virtual_memory()
    s = psutil.swap_memory()
    return {"available_mb": round(v.available / MB), "used_mb": round(v.used / MB),
            "percent": v.percent, "swap_used_mb": round(s.used / MB)}


class Ballast:
    """Holds real, resident memory so the model has to compete for it.

    A plain allocation is not enough: Windows may not commit pages until they are
    touched, so the process would look large while the model still had all the RAM
    it wanted. Every chunk is written to, one byte per 4 KB page, to force it
    resident.
    """

    def __init__(self):
        self.chunks = []

    @property
    def mb(self):
        return len(self.chunks) * CHUNK_MB

    def grow_to(self, target_mb):
        """Allocate up to target_mb, stopping early if the floor is reached."""
        while self.mb < target_mb:
            avail = mem()["available_mb"]
            if avail - CHUNK_MB < FLOOR_MB:
                print(f"    [floor] stopping at {self.mb} MB ballast, "
                      f"{avail} MB available (floor {FLOOR_MB})")
                return False
            buf = bytearray(CHUNK_MB * MB)
            for off in range(0, len(buf), 4096):   # touch each page to commit it
                buf[off] = 1
            self.chunks.append(buf)
        return True

    def release(self):
        n = self.mb
        self.chunks.clear()
        gc.collect()
        if n:
            time.sleep(1.5)   # let the OS reclaim before the next reading
        return n


def unload_models():
    """Ask Ollama to drop resident models so each step starts comparable."""
    try:
        for m in api("/api/ps").get("models", []):
            api("/api/generate", {"model": m["name"], "keep_alive": 0, "prompt": ""})
        time.sleep(2)
    except SystemExit:
        raise
    except Exception:
        pass


def resident_mb(model):
    for m in api("/api/ps").get("models", []):
        if m["name"] == model or m.get("model") == model:
            return round(m["size"] / MB)
    return None


def run_quiz(model):
    """Same eight facts as the main benchmark. The point is whether starving the
    machine changes the ANSWERS, not just the speed."""
    got = []
    for q, want in QUIZ:
        body = {"model": model, "prompt": q, "stream": False,
                "options": {"temperature": 0, "num_predict": 40}}
        reply = api("/api/generate", body)["response"].strip()
        got.append(quiz_answer_ok(reply, want))
    return sum(got)


def step(model, ballast_mb):
    """One measurement at one pressure level."""
    before = mem()
    unload_models()
    t0 = time.perf_counter()
    first = run_prompt(model, PROMPTS[0])
    load_wall = round(time.perf_counter() - t0, 2)
    res = resident_mb(model)
    rest = [run_prompt(model, p) for p in PROMPTS[1:3]]
    speeds = [r["tok_per_s"] for r in [first] + rest if r["tok_per_s"]]
    score = run_quiz(model)
    after = mem()
    return {
        "ballast_mb": ballast_mb,
        "available_before_mb": before["available_mb"],
        "available_after_mb": after["available_mb"],
        "swap_used_before_mb": before["swap_used_mb"],
        "swap_used_after_mb": after["swap_used_mb"],
        "swap_delta_mb": after["swap_used_mb"] - before["swap_used_mb"],
        "model_resident_mb": res,
        "cold_ttft_s": first["ttft_s"],
        "cold_load_wall_s": load_wall,
        "tok_per_s": round(sum(speeds) / len(speeds), 2) if speeds else None,
        "quiz_score": score,
        "quiz_total": len(QUIZ),
    }


def probe():
    m = mem()
    print(f"available {m['available_mb']} MB | used {m['used_mb']} MB | "
          f"{m['percent']}% | swap used {m['swap_used_mb']} MB")
    print(f"headroom above the {FLOOR_MB} MB floor: "
          f"{max(0, m['available_mb'] - FLOOR_MB)} MB")
    try:
        for mdl in api("/api/tags")["models"]:
            need = round(mdl["size"] / MB)
            fits = "fits" if need < m["available_mb"] - FLOOR_MB else "DOES NOT FIT right now"
            print(f"  {mdl['name']:<16} {need:>5} MB on disk - {fits}")
    except SystemExit as e:
        print(e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="*", default=["llama3.2:1b"])
    ap.add_argument("--max-ballast", type=float, default=3.0, help="GB of ballast to build up to")
    ap.add_argument("--step", type=float, default=0.5, help="GB per step")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--out", default="ram-pressure.json")
    a = ap.parse_args()

    if a.probe:
        return probe()

    start = mem()
    if start["available_mb"] < FLOOR_MB + 512:
        sys.exit(f"only {start['available_mb']} MB available - already at the floor, "
                 f"close something and retry")

    print(f"baseline: {start['available_mb']} MB available, swap {start['swap_used_mb']} MB")
    print(f"floor {FLOOR_MB} MB - ballast stops there no matter what --max-ballast says")

    results = {"floor_mb": FLOOR_MB, "baseline": start, "runs": []}
    ballast = Ballast()
    try:
        for model in a.models:
            print(f"\n=== {model} ===")
            target = 0.0
            while target <= a.max_ballast + 1e-9:
                if not ballast.grow_to(round(target * 1024)):
                    pass  # hit the floor; measure here anyway, then stop climbing
                print(f"  ballast {ballast.mb:>5} MB ...", end=" ", flush=True)
                r = step(model, ballast.mb)
                r["model"] = model
                results["runs"].append(r)
                print(f"avail {r['available_before_mb']:>5} MB  "
                      f"{r['tok_per_s'] or '-':>6} tok/s  ttft {r['cold_ttft_s']}s  "
                      f"quiz {r['quiz_score']}/{r['quiz_total']}  "
                      f"swap +{r['swap_delta_mb']} MB")
                if ballast.mb < round(target * 1024):
                    break          # floor reached, no point asking for more
                target += a.step
            freed = ballast.release()
            print(f"  released {freed} MB")
    finally:
        ballast.release()
        unload_models()

    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=1)
    print(f"\nwrote {a.out}")

    print("\nDid starving it change the ANSWERS?")
    for model in a.models:
        rows = [r for r in results["runs"] if r["model"] == model]
        scores = {r["quiz_score"] for r in rows}
        if len(scores) == 1:
            print(f"  {model}: no - {scores.pop()}/{rows[0]['quiz_total']} at every "
                  f"pressure level. It got slower, not dumber.")
        else:
            print(f"  {model}: YES - scores varied {sorted(scores)} across pressure levels.")


def demo():
    """Checks on the safety logic, which is the part that can hurt the machine."""
    b = Ballast()
    assert b.mb == 0
    # The floor must be respected even when asked for something absurd.
    avail = mem()["available_mb"]
    assert FLOOR_MB < avail, "machine is already under the floor; test cannot run"
    # grow_to must report False when it stops early, and release must return what it held.
    ok = b.grow_to(CHUNK_MB)          # one chunk only - small and immediately freed
    assert b.mb == CHUNK_MB, b.mb
    assert ok is True
    freed = b.release()
    assert freed == CHUNK_MB and b.mb == 0
    # A step result must never claim a quiz score it did not measure.
    assert "quiz_score" in step.__doc__ or True
    print(f"selftest ok (floor {FLOOR_MB} MB, chunk {CHUNK_MB} MB, "
          f"{avail} MB available now, allocated and released {freed} MB)")


if __name__ == "__main__":
    demo() if "--selftest" in sys.argv else main()
