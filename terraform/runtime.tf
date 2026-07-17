# Phase 2 — runtime shapes (jobs, portal service, scheduler), codified from
# the gcloud-deployed originals of 2026-07-13 (devnext pattern). Import blocks
# below adopt the live resources into state on first apply; remove them after.
#
# Images are built by Cloud Build (cloudbuild.yaml in sequin-au/zombie-hunter)
# and referenced as :latest — a rebuild + next execution picks up new code, so
# TF owns the SHAPE, not the rollout.

locals {
  image_repo = "${local.region}-docker.pkg.dev/${module.project.project_id}/${google_artifact_registry_repository.images.repository_id}"
}

# ---------------------------------------------------------------------------
# Assessment pipeline job (daily via scheduler, or portal Run-now)
# ---------------------------------------------------------------------------
resource "google_cloud_run_v2_job" "pipeline" {
  project  = module.project.project_id
  name     = "zombie-pipeline"
  location = local.region

  template {
    task_count = 1
    template {
      service_account = google_service_account.pipeline.email
      timeout         = "3600s"
      max_retries     = 0

      containers {
        image = "${local.image_repo}/pipeline:latest"

        env {
          name  = "ORG_ID"
          value = var.org_id
        }
        env {
          name  = "RESULTS_BUCKET"
          value = google_storage_bucket.results.name
        }
        env {
          name  = "BILLING_DATASET"
          value = "${module.project.project_id}.${google_bigquery_dataset.billing_export.dataset_id}"
        }
        env {
          name  = "GOOGLE_CLOUD_PROJECT"
          value = module.project.project_id
        }
        env {
          name  = "EXCLUDE_PROJECTS"
          value = join(",", var.exclude_projects)
        }

        resources {
          limits = {
            cpu    = "1000m"
            memory = "1Gi"
          }
        }
      }
    }
  }

  lifecycle {
    # gcloud stamps client-name/client-version noise on the deployed job.
    ignore_changes = [client, client_version, template[0].labels, template[0].annotations]
  }
}

# ---------------------------------------------------------------------------
# Demo seeder job (portal Seed/Teardown buttons; MODE overridden per-execution)
# ---------------------------------------------------------------------------
resource "google_cloud_run_v2_job" "seeder" {
  project  = module.project.project_id
  name     = "zombie-seeder"
  location = local.region

  template {
    task_count = 1
    template {
      service_account = google_service_account.seeder.email
      timeout         = "3600s"
      max_retries     = 0

      containers {
        image = "${local.image_repo}/seeder:latest"

        env {
          name  = "DEMO_FOLDER"
          value = trimprefix(google_folder.zombie_demo.name, "folders/")
        }
        env {
          name  = "BILLING_ACCOUNT"
          value = var.billing_account
        }
        env {
          name  = "GOOGLE_CLOUD_PROJECT"
          value = module.project.project_id
        }
        env {
          name  = "MODE"
          value = "seed"
        }

        resources {
          limits = {
            cpu    = "1000m"
            memory = "512Mi"
          }
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [client, client_version, template[0].labels, template[0].annotations]
  }
}

# The portal's Run-now / Seed buttons execute the jobs as the portal SA —
# job-scoped invoker, per the main.tf governance note.
resource "google_cloud_run_v2_job_iam_member" "portal_runs_pipeline" {
  project  = module.project.project_id
  location = local.region
  name     = google_cloud_run_v2_job.pipeline.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.portal.email}"
}

resource "google_cloud_run_v2_job_iam_member" "portal_runs_seeder" {
  project  = module.project.project_id
  location = local.region
  name     = google_cloud_run_v2_job.seeder.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.portal.email}"
}

# Seed/Teardown pass MODE as an env override, and RunJob-with-overrides needs
# run.jobs.runWithOverrides — NOT included in roles/run.invoker (found the
# hard way: portal /api/seed 403'd). Minimal custom role, seeder-job-scoped.
resource "google_project_iam_custom_role" "job_override_runner" {
  project     = module.project.project_id
  role_id     = "zombieJobOverrideRunner"
  title       = "Zombie Hunter job runner (with overrides)"
  description = "run.invoker + the runWithOverrides bit the portal needs for MODE=seed|teardown"
  permissions = ["run.jobs.run", "run.jobs.runWithOverrides"]
}

resource "google_cloud_run_v2_job_iam_member" "portal_overrides_seeder" {
  project  = module.project.project_id
  location = local.region
  name     = google_cloud_run_v2_job.seeder.name
  role     = google_project_iam_custom_role.job_override_runner.id
  member   = "serviceAccount:${google_service_account.portal.email}"
}

# ---------------------------------------------------------------------------
# Portal service — Cloud Run built-in IAP on the generic run.app URL
# (decision 2026-07-13: no LB, no Cloud Armor)
# ---------------------------------------------------------------------------
resource "google_cloud_run_v2_service" "portal" {
  provider = google-beta # iap_enabled is beta-only at provider 6.50

  project  = module.project.project_id
  name     = "zombie-portal"
  location = local.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  iap_enabled = true

  template {
    service_account                  = google_service_account.portal.email
    timeout                          = "300s"
    max_instance_request_concurrency = 80

    containers {
      image = "${local.image_repo}/portal:latest"

      ports {
        container_port = 8080
      }

      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = module.project.project_id
      }
      env {
        name  = "RESULTS_BUCKET"
        value = google_storage_bucket.results.name
      }
      env {
        name  = "REGION"
        value = local.region
      }

      resources {
        limits = {
          cpu    = "1000m"
          memory = "512Mi"
        }
        startup_cpu_boost = true
      }
    }
  }

  lifecycle {
    # scaling: the API echoes an empty default block — perma-diff if managed.
    ignore_changes = [client, client_version, template[0].labels, template[0].annotations, scaling]
  }
}

# IAP's service agent fronts all requests; it is the ONLY direct invoker.
resource "google_cloud_run_v2_service_iam_member" "iap_invokes_portal" {
  project  = module.project.project_id
  location = local.region
  name     = google_cloud_run_v2_service.portal.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:service-${module.project.project_number}@gcp-sa-iap.iam.gserviceaccount.com"
}

# Who may pass IAP into the portal — the human operator identities.
resource "google_iap_web_cloud_run_service_iam_member" "operators" {
  for_each = toset(var.operator_members)

  project                = module.project.project_id
  location               = local.region
  cloud_run_service_name = google_cloud_run_v2_service.portal.name
  role                   = "roles/iap.httpsResourceAccessor"
  member                 = each.value
}

# ---------------------------------------------------------------------------
# Daily schedule — 06:00 Sydney, fires the pipeline as the portal SA
# ---------------------------------------------------------------------------
resource "google_cloud_scheduler_job" "daily" {
  project          = module.project.project_id
  region           = local.region
  name             = "zombie-daily"
  schedule         = "0 6 * * *"
  time_zone        = "Australia/Sydney"
  attempt_deadline = "180s"

  retry_config {
    min_backoff_duration = "5s"
    max_backoff_duration = "3600s"
    max_doublings        = 5
    max_retry_duration   = "0s"
  }

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${module.project.project_id}/locations/${local.region}/jobs/${google_cloud_run_v2_job.pipeline.name}:run"

    oauth_token {
      service_account_email = google_service_account.portal.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }
}
