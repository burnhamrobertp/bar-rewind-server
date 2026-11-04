# A server with a temporary database, a stand-in bucket and stand-ins for Discord and bar-rts.com.
import json, os, tempfile, time, unittest, urllib.parse

from server import app, auth, db, settings, storage
from server.web import Request

GUILD = "100"
ROLE = "200"


class FakeStore:
    def __init__(self):
        self.objects = {}

    def get_url(self, key, seconds):
        return f"https://r2.example/bucket/{key}?X-Amz-Expires={seconds}&X-Amz-Signature=abc"

    def put_url(self, key, seconds, headers):
        return f"https://r2.example/bucket/{key}?X-Amz-Expires={seconds}&put=1"

    def size(self, key):
        return len(self.objects[key]) if key in self.objects else None

    def read(self, key):
        return self.objects[key]

    def delete(self, key):
        self.objects.pop(key, None)


class Outside:
    """What Discord and bar-rts.com answer: members by access token, replays by id"""

    def __init__(self):
        self.members = {}
        self.replays = {}
        self.calls = []
        self.member_status = None

    def member(self, token, uid, name="Mod", roles=(ROLE,)):
        self.members[token] = {"user": {"id": uid, "username": name.lower()}, "nick": name, "roles": list(roles)}

    def replay(self, replay_id, minutes=20, players=("a", "b")):
        self.replays[replay_id] = {"fileName": f"{replay_id}.sdfz", "durationMs": minutes * 60000, "startTime": "2026-11-01T00:00:00Z",
                                   "Map": {"scriptName": "Supreme Isthmus v2.1"},
                                   "AllyTeams": [{"Players": [{"name": p}]} for p in players]}

    def http(self, url, data=None, headers=None):
        self.calls.append(url)
        if url.endswith("/oauth2/token"):
            form = dict(urllib.parse.parse_qsl(data.decode()))
            if form.get("code") == "good":
                return 200, {"access_token": "tok-good", "refresh_token": "ref", "expires_in": 3600}
            if form.get("grant_type") == "refresh_token":
                return 200, {"access_token": "tok-" + form["refresh_token"], "refresh_token": form["refresh_token"] + "+", "expires_in": 3600}
            return 400, None
        if "/users/@me/guilds/" in url:
            if self.member_status:
                return self.member_status, None
            member = self.members.get((headers or {}).get("Authorization", "").removeprefix("Bearer "))
            return (200, member) if member else (404, None)
        if url.startswith("https://api.bar-rts.com/replays/"):
            info = self.replays.get(url.rsplit("/", 1)[-1])
            return (200, info) if info else (404, None)
        raise AssertionError("unexpected request " + url)


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        settings._cache = {"session-key": "k" * 32, "site-url": "https://rewind.example", "guild-id": GUILD, "role-ids": ROLE,
                           "user-ids": "", "admin-ids": "", "discord-client-id": "cid", "discord-client-secret": "secret",
                           "curves-version": "11"}
        db.init(os.path.join(self.dir.name, "test.db"))
        storage.store = FakeStore()
        self.outside = Outside()
        self.real_http = auth.http
        auth.http = self.outside.http

    def tearDown(self):
        auth.http = self.real_http
        db.conn().close()
        db._local.conn = None
        self.dir.cleanup()

    def session(self, uid="1", name="Mod", token="tok-1", checked=None):
        now = int(time.time())
        self.outside.member(token, uid, name)
        payload = {"uid": uid, "name": name, "tok": token, "chk": now if checked is None else checked, "exp": now + 3600}

        return "s=" + auth.sign(payload)

    def call(self, method, path, body=None, cookie="", query=None, headers=None):
        raw = json.dumps(body).encode() if body is not None else b""

        return app.route(Request(method, path, query or {}, {"cookie": cookie, **(headers or {})}, raw))
