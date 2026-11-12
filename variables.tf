variable "project_id" {
  type        = number
  default     = null
  description = "SimpleVM project ID. Leave null to use the SIMPLEVM_PROJECT_ID environment variable."
}

variable "vm_name" {
  type        = string
  default     = "biohack-rstudio"
  description = "Name of the VM, 1 to 27 characters (the optional volume is named \"<vm_name>-data\", and SimpleVM allows 32 characters for volume names)."

  validation {
    condition     = length(var.vm_name) >= 1 && length(var.vm_name) <= 27
    error_message = "vm_name must have 1 to 27 characters."
  }
}

variable "flavor" {
  type        = string
  default     = "de.NBI default"
  description = "Flavor (VM size) name, as shown in the SimpleVM portal."
}

variable "image" {
  type        = string
  default     = "RStudio-ubuntu24.04 de.NBI (2026-10-02)"
  description = "Name of an image that supports a research environment, for example \"RStudio-ubuntu24.04 de.NBI (2026-10-02)\"."
}

variable "research_environment_template" {
  type        = string
  default     = null
  description = "Research environment template, for example \"rstudio\". Leave null to use the template the image advertises."
}

variable "research_environment_user_path" {
  type        = string
  default     = ""
  description = "URL path under which the research environment is served. Empty lets SimpleVM choose."
}

variable "volume_size_gb" {
  type        = number
  default     = null
  description = "Size in GiB of a data volume to create and attach. Leave null for no volume."

  validation {
    condition     = var.volume_size_gb == null ? true : var.volume_size_gb >= 1
    error_message = "volume_size_gb must be at least 1, or null for no volume."
  }
}

variable "power_state" {
  type        = string
  default     = "active"
  description = "Desired power state of the VM: \"active\" or \"stopped\". The only setting that changes in place; everything else replaces the VM."

  validation {
    condition     = contains(["active", "stopped"], var.power_state)
    error_message = "power_state must be \"active\" or \"stopped\"."
  }
}

variable "create_timeout" {
  type        = string
  default     = "60m"
  description = "How long to wait for the VM and its research environment playbook, as a Go duration."
}
