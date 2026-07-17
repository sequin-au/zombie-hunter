"""Publishes one run: results.json (canonical) → GCS + Firestore, report.md
RENDERED FROM the JSON (single source of truth), optional webhook POST on
verdict transitions."""
from __future__ import annotations

import json
import logging
import os
import urllib.request

from google.cloud import firestore, storage

from .models import RunResult

log = logging.getLogger("zombie.publisher")

VERDICT_ORDER = ["zombie-high", "zombie-medium", "investigate", "active"]
BADGE = {"zombie-high": "🧟", "zombie-medium": "🧟‍♂️",
         "investigate": "🔍", "active": "✅"}
RISK_BADGE = {"low": "🟢", "medium": "🟡", "high": "🔴"}


def render_report(run: RunResult) -> str:
    d = run.to_dict()
    counts = {v: 0 for v in VERDICT_ORDER}
    for p in d["projects"]:
        counts[p["verdict"]] = counts.get(p["verdict"], 0) + 1

    lines = [
        "# Zombie Hunter — Tenancy Assessment",
        f"*Run `{d['run_id']}` — {d['timestamp']} — org {d['org_id']} — "
        f"policy {d['policy_version']}*",
        "",
        f"**{d['population']}** projects triaged, **{d['deep_assessed']}** "
        f"deep-assessed. Verdicts: "
        + ", ".join(f"{BADGE[v]} {v}: **{counts[v]}**" for v in VERDICT_ORDER),
        "",
    ]
    if d["notes"]:
        lines += ["> **Degraded signals:** " + " · ".join(d["notes"]), ""]

    for verdict in VERDICT_ORDER:
        ps = [p for p in d["projects"] if p["verdict"] == verdict]
        if not ps or verdict == "active":
            continue
        lines += [f"## {BADGE[verdict]} {verdict} ({len(ps)})", ""]
        for p in sorted(ps, key=lambda x: -x["score"]):
            lines += [f"### `{p['project_id']}` — score {p['score']}/100 "
                      f"(confidence: {p['confidence']})",
                      f"- **Owner of record:** {p['owner_of_record'] or 'unknown'}",
                      f"- **Recommended action:** {p['recommended_action']}"]
            if p.get("delete_risk"):
                lines.append(
                    f"- **Delete risk:** {RISK_BADGE[p['delete_risk']]} "
                    f"{p['delete_risk']} — "
                    + "; ".join(p["delete_risk_reasons"][:4]))
            inv = p.get("resources_summary") or {}
            if inv.get("total") is not None:
                top = ", ".join(f"{t.split('/')[-1]}×{n}" for t, n in
                                list(inv.get("by_type", {}).items())[:5])
                lines.append(f"- **Resources:** {inv['total']} ({top})")
            if p.get("attestation"):
                at = p["attestation"]
                lines.append(f"- **Owner attestation:** {at.get('type', 'ack')} "
                             f"by {at.get('by', '?')}"
                             + (f" — expires {at['expires'][:10]}"
                                if at.get("expires") else ""))
            if p["vetoes"]:
                lines.append("- **Vetoes:** " + "; ".join(
                    f"{v['code']} ({v['description']})" for v in p["vetoes"]))
            for name, pillar in p["pillars"].items():
                lines.append(f"- {name}: {pillar['score']}/100 — "
                             f"{pillar['rationale']}")
            a = p.get("analyst") or {}
            if a.get("case_against_zombie"):
                lines += ["", f"> **Analyst (self-challenge, "
                              f"{a.get('final_position')}):** "
                              f"{a['case_against_zombie']}"]
            lines += ["", "<details><summary>Evidence "
                          f"({len(p['evidence'])})</summary>", ""]
            for e in p["evidence"]:
                lines.append(f"- `{e['id']}` [{e['confidence']}] {e['summary']}")
            lines += ["</details>", ""]

    active = [p for p in d["projects"] if p["verdict"] == "active"]
    lines += [f"## ✅ active ({len(active)})", "",
              ", ".join(f"`{p['project_id']}`"
                        + (" *(tenure-guarded)*" if p["tenure_guarded"] else "")
                        for p in active), ""]
    return "\n".join(lines)


def publish(run: RunResult, bucket_name: str, webhook_url: str = "",
            inventories: dict | None = None) -> dict:
    date = run.timestamp[:10]
    payload = json.dumps(run.to_dict(), indent=1, default=str)
    report = render_report(run)

    bucket = storage.Client().bucket(bucket_name)
    for path, body, ctype in (
            (f"runs/{date}/results.json", payload, "application/json"),
            (f"runs/{date}/report.md", report, "text/markdown"),
            ("runs/latest/results.json", payload, "application/json"),
            ("runs/latest/report.md", report, "text/markdown")):
        bucket.blob(path).upload_from_string(body, content_type=ctype)
    # Full per-candidate resource inventories — the "what exactly dies if we
    # delete this" artifact; summaries live in results.json.
    for pid, items in (inventories or {}).items():
        bucket.blob(f"runs/{date}/inventory/{pid}.json").upload_from_string(
            json.dumps(items, indent=1), content_type="application/json")
    log.info("published to gs://%s/runs/%s/ (%d inventories)",
             bucket_name, date, len(inventories or {}))

    # Firestore: run summary + per-project history + verdict transitions
    db = firestore.Client()
    transitions = []
    for p in run.projects:
        pd = {"run_id": run.run_id, "date": date, "verdict": p.verdict,
              "score": p.score, "confidence": p.confidence,
              "vetoes": [v.code for v in p.vetoes],
              "delete_risk": p.delete_risk}
        ref = db.collection("projects").document(p.project_id)
        prev = ref.get().to_dict() or {}
        if prev.get("verdict") and prev["verdict"] != p.verdict:
            transitions.append({"project_id": p.project_id,
                                "from": prev["verdict"], "to": p.verdict,
                                "date": date})
        ref.set(pd)
        ref.collection("history").document(run.run_id).set(pd)
    db.collection("runs").document(run.run_id).set({
        "run_id": run.run_id, "timestamp": run.timestamp, "date": date,
        "population": run.population, "deep_assessed": run.deep_assessed,
        "verdicts": {v: sum(1 for p in run.projects if p.verdict == v)
                     for v in VERDICT_ORDER},
        "transitions": transitions, "notes": run.notes,
        "policy_version": run.policy_version})

    if webhook_url and transitions:
        try:
            req = urllib.request.Request(
                webhook_url, method="POST",
                data=json.dumps({"run_id": run.run_id,
                                 "transitions": transitions}).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:  # noqa: BLE001
            log.warning("webhook POST failed: %s", e)

    return {"transitions": transitions, "gcs": f"gs://{bucket_name}/runs/{date}/"}
