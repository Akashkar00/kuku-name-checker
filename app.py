"""Streamlit front end for name_checker.py.

Run:  uv run --with streamlit streamlit run app.py

Pick the bundled demo show or upload episode .txt files, get the same
clusters / tag aliases / downloads as the CLI. The LLM judge is optional and
uses the same env vars as llm_judge.py.

Public deployment: set APP_PASSWORD (from Secret Manager) and the LLM judge
only runs for people who enter it; the free rule-based check stays open.
"""

from __future__ import annotations

import hmac
import html
import os
import re
import sys
import tempfile
import time
from pathlib import Path

import streamlit as st

from name_checker import analyse, write_outputs

DEMO = Path(__file__).parent / "samples" / "demo_show"
SEV_ICON = {"HIGH": "🔴", "MEDIUM": "🟠", "LOW": "🟡"}
PASSWORD = os.environ.get("APP_PASSWORD", "")
MAX_FILES, MAX_TOTAL_MB = 200, 20


def plain(text: str, prefix: str = "") -> None:
    """Script lines and LLM reasons are untrusted, so they never go through
    markdown (which autolinks URLs): HTML-escaped plain text only."""
    st.html(f'<p style="margin:0 0 .5rem;opacity:.85">{html.escape(prefix + text)}</p>')


def safe_name(name: str) -> str:
    """Keep only a plain file name (no paths, no hidden files)."""
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", Path(name).name).lstrip(".")
    return name or "upload.txt"

st.set_page_config(page_title="Name-Drift Checker", page_icon="🔤", layout="wide")
st.title("Name-Drift Checker")
st.caption("Spell-check for names across a whole microdrama series, before AI voice and video generation.")

with st.container(border=True):
    left, right = st.columns([3, 2])
    with left:
        source = st.radio("Scripts", ["Demo show (3 episodes)", "Upload episode files"], horizontal=True)
        uploads = []
        if source == "Upload episode files":
            uploads = st.file_uploader("Screenplay .txt files (name them episode-001…)", type="txt",
                                       accept_multiple_files=True)
            st.caption("Files are processed in a temp folder on this server and not kept.")
    with right:
        threshold = st.slider("Spelling similarity threshold", 0.5, 0.95, 0.7, 0.05)
        use_llm = st.checkbox("LLM judge (same entity or alias?)",
                              help="Vertex AI Gemini by default; set LLM_PROVIDER / LLM_MODEL to change.")
        password = ""
        if use_llm and PASSWORD:
            password = st.text_input("Access code for the LLM judge", type="password",
                                     help="The rule-based check is free to use; the LLM costs money per call.")
    run = st.button("Check names", type="primary")

# A report only belongs to the inputs it was made from; drop it when they change.
inputs = (source, tuple((f.name, f.size) for f in uploads or []), threshold, use_llm)
if st.session_state.get("inputs") != inputs:
    st.session_state.pop("result", None)


def run_check(folder: Path) -> dict:
    result = analyse(folder, threshold)
    if use_llm and result["clusters"]:
        import llm_judge
        from name_checker import llm_judge as judge
        llm_judge._endpoint.cache_clear()  # gcloud tokens expire; fetch a fresh one per run
        try:
            result["llm_usage"] = judge(result["clusters"])
        except (SystemExit, Exception) as e:  # llm_judge exits on HTTP / auth errors
            print(f"LLM judge failed: {e!r}", file=sys.stderr)  # details go to the server log only
            st.warning("LLM judge is unavailable right now; showing rule results only.")
    return result


if run:
    if source == "Upload episode files" and not uploads:
        st.error("Upload at least one episode .txt file.")
        st.stop()
    if len(uploads) > MAX_FILES or sum(f.size for f in uploads) > MAX_TOTAL_MB * 2**20:
        st.error(f"Too much input: up to {MAX_FILES} files and {MAX_TOTAL_MB} MB in total.")
        st.stop()
    if use_llm and PASSWORD and not hmac.compare_digest(password.encode(), PASSWORD.encode()):
        time.sleep(1)  # slow down guessing
        st.error("Wrong or missing access code for the LLM judge. Untick it to run the free rule-based check.")
        st.stop()
    with tempfile.TemporaryDirectory() as tmp:
        folder = DEMO
        if uploads:
            folder = Path(tmp) / "show"
            folder.mkdir()
            for f in uploads:
                (folder / safe_name(f.name)).write_bytes(f.getvalue())
        with st.spinner("Checking names… (with the LLM judge this can take up to a minute)" if use_llm
                        else "Checking names…"):
            result = run_check(folder)
        out = Path(tmp) / "out"
        write_outputs(result, out)
        st.session_state["files"] = {n: (out / n).read_bytes() for n in ("report.md", "flags.csv", "report.json")}
    st.session_state["result"] = result
    st.session_state["inputs"] = inputs

result = st.session_state.get("result")
if not result:
    st.info("Choose scripts above and press **Check names**.")
    st.stop()

cols = st.columns(5)
for col, (label, value) in zip(cols, [("Episodes", result["episodes"]), ("Lines", result["lines"]),
                                       ("Proper nouns", result["proper_nouns"]),
                                       ("Spelling clusters", len(result["clusters"])),
                                       ("Speaker-tag aliases", len(result["tag_aliases"]))], strict=True):
    col.metric(label, value)
if usage := result.get("llm_usage"):
    st.caption(f"LLM: {usage['calls']} calls, {usage['prompt_tokens']} input / "
               f"{usage['completion_tokens']} output tokens")

st.subheader("Spelling clusters")
st.caption("Severity: 🔴 HIGH = spoken in dialogue · 🟠 MEDIUM = speaker tag · 🟡 LOW = description only")
if not result["clusters"]:
    st.success("No spelling drift found.")
for i, c in enumerate(result["clusters"], 1):
    kept = c.get("llm_kept")
    tag = "" if kept is None else (" · LLM: fix" if kept else " · LLM: dismiss")
    title = (f"{SEV_ICON[c['severity']]} {i}. {c['canonical']} ×{c['canonical_count']} — "
             f"{len(c['variants'])} other spelling(s){tag}")
    with st.expander(title, expanded=kept is not False):
        st.markdown(f"**Suggested spelling:** `{c['canonical']}`")
        plain(f"“{c['canonical_example'].strip()}”", "First seen: ")
        for v in c["variants"]:
            eps = sorted({o["episode"] for o in v["occurrences"]})
            verdict = v.get("llm_verdict")
            line = f"{SEV_ICON[v['severity']]} **{v['spelling']}** ×{v['count']} in ep {eps}"
            if verdict:
                same = verdict["same_entity"]
                line += " — LLM: " + ("**fix**, same entity" if same else
                                      "dismiss, different / alias" if same is False else "no verdict")
            st.markdown(line)
            if verdict:
                plain(verdict["reason"], "Why: ")
            st.dataframe([{"episode": o["episode"], "line": o["line"], "kind": o["kind"], "time": o["time"],
                           "speaker": o["speaker"], "text": o["text"]} for o in v["occurrences"]],
                         hide_index=True, width="stretch")

st.subheader("Speaker-tag aliases")
st.caption("Two tags for what may be one character → risk of two voices or two face references.")
if result["tag_aliases"]:
    st.dataframe([{"short tag": a["short"], "count": a["short_n"], "long tag": a["long"], "count ": a["long_n"],
                   "episodes (long)": ", ".join(map(str, a["episodes_long"]))} for a in result["tag_aliases"]],
                 hide_index=True, width="stretch")
else:
    st.success("No speaker-tag aliases found.")

st.subheader("Download")
files = st.session_state["files"]
d1, d2, d3 = st.columns(3)
# on_click="ignore": a download must not rerun the app (that re-registers the files
# under new URLs and can cancel a download still in flight on a slow connection)
d1.download_button("report.md", files["report.md"], "report.md", "text/markdown", on_click="ignore")
d2.download_button("flags.csv", files["flags.csv"], "flags.csv", "text/csv", on_click="ignore")
d3.download_button("report.json", files["report.json"], "report.json", "application/json", on_click="ignore")
