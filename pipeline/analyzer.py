"""Gemini as ANALYST, not sensor. The model receives the deterministic
evidence for one candidate, must argue FOR the zombie verdict, then AGAINST
itself, then land a position citing evidence IDs. It cannot override policy:
its output is attached to the assessment (and can downgrade zombie→investigate
via the challenge flag) but never upgrades toward deletion."""
from __future__ import annotations

import dataclasses
import json
import logging
import random
import time

from google import genai
from google.genai import errors, types

log = logging.getLogger("zombie.analyzer")

_RETRIES = 4  # Vertex 429s are transient per-minute quota; total worst-case wait ~30s

_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "case_for_zombie": {"type": "STRING"},
        "case_against_zombie": {"type": "STRING"},
        "final_position": {"type": "STRING",
                           "enum": ["agree", "challenge", "insufficient_evidence"]},
        "challenge_reason": {"type": "STRING"},
        "citations": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["case_for_zombie", "case_against_zombie",
                 "final_position", "citations"],
}

_PROMPT = """You are the adversarial analyst inside a GCP zombie-project \
assessment pipeline. Deterministic collectors produced the evidence below for \
project `{project_id}`; policy scored it {score}/100 with proposed verdict \
`{verdict}`.

The policy engine has ALREADY interpreted each pillar (below) — do not \
re-argue points it settled. In particular, activity during a project's \
provisioning window is setup, not life, and the scorer already accounts for \
it; do not cite "recent human/admin action" as a contradiction when the \
pillar rationale shows it was provisioning-era. Likewise, `runtime`-pillar \
evidence (API traffic, CPU, network, data access) measures DELETE RISK, not \
life — machine heartbeat never makes a project non-zombie, so do not cite it \
to challenge a zombie verdict; it belongs in case_against_zombie as a safety \
concern only.

PILLAR CONCLUSIONS (policy):
{pillars}

Your job is NOT to detect anything — the sensors already ran. Your job:
1. case_for_zombie: the strongest honest argument this project is abandoned.
2. case_against_zombie: argue AGAINST yourself — what could make deleting \
this project harmful that the evidence hints at or fails to rule out?
3. final_position: `agree` (verdict stands), `challenge` (a SPECIFIC piece of \
evidence directly contradicts the verdict — name it), or \
`insufficient_evidence`. Only `challenge` when you can cite the contradicting \
evidence ID; a general "could be important" is not grounds to challenge.
Every claim must cite evidence IDs from the list. Do not invent signals.

EVIDENCE:
{evidence}
"""


def analyse(client: genai.Client, model: str, temperature: float,
            project_id: str, verdict: str, score: int,
            evidence: list, labels: dict | None = None,
            pillars: dict | None = None) -> dict:
    ev_json = json.dumps([dataclasses.asdict(e) for e in evidence], indent=1)
    pillar_txt = "\n".join(
        f"- {k}: {v.score}/100 [{v.confidence}] — {v.rationale}"
        for k, v in (pillars or {}).items()) or "(none)"
    prompt = _PROMPT.format(project_id=project_id, score=score,
                            verdict=verdict, evidence=ev_json, pillars=pillar_txt)
    if (labels or {}).get("demo-zombie") == "true":
        prompt += ("\nNOTE: this project carries the `demo-zombie=true` label "
                   "— it is a SEEDED demonstration zombie. Do not treat its "
                   "young age or provisioning-era activity as evidence of "
                   "life; assess the constructed evidence at face value.")
    try:
        for attempt in range(_RETRIES + 1):
            try:
                resp = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=temperature,
                        response_mime_type="application/json",
                        response_schema=_RESPONSE_SCHEMA,
                    ),
                )
                break
            except errors.APIError as e:
                if e.code not in (429, 500, 503) or attempt == _RETRIES:
                    raise
                delay = 2 ** attempt + random.uniform(0, 1)
                log.warning("analyst %s got %s for %s — retry %d/%d in %.1fs",
                            model, e.code, project_id, attempt + 1, _RETRIES, delay)
                time.sleep(delay)
        out = json.loads(resp.text)
        out["model"] = model
        return out
    except Exception as e:  # noqa: BLE001 — analyst failure degrades, never blocks
        log.warning("analyst failed for %s: %s", project_id, e)
        return {"final_position": "insufficient_evidence",
                "error": f"{type(e).__name__}: {e}", "citations": [],
                "case_for_zombie": "", "case_against_zombie": "",
                "model": model}


def apply_analyst(assessment, analyst: dict):
    """A `challenge` can only make the verdict SAFER (→ investigate)."""
    assessment.analyst = analyst
    if (analyst.get("final_position") == "challenge"
            and assessment.verdict in ("zombie-high", "zombie-medium")):
        assessment.verdict = "investigate"
        assessment.recommended_action = (
            "manual review — analyst challenged the zombie verdict: "
            + analyst.get("challenge_reason", "")[:200])
    return assessment
