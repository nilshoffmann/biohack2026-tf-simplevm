# biohack2026-tf-simplevm

Terraform/OpenTofu deployment of a [SimpleVM](https://simplevm.denbi.de/) virtual
machine (de.NBI Cloud) with a predefined research environment, using the
[`deNBI/simplevm`](https://github.com/deNBI/terraform-provider-simplevm) provider.

By default it creates an **RStudio** VM (`RStudio-Ubuntu-22.04`, flavor
`de.NBI default`). The research environment template is read from the image, so
switching the environment means changing `image`.

## Prerequisites

- Terraform >= 1.5 or OpenTofu >= 1.6
- Go and git (the provider is not published to the registries yet and is built from source)
- SSH access to GitHub (or set `PROVIDER_REPO` to the HTTPS URL)
- A SimpleVM project and an API key (beta, on request) with the scopes
  `project:read`, `vm:read`, the VM change scopes, the volume scopes and
  `volume:change_state`. Create it in the portal: project -> **API Key** tab.

## Quickstart

```bash
make provider                       # build the provider into .provider/ (once)

cp .envrc.example .envrc            # put your API key and project ID in it
source .envrc

cp terraform.tfvars.example terraform.tfvars   # optional: adjust name, flavor, image, volume

make plan
make apply                          # waits until the research environment playbook has finished
make output                         # research_environment_url, fixed_ip, ...
make inventory                      # Ansible inventory (inventory.yml) with SSH gateway/port per VM
make destroy                        # deletes the VM and its volume
```

Use OpenTofu with `make plan TF=tofu`.

The Makefile sets `TF_CLI_CONFIG_FILE=.provider/dev.tfrc`, which points the CLI at
the locally built provider via `dev_overrides`. If you run `terraform` directly,
export it yourself:

```bash
export TF_CLI_CONFIG_FILE="$PWD/.provider/dev.tfrc"
```

**Do not run `terraform init`** while using `dev_overrides`: it tries to download
`deNBI/simplevm` from the registry and fails. `plan`/`apply` work without it (and
print a warning that overrides are in effect). Once the provider is released,
delete `.provider/`, drop `TF_CLI_CONFIG_FILE`, pin a version in `versions.tf` and
run `terraform init` as usual.

To rebuild from another branch, tag or commit: `PROVIDER_REF=v0.1.0 make provider`.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `project_id` | `null` | SimpleVM project ID; falls back to `SIMPLEVM_PROJECT_ID`. |
| `vm_name` | `biohack-rstudio` | VM name, 1 to 27 characters. |
| `flavor` | `de.NBI default` | Flavor (VM size). |
| `image` | `RStudio-Ubuntu-22.04` | Image that supports a research environment. |
| `research_environment_template` | `null` | Override the template; `null` uses the image's. |
| `research_environment_user_path` | `""` | URL path of the environment; empty lets SimpleVM choose. |
| `volume_size_gb` | `null` | Create and attach a data volume of this size. |
| `power_state` | `active` | `active` or `stopped`. |
| `create_timeout` | `60m` | How long to wait for the VM and the playbook. |

Flavor and image names are looked up at plan time, so typos fail before anything
is created. The plan also fails if the chosen image has no research environment.

To see which images and templates your project offers, add temporarily:

```hcl
data "simplevm_images" "all" {}
output "images" { value = data.simplevm_images.all.images }
```

## Things to know

- Only `power_state` changes in place. Changing `vm_name`, `flavor`, `image` or
  the research environment **replaces the VM** (deletes it and its local disk).
  Read the plan before applying.
- Set `power_state = "stopped"` to pause the VM (saves credits) without deleting it.
- A failed playbook taints the VM; the next apply replaces it.
- An attached volume is not formatted or mounted. Do that once inside the VM, see
  the [SimpleVM volumes docs](https://simplevm.denbi.de/wiki/simple_vm/volumes/).
  Shrinking `volume_size_gb` replaces the volume and **loses its data**.
- State is kept locally in `terraform.tfstate` (git-ignored). Keep it: without it
  Terraform forgets the VM and would create a new one. The owner of the API key
  owns the VM; share access in the SimpleVM portal.

## Layout

| File | Purpose |
|------|---------|
| `versions.tf` | Terraform and provider requirements |
| `providers.tf` | Provider configuration (credentials from the environment) |
| `main.tf` | Flavor/image lookup, VM with research environment, optional volume |
| `variables.tf`, `outputs.tf` | Inputs and outputs |
| `terraform.tfvars.example`, `.envrc.example` | Templates for local settings and credentials |
| `scripts/build-provider.sh` | Builds the provider and writes `.provider/dev.tfrc` |
| `tools/tf_inventory.py` | Ansible inventory from the Terraform state, see [tools/README.md](tools/README.md) |
| `tools/simplevm_api.py` | Read-only API client for debugging responses, see [tools/README.md](tools/README.md) |
