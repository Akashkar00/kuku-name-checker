# Name-Drift Checker (Kuku AI Generalist, Problem 3)

Spell-check for **names across a whole microdrama series**, run before the script goes to AI
voice/video generation. It finds people and places spelled more than one way, ranks each by what
the viewer would notice, and gives episode + line + quote so an editor can fix it in minutes.

On Kuku's 50-episode input pack it found a main family name spelled **8 different ways, all
spoken in dialogue**, a one-off misspelling of a lead character's name, and 15 speaker-tag
aliases (the same character tagged with and without a title). Details are in the submission's
raw outputs, not in this repo, because the pack is confidential.

## Run

Python 3.10+, standard library only. Nothing to install for the rules.

```bash
python name_checker.py <folder_of_episode_txt_files> --out outputs        # rules only, ~0.2 s
python name_checker.py <folder> --out outputs --llm                       # + LLM judge per cluster
python -m unittest discover tests                                         # 8 tests (demo show + CSV guard)
python name_checker.py samples/demo_show --out outputs/demo --llm         # try it on a public sample
```

Outputs: `report.md` (read this), `flags.csv` (one row per occurrence, for a spreadsheet),
`report.json` (everything).

### Web app (Streamlit)

```bash
uv run --with streamlit streamlit run app.py      # or: pip install -r requirements.txt && streamlit run app.py
```

Pick the bundled demo show or upload episode `.txt` files, optionally tick the LLM judge, and
get the same clusters, tag aliases and downloads as the CLI. If the LLM is not configured the app
says so and shows the rule results. Uploaded files are written to a temp folder for the run and
deleted afterwards.

### Deployed on Google Cloud Run

Live: https://kuku-name-checker-1056072922203.asia-south1.run.app

The rule-based check is open to anyone. The LLM judge costs money per call, so it asks for an
access code. Get it with:
`gcloud secrets versions access latest --secret=kuku-name-checker-llm-code`

| Concern | What is in place |
|---|---|
| Confidential scripts | Docker `COPY` lists files explicitly, and `.gcloudignore` is an allowlist. Only the code and the public demo show reach Cloud Build (`gcloud meta list-files-for-upload` shows 10 files) |
| Identity | Dedicated service account `kuku-name-checker@…` with only `roles/aiplatform.user`, plus read access to one secret. No key files: the Vertex token comes from the metadata server |
| LLM cost | Access code in Secret Manager, compared in constant time; a wrong code is slowed by 1 s; one instance at most |
| Untrusted uploads | File names sanitised; ≤5 MB per file, ≤200 files, ≤20 MB in total; script text and LLM reasons rendered as escaped plain text, never markdown or HTML; CSV cells starting with `= + - @` are neutralised |
| Response headers | `serve.py` wraps the app (`st.App` plus ASGI middleware) and sends a strict CSP (`default-src 'self'`, inline scripts allowed only by hash, `frame-ancestors 'none'`), HSTS, `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Permissions-Policy` and COOP. `/_stcore/metrics` and `/_stcore/agent/*` return 404. The agent API is also switched off in config |
| Web | HTTPS only. Port 80 does not answer, and `.app` is on the browser HSTS preload list. Streamlit XSRF protection is on, a cross-origin websocket is refused (403), and visitors see no error details |
| Container | `python:3.12-slim`, non-root user (uid 10001), code read-only, dependencies installed with `--require-hashes` (pip-audit: no known vulnerabilities) |
| Logs | One timing line per LLM call (latency, tokens). Script text is never logged |

**End-to-end test** in headless Chrome against the live URL (34 checks: headers, blocked
endpoints, demo results, byte-identical downloads, XSS payloads in the logline, dialogue,
speaker tags, file names and a prompt injection aimed at the LLM, the CSV guard, the access-code
gate, the LLM run, CSP violations, console errors, requests to foreign hosts). The access code is
read from Secret Manager inside the test and never printed:

```bash
uv run --with playwright python tests/e2e_browser.py https://kuku-name-checker-1056072922203.asia-south1.run.app \
  --code-secret kuku-name-checker-llm-code
```

Redeploy after a code change (the service keeps its settings):

```bash
gcloud run deploy kuku-name-checker --source . --region asia-south1
```

First-time setup used `--service-account kuku-name-checker@PROJECT.iam.gserviceaccount.com
--allow-unauthenticated --set-secrets APP_PASSWORD=kuku-name-checker-llm-code:latest
--max-instances 1 --concurrency 80 --memory 1Gi --timeout 3600 --session-affinity`.

Rotate the access code (new instances pick it up on the next deploy):

```bash
python3 -c "import secrets,sys; sys.stdout.write(secrets.token_urlsafe(18))" | \
  gcloud secrets versions add kuku-name-checker-llm-code --data-file=-
gcloud run deploy kuku-name-checker --source . --region asia-south1
```

**Known limits:**
- **One instance by design.** Streamlit keeps download files in instance memory, so with 2
  instances a download could reach the wrong one and return 404 (seen in testing). One instance
  with concurrency 80 removes that, but it caps the app at roughly 80 open tabs at once.
- **Two CSP allowances.** `style-src 'unsafe-inline'` is needed for Streamlit's CSS-in-JS, and
  `'wasm-unsafe-eval'` for its WebAssembly. JavaScript `eval` stays blocked.
- **The rule-based check is public.** Uploads exist only in a temp folder for the length of one run.
- **Gemini latency on Vertex varies a lot:** single calls have taken from 3 s to 50 s.
  `reasoning_effort=low` was tested and rejected, because it mislabelled a deliberate alias
  (a disguise name) in both runs.

### LLM judge: swap the model with env vars

| Variable | Default | |
|---|---|---|
| `LLM_PROVIDER` | `vertex` | `vertex` or `openai` (any OpenAI-compatible API) |
| `LLM_MODEL` | `gemini-3.8-flash` | any model the provider serves |
| `VERTEX_PROJECT` / `VERTEX_LOCATION` | gcloud project / `global` | auth = `gcloud auth print-access-token` |
| `LLM_BASE_URL` / `LLM_API_KEY` | Groq | for `openai` provider |

```bash
LLM_PROVIDER=openai LLM_BASE_URL=https://api.groq.com/openai/v1 LLM_API_KEY=... \
LLM_MODEL=llama-3.3-70b-versatile python name_checker.py <folder> --llm
```

## How it works

| Step | Who | |
|---|---|---|
| Tag each line: dialogue / speaker tag / description / notes | code | |
| Candidate names = capitalised words that never appear in lowercase in the show | code | drops "Close", "Then", "Grace" |
| Group look-alikes (difflib ratio ≥ 0.7, same first letter, union-find) | code | catches typos and drift |
| Severity: **HIGH** spoken · **MEDIUM** speaker tag · **LOW** description | rule | what the viewer would notice |
| Per spelling: "same entity → fix" or "different / alias → dismiss" | LLM, optional | one call per group; sees the whole group, speakers and scene lines |
| Fix | human | the tool never edits scripts |

## Evaluate

```bash
python eval_inject.py <folder> --n 30 --seeds 3 [--llm] # recall: plant typos, count caught
python eval_precision.py outputs/report.json gold_labels.json   # precision vs hand labels
```

Results on the input pack:
- **Recall:** 90/90 planted typos with the rules alone, 89/90 end to end with the Gemini judge.
- **Precision:** spelling groups go from 2/4 with the rules alone to **2/2** with the judge, which
  agrees with the hand labels on **10/10** spellings.
- **Speaker-tag aliases:** 4 of 15 are confirmed errors; 11 need a human.
- **Known miss:** a place-name variant whose similarity (0.62) is below the threshold.

## Confidentiality

The input pack is Kuku's content. Episode files, pack outputs and `gold_labels.json` (which quotes
the pack) are git-ignored and shared only in the submission's raw-outputs zip. `samples/demo_show`
is written from scratch and safe to publish.
