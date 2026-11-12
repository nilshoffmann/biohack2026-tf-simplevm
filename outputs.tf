output "vm_id" {
  description = "UUID of the VM in SimpleVM."
  value       = simplevm_vm.this.id
}

output "fixed_ip" {
  description = "Fixed (internal) IP address of the VM."
  value       = simplevm_vm.this.fixed_ip
}

output "research_environment_template" {
  description = "Research environment template installed on the VM."
  value       = local.research_environment_template
}

output "research_environment_url" {
  description = "URL of the research environment."
  value       = simplevm_vm.this.research_environment_url
}

output "volume_device" {
  description = "Device of the data volume inside the VM (format and mount it once), or null without a volume."
  value       = one(simplevm_volume_attachment.data[*].device)
}

output "flavor" {
  description = "Size of the VM."
  value = {
    name                  = data.simplevm_flavor.this.name
    vcpus                 = data.simplevm_flavor.this.vcpus
    ram_mb                = data.simplevm_flavor.this.ram
    credits_cost_per_hour = data.simplevm_flavor.this.credits_cost_per_hour
  }
}
