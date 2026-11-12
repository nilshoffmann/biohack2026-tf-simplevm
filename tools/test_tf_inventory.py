"""Tests for tf_inventory.py: python3 -m unittest discover -s tools"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tf_inventory as inv  # noqa: E402


def vm(uuid, name, template=None, fixed_ip="192.168.1.10"):
    return {"id": uuid, "name": name, "fixed_ip": fixed_ip, "flavor": "de.NBI default", "image": "img",
            "project_id": 7, "vm_state": "active", "research_environment_url": "",
            "research_environment": {"template": template, "create_only": False, "user_path": ""} if template else None}


STATE = {
    "version": 4, "serial": 3, "resources": [
        {"mode": "data", "type": "simplevm_image", "name": "x", "instances": [{"attributes": {"id": "i"}}]},
        {"mode": "managed", "type": "simplevm_vm", "name": "this",
         "instances": [{"attributes": vm("u-root", "rstudio", "rstudio")}]},
        {"mode": "managed", "type": "simplevm_volume", "name": "data",
         "instances": [{"index_key": 0, "attributes": {"id": "vol1", "name": "rstudio-data", "size": 50}}]},
        {"mode": "managed", "type": "simplevm_volume_attachment", "name": "data",
         "instances": [{"index_key": 0, "attributes": {"id": "vol1/u-root", "volume_id": "vol1", "vm_id": "u-root",
                                                       "device": "/dev/vdb", "path": ""}}]},
        {"module": "module.workers[\"a\"]", "mode": "managed", "type": "simplevm_vm", "name": "this",
         "instances": [{"index_key": 0, "attributes": vm("u-w0", "worker")},
                       {"index_key": 1, "attributes": vm("u-w1", "worker"), "status": "tainted"}]},
        {"mode": "managed", "type": "simplevm_vm", "name": "gone",
         "instances": [{"index_key": "k", "attributes": vm("u-gone", "gone")}]},
    ],
}

API = {
    "u-root": {"uuid": "u-root", "name": "rstudio-abcde", "vm_state": "active", "fixed_ip": "192.168.1.10",
               "project": 7, "image_name": "img", "flavor": {"name": "de.NBI default"},
               "research_environment_url": "https://simplevm.example/rs",
               "modes": [{"name": "SSH", "type": "terminal",
                          "copy_field": "ssh -i /path/to/your/ssh/private/key ubuntu@129.70.51.6 -p 30424"},
                         {"name": "rstudio", "type": "browser", "copy_field": "https://simplevm.example/rs"}]},
    "u-w0": {"uuid": "u-w0", "name": "worker-1", "vm_state": "active",
             "modes": [{"name": "SSH", "copy_field": "ssh -p 30011 -i key ubuntu@gw.example"}]},
    "u-w1": {"uuid": "u-w1", "name": "worker-2", "vm_state": "stopped", "fixed_ip": "192.168.1.12", "modes": []},
}


class ParseSSHTest(unittest.TestCase):
    def test_simplevm_format(self):
        a = inv.parse_ssh_command("ssh -i /path/to/your/ssh/private/key ubuntu@129.70.51.6 -p 30424")
        self.assertEqual((a.user, a.host, a.port), ("ubuntu", "129.70.51.6", 30424))

    def test_variants(self):
        a = inv.parse_ssh_command("ssh -p30011 -o StrictHostKeyChecking=no -l centos gw.example")
        self.assertEqual((a.user, a.host, a.port), ("centos", "gw.example", 30011))
        a = inv.parse_ssh_command("ssh -A gw.example")
        self.assertEqual((a.user, a.host, a.port), (None, "gw.example", None))

    def test_not_ssh(self):
        self.assertIsNone(inv.parse_ssh_command("https://example.org"))
        self.assertIsNone(inv.parse_ssh_command(""))


class BuildTest(unittest.TestCase):
    def build(self, api=API, **opts):
        return inv.build_inventory(inv.vms_from_state(STATE), api, inv.Options(**opts))

    def test_online(self):
        r = self.build(ssh_key="~/.ssh/id")
        h = r["hosts"]
        self.assertEqual(set(h), {"rstudio", "worker_0", "worker_1"})
        root = h["rstudio"]
        self.assertEqual((root["ansible_host"], root["ansible_port"], root["ansible_user"]),
                         ("129.70.51.6", 30424, "ubuntu"))
        self.assertEqual(root["ansible_ssh_private_key_file"], "~/.ssh/id")
        self.assertEqual(root["simplevm_name"], "rstudio-abcde")
        self.assertEqual(root["simplevm_research_environment"],
                         {"template": "rstudio", "url": "https://simplevm.example/rs"})
        self.assertEqual(root["simplevm_volumes"],
                         [{"name": "rstudio-data", "id": "vol1", "size_gb": 50, "device": "/dev/vdb"}])
        self.assertEqual(h["worker_0"]["simplevm_tf_address"], 'module.workers["a"].simplevm_vm.this[0]')
        self.assertEqual(h["worker_0"]["ansible_port"], 30011)
        self.assertTrue(h["worker_1"]["simplevm_tainted"])

        g = r["groups"]
        self.assertEqual(g["simplevm"], ["rstudio", "worker_0", "worker_1"])
        self.assertEqual(g["resenv_rstudio"], ["rstudio"])
        self.assertEqual(g["tf_module_workers"], ["worker_0", "worker_1"])
        self.assertEqual(g["simplevm_unreachable"], ["worker_1"])
        self.assertEqual(h["worker_1"]["ansible_host"], "192.168.1.12")
        joined = "\n".join(r["warnings"])
        self.assertIn("u-gone", joined)
        self.assertIn("tainted", joined)

    def test_skip_tainted_and_prefix(self):
        r = self.build(skip_tainted=True, group_prefix="lab_")
        self.assertNotIn("worker_1", r["hosts"])
        self.assertIn("lab_simplevm", r["groups"])
        self.assertIn("lab_resenv_rstudio", r["groups"])

    def test_offline_uses_full_name_from_state(self):
        state = json.loads(json.dumps(STATE))
        state["resources"][1]["instances"][0]["attributes"]["full_name"] = "rstudio-xyz12"
        r = inv.build_inventory(inv.vms_from_state(state), None, inv.Options(offline=True))
        self.assertEqual(r["hosts"]["rstudio"]["simplevm_name"], "rstudio-xyz12")

    def test_offline_uses_fixed_ip_and_keeps_all(self):
        r = self.build(api=None, offline=True, user="admin")
        self.assertEqual(r["hosts"]["rstudio"]["ansible_host"], "192.168.1.10")
        self.assertEqual(r["hosts"]["rstudio"]["ansible_user"], "admin")
        self.assertNotIn("ansible_port", r["hosts"]["rstudio"])
        self.assertIn("gone", r["hosts"])

    def test_dynamic_format(self):
        d = inv.to_dynamic_inventory(self.build())
        self.assertIn("rstudio", d["_meta"]["hostvars"])
        self.assertEqual(d["resenv_rstudio"], {"hosts": ["rstudio"]})
        self.assertIn("simplevm", d["all"]["children"])

    def test_empty_inventory_yaml(self):
        self.assertIn("hosts: {}", inv.to_yaml_inventory({"hosts": {}, "groups": {}, "warnings": []}))


class StateTest(unittest.TestCase):
    def test_rejects_old_format(self):
        with tempfile.NamedTemporaryFile("w", suffix=".tfstate", delete=False) as f:
            json.dump({"version": 3}, f)
        try:
            with self.assertRaises(SystemExit):
                inv.load_state(f.name, None, "terraform")
        finally:
            os.unlink(f.name)

    def test_state_pull_failure(self):
        with self.assertRaises(SystemExit) as cm:
            inv.load_state(None, None, "definitely-not-terraform")
        self.assertIn("not found", str(cm.exception))


@unittest.skipUnless(shutil.which("ansible-inventory"), "ansible-inventory not installed")
class AnsibleParsesTest(unittest.TestCase):
    def test_yaml_and_dynamic(self):
        r = inv.build_inventory(inv.vms_from_state(STATE), API, inv.Options())
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "hosts.yml")
            with open(path, "w") as f:
                f.write(inv.to_yaml_inventory(r, "header\nwith: yaml-ish: text"))
            out = subprocess.run(["ansible-inventory", "-i", path, "--list"], capture_output=True, text=True, check=True)
        parsed = json.loads(out.stdout)
        self.assertEqual(parsed["_meta"]["hostvars"]["rstudio"]["ansible_port"], 30424)
        self.assertEqual(parsed["_meta"]["hostvars"]["rstudio"]["simplevm_volumes"][0]["device"], "/dev/vdb")
        self.assertEqual(sorted(parsed["resenv_rstudio"]["hosts"]), ["rstudio"])


class MainTest(unittest.TestCase):
    def test_writes_file_from_state_file(self):
        with tempfile.TemporaryDirectory() as d:
            st, out = os.path.join(d, "s.tfstate"), os.path.join(d, "inv.yml")
            with open(st, "w") as f:
                json.dump(STATE, f)
            err = io.StringIO()
            with mock.patch.object(inv, "fetch_api_vms", return_value=API), \
                    mock.patch.dict(os.environ, {"SIMPLEVM_API_KEY": "k"}), contextlib.redirect_stderr(err):
                inv.main(["--state", st, "-o", out])
            text = open(out).read()
        self.assertIn('ansible_host: "129.70.51.6"', text)
        self.assertIn("# - simplevm_vm.gone", text)
        self.assertIn("wrote 3 host(s)", err.getvalue())

    def test_dynamic_mode_is_quiet(self):
        with tempfile.TemporaryDirectory() as d:
            st = os.path.join(d, "s.tfstate")
            with open(st, "w") as f:
                json.dump(STATE, f)
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(inv, "fetch_api_vms", return_value=API), \
                    mock.patch.dict(os.environ, {"SIMPLEVM_API_KEY": "k", "SIMPLEVM_INVENTORY_STATE": st}), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                inv.main(["--list"])
        self.assertEqual(err.getvalue(), "")
        self.assertTrue(json.loads(out.getvalue())["all"]["vars"]["simplevm_inventory_warnings"])

    def test_needs_key_unless_offline(self):
        with tempfile.TemporaryDirectory() as d:
            st = os.path.join(d, "s.tfstate")
            with open(st, "w") as f:
                json.dump(STATE, f)
            with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(SystemExit) as cm:
                inv.main(["--state", st])
        self.assertIn("--offline", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
