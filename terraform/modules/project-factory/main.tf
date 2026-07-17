terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

resource "google_project" "this" {
  name                = var.project_name
  project_id          = var.project_id
  folder_id           = var.folder_id
  billing_account     = var.billing_account
  auto_create_network = false

  labels = merge(
    {
      env        = var.env
      domain     = var.domain
      owner      = var.owner
      managed_by = "terraform"
    },
    var.extra_labels,
  )
}

resource "google_project_service" "apis" {
  for_each = toset(var.activate_apis)

  project = google_project.this.project_id
  service = each.value

  disable_dependent_services = false
  disable_on_destroy         = false
}

resource "google_project_iam_member" "bindings" {
  for_each = {
    for pair in flatten([
      for role, members in var.iam_bindings : [
        for member in members : {
          role   = role
          member = member
        }
      ]
    ]) : "${pair.role}--${pair.member}" => pair
  }

  project = google_project.this.project_id
  role    = each.value.role
  member  = each.value.member
}
