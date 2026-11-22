"""Precision test: score a report.json against hand labels (gold_labels.json).

Prints, for spelling clusters:
  * rules alone       - how many flags are real naming errors
  * rules + LLM judge - after dropping clusters the LLM calls "different"
and for speaker-tag aliases, how many are confirmed errors vs need review.

Usage: python eval_precision.py outputs/report.json gold_labels.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(report_path: str, gold_path: str) -> None:
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    gold = json.loads(Path(gold_path).read_text(encoding="utf-8"))
    labels = gold["clusters"]

    rows, variant_rows, unlabeled = [], [], []
    for c in report["clusters"]:
        g = labels.get(c["canonical"], {}).get("label")
        if g is None:
            unlabeled.append(c["canonical"])
            continue
        verdict = c.get("llm_kept")  # True if the judge says "fix" for any variant
        rows.append((c["canonical"], g, verdict))
        for v in c["variants"]:
            if "llm_verdict" in v:
                variant_rows.append((f'{c["canonical"]}->{v["spelling"]}', g, v["llm_verdict"]["same_entity"]))

    real = sum(g == "error" for _, g, _ in rows)
    print(f"Spelling clusters flagged by rules: {len(rows)}  (real errors: {real})")
    print(f"  rules precision           {real}/{len(rows)} = {real / max(len(rows), 1):.0%}")
    judged = [r for r in rows if r[2] is not None]
    if judged:
        kept = [r for r in judged if r[2]]
        tp = sum(g == "error" for _, g, _ in kept)
        agree = sum((g == "error") == v for _, g, v in judged)
        print(f"  rules + LLM precision     {tp}/{len(kept)} = {tp / max(len(kept), 1):.0%}")
        print(f"  rules + LLM recall        {tp}/{real} of the real errors kept")
        print(f"  LLM agrees with human     {agree}/{len(judged)}")
        for name, g, v in judged:
            mark = "ok " if (g == "error") == v else "XX "
            print(f"    {mark}{name:<12} human={g:<6} llm_same_entity={v}")
    if variant_rows:
        agree_v = sum((g == "error") == v for _, g, v in variant_rows)
        print(f"  per-variant LLM agreement {agree_v}/{len(variant_rows)}")
        for name, g, v in variant_rows:
            if (g == "error") != v:
                print(f"    XX {name} human={g} llm_same_entity={v}")
    if unlabeled:
        print(f"  unlabeled clusters (label them in gold): {unlabeled}")
    print(f"Known misses listed in gold: {len(gold.get('known_misses', []))}")

    tags = gold.get("tag_aliases", {})
    got = [f"{a['short']}|{a['long']}" for a in report["tag_aliases"]]
    err = sum(tags.get(k) == "error" for k in got)
    rev = sum(tags.get(k) == "needs_review" for k in got)
    print(f"Speaker-tag aliases: {len(got)}  confirmed errors {err}, needs human review {rev}, "
          f"unlabeled {len(got) - err - rev}")


if __name__ == "__main__":
    main(*(sys.argv[1:3] if len(sys.argv) >= 3 else ["outputs/report.json", "gold_labels.json"]))
