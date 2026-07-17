# 🧟 Zombie Hunter

*GCP, the walking dead.*

Finds forgotten test/dev projects in a GCP organization and produces an
evidence-cited assessment — without ever risking a project that still matters.

Full design: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Infra:
Terraform in this repo at `terraform/` (backend + deployment values via
untracked `backend.hcl` / `terraform.tfvars`; examples committed).

## How it decides

Zombies are **self-contained activity, not zero activity**. Three pillars:

| Pillar | Signal |
|---|---|
| Billing (0.35) | Spend **composition** (holding-cost SKUs) + **variance** (flat beats low) |
| Last accessed (0.40) | Last **human** admin action from audit logs — machine heartbeat ≠ life |
| Connectivity (0.25) | Cross-project blast radius |

Eight **hard vetoes** (V1 cross-project SA — corroborated with Policy
Analyzer last-auth, V2 CMEK, V3 shared-VPC/peering/PSC/VPN/custom-routes,
V4 OAuth clients, V5 sink/DNS/TF-state destination, V6 pulled AR images,
V7 liens/retain, V8 SA-bound API keys) force `investigate` regardless of
score. Projects younger than the tenure guard are never zombies (unless
labelled `demo-zombie=true`). Gemini is the **analyst, not the sensor**: it
argues for the verdict, then against itself, citing evidence IDs — a
challenge can only make the verdict safer. All thresholds live in
`config/policy.yaml` (policy-as-file).

Each deep-assessed project also gets a **delete-risk profile**
(`low`/`medium`/`high`) — the orthogonal "can we delete *safely*?" axis,
fed by the vetoes plus runtime-activity signals (API traffic, instance
CPU/network, data-access events) that deliberately never touch the deadness
score, a full CAI resource inventory, and typed expiring owner attestations.
See `docs/ARCHITECTURE.md` §3.3 and `docs/PHASE1-GAP-ANALYSIS.md`.

## Components

- `pipeline/` — Cloud Run Job: census → triage (all projects, cheap) → deep
  assessment (candidates: audit mining + veto sweep) → analyst → publish.
  Output: `results.json` (canonical) + `report.md` (rendered FROM the JSON)
  to `gs://sdx-zombie-results/runs/<date>/`, Firestore run/history/transition
  docs, optional webhook POST on verdict transitions.
- `portal/` — demo-only FastAPI + SPA on Cloud Run (built-in IAP, run.app
  URL). Views: verdict cards, evidence, transition feed, run history.
  Actions: run-now (concurrency-guarded), schedule presets, typed expiring
  attestations (known-keep / no-DR-seasonal-use), seed/teardown demo
  zombies. Prod would be programmatic-only.
- `seeder/` — Cloud Run Job populating/stripping resources inside a
  **TF-managed pool** of 5 low-cost demo projects (`sdx-zh-*`) in the Zombie
  Demo folder. It cannot create or delete projects — the pool itself is
  Terraform, applied by a human.
- `config/policy.yaml` — versioned assessment policy.

## Service accounts (governance line)

- `zombie-pipeline` — org-wide **read-only** (browser, asset/recommender/KMS
  viewer, security reviewer, policy analyzer, logging viewer).
- `zombie-portal` — operates the tool only; zero tenancy rights.
- `zombie-seeder` — mutates resources **inside the 5 pool projects only**;
  holds NO projectCreator/projectDeleter anywhere (removed 2026-07-15).

**Every delete requires a human.** The pipeline cannot mutate the tenancy
(viewer-only IAM + no mutating code — verdicts are advice). Real-project
decommission is the manual notify → scream-test → delete workflow, run by a
human admin outside the tool. Demo teardown only fires from a human clicking
the IAP'd portal button, and only strips resources inside the pool. Nothing
on a schedule can delete anything: the only unattended job is the read-only
pipeline.

## Deploy

```sh
gcloud builds submit --project sdx-demos-zombie-dev \
  --config cloudbuild.yaml
# jobs + service + scheduler are managed in the TF stage (phase 2)
```

## Notes

- Billing export must be enabled manually (Billing console → BigQuery export
  → dataset `sdx-demos-zombie-dev.billing_export`); the pipeline degrades
  gracefully until data accrues.
- Unattended Project Recommender needs ~30 days of observation; structural
  signals and audit history work immediately — seed ahead of demo sessions.
- Decommission is staged: notify → scream test → delete (30-day soft-delete).
  Never blind billing-disable.
