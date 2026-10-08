#!/usr/bin/env python3
"""
probe_manifests.py - produce the artefact behind video 005's central claim.

    python probe_manifests.py              # writes cloud-manifest.json
    python probe_manifests.py --selftest   # control probe only, writes nothing

WHY THIS EXISTS.

Video 005 is built on one number: pulling `gpt-oss:120b-cloud` downloads zero
bytes, because a "-cloud" tag's manifest has no layers. That number was sitting
in gen_figures.py as a hand-typed literal. Everything else this channel
publishes is diffed against the run that produced it; the one figure the whole
video rests on was an assertion. This closes that.

THE CONTROL MATTERS MORE THAN THE PROBE.

registry.ollama.ai answers a nonsense model name with 404, so a 200 means the
tag is real. Without that check a typo'd tag could 404 silently, be read as
"no layers", and confirm exactly the thing we are trying to prove. The selftest
asserts the control 404s BEFORE any real tag is trusted - a probe that cannot
fail cannot be evidence.

ollama.com/library is blocked to our tooling; the registry v2 API is not.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "cloud-manifest.json")
REG = "https://registry.ollama.ai/v2/library/{name}/manifests/{tag}"
ACCEPT = ("application/vnd.docker.distribution.manifest.v2+json,"
          "application/vnd.oci.image.manifest.v1+json")

# The claim under test, its local counterpart for contrast, and the model the
# second half of the video actually runs.
TAGS = [
    ("cloud", "gpt-oss", "120b-cloud"),
    ("local", "gpt-oss", "20b"),
    ("tested", "qwen3.5", "4b"),
]
# A tag that must NOT exist. If this ever returns 200 the registry is answering
# anything we ask and no result below means a thing.
CONTROL = ("gpt-oss", "120b-cloud-notarealtag-xyzzy")


def fetch(name, tag):
    """Return (status, parsed-json-or-None). 404 is an answer, not an error."""
    req = urllib.request.Request(REG.format(name=name, tag=tag),
                                 headers={"Accept": ACCEPT})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, None


def control_ok():
    status, _ = fetch(*CONTROL)
    print(f"control  {CONTROL[0]}:{CONTROL[1]}  -> {status}"
          f"{'  OK (404 as required)' if status == 404 else '  *** BROKEN ***'}")
    return status == 404


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true",
                    help="run the control probe only; write nothing")
    a = ap.parse_args()

    if not control_ok():
        sys.exit("control probe did not 404 - the registry is not discriminating, "
                 "so nothing it returns can be trusted. Refusing to write.")
    if a.selftest:
        return 0

    out = {
        "source": "registry.ollama.ai v2 manifests API",
        "control": {"tag": f"{CONTROL[0]}:{CONTROL[1]}", "status": 404,
                    "note": "must 404 or the probe is meaningless"},
        "models": {},
    }

    for key, name, tag in TAGS:
        status, man = fetch(name, tag)
        if status != 200:
            sys.exit(f"{name}:{tag} returned {status}, expected 200")
        layers = man.get("layers", [])
        total = sum(l.get("size", 0) for l in layers)
        out["models"][key] = {
            "tag": f"{name}:{tag}",
            "status": status,
            "layers": len(layers),
            "bytes": total,
            "mb": round(total / 1_000_000, 1),
            "gb": round(total / 1_000_000_000, 2),
        }
        print(f"{key:8} {name}:{tag:12} {len(layers)} layer(s)  "
              f"{total:>14,} bytes  {total/1_000_000:>9,.1f} MB")

    c = out["models"]["cloud"]
    if c["layers"] != 0 or c["bytes"] != 0:
        print(f"\n*** THE VIDEO'S CENTRAL CLAIM NO LONGER HOLDS ***\n"
              f"    {c['tag']} now has {c['layers']} layer(s), {c['bytes']:,} bytes.\n"
              f"    Ollama changed how -cloud tags are served. Do not publish.",
              file=sys.stderr)

    json.dump(out, open(OUT, "w", encoding="utf-8"), indent=1)
    print(f"\nwrote {os.path.basename(OUT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
