#!/usr/bin/env python3
"""Read-only SimpleVM REST API client for debugging API responses.

Talks to the same endpoints as the deNBI/simplevm Terraform provider, but
prints what the API actually returns, so responses can be inspected
independently of Terraform. Only GET requests are ever sent: nothing in a
project can be changed with this tool.

Credentials come from the environment, never from the command line:

    export SIMPLEVM_API_KEY=...        # or name another variable with --key-env
    export SIMPLEVM_PROJECT_ID=1234    # optional, defaults to the key's project

Examples:

    simplevm_api.py whoami
    simplevm_api.py vms --resenv-only
    simplevm_api.py resenv my-rstudio-vm        # diagnose a research environment VM
    simplevm_api.py check                       # provider compatibility of all responses
    simplevm_api.py get vms/ --all -v --save responses/

Only the Python standard library is used (Python 3.9+).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

DEFAULT_ENDPOINT = "https://simplevm.denbi.de/portal/api/"
USER_AGENT = "simplevm-api-debug/1.0"
PAGE_SIZE = 100
MAX_PAGES = 1000

# State vocabulary mirrored from the provider (internal/client/types.go).
VM_FAILURE_STATES = {"error", "creation_failed"}
VM_GONE_STATES = {"deleted", "soft-delete", "marked_as_deletable", "not_found"}
PLAYBOOK_RUNNING_TASK_STATES = {"prepare_playbook_build", "building_playbook", "checking_ssh_connection"}
TASK_STATE_PLAYBOOK_FAILED = "playbook_failed"
TASK_STATE_PLAYBOOK_SUCCESSFUL = "playbook_successful"
VM_BUSY_TASK_STATES = {
    "scheduling", "spawning", "networking", "block_device_mapping",
    "powering-off", "powering-on",
    "rebooting", "reboot_pending", "reboot_started",
    "rebooting_hard", "reboot_pending_hard", "reboot_started_hard",
    "pausing", "unpausing", "suspending", "resuming",
    "rescuing", "unrescuing", "rebuilding", "shelving", "unshelving", "deleting",
}


class APIError(Exception):
    def __init__(self, method: str, url: str, status: int, body: str, location: str = ""):
        self.method, self.url, self.status, self.body, self.location = method, url, status, body, location
        msg = f"{method} {url} returned HTTP {status}"
        if status in (401, 403):
            msg += " (check that the API key is valid, not expired, belongs to this project and has the scope)"
        if 300 <= status < 400:
            msg += f" (redirect to {location!r} not followed; check the endpoint and its trailing slash)"
        if body:
            msg += f": {body[:2048]}"
        super().__init__(msg)


# --------------------------------------------------------------------------
# HTTP


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: urllib would forward X-API-KEY to the new host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _is_loopback(host: str) -> bool:
    return host in ("localhost", "127.0.0.1", "::1")


@dataclass
class Response:
    method: str
    url: str
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        text = self.body.decode("utf-8", errors="replace").strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise SystemExit(f"error: response of {self.method} {self.url} is not JSON ({e}):\n{text[:2048]}")


@dataclass
class Client:
    endpoint: str
    api_key: str
    timeout: float = 60.0
    verbose: bool = False
    save_dir: Path | None = None
    on_response: list[Callable[[Response], None]] = field(default_factory=list)
    _counter: int = 0

    def __post_init__(self) -> None:
        if not self.endpoint.endswith("/"):
            self.endpoint += "/"  # avoids Django's slash redirect
        u = urllib.parse.urlsplit(self.endpoint)
        if not u.scheme or not u.netloc:
            raise SystemExit(f"error: endpoint {self.endpoint!r} must be an absolute URL")
        if u.scheme != "https" and not _is_loopback(u.hostname or ""):
            raise SystemExit(f"error: endpoint {self.endpoint!r} must use https (the API key would be sent unencrypted)")
        self._base = u
        self._opener = urllib.request.build_opener(_NoRedirect)
        if self.save_dir:
            self.save_dir.mkdir(parents=True, exist_ok=True)

    def url(self, path: str) -> str:
        if re.match(r"^[a-z]+://", path):
            return self._safe_url(path)
        return self.endpoint + path.lstrip("/")

    def _safe_url(self, raw: str) -> str:
        """Rebuild a server-supplied URL on our own scheme and host."""
        u = urllib.parse.urlsplit(raw)
        if u.netloc and u.netloc != self._base.netloc:
            raise SystemExit(f"error: URL {raw!r} points to another host than the endpoint ({self._base.netloc})")
        if not u.path.startswith(self._base.path):
            raise SystemExit(f"error: URL {raw!r} is outside the endpoint path {self._base.path!r}")
        return urllib.parse.urlunsplit((self._base.scheme, self._base.netloc, u.path, u.query, ""))

    def get(self, path: str, check: bool = True) -> Response:
        url = self.url(path)
        req = urllib.request.Request(url, method="GET", headers={
            "X-API-KEY": self.api_key,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        })
        if self.verbose:
            _err(f"> GET {url}")
            _err("> X-API-KEY: ***")
        try:
            with self._opener.open(req, timeout=self.timeout) as r:
                resp = Response("GET", url, r.status, dict(r.headers.items()), r.read())
        except urllib.error.HTTPError as e:
            with e:
                resp = Response("GET", url, e.code, dict(e.headers.items()) if e.headers else {}, e.read() or b"")
        except urllib.error.URLError as e:
            raise SystemExit(f"error: GET {url}: {e.reason}")
        if self.verbose:
            _err(f"< HTTP {resp.status}")
            for k, v in resp.headers.items():
                _err(f"< {k}: {v}")
            _err("")
        self._save(resp)
        for cb in self.on_response:
            cb(resp)
        if check and not 200 <= resp.status < 300:
            raise APIError("GET", url, resp.status, resp.body.decode("utf-8", "replace").strip(),
                           resp.headers.get("Location", ""))
        return resp

    def _save(self, resp: Response) -> None:
        if not self.save_dir:
            return
        self._counter += 1
        rel = urllib.parse.urlsplit(resp.url)
        slug = re.sub(r"[^A-Za-z0-9]+", "_", rel.path[len(self._base.path):] + ("?" + rel.query if rel.query else "")).strip("_")
        stem = self.save_dir / f"{self._counter:03d}-{slug or 'root'}"
        stem.with_suffix(".body").write_bytes(resp.body)
        stem.with_suffix(".meta.json").write_text(json.dumps(
            {"method": resp.method, "url": resp.url, "status": resp.status, "headers": resp.headers}, indent=2) + "\n")
        if self.verbose:
            _err(f"saved {stem}.body")

    def list_all(self, path: str) -> list[Any]:
        """GET a list endpoint and follow DRF pagination ("next" links)."""
        sep = "&" if "?" in path else "?"
        target: str | None = self.url(f"{path}{sep}page_size={PAGE_SIZE}")
        seen: set[str] = set()
        items: list[Any] = []
        for _ in range(MAX_PAGES):
            if target is None:
                return items
            if target in seen:
                raise SystemExit(f"error: pagination of {path} loops at {target}")
            seen.add(target)
            data = self.get(target).json()
            if isinstance(data, list):
                return items + data
            if not isinstance(data, dict) or "results" not in data:
                raise SystemExit(f"error: {path} returned neither a list nor a page object: {json.dumps(data)[:500]}")
            items += data.get("results") or []
            nxt = data.get("next")
            target = self._safe_url(nxt) if nxt else None
        raise SystemExit(f"error: pagination of {path} exceeded {MAX_PAGES} pages")


# --------------------------------------------------------------------------
# Provider compatibility check
#
# Field types the provider's Go structs expect (internal/client/types.go).
# Go's JSON decoder fails on a non-null value of the wrong type, ignores
# unknown fields and leaves missing ones at their zero value.

STR, INT, NUM, BOOL, STRLIST = "string", "integer", "number", "boolean", "list of strings"

FLAVOR = {"id": INT, "name": STR, "vcpus": INT, "ram": INT, "root_disk": INT,
          "ephemeral_disk": INT, "gpu": INT, "credits_cost_per_hour": NUM}
PROJECT = {"id": INT, "shortname": STR, "description": STR, "cluster_activated": BOOL, "disabled": BOOL,
           "vms_limit": INT, "cores_limit": INT, "ram_limit": INT, "volume_counter_limit": INT,
           "volume_storage_limit": INT, "is_admin": BOOL, "is_member": BOOL}
IMAGE = {"openstack_id": STR, "name": STR, "title": STR, "description": STR, "status": STR, "os_distro": STR,
         "os_version": STR, "tags": STRLIST, "min_ram": INT, "min_disk": INT, "is_snapshot": BOOL,
         "resenv_template_name": STR, "created_at": STR}
VM = {"uuid": STR, "name": STR, "openstack_id": STR, "vm_state": STR, "task_state": STR, "fixed_ip": STR,
      "research_environment_url": STR, "image_name": STR, "project": INT, "flavor": FLAVOR,
      "playbook": "playbook", "created_at_date": STR}
VOLUME = {"uuid": STR, "name": STR, "device": STR, "path": STR, "status": STR, "storage": INT,
          "openstack_id": STR, "project_id": "json.Number",
          "virtual_machine": {"name": STR, "openstack_id": STR, "vm_state": STR}}

# Fields the provider relies on; missing ones are reported, not just tolerated.
IMPORTANT = {
    "vm": {"uuid", "name", "vm_state", "task_state", "image_name", "flavor", "playbook", "research_environment_url"},
    "volume": {"uuid", "name", "status", "storage"},
    "image": {"name", "resenv_template_name"},
    "flavor": {"name"},
    "project": {"id"},
}


def _type_ok(value: Any, want: Any) -> bool:
    if want == STR:
        return isinstance(value, str)
    if want == INT:
        return isinstance(value, int) and not isinstance(value, bool)
    if want == NUM:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if want == BOOL:
        return isinstance(value, bool)
    if want == STRLIST:
        return isinstance(value, list) and all(isinstance(v, str) or v is None for v in value)
    if want == "json.Number":
        return (isinstance(value, (int, float)) and not isinstance(value, bool)) or \
            (isinstance(value, str) and re.fullmatch(r"-?\d+(\.\d+)?([eE][+-]?\d+)?", value) is not None)
    return False


def _jtype(value: Any) -> str:
    return {dict: "object", list: "array", str: "string", bool: "boolean", type(None): "null",
            int: "integer", float: "number"}.get(type(value), type(value).__name__)


def check_object(obj: Any, schema: dict, kind: str, where: str) -> list[str]:
    """Return problems that would break or confuse the provider."""
    problems: list[str] = []
    if not isinstance(obj, dict):
        return [f"{where}: expected an object, got {_jtype(obj)}"]
    for key, want in schema.items():
        if key not in obj:
            if key in IMPORTANT.get(kind, ()):
                problems.append(f"{where}.{key}: missing (provider uses the zero value)")
            continue
        value = obj[key]
        if value is None:
            continue
        if isinstance(want, dict):
            sub = "flavor" if want is FLAVOR else kind
            problems += check_object(value, want, sub, f"{where}.{key}")
        elif want == "playbook":
            pbs = value if isinstance(value, list) else [value]
            for i, pb in enumerate(pbs):
                p = f"{where}.playbook" + (f"[{i}]" if isinstance(value, list) else "")
                if not isinstance(pb, dict):
                    problems.append(f"{p}: expected null, an object or an array of objects, got {_jtype(pb)} -> decode error")
                    continue
                for k, w in (("id", INT), ("done", BOOL)):
                    if pb.get(k) is not None and not _type_ok(pb[k], w):
                        problems.append(f"{p}.{k}: expected {w}, got {_jtype(pb[k])} {pb[k]!r} -> decode error")
        elif not _type_ok(value, want):
            problems.append(f"{where}.{key}: expected {want}, got {_jtype(value)} {value!r} -> decode error")
    return problems


def check_list(items: list[Any], schema: dict, kind: str, label: str) -> list[str]:
    out: list[str] = []
    for i, it in enumerate(items):
        name = it.get("name") or it.get("uuid") or it.get("id") if isinstance(it, dict) else None
        out += check_object(it, schema, kind, f"{label}[{i}]" + (f"({name})" if name else ""))
    return out


# --------------------------------------------------------------------------
# Research environment diagnosis (mirrors untilCreated in vm_resource.go)


def _playbooks(vm: dict) -> list[dict]:
    pb = vm.get("playbook")
    if pb is None:
        return []
    return pb if isinstance(pb, list) else [pb]


def provider_verdict(vm: dict) -> tuple[str, str]:
    """What the provider would conclude while creating this VM with a research environment."""
    state = vm.get("vm_state") or ""
    task = vm.get("task_state") or ""
    if state in VM_FAILURE_STATES:
        return "FAILED", f"vm_state {state!r} is a failure state; apply fails and the VM is tainted"
    if state in VM_GONE_STATES:
        return "GONE", f"vm_state {state!r} means the VM is being deleted; refresh drops it from state"
    if task == TASK_STATE_PLAYBOOK_FAILED:
        return "FAILED", "task_state 'playbook_failed'; apply fails and the VM is tainted"
    if state != "active":
        return "WAITING", f"vm_state is {state!r}, provider waits for 'active'"
    if task in PLAYBOOK_RUNNING_TASK_STATES:
        return "WAITING", f"task_state {task!r} means the playbook is still running"
    pbs = _playbooks(vm)
    all_done = all(isinstance(p, dict) and p.get("done") is True for p in pbs)
    if all_done or task == TASK_STATE_PLAYBOOK_SUCCESSFUL:
        why = "task_state 'playbook_successful'" if task == TASK_STATE_PLAYBOOK_SUCCESSFUL else \
            ("no playbook reported" if not pbs else "all playbooks report done=true")
        return "READY", f"vm_state 'active' and {why}"
    pending = [p.get("id") for p in pbs if isinstance(p, dict) and p.get("done") is not True]
    return "WAITING", (f"playbook(s) {pending} not done and task_state is {task!r}; the provider would keep "
                       "waiting until its create timeout")


def power_verdict(vm: dict) -> str:
    state, task = vm.get("vm_state") or "", vm.get("task_state") or ""
    if state in ("active", "stopped") and task not in VM_BUSY_TASK_STATES:
        return f"power_state reads as {state!r}"
    return f"transient (vm_state={state!r}, task_state={task!r}); power_state drift is not reported"


# --------------------------------------------------------------------------
# Output helpers


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def emit_json(data: Any) -> None:
    json.dump(data, sys.stdout, indent=2, ensure_ascii=False, sort_keys=False)
    sys.stdout.write("\n")


def table(rows: list[dict], cols: list[str]) -> None:
    if not rows:
        _err("(no entries)")
        return
    cells = [[("" if r.get(c) is None else str(r.get(c))) for c in cols] for r in rows]
    widths = [max(len(c), *(len(row[i]) for row in cells)) for i, c in enumerate(cols)]
    print("  ".join(c.upper().ljust(w) for c, w in zip(cols, widths)).rstrip())
    for row in cells:
        print("  ".join(v.ljust(w) for v, w in zip(row, widths)).rstrip())


def output(args: argparse.Namespace, data: Any, cols: list[str] | None = None, rows: list[dict] | None = None) -> None:
    if args.json or cols is None:
        emit_json(data)
    else:
        table(rows if rows is not None else data, cols)


# --------------------------------------------------------------------------
# Commands


def project_id(args: argparse.Namespace, c: Client) -> int:
    if args.project:
        return args.project
    env = os.environ.get("SIMPLEVM_PROJECT_ID")
    if env:
        try:
            return int(env)
        except ValueError:
            raise SystemExit(f"error: SIMPLEVM_PROJECT_ID={env!r} is not a number")
    data = c.get("projects/by-api-key/").json()
    p = data[0] if isinstance(data, list) and data else data
    if not isinstance(p, dict) or "id" not in p:
        raise SystemExit("error: cannot determine the project; pass --project or set SIMPLEVM_PROJECT_ID")
    return int(p["id"])


def find_vms(c: Client, ref: str) -> list[dict]:
    vms = c.list_all("vms/by-api-key/")
    exact = [v for v in vms if v.get("uuid") == ref or v.get("openstack_id") == ref]
    if exact:
        return exact
    named = [v for v in vms if v.get("name") == ref]
    if not named:
        raise SystemExit(f"error: no VM with uuid, openstack_id or name {ref!r} is visible to this key "
                         f"({len(vms)} VMs listed; keys with 'own objects only' only see their own VMs)")
    return named


def cmd_whoami(args, c):
    emit_json(c.get("projects/by-api-key/").json())


def cmd_projects(args, c):
    data = c.list_all("projects/")
    output(args, data, ["id", "shortname", "is_admin", "disabled", "vms_limit", "cores_limit", "ram_limit"])


def cmd_flavors(args, c):
    pid = project_id(args, c)
    data = c.list_all(f"projects/{pid}/flavors/")
    output(args, data, ["name", "vcpus", "ram", "root_disk", "gpu", "credits_cost_per_hour"])


def cmd_images(args, c):
    pid = project_id(args, c)
    path = f"projects/{pid}/snapshots/images/" if args.snapshots else f"projects/{pid}/images/"
    data = c.list_all(path)
    if args.resenv_only:
        data = [i for i in data if isinstance(i, dict) and i.get("resenv_template_name")]
    rows = [{**i, "tags": ",".join(t for t in (i.get("tags") or []) if t)} for i in data if isinstance(i, dict)]
    output(args, data, ["name", "resenv_template_name", "os_distro", "os_version", "tags", "status"], rows)


def cmd_vms(args, c):
    data = c.list_all("vms/by-api-key/")
    if not args.all_states:
        data = [v for v in data if v.get("vm_state") not in VM_GONE_STATES]
    if args.name:
        data = [v for v in data if args.name.lower() in (v.get("name") or "").lower()]
    if args.resenv_only:
        data = [v for v in data if v.get("research_environment_url") or _playbooks(v)]
    rows = [{**v, "flavor": (v.get("flavor") or {}).get("name") if isinstance(v.get("flavor"), dict) else v.get("flavor"),
             "resenv": "yes" if (v.get("research_environment_url") or _playbooks(v)) else ""} for v in data]
    output(args, data, ["uuid", "name", "vm_state", "task_state", "image_name", "flavor", "resenv"], rows)


def cmd_vm(args, c):
    found = find_vms(c, args.ref)
    emit_json(found[0] if len(found) == 1 else found)


def cmd_resenv(args, c):
    found = find_vms(c, args.ref)
    if len(found) > 1:
        _err(f"note: {len(found)} VMs are named {args.ref!r}; showing all, use the uuid to pick one")
    images_cache: dict[int, list[dict]] = {}
    report = []
    for vm in found:
        verdict, why = provider_verdict(vm)
        pid = vm.get("project")
        template = None
        image_note = None
        if isinstance(pid, int):
            try:
                if pid not in images_cache:
                    images_cache[pid] = c.list_all(f"projects/{pid}/images/") + \
                        c.list_all(f"projects/{pid}/snapshots/images/")
                match = [i for i in images_cache[pid] if i.get("name") == vm.get("image_name")]
                if match:
                    template = match[0].get("resenv_template_name") or ""
                else:
                    image_note = "image not found among the project's base or snapshot images"
            except APIError as e:
                image_note = f"image lookup failed: HTTP {e.status}"
        else:
            image_note = f"VM has no numeric 'project' ({vm.get('project')!r}); image lookup skipped"
        r = {
            "uuid": vm.get("uuid"),
            "name": vm.get("name"),
            "vm_state": vm.get("vm_state"),
            "task_state": vm.get("task_state"),
            "image_name": vm.get("image_name"),
            "image_resenv_template_name": template,
            "research_environment_url": vm.get("research_environment_url"),
            "playbook_raw": vm.get("playbook"),
            "provider_create_verdict": verdict,
            "provider_create_reason": why,
            "provider_power_state": power_verdict(vm),
            "compat_problems": check_object(vm, VM, "vm", "vm"),
        }
        if image_note:
            r["image_note"] = image_note
        report.append(r)
    if args.json:
        emit_json(report[0] if len(report) == 1 else report)
        return
    for r in report:
        print(f"VM {r['name']} ({r['uuid']})")
        for k in ("vm_state", "task_state", "image_name", "image_resenv_template_name", "research_environment_url"):
            print(f"  {k:28} {r[k]!r}")
        print(f"  {'playbook (raw)':28} {json.dumps(r['playbook_raw'])}")
        if r.get("image_note"):
            print(f"  {'image note':28} {r['image_note']}")
        print(f"  {'provider would say':28} {r['provider_create_verdict']}: {r['provider_create_reason']}")
        print(f"  {'power_state':28} {r['provider_power_state']}")
        if r["compat_problems"]:
            print("  provider compatibility problems:")
            for p in r["compat_problems"]:
                print(f"    - {p}")
        else:
            print(f"  {'provider compatibility':28} ok")
        print(f"  {'import with':28} terraform import <address> {r['uuid']}")
        print(f"  {'':28} + lifecycle {{ ignore_changes = [research_environment] }}")
        print()


def cmd_volumes(args, c):
    data = c.list_all("volumes/by-api-key/")
    rows = [{**v, "attached_to": (v.get("virtual_machine") or {}).get("name")} for v in data if isinstance(v, dict)]
    output(args, data, ["uuid", "name", "status", "storage", "device", "attached_to"], rows)


def cmd_volume(args, c):
    emit_json(c.get(f"volumes/{urllib.parse.quote(args.uuid, safe='')}/").json())


def cmd_check(args, c):
    """Fetch every endpoint the provider reads and validate the shapes."""
    problems: list[str] = []
    counts: dict[str, Any] = {}

    def run(label: str, fn: Callable[[], list[str]]) -> None:
        try:
            p = fn()
            problems.extend(p)
            _err(f"{'ok  ' if not p else 'FAIL'} {label}" + (f" ({len(p)} problem(s))" if p else ""))
        except APIError as e:
            problems.append(f"{label}: HTTP {e.status} {e.body[:300]}")
            _err(f"FAIL {label}: HTTP {e.status}")

    def by_key() -> list[str]:
        d = c.get("projects/by-api-key/").json()
        counts["projects/by-api-key/"] = "list" if isinstance(d, list) else _jtype(d)
        if isinstance(d, list):
            return ["projects/by-api-key/: empty list"] if not d else check_object(d[0], PROJECT, "project", "by-api-key[0]")
        return check_object(d, PROJECT, "project", "by-api-key")

    def listing(path: str, schema: dict, kind: str) -> Callable[[], list[str]]:
        def f() -> list[str]:
            items = c.list_all(path)
            counts[path] = len(items)
            return check_list(items, schema, kind, path)
        return f

    pid = project_id(args, c)
    run("projects/by-api-key/", by_key)
    run("projects/", listing("projects/", PROJECT, "project"))
    run(f"projects/{pid}/flavors/", listing(f"projects/{pid}/flavors/", FLAVOR, "flavor"))
    run(f"projects/{pid}/images/", listing(f"projects/{pid}/images/", IMAGE, "image"))
    run(f"projects/{pid}/snapshots/images/", listing(f"projects/{pid}/snapshots/images/", IMAGE, "image"))
    run("vms/by-api-key/", listing("vms/by-api-key/", VM, "vm"))
    run("volumes/by-api-key/", listing("volumes/by-api-key/", VOLUME, "volume"))

    if args.json:
        emit_json({"project_id": pid, "counts": counts, "problems": problems})
    else:
        print(f"project {pid}: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
        for p in problems:
            print(f"- {p}")
        print("no provider compatibility problems found" if not problems else f"{len(problems)} problem(s)")
    if problems:
        sys.exit(1)


def cmd_get(args, c):
    if args.all:
        emit_json(c.list_all(args.path))
        return
    resp = c.get(args.path, check=False)
    if args.include:
        print(f"HTTP {resp.status}")
        for k, v in resp.headers.items():
            print(f"{k}: {v}")
        print()
    text = resp.body.decode("utf-8", errors="replace")
    try:
        emit_json(json.loads(text)) if not args.raw else print(text)
    except json.JSONDecodeError:
        print(text)
    if not 200 <= resp.status < 300:
        _err(f"HTTP {resp.status}")
        sys.exit(1)


# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    g = common.add_argument_group("connection and output")
    g.add_argument("--endpoint", default=argparse.SUPPRESS,
                   help=f"API base URL (env SIMPLEVM_ENDPOINT, default {DEFAULT_ENDPOINT})")
    g.add_argument("--key-env", default=argparse.SUPPRESS, metavar="VAR",
                   help="environment variable holding the API key (default SIMPLEVM_API_KEY); "
                        "use one variable per project, e.g. --key-env SIMPLEVM_API_KEY_OTHER")
    g.add_argument("-p", "--project", type=int, default=argparse.SUPPRESS,
                   help="project ID (env SIMPLEVM_PROJECT_ID, default: the key's project)")
    g.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                   help="print JSON instead of a table")
    g.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
                   help="print requests and response headers to stderr (the key is masked)")
    g.add_argument("--save", type=Path, metavar="DIR", default=argparse.SUPPRESS,
                   help="save every raw response body and its status/headers to DIR")
    g.add_argument("--timeout", type=float, default=argparse.SUPPRESS, help="request timeout in seconds (default 60)")

    p = argparse.ArgumentParser(
        prog="simplevm_api.py", parents=[common],
        description="Read-only SimpleVM API client for debugging responses (sends GET requests only).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="The API key is read from $SIMPLEVM_API_KEY (or --key-env); it is never accepted as an argument.")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    def add(name: str, fn, help: str):
        sp = sub.add_parser(name, parents=[common], help=help, description=help)
        sp.set_defaults(fn=fn)
        return sp

    add("whoami", cmd_whoami, "show the project the API key belongs to")
    add("projects", cmd_projects, "list projects visible to the key")
    add("flavors", cmd_flavors, "list flavors of the project")
    sp = add("images", cmd_images, "list base images (or snapshot images) of the project")
    sp.add_argument("--snapshots", action="store_true", help="list snapshot images instead")
    sp.add_argument("--resenv-only", action="store_true", help="only images with a research environment template")
    sp = add("vms", cmd_vms, "list VMs visible to the key")
    sp.add_argument("--name", help="filter by name substring")
    sp.add_argument("--resenv-only", action="store_true", help="only VMs with a research environment")
    sp.add_argument("--all-states", action="store_true", help="include deleted VMs")
    sp = add("vm", cmd_vm, "show the raw API record of one VM")
    sp.add_argument("ref", help="VM uuid, openstack_id or name")
    sp = add("resenv", cmd_resenv,
             "diagnose a research environment VM: raw state fields, what the provider would conclude, "
             "the image's template and provider compatibility problems")
    sp.add_argument("ref", help="VM uuid, openstack_id or name")
    add("volumes", cmd_volumes, "list volumes visible to the key")
    sp = add("volume", cmd_volume, "show the raw API record of one volume")
    sp.add_argument("uuid")
    add("check", cmd_check,
        "fetch every endpoint the provider reads and report fields the provider could not decode "
        "(exit code 1 on problems)")
    sp = add("get", cmd_get, "GET any API path relative to the endpoint, e.g. 'vms/' or 'projects/1234/images/'")
    sp.add_argument("path")
    sp.add_argument("--all", action="store_true", help="follow pagination and print the merged list")
    sp.add_argument("-i", "--include", action="store_true", help="print status line and response headers")
    sp.add_argument("--raw", action="store_true", help="print the body exactly as received")
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    defaults = {"endpoint": os.environ.get("SIMPLEVM_ENDPOINT") or DEFAULT_ENDPOINT, "key_env": "SIMPLEVM_API_KEY",
                "project": None, "json": False, "verbose": False, "save": None, "timeout": 60.0}
    for k, v in defaults.items():
        if not hasattr(args, k):
            setattr(args, k, v)
    key = os.environ.get(args.key_env, "")
    if not key:
        raise SystemExit(f"error: set the API key in ${args.key_env} (see --key-env)")
    c = Client(args.endpoint, key, timeout=args.timeout, verbose=args.verbose, save_dir=args.save)
    try:
        args.fn(args, c)
    except APIError as e:
        raise SystemExit(f"error: {e}")
    except BrokenPipeError:
        pass


if __name__ == "__main__":
    main()
