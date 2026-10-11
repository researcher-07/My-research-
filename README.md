# Atlas Research 2.0 — deployable multi-source research website

Atlas Research is a mobile-friendly research application with a Python API backend. It accepts a plain-language question, searches selected public research sources concurrently, applies visible query-expansion rules, deduplicates overlapping records, preserves source URLs/metadata, and exports results as CSV or JSON.

## What is included

### Direct connectors (queried by Atlas)

- **OpenAlex** — scholarly works, authorship, OA flags and citation counts
- **Crossref** — DOI and publication metadata
- **arXiv** — preprints
- **PubMed** — biomedical literature
- **Wikipedia** — topic orientation; not primary evidence
- **Europe PMC** — biomedical and life-sciences literature
- **Semantic Scholar** — scholarly papers and citation metadata (API availability/rate limits may vary)

The UI also offers links to Google Web, Google Scholar, Semantic Scholar search, Google Patents, the Wayback Machine, and Google dorking. These are supplemental links, not APIs queried by Atlas.

### Research workflow

- Research depth presets: **Quick** (core query), **Balanced** (core + one expansion), **Deep** (up to three query angles)
- Transparent rule-based query expansion for a few common technical topics; generic fallback angles use review/evaluation/limitations terms
- Parallel source calls with per-source status and partial-failure handling
- Deduplication by DOI, then normalized title
- Evidence metadata, source overlap, in-browser result filtering/sorting, local recent-search history
- CSV and JSON exports
- Public endpoints: `GET /api/health`, `POST /api/search`

Query expansion is deliberately transparent and rule-based. It is not an LLM, does not establish scientific consensus, and does not guarantee exhaustive coverage.

## Deploy as a normal website (recommended: Render)

This repository contains `render.yaml` so the deployment settings are already written. Render can run the Flask app behind Gunicorn and provide a public HTTPS URL.

1. Extract this ZIP on a computer or phone.
2. Put the extracted files into a Git repository on GitHub. Keep `render.yaml`, `webapp.py`, `engine.py`, `requirements.txt`, `Dockerfile`, and the `templates/` folder at the repository root.
3. Sign in to Render and create a **New → Blueprint** from that repository, or create a **Web Service** with these settings:
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn --workers 2 --threads 4 --timeout 100 --bind 0.0.0.0:$PORT webapp:app`
   - Health check path: `/api/health`
4. Wait for the first deployment to finish. Render will provide an HTTPS `onrender.com` URL. Open that URL on your phone; no Termux or local server is needed after deployment.

Official hosting guide: https://render.com/docs/deploy-flask
Blueprint reference: https://render.com/docs/blueprint-spec

**Phone note:** On Android, extract the ZIP with a file manager. Upload the extracted files to a new GitHub repository using GitHub's website, or use Termux + Git if preferred. Upload the *contents* of the extracted project, not only the ZIP file.

A free web service may sleep when idle and take longer on the first request after sleeping. See Render's current plan limits before publishing: https://render.com/docs/your-first-deploy

## Run locally

Python 3.10+ is recommended.

```bash
python -m venv .venv
# macOS/Linux
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
python webapp.py
```

Then visit http://127.0.0.1:5055.

For a production-like local run:

```bash
gunicorn --workers 2 --threads 4 --timeout 100 --bind 127.0.0.1:5055 webapp:app
```

## API example

```bash
curl -X POST http://127.0.0.1:5055/api/search \
  -H 'Content-Type: application/json' \
  -d '{"question":"eco-friendly soil stabilization methods","sources":["openalex","crossref","arxiv","pubmed","wikipedia","europepmc","semanticscholar"],"limit":8,"depth":"deep","year_from":2018,"year_to":2026}'
```

Valid `depth`: `quick`, `balanced`, `deep`. Valid source keys are returned from `GET /api/health`.

## Production/security notes

- Atlas does not bypass paywalls, login screens, access controls, or robots/access policies.
- HTTP responses include basic browser security headers; JSON requests have body-size limits. The app applies a best-effort in-process search rate limit and limits concurrent searches per worker.
- Rate limiting is not a distributed abuse-prevention system. Before a high-traffic or sensitive public launch, add a shared limiter, monitoring, and any required authentication/quotas. Do not submit private or confidential research questions to a public instance.
- Search queries are not persisted server-side by Atlas. Recent searches live only in the user's own browser local storage. Hosting providers may still log operational request metadata.
- Never commit credentials or API keys. Public source APIs can change, be rate-limited, or return partial results.
- A successful connector call with zero records does not prove that no evidence exists. Verify important claims against original source documents and look for contradictory evidence.

## Health check

```bash
curl http://127.0.0.1:5055/api/health
```
