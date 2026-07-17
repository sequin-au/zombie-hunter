"""Canonical data model. results.json is the single source of truth;
report.md is RENDERED from it, never generated in parallel."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

SCHEMA_VERSION = "1.1"   # 1.1: + delete_risk, runtime evidence, inventory, attestation


@dataclass
class Evidence:
    id: str                 # e.g. "E-billing-01" — analyst citations point here
    pillar: str             # billing | last_accessed | connectivity | veto | runtime | best_practice
    summary: str
    detail: dict = field(default_factory=dict)
    confidence: str = "high"  # high | medium | low | unknown


@dataclass
class Veto:
    code: str               # V1..V8
    description: str
    evidence_id: str


@dataclass
class PillarScore:
    score: int              # 0-100, 100 = deadest
    confidence: str
    rationale: str


@dataclass
class ProjectAssessment:
    project_id: str
    project_number: str = ""
    display_name: str = ""
    folder: str = ""
    labels: dict = field(default_factory=dict)
    create_time: str = ""
    owner_of_record: str = ""          # best-effort from IAM / audit trail
    verdict: str = "active"            # zombie-high | zombie-medium | investigate | active
    score: int = 0
    confidence: str = "unknown"
    # Second axis (deep-assessed only): verdict answers "should we consider
    # deleting?"; delete_risk answers "can we delete SAFELY?" — a zombie-high
    # with delete_risk=low is the ideal decommission candidate.
    delete_risk: str = ""              # low | medium | high | "" (not assessed)
    delete_risk_reasons: list = field(default_factory=list)
    runtime_activity: dict = field(default_factory=dict)   # monitoring sample
    resources_summary: dict = field(default_factory=dict)  # CAI inventory counts
    attestation: dict = field(default_factory=dict)        # owner attestation (portal)
    tenure_guarded: bool = False
    vetoes: list = field(default_factory=list)      # [Veto]
    pillars: dict = field(default_factory=dict)     # name -> PillarScore
    evidence: list = field(default_factory=list)    # [Evidence]
    analyst: dict = field(default_factory=dict)     # Gemini for/against/final + citations
    recommended_action: str = ""
    best_practice_findings: list = field(default_factory=list)


@dataclass
class RunResult:
    run_id: str
    timestamp: str
    org_id: str
    policy_version: str
    observable_window_days: int
    population: int                    # projects triaged
    deep_assessed: int                 # projects that reached deep assessment
    projects: list = field(default_factory=list)   # [ProjectAssessment]
    notes: list = field(default_factory=list)      # degraded-signal notices etc.
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)
