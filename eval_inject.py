"""Recall test: plant misspellings in a copy of the scripts, see how many we catch.

Real scripts have no answer key for "every misspelling", so we make one:
take names the checker already knows (frequent proper nouns), corrupt single
occurrences the way a typo or an LLM rewrite would (swap/drop/double/replace
a letter), and check whether each planted spelling shows up as a flagged
variant. Seeds are fixed so the numbers are reproducible.

Usage: python eval_inject.py <screenplay_dir> [--n 30] [--seeds 3] [--llm]
"""

from __future__ import annotations

import argparse
import random
import re
import shutil
import tempfile
from pathlib import Path

from name_checker import analyse, load_show, proper_nouns

VOWELS = "aeiou"


def corrupt(name: str, rng: random.Random) -> str:
    i = rng.randrange(1, len(name) - 1)  # keep the first letter, like most real typos
    kind = rng.choice(["swap", "drop", "double", "vowel"])
    if kind == "swap" and i < len(name) - 1:
        return name[:i] + name[i + 1] + name[i] + name[i + 2:]
    if kind == "drop":
        return name[:i] + name[i + 1:]
    if kind == "double":
        return name[:i] + name[i] + name[i:]
    vs = [j for j, c in enumerate(name) if c in VOWELS and j > 0]
    if vs:
        j = rng.choice(vs)
        return name[:j] + rng.choice([v for v in VOWELS if v != name[j]]) + name[j + 1:]
    return name[:i] + name[i + 1:]


def run(src: Path, n: int, seed: int, llm: bool = False) -> tuple[int, int, list[str]]:
    rng = random.Random(seed)
    hits = proper_nouns(load_show(src))
    names = [w for w, occ in hits.items() if len(occ) >= 10 and len(w) >= 5]
    tmp = Path(tempfile.mkdtemp())
    try:
        for f in src.glob("*.txt"):
            shutil.copy(f, tmp / f.name)
        files = sorted(tmp.glob("*.txt"))
        planted: list[tuple[str, str]] = []
        existing = set(hits)
        while len(planted) < n:
            name = rng.choice(names)
            bad = corrupt(name, rng)
            if bad == name or bad in existing:
                continue
            f = rng.choice(files)
            text = f.read_text(encoding="utf-8")
            spots = [m.start() for m in re.finditer(rf"\b{name}\b", text)]
            if not spots:
                continue
            p = rng.choice(spots)
            f.write_text(text[:p] + bad + text[p + len(name):], encoding="utf-8")
            planted.append((name, bad))
            existing.add(bad)
        clusters = analyse(tmp)["clusters"]
        if llm:  # end-to-end: a typo only counts if the judge also says "fix"
            from name_checker import llm_judge
            llm_judge(clusters)
            flagged = {v["spelling"] for c in clusters for v in c["variants"] if v["llm_verdict"]["same_entity"]}
        else:
            flagged = {v["spelling"] for c in clusters for v in c["variants"]}
        # a typo can be the most frequent spelling in its own cluster only if it is
        # the sole member; count canonicals too, as before
        flagged |= {c["canonical"] for c in clusters if not llm}
        missed = [f"{good}->{bad}" for good, bad in planted if bad not in flagged]
        return len(planted) - len(missed), len(planted), missed
    finally:
        shutil.rmtree(tmp)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("screenplays", type=Path)
    ap.add_argument("--n", type=int, default=30, help="misspellings planted per run")
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--llm", action="store_true", help="count a typo only if the LLM judge keeps it")
    args = ap.parse_args()
    total_c = total_n = 0
    for s in range(args.seeds):
        caught, n, missed = run(args.screenplays, args.n, s, args.llm)
        total_c, total_n = total_c + caught, total_n + n
        print(f"seed {s}: caught {caught}/{n}  missed: {', '.join(missed) or '-'}")
    print(f"RECALL {total_c}/{total_n} = {total_c / total_n:.0%}")


if __name__ == "__main__":
    main()
