# local-llm-bench

Measure what a self-hosted LLM actually costs on the laptop you already own — tokens/sec, time to
first token, sustained-run throttling, real power draw, and whether the model is *right*.

Every "run an LLM locally" tutorial stops at "it works". This measures what happens next.

```
MODEL                  tok/s   TTFT  W tot  W net  MODEL MB  THROTTLE  HRS/Mtok  ENERGY/Mtok
--------------------------------------------------------------------------------------------
llama3.2:1b            19.24   0.38      -      -      1449      9.7%      15.0            -
llama3.2:3b            12.86   0.68   23.2   10.0      2443      1.4%      21.5      $0.0234
mistral:7b              6.09   1.55   24.9   11.7      4802      2.4%      45.9      $0.0587

One million output tokens, bought instead:
  GPT-4.1 nano         $  0.40
  GPT-4o mini          $  0.60
  Gemini Flash-Lite    $  2.50
  Claude Haiku 4.5     $  5.00
```

Measured on an i5-1145G7 / 16 GB / integrated graphics, CPU inference. See [RESULTS.md](RESULTS.md)
for the full run, what was refused, and why.

## What it finds

**Electricity is not the problem.** A million tokens costs $0.10–0.24 in power against $0.40 to
buy from the cheapest API — self-hosting wins the money argument by 7–17×.

**Time is the problem.** That same million tokens costs 22–46 hours of a laptop you cannot use for
anything else while it runs.

**And the fast model is the wrong one.** On eight IT-ops facts with exactly one right answer,
the 1B scores 3/8 — it says rate limiting is HTTP 404, SIGKILL is signal 0, and a /26 subnet holds
65,536 hosts. The 7B gets 7/8 and is the one that takes 46 hours.

## Quick start

```bash
pip install psutil
ollama pull llama3.2:1b llama3.2:3b mistral:7b

python local_llm_bench.py --selftest                    # check the maths, no model needed
python local_llm_bench.py --list                        # what Ollama has
python local_llm_bench.py --models llama3.2:3b mistral:7b
python local_llm_bench.py --quiz --models llama3.2:1b   # correctness only
```

Unplug the laptop for the power figures. Plugged in, the battery counter stops falling and the
energy columns are left empty rather than filled with zeros.

Set your own tariff and currency:

```bash
python local_llm_bench.py --models mistral:7b --tariff 0.30 --currency '$'
```

## How the numbers are produced

**Speed** comes from Ollama's own `eval_count` and `eval_duration`, not from wall-clock guessing.
Time to first token is measured on the stream. Throttling is the second half of a sustained run
against the first half, so heat shows up as a decline rather than hiding in an average.

**Correctness** is eight facts with one right answer each, scored by exact token match. No grader
model, no judgment call. `429` is not credited for `4290`, and not for SIGKILL's `9`.

**Power** is differenced from the battery's `RemainingCapacity` energy counter over a timed window,
then charged only the *marginal* draw above a measured idle baseline — so a model is not billed for
your screen being on.

## The instrument that had to be thrown away

The obvious way to read power on Windows is `root\WMI BatteryStatus.DischargeRate`. It does not work
on this hardware, and it fails *quietly*.

Validated against a known load — 45 s idle, then a full 8-core busy loop:

```
IDLE (settling)    n=95   mean=23.2W  min=20.2 max=27.4  distinct=7
ALL-CORE BURN      n=78   mean=22.4W  min=21.7 max=23.7  distinct=7

DELTA = -0.8 W for a full all-core burn
```

A full CPU burn read *negative*. The value is heavily smoothed and tracks the display more than the
CPU. Before that validation it had already produced a non-physical result: a 3B model appearing to
draw less power than a 1B while doing more work.

It was replaced with the energy counter, which cannot fail that way — and `watts_between()` now
returns `None`, never a number, in three cases: plugged in, counter never moved, or a window shorter
than 300 seconds. That last one matters. A 2-minute measurement of the 1B read 22.5 W against
10.0 W for the larger 3B, because the window was dominated by loading the model off disk and boost
clocks. During a later re-run the laptop went to sleep mid-benchmark, producing an 8,265-second
window; the guard refused it instead of dividing a small energy drop by two hours and reporting a
convincingly low wattage.

Every power number that looked wrong was wrong. The arithmetic was fine every time — the measurement
window lied. That is why the tool refuses rather than estimates, and why the 1B's energy cell in the
table above is empty. It could be inferred from the flat-power finding. It would look like a
measurement, so it isn't there.

## Limits

One laptop, integrated graphics, CPU inference, three quantised models from one runtime. It says
nothing about a machine with a dedicated GPU, where the whole shape of this changes. Electricity is
the only cost measured — hardware, your time, and the laptop being unusable while it runs are not in
that number.

Run it on your own machine and post what you get.

## Licence

MIT.
