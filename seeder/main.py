"""Zombie Hunter demo seeder — Cloud Run Job (image: google-cloud-cli).

The 5 demo projects are a TF-managed POOL (the demo-org billing account only
lets humans link billing, so runtime project creation is impossible — see
terraform/main.tf). This job populates or strips the
resources INSIDE the pool, which is what actually shapes the verdicts:

  sdx-zh-husk-alpha  zombie-high    10GB unattached disk (pure holding cost)
  sdx-zh-husk-beta   zombie-high    GCS bucket with one stale object
  sdx-zh-heartbeat   zombie-medium  Scheduler→Pub/Sub pulse, zero humans
  sdx-zh-veto-sa     investigate    V1: its SA granted a role in husk-alpha
  sdx-zh-veto-cmek   investigate    V2: CMEK key granted to an external SA

MODE=seed populates, MODE=teardown strips (KMS keyrings are immortal by
design — teardown destroys key versions and removes the external grant).
All pool projects carry demo-zombie=true (tenure-guard bypass + seed badge).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

from google.cloud import firestore

TOOL_PROJECT = os.environ["GOOGLE_CLOUD_PROJECT"]
HUSK_A = "sdx-zh-husk-alpha"
HUSK_B = "sdx-zh-husk-beta"
HEART = "sdx-zh-heartbeat"
VETO_SA = "sdx-zh-veto-sa"
VETO_KMS = "sdx-zh-veto-cmek"
SA_EMAIL = f"tangled-sa@{VETO_SA}.iam.gserviceaccount.com"


def gcloud(*args: str, project: str = "", check: bool = True) -> bool:
    cmd = ["gcloud", *args, "--quiet"]
    if project:
        cmd += ["--project", project]
    print("+", " ".join(cmd), flush=True)
    r = subprocess.run(cmd, text=True)
    if check and r.returncode:
        raise SystemExit(f"FAILED ({r.returncode}): {' '.join(cmd)}")
    return r.returncode == 0


def seed() -> dict:
    # 1. husk-alpha — unattached disk, pure holding cost
    gcloud("compute", "disks", "create", "forgotten-disk", "--size=10GB",
           "--type=pd-standard", "--zone=us-central1-a", project=HUSK_A,
           check=False)  # creates are idempotent-tolerant: exists == seeded

    # 2. husk-beta — bucket with one stale object
    gcloud("storage", "buckets", "create", f"gs://{HUSK_B}-attic",
           "--location=us-central1", project=HUSK_B, check=False)
    subprocess.run(
        ["bash", "-c",
         f"echo 'output of an experiment nobody remembers' | "
         f"gcloud storage cp - gs://{HUSK_B}-attic/results-final-v2-FINAL.txt "
         f"--project {HUSK_B} --quiet"], check=True)

    # 3. heartbeat — machine pulse with zero human involvement
    gcloud("pubsub", "topics", "create", "pulse", project=HEART, check=False)
    gcloud("scheduler", "jobs", "create", "pubsub", "pulse-beat",
           "--schedule=*/10 * * * *",
           # bare topic names resolve against the ADC project, not --project
           f"--topic=projects/{HEART}/topics/pulse",
           "--message-body=lub-dub", "--location=us-central1", project=HEART,
           check=False)

    # 4. veto-sa — V1 trip: SA here holds a grant in husk-alpha
    gcloud("iam", "service-accounts", "create", "tangled-sa",
           "--display-name=Cross-project demo SA", project=VETO_SA, check=False)
    gcloud("projects", "add-iam-policy-binding", HUSK_A,
           f"--member=serviceAccount:{SA_EMAIL}",
           "--role=roles/storage.objectViewer")

    # 5. veto-cmek — V2 trip: CMEK key granted to an SA from another project
    gcloud("kms", "keyrings", "create", "demo-ring",
           "--location=us-central1", project=VETO_KMS, check=False)  # immortal
    gcloud("kms", "keys", "create", "shared-key", "--keyring=demo-ring",
           "--location=us-central1", "--purpose=encryption",
           project=VETO_KMS, check=False)
    gcloud("kms", "keys", "add-iam-policy-binding", "shared-key",
           "--keyring=demo-ring", "--location=us-central1",
           f"--member=serviceAccount:{SA_EMAIL}",
           "--role=roles/cloudkms.cryptoKeyEncrypterDecrypter",
           project=VETO_KMS)

    return {"seeded": True, "projects": [
        {"id": HUSK_A, "expected": "zombie-high", "seed": "husk + 10GB disk"},
        {"id": HUSK_B, "expected": "zombie-high", "seed": "GCS bucket husk"},
        {"id": HEART, "expected": "zombie-medium", "seed": "Scheduler→Pub/Sub heartbeat"},
        {"id": VETO_SA, "expected": "investigate", "seed": "V1 cross-project SA grant"},
        {"id": VETO_KMS, "expected": "investigate", "seed": "V2 CMEK external grant"},
    ]}


def teardown() -> dict:
    ops = [
        lambda: gcloud("compute", "disks", "delete", "forgotten-disk",
                       "--zone=us-central1-a", project=HUSK_A, check=False),
        lambda: gcloud("storage", "rm", "-r", f"gs://{HUSK_B}-attic",
                       project=HUSK_B, check=False),
        lambda: gcloud("scheduler", "jobs", "delete", "pulse-beat",
                       "--location=us-central1", project=HEART, check=False),
        lambda: gcloud("pubsub", "topics", "delete", "pulse", project=HEART,
                       check=False),
        lambda: gcloud("projects", "remove-iam-policy-binding", HUSK_A,
                       f"--member=serviceAccount:{SA_EMAIL}",
                       "--role=roles/storage.objectViewer", check=False),
        lambda: gcloud("kms", "keys", "remove-iam-policy-binding", "shared-key",
                       "--keyring=demo-ring", "--location=us-central1",
                       f"--member=serviceAccount:{SA_EMAIL}",
                       "--role=roles/cloudkms.cryptoKeyEncrypterDecrypter",
                       project=VETO_KMS, check=False),
        lambda: gcloud("iam", "service-accounts", "delete", SA_EMAIL,
                       project=VETO_SA, check=False),
    ]
    for op in ops:
        op()
    return {"seeded": False, "projects": []}


def main():
    mode = os.environ.get("MODE", "seed")
    db = firestore.Client(project=TOOL_PROJECT)
    ref = db.collection("seeds").document("current")
    if mode == "seed":
        if (ref.get().to_dict() or {}).get("seeded"):
            print("seeds already populated — teardown first", flush=True)
            sys.exit(1)
        manifest = seed()
    else:
        manifest = teardown()
    ref.set(manifest)
    print(json.dumps(manifest, indent=1), flush=True)


if __name__ == "__main__":
    main()
