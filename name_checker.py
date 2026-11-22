"""Find names that are spelled more than one way across a microdrama's scripts.

AI video/voice pipelines read the script literally: if the family estate is
"Ravenscroft Hall" in one episode and "Ravenscraft Hall" in the next, the voice
says two different names and the image model may draw two different houses.
This tool lists every such drift with episode, line and severity so a script
editor can fix it before generation.

Pure Python standard library. The rules find candidates; an optional LLM
(--llm) only judges "same entity or not?" for each cluster.

Usage:
    python name_checker.py <screenplay_dir> [--out outputs] [--llm]
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

# ---- screenplay line classification (format of the Kuku input pack) -------

_LABEL = re.compile(r"^\*\*(.+?):\*\*\s*$")
_TIMING = re.compile(r"^Start:\s*\(([\d:.]+)\)", re.I)
_HEADER = re.compile(r"^(EPISODE LOGLINE|CONTINUITY NOTES):", re.I)
_PAREN = re.compile(r"\([^)]*\)")
_FILE_EP = re.compile(r"episode-(\d+)", re.I)
_NON_DIALOGUE = {"CAMERA", "ACTION", "SOUND FX", "SFX"}

# Candidate names: capitalised words. Sentence-start words ("Close", "The")
# are dropped later because they also appear in lowercase somewhere.
_CAP = re.compile(r"\b([A-Z][a-z]{3,})\b")
_WORD = re.compile(r"[A-Za-z]+")

# Severity: what the viewer actually experiences if the drift ships.
SEVERITY = {
    "dialogue": "HIGH",      # spoken by the AI voice / shown as subtitle
    "speaker_tag": "MEDIUM", # decides which voice + face reference is used
    "description": "LOW",    # shapes the video prompt
    "notes": "LOW",          # logline / continuity notes
}
_RANK = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}


@dataclass
class Line:
    episode: int
    lineno: int
    kind: str      # dialogue | speaker_tag | description | notes
    text: str
    time: str = ""     # screenplay timestamp of the block, if any
    speaker: str = ""  # who says it (dialogue lines)
    context: str = ""  # the scene description just before (for the LLM judge)


def read_lines(path: Path) -> list[Line]:
    """Tag every non-empty line of one episode with what kind of text it is."""
    m = _FILE_EP.search(path.name)
    ep = int(m.group(1)) if m else 0
    out: list[Line] = []
    block, time, speaker, last_desc = "description", "", "", ""
    for i, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        s = raw.strip()
        if not s:
            continue
        if _HEADER.match(s):
            out.append(Line(ep, i, "notes", s.split(":", 1)[1].strip()))
            continue
        if lab := _LABEL.match(s):
            label = lab.group(1)
            if label.strip().upper() in _NON_DIALOGUE:
                block = "description"
            else:
                block, speaker = "dialogue", _PAREN.sub("", label).strip()
                out.append(Line(ep, i, "speaker_tag", speaker))
            continue
        if t := _TIMING.match(s):
            time = t.group(1)
            continue
        if block == "dialogue":
            out.append(Line(ep, i, block, s, time, speaker, last_desc))
        else:
            out.append(Line(ep, i, block, s, time))
            last_desc = s
    return out


def load_show(folder: Path) -> list[Line]:
    files = sorted(folder.glob("*.txt"))
    if not files:
        raise SystemExit(f"no .txt screenplays in {folder}")
    return [ln for f in files for ln in read_lines(f)]


# ---- candidate names ------------------------------------------------------

def proper_nouns(lines: list[Line]) -> dict[str, list[Line]]:
    """word -> lines it occurs in. A word that also appears in lowercase
    anywhere in the show ("noble", "grace") is a common word, not a name."""
    lowercase = {w for ln in lines for w in _WORD.findall(ln.text) if w.islower()}
    hits: dict[str, list[Line]] = defaultdict(list)
    for ln in lines:
        if ln.kind == "speaker_tag":
            words = [w.title() for w in _WORD.findall(ln.text) if len(w) >= 4]
        else:
            words = _CAP.findall(ln.text)
        for w in words:
            if w.lower() not in lowercase:
                hits[w].append(ln)
    # Second pass: once a word is known to be a name, also count it at the
    # start of sentences, so occurrence counts are complete.
    names = set(hits)
    for ln in lines:
        if ln.kind == "speaker_tag":
            continue
        for w in set(_WORD.findall(ln.text)) & names:
            if ln not in hits[w]:
                hits[w].append(ln)
    return hits


def _stem(w: str) -> str:
    w = w.lower()
    return w[:-1] if w.endswith("s") and len(w) > 4 else w


def similar(a: str, b: str, threshold: float) -> bool:
    a, b = _stem(a), _stem(b)
    if a == b:
        return True
    if a[0] != b[0]:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= threshold


def cluster(names: list[str], threshold: float) -> list[list[str]]:
    """Single-link clustering (union-find): Ravenscroft~Ravenscraft~Ravenscrift~... ."""
    parent = {n: n for n in names}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if similar(a, b, threshold):
                parent[find(a)] = find(b)
    groups: dict[str, list[str]] = defaultdict(list)
    for n in names:
        groups[find(n)].append(n)
    return [g for g in groups.values() if len({_stem(x) for x in g}) > 1]


# ---- speaker-tag checks ---------------------------------------------------

def tag_aliases(lines: list[Line]) -> list[dict]:
    """'JULIANA' vs 'LADY JULIANA': two tags, one character -> risk of two
    voices / two face references."""
    tags = Counter(ln.text.upper() for ln in lines if ln.kind == "speaker_tag")
    eps = defaultdict(set)
    for ln in lines:
        if ln.kind == "speaker_tag":
            eps[ln.text.upper()].add(ln.episode)
    out = []
    for short in tags:
        for long in tags:
            if short != long and set(short.split()) < set(long.split()):
                out.append({"short": short, "long": long, "short_n": tags[short], "long_n": tags[long],
                            "episodes_short": sorted(eps[short]), "episodes_long": sorted(eps[long])})
    return out


# ---- optional LLM judge ---------------------------------------------------

def llm_judge(clusters: list[dict], workers: int = 4) -> dict:
    """Ask an LLM, for each variant, "same entity as the canonical spelling?",
    with the whole cluster as context (one call per cluster). Adds
    'llm_verdict' to each variant and 'llm_kept' (any variant kept) to each
    cluster. Model is swappable via env vars (see llm_judge.py)."""
    from concurrent.futures import ThreadPoolExecutor

    from llm_judge import judge_cluster  # local module, stdlib only

    with ThreadPoolExecutor(workers) as pool:
        results = list(pool.map(judge_cluster, clusters))
    total = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
    for c, (verdicts, usage) in zip(clusters, results, strict=True):
        for v in c["variants"]:
            v["llm_verdict"] = verdicts[v["spelling"]]
        c["llm_kept"] = any(v["llm_verdict"]["same_entity"] for v in c["variants"])
        for k in total:
            total[k] += usage[k]
    return total  # per run, so overlapping runs (e.g. in the web app) don't mix


# ---- report ---------------------------------------------------------------

def analyse(folder: Path, threshold: float = 0.7) -> dict:
    lines = load_show(folder)
    hits = proper_nouns(lines)
    clusters = []
    for group in cluster(sorted(hits), threshold):
        counts = {w: len(hits[w]) for w in group}
        canonical = max(group, key=lambda w: counts[w])
        variants = []
        for w in sorted(group, key=lambda w: -counts[w]):
            if w == canonical:
                continue
            occ = sorted(hits[w], key=lambda line: (line.episode, line.lineno))
            sev = min((SEVERITY[o.kind] for o in occ), key=_RANK.get)
            variants.append({
                "spelling": w, "count": counts[w], "severity": sev,
                "occurrences": [{"episode": o.episode, "line": o.lineno, "kind": o.kind,
                                 "time": o.time, "text": o.text[:160],
                                 "speaker": o.speaker, "context": o.context[:200]} for o in occ],
            })
        sev = min((v["severity"] for v in variants), key=_RANK.get)
        clusters.append({"canonical": canonical, "canonical_count": counts[canonical],
                         "canonical_example": hits[canonical][0].text[:160],
                         "severity": sev, "variants": variants})
    clusters.sort(key=lambda c: (_RANK[c["severity"]], -sum(v["count"] for v in c["variants"])))
    return {"episodes": len({line.episode for line in lines}), "lines": len(lines),
            "proper_nouns": len(hits), "clusters": clusters, "tag_aliases": tag_aliases(lines)}


def _cell(text: str) -> str:
    """Spreadsheet apps run cells starting with = + - @ as formulas; script text
    is untrusted, so prefix those with ' (OWASP CSV-injection advice)."""
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def write_outputs(result: dict, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(result, indent=2), encoding="utf-8")

    with open(out / "flags.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["cluster", "suggested_canonical", "variant", "severity", "episode", "line",
                    "kind", "timestamp", "text", "llm_same_entity"])
        for i, c in enumerate(result["clusters"], 1):
            for v in c["variants"]:
                verdict = v.get("llm_verdict", {}).get("same_entity", "")
                for o in v["occurrences"]:
                    w.writerow([i, c["canonical"], v["spelling"], SEVERITY[o["kind"]], o["episode"],
                                o["line"], o["kind"], o["time"], _cell(o["text"]), verdict])

    md = ["# Name-drift report\n",
          f"{result['episodes']} episodes, {result['lines']} lines, "
          f"{result['proper_nouns']} proper nouns, **{len(result['clusters'])} spelling clusters**, "
          f"{len(result['tag_aliases'])} speaker-tag aliases.\n",
          "Severity: HIGH = spoken in dialogue, MEDIUM = speaker tag (voice/face reference), "
          "LOW = description only.\n", "## Spelling clusters\n"]
    for i, c in enumerate(result["clusters"], 1):
        md.append(f"### {i}. `{c['canonical']}` ×{c['canonical_count']} [{c['severity']}]")
        for var in c["variants"]:
            eps = sorted({o['episode'] for o in var['occurrences']})
            v = var.get("llm_verdict")
            judged = (f" — LLM: {'**fix**, same entity' if v['same_entity'] else 'dismiss, different/alias'}"
                      f" ({v['reason']})") if v else ""
            md.append(f"- **{var['spelling']}** ×{var['count']} [{var['severity']}] in ep {eps}{judged}")
            first = next((o for o in var["occurrences"] if o["kind"] == "dialogue"), var["occurrences"][0])
            md.append(f"  - ep {first['episode']} L{first['line']} ({first['kind']}): {first['text']}")
        md.append("")
    md.append("## Speaker-tag aliases\n")
    for a in result["tag_aliases"]:
        md.append(f"- `{a['short']}` ×{a['short_n']} vs `{a['long']}` ×{a['long_n']} "
                  f"(eps {a['episodes_long'][:8]}{'…' if len(a['episodes_long']) > 8 else ''})")
    (out / "report.md").write_text("\n".join(md) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("screenplays", type=Path)
    ap.add_argument("--out", type=Path, default=Path("outputs"))
    ap.add_argument("--threshold", type=float, default=0.7, help="spelling similarity 0-1")
    ap.add_argument("--llm", action="store_true", help="LLM judges each cluster (needs LLM_API_KEY)")
    args = ap.parse_args()

    result = analyse(args.screenplays, args.threshold)
    if args.llm:
        result["llm_usage"] = llm_judge(result["clusters"])
        print(f"LLM usage: {result['llm_usage']}")
    write_outputs(result, args.out)
    print(f"{len(result['clusters'])} clusters, {len(result['tag_aliases'])} tag aliases -> {args.out}/report.md")


if __name__ == "__main__":
    main()
