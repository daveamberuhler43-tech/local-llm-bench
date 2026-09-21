# Results

One laptop, measured 2026-09-20. Every figure here traces to a JSON file in this repo, and
`gate1_check.py` diffs the README table against them on demand.

## Machine

| | |
|---|---|
| CPU | Intel Core i5-1145G7 (4 cores / 8 threads) |
| RAM | 16 GB |
| GPU | integrated only — no NVIDIA, no `nvidia-smi` |
| Inference | CPU, Ollama 0.34.2 |
| Battery | 8,588 mWh full charge |
| OS | Windows 11 |
| Electricity | $0.1092/kWh |

## The run

| model | tok/s | TTFT warm | resident | throttle | hrs/Mtok | net W | energy/Mtok | correct |
|---|---|---|---|---|---|---|---|---|
| llama3.2:1b | 19.24 | 0.38 s | 1,449 MB | 9.7% | 15.0 | *refused* | *refused* | 3/8 |
| llama3.2:3b | 12.86 | 0.68 s | 2,443 MB | 1.4% | 21.5 | 10.0 | $0.0234 | 6/8 |
| mistral:7b | 6.09 | 1.55 s | 4,802 MB | 2.4% | 45.9 | 11.7 | $0.0587 | 7/8 |

Idle baseline: **13.2 W**, measured over 300 s with the machine untouched. Net watts are the
marginal draw above that, so a model is not charged for the screen.

Cold start is separate from the warm TTFT above: mistral's *first* token after loading from disk
took **16.21 s** (and 20.89 s on an earlier pass). Warm, it is 1.55 s.

Speed figures for the 1B come from a 30-run pass (`bench-1b-long.json`); the 3B and 7B come from the
clean on-battery run (`bench-battery.json`). They are separate files because the 1B had to be
re-measured — see below.

## Correctness

Eight IT-ops facts, one right answer each, scored by exact token match. No grader model.

| question | answer | 1b | 3b | 7b |
|---|---|---|---|---|
| HTTP status for Too Many Requests | 429 | 404 ✗ | 503 ✗ | ✓ |
| SIGKILL signal number | 9 | 0 ✗ | ✓ | ✓ |
| Default DNS port | 53 | ✓ | ✓ | ✓ |
| Default PostgreSQL port | 5432 | ✓ | ✓ | ✓ |
| Usable hosts in an IPv4 /26 | 62 | 65536 ✗ | 256 ✗ | ✓ |
| Group digit in chmod 640 | 4 | 3 ✗ | ✓ | ✓ |
| Which is NOT idempotent | POST | GET ✗ | ✓ | DELETE ✗ |
| Default SSH port | 22 | ✓ | ✓ | ✓ |
| **score** | | **3/8** | **6/8** | **7/8** |

Speed and correctness move in opposition. The 1B is 3.2× faster than the 7B and gets less than half
as many right — and the ones it misses are the ones an ops person needs: rate limiting, signals,
subnetting.

## What this costs against buying the tokens

One million output tokens, list prices verified 2026-09-20. All figures in USD.

The electricity tariff is measured locally (TNB domestic, 44.43 sen/kWh) and converted
once at 4.07 MYR/USD (xe.com, 2026-09-12); nothing below is reported in another currency.

| | per 1M output tokens |
|---|---|
| GPT-4.1 nano | $0.40 |
| GPT-4o mini | $0.60 |
| Gemini Flash-Lite | $2.50 |
| Claude Haiku 4.5 | $5.00 |

Against $0.0234–$0.0587 in electricity. Local wins on money by 7–17×, and loses anyway: the same
million tokens is 21.5 to 45.9 hours of a laptop that can do nothing else meanwhile.

Marginal draw is roughly flat at 10–12 W across model sizes, because it is CPU-bound either way.
**Energy per token is a clock, not a property of the model.**

Comparison is against the small/cheap API tier deliberately. A quantised 7B on a laptop does not
compete with a frontier model, and pricing it against one would flatter it.

## Three measurements that were thrown away

Kept here because the failures are more instructive than the results.

**1. `DischargeRate` cannot see the CPU.** The obvious Windows power source, validated against a
known load — 45 s idle, then a full 8-core busy loop:

```
IDLE (settling)    n=95   mean=23.2W  min=20.2 max=27.4  distinct=7
ALL-CORE BURN      n=78   mean=22.4W  min=21.7 max=23.7  distinct=7
DELTA = -0.8 W
```

A full CPU burn read negative, across only 7 distinct values in 95 samples. It is smoothed and
tracks the display. It had already produced a non-physical result before that test: the 3B
apparently drawing 0.6 W against the 1B's 1.6 W while doing more work.

**2. A 2-minute window is not a measurement.** After switching to the battery energy counter, the 1B
read 22.5 W against the larger 3B's 10.0 W. The 1B's run was short enough to be dominated by loading
the model off disk and by CPU boost clocks. A 300-second floor now sits in `watts_between()`.

**3. The laptop slept mid-benchmark.** The 1B re-run produced an 8,265-second window (Windows event
42 at 16:42, resume at 18:54). The guard refused it rather than dividing a small energy drop by two
hours and reporting a convincingly low wattage. That refusal is why the 1B's energy cell is empty
here instead of holding a plausible number.

The arithmetic was correct in all three cases. The measurement window lied. That is the whole reason
this tool returns `None` rather than its best guess.

## Limits

One laptop, integrated graphics, CPU inference, three models, one runtime. Nothing here transfers to
a machine with a dedicated GPU. Electricity is the only cost measured — not hardware, not your time,
and not the fact that the machine is unusable while it runs.
