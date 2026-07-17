"""Deterministic deadness scoring. The policy file decides; Gemini advises.
Verdicts: zombie-high | zombie-medium | investigate | active."""
from __future__ import annotations

import datetime as dt

from .models import Evidence, PillarScore, ProjectAssessment


def _age_days(iso: str) -> int:
    if not iso:
        return 0
    t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (dt.datetime.now(dt.timezone.utc) - t).days


def score_billing(spend: dict | None) -> PillarScore:
    if spend is None:
        return PillarScore(score=50, confidence="unknown",
                           rationale="billing export not available — neutral score")
    if spend["total"] == 0:
        return PillarScore(score=90, confidence="medium",
                           rationale="zero spend in window (either dead or free-tier)")
    s = 0
    # Composition: pure holding costs = corpse paying rent.
    s += int(spend["holding_pct"])                      # 0-100
    # Variance: FLAT spend beats LOW spend as a deadness signal.
    if spend["variance_coeff"] < 0.05 and spend["months"] >= 2:
        s += 30
    return PillarScore(score=min(100, int(s * 0.8)), confidence="high",
                       rationale=f"holding={spend['holding_pct']}% "
                                 f"variance={spend['variance_coeff']} "
                                 f"total=${spend['total']}")


def score_last_accessed(ev: Evidence, window_days: int,
                        create_time: str = "") -> PillarScore:
    if ev.confidence == "unknown":
        return PillarScore(score=50, confidence="unknown",
                           rationale="audit-log signal unavailable")
    age = ev.detail.get("age_days")
    if age is None:  # no human action in 400d retention
        return PillarScore(score=100, confidence="high",
                           rationale="no human admin action in retained logs")
    # Provisioning grace: a human action inside the first 48h of the
    # project's life is setup, not usage — a forgotten project's only human
    # touch is its own creation.
    if create_time:
        project_age = _age_days(create_time)
        if project_age - age <= 2:
            return PillarScore(score=90, confidence="high",
                               rationale="only provisioning-era human activity "
                                         f"(project {project_age}d old, last "
                                         f"action {age}d ago)")
    if age > window_days:
        return PillarScore(score=85, confidence="high",
                           rationale=f"last human action {age}d ago (> window)")
    return PillarScore(score=max(0, int(age / window_days * 60)),
                       confidence="high",
                       rationale=f"last human action {age}d ago")


def score_connectivity(evidence: list[Evidence], veto_count: int) -> PillarScore:
    if veto_count:
        # vetoes force investigate anyway; keep the pillar informative
        return PillarScore(score=0, confidence="high",
                           rationale=f"{veto_count} hard veto(es) — blast radius exists")
    conn = [e for e in evidence if e.pillar == "connectivity"]
    unknowns = [e for e in evidence if e.pillar == "veto" and e.confidence == "unknown"]

    # Real (low-grade) connectivity surface found: AR repos, KMS keys, etc.
    if conn:
        base = 70
        conf = "medium"
        rationale = f"{len(conn)} low-grade connectivity signal(s)"
    else:
        base = 95
        conf = "high"
        rationale = "no cross-project surface detected"

    # Incomplete checks LOWER CONFIDENCE and shave the score a little — they no
    # longer overwrite the real signal with a flat constant (which used to peg
    # every project with a transient failure to the identical value).
    if unknowns:
        base = min(base, 85) - 3 * len(unknowns)
        conf = "low" if conf == "medium" else "medium"
        rationale += f"; {len(unknowns)} veto check(s) incomplete (lower confidence)"

    return PillarScore(score=max(0, base), confidence=conf, rationale=rationale)


def derive_delete_risk(p: ProjectAssessment, policy: dict) -> ProjectAssessment:
    """Delete-risk profile — the SECOND axis, orthogonal to deadness.
    Deadness asks "should we consider deleting?"; this asks "can we delete
    safely?". Mapping (worst signal wins):
      high   — certain external impact: any veto except a dormant V1 grant
      medium — possible impact: dormant V1, incomplete veto checks, low-grade
               connectivity surface, runtime activity above thresholds,
               recent data-access events, degraded billing signal
      low    — every check completed, clean and quiet
    Runtime signals (machine heartbeat) land HERE, never in the deadness
    score: traffic doesn't make a project alive, but it makes deletion risky."""
    th = policy.get("delete_risk", {})
    reasons: list[str] = []
    level = 0  # 0=low 1=medium 2=high

    def bump(to: int, why: str):
        nonlocal level
        level = max(level, to)
        reasons.append(why)

    for v in p.vetoes:
        if v.code == "V1":
            ev = next((e for e in p.evidence if e.id == v.evidence_id), None)
            if ev and not ev.detail.get("active_use"):
                bump(1, "V1: dormant cross-project SA grant (no auth in 90d) "
                        "— possible dependency")
                continue
        bump(2, f"{v.code}: {v.description}")

    for e in p.evidence:
        if e.pillar == "veto" and e.confidence == "unknown":
            bump(1, f"incomplete check: {e.summary}")
        elif e.pillar == "connectivity" and e.confidence in ("low", "medium"):
            bump(1, f"low-grade surface: {e.summary}")

    rt = p.runtime_activity
    if rt:
        if rt.get("api_requests", 0) > th.get("api_requests_threshold", 100):
            bump(1, f"still serving API traffic ({rt['api_requests']} "
                    "requests in window)")
        if (rt.get("cpu_mean") or 0) > th.get("cpu_mean_threshold", 0.02):
            bump(1, f"running compute (CPU mean {rt['cpu_mean']})")
        if rt.get("net_bytes", 0) > th.get("net_bytes_threshold", 100 * 2**20):
            bump(1, f"instance network traffic ({rt['net_bytes']} bytes)")
        if rt.get("checks_failed"):
            bump(1, "runtime-activity sampling incomplete: "
                    + ", ".join(rt["checks_failed"]))
    da = next((e for e in p.evidence if e.id.startswith("E-dataaccess-")), None)
    if da and da.detail.get("events_sampled", 0) > 0:
        bump(1, f"data-access events in window ({da.summary})")

    billing = p.pillars.get("billing")
    if billing and billing.confidence == "unknown":
        bump(1, "billing signal degraded — spend-based dependencies unverified")

    p.delete_risk = ("low", "medium", "high")[level]
    p.delete_risk_reasons = reasons or ["all checks complete, clean and quiet"]
    return p


def assess(p: ProjectAssessment, policy: dict) -> ProjectAssessment:
    """Combine pillar scores → weighted deadness score → verdict."""
    w = policy["weights"]
    th = policy["verdicts"]

    total = sum(
        p.pillars[k].score * w[k] for k in ("billing", "last_accessed", "connectivity"))
    p.score = int(round(total))

    confs = [p.pillars[k].confidence for k in p.pillars]
    p.confidence = ("low" if "unknown" in confs
                    else "medium" if "medium" in confs or "low" in confs
                    else "high")

    # Tenure guard — young projects are never zombies (unless seeded demos).
    if (_age_days(p.create_time) < policy["tenure_guard_days"]
            and p.labels.get("demo-zombie") != "true"):
        p.tenure_guarded = True
        p.verdict = "active"
        p.recommended_action = "none — inside tenure guard"
        return p

    if p.vetoes:
        p.verdict = "investigate"
        p.recommended_action = ("manual review — hard veto(es): "
                                + ", ".join(v.code for v in p.vetoes))
    elif p.score >= th["zombie_high"]:
        p.verdict = "zombie-high"
        p.recommended_action = ("stage decommission: notify owner → 14d scream "
                                "test (disable APIs) → delete (30d soft-delete)")
    elif p.score >= th["zombie_medium"]:
        p.verdict = "zombie-medium"
        p.recommended_action = "notify owner; re-assess next run"
    else:
        p.verdict = "active"
        p.recommended_action = "none"
    return p
