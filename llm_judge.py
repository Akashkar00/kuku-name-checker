"""Optional LLM step: is a spelling cluster one entity or several?

The rules in name_checker.py only say "these spellings look alike". Some
look-alikes are genuinely different people or places (or a deliberate alias),
so an LLM reads a few quoted lines per spelling and answers same/different.
It never invents flags; it can only confirm or dismiss the ones the rules found.

Two providers, both OpenAI-style chat completions; swap with env vars:
    LLM_PROVIDER     vertex (default) | openai
    LLM_MODEL        default gemini-3.8-flash

  vertex:  auth = `gcloud auth print-access-token` locally, or the attached
           service account via the metadata server on Cloud Run (no key files)
    VERTEX_PROJECT   default: gcloud config project, else the metadata server's
    VERTEX_LOCATION  default: global
  openai:  any OpenAI-compatible endpoint (Groq, OpenAI, an API Kuku provides)
    LLM_BASE_URL, LLM_API_KEY
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from functools import lru_cache

PROMPT = """You check scripts of a TV series for naming mistakes.
Below is a group of similar-looking spellings, each with lines where it appears.
The first one is the most frequent spelling. For EACH other spelling decide:
does it refer to the SAME person/place/thing as the first one (so it is a
spelling mistake to fix), or to a DIFFERENT entity, or is it a deliberate alias
such as a fake name used in disguise (so nothing needs fixing)?
Use the whole group as context: a name spelled many ways for the same family or
place is still one entity. Base the decision only on the quoted lines.

{evidence}

Answer with JSON only:
{{"verdicts": [{{"spelling": "<spelling>", "same_entity": true or false, "reason": "<one sentence>"}}, ...]}}"""


def _evidence(cluster: dict, per_spelling: int = 3) -> str:
    parts = [f'"{cluster["canonical"]}" (x{cluster["canonical_count"]}): {cluster["canonical_example"]}']
    for v in cluster["variants"]:
        quotes = " | ".join(_quote(o) for o in v["occurrences"][:per_spelling])
        parts.append(f'"{v["spelling"]}" (x{v["count"]}): {quotes}')
    return "\n".join(parts)


def _quote(o: dict) -> str:
    """One occurrence with who says it and what is on screen, so the model
    does not have to guess the situation from a bare line."""
    if o.get("speaker"):
        return f'ep{o["episode"]} [scene: {o["context"]}] {o["speaker"]} says: {o["text"]}'
    return f'ep{o["episode"]}: {o["text"]}'


def _gcloud(*args: str) -> str:
    return subprocess.run(["gcloud", *args], capture_output=True, text=True, check=True).stdout.strip()


_METADATA = "http://metadata.google.internal/computeMetadata/v1/"


def _metadata(path: str) -> str:
    req = urllib.request.Request(_METADATA + path, headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.read().decode()


def _vertex_auth() -> tuple[str, str]:
    """-> (project, access token). gcloud CLI on a laptop; on Cloud Run there is
    no CLI, so use the service account attached to the service."""
    project = os.environ.get("VERTEX_PROJECT")
    try:
        return project or _gcloud("config", "get-value", "project"), _gcloud("auth", "print-access-token")
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass
    try:
        token = json.loads(_metadata("instance/service-accounts/default/token"))["access_token"]
        return project or _metadata("project/project-id"), token
    except (OSError, KeyError, ValueError) as e:
        raise SystemExit(f"no Vertex credentials (gcloud CLI or metadata server): {e}") from e


@lru_cache(maxsize=1)
def _endpoint() -> tuple[str, str, str]:
    """-> (url, bearer token, model name as the endpoint expects it)."""
    provider = os.environ.get("LLM_PROVIDER", "vertex")
    model = os.environ.get("LLM_MODEL", "gemini-3.8-flash")
    if provider == "vertex":
        project, token = _vertex_auth()
        loc = os.environ.get("VERTEX_LOCATION", "global")
        host = "aiplatform.googleapis.com" if loc == "global" else f"{loc}-aiplatform.googleapis.com"
        url = f"https://{host}/v1/projects/{project}/locations/{loc}/endpoints/openapi/chat/completions"
        return url, token, f"google/{model}"
    key = os.environ.get("LLM_API_KEY")
    if not key:
        raise SystemExit("LLM_PROVIDER=openai needs LLM_API_KEY (and LLM_BASE_URL)")
    base = os.environ.get("LLM_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
    return f"{base}/chat/completions", key, model


def model_name() -> str:
    return _endpoint()[2]


def _post(prompt: str, retries: int = 3) -> tuple[str, dict]:
    """-> (reply text, token usage of this one call)."""
    url, token, model = _endpoint()
    body = {"model": model, "temperature": 0, "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json",
                                          "User-Agent": "name-checker/0.1"})
    for attempt in range(retries):
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.load(r)
            u = data.get("usage") or {}
            usage = {"calls": 1, "prompt_tokens": u.get("prompt_tokens", 0),
                     "completion_tokens": u.get("completion_tokens", 0)}
            # timing only, never script text: goes to stderr / Cloud Logging
            print(f"llm_call ok attempt={attempt + 1} secs={time.monotonic() - t0:.1f} "
                  f"tokens={usage['prompt_tokens']}+{usage['completion_tokens']}", file=sys.stderr)
            return data["choices"][0]["message"]["content"], usage
        except urllib.error.HTTPError as e:
            print(f"llm_call http_{e.code} attempt={attempt + 1} secs={time.monotonic() - t0:.1f}", file=sys.stderr)
            if e.code in (429, 500, 503) and attempt < retries - 1:
                time.sleep(2 ** attempt * 3)
                continue
            raise SystemExit(f"LLM call failed: HTTP {e.code} {e.read()[:300]!r}") from e
        except (TimeoutError, urllib.error.URLError) as e:  # network blip: retry, then give up cleanly
            print(f"llm_call network_error attempt={attempt + 1} secs={time.monotonic() - t0:.1f}", file=sys.stderr)
            if attempt < retries - 1:
                time.sleep(2 ** attempt * 3)
                continue
            raise SystemExit(f"LLM call failed: {e}") from e
    raise SystemExit("LLM call failed after retries")


def judge_cluster(cluster: dict) -> tuple[dict[str, dict], dict]:
    """One call per cluster -> ({spelling: verdict} for every variant, usage).

    v1 gave one verdict per cluster, so a deliberate alias (a disguise name) vetoed
    real typos of the same name in that cluster. v2 judged each variant on its
    own and lost the group context (one spelling alone looked like a separate
    family). v3 shows the whole group but asks for a verdict per spelling."""
    text, usage = _post(PROMPT.format(evidence=_evidence(cluster)))
    model = model_name()
    out: dict[str, dict] = {}
    try:
        for v in json.loads(text.strip().removeprefix("```json").removesuffix("```"))["verdicts"]:
            out[v["spelling"]] = {"same_entity": bool(v["same_entity"]),
                                  "reason": v.get("reason", ""), "model": model}
    except (json.JSONDecodeError, KeyError, TypeError):
        pass
    missing = {"same_entity": None, "reason": f"no verdict in reply: {text[:120]}", "model": model}
    return {v["spelling"]: out.get(v["spelling"], missing) for v in cluster["variants"]}, usage
