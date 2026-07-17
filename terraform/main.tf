# Zombie Hunter — GCP tenancy assessment demo ("GCP, the walking dead")
# Full design: ../docs/ARCHITECTURE.md
#
# SOURCE OF TRUTH for the tool's infrastructure. The project-factory module
# is vendored under ./modules/. State backend and deployment-specific values
# are supplied via untracked files:
#   terraform init -backend-config=backend.hcl   (see backend.hcl.example)
#   terraform.tfvars                              (see terraform.tfvars.example)
#
# Three concerns:
#   1. The TOOL project (sdx-demos-zombie-dev) — pipeline/portal/seeder jobs,
#      Firestore, results bucket, images. Enterprise/Dev folder like other demos.
#   2. The ZOMBIE DEMO folder (under Sandbox) — the only place the seeder SA
#      can create/delete the 5 seeded demo projects. Blast radius = this folder.
#   3. ORG-LEVEL READ-ONLY grants for the pipeline SA — the assessment can see
#      the whole tenancy but can never mutate it (the governance line).
#
# Portal exposure decision (2026-07-13): generic run.app URL protected by
# Cloud Run's built-in IAP — no LB, no Cloud Armor (Armor requires an LB; see
# spec §4b note). Job/service/scheduler shapes live in runtime.tf (phase 2,
# imported from the gcloud originals 2026-07-15).

terraform {
  required_version = ">= 1.9"

  backend "gcs" {} # bucket/prefix via backend.hcl (untracked)

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 6.0"
    }
  }
}

provider "google" {
  user_project_override = true
  billing_project       = var.billing_project
}

# Beta needed only for iap_enabled on the portal service (GA lacks it @6.50).
provider "google-beta" {
  user_project_override = true
  billing_project       = var.billing_project
}

variable "billing_account" {
  type = string
}

variable "org_id" {
  type = string
}

variable "enterprise_dev_folder_id" {
  description = "Enterprise/Dev folder (folders/XXXX) — hosts the tool project"
  type        = string
}

variable "sandbox_folder_id" {
  description = "Sandbox folder (folders/XXXX) — parent of the Zombie Demo folder"
  type        = string
}

variable "billing_project" {
  description = "Quota/billing project for user_project_override API calls"
  type        = string
}

variable "operator_members" {
  description = "Human operator principals (user:… IAM members) — get owner on the tool project + portal IAP access"
  type        = list(string)
}

variable "exclude_projects" {
  description = "Project IDs the pipeline must never assess (foundation plumbing etc.); the tool project itself is always excluded"
  type        = list(string)
  default     = []
}

variable "owner_label" {
  description = "Value for the `owner` label on created projects (team or handle)"
  type        = string
}

locals {
  project = "sdx-demos-zombie-dev"
  region  = "us-central1"
}

# ---------------------------------------------------------------------------
# 1. Tool project
# ---------------------------------------------------------------------------
module "project" {
  source = "./modules/project-factory"

  project_id      = local.project
  project_name    = "SDX Demo Zombie Hunter DEV"
  folder_id       = var.enterprise_dev_folder_id
  billing_account = var.billing_account
  domain          = "demos"
  env             = "dev"
  owner           = var.owner_label

  extra_labels = {
    purpose = "zombie-hunter-demo"
  }

  activate_apis = [
    "run.googleapis.com",
    "cloudbuild.googleapis.com",
    "artifactregistry.googleapis.com",
    "firestore.googleapis.com",
    "storage.googleapis.com",
    "cloudscheduler.googleapis.com",
    "aiplatform.googleapis.com", # Gemini via Vertex for the analyze stage
    "bigquery.googleapis.com",         # hosts the billing-export dataset
    "bigquerydatatransfer.googleapis.com", # required by the billing-export setup form
    "iap.googleapis.com",        # built-in IAP on the portal's run.app URL
    "recommender.googleapis.com",
    "cloudasset.googleapis.com",
    "policyanalyzer.googleapis.com", # SA last-authentication
    "cloudresourcemanager.googleapis.com",
    "iam.googleapis.com",
    "serviceusage.googleapis.com",
    "monitoring.googleapis.com",
    "logging.googleapis.com",
    "apikeys.googleapis.com", # V8 veto: keys bound to SAs (metadata only)
    "geminicloudassist.googleapis.com", # Cloud Assist MCP (analyze stage)
    "compute.googleapis.com",           # Cloud Build default SA lives here
    "cloudkms.googleapis.com",          # seeder's KMS calls bill quota here
  ]
}

# ---------------------------------------------------------------------------
# 2. Zombie Demo folder — the seeder's sandbox
# ---------------------------------------------------------------------------
resource "google_folder" "zombie_demo" {
  display_name = "Zombie Demo"
  parent       = var.sandbox_folder_id
}

# ---------------------------------------------------------------------------
# 3. Service accounts
# ---------------------------------------------------------------------------
resource "google_service_account" "pipeline" {
  project      = module.project.project_id
  account_id   = "zombie-pipeline"
  display_name = "Zombie Hunter assessment pipeline (READ-ONLY vs tenancy)"
}

resource "google_service_account" "portal" {
  project      = module.project.project_id
  account_id   = "zombie-portal"
  display_name = "Zombie Hunter portal (operates the tool only)"
}

resource "google_service_account" "seeder" {
  project      = module.project.project_id
  account_id   = "zombie-seeder"
  display_name = "Zombie Hunter demo seeder (Zombie Demo folder only)"
}

# --- Pipeline SA: org-wide READ-ONLY (spec §5) ------------------------------
locals {
  pipeline_org_roles = [
    "roles/browser",
    "roles/cloudasset.viewer",
    "roles/recommender.viewer",
    "roles/iam.securityReviewer",                 # Policy Analyzer cross-project grants
    "roles/policyanalyzer.activityAnalysisViewer", # SA/key last-authentication
    "roles/cloudkms.viewer",                       # key inventory (V2 sweep) — never decrypt
    "roles/monitoring.viewer",                     # request/activity metrics
    "roles/logging.viewer",                        # audit-log queries via Logging API
    "roles/serviceusage.serviceUsageViewer",       # enabled-APIs census
    "roles/serviceusage.apiKeysViewer",            # V8: API-key metadata (the
    # pipeline lists keys + SA bindings only — it never calls GetKeyString)
  ]

  # In-project working roles for the pipeline job.
  pipeline_project_roles = [
    "roles/datastore.user",
    "roles/aiplatform.user",
    "roles/bigquery.jobUser",
    "roles/logging.logWriter",
  ]
}

resource "google_organization_iam_member" "pipeline_org" {
  for_each = toset(local.pipeline_org_roles)

  org_id = var.org_id
  role   = each.value
  member = "serviceAccount:${google_service_account.pipeline.email}"
}

resource "google_project_iam_member" "pipeline_project" {
  for_each = toset(local.pipeline_project_roles)

  project = module.project.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.pipeline.email}"
}

# Read the org audit-log sink bucket (last-human-action mining).
resource "google_storage_bucket_iam_member" "pipeline_audit_logs" {
  bucket = "sdx-audit-logs"
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.pipeline.email}"
}

# --- Portal SA: operates the tool, never the tenancy ------------------------
locals {
  portal_project_roles = [
    "roles/datastore.user",       # acks + manifests + run metadata
    "roles/logging.logWriter",
    # Cloud Scheduler has no per-job IAM; this project contains only the
    # tool's own scheduler job, so project-level admin == job-scoped intent.
    "roles/cloudscheduler.admin",
    "roles/run.viewer",           # list executions (run-now concurrency guard)
    # run.invoker is granted JOB-SCOPED in runtime.tf.
  ]
}

resource "google_project_iam_member" "portal_project" {
  for_each = toset(local.portal_project_roles)

  project = module.project.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.portal.email}"
}

# --- Seeder SA: mutation rights ONLY inside the Zombie Demo folder ----------
locals {
  # NO projectCreator/projectDeleter: the pool projects are TF-managed (human
  # apply); the seeder only populates/strips resources INSIDE them. Removed
  # 2026-07-15 so project deletion always requires a human — everywhere.
  seeder_folder_roles = [
    "roles/resourcemanager.projectIamAdmin",  # V1 seed: cross-demo-project SA grant
    "roles/resourcemanager.folderViewer",
    "roles/serviceusage.serviceUsageAdmin",   # enable APIs in seeded projects
    "roles/editor",                           # create the tiny resources in seeds
    "roles/cloudkms.admin",                   # V2 seed: CMEK key + cross-project use
  ]
}

resource "google_folder_iam_member" "seeder_folder" {
  for_each = toset(local.seeder_folder_roles)

  folder = google_folder.zombie_demo.name
  role   = each.value
  member = "serviceAccount:${google_service_account.seeder.email}"
}

# The billing account is Argolis-managed: billing.admin is held only by
# Google's provisioning groups, so NO service account can ever be granted
# billing.user on it. The seeder therefore cannot create billing-linked
# projects at runtime. Instead the 5 demo projects are a TF-managed POOL
# (created/billing-linked here by the human caller, who has billing.user)
# and the seeder job only populates/strips the resources INSIDE them.
locals {
  seed_pool = {
    "sdx-zh-husk-alpha" = { apis = ["compute.googleapis.com"] }
    "sdx-zh-husk-beta"  = { apis = ["storage.googleapis.com"] }
    "sdx-zh-heartbeat"  = { apis = ["pubsub.googleapis.com", "cloudscheduler.googleapis.com"] }
    "sdx-zh-veto-sa"    = { apis = ["iam.googleapis.com"] }
    "sdx-zh-veto-cmek"  = { apis = ["cloudkms.googleapis.com"] }
  }
}

module "seed_pool" {
  source   = "./modules/project-factory"
  for_each = local.seed_pool

  project_id      = each.key
  project_name    = "Zombie Demo ${trimprefix(each.key, "sdx-zh-")}"
  folder_id       = google_folder.zombie_demo.name
  billing_account = var.billing_account
  domain          = "demos"
  env             = "sandbox"
  owner           = var.owner_label

  extra_labels = {
    demo-zombie = "true" # bypasses the pipeline tenure guard, badges as seed
  }

  activate_apis = concat(each.value.apis, [
    "cloudresourcemanager.googleapis.com",
    "serviceusage.googleapis.com",
    "iam.googleapis.com",
  ])
}

resource "google_project_iam_member" "seeder_project" {
  for_each = toset(["roles/datastore.user", "roles/logging.logWriter"])

  project = module.project.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.seeder.email}"
}

# Cloud Build runs as the compute default SA on new projects; the landing
# zone strips its primitive grants, so give it the builder role explicitly
# (same fix as devnext/builder.tf).
resource "google_project_iam_member" "cloudbuild_builder" {
  project = module.project.project_id
  role    = "roles/cloudbuild.builds.builder"
  member  = "serviceAccount:${module.project.project_number}-compute@developer.gserviceaccount.com"
}

# ---------------------------------------------------------------------------
# 4. Data plane: Firestore, results bucket, images, billing-export dataset
# ---------------------------------------------------------------------------
resource "google_firestore_database" "db" {
  project     = module.project.project_id
  name        = "(default)"
  location_id = local.region
  type        = "FIRESTORE_NATIVE"
}

resource "google_storage_bucket" "results" {
  project                     = module.project.project_id
  name                        = "sdx-zombie-results"
  location                    = "US"
  uniform_bucket_level_access = true
  # Dated run paths accumulate trend history — no lifecycle delete.
}

resource "google_storage_bucket_iam_member" "pipeline_results" {
  bucket = google_storage_bucket.results.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.pipeline.email}"
}

resource "google_storage_bucket_iam_member" "portal_results" {
  bucket = google_storage_bucket.results.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.portal.email}"
}

# Human operators — kept IAM-identical on this project (the project creator
# already holds the creator-default owner grant; this codifies it and
# matches the other operator identities to it). Owner supersedes piecemeal
# console-access grants (viewer/objectViewer/bigquery.admin).
resource "google_project_iam_member" "operator_owner" {
  for_each = toset(var.operator_members)

  project = module.project.project_id
  role    = "roles/owner"
  member  = each.value
}

resource "google_artifact_registry_repository" "images" {
  project       = module.project.project_id
  location      = local.region
  repository_id = "zombie-images"
  format        = "DOCKER"
}

# Billing-export landing dataset. NOTE: the export itself is enabled manually
# in the console (Billing → Billing export → BigQuery, standard usage cost)
# pointing at THIS dataset — there is no public API for that step. The
# collector degrades gracefully while the dataset is empty.
resource "google_bigquery_dataset" "billing_export" {
  project     = module.project.project_id
  dataset_id  = "billing_export"
  location    = "US"
  description = "Standard usage cost export target (enable manually in Billing console)"
}

resource "google_bigquery_dataset_iam_member" "pipeline_billing" {
  project    = module.project.project_id
  dataset_id = google_bigquery_dataset.billing_export.dataset_id
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.pipeline.email}"
}

# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------
output "project_id" {
  value = module.project.project_id
}

output "zombie_demo_folder" {
  value = google_folder.zombie_demo.name
}

output "pipeline_sa" {
  value = google_service_account.pipeline.email
}

output "portal_sa" {
  value = google_service_account.portal.email
}

output "seeder_sa" {
  value = google_service_account.seeder.email
}

output "results_bucket" {
  value = google_storage_bucket.results.url
}
