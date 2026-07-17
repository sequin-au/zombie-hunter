"""Zombie Hunter assessment pipeline — Cloud Run Job entrypoint.

Stages: census → triage → deep assessment (candidates) → analyst → publish.
Config via env: ORG_ID, RESULTS_BUCKET, BILLING_DATASET, GOOGLE_CLOUD_PROJECT,
WEBHOOK_URL (optional), POLICY_PATH (default config/policy.yaml)."""
from __future__ import annotations

import datetime as dt
import logging
import os
import uuid

import yaml
from google import genai

from . import analyzer, collectors, scoring
from .models import Evidence, ProjectAssessment, RunResult

logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
log = logging.getLogger("zombie.main")

# The tool must never assess itself; EXCLUDE_PROJECTS (comma-separated)
# adds deployment-specific exclusions like foundation plumbing projects.
EXCLUDE_ALWAYS = {os.environ.get("GOOGLE_CLOUD_PROJECT", "")} | {
    p.strip() for p in os.environ.get("EXCLUDE_PROJECTS", "").split(",") if p.strip()}


def run() -> RunResult:
    org_id = os.environ["ORG_ID"]
    bucket = os.environ["RESULTS_BUCKET"]
    dataset = os.environ.get("BILLING_DATASET", "")
    policy = yaml.safe_load(open(os.environ.get("POLICY_PATH", "config/policy.yaml")))
    window = policy["window_days"]
    notes: list[str] = []

    now = dt.datetime.now(dt.timezone.utc)
    result = RunResult(
        run_id=f"run-{now:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}",
        timestamp=now.isoformat(), org_id=org_id,
        policy_version=policy["policy_version"],
        observable_window_days=window, population=0, deep_assessed=0,
        notes=notes)

    # ---- Stage 1: triage (all projects, cheap) ----
    projects = [p for p in collectors.census(org_id)
                if p["project_id"] not in EXCLUDE_ALWAYS]
    result.population = len(projects)
    spend = collectors.billing_sketch(dataset, window, policy, notes) if dataset else {}
    if not dataset:
        notes.append("BILLING_DATASET unset — billing pillar neutral")

    candidates = []
    for p in projects:
        rec = collectors.unattended_recommendations(p["project_id"])
        p["_rec"] = rec
        p["_spend"] = spend.get(p["project_id"])
        # Candidate = recommender flagged it, OR spend profile smells dead,
        # OR no billing signal at all (must look closer to be safe).
        sp = p["_spend"]
        # Flat spend needs >=2 billed months to prove itself; younger projects
        # with a pure holding-cost profile smell dead on composition alone.
        smells = sp is None or sp["total"] == 0 or (
            sp["holding_pct"] > 80
            and (sp["months"] < 2 or sp["variance_coeff"] < 0.1))
        if rec is not None or smells:
            candidates.append(p)
    log.info("triage: %d/%d candidates for deep assessment",
             len(candidates), len(projects))

    # Seasonality guard: 13-month recurrence analysis needs enough billing
    # history to see a cycle. Until the export has accrued it, say so —
    # a young export must not silently pass DR/seasonal projects as safe.
    hist_months = collectors.billing_history_months(dataset) if dataset else 0
    min_months = policy.get("delete_risk", {}).get("seasonality_min_months", 6)
    seasonality_ok = hist_months >= min_months
    if not seasonality_ok:
        notes.append(
            f"seasonality analysis unavailable — billing export holds "
            f"{hist_months} month(s), needs >={min_months} (13-month "
            f"DR/seasonal-use detection matures as history accrues)")

    # Owner attestations recorded via the portal (typed + expiring).
    from google.cloud import firestore
    try:
        acks = {d.id: d.to_dict()
                for d in firestore.Client().collection("acks").stream()}
    except Exception as e:  # noqa: BLE001
        acks = {}
        notes.append(f"attestation lookup failed ({type(e).__name__})")

    # ---- Stage 2: deep assessment ----
    gclient = genai.Client(vertexai=True,
                           project=os.environ["GOOGLE_CLOUD_PROJECT"],
                           location=policy["analyst"]["location"])
    deep_ids = {c["project_id"] for c in candidates}
    inventories: dict[str, list] = {}

    for p in projects:
        a = ProjectAssessment(
            project_id=p["project_id"], project_number=p["project_number"],
            display_name=p["display_name"], folder=p["parent"],
            labels=p["labels"], create_time=p["create_time"])

        if p["project_id"] not in deep_ids:
            a.verdict = "active"
            a.confidence = "high"
            a.recommended_action = "none — passed triage"
            a.pillars = {k: scoring.PillarScore(0, "high", "passed triage")
                         for k in ("billing", "last_accessed", "connectivity")}
            result.projects.append(a)
            continue

        result.deep_assessed += 1
        a.owner_of_record = collectors.owner_of_record(p["project_id"])
        last = collectors.last_human_action(org_id, p["project_id"], window)
        vetoes, veto_ev = collectors.veto_sweep(
            org_id, p["project_id"], p["project_number"], p["labels"],
            policy["vetoes"])
        a.vetoes = vetoes
        a.evidence = [last] + veto_ev

        # Delete-risk signals: runtime activity + data access feed
        # delete_risk ONLY — never the deadness pillars below.
        a.runtime_activity, rt_ev = collectors.runtime_activity(
            p["project_id"], window)
        a.evidence.append(rt_ev)
        a.evidence.append(collectors.data_access_activity(
            p["project_id"], window))
        a.resources_summary, inv = collectors.resource_inventory(p["project_id"])
        if inv:
            inventories[p["project_id"]] = inv
        if not seasonality_ok:
            a.evidence.append(Evidence(
                id=f"E-seasonality-{p['project_id']}", pillar="runtime",
                summary=f"DR/seasonal-use check unavailable: {hist_months} "
                        f"month(s) of billing history (needs >={min_months})",
                confidence="unknown"))
        ack = acks.get(p["project_id"], {})
        if ack and (not ack.get("expires") or ack["expires"] >= now.isoformat()):
            a.attestation = ack

        if p["_rec"]:
            a.evidence.append(p["_rec"])
        if p["_spend"] is not None:
            a.evidence.append(Evidence(
                id=f"E-billing-{p['project_id']}", pillar="billing",
                summary=f"Spend window: ${p['_spend']['total']} "
                        f"({p['_spend']['holding_pct']}% holding-cost SKUs, "
                        f"variance {p['_spend']['variance_coeff']})",
                detail=p["_spend"], confidence="high"))

        a.pillars = {
            "billing": scoring.score_billing(p["_spend"]),
            "last_accessed": scoring.score_last_accessed(
                last, window, p["create_time"]),
            "connectivity": scoring.score_connectivity(a.evidence, len(vetoes)),
        }
        a = scoring.assess(a, policy)
        a = scoring.derive_delete_risk(a, policy)

        # Analyst pass only where it matters (zombie/investigate verdicts).
        if a.verdict != "active":
            verdictless = analyzer.analyse(
                gclient, policy["analyst"]["model"],
                policy["analyst"]["temperature"],
                a.project_id, a.verdict, a.score, a.evidence, a.labels,
                a.pillars)
            a = analyzer.apply_analyst(a, verdictless)
        result.projects.append(a)

    # ---- Stage 3: publish ----
    from . import publisher
    out = publisher.publish(result, bucket, os.environ.get("WEBHOOK_URL", ""),
                            inventories=inventories)
    log.info("done: %s (%d transitions)", out["gcs"], len(out["transitions"]))
    return result


if __name__ == "__main__":
    run()
