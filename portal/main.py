"""Zombie Hunter demo portal — FastAPI behind Cloud Run built-in IAP.

Demo-only surface: prod is programmatic (results.json + webhook). The portal
SA operates the TOOL only (read results, execute jobs, edit the schedule) —
it holds zero rights against the assessed tenancy."""
from __future__ import annotations

import json
import os

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from google.api_core import exceptions as gexc
from google.cloud import firestore, run_v2, scheduler_v1, storage

PROJECT = os.environ["GOOGLE_CLOUD_PROJECT"]
REGION = os.environ.get("REGION", "us-central1")
BUCKET = os.environ["RESULTS_BUCKET"]
PIPELINE_JOB = os.environ.get("PIPELINE_JOB", "zombie-pipeline")
SEEDER_JOB = os.environ.get("SEEDER_JOB", "zombie-seeder")
SCHEDULE_JOB = os.environ.get("SCHEDULE_JOB", "zombie-daily")

SCHEDULE_PRESETS = {"daily": "0 6 * * *", "weekly": "0 6 * * 1",
                    "monthly": "0 6 1 * *", "paused": None}

app = FastAPI(title="Zombie Hunter")
db = firestore.Client()
gcs = storage.Client()


def _blob_json(path: str):
    blob = gcs.bucket(BUCKET).blob(path)
    if not blob.exists():
        raise HTTPException(404, f"no results at {path}")
    return json.loads(blob.download_as_text())


@app.get("/api/whoami")
def whoami(request: Request):
    email = request.headers.get("X-Goog-Authenticated-User-Email", "")
    return {"email": email.removeprefix("accounts.google.com:")}


@app.get("/api/runs/latest")
def latest():
    return _blob_json("runs/latest/results.json")


@app.get("/api/runs/{date}")
def by_date(date: str):
    return _blob_json(f"runs/{date}/results.json")


@app.get("/api/report/latest", response_class=PlainTextResponse)
def report():
    blob = gcs.bucket(BUCKET).blob("runs/latest/report.md")
    if not blob.exists():
        raise HTTPException(404, "no report yet — run an assessment")
    return blob.download_as_text()


@app.get("/api/runs")
def runs():
    docs = (db.collection("runs")
            .order_by("timestamp", direction=firestore.Query.DESCENDING)
            .limit(30).stream())
    return [d.to_dict() for d in docs]


@app.get("/api/projects/{project_id}/history")
def history(project_id: str):
    docs = (db.collection("projects").document(project_id)
            .collection("history").order_by("date").stream())
    return [d.to_dict() for d in docs]


@app.get("/api/seeds")
def seeds():
    return db.collection("seeds").document("current").get().to_dict() or {"projects": []}


def _job_running(client: run_v2.ExecutionsClient, job: str) -> bool:
    parent = f"projects/{PROJECT}/locations/{REGION}/jobs/{job}"
    for ex in client.list_executions(parent=parent):
        if not ex.completion_time:
            return True
    return False


def _run_job(job: str, env: dict | None = None) -> dict:
    jobs = run_v2.JobsClient()
    if _job_running(run_v2.ExecutionsClient(), job):
        raise HTTPException(409, f"{job} already has a running execution")
    name = f"projects/{PROJECT}/locations/{REGION}/jobs/{job}"
    overrides = None
    if env:
        overrides = run_v2.RunJobRequest.Overrides(
            container_overrides=[run_v2.RunJobRequest.Overrides.ContainerOverride(
                env=[run_v2.EnvVar(name=k, value=v) for k, v in env.items()])])
    try:
        op = jobs.run_job(request=run_v2.RunJobRequest(name=name, overrides=overrides))
    except gexc.GoogleAPICallError as e:
        # Surface a JSON error the UI can show, not a bare 500 traceback.
        raise HTTPException(502, f"couldn't start {job}: {e.message}") from e
    return {"execution": op.metadata.name}


@app.post("/api/run-now")
def run_now():
    return _run_job(PIPELINE_JOB)


@app.post("/api/seed")
def seed():
    # Mirror the seeder's own guard so the click fails loud, not in job logs.
    if (db.collection("seeds").document("current").get().to_dict() or {}).get("seeded"):
        raise HTTPException(409, "seeds already populated — teardown first")
    return _run_job(SEEDER_JOB, {"MODE": "seed"})


@app.post("/api/teardown")
def teardown():
    return _run_job(SEEDER_JOB, {"MODE": "teardown"})


@app.get("/api/schedule")
def get_schedule():
    client = scheduler_v1.CloudSchedulerClient()
    name = f"projects/{PROJECT}/locations/{REGION}/jobs/{SCHEDULE_JOB}"
    try:
        job = client.get_job(name=name)
        preset = next((k for k, v in SCHEDULE_PRESETS.items()
                       if v == job.schedule), "custom")
        paused = job.state == scheduler_v1.Job.State.PAUSED
        return {"schedule": job.schedule, "preset": "paused" if paused else preset}
    except gexc.NotFound:
        return {"schedule": None, "preset": "none"}


@app.post("/api/schedule/{preset}")
def set_schedule(preset: str):
    if preset not in SCHEDULE_PRESETS:
        raise HTTPException(400, f"preset must be one of {list(SCHEDULE_PRESETS)}")
    client = scheduler_v1.CloudSchedulerClient()
    name = f"projects/{PROJECT}/locations/{REGION}/jobs/{SCHEDULE_JOB}"
    if preset == "paused":
        client.pause_job(name=name)
    else:
        job = client.get_job(name=name)
        job.schedule = SCHEDULE_PRESETS[preset]
        client.update_job(job=job)
        client.resume_job(name=name)
    return {"preset": preset}


ACK_TYPES = {"known-keep", "no-dr-seasonal-use"}


@app.post("/api/ack/{project_id}")
async def ack(project_id: str, request: Request):
    """Typed, expiring owner attestation. `known-keep` = seen the verdict,
    keep the project; `no-dr-seasonal-use` = owner asserts no DR/seasonal
    role (covers the window automated seasonality analysis can't yet)."""
    import datetime as dt
    body = await request.json() if await request.body() else {}
    ack_type = body.get("type", "known-keep")
    if ack_type not in ACK_TYPES:
        raise HTTPException(400, f"type must be one of {sorted(ACK_TYPES)}")
    days = int(body.get("expires_days", 90))
    email = request.headers.get("X-Goog-Authenticated-User-Email", "?")
    now = dt.datetime.now(dt.timezone.utc)
    db.collection("acks").document(project_id).set({
        "project_id": project_id, "by": email.removeprefix("accounts.google.com:"),
        "type": ack_type, "note": body.get("note", ""),
        "created": now.isoformat(),
        "expires": (now + dt.timedelta(days=days)).isoformat(),
        "runs_remaining": 3})
    return {"acked": project_id, "type": ack_type}


@app.get("/api/acks")
def acks():
    return {d.id: d.to_dict() for d in db.collection("acks").stream()}


app.mount("/", StaticFiles(directory="static", html=True), name="static")
