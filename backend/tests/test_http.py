import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from backend.server import Server
from backend.service import Manager
from backend.tests.test_service import FakePlatform, finished_runner


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = Manager(Path(self.temp.name), platform_factory=FakePlatform, runner=finished_runner)
        self.server = Server(("127.0.0.1", 0), self.manager)
        self.port = self.server.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cookie = self.csrf = ""

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.manager.close()
        self.temp.cleanup()

    def request(self, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        h = {"Cookie": self.cookie, "Origin": self.origin, "X-Requested-With": "learning-web", "X-CSRF-Token": self.csrf}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        conn.request("POST" if body is not None else "GET", path, body=data, headers=h)
        response = conn.getresponse()
        raw = response.read()
        info = dict(response.getheaders())
        value = json.loads(raw) if "application/json" in info.get("Content-Type", "") else raw.decode()
        result = (response.status, value, info)
        conn.close()
        return result

    def login(self):
        status, body, headers = self.request("/api/login", {"cookie": "unit-test-credential"})
        self.assertEqual(status, 200)
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.csrf = body["csrf"]
        return body, headers

    def test_frontend_headers_and_unknown_path_do_not_expose_files(self):
        status, html, headers = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn('lang="zh-CN"', html)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        for path in ("/cookie.txt", "/../cookie.txt", "/data/job/summary.json"):
            self.assertEqual(self.request(path)[0], 404)

    def test_authentication_csrf_and_real_frontend_workflow(self):
        self.assertEqual(self.request("/api/session")[0], 401)
        body, headers = self.login()
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        self.assertEqual(len(body["courses"]), 2)
        self.assertEqual(self.request("/api/jobs", {"courseIds":["course-one"]}, {"X-CSRF-Token":"wrong"})[0], 403)
        status, job, _ = self.request("/api/jobs", {"courseIds":["course-one"]})
        self.assertEqual(status, 202)
        session = self.manager.session(self.cookie.split("=",1)[1])
        self.manager.job(session, job["id"]).thread.join(2)
        status, state, _ = self.request("/api/jobs/" + job["id"])
        self.assertEqual(state["status"], "completed")
        self.assertEqual(self.request("/api/session")[1]["jobs"][0]["id"], job["id"])
        self.assertEqual(self.request("/api/logout", {})[0], 200)
        self.assertEqual(self.request("/api/session")[0], 401)

    def test_cross_origin_and_dns_rebinding_are_rejected(self):
        for header in ({"Origin":"https://evil.example"},{"Host":"evil.example"},{"X-Requested-With":""}):
            self.assertEqual(self.request("/api/login", {"cookie":"test"}, header)[0], 403)

    def test_http_data_limits_and_cookie_not_returned(self):
        self.assertEqual(self.request("/api/login", {"cookie":"x"*20000})[0], 400)
        self.assertEqual(self.request("/api/login", {"cookie":"x"*25000})[0], 413)
        body, _ = self.login()
        self.assertNotIn("unit-test-credential", json.dumps(body))
        self.assertNotIn("unit-test-credential", json.dumps(self.request("/api/session")[1]))

    def test_job_id_is_scoped_to_session_over_http(self):
        self.login()
        _, job, _ = self.request("/api/jobs", {"courseIds":["course-one"]})
        self.cookie = self.csrf = ""
        self.login()
        self.assertEqual(self.request("/api/jobs/"+job["id"])[0], 404)

    def test_course_to_task_selection_api_and_scan_ownership(self):
        self.login()
        self.assertFalse(self.request("/api/courses/course-one/tasks")[1]["ready"])
        self.assertEqual(self.request("/api/courses/not-owned/tasks/scan", {})[0], 404)
        status, scan, _ = self.request("/api/courses/course-one/tasks/scan", {})
        self.assertEqual(status, 202)
        s = self.manager.session(self.cookie.split("=", 1)[1])
        self.manager.job(s, scan["id"]).thread.join(2)
        status, catalog, _ = self.request("/api/courses/course-one/tasks")
        self.assertEqual(status, 200)
        self.assertTrue(catalog["ready"])
        self.assertEqual(len(catalog["tasks"]), 1)
        self.assertTrue(catalog["tasks"][0]["selectable"])
        for selection in ([], ["unknown-task"], None):
            self.assertIn(self.request("/api/jobs", {"courseIds":["course-one"], "taskIds":selection})[0], (400, 409))
        status, job, _ = self.request("/api/jobs", {"courseIds":["course-one"], "taskIds":[catalog["tasks"][0]["id"]]})
        self.assertEqual(status, 202)
        self.manager.job(s, job["id"]).thread.join(2)
        self.assertEqual(self.request("/api/jobs/"+job["id"])[1]["selectedTaskCount"], 1)

    def test_outline_to_chapter_queue_over_http(self):
        self.login()
        s = self.manager.session(self.cookie.split("=", 1)[1])
        status, job, _ = self.request("/api/courses/course-one/tasks/scan", {"scope": "outline"})
        self.assertEqual(status, 202)
        self.manager.job(s, job["id"]).thread.join(2)
        catalog = self.request("/api/courses/course-one/tasks")[1]
        self.assertEqual(catalog["tasks"], [])
        self.assertEqual(len(catalog["chapters"]), 1)
        for ids in (None, [], ["foreign"]):
            self.assertIn(self.request("/api/jobs", {"courseIds": ["course-one"], "chapterIds": ids})[0], (400, 409))
        status, job, _ = self.request("/api/jobs", {"courseIds": ["course-one"], "chapterIds": ["chapter-one"], "quizPolicy": "random_submit"})
        self.assertEqual(status, 202)
        self.manager.job(s, job["id"]).thread.join(2)
        state = self.request("/api/jobs/" + job["id"])[1]
        self.assertEqual(state["kind"], "learning")
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["selectedChapterCount"], 1)

    def test_switching_courses_and_loading_one_chapter_over_http(self):
        self.login()
        s = self.manager.session(self.cookie.split("=", 1)[1])
        for course in ("course-one", "course-two"):
            status, job, _ = self.request(f"/api/courses/{course}/tasks/scan", {"scope":"initial"})
            self.assertEqual(status, 202)
            self.manager.job(s, job["id"]).thread.join(2)
            self.assertEqual(self.request("/api/jobs/" + job["id"])[1]["status"], "completed")
        original = self.request("/api/courses/course-one/tasks")[1]
        status, job, _ = self.request("/api/courses/course-one/tasks/scan", {"scope":"chapters", "chapterIds":["chapter-one"]})
        self.assertEqual(status, 202)
        self.manager.job(s, job["id"]).thread.join(2)
        self.assertEqual(self.request("/api/courses/course-one/tasks")[1]["tasks"], original["tasks"])
        self.assertEqual(self.request("/api/courses/course-one/tasks/scan", {"scope":"chapters", "chapterIds":["foreign"]})[0], 409)
        status, health, _ = self.request("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["version"], self.server.source_version)


if __name__ == "__main__":
    unittest.main()
