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

## How much RAM does it actually need? (`ram_pressure.py`)

Every "how much RAM do you need" answer is a rule of thumb. This takes the RAM away a gigabyte at a
time and measures what breaks, in order: time to first token, tokens/sec, swap, and — the question
nobody seems to ask — whether the answers themselves get worse.

```bash
python ram_pressure.py --selftest              # maths + safety, allocates nothing
python ram_pressure.py --probe                 # show headroom, allocates nothing
python ram_pressure.py --models llama3.2:1b --max-ballast 3
```

It deliberately starves the machine it runs on. Three guards: it never allocates past 900 MB of
available memory, checked before *every* step; it releases everything on exit including Ctrl-C and
exceptions; and it refuses to start if the machine is already below that floor. Close your work
anyway — a swapping Windows box is unpleasant to use.

**The file size is not the memory size.** Resident memory runs ~500–630 MB above what the model
weighs on disk, on every model measured:

```
MODEL                       ON DISK   RESIDENT    GAP
--------------------------------------------------------
llama3.2:3b-instruct-q8_0    3263 MB   3780 MB   +517 MB
mistral:7b-instruct-q3_K_M   3357 MB   3987 MB   +630 MB
mistral:7b                   4170 MB   4802 MB   +632 MB
```

And *loading* costs more than either. Bringing up llama3.2:3b — a model that reports 2443 MB
resident — took available memory from 4477 MB to 1603 MB. That is 2874 MB gone to hold a 2443 MB
model, because the runtime reads the file through the page cache on the way in.

**Taking RAM away makes it slower. It does not make it wrong.** The 1B, walked down a ballast
ladder until the safety floor stopped the test:

```
AVAILABLE   tok/s   SWAP DELTA   QUIZ
--------------------------------------
  4614 MB   24.82         0 MB   3/8
  2868 MB   24.47         0 MB   3/8
  2172 MB   23.24         0 MB   3/8
  1696 MB   18.93         0 MB   3/8
  1324 MB   16.25       441 MB   3/8
  1122 MB   18.13        18 MB   3/8
```

24.82 → 16.25 tok/s is a 35% loss. The score is the same at the top of that ladder and at the
bottom. Across 12 measurements and three models — 1B at 3/8, 3B at 6/8, 7B at 7/8 — no score moved
by a single question under any amount of memory pressure. Starving a model degrades throughput,
not accuracy. Whatever it got wrong with 4.6 GB free, it got wrong the same way with 1.1 GB free.

**The 7B does not fit.** mistral:7b needs no help to run the machine out: loading it with *zero*
ballast added left 837 MB available, already under the floor the tool refuses to cross.

**So run the smaller one.** On the same eight questions:

```
                            RESIDENT   tok/s   QUIZ
---------------------------------------------------
mistral:7b                   4802 MB    6.09   7/8
llama3.2:3b-instruct-q8_0    3780 MB    8.43   7/8
```

1022 MB less, 38% faster, identical score. The q3 quantisation of the 7B also scores 7/8 at
3987 MB — but it throttles 27.0% over a sustained run against 2.4% for the q4, so it is working the
processor harder for its size and that advantage may not survive a longer test than this one.

## Is "local" actually local? (`probe_manifests.py`, `coding_task.py`)

Two small tools, written for the video "Your 'Free Claude Code' Is Running in the Cloud".

**`probe_manifests.py`** asks the Ollama registry for the manifest of three tags and counts the
weight layers each one would download. A control tag that does not exist must return 404, or the
probe is meaningless. On 7 Oct 2026 (`cloud-manifest.json`):

| tag | layers | bytes |
|---|---|---|
| `gpt-oss:120b-cloud` | 0 | 0 |
| `gpt-oss:20b` | 4 | 13,793,440,755 |
| `qwen3.5:4b` | 5 | 3,324,173,757 |

A `-cloud` tag has nothing to download because nothing is stored locally: requests go to a hosted
service. This is how Ollama documents and prices those tags; the point is only that the word
"local" in a tutorial is not evidence, and the download size is.

**`coding_task.py`** sends one local model three ordinary coding requests (fix a bug, explain a
regex, write `parse_duration`) with thinking off, temperature 0, seed 7, and records wall time,
tokens, and the exact reply. The function is graded by running it against four inputs
(`2h`, `45s`, `90m`, `1h30m`). `--live` runs the same thing with a real-time clock pinned to the
corner, in the order the video shows them.

```bash
python probe_manifests.py                 # needs network, writes cloud-manifest.json
python coding_task.py --models qwen3.5:4b # needs Ollama running, writes coding-task.json
python coding_task.py --live              # same, with the clock
```

What the files in this repo say, with the caveats that matter:

- `coding-task.json` records the **build digest and Ollama version** it measured
  (`d8b0f5e9760c`, Ollama 0.35.1). A tag is not an identity; the digest is. If yours differs, you
  are not running the same build as the video.
- On that build `parse_duration` passes **1 of 4** cases (`2h`); `45s` and `90m` raise
  `UnboundLocalError`; `1h30m` returns 11400 instead of 5400. All three runs returned the same
  reply. `coding-task-2026-09-30-oldbuild.json` is the same tag measured on 30 Sep: **3 of 4**.
  The registry served a different build under the same tag in between, and the Ollama version
  changed as well, so **the cause is unknown** and the two cannot be separated
  (`build-history.json`).
- **Timings move with whatever else the laptop is doing.** The same three requests took 32.19 s and
  40.82 s unrecorded (`...-run1.json`, `...-unrecorded.json`), 42.29 to 44.61 s with a screen
  recorder running on an idle machine, and 79.74 s and 86.43 s while another program was
  rendering video (`...contended...json`, kept on purpose). The video quotes the recorded run
  (`coding-task.json`, 44.61 s) and says so. A screen recorder alone cost about 45% in a direct
  A/B (60.4 s against 40.8 s).
- `search-views.json` is the dated search snapshot behind the video's view-count claim. It was
  taken with Restricted Mode on, so it is a filtered list.

Nothing here measures the hosted path: no cloud timings, and no comparison with it, were taken.

## Limits

One laptop, integrated graphics, CPU inference, three quantised models from one runtime. It says
nothing about a machine with a dedicated GPU, where the whole shape of this changes. Electricity is
the only cost measured — hardware, your time, and the laptop being unusable while it runs are not in
that number.

The pressure ladder is one laptop's page-cache and swap behaviour under Windows; a Linux box
with different swappiness will not fall over at the same place. And "the score never moved" is
eight questions, not a benchmark suite — it rules out memory pressure silently corrupting
output, it does not certify any of these models as correct.

Run it on your own machine and post what you get.

## Licence

MIT.
