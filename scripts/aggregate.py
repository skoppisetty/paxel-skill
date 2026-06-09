#!/usr/bin/env python3
"""
aggregate.py — roll per-episode 5-axis scores up to an overall score + band.

HONESTY NOTE — read this:
  Paxel computes the overall score, the per-axis rollup, and the band on its
  SERVER (Api::V1::ResultsController#build_v3_results), which is NOT in the
  public client image. The exact rule (mean? confidence-weighted? recency- or
  volume-weighted? trimmed?) is unknown. So the OVERALL number and the BAND
  below are an APPROXIMATION, not YC's verdict. Only the band CUT thresholds are
  verbatim from the client (language_band.rb). The per-axis scores ARE faithful.

  Default rollup here = confidence-weighted mean per axis, then mean of the
  per-axis means. To match YC, collect a few real (your upload -> band YC showed
  you) pairs and refit the weights; this is a calibration target, not truth.

Usage:
  python3 aggregate.py <episodes.json>      # list of {"scores":{...},"confidence":x}
  cat episodes.json | python3 aggregate.py  # or via stdin
"""
import sys, json

AXES = ["execution_leverage", "steering", "engineering_quality", "product_thinking", "planning"]

# Verbatim from language_band.rb BANDS (the ONLY part that is authoritative).
BANDS = [("WEAK", 0, 4), ("LIMITED", 4, 6), ("STRONG", 6, 8), ("ELITE", 8, 9), ("EXEMPLAR", 9, 10.0001)]

def band_for_score(score):
    for name, lo, hi in BANDS:
        if lo <= score < hi:
            return name
    return "EXEMPLAR" if score >= 9 else "WEAK"

def rollup(episodes):
    # confidence-weighted mean per axis across episodes that scored that axis
    per_axis = {}
    for axis in AXES:
        num = den = 0.0
        for ep in episodes:
            scores = (ep or {}).get("scores") or {}
            if axis in scores and isinstance(scores[axis], (int, float)):
                # Preserve an explicit confidence of 0 (=> no weight); only a
                # missing or non-numeric confidence falls back to the 0.8 default.
                conf = ep.get("confidence", 0.8)
                w = float(conf) if isinstance(conf, (int, float)) else 0.8
                num += float(scores[axis]) * w
                den += w
        if den > 0:
            per_axis[axis] = round(num / den, 2)
    overall = round(sum(per_axis.values()) / len(per_axis), 2) if per_axis else None
    return per_axis, overall

def main():
    raw = open(sys.argv[1]).read() if len(sys.argv) > 1 else sys.stdin.read()
    episodes = json.loads(raw)
    if isinstance(episodes, dict):
        episodes = episodes.get("episodes", [])
    per_axis, overall = rollup(episodes)
    out = {
        "episodes_scored": len(episodes),
        "axes_APPROX": per_axis,
        "overall_score_APPROX": overall,
        "band_APPROX": band_for_score(overall) if overall is not None else None,
        "_disclaimer": "axes are faithful; overall_score and band are an APPROXIMATION of YC's "
                       "server-side rollup (build_v3_results), which is not in the client image. "
                       "Band cut thresholds (WEAK<4, LIMITED<6, STRONG<8, ELITE<9, EXEMPLAR>=9) are verbatim.",
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    main()
