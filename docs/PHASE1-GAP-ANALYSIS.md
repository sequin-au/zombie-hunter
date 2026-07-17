# Phase 1 requirements — "Project Delete Risk Profile" gap analysis

Traceability from an internal requirements document ("Phase 1: Project
Delete Risk Profile — automated delete risk assessment") to this
implementation. The document asks a deceptively different question than
Zombie Hunter originally answered:

- **Zombie Hunter's question:** *should we consider deleting this?* (deadness)
- **The document's question:** *is it safe to delete this?* (impact profile:
  Low = no impact, Medium = possible impact, High = certain impact)

Rather than bend the deadness score, the implementation emits both as
orthogonal axes: `verdict`/`score` (deadness) and `delete_risk`
(safety), per ARCHITECTURE §3.3. A zombie-high with delete_risk=low is
the ideal decommission candidate.

## Requirement coverage

| # | Requirement | Status | Where |
|---|---|---|---|
| 1 | Cross-project IAM dependencies (SA grants elsewhere) | ✅ | V1 (CAI org-scope IAM search) + Policy Analyzer SA last-authentication distinguishing *actively exercised* (high) from *dormant* (medium) grants |
| 2 | CMEK / KMS blast radius | ✅ | V2 (project-scoped key + external-principal grant sweep) |
| 3 | API keys as hidden external credentials | ✅ | V8: keys bound to service accounts veto (certain external auth path); unbound keys are evidence-grade connectivity surface |
| 4 | Runtime API usage (is anything still calling in?) | ✅ | Cloud Monitoring `serviceruntime/api/request_count` per consumed service — feeds `delete_risk` only, never deadness |
| 5 | Compute / network activity | ✅ | Instance CPU utilization + NIC byte counters (Cloud Monitoring). VPC Flow Logs only where enabled — instance counters are the documented fallback |
| 6 | Recent data access (any principal) | ✅ | Data Access audit-log sample: `DATA_READ`/`DATA_WRITE` only (ADMIN_READ is control-plane noise — Terraform refreshes, console browsing), excluding the tool's own SAs and Google platform agents (the observer effect: the pipeline's daily reads would otherwise mark every project "in use"); capped at `medium` confidence because only services with Data Access logging enabled appear (BigQuery has it by default) |
| 7 | Explicit do-not-delete markers | ✅ | V7: resource liens + `retain=true` label |
| 8 | Network fabric dependencies | ✅ | V3 extended: Shared VPC host detection (subnet IAM grants to external principals), VPC peering, PSC, VPN tunnels, custom routes |
| 9 | Destination-for-others (state, DNS, sinks, registries) | ✅ | V5 (DNS zones, tfstate-named buckets) + V6 (Artifact Registry, evidence-grade) + V4 (OAuth/IAP brands) |
| 10 | DR / seasonal-use detection (13-month lookback) | ⚠️ partial, honestly bounded | 400-day Admin Activity lookback covers annual human exercise cadence; billing seasonality analysis activates at ≥6 months of export history (13-month recurrence as it accrues — a young export emits an explicit degraded-signal note instead of silently passing); typed expiring `no-dr-seasonal-use` owner attestations cover the residual window |
| 11 | Owner identification + human-in-the-loop | ✅ | `owner_of_record`, advice-only verdicts, portal attestations, scream-test workflow contract (§5.4) |
| 12 | Full resource inventory per deletion candidate | ✅ | CAI dump per candidate: counts in `results.json` (`resources_summary`), full listing at `runs/<date>/inventory/<project_id>.json` |

## Known limitations (by design, stated not hidden)

- **Billing seasonality** matures with the export: recurrence analysis
  needs ≥6 months of history and ideally 13. Until then every run carries
  a run-level note and per-candidate `unknown` evidence.
- **VPC Flow Logs** are not parsed; instance NIC counters are the network
  signal. Flow-log parsing only pays off where flow logs are enabled,
  which is rare in dev tenancies.
- **Data Access logs** cover only services where that log type is enabled;
  absence of events is weak evidence and scored accordingly.
- **Attestations annotate, never override:** a `no-dr-seasonal-use`
  attestation is recorded on the assessment but does not lower the
  automated `delete_risk` rating — humans vouch, sensors decide.

## Design invariant preserved

Runtime activity (API traffic, CPU, network, data access) feeds
`delete_risk` **only**. Machine heartbeat never makes a project alive —
the heartbeat seed stays a zombie — but it does make deletion risky.
Keeping the two axes separate is the core of this phase.
