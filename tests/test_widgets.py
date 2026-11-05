import os, sqlite3

from server import db, settings, storage, widgets
from tests.test_workers import REPLAY, WorkerTest

SET = [{"name": "a_widget.lua", "text": "-- one"}, {"name": "b_widget.lua", "text": "-- two"}]


class Widgets(WorkerTest):
    def setUp(self):
        super().setUp()
        settings._cache["admin-ids"] = "1"
        self.auth, self.worker_id, self.admin = self.enroll()

    def upload(self, files=SET, cookie=None):
        return self.call("POST", "/api/widgets", {"files": files}, cookie or self.admin)

    def test_only_admins_see_or_replace_the_set_and_names_are_checked(self):
        other = self.session("2", "Bea", "tok-2")
        self.assertEqual(403, self.call("GET", "/api/widgets", cookie=other).status)
        self.assertEqual(403, self.upload(cookie=other).status)
        self.assertEqual(400, self.upload([{"name": "../x.lua", "text": ""}]).status)
        self.assertEqual(400, self.upload([{"name": "x.txt", "text": ""}]).status)
        self.assertEqual(400, self.upload([{"name": "..lua", "text": ""}]).status)
        self.assertEqual(400, self.upload([SET[0], SET[0]]).status)
        listed = self.upload().json()
        self.assertEqual(["a_widget.lua", "b_widget.lua"], [f["name"] for f in listed["files"]])
        self.assertNotIn("text", listed["files"][0])

    def test_the_set_stays_out_of_the_database_so_its_backup_never_has_it(self):
        self.upload([{"name": "a_widget.lua", "text": "-- kept off the backup"}])
        with open(db._path, "rb") as f:
            self.assertNotIn(b"kept off the backup", f.read())
        self.assertEqual(["a_widget.lua"], [p.name for p in widgets.folder().iterdir()])
        self.call("DELETE", "/api/widgets/a_widget.lua", cookie=self.admin)
        self.assertEqual([], list(widgets.folder().iterdir()))
        self.assertEqual([], [p.name for p in widgets.folder().parent.iterdir() if p.name.startswith(".widgets")])

    def test_adding_keeps_the_rest_and_replaces_one_of_the_same_name_and_removing_takes_one(self):
        self.upload([SET[0]])
        self.upload([SET[1], {"name": "a_widget.lua", "text": "-- newer"}])
        self.assertEqual({"a_widget.lua": "-- newer", "b_widget.lua": "-- two"}, {f["name"]: f["body"] for f in widgets.files()})
        listed = self.call("DELETE", "/api/widgets/a_widget.lua", cookie=self.admin).json()
        self.assertEqual(["b_widget.lua"], [f["name"] for f in listed["files"]])
        self.assertEqual(403, self.call("DELETE", "/api/widgets/b_widget.lua", cookie=self.session("2", "Bea", "tok-2")).status)

    def test_a_worker_gets_the_set_only_with_its_token(self):
        self.upload()
        self.assertEqual(401, self.worker_call({}, "/api/worker/widgets").status)
        got = self.worker_call(self.auth, "/api/worker/widgets").json()
        self.assertEqual(SET, got["files"])
        self.assertEqual(widgets.digest(), got["hash"])
        self.assertEqual(got["hash"], self.worker_call(self.auth, "/api/worker/heartbeat", {}).json()["widgets"])

    def test_no_jobs_without_the_current_set(self):
        self.queue()
        self.upload()
        self.assertEqual(204, self.worker_call(self.auth, "/api/worker/claim").status)
        self.worker_call(self.auth, "/api/worker/heartbeat", {"widgets": widgets.digest()})
        self.assertEqual(200, self.worker_call(self.auth, "/api/worker/claim").status)

    def test_a_parse_with_another_set_is_missing_widgets_and_can_be_queued_again(self):
        self.upload()
        self.worker_call(self.auth, "/api/worker/heartbeat", {"widgets": widgets.digest()})
        self.queue()
        claim = self.worker_call(self.auth, "/api/worker/claim").json()
        key = db.one("SELECT curves_key FROM jobs")["curves_key"]
        storage.store.objects[key] = b"g" * 10
        self.worker_call(self.auth, f"/api/worker/jobs/{claim['job']['id']}/done", {"size": 20, "packed_size": 10, "curves_version": 11})
        self.assertFalse(self.call("GET", "/api/jobs", cookie=self.admin).json()["jobs"][0]["missing_widgets"])

        self.call("DELETE", "/api/widgets/b_widget.lua", cookie=self.admin)
        self.assertTrue(self.call("GET", "/api/jobs", cookie=self.admin).json()["jobs"][0]["missing_widgets"])
        self.outside.replay(REPLAY)
        self.assertEqual("queued", self.call("POST", "/api/jobs", {"replay": REPLAY}, self.admin).json()["job"]["status"])

    def test_no_set_needs_nothing(self):
        self.queue()
        self.assertEqual("", self.worker_call(self.auth, "/api/worker/heartbeat", {}).json()["widgets"])
        self.assertEqual(200, self.worker_call(self.auth, "/api/worker/claim").status)


class OlderDatabase(WorkerTest):
    def test_columns_added_since_are_added_to_an_existing_database(self):
        path = os.path.join(self.dir.name, "old.db")
        old = sqlite3.connect(path)
        old.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, status TEXT, created INTEGER)")
        old.execute("CREATE TABLE workers (id TEXT PRIMARY KEY)")
        old.commit()
        old.close()
        db.conn().close()
        db.init(path)
        self.assertIn("widgets", {r["name"] for r in db.conn().execute("PRAGMA table_info(jobs)")})
        self.assertIn("widgets", {r["name"] for r in db.conn().execute("PRAGMA table_info(workers)")})
