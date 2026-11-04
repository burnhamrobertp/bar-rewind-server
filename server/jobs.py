# Jobs: one per bar-rts.com replay, queued by a signed-in user and parsed by a worker. Parsed replays
# and their logs live in R2; a job expires a day before R2's lifecycle rule deletes its files.
import json, re, time, urllib.parse

from . import auth, db, settings, storage
from .web import respond, text

REPLAY_API = "https://api.bar-rts.com/replays/"
REPLAY_ID = re.compile(r"\b([0-9a-f]{32})\b")
LINK_SECONDS = 3600
LOG_TAIL = 256 * 1024
PUBLIC = ("id", "status", "stage", "pct", "created", "started", "finished", "heartbeat", "progress_at", "user", "map",
          "duration_ms", "players", "size", "packed_size", "error", "attempts", "start_time", "event_count")


def curves_version():
    """The .curves format the pinned parser writes: jobs parsed in an older one are parsed again"""
    pinned = db.one("SELECT value FROM pins WHERE key = 'curves_version'")

    return int(pinned["value"]) if pinned and pinned["value"].isdigit() else settings.number("curves-version", 0)


def outdated(job, version=None):
    return job["status"] == "done" and (job["curves_version"] or 0) < (curves_version() if version is None else version)


def stored(job):
    return bool(job["curves_key"]) and storage.store.size(job["curves_key"]) is not None


def public(job, version=None):
    fields = {k: job[k] for k in PUBLIC}
    fields["players"] = json.loads(job["players"])

    return {**fields, "outdated": outdated(job, version), "has_log": bool(job["log_key"]),
            "events": json.loads(job["events"]) if job["events"] else []}


def list_jobs(req, user):
    from . import workers
    jobs = db.rows("SELECT * FROM jobs ORDER BY created DESC")
    version = curves_version()
    names = {w["id"]: w["name"] for w in db.rows("SELECT id, name FROM workers")}
    ratio = workers.memory_ratio()
    admin = auth.is_admin(user)
    listed = []
    for j in jobs:
        shown = public(j, version)
        shown["cancellable"] = j["status"] in ("queued", "running") and (j["uid"] == user["uid"] or admin)
        if j["status"] == "running":
            shown["worker"] = names.get(j["worker"])
        if j["status"] == "queued":
            shown["needs_mb"] = workers.estimate_mb(j, ratio)
        listed.append(shown)

    return respond(200, {"jobs": listed})


def cancel(req, user, replay_id):
    """Takes a job out of the queue; a worker parsing it hears so at its next report and stops"""
    with db.tx() as c:
        job = c.execute("SELECT uid, status FROM jobs WHERE id = ?", (replay_id,)).fetchone()
        if job is None:
            return respond(404, {"error": "no such job"})
        if job["uid"] != user["uid"] and not auth.is_admin(user):
            return respond(409, {"error": "only whoever queued it, or an admin, can cancel it"})
        if job["status"] not in ("queued", "running"):
            return respond(409, {"error": "it already finished"})
        c.execute("DELETE FROM jobs WHERE id = ?", (replay_id,))

    return respond(200, {})


def submit(req, user):
    m = REPLAY_ID.search(str(req.json().get("replay", "")).lower())
    if not m:
        return respond(400, {"error": "paste a bar-rts.com replay link or its 32-character id"})
    replay_id = m.group(1)

    existing = db.one("SELECT * FROM jobs WHERE id = ?", replay_id)
    if existing and existing["status"] != "failed" and not (existing["status"] == "done" and (outdated(existing) or not stored(existing))):
        return respond(200, {"job": public(existing)})
    now = int(time.time())
    if db.one("SELECT count(*) AS n FROM jobs WHERE status IN ('queued', 'running')")["n"] >= settings.number("queue-limit", 50):
        return respond(429, {"error": "the queue is full: try again later"})

    status, info = auth.http(REPLAY_API + replay_id)
    if status != 200 or not isinstance(info, dict) or not info.get("fileName"):
        return respond(404, {"error": "bar-rts.com has no replay " + replay_id})
    max_minutes = settings.number("max-minutes", 150)
    if int(info.get("durationMs") or 0) > max_minutes * 60000:
        return respond(400, {"error": f"replays over {max_minutes} minutes are not parsed here"})
    players = [p["name"] for team in info.get("AllyTeams") or [] for p in team.get("Players") or []]
    job = {"id": replay_id, "status": "queued", "stage": "waiting for a worker", "pct": 0, "created": now,
           "expires": now + (settings.number("retention-days", 60) - 1) * 86400, "uid": user["uid"], "user": user["name"],
           "map": (info.get("Map") or {}).get("scriptName", ""), "file_name": info["fileName"],
           "duration_ms": int(info.get("durationMs") or 0), "players": players[:64], "start_time": info.get("startTime", "")}
    with db.tx() as c:
        if c.execute("SELECT 1 FROM jobs WHERE id = ? AND status IN ('queued', 'running')", (replay_id,)).fetchone():
            return respond(200, {"job": public(db.one("SELECT * FROM jobs WHERE id = ?", replay_id))})
        c.execute("DELETE FROM jobs WHERE id = ?", (replay_id,))
        db.insert(c, "jobs", job)

    return respond(202, {"job": public(db.one("SELECT * FROM jobs WHERE id = ?", replay_id))})


def watch(req, user, replay_id):
    job = db.one("SELECT * FROM jobs WHERE id = ?", replay_id)
    if not job or job["status"] != "done":
        return respond(404, {"error": "that replay is not parsed"})
    if outdated(job):
        return respond(409, {"error": "parsed for an older player: queue it again"})
    if not stored(job):
        return respond(404, {"error": "that replay's parse has expired: queue it again"})
    url = storage.store.get_url(job["curves_key"], LINK_SECONDS)

    return respond(200, {"url": url, "link": "barrewind://play?url=" + urllib.parse.quote(url, safe="")})


def job_log(req, user, replay_id):
    """Head and tail of the latest log: the worker's account comes first, failures show at the end"""
    job = db.one("SELECT * FROM jobs WHERE id = ?", replay_id)
    size = storage.store.size(job["log_key"]) if job and job["log_key"] else None
    if size is None:
        return respond(404, {"error": "no log for that replay yet"})
    if size > 2 * LOG_TAIL:
        head = storage.store.read(job["log_key"], 0, LOG_TAIL // 4 - 1)
        tail = storage.store.read(job["log_key"], size - LOG_TAIL)
        data = head + b"\n\n[... %d bytes left out ...]\n\n" % (size - LOG_TAIL - LOG_TAIL // 4) + tail
    else:
        data = storage.store.read(job["log_key"])

    return text(200, data.decode(errors="replace"))


def expire(now):
    with db.tx() as c:
        c.execute("DELETE FROM jobs WHERE expires < ?", (now,))
