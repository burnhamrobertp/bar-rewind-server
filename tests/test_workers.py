import json, time, uuid

from server import db, settings, storage, workers
from tests.helpers import ServerTest

REPLAY = "0123456789abcdef0123456789abcdef"
DESK = "3f2a9c1e-0b7d-4e5a-9c61-2d8f0a4b7e13"


class WorkerTest(ServerTest):
    def enroll(self, mem_mb=32768, owner="1"):
        start = self.call("POST", "/api/worker/enroll", {"id": str(uuid.uuid4()), "mem_mb": mem_mb, "cpus": 8, "os": "windows"}).json()
        cookie = self.session(uid=owner, token=f"tok-{owner}")
        self.assertEqual(200, self.call("POST", "/api/link", {"code": start["user_code"]}, cookie).status)
        got = self.call("POST", "/api/worker/enroll/poll", {"device_code": start["device_code"]}).json()

        return {"authorization": "Bearer " + got["token"]}, got["worker_id"], cookie

    def worker_call(self, auth, path, body=None):
        return self.call("POST", path, body or {}, headers=auth)

    def queue(self, replay_id=REPLAY, minutes=20, players=("a", "b"), created=None, **fields):
        with db.tx() as c:
            db.insert(c, "jobs", {"id": replay_id, "status": "queued", "created": created or int(time.time()), "expires": 2**40, "uid": "1",
                                  "user": "Mod", "file_name": f"{replay_id} Map.sdfz", "duration_ms": minutes * 60000,
                                  "players": json.dumps(list(players)), **fields})


class Enrollment(WorkerTest):
    def test_a_worker_gets_its_token_once_its_owner_approves(self):
        start = self.call("POST", "/api/worker/enroll", {"id": DESK.upper(), "mem_mb": 16384}).json()
        self.assertTrue(start["verify_url"].startswith("https://rewind.example/link.html?code="))
        self.assertEqual(202, self.call("POST", "/api/worker/enroll/poll", {"device_code": start["device_code"]}).status)
        cookie = self.session()
        info = self.call("GET", "/api/link/" + start["user_code"].lower(), cookie=cookie).json()
        self.assertEqual((DESK, 16384), (info["id"], info["mem_mb"]))
        self.call("POST", "/api/link", {"code": start["user_code"]}, cookie)

        got = self.call("POST", "/api/worker/enroll/poll", {"device_code": start["device_code"]})
        self.assertEqual(200, got.status)
        self.assertEqual(410, self.call("POST", "/api/worker/enroll/poll", {"device_code": start["device_code"]}).status)
        worker = db.one("SELECT * FROM workers")
        self.assertEqual(("1", "Mod", 16384), (worker["owner_uid"], worker["owner_name"], worker["mem_mb"]))
        self.assertNotIn(got.json()["token"], json.dumps(worker))
        self.assertEqual(DESK, got.json()["worker_id"])

    def test_a_worker_picks_a_uuid_nobody_else_has(self):
        self.assertEqual(400, self.call("POST", "/api/worker/enroll", {"id": "desk"}).status)
        _, worker_id, _ = self.enroll()
        self.assertEqual(409, self.call("POST", "/api/worker/enroll", {"id": worker_id}).status)

    def test_approving_needs_a_session_and_a_live_code(self):
        start = self.call("POST", "/api/worker/enroll", {"id": DESK}).json()
        self.assertEqual(401, self.call("POST", "/api/link", {"code": start["user_code"]}).status)
        self.assertEqual(404, self.call("POST", "/api/link", {"code": "BBBB-BBBB"}, self.session()).status)
        db.conn().execute("UPDATE enrollments SET expires = 0")
        self.assertEqual(404, self.call("POST", "/api/link", {"code": start["user_code"]}, self.session()).status)

    def test_only_so_many_wait_at_once(self):
        for _ in range(workers.PENDING_ENROLLMENTS):
            self.call("POST", "/api/worker/enroll", {"id": str(uuid.uuid4())})
        self.assertEqual(429, self.call("POST", "/api/worker/enroll", {}).status)

    def test_an_unknown_token_is_refused(self):
        self.assertEqual(401, self.worker_call({"authorization": "Bearer nope"}, "/api/worker/heartbeat").status)
        self.assertEqual(401, self.worker_call({}, "/api/worker/claim").status)


class Heartbeat(WorkerTest):
    def test_it_records_the_machine_and_hands_back_settings_and_pinned_releases(self):
        auth, worker_id, cookie = self.enroll()
        settings._cache["admin-ids"] = "1"
        self.call("POST", "/api/pins", {"parser": "v0.5.0", "worker": "v0.1.0", "curves_version": "12"}, cookie)
        r = self.worker_call(auth, "/api/worker/heartbeat", {"mem_mb": 65536, "disk_free_mb": 9000, "version": "v0.1.0", "mem_free_mb": "lots"}).json()
        self.assertEqual("https://github.com/burnhamrobertp/BAR-Rewind/releases/download/v0.5.0/manifest.json", r["releases"]["parser"]["manifest"])
        self.assertEqual("v0.1.0", r["releases"]["worker"]["version"])
        worker = db.one("SELECT * FROM workers")
        self.assertEqual((65536, 9000, None), (worker["mem_mb"], worker["disk_free_mb"], worker["mem_free_mb"]))

    def test_only_admins_pin_releases_and_a_curves_version_is_a_number(self):
        _, _, cookie = self.enroll()
        self.assertEqual(403, self.call("POST", "/api/pins", {"parser": "v9"}, cookie).status)
        settings._cache["admin-ids"] = "1"
        self.assertEqual(400, self.call("POST", "/api/pins", {"curves_version": "v12"}, cookie).status)
        self.assertEqual(200, self.call("POST", "/api/pins", {"curves_version": "12"}, cookie).status)


class Claim(WorkerTest):
    def test_a_worker_gets_the_oldest_job_with_its_demo_and_upload_urls(self):
        auth, worker_id, _ = self.enroll()
        self.assertEqual(204, self.worker_call(auth, "/api/worker/claim").status)
        self.queue("b" * 32, created=200)
        self.queue("a" * 32, created=100)
        r = self.worker_call(auth, "/api/worker/claim").json()
        self.assertEqual("a" * 32, r["job"]["id"])
        self.assertTrue(r["job"]["demo_url"].endswith("/BAR/demos/" + "a" * 32 + "%20Map.sdfz"))
        self.assertEqual("gzip", r["uploads"]["curves"]["headers"]["Content-Encoding"])
        job = db.one("SELECT * FROM jobs WHERE id = ?", "a" * 32)
        self.assertEqual(("running", worker_id, 1), (job["status"], job["worker"], job["attempts"]))

    def test_paused_workers_and_too_long_replays(self):
        auth, worker_id, cookie = self.enroll()
        self.queue(minutes=90)
        self.call("POST", f"/api/workers/{worker_id}", {"max_minutes": 60}, cookie)
        self.assertEqual(204, self.worker_call(auth, "/api/worker/claim").status)
        self.call("POST", f"/api/workers/{worker_id}", {"max_minutes": 120, "paused": True}, cookie)
        self.assertEqual(204, self.worker_call(auth, "/api/worker/claim").status)
        self.call("POST", f"/api/workers/{worker_id}", {"paused": False}, cookie)
        self.assertEqual(200, self.worker_call(auth, "/api/worker/claim").status)

    def test_a_job_a_worker_died_on_waits_for_a_bigger_one(self):
        small, _, _ = self.enroll(mem_mb=16384)
        bigger, _, _ = self.enroll(mem_mb=32768, owner="2")
        now = int(time.time())
        self.queue(too_small_mb=16384, heartbeat=now - 60)
        self.assertEqual(204, self.worker_call(small, "/api/worker/claim").status)
        self.assertEqual(200, self.worker_call(bigger, "/api/worker/claim").status)

        db.conn().execute("UPDATE jobs SET status = 'queued', worker = NULL, heartbeat = ?", (now - workers.BIGGER_WORKER_WAIT - 1,))
        self.assertEqual(200, self.worker_call(small, "/api/worker/claim").status)

    def test_memory_estimates_start_after_five_parses(self):
        auth, _, _ = self.enroll(mem_mb=8000)
        big, _, _ = self.enroll(mem_mb=65536, owner="2")
        self.enroll(mem_mb=131072, owner="3")
        db.conn().execute("UPDATE workers SET last_seen = ? WHERE mem_mb = 131072", (int(time.time()),))
        for i in range(4):
            self.queue(f"{i:032x}", status="done", peak_mem_mb=20000, finished=i)
        self.assertIsNone(workers.mem_per_size(db.rows("SELECT * FROM jobs WHERE status = 'done'")))
        self.queue("4" * 32, status="done", peak_mem_mb=20000, finished=4)
        ratio = workers.mem_per_size(db.rows("SELECT * FROM jobs WHERE status = 'done'"))
        self.queue()
        self.assertEqual(20000 * 1.25, workers.estimate_mb(db.one("SELECT * FROM jobs WHERE id = ?", REPLAY), ratio))
        self.assertEqual(204, self.worker_call(auth, "/api/worker/claim").status)
        self.assertEqual(200, self.worker_call(big, "/api/worker/claim").status)

    def test_a_job_too_big_for_every_estimate_still_goes_to_the_biggest_machine_online(self):
        auth, _, _ = self.enroll(mem_mb=8000)
        self.enroll(mem_mb=65536, owner="2")
        db.conn().execute("UPDATE workers SET last_seen = ? WHERE mem_mb = 65536", (int(time.time()) - workers.STALE - 1,))
        for i in range(5):
            self.queue(f"{i:032x}", status="done", peak_mem_mb=900000, finished=i)
        self.queue()
        self.assertEqual(200, self.worker_call(auth, "/api/worker/claim").status)

    def test_one_job_at_a_time_and_only_on_the_pinned_parser(self):
        auth, _, cookie = self.enroll()
        self.queue("a" * 32)
        self.queue("b" * 32)
        self.assertEqual(200, self.worker_call(auth, "/api/worker/claim").status)
        self.assertEqual(204, self.worker_call(auth, "/api/worker/claim").status)
        settings._cache["admin-ids"] = "1"
        self.call("POST", "/api/pins", {"parser": "v0.5.0"}, cookie)
        other, _, _ = self.enroll(owner="2")
        self.assertEqual(204, self.worker_call(other, "/api/worker/claim").status)
        self.worker_call(other, "/api/worker/heartbeat", {"parser_version": "v0.5.0"})
        self.assertEqual(200, self.worker_call(other, "/api/worker/claim").status)


class Reports(WorkerTest):
    def claimed(self):
        auth, worker_id, cookie = self.enroll()
        self.queue()
        self.worker_call(auth, "/api/worker/claim")
        job = db.one("SELECT * FROM jobs")

        return auth, worker_id, cookie, job["curves_key"], job["log_key"]

    def test_progress_is_the_jobs_heartbeat_and_only_its_worker_reports(self):
        auth, _, _, _, _ = self.claimed()
        self.assertEqual(200, self.worker_call(auth, f"/api/worker/jobs/{REPLAY}/progress", {"stage": "re-simulating", "pct": 140, "peak_mem_mb": 900}).status)
        job = db.one("SELECT * FROM jobs")
        self.assertEqual(("re-simulating", 100, 900), (job["stage"], job["pct"], job["peak_mem_mb"]))
        other, _, _ = self.enroll(owner="2")
        self.assertEqual(409, self.worker_call(other, f"/api/worker/jobs/{REPLAY}/progress", {}).status)

    def test_done_needs_the_upload_to_be_there_at_its_size(self):
        auth, worker_id, _, curves_key, log_key = self.claimed()
        body = {"size": 900, "packed_size": 300, "curves_version": 11, "event_count": 2,
                "events": [{"f": 30, "kind": "death", "text": "x" * 1000, "junk": 1}, "not an event"]}
        self.assertEqual(409, self.worker_call(auth, f"/api/worker/jobs/{REPLAY}/done", body).status)
        storage.store.objects[curves_key] = b"g" * 299
        self.assertEqual(409, self.worker_call(auth, f"/api/worker/jobs/{REPLAY}/done", body).status)
        storage.store.objects[curves_key] = b"g" * 300
        storage.store.objects[log_key] = b"log"
        self.assertEqual(200, self.worker_call(auth, f"/api/worker/jobs/{REPLAY}/done", body).status)
        job = db.one("SELECT * FROM jobs")
        self.assertEqual(("done", 300, 11, log_key), (job["status"], job["packed_size"], job["curves_version"], job["log_key"]))
        self.assertEqual([{"f": 30, "kind": "death", "player": "", "text": "x" * 300}], json.loads(job["events"]))
        self.assertEqual(409, self.worker_call(auth, f"/api/worker/jobs/{REPLAY}/done", body).status)

    def test_running_out_of_memory_hands_the_job_to_a_bigger_machine(self):
        auth, worker_id, _, _, _ = self.claimed()
        self.worker_call(auth, f"/api/worker/jobs/{REPLAY}/fail", {"error": "the engine ran out of memory", "out_of_memory": True})
        job = db.one("SELECT * FROM jobs")
        self.assertEqual(("queued", None, 32768), (job["status"], job["worker"], job["too_small_mb"]))
        self.assertEqual(0, db.one("SELECT failures FROM workers")["failures"])
        db.conn().execute("UPDATE jobs SET status = 'running', worker = ?, attempts = ?", (worker_id, workers.MAX_ATTEMPTS))
        self.worker_call(auth, f"/api/worker/jobs/{REPLAY}/fail", {"out_of_memory": True})
        self.assertEqual("failed", db.one("SELECT status FROM jobs")["status"])

    def test_failing_too_often_in_a_row_pauses_the_worker(self):
        auth, worker_id, _, _, _ = self.claimed()
        self.worker_call(auth, f"/api/worker/jobs/{REPLAY}/fail", {"error": "the engine crashed"})
        self.assertEqual(("failed", "the engine crashed", None), tuple(db.one("SELECT status, error, log_key FROM jobs").values()))
        db.conn().execute("UPDATE workers SET failures = ?", (workers.FAILURE_STREAK - 1,))
        db.conn().execute("UPDATE jobs SET status = 'running', worker = ?", (worker_id,))
        self.worker_call(auth, f"/api/worker/jobs/{REPLAY}/fail", {})
        self.assertEqual(1, db.one("SELECT paused FROM workers")["paused"])


class Tidy(WorkerTest):
    def test_a_job_whose_worker_went_quiet_goes_back_in_the_queue_then_fails(self):
        auth, _, _ = self.enroll(mem_mb=16384)
        self.queue()
        self.worker_call(auth, "/api/worker/claim")
        now = int(time.time())
        workers.requeue_stale(now)
        self.assertEqual("running", db.one("SELECT status FROM jobs")["status"])
        workers.requeue_stale(now + workers.STALE + 1)
        job = db.one("SELECT * FROM jobs")
        self.assertEqual(("queued", None, 16384), (job["status"], job["worker"], job["too_small_mb"]))
        db.conn().execute("UPDATE jobs SET status = 'running', attempts = ?, heartbeat = 0", (workers.MAX_ATTEMPTS,))
        workers.requeue_stale(now)
        self.assertEqual("failed", db.one("SELECT status FROM jobs")["status"])
        self.assertEqual(404, self.call("GET", f"/api/jobs/{REPLAY}/log", cookie=self.session()).status)

    def test_owners_who_lose_access_have_their_workers_paused(self):
        for owner in "123":
            self.enroll(owner=owner)
        db.conn().execute("INSERT INTO users (uid, name, refresh_token, checked) VALUES ('1', 'A', 'a', 0), ('2', 'B', NULL, 0), ('3', 'C', 'c', 0)")
        self.outside.member("tok-a", "1")
        self.outside.member("tok-c", "3", roles=())
        workers.recheck_owners(int(time.time()))
        paused = {r["owner_uid"]: r["paused"] for r in db.rows("SELECT owner_uid, paused FROM workers")}
        self.assertEqual({"1": 0, "2": 1, "3": 1}, paused)
        self.assertEqual("a+", db.one("SELECT refresh_token FROM users WHERE uid = '1'")["refresh_token"])

    def test_an_unreachable_discord_pauses_nobody_and_keeps_a_new_refresh_token(self):
        self.enroll()
        db.conn().execute("INSERT INTO users (uid, name, refresh_token, checked) VALUES ('1', 'A', 'a', 0)")
        self.outside.member_status = 503
        workers.recheck_owners(int(time.time()))
        self.assertEqual(0, db.one("SELECT paused FROM workers")["paused"])
        self.assertEqual(("a+", 0), tuple(db.one("SELECT refresh_token, checked FROM users").values()))

    def test_a_pinned_curves_version_marks_older_parses_outdated(self):
        _, _, cookie = self.enroll()
        settings._cache["admin-ids"] = "1"
        self.queue(status="done", curves_version=11, curves_key="curves/x.curves")
        self.assertFalse(self.call("GET", "/api/jobs", cookie=cookie).json()["jobs"][0]["outdated"])
        self.call("POST", "/api/pins", {"curves_version": "12"}, cookie)
        self.assertTrue(self.call("GET", "/api/jobs", cookie=cookie).json()["jobs"][0]["outdated"])

    def test_removing_a_worker_requeues_its_job_and_only_its_owner_or_an_admin_may(self):
        auth, worker_id, cookie = self.enroll()
        self.queue()
        self.worker_call(auth, "/api/worker/claim")
        stranger = self.session(uid="9", token="tok-9")
        self.assertEqual(404, self.call("DELETE", f"/api/workers/{worker_id}", cookie=stranger).status)
        self.assertEqual(200, self.call("DELETE", f"/api/workers/{worker_id}", cookie=cookie).status)
        self.assertEqual(("queued", None), tuple(db.one("SELECT status, worker FROM jobs").values()))
        self.assertEqual(401, self.worker_call(auth, "/api/worker/heartbeat").status)


class Cancel(WorkerTest):
    def test_whoever_queued_it_or_an_admin_can_cancel_a_waiting_job(self):
        self.queue()
        stranger = self.session(uid="9", token="tok-9")
        self.assertEqual(409, self.call("POST", f"/api/jobs/{REPLAY}/cancel", cookie=stranger).status)
        self.assertEqual(200, self.call("POST", f"/api/jobs/{REPLAY}/cancel", cookie=self.session()).status)
        self.assertIsNone(db.one("SELECT id FROM jobs"))
        self.assertEqual(404, self.call("POST", f"/api/jobs/{REPLAY}/cancel", cookie=self.session()).status)
        self.queue()
        settings._cache["admin-ids"] = "9"
        self.assertEqual(200, self.call("POST", f"/api/jobs/{REPLAY}/cancel", cookie=stranger).status)

    def test_a_running_job_stops_on_its_worker_and_a_finished_one_stays(self):
        auth, _, cookie = self.enroll()
        self.queue()
        self.worker_call(auth, "/api/worker/claim")
        self.assertEqual(200, self.call("POST", f"/api/jobs/{REPLAY}/cancel", cookie=cookie).status)
        self.assertEqual(409, self.worker_call(auth, f"/api/worker/jobs/{REPLAY}/progress", {"stage": "x"}).status)
        self.queue(status="done")
        self.assertEqual(409, self.call("POST", f"/api/jobs/{REPLAY}/cancel", cookie=cookie).status)


class Overview(WorkerTest):
    def test_the_list_says_who_parses_what_and_what_waiting_jobs_need(self):
        auth, desk, cookie = self.enroll()
        self.worker_call(auth, "/api/worker/heartbeat", {})
        for i in range(5):
            self.queue(f"{i:032x}", status="done", peak_mem_mb=20000, finished=i)
        self.queue("a" * 32, created=1)
        self.queue("b" * 32, created=2)
        self.worker_call(auth, "/api/worker/claim")
        listed = {j["id"]: j for j in self.call("GET", "/api/jobs", cookie=cookie).json()["jobs"]}
        self.assertEqual((desk, "Mod", True), (listed["a" * 32]["worker"], listed["a" * 32]["worker_owner"], listed["a" * 32]["cancellable"]))
        self.assertEqual(25000, listed["b" * 32]["needs_mb"])
        self.assertFalse(listed["0" * 32]["cancellable"])
        stranger = self.session(uid="9", token="tok-9")
        seen = {j["id"]: j for j in self.call("GET", "/api/jobs", cookie=stranger).json()["jobs"]}
        self.assertFalse(seen["a" * 32]["cancellable"] or seen["b" * 32]["cancellable"])

        o = self.call("GET", "/api/overview", cookie=cookie).json()
        self.assertEqual((1, 1, 1, 1, False), (o["queued"], o["running"], o["workers_online"], o["workers"], o["admin"]))
        self.assertEqual(500, o["mb_per_player_minute"])
