variable "project_id" {
  description = "Globally unique project ID (e.g. sdx-agents-core-dev)"
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "Project ID must be 6-30 chars, lowercase alphanumeric and hyphens, start with letter, end with letter or digit."
  }
}

variable "project_name" {
  description = "Human-readable project name"
  type        = string
}

variable "folder_id" {
  description = "Folder ID to place the project in (format: folders/XXXXXXXXXX)"
  type        = string
}

variable "billing_account" {
  description = "Billing account ID"
  type        = string
}

variable "domain" {
  description = "Domain label (platform, agents, demos, lab)"
  type        = string
}

variable "env" {
  description = "Environment label (dev, prod, sandbox)"
  type        = string
}

variable "owner" {
  description = "Owner label applied to created projects"
  type        = string
}

variable "extra_labels" {
  description = "Additional labels to apply"
  type        = map(string)
  default     = {}
}

variable "activate_apis" {
  description = "List of GCP APIs to enable"
  type        = list(string)
  default = [
    "compute.googleapis.com",
    "iam.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "serviceusage.googleapis.com",
  ]
}

variable "iam_bindings" {
  description = "Map of IAM role => list of members"
  type        = map(list(string))
  default     = {}
}
