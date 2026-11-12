# simplevm_api.py: read-only SimpleVM API debug client

A small command line client for the SimpleVM REST API. It reads the same
endpoints as the `deNBI/simplevm` Terraform provider, so you can look at the
real responses without Terraform. Typical use: a VM with a research
environment already exists in some project, and you want to see what the API
reports for it and how the provider would read that.

- **It only sends GET requests**, so it cannot change anything in a project.
- It uses only the Python standard library (Python 3.9+), so there is nothing to install.
- The API key is read from an environment variable and is never accepted as a
  command line argument (arguments are visible in `ps` and in shell history).
  Verbose output masks it.
- It does not follow redirects and only follows pagination links on the
  endpoint's own host, so the key cannot leak to another server. Like the
  provider, it insists on https except for localhost.

## Setup

```bash
export SIMPLEVM_API_KEY="..."        # key of the project you want to inspect
export SIMPLEVM_PROJECT_ID="1234"    # optional; defaults to the key's project
```

API keys belong to one project. To inspect several projects, keep one variable
per key and select it with `--key-env`:

```bash
export SIMPLEVM_KEY_LAB_A="..." SIMPLEVM_KEY_LAB_B="..."
tools/simplevm_api.py --key-env SIMPLEVM_KEY_LAB_B vms
```

The key needs at least `project:read` and `vm:read` (plus volume read access
for the volume commands). If "own objects only" is switched on for the key, it
only sees VMs that its owner created.

## Commands

| Command | What it shows |
|---------|---------------|
| `whoami` | The project of the API key (`projects/by-api-key/`). |
| `projects` | Projects visible to the key. |
| `flavors` | Flavors of the project. |
| `images [--resenv-only] [--snapshots]` | Images with their research environment template. |
| `vms [--name S] [--resenv-only] [--all-states]` | VMs, with state, task state and image. |
| `vm <uuid\|name>` | The raw API record of one VM. |
| `resenv <uuid\|name>` | Diagnosis of a research environment VM (see below). |
| `volumes`, `volume <uuid>` | Volumes, or the raw record of one. |
| `check` | Fetches every endpoint the provider reads and reports fields it could not decode. Exits with 1 on problems. |
| `get <path> [--all] [-i] [--raw]` | Any API path, e.g. `get projects/1234/images/ -i`. `--all` follows pagination. |

Global options (before or after the command): `--json` (JSON instead of
tables), `-v` (requests and response headers on stderr), `--save DIR` (writes
every raw response body plus its status and headers into DIR),
`--endpoint URL`, `-p/--project ID`, `--timeout SECONDS`.

## Debugging an existing research environment VM

```bash
tools/simplevm_api.py vms --resenv-only
tools/simplevm_api.py resenv my-rstudio-vm
```

```
VM my-rstudio-vm (3f2a9c4e-...)
  vm_state                     'active'
  task_state                   ''
  image_name                   'RStudio-Ubuntu-22.04'
  image_resenv_template_name   'rstudio'
  research_environment_url     'https://.../'
  playbook (raw)               [{"id": 1, "done": true}]
  provider would say           READY: vm_state 'active' and all playbooks report done=true
  power_state                  power_state reads as 'active'
  provider compatibility       ok
  import with                  terraform import <address> 3f2a9c4e-...
                               + lifecycle { ignore_changes = [research_environment] }
```

- **provider would say** applies the provider's create-wait logic to the
  current response: `READY`, `WAITING` (with the reason, for example a playbook
  that never reports `done`), `FAILED` (`playbook_failed`, `error`, ...) or
  `GONE`. A VM stuck in `WAITING` is what makes `terraform apply` run into its
  create timeout.
- **provider compatibility** lists fields whose JSON type does not match what
  the provider expects, for example `"project": "1234"` instead of a number,
  or a `playbook` that is neither null, an object nor a list. Such responses
  make the provider fail with a decode error. Missing fields the provider
  relies on are listed too. Null values and unknown extra fields are fine.

To hand responses to the provider developers, save them and check the files
before sharing (they contain project details such as VM names and IPs, but
not the API key):

```bash
tools/simplevm_api.py check --save responses/
tools/simplevm_api.py get vms/ -i --raw
```

## tf_inventory.py: Ansible inventory from the Terraform state

Writes an Ansible inventory for every `simplevm_vm` in the Terraform state, at
any module depth and with `count`/`for_each`. The **topology** comes from the
state: Terraform address, research environment template and attached volumes.
**How to reach a VM** comes from the SimpleVM API, because the provider does not
store it. SimpleVM VMs are reached through an SSH gateway on a per-VM port,
which the API reports as the VM's "SSH" access mode
(`ssh -i … ubuntu@129.70.51.6 -p 30424`). The API is only read, never changed.

```bash
source .envrc                                   # SIMPLEVM_API_KEY of the project
make inventory INVENTORY_ARGS="--ssh-key ~/.ssh/id_ed25519"   # -> inventory.yml (git-ignored)
ansible -i inventory.yml simplevm -m ping
```

Or use it directly as a dynamic inventory script, so Ansible always sees the
current state:

```bash
SIMPLEVM_INVENTORY_SSH_KEY=~/.ssh/id_ed25519 ansible-inventory -i tools/tf_inventory.py --graph
```

Generated for each host (the name is the VM's `name` in Terraform; SimpleVM's
own name, which has a random suffix, is in `simplevm_name`):

```yaml
"biohack-rstudio":
  ansible_host: "129.70.51.6"           # SSH gateway
  ansible_port: 30424
  ansible_user: "ubuntu"
  simplevm_uuid: "91545ad4-…"
  simplevm_name: "biohack-rstudio-faaef"
  simplevm_tf_address: "simplevm_vm.this"
  simplevm_fixed_ip: "192.168.1.168"     # internal address
  simplevm_research_environment: { template: "rstudio", url: "https://…" }
  simplevm_volumes: [ { name: "…-data", device: "/dev/vdb", size_gb: 100, id: "…" } ]
```

Groups: `simplevm` (all VMs), `resenv_<template>` (for example `resenv_rstudio`),
`tf_module_<name>` for VMs created in a module, and `simplevm_unreachable` for
VMs the API reports no SSH access for (for example stopped ones; they keep
their internal IP). `--group-prefix lab_` prefixes all group names.

| Option | Environment (dynamic mode) | Effect |
|--------|----------------------------|--------|
| `--state FILE` / `-` | `SIMPLEVM_INVENTORY_STATE` | Read a state file or stdin instead of running `terraform state pull`. |
| `--chdir DIR` | `SIMPLEVM_INVENTORY_TF_DIR` | Directory for `terraform state pull` (`TF=tofu` for OpenTofu). |
| `--ssh-key PATH` | `SIMPLEVM_INVENTORY_SSH_KEY` | Sets `ansible_ssh_private_key_file`. |
| `--user NAME` | `SIMPLEVM_INVENTORY_USER` | Overrides `ansible_user`. |
| `--offline` | `SIMPLEVM_INVENTORY_OFFLINE=1` | No API call; `ansible_host` is the internal `fixed_ip`, reachable only from inside the de.NBI Cloud network (for example from another VM of the project). |
| `--skip-tainted` | | Leave out VMs that the next apply replaces. |
| `--key-env VAR` | | Variable holding the API key (default `SIMPLEVM_API_KEY`). |

Warnings (tainted VMs, VMs missing from the API, VMs without SSH access) go to
stderr and into the header of the written file. In dynamic inventory mode they
are put into the `simplevm_inventory_warnings` variable instead, because
Ansible shows a script's stderr as errors. Set `SIMPLEVM_INVENTORY_VERBOSE=1`
to print them anyway.

Regenerate the inventory after every apply that replaces a VM: the gateway
port changes with the VM.

## Tests

```bash
python3 -m unittest discover -s tools    # or: make test-tools
```

The Ansible parsing test runs only when `ansible-inventory` is on `PATH`.
