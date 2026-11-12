# Look up the flavor and image first so that typos fail at plan time,
# not after a half-created VM.
data "simplevm_flavor" "this" {
  name = var.flavor
}

data "simplevm_image" "this" {
  name = var.image
}

locals {
  research_environment_template = coalesce(
    var.research_environment_template,
    data.simplevm_image.this.resenv_template_name,
    "none",
  )
}

resource "simplevm_vm" "this" {
  name        = var.vm_name
  flavor      = data.simplevm_flavor.this.name
  image       = data.simplevm_image.this.name
  power_state = var.power_state

  research_environment = {
    template  = local.research_environment_template
    user_path = var.research_environment_user_path
  }

  timeouts = {
    create = var.create_timeout
  }

  lifecycle {
    precondition {
      condition     = local.research_environment_template != "none"
      error_message = "Image \"${var.image}\" has no research environment template. Pick an image with one (see the simplevm_images data source) or set research_environment_template."
    }
  }
}

resource "simplevm_volume" "data" {
  count = var.volume_size_gb == null ? 0 : 1

  name = "${var.vm_name}-data"
  size = var.volume_size_gb
}

resource "simplevm_volume_attachment" "data" {
  count = var.volume_size_gb == null ? 0 : 1

  volume_id = simplevm_volume.data[0].id
  vm_id     = simplevm_vm.this.id
}
