#!/usr/bin/env python3
"""Write an Ansible inventory for the SimpleVM VMs in a Terraform state.

The topology (which VMs exist, their Terraform addresses, research
environments and attached volumes) is read from the Terraform state. How to
reach each VM is read from the SimpleVM API, because the provider does not
store it: SimpleVM VMs are reached through an SSH gateway on a per-VM port,
which the API reports as the VM's "SSH" access mode, for example
"ssh -i /path/to/key ubuntu@129.70.51.6 -p 30424".

Only GET requests are sent to the API (via simplevm_api.py).

    tools/tf_inventory.py -o inventory.yml                      # from `terraform state pull`
    tools/tf_inventory.py --state other/terraform.tfstate -o inv.yml
    tools/tf_inventory.py --offline -o inventory.yml            # internal IPs, no API call
    ansible-inventory -i tools/tf_inventory.py --graph          # as a dynamic inventory script

Environment (also used in dynamic inventory mode, where Ansible passes no options):
    SIMPLEVM_API_KEY, SIMPLEVM_ENDPOINT   API access, see simplevm_api.py
    SIMPLEVM_INVENTORY_STATE              state file instead of `terraform state pull`
    SIMPLEVM_INVENTORY_TF_DIR             directory to run `terraform state pull` in
    SIMPLEVM_INVENTORY_SSH_KEY            sets ansible_ssh_private_key_file
    SIMPLEVM_INVENTORY_USER               overrides ansible_user
    SIMPLEVM_INVENTORY_OFFLINE=1          same as --offline
    SIMPLEVM_INVENTORY_VERBOSE=1          print warnings to stderr in dynamic inventory mode too
    TF                                    terraform binary (default: terraform; e.g. tofu)
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import simplevm_api as api  # noqa: E402

VM_TYPE = "simplevm_vm"
VOLUME_TYPE = "simplevm_volume"
ATTACHMENT_TYPE = "simplevm_volume_attachment"


def warn(msg: str) -> None:
    print(f"warning: {msg}", file=sys.stderr)


# --------------------------------------------------------------------------
# Terraform state


@dataclass
class StateVM:
    address: str
    module: str  # "" for the root module, e.g. "module.lab[0]" otherwise
    resource_name: str
    index_key: Any
    tainted: bool
    attrs: dict
    volumes: list[dict] = field(default_factory=list)

    @property
    def uuid(self) -> str:
        return self.attrs.get("id") or ""


def load_state(state_file: str | None, tf_dir: str | None, tf_bin: str) -> dict:
    if state_file == "-":
        text = sys.stdin.read()
    elif state_file:
        text = Path(state_file).read_text()
    else:
        cmd = [tf_bin] + ([f"-chdir={tf_dir}"] if tf_dir else []) + ["state", "pull"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        except FileNotFoundError:
            raise SystemExit(f"error: {tf_bin!r} not found; install it, set TF, or pass --state FILE")
        if proc.returncode != 0:
            raise SystemExit(f"error: {' '.join(cmd)} failed:\n{proc.stderr.strip()}")
        text = proc.stdout
    if not text.strip():
        raise SystemExit("error: the Terraform state is empty; nothing has been applied yet")
    try:
        state = json.loads(text)
    except json.JSONDecodeError as e:
        raise SystemExit(f"error: the state is not JSON: {e}")
    if state.get("version") != 4:
        raise SystemExit(f"error: unsupported state format version {state.get('version')!r} (expected 4)")
    return state


def _address(r: dict, inst: dict) -> str:
    addr = f"{r['type']}.{r['name']}"
    if "index_key" in inst:
        addr += f"[{json.dumps(inst['index_key'])}]"
    return f"{r['module']}.{addr}" if r.get("module") else addr


def vms_from_state(state: dict) -> list[StateVM]:
    vms: list[StateVM] = []
    volumes: dict[str, dict] = {}
    attachments: list[dict] = []
    for r in state.get("resources", []):
        if r.get("mode") != "managed":
            continue
        for inst in r.get("instances", []):
            a = inst.get("attributes") or {}
            if r["type"] == VM_TYPE:
                vms.append(StateVM(_address(r, inst), r.get("module", ""), r["name"], inst.get("index_key"),
                                   inst.get("status") == "tainted", a))
            elif r["type"] == VOLUME_TYPE:
                volumes[a.get("id", "")] = a
            elif r["type"] == ATTACHMENT_TYPE:
                attachments.append(a)
    by_id = {vm.uuid: vm for vm in vms}
    for att in attachments:
        vm = by_id.get(att.get("vm_id", ""))
        if vm is None:
            continue
        vol = volumes.get(att.get("volume_id", ""), {})
        vm.volumes.append({k: v for k, v in {
            "name": vol.get("name"), "id": att.get("volume_id"), "size_gb": vol.get("size"),
            "device": att.get("device") or None, "path": att.get("path") or None,
        }.items() if v is not None})
    return vms


# --------------------------------------------------------------------------
# SimpleVM API


@dataclass
class SSHAccess:
    user: str | None
    host: str
    port: int | None


def parse_ssh_command(cmd: str) -> SSHAccess | None:
    """Parse e.g. 'ssh -i /path/key ubuntu@129.70.51.6 -p 30424'."""
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        tokens = cmd.split()
    if not tokens or Path(tokens[0]).name != "ssh":
        return None
    user = host = None
    port = None
    takes_value = set("BbcDEeFIiJLlmOoPpQRSWw")
    i = 1
    while i < len(tokens):
        t = tokens[i]
        if t.startswith("-") and len(t) >= 2 and t[1] in takes_value:
            value = t[2:] or (tokens[i + 1] if i + 1 < len(tokens) else "")
            if t[1] == "p" and value.isdigit():
                port = int(value)
            elif t[1] == "l":
                user = value
            i += 1 if t[2:] else 2
            continue
        if t.startswith("-"):
            i += 1
            continue
        if host is None:
            if "@" in t:
                user, host = t.rsplit("@", 1)
            else:
                host = t
        i += 1
    if not host:
        return None
    return SSHAccess(user, host, port)


# Values from the API (and the state, which the provider fills from the API)
# are untrusted: VM names, paths and URLs are chosen by project members. They
# must not be able to inject ssh options or Ansible templates.
_HOSTNAME = re.compile(r"(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                       r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*")
_USERNAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,31}")
_JINJA = re.compile(r"\{[{%#]")


def valid_ip(value: Any) -> bool:
    """An IPv4 or IPv6 address without a scope ID: ipaddress accepts any
    characters after '%' (e.g. 'fe80::1%-oProxyCommand=…'), and VM and gateway
    addresses never need one."""
    if not isinstance(value, str) or "%" in value:
        return False
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def valid_host(value: Any) -> bool:
    """An IP address or DNS hostname; never something ssh could read as an option."""
    return isinstance(value, str) and (valid_ip(value) or _HOSTNAME.fullmatch(value) is not None)


def valid_user(value: Any) -> bool:
    return isinstance(value, str) and _USERNAME.fullmatch(value) is not None


def defuse(text: str) -> str:
    """Break Jinja delimiters in text that is only informational (warnings).

    Every '{' gets a space after it, so no '{' can be directly followed by
    '{', '%' or '#'. Replacing matches one at a time is not enough: '{{{'
    would leave '{ {{'."""
    return text.replace("{", "{ ")


def drop_templates(value: Any, path: str, warnings: list[str]) -> Any:
    """Remove strings that Ansible would evaluate as templates; returns the cleaned value."""
    if isinstance(value, str):
        if _JINJA.search(value):
            warnings.append(f"{path}: value contains a Jinja delimiter and was left out "
                            "(Ansible would evaluate it as a template)")
            return None
        return value
    if isinstance(value, dict):
        out = {k: drop_templates(v, f"{path}.{k}", warnings) for k, v in value.items()}
        return {k: v for k, v in out.items() if v is not None}
    if isinstance(value, list):
        return [v for v in (drop_templates(v, f"{path}[{i}]", warnings) for i, v in enumerate(value)) if v is not None]
    return value


def ssh_access(vm: dict) -> tuple[SSHAccess | None, str | None]:
    """The VM's SSH access from its API modes, and a reason if it was rejected."""
    rejected = None
    for mode in vm.get("modes") or []:
        if not isinstance(mode, dict):
            continue
        cmd = mode.get("copy_field") or ""
        if not isinstance(cmd, str):
            continue
        if (mode.get("name") or "").upper() == "SSH" or cmd.startswith("ssh "):
            parsed = parse_ssh_command(cmd)
            if not parsed:
                continue
            if not valid_host(parsed.host):
                rejected = f"SSH host {parsed.host!r} is not a valid hostname or IP address"
            elif parsed.user is not None and not valid_user(parsed.user):
                rejected = f"SSH user {parsed.user!r} is not a valid user name"
            elif parsed.port is not None and not 1 <= parsed.port <= 65535:
                rejected = f"SSH port {parsed.port} is out of range"
            else:
                return parsed, None
    return None, rejected


def fetch_api_vms(client: api.Client) -> dict[str, dict]:
    return {v.get("uuid"): v for v in client.list_all("vms/by-api-key/") if isinstance(v, dict)}


# --------------------------------------------------------------------------
# Inventory


def group_name(text: str) -> str:
    """Ansible group names may only contain letters, digits and underscores."""
    g = re.sub(r"[^A-Za-z0-9_]", "_", text).strip("_").lower()
    return g if g and not g[0].isdigit() else f"g_{g}"


def host_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", text)


@dataclass
class Options:
    offline: bool = False
    ssh_key: str | None = None
    user: str | None = None
    skip_tainted: bool = False
    group_prefix: str = ""


def build_inventory(state_vms: list[StateVM], api_vms: dict[str, dict] | None, opts: Options) -> dict:
    """Return {"hosts": {name: vars}, "groups": {group: [names]}, "warnings": [...]}."""
    hosts: dict[str, dict] = {}
    groups: dict[str, list[str]] = {}
    warnings: list[str] = []
    p = opts.group_prefix
    names = [vm.attrs.get("name") or vm.resource_name for vm in state_vms]

    for vm, name in zip(state_vms, names):
        if vm.tainted:
            msg = f"{vm.address} is tainted; the next apply replaces it"
            if opts.skip_tainted:
                warnings.append(msg + " (skipped)")
                continue
            warnings.append(msg)
        hostname = host_name(name if names.count(name) == 1 else f"{name}_{vm.index_key if vm.index_key is not None else vm.address}")
        live = (api_vms or {}).get(vm.uuid) if api_vms is not None else None
        if api_vms is not None and live is None:
            warnings.append(f"{vm.address} ({vm.uuid}) is not visible to the API key; skipped "
                            "(deleted outside Terraform, another project, or 'own objects only')")
            continue

        resenv = vm.attrs.get("research_environment") or None
        hv: dict[str, Any] = {}
        reachable = True
        if opts.offline:
            ip = vm.attrs.get("fixed_ip")
            if not valid_ip(ip):
                warnings.append(f"{vm.address} has no valid fixed_ip in the state ({ip!r}); skipped")
                continue
            hv["ansible_host"] = ip
            hv["ansible_user"] = opts.user or "ubuntu"
        else:
            access, rejected = ssh_access(live)
            if access:
                hv["ansible_host"] = access.host
                if access.port:
                    hv["ansible_port"] = access.port
                hv["ansible_user"] = opts.user or access.user or "ubuntu"
            else:
                reachable = False
                ip = next((c for c in (live.get("fixed_ip"), vm.attrs.get("fixed_ip")) if valid_ip(c)), None)
                why = rejected or f"reports no SSH access (vm_state={live.get('vm_state')!r})"
                if ip is None:
                    warnings.append(f"{vm.address}: {why}, and has no valid internal IP; skipped")
                    continue
                warnings.append(f"{vm.address}: {why}; listed in group {p}simplevm_unreachable with its internal IP")
                hv["ansible_host"] = ip
                hv["ansible_user"] = opts.user or "ubuntu"
        if opts.ssh_key:
            hv["ansible_ssh_private_key_file"] = opts.ssh_key

        src = live or {}
        flavor = src.get("flavor") if isinstance(src.get("flavor"), dict) else {}

        def pick(var: str, *candidates: Any) -> Any:
            """First non-empty candidate (API before state) that Ansible would not template."""
            for c in candidates:
                if c in (None, ""):
                    continue
                if isinstance(c, str) and _JINJA.search(c):
                    warnings.append(f"{vm.address}: {var} value contains a Jinja delimiter and was ignored "
                                    "(Ansible would evaluate it as a template)")
                    continue
                return c
            return None

        hv.update({k: v for k, v in {
            "simplevm_uuid": vm.uuid,
            # full_name: the name SimpleVM reports (with its suffix), provider >= 3bea398
            "simplevm_name": pick("simplevm_name", src.get("name"), vm.attrs.get("full_name"), vm.attrs.get("name")),
            "simplevm_tf_address": vm.address,
            "simplevm_project_id": pick("simplevm_project_id", src.get("project"), vm.attrs.get("project_id")),
            "simplevm_fixed_ip": next((c for c in (src.get("fixed_ip"), vm.attrs.get("fixed_ip")) if valid_ip(c)), None),
            "simplevm_flavor": pick("simplevm_flavor", (flavor or {}).get("name"), vm.attrs.get("flavor")),
            "simplevm_image": pick("simplevm_image", src.get("image_name"), vm.attrs.get("image")),
            "simplevm_vm_state": pick("simplevm_vm_state", src.get("vm_state"), vm.attrs.get("vm_state")),
            "simplevm_research_environment": None if not resenv else {
                "template": resenv.get("template"),
                "url": pick("research_environment.url", src.get("research_environment_url"),
                            vm.attrs.get("research_environment_url")),
            },
            "simplevm_volumes": vm.volumes or None,
            "simplevm_tainted": True if vm.tainted else None,
        }.items() if v not in (None, "")})
        if hv.get("simplevm_research_environment"):
            hv["simplevm_research_environment"] = {k: v for k, v in hv["simplevm_research_environment"].items() if v}
        hv = drop_templates(hv, vm.address, warnings)

        if hostname in hosts:
            hostname = host_name(f"{hostname}_{vm.uuid[:8]}")
        hosts[hostname] = hv
        groups.setdefault(f"{p}simplevm", []).append(hostname)
        if resenv and resenv.get("template"):
            groups.setdefault(group_name(f"{p}resenv_{resenv['template']}"), []).append(hostname)
        if vm.module:
            module = re.sub(r"\[.*?\]", "", vm.module)
            groups.setdefault(group_name(f"{p}tf_{module}"), []).append(hostname)
        if not reachable:
            groups.setdefault(f"{p}simplevm_unreachable", []).append(hostname)
    return {"hosts": hosts, "groups": groups, "warnings": [defuse(w) for w in warnings]}


def to_yaml_inventory(inv: dict, header: str = "") -> str:
    """Ansible YAML inventory. Scalars are written as JSON, which is valid YAML."""
    out = [f"# {line}" if line else "#" for line in header.splitlines()]
    out.append("all:")
    if not inv["hosts"]:
        out.append("  hosts: {}")
        return "\n".join(out) + "\n"
    out.append("  hosts:")
    for h, hv in inv["hosts"].items():
        out.append(f"    {json.dumps(h)}:")
        out += _yaml_map(hv, 6)
    out.append("  children:")
    for g, members in sorted(inv["groups"].items()):
        out.append(f"    {g}:")
        out.append("      hosts:")
        out += [f"        {json.dumps(m)}: {{}}" for m in members]
    return "\n".join(out) + "\n"


def _yaml_map(d: dict, indent: int) -> list[str]:
    pad = " " * indent
    lines: list[str] = []
    for k, v in d.items():
        if isinstance(v, dict) and v:
            lines.append(f"{pad}{k}:")
            lines += _yaml_map(v, indent + 2)
        elif isinstance(v, list) and v:
            lines.append(f"{pad}{k}:")
            for item in v:
                if isinstance(item, dict) and item:
                    sub = _yaml_map(item, indent + 4)
                    sub[0] = f"{pad}  - " + sub[0].lstrip()
                    lines += sub
                else:
                    lines.append(f"{pad}  - {json.dumps(item)}")
        else:
            lines.append(f"{pad}{k}: {json.dumps(v)}")
    return lines


def to_dynamic_inventory(inv: dict) -> dict:
    data: dict[str, Any] = {"_meta": {"hostvars": inv["hosts"]}}
    data["all"] = {"children": sorted(inv["groups"]) + ["ungrouped"],
                   "vars": {"simplevm_inventory_warnings": inv["warnings"]}}
    for g, members in inv["groups"].items():
        data[g] = {"hosts": members}
    return data


# --------------------------------------------------------------------------


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    env = os.environ.get
    p = argparse.ArgumentParser(
        description="Write an Ansible inventory for the SimpleVM VMs in a Terraform state.",
        epilog="Without -o the YAML inventory goes to stdout. Called with --list or --host, "
               "it behaves as an Ansible dynamic inventory script.")
    p.add_argument("--state", default=env("SIMPLEVM_INVENTORY_STATE"), metavar="FILE",
                   help="state file to read ('-' for stdin); default: run `terraform state pull`")
    p.add_argument("--chdir", default=env("SIMPLEVM_INVENTORY_TF_DIR"), metavar="DIR",
                   help="Terraform directory for `state pull` (default: current directory)")
    p.add_argument("-o", "--output", metavar="FILE", help="write the YAML inventory to FILE")
    p.add_argument("--offline", action="store_true", default=env("SIMPLEVM_INVENTORY_OFFLINE", "") not in ("", "0"),
                   help="do not call the API; use the internal fixed_ip from the state (reachable only "
                        "from inside the de.NBI Cloud network, e.g. from another VM of the project)")
    p.add_argument("--ssh-key", default=env("SIMPLEVM_INVENTORY_SSH_KEY"), metavar="PATH",
                   help="private key, set as ansible_ssh_private_key_file")
    p.add_argument("--user", default=env("SIMPLEVM_INVENTORY_USER"), help="override ansible_user (default: from the API, else ubuntu)")
    p.add_argument("--skip-tainted", action="store_true", help="leave out VMs that Terraform will replace")
    p.add_argument("--group-prefix", default="", help="prefix for all generated group names")
    p.add_argument("--endpoint", default=env("SIMPLEVM_ENDPOINT") or api.DEFAULT_ENDPOINT, help="SimpleVM API base URL")
    p.add_argument("--key-env", default="SIMPLEVM_API_KEY", metavar="VAR", help="variable holding the API key")
    p.add_argument("--list", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--host", help=argparse.SUPPRESS)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    state = load_state(args.state, args.chdir, os.environ.get("TF") or "terraform")
    vms = vms_from_state(state)

    api_vms = None
    if not args.offline and vms:
        key = os.environ.get(args.key_env, "")
        if not key:
            raise SystemExit(f"error: set ${args.key_env} to look up SSH access, or use --offline")
        try:
            api_vms = fetch_api_vms(api.Client(args.endpoint, key))
        except api.APIError as e:
            raise SystemExit(f"error: {e}")

    inv = build_inventory(vms, api_vms, Options(args.offline, args.ssh_key, args.user, args.skip_tainted, args.group_prefix))
    if not vms:
        inv["warnings"].append("the state contains no simplevm_vm resources")
    # Ansible shows a dynamic inventory script's stderr as [ERROR] lines, so in
    # that mode warnings go into the simplevm_inventory_warnings variable.
    dynamic = args.list or args.host is not None
    if not dynamic or os.environ.get("SIMPLEVM_INVENTORY_VERBOSE"):
        for w in inv["warnings"]:
            warn(w)

    if args.list:
        api.emit_json(to_dynamic_inventory(inv))
        return
    if args.host is not None:
        api.emit_json(inv["hosts"].get(args.host, {}))
        return

    source = args.state or f"{os.environ.get('TF') or 'terraform'} state pull" + (f" (in {args.chdir})" if args.chdir else "")
    header = (f"Generated by tools/tf_inventory.py from {source}, serial {state.get('serial')}.\n"
              f"{'Internal IPs (offline mode).' if args.offline else 'SSH access from the SimpleVM API.'} "
              "Regenerate instead of editing.")
    if inv["warnings"]:
        header += "\n\nWarnings:\n" + "\n".join(f"- {w}" for w in inv["warnings"])
    text = to_yaml_inventory(inv, header)
    if args.output:
        Path(args.output).write_text(text)
        print(f"wrote {len(inv['hosts'])} host(s) to {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
