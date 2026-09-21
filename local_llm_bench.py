#!/usr/bin/env python3
"""
local_llm_bench.py - what a local LLM actually costs on the machine you own.

Every "run an LLM locally" tutorial stops at "it works". This measures what
happens next: how fast it runs, how much power it draws, and how many tokens a
month you need before self-hosting beats paying an API.

    python local_llm_bench.py --list                 # models Ollama has
    python local_llm_bench.py --models llama3.2:1b   # benchmark
    python local_llm_bench.py --selftest             # check the maths, no model needed

Measures per model:
  - tokens/sec (generation), time to first token
  - resident model size (Ollama /api/ps) and peak CPU
  - power draw in watts, differenced from the battery energy counter (ON BATTERY)
  - sustained-run throughput, so thermal throttling shows up as a decline

Then prices it: energy per million tokens at your electricity tariff, against
published API rates in api_rates.json.

Requires: Ollama running locally, psutil. No API keys, no cloud spend.
"""

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
HERE = os.path.dirname(os.path.abspath(__file__))
NS = 1_000_000_000  # Ollama reports durations in nanoseconds

# Fixed workload. Same prompts for every model, temperature 0, capped output —
# so the only variable is the model.
PROMPTS = [
    "Explain what a reverse proxy does, in one paragraph.",
    "Write a bash one-liner that finds the 10 largest files under /var/log.",
    "A server's disk fills up every Tuesday at 3am. List four things you would check, in order.",
    "Summarise the difference between a container image and a running container.",
    "Write a Python function that retries a call with exponential backoff.",
]
NUM_PREDICT = 300


# A deterministic correctness floor. Not a benchmark - eight IT-ops facts with one
# right answer each, checked by exact token match so no grader model and no judgment
# call sits between the model and the score. A model that fails these is not a model
# you would leave running your runbook.
QUIZ = [
    ("Which HTTP status code means Too Many Requests? Reply with only the number.", "429"),
    ("On Linux, what is the signal NUMBER for SIGKILL? Reply with only the number.", "9"),
    ("What is the default port for DNS? Reply with only the number.", "53"),
    ("What is the default port for PostgreSQL? Reply with only the number.", "5432"),
    ("How many usable host addresses does an IPv4 /26 subnet have? Reply with only the number.", "62"),
    ("In chmod 640, what is the octal digit for group permissions? Reply with only the digit.", "4"),
    ("Which one of GET, PUT, POST, DELETE is NOT idempotent? Reply with only that word.", "POST"),
    ("What is the default port for SSH? Reply with only the number.", "22"),
]


def quiz_answer_ok(reply, expected):
    """Exact token match, so a model cannot score by rambling past the answer.

    Digits are matched as whole numbers - '429' must not be credited for '4290'
    or for a stray '9' from SIGKILL. Words are matched case-insensitively.
    """
    toks = re.findall(r"[A-Za-z]+|\d+", reply)
    return any(t.upper() == expected.upper() for t in toks)


def quiz(models):
    print()
    print("CORRECTNESS - eight IT-ops facts, exact-answer scoring")
    scores = {}
    for m in models:
        got = []
        for q, want in QUIZ:
            body = {"model": m, "prompt": q, "stream": False,
                    "options": {"temperature": 0, "num_predict": 40}}
            reply = api("/api/generate", body)["response"].strip()
            ok = quiz_answer_ok(reply, want)
            got.append(ok)
            print(f"  {m:<16} {'PASS' if ok else 'FAIL':<5} want {want:<5} got: {reply[:60]!r}")
        scores[m] = f"{sum(got)}/{len(QUIZ)}"
        print(f"  {m:<16} SCORE {scores[m]}")
        print()
    return scores


def api(path, payload=None, stream=False, timeout=600):
    url = f"{OLLAMA}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.URLError as e:
        sys.exit(f"Cannot reach Ollama at {OLLAMA} ({e}). Is it running? Try: ollama serve")
    return r if stream else json.load(r)


def battery_mwh():
    """Energy left in the battery, in mWh.

    A counter, not a rate. Differencing it over a timed window gives real average
    power. Windows' DischargeRate was tried first and rejected: on this i5-1145G7
    an all-core burn moved it by -0.8 W, because it is heavily smoothed and tracks
    the display more than the CPU. A counter cannot fail that way.

    Returns None when plugged in - the counter stops falling, and calling that zero
    draw would silently make inference look free.
    """
    ps = (r"try { $b = Get-CimInstance -Namespace root\WMI -ClassName BatteryStatus -EA Stop; "
          r'"$($b.RemainingCapacity)|$($b.PowerOnline)" } catch { ' "'NA|NA' }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=15).stdout.strip()
        mwh, online = out.split("|")
        if mwh in ("NA", "") or online.strip().lower() in ("true", "1"):
            return None
        return float(mwh)
    except Exception:
        return None


_UNSET = object()  # distinguishes "read it live" from "there was no reading"


MIN_POWER_WINDOW_S = 300


def watts_between(start_mwh, start_t, end_mwh=_UNSET, end_t=None, min_window_s=MIN_POWER_WINDOW_S):
    """Average watts since (start_mwh, start_t), or None if it cannot be defended.

    Three ways to get None, all deliberate:
      - no reading at either end (plugged in)
      - the counter never moved
      - the window was shorter than min_window_s

    The window floor exists because a short run is dominated by transients: loading
    the model off disk, CPU boost clocks, whatever else the OS was doing. A 2-minute
    measurement of llama3.2:1b read 22.5 W against 10.0 W for the larger 3B over 8
    minutes - the small model cannot really cost more to run, the window was just too
    short to average. Refusing to answer beats publishing that.

    The end reading is a parameter so the maths can be tested without a draining battery.
    """
    if end_mwh is _UNSET:
        end_mwh = battery_mwh()
    if end_t is None:
        end_t = time.perf_counter()
    if start_mwh is None or end_mwh is None:
        return None
    drop_mwh, seconds = start_mwh - end_mwh, end_t - start_t
    if drop_mwh <= 0 or seconds < min_window_s:
        return None
    return round(drop_mwh / 1000 / (seconds / 3600), 1)


class Sampler(threading.Thread):
    """Samples RAM and CPU while a model generates.

    Power is deliberately NOT sampled here. It is read once at each end of the run
    from the battery's energy counter, because an instantaneous rate on this
    hardware is too smoothed to resolve a single model.
    """

    def __init__(self, interval=2.0):
        super().__init__(daemon=True)
        self.interval, self.stop_flag = interval, threading.Event()
        self.ram_mb, self.cpu = [], []

    def run(self):
        import psutil
        psutil.cpu_percent(interval=None)
        while not self.stop_flag.is_set():
            self.ram_mb.append(psutil.virtual_memory().used / 1024 / 1024)
            self.cpu.append(psutil.cpu_percent(interval=None))
            self.stop_flag.wait(self.interval)

    def stop(self):
        self.stop_flag.set()
        self.join(timeout=10)
        return {"system_ram_peak_mb": round(max(self.ram_mb)) if self.ram_mb else None,
                "cpu_avg_pct": round(statistics.fmean(self.cpu), 1) if self.cpu else None}


def run_prompt(model, prompt):
    """One generation. Returns Ollama's own token counts plus measured TTFT."""
    body = {"model": model, "prompt": prompt, "stream": True,
            "options": {"temperature": 0, "num_predict": NUM_PREDICT}}
    t0 = time.perf_counter()
    ttft, final = None, None
    r = api("/api/generate", body, stream=True)
    for line in r:
        if not line.strip():
            continue
        chunk = json.loads(line)
        if ttft is None and chunk.get("response"):
            ttft = time.perf_counter() - t0
        if chunk.get("done"):
            final = chunk
    if not final:
        sys.exit(f"{model}: no completion returned")
    gen_tok = final.get("eval_count", 0)
    gen_s = final.get("eval_duration", 0) / NS
    return {"ttft_s": round(ttft, 2) if ttft else None,
            "gen_tokens": gen_tok,
            "tok_per_s": round(gen_tok / gen_s, 2) if gen_s else None,
            "prompt_tokens": final.get("prompt_eval_count", 0),
            "load_s": round(final.get("load_duration", 0) / NS, 2),
            "total_s": round(final.get("total_duration", 0) / NS, 2)}


def energy_per_mtok(watts, tok_per_s):
    """kWh to generate one million tokens at this speed and power draw."""
    if not watts or not tok_per_s:
        return None
    seconds = 1_000_000 / tok_per_s
    return watts * seconds / 3600 / 1000


IDLE_SECONDS = 300


def idle_baseline(seconds=IDLE_SECONDS):
    """Watts the laptop draws doing nothing - screen, OS, everything else.

    Inference is charged only the delta above this; total draw would bill the model
    for the screen being on, which it did not cause. The window is long because the
    counter has ~10 mWh granularity: a short window rounds to nothing.
    """
    print(f"measuring idle baseline for {seconds}s (do not touch the laptop)...", end=" ", flush=True)
    start, t0 = battery_mwh(), time.perf_counter()
    time.sleep(seconds)
    idle = watts_between(start, t0)
    print(f"{idle} W" if idle else "no battery data (plugged in?)")
    return idle


def bench(models, tariff, runs, idle=None):
    results = []
    for m in models:
        print(f"\n=== {m} ===")
        mwh0, t0 = battery_mwh(), time.perf_counter()
        s = Sampler(); s.start()
        per_run = []
        for i in range(runs):
            for p in PROMPTS:
                r = run_prompt(m, p)
                per_run.append(r)
                print(f"  run {i+1} {r['tok_per_s'] or '?':>6} tok/s  ttft {r['ttft_s']}s  {p[:40]}")
        total_watts = watts_between(mwh0, t0)
        window_s = round(time.perf_counter() - t0)
        ps = api("/api/ps").get("models", [])
        loaded = next((x for x in ps if x["name"] == m or x["model"] == m), None)
        sysm = s.stop()
        sysm["watts_total"] = total_watts
        sysm["model_resident_mb"] = round(loaded["size"] / 1024 / 1024) if loaded else None
        speeds = [r["tok_per_s"] for r in per_run if r["tok_per_s"]]
        ttfts = [r["ttft_s"] for r in per_run if r["ttft_s"]]
        first_half = speeds[:len(speeds)//2] or speeds
        second_half = speeds[len(speeds)//2:] or speeds
        marginal = (round(total_watts - idle, 1)
                    if total_watts and idle and total_watts > idle else None)
        kwh = energy_per_mtok(marginal, statistics.fmean(speeds)) if speeds else None
        results.append({
            "model": m, "runs": len(per_run),
            "tok_per_s_median": round(statistics.median(speeds), 2) if speeds else None,
            "tok_per_s_first_half": round(statistics.fmean(first_half), 2) if speeds else None,
            "tok_per_s_second_half": round(statistics.fmean(second_half), 2) if speeds else None,
            "throttle_pct": round((1 - statistics.fmean(second_half) / statistics.fmean(first_half)) * 100, 1)
                            if speeds and statistics.fmean(first_half) else None,
            "ttft_median_s": round(statistics.median(ttfts), 2) if ttfts else None,
            "idle_watts": idle, "marginal_watts": marginal, "power_window_s": window_s,
            "hours_per_mtok": round(1_000_000 / statistics.fmean(speeds) / 3600, 1) if speeds else None,
            "kwh_per_mtok": round(kwh, 4) if kwh else None,
            "energy_cost_per_mtok": round(kwh * tariff, 4) if kwh else None,
            **sysm})
    return results


def report(results, tariff, currency):
    rates_path = os.path.join(HERE, "api_rates.json")
    rates = json.load(open(rates_path, encoding="utf-8")) if os.path.exists(rates_path) else None
    fx = rates["usd_to_myr"] if rates else 1.0

    print()
    print(f"{'MODEL':<20}{'tok/s':>8}{'TTFT':>7}{'W tot':>7}{'W net':>7}{'MODEL MB':>10}{'THROTTLE':>10}{'HRS/Mtok':>10}{'ENERGY/Mtok':>13}")
    print("-" * 92)
    for r in results:
        cost = f"{currency}{r['energy_cost_per_mtok']:.3f}" if r["energy_cost_per_mtok"] else "no battery"
        print(f"{r['model']:<20}{r['tok_per_s_median'] or '-':>8}{r['ttft_median_s'] or '-':>7}"
              f"{r['watts_total'] or '-':>7}{r['marginal_watts'] or '-':>7}{r['model_resident_mb'] or '-':>10}"
              f"{(str(r['throttle_pct']) + '%') if r['throttle_pct'] is not None else '-':>10}"
              f"{r['hours_per_mtok'] or '-':>10}{cost:>13}")

    if not any(r["watts_total"] for r in results):
        print()
        print("NO POWER DATA - laptop plugged in, or the run was too short to move the counter.")

    if not rates:
        return

    print()
    print(f"One million output tokens, bought instead (list prices, verified "
          f"{rates['verified']}, at {currency}{fx}/USD):")
    for name, v in rates["rates"].items():
        usd = v["output_per_mtok"]
        print(f"  {name:<20} ${usd:>5.2f}  = {currency}{usd * fx:>6.2f}   [{v['source']}]")

    cheapest = min(v["output_per_mtok"] for v in rates["rates"].values()) * fx
    for r in results:
        if not r["energy_cost_per_mtok"]:
            continue
        print()
        print(f"{r['model']}: electricity is {cheapest / r['energy_cost_per_mtok']:.1f}x cheaper "
              f"per token than the cheapest API,")
        print(f"  but costs {r['hours_per_mtok']} hours of this laptop at {r['cpu_avg_pct']}% CPU "
              f"to produce them.")

    print()
    print("Watts are the MARGINAL draw above idle, so the model is not billed for the screen.")
    print("Electricity is the only cost measured here. Hardware, your time, and the fact that the")
    print("laptop is unusable while it runs are not in that number.")

def demo():
    """Self-check on the maths that cannot be eyeballed later."""
    # 10 W sustained at 5 tok/s: 1M tokens takes 200,000 s = 55.56 h -> 0.5556 kWh
    kwh = energy_per_mtok(10, 5)
    assert abs(kwh - 0.5556) < 0.001, kwh
    # Twice the speed, half the energy
    assert abs(energy_per_mtok(10, 10) - kwh / 2) < 1e-9
    # Plugged in (no watts) must yield None, never 0 — a 0 would read as free power
    assert energy_per_mtok(None, 5) is None and energy_per_mtok(0, 5) is None

    # Counter maths: 5000 mWh spent over 1 hour is 5 W.
    assert watts_between(10000.0, 0.0, end_mwh=5000.0, end_t=3600.0) == 5.0
    # A window the counter never resolved must be None, not 0.0 W.
    assert watts_between(10000.0, 0.0, end_mwh=10000.0, end_t=3600.0) is None
    # A window too short to average out model load and boost clocks must be refused,
    # however plausible the arithmetic looks.
    assert watts_between(10000.0, 0.0, end_mwh=9250.0, end_t=120.0) is None
    assert watts_between(10000.0, 0.0, end_mwh=9250.0, end_t=301.0) == 9.0
    # Plugged in: no reading at either end means no answer.
    assert watts_between(None, 0.0, end_mwh=5000.0, end_t=3600.0) is None
    assert watts_between(10000.0, 0.0, end_mwh=None, end_t=3600.0) is None

    # Quiz scoring must not credit a substring, or a number from a different fact.
    assert quiz_answer_ok("The answer is 429.", "429")
    assert not quiz_answer_ok("Status 4290 is returned", "429")
    assert not quiz_answer_ok("SIGKILL is signal 9", "429")
    assert quiz_answer_ok("post", "POST") and not quiz_answer_ok("posted", "POST")

    print(f"selftest ok (10 W at 5 tok/s = {kwh:.4f} kWh per 1M tokens)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="*", help="Ollama model tags to benchmark")
    ap.add_argument("--list", action="store_true", help="show installed models")
    ap.add_argument("--quiz", action="store_true", help="run the correctness quiz only")
    ap.add_argument("--runs", type=int, default=2, help="passes over the prompt set (default 2)")
    ap.add_argument("--tariff", type=float, default=0.4443,
                    help="electricity price per kWh (default RM0.4443 = TNB domestic 44.43 sen, <=1500kWh)")
    ap.add_argument("--currency", default="RM")
    ap.add_argument("--idle", type=float, default=None,
                    help="reuse a previously measured idle baseline in watts, skipping the 5min wait")
    ap.add_argument("--out", default="bench-results.json")
    a = ap.parse_args()

    if a.list:
        for m in api("/api/tags")["models"]:
            print(f"{m['name']:<28} {m['size']/1e9:>5.1f} GB")
        return
    if not a.models:
        sys.exit("pass --models, or --list to see what's installed")

    if a.quiz:
        quiz(a.models)
        return

    quiz_scores = quiz(a.models)
    results = bench(a.models, a.tariff, a.runs, a.idle if a.idle else idle_baseline())
    for r in results:
        r["quiz_score"] = quiz_scores.get(r["model"])
    json.dump({"tariff_per_kwh": a.tariff, "currency": a.currency,
               "prompts": PROMPTS, "num_predict": NUM_PREDICT, "results": results},
              open(a.out, "w", encoding="utf-8"), indent=1)
    report(results, a.tariff, a.currency)
    print(f"\nwrote {a.out}")


if __name__ == "__main__":
    demo() if "--selftest" in sys.argv else main()
