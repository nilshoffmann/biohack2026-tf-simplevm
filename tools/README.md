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
tools/simplevm_api.py get vms/by-api-key/ -i --raw   # GET vms/ is forbidden (403) for API keys
```

## Tests

```bash
python3 -m unittest discover -s tools
```
