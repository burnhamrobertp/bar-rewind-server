import time, urllib.parse

from server import auth, db, jobs, storage
from tests.helpers import ServerTest

REPLAY = "0123456789abcdef0123456789abcdef"


class SignIn(ServerTest):
    def test_a_member_with_the_role_gets_a_session_and_is_remembered(self):
        self.outside.member("tok-good", "7", "Ada")
        r = self.call("GET", "/api/callback", query={"code": "good", "state": "x"}, cookie="st=x")
        self.assertEqual(302, r.status)
        session = next(c for c in r.cookies if c.startswith("s=")).split(";")[0]
        self.assertEqual({"name": "Ada", "admin": False}, self.call("GET", "/api/me", cookie=session).json())
        self.assertEqual("ref", db.one("SELECT refresh_token FROM users WHERE uid = '7'")["refresh_token"])

    def test_no_role_no_session(self):
        self.outside.member("tok-good", "7", roles=())
        r = self.call("GET", "/api/callback", query={"code": "good", "state": "x"}, cookie="st=x")
        self.assertEqual("/?signin=denied", r.headers["location"])
        self.assertFalse(any(c.startswith("s=") for c in r.cookies))

    def test_a_state_that_does_not_match_its_cookie_fails(self):
        r = self.call("GET", "/api/callback", query={"code": "good", "state": "x"}, cookie="st=y")
        self.assertEqual("/?signin=failed", r.headers["location"])

    def test_a_forged_or_missing_session_is_refused(self):
        self.assertEqual(401, self.call("GET", "/api/me").status)
        self.assertEqual(401, self.call("GET", "/api/me", cookie="s=e30.AAAA").status)
        self.assertEqual(401, self.call("GET", "/api/me", cookie="s=not-base64!!.x").status)

    def test_losing_the_role_ends_access_at_the_next_check(self):
        cookie = self.session(checked=0)
        self.outside.members["tok-1"]["roles"] = []
        r = self.call("GET", "/api/me", cookie=cookie)
        self.assertEqual(403, r.status)
        self.assertIn("s=; Path=/api; Max-Age=0; HttpOnly; Secure; SameSite=Lax", r.cookies)

    def test_discord_being_unreachable_keeps_the_session(self):
        cookie = self.session(checked=0)
        for status in (429, 0):
            auth.http = lambda url, data=None, headers=None: (status, None)
            r = self.call("GET", "/api/me", cookie=cookie)
            self.assertEqual((503, []), (r.status, r.cookies))

    def test_queueing_always_checks_the_role(self):
        cookie = self.session()
        self.outside.members["tok-1"]["roles"] = []
        self.assertEqual(403, self.call("POST", "/api/jobs", {"replay": REPLAY}, cookie).status)


class Queue(ServerTest):
    def test_a_link_queues_its_replay_with_what_bar_rts_knows(self):
        self.outside.replay(REPLAY, minutes=40, players=("x", "y", "z"))
        r = self.call("POST", "/api/jobs", {"replay": f"https://www.bar-rts.com/replays/{REPLAY}"}, self.session())
        self.assertEqual(202, r.status)
        job = r.json()["job"]
        self.assertEqual(("queued", "Supreme Isthmus v2.1", ["x", "y", "z"], 40 * 60000), (job["status"], job["map"], job["players"], job["duration_ms"]))
        self.assertEqual(f"{REPLAY}.sdfz", db.one("SELECT file_name FROM jobs")["file_name"])

    def test_queueing_it_again_returns_the_same_job(self):
        self.outside.replay(REPLAY)
        cookie = self.session()
        self.call("POST", "/api/jobs", {"replay": REPLAY}, cookie)
        r = self.call("POST", "/api/jobs", {"replay": REPLAY}, cookie)
        self.assertEqual(200, r.status)
        self.assertEqual(1, db.one("SELECT count(*) AS n FROM jobs")["n"])

    def test_a_failed_or_outdated_job_is_queued_afresh(self):
        self.outside.replay(REPLAY)
        cookie = self.session()
        self.call("POST", "/api/jobs", {"replay": REPLAY}, cookie)
        storage.store.objects["curves/x.curves"] = b"gz"
        for fields in ({"status": "done", "curves_version": 10, "curves_key": "curves/x.curves"}, {"status": "failed", "error": "boom"}):
            with db.tx() as c:
                db.update(c, "jobs", "id", REPLAY, fields)
            self.assertEqual(202, self.call("POST", "/api/jobs", {"replay": REPLAY}, cookie).status)
            self.assertEqual("queued", db.one("SELECT status FROM jobs")["status"])

    def test_limits(self):
        cookie = self.session()
        self.assertEqual(400, self.call("POST", "/api/jobs", {"replay": "nope"}, cookie).status)
        self.assertEqual(404, self.call("POST", "/api/jobs", {"replay": REPLAY}, cookie).status)
        self.outside.replay(REPLAY, minutes=151)
        self.assertEqual(400, self.call("POST", "/api/jobs", {"replay": REPLAY}, cookie).status)

    def test_listing_is_newest_first(self):
        cookie = self.session()
        for i, created in enumerate((100, 300, 200)):
            with db.tx() as c:
                db.insert(c, "jobs", {"id": f"{i:032x}", "status": "queued", "created": created, "expires": 2**40, "uid": "1", "user": "Mod"})
        listed = self.call("GET", "/api/jobs", cookie=cookie).json()["jobs"]
        self.assertEqual([300, 200, 100], [j["created"] for j in listed])


class Watch(ServerTest):
    def done(self, version=11, stored=True):
        with db.tx() as c:
            db.insert(c, "jobs", {"id": REPLAY, "status": "done", "created": 1, "expires": 2**40, "uid": "1", "user": "Mod",
                                  "curves_version": version, "curves_key": f"curves/{REPLAY}-5.curves", "log_key": "logs/a.log"})
        if stored:
            storage.store.objects[f"curves/{REPLAY}-5.curves"] = b"gz"

    def test_a_parsed_replay_gets_a_link_the_player_can_open(self):
        self.done()
        r = self.call("GET", f"/api/jobs/{REPLAY}/watch", cookie=self.session()).json()
        self.assertEqual(r["url"], urllib.parse.unquote(r["link"].removeprefix("barrewind://play?url=")))
        self.assertNotIn("&", r["link"])

    def test_outdated_expired_or_unparsed(self):
        cookie = self.session()
        self.assertEqual(404, self.call("GET", f"/api/jobs/{REPLAY}/watch", cookie=cookie).status)
        self.done(version=10)
        self.assertEqual(409, self.call("GET", f"/api/jobs/{REPLAY}/watch", cookie=cookie).status)
        db.conn().execute("DELETE FROM jobs")
        storage.store.objects.clear()
        self.done(stored=False)
        self.assertEqual(404, self.call("GET", f"/api/jobs/{REPLAY}/watch", cookie=cookie).status)

    def test_a_long_log_keeps_its_head_and_its_end(self):
        self.done()
        storage.store.objects["logs/a.log"] = b"H" * 100 + b"m" * (1 << 20) + b"the engine said why"
        body = self.call("GET", f"/api/jobs/{REPLAY}/log", cookie=self.session()).body
        self.assertTrue(body.startswith(b"H" * 100))
        self.assertTrue(body.endswith(b"the engine said why"))
        self.assertLess(len(body), 2 * jobs.LOG_TAIL)


class Site(ServerTest):
    def test_pages_come_from_site_and_nothing_outside_it(self):
        r = self.call("GET", "/")
        self.assertEqual((200, "text/html"), (r.status, r.headers["content-type"]))
        self.assertEqual(404, self.call("GET", "/../server/app.py").status)
        self.assertEqual(404, self.call("GET", "/api/nothing", cookie=self.session()).status)

    def test_jobs_past_their_day_are_dropped(self):
        with db.tx() as c:
            db.insert(c, "jobs", {"id": REPLAY, "status": "done", "created": 1, "expires": 50, "uid": "1", "user": "Mod"})
        jobs.expire(int(time.time()))
        self.assertIsNone(db.one("SELECT id FROM jobs"))
