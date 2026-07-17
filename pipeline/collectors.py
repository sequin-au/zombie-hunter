"""Deterministic collectors. Gemini is the ANALYST, not the sensor — everything
here is boring, reproducible API calls that emit Evidence objects.

Two-stage design (also the production scaling story):
  triage()  — cheap, runs against EVERY project: census + Unattended Project
              Recommender + billing sketch. Produces candidate list.
  deep()    — expensive, candidates only: audit-log mining, SA last-auth,
              full veto sweep V1-V8, runtime-activity sampling, inventory.

Two separate questions, two separate signal sets:
  deadness (should we consider deleting?) — billing/last-human/connectivity
              pillars. Machine heartbeat is deliberately NOT life here.
  delete risk (can we delete safely?)     — vetoes + runtime-activity
              collectors (API traffic, CPU, network, data-access events).
              A machine heartbeat doesn't make a project alive, but it DOES
              make deleting it risky — those signals feed delete_risk only.

Every collector degrades gracefully: a missing API, empty dataset, or
permission gap yields confidence="unknown" evidence + a run note — never a
crash, and never a fabricated signal.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import re
from collections import defaultdict

from google.api_core import exceptions as gexc
from google.cloud import asset_v1, bigquery, logging_v2, recommender_v1
from google.cloud import resourcemanager_v3 as crm

from .models import Evidence, Veto

log = logging.getLogger("zombie.collectors")

HUMAN_RE = re.compile(r"@(?!.*gserviceaccount\.com$)")  # principal looks human


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ---------------------------------------------------------------------------
# Stage 1 — triage (all projects, cheap)
# ---------------------------------------------------------------------------

def census(org_id: str) -> list[dict]:
    """Every ACTIVE project in the org, with labels/parent/create time."""
    client = crm.ProjectsClient()
    out = []
    for p in client.search_projects(query="state:ACTIVE"):
        out.append({
            "project_id": p.project_id,
            "project_number": p.name.split("/")[-1],
            "display_name": p.display_name,
            "parent": p.parent,
            "labels": dict(p.labels),
            "create_time": p.create_time.isoformat() if p.create_time else "",
        })
    log.info("census: %d active projects", len(out))
    return out


def unattended_recommendations(project_id: str) -> Evidence | None:
    """Google's own Unattended Project Recommender — free triage signal.
    Needs ~30d of the recommender observing; absent != alive."""
    try:
        client = recommender_v1.RecommenderClient()
        parent = (f"projects/{project_id}/locations/global/recommenders/"
                  "google.resourcemanager.projectUtilization.Recommender")
        recs = list(client.list_recommendations(parent=parent))
        if not recs:
            return None
        r = recs[0]
        return Evidence(
            id=f"E-recommender-{project_id}",
            pillar="last_accessed",
            summary=f"Unattended Project Recommender: {r.description}",
            detail={"recommendation": r.name, "priority": str(r.priority)},
            confidence="high",
        )
    except (gexc.PermissionDenied, gexc.NotFound, gexc.FailedPrecondition) as e:
        log.debug("recommender unavailable for %s: %s", project_id, e)
        return None


def billing_sketch(dataset: str, window_days: int, policy: dict,
                   notes: list) -> dict[str, dict]:
    """Per-project spend composition + variance from the billing export.

    Returns {project_id: {total, holding_pct, variance_coeff, months}} or {}
    when the export isn't populated yet (degrade: lean on other pillars)."""
    try:
        bq = bigquery.Client()
        tables = [t.table_id for t in bq.list_tables(dataset)
                  if t.table_id.startswith("gcp_billing_export_v1")]
        if not tables:
            notes.append("billing export dataset empty — billing pillar "
                         "degraded to 'unknown' (enable export in Billing console)")
            return {}
        q = f"""
        SELECT project.id AS pid, service.description AS service,
               sku.description AS sku,
               FORMAT_DATE('%Y-%m', DATE(usage_start_time)) AS month,
               SUM(cost) AS cost
        FROM `{dataset}.{tables[0]}`
        WHERE usage_start_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(),
                                                INTERVAL {window_days} DAY)
        GROUP BY pid, service, sku, month"""
        rows = list(bq.query(q).result())
    except Exception as e:  # noqa: BLE001 — any BQ failure degrades, never kills
        notes.append(f"billing query failed ({type(e).__name__}) — "
                     "billing pillar degraded to 'unknown'")
        return {}

    hold_kw = [k.lower() for k in policy.get("holding_cost_sku_keywords", [])]
    hold_svc = [s.lower() for s in policy.get("holding_cost_services", [])]
    agg: dict[str, dict] = defaultdict(
        lambda: {"total": 0.0, "holding": 0.0, "monthly": defaultdict(float)})
    for r in rows:
        if not r.pid:
            continue
        a = agg[r.pid]
        a["total"] += r.cost
        a["monthly"][r.month] += r.cost
        # Holding cost = SKU keyword match (disks/IPs/snapshots inside mixed
        # services) OR a service that is holding-cost in its entirety (KMS
        # key storage, DNS zone rental, ...).
        if (any(k in (r.sku or "").lower() for k in hold_kw)
                or (r.service or "").lower() in hold_svc):
            a["holding"] += r.cost

    out = {}
    for pid, a in agg.items():
        months = sorted(a["monthly"].values())
        mean = sum(months) / len(months) if months else 0
        var = (sum((m - mean) ** 2 for m in months) / len(months)) ** 0.5 if months else 0
        out[pid] = {
            "total": round(a["total"], 2),
            "holding_pct": round(100 * a["holding"] / a["total"], 1) if a["total"] else 0,
            "variance_coeff": round(var / mean, 3) if mean else 0,
            "months": len(months),
        }
    return out


# ---------------------------------------------------------------------------
# Stage 2 — deep assessment (candidates only)
# ---------------------------------------------------------------------------

def last_human_action(org_id: str, project_id: str, window_days: int) -> Evidence:
    """Most recent Admin Activity audit entry by a HUMAN principal (Admin
    Activity logs: 400-day retention, no data-access config needed).
    Machine heartbeat (SA-driven writes) is deliberately excluded."""
    try:
        client = logging_v2.Client()
        flt = (f'logName:"cloudaudit.googleapis.com%2Factivity" '
               f'AND resource.labels.project_id="{project_id}" '
               f'AND protoPayload.authenticationInfo.principalEmail:"@" '
               f'AND NOT protoPayload.authenticationInfo.principalEmail:"gserviceaccount.com"')
        # Project-scoped audit entries live in the PROJECT's log bucket;
        # the org bucket only holds org-level operations.
        entries = client.list_entries(
            resource_names=[f"projects/{project_id}"], filter_=flt,
            order_by=logging_v2.DESCENDING, page_size=5, max_results=5)
        latest = next(iter(entries), None)
        if latest is None:
            return Evidence(
                id=f"E-lastaccess-{project_id}", pillar="last_accessed",
                summary="No human admin action found in retained audit logs (400d)",
                confidence="high")
        pl = latest.payload or {}
        actor = pl.get("authenticationInfo", {}).get("principalEmail", "?")
        method = pl.get("methodName", "?")
        age = (_now() - latest.timestamp).days
        return Evidence(
            id=f"E-lastaccess-{project_id}", pillar="last_accessed",
            summary=f"Last human admin action {age}d ago: {actor} → {method}",
            detail={"actor": actor, "method": method,
                    "timestamp": latest.timestamp.isoformat(), "age_days": age},
            confidence="high")
    except Exception as e:  # noqa: BLE001
        log.warning("last_human_action failed for %s: %s", project_id, e)
        return Evidence(id=f"E-lastaccess-{project_id}", pillar="last_accessed",
                        summary=f"Audit-log query failed: {type(e).__name__}",
                        confidence="unknown")


def veto_sweep(org_id: str, project_id: str, project_number: str,
               labels: dict, vetodefs: dict) -> tuple[list[Veto], list[Evidence]]:
    """Hard-veto checks V1-V7. Anything triggered forces verdict=investigate.
    Checks that cannot complete return 'unknown' evidence (surfaced in the
    report) rather than silently passing — absence of signal is not safety."""
    vetoes: list[Veto] = []
    ev: list[Evidence] = []
    asset = asset_v1.AssetServiceClient()
    scope = f"organizations/{org_id}"
    sa_suffix = f"@{project_id}.iam.gserviceaccount.com"

    def hit(code: str, summary: str, detail: dict):
        eid = f"E-veto-{code}-{project_id}"
        ev.append(Evidence(id=eid, pillar="veto", summary=summary,
                           detail=detail, confidence="high"))
        vetoes.append(Veto(code=code, description=vetodefs.get(code, code),
                           evidence_id=eid))

    # V1 — this project's SAs granted roles in OTHER projects. Corroborated
    # with Policy Analyzer SA last-authentication: a grant that is actively
    # exercised is a CERTAIN external dependency; a dormant grant is only a
    # possible one (delete_risk high vs medium — the veto fires either way).
    try:
        results = asset.search_all_iam_policies(request={
            "scope": scope, "query": f'policy:"{sa_suffix}"', "page_size": 100})
        external = [r.resource for r in results
                    if f"/projects/{project_id}" not in r.resource
                    and f"/projects/{project_number}" not in r.resource]
        if external:
            last_auth = _sa_last_auth(project_id)
            recent = [a for a in last_auth
                      if a.get("age_days") is not None and a["age_days"] <= 90]
            active = bool(recent)
            hit("V1", f"Service accounts from {project_id} hold grants in "
                      f"{len(external)} external resource(s) — "
                      + (f"ACTIVELY authenticating ({len(recent)} SA(s) used "
                         f"in last 90d)" if active
                         else "no SA authentication observed in last 90d "
                              "(dormant grant)"),
                {"resources": external[:10], "sa_last_auth": last_auth[:10],
                 "active_use": active})
    except Exception as e:  # noqa: BLE001
        ev.append(Evidence(id=f"E-veto-V1-{project_id}", pillar="veto",
                           summary=f"V1 check incomplete: {type(e).__name__}",
                           confidence="unknown"))

    # V2 — KMS keys with grants to principals outside this project.
    # Scope directly to the project: org-scope + a `project:` query filter
    # misses freshly-ingested keys, whereas project-scope resolves them.
    try:
        keys = list(asset.search_all_resources(request={
            "scope": f"projects/{project_id}",
            "asset_types": ["cloudkms.googleapis.com/CryptoKey"], "page_size": 50}))
        if keys:
            pols = asset.search_all_iam_policies(request={
                "scope": f"projects/{project_id}",
                "asset_types": ["cloudkms.googleapis.com/CryptoKey"], "page_size": 100})
            outsiders = []
            for p in pols:
                for b in p.policy.bindings:
                    outsiders += [m for m in b.members
                                  if "gserviceaccount.com" in m and sa_suffix not in m]
            if outsiders:
                hit("V2", f"{len(keys)} CMEK key(s) with external principals — "
                          "may encrypt data OUTSIDE this project",
                    {"external_members": sorted(set(outsiders))[:10]})
            else:
                ev.append(Evidence(id=f"E-kms-{project_id}", pillar="connectivity",
                                   summary=f"{len(keys)} KMS key(s), no external grants",
                                   confidence="medium"))
    except Exception as e:  # noqa: BLE001
        ev.append(Evidence(id=f"E-veto-V2-{project_id}", pillar="veto",
                           summary=f"V2 check incomplete: {type(e).__name__}",
                           confidence="unknown"))

    # V3 — shared VPC / peering / PSC / VPN tunnels / custom routes
    try:
        nets = asset.search_all_resources(request={
            "scope": f"projects/{project_id}",
            "asset_types": ["compute.googleapis.com/Network",
                            "compute.googleapis.com/ServiceAttachment",
                            "compute.googleapis.com/VpnTunnel",
                            "compute.googleapis.com/Route"],
            "read_mask": "name,assetType,additionalAttributes", "page_size": 200})
        flags = []
        for n in nets:
            short = n.name.split("/")[-1]
            if n.asset_type.endswith("ServiceAttachment"):
                flags.append(f"PSC attachment: {short}")
            elif n.asset_type.endswith("VpnTunnel"):
                flags.append(f"VPN tunnel: {short}")
            elif n.asset_type.endswith("Route"):
                # Auto-generated routes are named default-route-*; anything
                # else is a custom route someone built traffic paths around.
                if not short.startswith("default-route-"):
                    flags.append(f"custom route: {short}")
            else:
                attrs = dict(n.additional_attributes) if n.additional_attributes else {}
                if attrs.get("peerings"):
                    flags.append(f"VPC peering on {short}")
        # Shared VPC host detection: service projects consume host subnets via
        # subnet-level compute.networkUser grants — any subnet IAM binding to
        # a principal outside this project marks it a host.
        subnet_pols = asset.search_all_iam_policies(request={
            "scope": f"projects/{project_id}",
            "asset_types": ["compute.googleapis.com/Subnetwork"],
            "page_size": 100})
        shared_to = set()
        for sp in subnet_pols:
            for b in sp.policy.bindings:
                shared_to.update(
                    m for m in b.members
                    if project_id not in m and project_number not in m)
        if shared_to:
            flags.append(f"Shared VPC host: subnets granted to "
                         f"{len(shared_to)} external principal(s)")
        if flags:
            hit("V3", "Network sharing surface: " + "; ".join(flags[:3]),
                {"flags": flags,
                 "shared_vpc_members": sorted(shared_to)[:10]})
    except Exception as e:  # noqa: BLE001
        ev.append(Evidence(id=f"E-veto-V3-{project_id}", pillar="veto",
                           summary=f"V3 check incomplete: {type(e).__name__}",
                           confidence="unknown"))

    # V4 — OAuth/IAP brands (best-effort: API surface is limited)
    try:
        brands = list(asset.search_all_resources(request={
            "scope": f"projects/{project_id}",
            "asset_types": ["iap.googleapis.com/Brand"], "page_size": 10}))
        if brands:
            hit("V4", f"{len(brands)} OAuth/IAP brand(s) — clients may serve "
                      "logins for other systems", {"brands": [b.name for b in brands]})
    except Exception:  # noqa: BLE001 — asset type not searchable everywhere
        pass

    # V5 — destination for sinks / DNS / TF state
    try:
        dns = list(asset.search_all_resources(request={
            "scope": f"projects/{project_id}",
            "asset_types": ["dns.googleapis.com/ManagedZone"], "page_size": 10}))
        if dns:
            hit("V5", f"{len(dns)} DNS managed zone(s) — other systems may resolve "
                      "through this project", {"zones": [d.name for d in dns]})
        buckets = asset.search_all_resources(request={
            "scope": f"projects/{project_id}",
            "asset_types": ["storage.googleapis.com/Bucket"], "page_size": 50})
        tf = [b.name for b in buckets
              if any(k in b.name.lower() for k in ("tfstate", "terraform", "state"))]
        if tf:
            hit("V5", "Bucket name suggests Terraform state destination",
                {"buckets": tf})
    except Exception as e:  # noqa: BLE001
        ev.append(Evidence(id=f"E-veto-V5-{project_id}", pillar="veto",
                           summary=f"V5 check incomplete: {type(e).__name__}",
                           confidence="unknown"))

    # V6 — Artifact Registry repos (external-pull detection needs data-access
    # logs; presence alone is evidence-grade, not veto-grade, unless pulled)
    try:
        repos = list(asset.search_all_resources(request={
            "scope": f"projects/{project_id}",
            "asset_types": ["artifactregistry.googleapis.com/Repository"],
            "page_size": 10}))
        if repos:
            ev.append(Evidence(
                id=f"E-ar-{project_id}", pillar="connectivity",
                summary=f"{len(repos)} Artifact Registry repo(s) present — "
                        "external pulls not verifiable without data-access logs",
                detail={"repos": [r.name for r in repos]}, confidence="low"))
    except Exception:  # noqa: BLE001
        pass

    # V7 — liens + retain label. Liens are NOT in the resourcemanager_v3 gRPC
    # client (no LiensClient), so call the REST v3 endpoint directly.
    try:
        liens = _list_liens(project_id)
        if liens:
            hit("V7", f"{len(liens)} resource lien(s) present",
                {"liens": liens})
    except Exception as e:  # noqa: BLE001
        ev.append(Evidence(id=f"E-veto-V7-{project_id}", pillar="veto",
                           summary=f"V7 lien check incomplete: {type(e).__name__}",
                           confidence="unknown"))
    if labels.get("retain") == "true":
        hit("V7", "Project labelled retain=true", {"labels": labels})

    # V8 — API keys. A key bound to a service account authenticates external
    # callers AS that SA (certain breakage on delete); any other key may be
    # embedded in clients we can't see (evidence-grade, not veto-grade).
    try:
        keys = _list_api_keys(project_id)
        sa_bound = [k for k in keys if k.get("serviceAccountEmail")]
        if sa_bound:
            hit("V8", f"{len(sa_bound)} API key(s) bound to service accounts "
                      "— external callers authenticate through them",
                {"keys": [{"name": k["name"].split("/")[-1],
                           "sa": k["serviceAccountEmail"],
                           "display_name": k.get("displayName", "")}
                          for k in sa_bound[:10]]})
        elif keys:
            ev.append(Evidence(
                id=f"E-apikeys-{project_id}", pillar="connectivity",
                summary=f"{len(keys)} API key(s) present — may be embedded in "
                        "external clients (holders unverifiable)",
                detail={"keys": [k["name"].split("/")[-1] for k in keys[:10]]},
                confidence="low"))
    except Exception as e:  # noqa: BLE001
        ev.append(Evidence(id=f"E-veto-V8-{project_id}", pillar="veto",
                           summary=f"V8 API-key check incomplete: {type(e).__name__}",
                           confidence="unknown"))

    return vetoes, ev


def _rest_get(url: str, timeout: int = 15) -> dict:
    """Authenticated GET against a Google REST API — for surfaces the
    generated gRPC clients don't cover (liens, API keys, Policy Analyzer)."""
    import json
    import urllib.request

    import google.auth
    import google.auth.transport.requests

    creds, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(google.auth.transport.requests.Request())
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {creds.token}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _list_liens(project_id: str) -> list[str]:
    """List liens via the CRM v3 REST API (no LiensClient in the gRPC lib)."""
    data = _rest_get("https://cloudresourcemanager.googleapis.com/v3/liens"
                     f"?parent=projects/{project_id}&pageSize=100")
    return [l["name"] for l in data.get("liens", [])]


def _list_api_keys(project_id: str) -> list[dict]:
    """List API keys (metadata only — never keyString) via API Keys v2."""
    data = _rest_get(f"https://apikeys.googleapis.com/v2/projects/{project_id}"
                     "/locations/global/keys?pageSize=300")
    return data.get("keys", [])


def _sa_last_auth(project_id: str) -> list[dict]:
    """Policy Analyzer serviceAccountLastAuthentication for every SA in the
    project. Returns [{sa, last_auth, age_days}] sorted most-recent first."""
    data = _rest_get(
        f"https://policyanalyzer.googleapis.com/v1/projects/{project_id}"
        "/locations/global/activityTypes/serviceAccountLastAuthentication"
        "/activities:query?pageSize=100", timeout=30)
    out = []
    for act in data.get("activities", []):
        a = act.get("activity", {})
        ts = a.get("lastAuthenticatedTime", "")
        age = None
        if ts:
            age = (_now() - dt.datetime.fromisoformat(
                ts.replace("Z", "+00:00"))).days
        sa = (a.get("serviceAccount", {}).get("fullResourceName", "")
              or act.get("fullResourceName", ""))
        out.append({"sa": sa.split("/")[-1], "last_auth": ts, "age_days": age})
    out.sort(key=lambda x: x["age_days"] if x["age_days"] is not None else 10**6)
    return out


def owner_of_record(project_id: str) -> str:
    """Best-effort owner: first human owner/editor in project IAM."""
    try:
        pol = crm.ProjectsClient().get_iam_policy(
            resource=f"projects/{project_id}")
        for b in pol.bindings:
            if b.role in ("roles/owner", "roles/editor"):
                humans = [m for m in b.members
                          if m.startswith("user:")]
                if humans:
                    return humans[0].removeprefix("user:")
    except Exception:  # noqa: BLE001
        pass
    return ""


# ---------------------------------------------------------------------------
# Runtime-activity signals — feed delete_risk ONLY, never the deadness score.
# A project serving API traffic with no human touch is still a zombie by our
# definition, but it is NOT safe to delete: something depends on it running.
# ---------------------------------------------------------------------------

def runtime_activity(project_id: str, window_days: int) -> tuple[dict, Evidence]:
    """Cloud Monitoring sample of the window: consumed-API request volume,
    instance CPU, instance network bytes. VPC Flow Logs are only parsed where
    enabled (rare in dev tenancies) — instance NIC counters are the fallback
    and what we use here; the limitation is documented, not hidden."""
    from google.cloud import monitoring_v3

    stats = {"api_requests": 0, "api_top_services": {}, "cpu_mean": None,
             "cpu_max": None, "net_bytes": 0, "checks_failed": []}
    client = monitoring_v3.MetricServiceClient()
    now = _now()
    interval = monitoring_v3.TimeInterval(
        end_time={"seconds": int(now.timestamp())},
        start_time={"seconds": int((now - dt.timedelta(days=window_days))
                                   .timestamp())})

    def series(metric: str, aligner, reducer, group_by=()):
        return client.list_time_series(request={
            "name": f"projects/{project_id}",
            "filter": f'metric.type="{metric}"',
            "interval": interval,
            "aggregation": monitoring_v3.Aggregation(
                alignment_period={"seconds": 86400},
                per_series_aligner=aligner,
                cross_series_reducer=reducer,
                group_by_fields=list(group_by)),
            "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL})

    A = monitoring_v3.Aggregation.Aligner
    R = monitoring_v3.Aggregation.Reducer
    try:
        for ts in series("serviceruntime.googleapis.com/api/request_count",
                         A.ALIGN_SUM, R.REDUCE_SUM,
                         group_by=["resource.labels.service"]):
            svc = ts.resource.labels.get("service", "?")
            n = sum(p.value.int64_value for p in ts.points)
            stats["api_requests"] += n
            stats["api_top_services"][svc] = n
        stats["api_top_services"] = dict(sorted(
            stats["api_top_services"].items(), key=lambda kv: -kv[1])[:5])
    except Exception as e:  # noqa: BLE001
        stats["checks_failed"].append(f"api_requests:{type(e).__name__}")
    try:
        vals = [p.value.double_value
                for ts in series("compute.googleapis.com/instance/cpu/utilization",
                                 A.ALIGN_MEAN, R.REDUCE_MEAN)
                for p in ts.points]
        if vals:
            stats["cpu_mean"] = round(sum(vals) / len(vals), 4)
            stats["cpu_max"] = round(max(vals), 4)
    except Exception as e:  # noqa: BLE001
        stats["checks_failed"].append(f"cpu:{type(e).__name__}")
    try:
        for metric in ("compute.googleapis.com/instance/network/received_bytes_count",
                       "compute.googleapis.com/instance/network/sent_bytes_count"):
            for ts in series(metric, A.ALIGN_SUM, R.REDUCE_SUM):
                stats["net_bytes"] += sum(p.value.int64_value for p in ts.points)
    except Exception as e:  # noqa: BLE001
        stats["checks_failed"].append(f"network:{type(e).__name__}")

    complete = not stats["checks_failed"]
    top = ", ".join(f"{s}={n}" for s, n in stats["api_top_services"].items())
    return stats, Evidence(
        id=f"E-runtime-{project_id}", pillar="runtime",
        summary=(f"Runtime activity ({window_days}d): "
                 f"{stats['api_requests']} API request(s)"
                 + (f" [{top}]" if top else "")
                 + (f", CPU mean {stats['cpu_mean']}" if stats["cpu_mean"]
                    is not None else ", no running instances")
                 + f", {stats['net_bytes']} instance network bytes"
                 + ("" if complete
                    else f" — {len(stats['checks_failed'])} check(s) failed")),
        detail=stats, confidence="high" if complete else "medium")


# The observer effect, learned live: the pipeline's own daily reads (CAI,
# Monitoring, Logging) and Google's platform scanners (SCC, web security
# scanner, console backends) generate data-access entries in EVERY project
# — a sensor that counts them declares the whole org "in use". Google
# service agents all match service-*@ or the system/appspot/cloudservices
# domains; the tool's own SAs are homed in the tool project.
_PLATFORM_NOISE = re.compile(
    r"^service-|@(system|appspot|cloudservices)\.gserviceaccount\.com$")


def data_access_activity(project_id: str, window_days: int) -> Evidence:
    """Data Access audit-log sample (human or workload principals — this is
    a delete-risk signal, not a life signal). The tool's own SAs and Google
    platform agents are excluded (see _PLATFORM_NOISE), and only
    DATA_READ/DATA_WRITE count — ADMIN_READ is control-plane traffic
    (GetProject, GetIamPolicy: Terraform refreshes, console browsing,
    scanners) that says nothing about anyone USING the project's data.
    Only services with Data Access logging enabled appear (BigQuery has it
    by default); absence is therefore weak evidence — confidence never
    exceeds medium."""
    tool_project = os.environ.get("GOOGLE_CLOUD_PROJECT", "")

    def _noise(email: str) -> bool:
        return bool(_PLATFORM_NOISE.search(email)) or bool(
            tool_project and email.endswith(
                f"@{tool_project}.iam.gserviceaccount.com"))

    try:
        client = logging_v2.Client(project=project_id)
        cutoff = (_now() - dt.timedelta(days=window_days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        flt = (f'logName:"cloudaudit.googleapis.com%2Fdata_access" '
               f'AND timestamp >= "{cutoff}" '
               f'AND protoPayload.authorizationInfo.permissionType='
               f'("DATA_READ" OR "DATA_WRITE")')
        entries = list(client.list_entries(
            resource_names=[f"projects/{project_id}"], filter_=flt,
            order_by=logging_v2.DESCENDING, page_size=500, max_results=500))
        real = [e for e in entries
                if not _noise((e.payload or {}).get("authenticationInfo", {})
                              .get("principalEmail", "?"))]
        excluded = len(entries) - len(real)
        if not real:
            return Evidence(
                id=f"E-dataaccess-{project_id}", pillar="runtime",
                summary=f"No DATA_READ/DATA_WRITE events in {window_days}d "
                        f"beyond tool/platform noise ({excluded} excluded; "
                        "where Data Access logging is enabled)",
                detail={"events_sampled": 0, "noise_excluded": excluded},
                confidence="medium")
        principals = {(e.payload or {}).get("authenticationInfo", {})
                      .get("principalEmail", "?") for e in real}
        age = (_now() - real[0].timestamp).days
        return Evidence(
            id=f"E-dataaccess-{project_id}", pillar="runtime",
            summary=f"{len(real)}{'+' if len(entries) == 500 else ''} "
                    f"data-access event(s) in {window_days}d, latest {age}d "
                    f"ago by {len(principals)} principal(s) "
                    f"({excluded} tool/platform events excluded)",
            detail={"events_sampled": len(real), "latest_age_days": age,
                    "principals": sorted(principals)[:10],
                    "noise_excluded": excluded},
            confidence="medium")
    except Exception as e:  # noqa: BLE001
        return Evidence(id=f"E-dataaccess-{project_id}", pillar="runtime",
                        summary=f"Data-access scan failed: {type(e).__name__}",
                        confidence="unknown")


def resource_inventory(project_id: str, cap: int = 2000) -> tuple[dict, list[dict]]:
    """Full CAI resource inventory for a candidate — the 'what exactly dies
    if we delete this' artifact. Summary goes in results.json; the full list
    is published as a separate GCS object per project."""
    asset = asset_v1.AssetServiceClient()
    items: list[dict] = []
    counts: dict[str, int] = defaultdict(int)
    truncated = False
    try:
        for r in asset.search_all_resources(request={
                "scope": f"projects/{project_id}",
                "read_mask": "name,assetType,location", "page_size": 500}):
            counts[r.asset_type] += 1
            if len(items) < cap:
                items.append({"name": r.name, "type": r.asset_type,
                              "location": r.location})
            else:
                truncated = True
        summary = {"total": sum(counts.values()),
                   "by_type": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
                   "truncated": truncated, "confidence": "high"}
    except Exception as e:  # noqa: BLE001
        summary = {"total": None, "by_type": {}, "truncated": False,
                   "confidence": "unknown",
                   "error": type(e).__name__}
    return summary, items


def billing_history_months(dataset: str) -> int:
    """Distinct months present in the billing export (13-month seasonality
    analysis needs >=6; the export only accrues data from when it was
    enabled, so young exports honestly report their own blindness)."""
    try:
        bq = bigquery.Client()
        tables = [t.table_id for t in bq.list_tables(dataset)
                  if t.table_id.startswith("gcp_billing_export_v1")]
        if not tables:
            return 0
        q = (f"SELECT COUNT(DISTINCT FORMAT_DATE('%Y-%m', "
             f"DATE(usage_start_time))) AS row_count "
             f"FROM `{dataset}.{tables[0]}` "
             f"WHERE usage_start_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), "
             f"INTERVAL 400 DAY)")
        return next(iter(bq.query(q).result())).row_count
    except Exception:  # noqa: BLE001
        return 0
