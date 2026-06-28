# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A one-shot Google Cloud Run Job (Python 3.11) that runs a configured list of BigQuery
queries, checks each result against a threshold, and sends **one** email containing
every query whose threshold was met. It is a plain script (`python main.py`) that runs
to completion and exits — **not** a web server, no HTTP endpoint. It is built to be
cloned and re-pointed at a different GCP project / dataset / set of queries with **zero
changes to `main.py`**.

## Transferability contract (the whole point of this repo)

When reusing this on another project, only these change:

1. **`config.json`** — `project_id` (null = use ambient default), `dataset`, email
   recipients/subject, and the `queries` list (name, SQL file, threshold condition).
2. **`SQL/*.sql`** — the queries. Tables must be referenced as `` `{{dataset}}.table` ``;
   the `{{dataset}}` placeholder is substituted at runtime from `config.json`.
3. **Secret env vars** — `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD` (see `.env.example`).

`main.py` is generic and should stay untouched when porting. Keep it that way — if a
new requirement seems to need editing `main.py`, prefer expressing it in `config.json`.

## Architecture (`main.py`)

- Entry point `main()` (run as `python main.py`), which wraps `run_pipeline()` in a
  try/except: on any exception it prints to stderr and `sys.exit(1)` so the Cloud Run
  Job execution is marked **Failed**; otherwise it exits 0. Flow:
  load config → for each query, `render_sql` (placeholder substitution) → run on
  BigQuery → filter rows via `row_meets_condition` → collect triggered queries →
  if any, build one HTML email and send.
- **Condition model**: each query has a `condition`. Two operator families:
  - Binary (`OPERATORS` dict — `>`, `>=`, `<`, `<=`, `==`, `!=`): `{column, operator, value}`,
    included when `row[column] <op> value`.
  - Unary BOOL (`BOOL_OPERATORS` dict — `is_true`, `is_false`): `{column, operator}` with no
    `value`, included when the BOOL column is exactly `True` / `False`.
  A query with **no** `condition` is **skipped** (not run, not emailed) — the function is an
  alerter, so only condition matches matter. The email is sent only when ≥1 query has matching
  rows; nothing is sent when all clear. To add an operator, extend the relevant dict in `main.py`.
- **Result-set size**: the condition runs against *every* returned row and *every* match is
  emailed. Queries must return only the row(s) to alert on (e.g. `ORDER BY ... LIMIT 1`), or a
  large result set will alert on any single matching row and email them all.
- **Email**: stdlib `smtplib` + Gmail over `smtp.gmail.com:587` with STARTTLS
  (`timeout=30` so a blocked port fails fast instead of hanging until the job times out).
  Port 25 is blocked on Cloud Run; 587 is not. Requires a Gmail **App Password**,
  not the account password.
- **Query order** is the order in `config.json["queries"]`, not filesystem glob order.
  (SQL filenames are prefixed `1_`, `2_`, … by convention for human readability only.)

## Commands

Run locally (PowerShell) — runs once and exits, no server:

```powershell
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:GMAIL_ADDRESS="you@gmail.com"; $env:GMAIL_APP_PASSWORD="app-password"
python main.py
```

Authenticate to BigQuery first with `gcloud auth application-default login`.

## Deployment (Cloud Build → Cloud Run Job → Cloud Scheduler)

The deployed pipeline has three pieces:

1. **Cloud Build builds the Docker image.** The `Dockerfile` (`CMD ["python", "main.py"]`)
   is built into a container image pushed to Artifact Registry. Wire a Cloud Build trigger
   to the repo so every push rebuilds the image automatically.
2. **A Cloud Run Job executes that image.** The job runs the container to completion and
   exits — there is no served URL. `GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD` are set on the job
   (prefer Secret Manager via `--set-secrets` for the password in production).
3. **Cloud Scheduler triggers the job on the desired routine** (Cloud Run Jobs expose a
   built-in scheduler trigger), so the alert queries run on a cron schedule.

```powershell
# Build & (re)deploy the job from source — Cloud Build does the image build:
gcloud run jobs deploy bq-alert-job --source . --region <region> `
  --set-env-vars GMAIL_ADDRESS=you@gmail.com,GMAIL_APP_PASSWORD=app-password

# Run once on demand:
gcloud run jobs execute bq-alert-job --region <region>
```