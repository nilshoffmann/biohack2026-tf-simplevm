"""Tests for simplevm_api.py: python3 -m unittest discover -s tools"""

import contextlib
import io
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import simplevm_api as api  # noqa: E402

GOOD_VM = {
    "uuid": "u1", "name": "rstudio", "openstack_id": "os1", "vm_state": "active", "task_state": None,
    "fixed_ip": "10.0.0.1", "research_environment_url": "https://x/u1/", "image_name": "RStudio",
    "project": 7, "flavor": {"id": 1, "name": "de.NBI default", "vcpus": 4, "ram": 8192},
    "playbook": {"id": 3, "done": True}, "created_at_date": "2026-01-01",
}


class VerdictTest(unittest.TestCase):
    def v(self, **kw):
        return api.provider_verdict({**GOOD_VM, **kw})[0]

    def test_ready_single_object_playbook(self):
        self.assertEqual(self.v(), "READY")

    def test_ready_on_playbook_successful_even_if_not_done(self):
        self.assertEqual(self.v(playbook=[{"id": 1, "done": False}], task_state="playbook_successful"), "READY")

    def test_waiting_while_playbook_runs(self):
        self.assertEqual(self.v(task_state="building_playbook"), "WAITING")

    def test_waiting_when_playbook_not_done(self):
        self.assertEqual(self.v(playbook=[{"id": 1, "done": True}, {"id": 2, "done": False}]), "WAITING")

    def test_waiting_when_not_active(self):
        self.assertEqual(self.v(vm_state="building"), "WAITING")

    def test_failed(self):
        self.assertEqual(self.v(task_state="playbook_failed"), "FAILED")
        self.assertEqual(self.v(vm_state="creation_failed"), "FAILED")

    def test_gone(self):
        self.assertEqual(self.v(vm_state="soft-delete"), "GONE")

    def test_null_playbook_counts_as_done(self):
        self.assertEqual(self.v(playbook=None), "READY")


class CompatTest(unittest.TestCase):
    def test_good_vm(self):
        self.assertEqual(api.check_object(GOOD_VM, api.VM, "vm", "vm"), [])

    def test_wrong_types_are_decode_errors(self):
        vm = {**GOOD_VM, "project": "7", "flavor": {"id": "1", "name": "x"}, "playbook": [{"id": 1, "done": "yes"}]}
        p = api.check_object(vm, api.VM, "vm", "vm")
        self.assertEqual(len(p), 3, p)
        self.assertTrue(any("vm.project" in x for x in p))
        self.assertTrue(any("vm.flavor.id" in x for x in p))
        self.assertTrue(any("playbook[0].done" in x for x in p))

    def test_playbook_must_be_object_or_list(self):
        p = api.check_object({**GOOD_VM, "playbook": "done"}, api.VM, "vm", "vm")
        self.assertEqual(len(p), 1)

    def test_missing_important_field(self):
        vm = dict(GOOD_VM)
        del vm["research_environment_url"]
        p = api.check_object(vm, api.VM, "vm", "vm")
        self.assertEqual(p, ["vm.research_environment_url: missing (provider uses the zero value)"])

    def test_nulls_and_extra_fields_are_fine(self):
        vm = {**GOOD_VM, "fixed_ip": None, "something_new": [1, 2]}
        self.assertEqual(api.check_object(vm, api.VM, "vm", "vm"), [])

    def test_volume_project_id_numeric_string(self):
        vol = {"uuid": "v", "name": "d", "status": "available", "storage": 1, "project_id": "12"}
        self.assertEqual(api.check_object(vol, api.VOLUME, "volume", "v"), [])
        self.assertEqual(len(api.check_object({**vol, "project_id": "abc"}, api.VOLUME, "volume", "v")), 1)


class Handler(BaseHTTPRequestHandler):
    routes: dict = {}
    seen: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        Handler.seen.append((self.path, self.headers.get("X-API-KEY")))
        status, body, headers = Handler.routes.get(self.path.split("?")[0], (404, {"detail": "nope"}, {}))
        if callable(body):
            body = body(self)
        data = json.dumps(body).encode()
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class HTTPTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}/portal/api/"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        Handler.routes, Handler.seen = {}, []

    def client(self):
        return api.Client(self.base, "secret")

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        env = {"SIMPLEVM_API_KEY": "secret", "SIMPLEVM_ENDPOINT": self.base, "SIMPLEVM_PROJECT_ID": "7"}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = 0
            try:
                api.main(list(argv))
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
                err.write(str(e.code) if not isinstance(e.code, int) else "")
        return code, out.getvalue(), err.getvalue()

    def test_sends_key_and_follows_pagination(self):
        def page2(_):
            return {"next": None, "results": [{"uuid": "b"}]}
        Handler.routes = {
            "/portal/api/vms/": (200, {"next": self.base + "vms/p2/?page=2", "results": [{"uuid": "a"}]}, {}),
            "/portal/api/vms/p2/": (200, page2, {}),
        }
        items = self.client().list_all("vms/")
        self.assertEqual([i["uuid"] for i in items], ["a", "b"])
        self.assertTrue(all(k == "secret" for _, k in Handler.seen))
        self.assertIn("page_size=100", Handler.seen[0][0])

    def test_bare_list(self):
        Handler.routes = {"/portal/api/vms/": (200, [{"uuid": "a"}], {})}
        self.assertEqual(len(self.client().list_all("vms/")), 1)

    def test_next_to_foreign_host_is_refused(self):
        Handler.routes = {"/portal/api/vms/": (200, {"next": "https://evil.example/portal/api/vms/", "results": []}, {})}
        with self.assertRaises(SystemExit) as cm:
            self.client().list_all("vms/")
        self.assertIn("another host", str(cm.exception))

    def test_redirect_is_not_followed(self):
        Handler.routes = {"/portal/api/vms": (301, {}, {"Location": "https://evil.example/"})}
        with self.assertRaises(api.APIError) as cm:
            self.client().get("vms")
        self.assertEqual(cm.exception.status, 301)
        self.assertEqual(len(Handler.seen), 1)

    def test_resenv_command_reports_problem(self):
        bad = {**GOOD_VM, "task_state": "building_playbook", "playbook": [{"id": "3", "done": False}]}
        Handler.routes = {
            "/portal/api/vms/by-api-key/": (200, [bad], {}),
            "/portal/api/projects/7/images/": (200, [{"name": "RStudio", "resenv_template_name": "rstudio"}], {}),
            "/portal/api/projects/7/snapshots/images/": (200, [], {}),
        }
        code, out, _ = self.run_cli("resenv", "rstudio")
        self.assertEqual(code, 0)
        self.assertIn("WAITING", out)
        self.assertIn("'rstudio'", out)
        self.assertIn("playbook[0].id: expected integer", out)

    def test_check_exits_nonzero_on_problems(self):
        Handler.routes = {
            "/portal/api/projects/by-api-key/": (200, {"id": 7}, {}),
            "/portal/api/projects/": (200, [{"id": 7}], {}),
            "/portal/api/projects/7/flavors/": (200, [{"name": "f", "ram": "8GB"}], {}),
            "/portal/api/projects/7/images/": (200, [], {}),
            "/portal/api/projects/7/snapshots/images/": (403, {"detail": "scope"}, {}),
            "/portal/api/vms/by-api-key/": (200, [GOOD_VM], {}),
            "/portal/api/volumes/by-api-key/": (200, [], {}),
        }
        code, out, err = self.run_cli("check")
        self.assertEqual(code, 1)
        self.assertIn("ram: expected integer", out)
        self.assertIn("HTTP 403", out)

    def test_get_prints_error_body_and_fails(self):
        Handler.routes = {"/portal/api/vms/": (403, {"detail": "missing scope vm:read"}, {})}
        code, out, err = self.run_cli("get", "vms/")
        self.assertEqual(code, 1)
        self.assertIn("missing scope", out)


if __name__ == "__main__":
    unittest.main()
