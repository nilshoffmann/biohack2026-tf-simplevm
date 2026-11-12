# Two research environment VMs, each with its own data volume:
#   - Visual Studio Code (code-server) on Ubuntu 24.04
#   - Guacamole VNC desktop on Ubuntu 24.04
#
# A standalone configuration with its own state, separate from the repository
# root. Run it from the repository root (the Makefile points Terraform at the
# locally built provider):
#
#   source .envrc
#   make plan  TF="terraform -chdir=examples/two-vms"
#   make apply TF="terraform -chdir=examples/two-vms"
#   make destroy TF="terraform -chdir=examples/two-vms"
#
# Quota: with the defaults this needs 2 VMs, 4 vCPUs, 8 GiB RAM, 2 volumes and
# 40 GiB of volume storage in addition to whatever the project already runs.

terraform {
  required_version = ">= 1.5"

  required_providers {
    simplevm = {
      source = "deNBI/simplevm"
    }
  }
}

# API key from SIMPLEVM_API_KEY, project from SIMPLEVM_PROJECT_ID.
provider "simplevm" {}

variable "name_prefix" {
  type        = string
  default     = "biohack"
  description = "Prefix of the VM names (\"<prefix>-<key>\") and volume names (\"<prefix>-<key>-data\")."
}

variable "vms" {
  description = "VMs to create, by key. The research environment template is taken from the image."
  type = map(object({
    image          = string
    flavor         = optional(string, "de.NBI default")
    volume_size_gb = optional(number, 20)
  }))
  default = {
    vscode = {
      image = "VSCode-ubuntu24.04 de.NBI (2026-10-06)"
    }
    guacamole = {
      image = "GuacamoleVNC-ubuntu24.04 de.NBI (2026-09-30)"
    }
  }
}

variable "create_timeout" {
  type        = string
  default     = "30m"
  description = "How long to wait for each VM and its research environment playbook."
}

# Look up images and flavors first, so that typos fail at plan time.
data "simplevm_image" "this" {
  for_each = var.vms
  name     = each.value.image
}

data "simplevm_flavor" "this" {
  for_each = toset([for vm in var.vms : vm.flavor])
  name     = each.key
}

resource "simplevm_vm" "this" {
  for_each = var.vms

  name   = "${var.name_prefix}-${each.key}"
  flavor = data.simplevm_flavor.this[each.value.flavor].name
  image  = data.simplevm_image.this[each.key].name

  research_environment = {
    template = data.simplevm_image.this[each.key].resenv_template_name
    # URL path of the environment, for example .../biohack_vscode_<n>
    user_path = replace("${var.name_prefix}_${each.key}", "-", "_")
  }

  timeouts = {
    create = var.create_timeout
  }

  lifecycle {
    precondition {
      condition     = data.simplevm_image.this[each.key].resenv_template_name != null && data.simplevm_image.this[each.key].resenv_template_name != ""
      error_message = "Image \"${each.value.image}\" has no research environment template."
    }
  }
}

resource "simplevm_volume" "data" {
  for_each = var.vms

  name = "${var.name_prefix}-${each.key}-data"
  size = each.value.volume_size_gb

  lifecycle {
    precondition {
      condition     = length("${var.name_prefix}-${each.key}-data") <= 32
      error_message = "Volume name \"${var.name_prefix}-${each.key}-data\" is longer than the 32 characters SimpleVM allows; shorten name_prefix or the key."
    }
  }
}

resource "simplevm_volume_attachment" "data" {
  for_each = var.vms

  volume_id = simplevm_volume.data[each.key].id
  vm_id     = simplevm_vm.this[each.key].id
}

output "vms" {
  description = "Per VM: SimpleVM name, research environment, internal IP and the data volume's device (format and mount it once)."
  value = {
    for k, vm in simplevm_vm.this : k => {
      id                       = vm.id
      full_name                = vm.full_name
      template                 = vm.research_environment.template
      research_environment_url = vm.research_environment_url
      fixed_ip                 = vm.fixed_ip
      volume_device            = simplevm_volume_attachment.data[k].device
    }
  }
}
