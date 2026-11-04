# Workers: machines that moderators run, which parse queued replays. A worker enrolls once (it shows a
# code, its owner approves it on the site), then talks to the API with its own token:
#   POST /api/worker/enroll {id, mem_mb, cpus, os}     a device code to poll with, and a code to approve
#   POST /api/worker/enroll/poll {device_code}         202 until approved, then its id and token, once
#   POST /api/worker/heartbeat {mem_mb, ...}           what it has; back come its settings and the releases to run
#   POST /api/worker/claim                             204, or a job with where to fetch its demo and upload to
#   POST /api/worker/jobs/<id>/progress {stage, pct, peak_mem_mb}   also the job's heartbeat; 409 once it isn't theirs
#   POST /api/worker/jobs/<id>/uploads                 fresh upload URLs, for an upload that outlives the first
#   POST /api/worker/jobs/<id>/done {size, packed_size, curves_version, event_count, events, peak_mem_mb}
#   POST /api/worker/jobs/<id>/fail {error, peak_mem_mb, out_of_memory}
# and for signed-in users:
#   GET  /api/link/<code>, POST /api/link {code}       what is asking to enroll, and approving it
#   GET  /api/workers                                  their workers (an admin's: everyone's)
#   POST /api/workers/<id> {paused, max_minutes}, DELETE /api/workers/<id>
#   GET, POST /api/pins {parser, worker, curves_version}   (admins) the releases workers run
#   GET  /api/overview                                 queue and worker counts, the pins, the memory estimate
import hashlib, json, math, re, secrets, time, urllib.parse

from . import auth, db, jobs, settings, storage
from .web import respond

DEMOS = "https://storage.uk.cloud.ovh.net/v1/AUTH_10286efc0d334efd917d476d7183232e/BAR/demos/"
PARSER_RELEASES = "https://github.com/burnhamrobertp/BAR-Rewind/releases/download/"
WORKER_RELEASES = "https://github.com/burnhamrobertp/bar-rewind-worker/releases/download/"
WORKER_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
CODE_LETTERS = "BCDFGHJKLMNPQRSTVWXZ"
ENROLL_SECONDS = 15 * 60
PENDING_ENROLLMENTS = 50
POLL_SECONDS = 15
UPLOAD_SECONDS = 3 * 3600
STALE = 300
MAX_ATTEMPTS = 3
FAILURE_STREAK = 5
BIGGER_WORKER_WAIT = 1800
OWNER_RECHECK = 6 * 3600
EVENTS_KEPT = 200


def digest(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


def number(body, key, low=0, high=1 << 40):
    value = body.get(key)

    return min(max(int(value), low), high) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def short(body, key, n=200):
    return str(body.get(key) or "")[:n]


def enroll(req):
    now = int(time.time())
    if db.one("SELECT count(*) AS n FROM enrollments WHERE expires > ?", now)["n"] >= PENDING_ENROLLMENTS:
        return respond(429, {"error": "too many workers are waiting to be approved: try again later"})
    body = req.json()
    worker_id = short(body, "id", 64).lower()
    if not WORKER_ID.fullmatch(worker_id):
        return respond(400, {"error": "a worker enrolls with an id it picked, a UUID"})
    if db.one("SELECT 1 FROM workers WHERE id = ?", worker_id):
        return respond(409, {"error": "a worker with that id is already enrolled"})
    info = {"id": worker_id, "mem_mb": number(body, "mem_mb"), "cpus": number(body, "cpus", 0, 4096),
            "os": short(body, "os", 64)}
    device_code = secrets.token_urlsafe(32)
    user_code = "".join(secrets.choice(CODE_LETTERS) for _ in range(8))
    user_code = user_code[:4] + "-" + user_code[4:]
    with db.tx() as c:
        db.insert(c, "enrollments", {"user_code": user_code, "device_hash": digest(device_code), "expires": now + ENROLL_SECONDS, "info": info})

    return respond(200, {"device_code": device_code, "user_code": user_code, "interval": 5, "expires_in": ENROLL_SECONDS,
                         "verify_url": f"{settings.site()}/link.html?code={urllib.parse.quote(user_code)}"})


def poll(req):
    device_code = short(req.json(), "device_code", 128)
    with db.tx() as c:
        row = c.execute("SELECT * FROM enrollments WHERE device_hash = ? AND expires > ?", (digest(device_code), int(time.time()))).fetchone()
        if row is None:
            return respond(410, {"error": "that enrollment expired: start again"})
        if row["token"] is None:
            return respond(202, {"status": "waiting for approval"})
        c.execute("DELETE FROM enrollments WHERE user_code = ?", (row["user_code"],))

    return respond(200, {"worker_id": row["worker_id"], "token": row["token"]})


def pending(code):
    return db.one("SELECT * FROM enrollments WHERE user_code = ? AND expires > ? AND token IS NULL", code.upper(), int(time.time()))


def link_info(req, user, code):
    row = pending(code)
    if row is None:
        return respond(404, {"error": "no worker is waiting with that code: it may have expired"})

    return respond(200, {"code": row["user_code"], **json.loads(row["info"])})


def approve(req, user):
    code = short(req.json(), "code", 16).upper()
    token = secrets.token_urlsafe(32)
    with db.tx() as c:
        row = c.execute("SELECT * FROM enrollments WHERE user_code = ? AND expires > ? AND token IS NULL", (code, int(time.time()))).fetchone()
        if row is None:
            return respond(404, {"error": "no worker is waiting with that code: it may have expired"})
        info = json.loads(row["info"])
        worker_id = info["id"]
        if c.execute("SELECT 1 FROM workers WHERE id = ?", (worker_id,)).fetchone():
            return respond(409, {"error": "a worker with that id is already enrolled"})
        db.insert(c, "workers", {"id": worker_id, "owner_uid": user["uid"], "owner_name": user["name"],
                                 "token_hash": digest(token), "created": int(time.time()), "mem_mb": info["mem_mb"] or 0,
                                 "cpus": info["cpus"], "os": info["os"]})
        db.update(c, "enrollments", "user_code", code, {"worker_id": worker_id, "token": token})

    return respond(200, {"worker": worker_id})


def authenticate(req):
    scheme, _, token = req.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None

    return db.one("SELECT * FROM workers WHERE token_hash = ?", digest(token.strip()))


def pins():
    return {r["key"]: r["value"] for r in db.rows("SELECT * FROM pins")}


def releases():
    pinned = pins()
    out = {}
    for kind, base in (("parser", settings.get("parser-releases", PARSER_RELEASES)), ("worker", settings.get("worker-releases", WORKER_RELEASES))):
        if pinned.get(kind):
            out[kind] = {"version": pinned[kind], "manifest": base.rstrip("/") + "/" + urllib.parse.quote(pinned[kind]) + "/manifest.json"}

    return out


def heartbeat(req, worker):
    body = req.json()
    fields = {"last_seen": int(time.time())}
    for key in ("mem_mb", "mem_free_mb", "cpus", "disk_free_mb"):
        if number(body, key) is not None:
            fields[key] = number(body, key)
    for key in ("os", "version", "parser_version"):
        if key in body:
            fields[key] = short(body, key, 64)
    with db.tx() as c:
        db.update(c, "workers", "id", worker["id"], fields)

    return respond(200, {"poll_seconds": POLL_SECONDS, "paused": bool(worker["paused"]), "pause_reason": worker["pause_reason"],
                         "max_minutes": worker["max_minutes"], "releases": releases()})


def size(job):
    return max(job["duration_ms"] / 60000, 1) * max(len(json.loads(job["players"])), 2)


def mem_per_size(done):
    """Peak memory per minute of game per player that nine in ten finished parses stayed under; None
    until five have finished"""
    ratios = sorted(j["peak_mem_mb"] / size(j) for j in done)
    if len(ratios) < 5:
        return None

    return ratios[math.ceil(len(ratios) * 0.9) - 1]


def memory_ratio():
    return mem_per_size(db.rows("SELECT duration_ms, players, peak_mem_mb FROM jobs WHERE status = 'done' AND peak_mem_mb > 0 "
                                "ORDER BY finished DESC LIMIT 500"))


def estimate_mb(job, ratio):
    return None if ratio is None else int(ratio * size(job) * 1.25)


def fits(job, worker, ratio, biggest, now):
    if worker["max_minutes"] and job["duration_ms"] > worker["max_minutes"] * 60000:
        return False
    if job["too_small_mb"] and worker["mem_mb"] <= job["too_small_mb"] and now - (job["heartbeat"] or job["created"]) < BIGGER_WORKER_WAIT:
        return False
    estimate = estimate_mb(job, ratio)

    # the biggest machine there is takes what nothing could: it fails there, not waits forever
    return estimate is None or estimate <= worker["mem_mb"] or worker["mem_mb"] >= biggest


def uploads(job):
    curves = {"Content-Type": "application/octet-stream", "Content-Encoding": "gzip", "Content-Disposition": f'attachment; filename="{job["id"]}.curves"'}
    log = {"Content-Type": "text/plain; charset=utf-8"}

    return {"curves": {"url": storage.store.put_url(job["curves_key"], UPLOAD_SECONDS, curves), "headers": curves},
            "log": {"url": storage.store.put_url(job["log_key"], UPLOAD_SECONDS, log), "headers": log}}


def claim(req, worker):
    if worker["paused"]:
        return respond(204)
    parser = pins().get("parser")
    if parser and worker["parser_version"] != parser:
        return respond(204)
    now = int(time.time())
    ratio = memory_ratio()
    online = db.one("SELECT max(mem_mb) AS m FROM workers WHERE paused = 0 AND last_seen > ?", now - STALE)["m"] or 0
    biggest = max(online, worker["mem_mb"])
    with db.tx() as c:
        if c.execute("SELECT 1 FROM jobs WHERE status = 'running' AND worker = ?", (worker["id"],)).fetchone():
            return respond(204)
        queued = [dict(r) for r in c.execute("SELECT * FROM jobs WHERE status = 'queued' ORDER BY created")]
        job = next((j for j in queued if fits(j, worker, ratio, biggest, now)), None)
        if job is None:
            return respond(204)
        attempt = job["attempts"] + 1
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
        job.update(status="running", worker=worker["id"], started=now, heartbeat=now, progress_at=now, attempts=attempt,
                   stage="starting", pct=0, curves_key=f"curves/{job['id']}-{now}.curves", log_key=f"logs/{job['id']}/{stamp}-{attempt}.log")
        db.update(c, "jobs", "id", job["id"], {k: job[k] for k in ("status", "worker", "started", "heartbeat", "progress_at",
                                                                   "attempts", "stage", "pct", "curves_key", "log_key")})
        db.update(c, "workers", "id", worker["id"], {"last_seen": now})

    return respond(200, {"job": {"id": job["id"], "file_name": job["file_name"], "demo_url": DEMOS + urllib.parse.quote(job["file_name"]),
                                 "duration_ms": job["duration_ms"], "attempt": attempt},
                         "uploads": uploads(job)})


def mine(c, worker, replay_id):
    row = c.execute("SELECT * FROM jobs WHERE id = ? AND status = 'running' AND worker = ?", (replay_id, worker["id"])).fetchone()

    return dict(row) if row else None


def not_yours():
    return respond(409, {"error": "that job isn't yours any more: drop it"})


def progress(req, worker, replay_id):
    body = req.json()
    now = int(time.time())
    with db.tx() as c:
        job = mine(c, worker, replay_id)
        if job is None:
            return not_yours()
        fields = {"heartbeat": now}
        if "stage" in body:
            fields.update(stage=short(body, "stage"), pct=number(body, "pct", 0, 100) or 0, progress_at=now)
        if number(body, "peak_mem_mb") is not None:
            fields["peak_mem_mb"] = max(job["peak_mem_mb"] or 0, number(body, "peak_mem_mb"))
        db.update(c, "jobs", "id", replay_id, fields)
        db.update(c, "workers", "id", worker["id"], {"last_seen": now})

    return respond(200, {})


def fresh_uploads(req, worker, replay_id):
    with db.tx() as c:
        job = mine(c, worker, replay_id)
    if job is None:
        return not_yours()

    return respond(200, {"uploads": uploads(job)})


def finish(req, worker, replay_id):
    body = req.json()
    now = int(time.time())
    with db.tx() as c:
        job = mine(c, worker, replay_id)
    if job is None:
        return not_yours()
    packed = number(body, "packed_size")
    stored = storage.store.size(job["curves_key"])
    if packed is None or stored != packed:
        return respond(409, {"error": f"the .curves in storage is {stored} bytes, not the {packed} reported: upload it again"})
    events = body.get("events") if isinstance(body.get("events"), list) else []
    events = [{"f": number(e, "f") or 0, "kind": short(e, "kind", 40), "player": short(e, "player", 64), "text": short(e, "text", 300)}
              for e in events[:EVENTS_KEPT] if isinstance(e, dict)]
    has_log = storage.store.size(job["log_key"]) is not None
    with db.tx() as c:
        if mine(c, worker, replay_id) is None:
            return not_yours()
        db.update(c, "jobs", "id", replay_id, {
            "status": "done", "stage": "done", "pct": 100, "finished": now, "error": None, "size": number(body, "size"),
            "packed_size": packed, "curves_version": number(body, "curves_version"), "event_count": number(body, "event_count") or len(events),
            "events": events, "peak_mem_mb": max(job["peak_mem_mb"] or 0, number(body, "peak_mem_mb") or 0) or None,
            "log_key": job["log_key"] if has_log else None})
        db.update(c, "workers", "id", worker["id"], {"failures": 0, "last_seen": now})

    return respond(200, {})


def fail(req, worker, replay_id):
    body = req.json()
    now = int(time.time())
    with db.tx() as c:
        job = mine(c, worker, replay_id)
    if job is None:
        return not_yours()
    has_log = storage.store.size(job["log_key"]) is not None
    peak = max(job["peak_mem_mb"] or 0, number(body, "peak_mem_mb") or 0) or None
    error = short(body, "error", 2000) or "the worker gave no reason"
    with db.tx() as c:
        if mine(c, worker, replay_id) is None:
            return not_yours()
        # too big for this machine is no fault of its own: a bigger one tries it
        if body.get("out_of_memory") is True and job["attempts"] < MAX_ATTEMPTS:
            db.update(c, "jobs", "id", replay_id, {
                "status": "queued", "stage": "waiting for a worker with more memory", "worker": None, "peak_mem_mb": peak,
                "too_small_mb": max(job["too_small_mb"], worker["mem_mb"]), "log_key": job["log_key"] if has_log else None})
            db.update(c, "workers", "id", worker["id"], {"last_seen": now})

            return respond(200, {})
        db.update(c, "jobs", "id", replay_id, {
            "status": "failed", "stage": "failed", "finished": now, "error": error, "peak_mem_mb": peak,
            "log_key": job["log_key"] if has_log else None})
        failures = worker["failures"] + 1
        fields = {"failures": failures, "last_seen": now}
        if failures >= FAILURE_STREAK:
            fields.update(paused=1, pause_reason=f"paused after {failures} failed parses in a row")
        db.update(c, "workers", "id", worker["id"], fields)

    return respond(200, {})


PUBLIC = ("id", "owner_name", "created", "last_seen", "mem_mb", "mem_free_mb", "cpus", "disk_free_mb", "os",
          "version", "parser_version", "paused", "pause_reason", "max_minutes", "failures")


def listing(req, user):
    if auth.is_admin(user):
        rows = db.rows("SELECT * FROM workers ORDER BY created")
    else:
        rows = db.rows("SELECT * FROM workers WHERE owner_uid = ? ORDER BY created", user["uid"])
    running = {r["worker"]: r["id"] for r in db.rows("SELECT id, worker FROM jobs WHERE status = 'running'")}

    return respond(200, {"workers": [{**{k: w[k] for k in PUBLIC}, "job": running.get(w["id"])} for w in rows]})


def managed(user, worker_id):
    worker = db.one("SELECT * FROM workers WHERE id = ?", worker_id)
    if worker is None or (worker["owner_uid"] != user["uid"] and not auth.is_admin(user)):
        return None

    return worker


def change(req, user, worker_id):
    if managed(user, worker_id) is None:
        return respond(404, {"error": "no such worker"})
    body = req.json()
    fields = {}
    if isinstance(body.get("paused"), bool):
        fields.update(paused=int(body["paused"]), pause_reason="paused by its owner" if body["paused"] else None)
        if not body["paused"]:
            fields["failures"] = 0
    if "max_minutes" in body:
        fields["max_minutes"] = number(body, "max_minutes", 1, 24 * 60)
    if fields:
        with db.tx() as c:
            db.update(c, "workers", "id", worker_id, fields)

    return respond(200, {})


def remove(req, user, worker_id):
    if managed(user, worker_id) is None:
        return respond(404, {"error": "no such worker"})
    with db.tx() as c:
        c.execute("UPDATE jobs SET status = 'queued', stage = 'waiting for a worker (its worker was removed)', worker = NULL "
                  "WHERE status = 'running' AND worker = ?", (worker_id,))
        c.execute("DELETE FROM workers WHERE id = ?", (worker_id,))

    return respond(200, {})


def overview(req, user):
    now = int(time.time())
    counts = {r["status"]: r["n"] for r in db.rows("SELECT status, count(*) AS n FROM jobs GROUP BY status")}
    online = db.one("SELECT count(*) AS n FROM workers WHERE paused = 0 AND last_seen > ?", now - STALE)["n"]

    return respond(200, {"queued": counts.get("queued", 0), "running": counts.get("running", 0), "workers_online": online,
                         "workers": db.one("SELECT count(*) AS n FROM workers")["n"], "pins": pins(),
                         "mb_per_player_minute": memory_ratio(), "admin": auth.is_admin(user)})


def get_pins(req, user):
    return respond(200, pins())


def set_pins(req, user):
    if not auth.is_admin(user):
        return respond(403, {"error": "admins only"})
    body = req.json()
    version = short(body, "curves_version", 16)
    if version and not version.isdigit():
        return respond(400, {"error": "the .curves version is a number, like 11"})
    with db.tx() as c:
        for key in ("parser", "worker", "curves_version"):
            if key in body:
                value = short(body, key, 64)
                if value:
                    c.execute("INSERT INTO pins (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))
                else:
                    c.execute("DELETE FROM pins WHERE key = ?", (key,))

    return respond(200, pins())


def requeue_stale(now):
    """Jobs whose worker stopped reporting (a crash, a machine switched off) go back in the queue, noting
    how much memory that worker had, up to MAX_ATTEMPTS tries"""
    with db.tx() as c:
        stale = c.execute("SELECT jobs.*, workers.mem_mb AS worker_mem FROM jobs LEFT JOIN workers ON workers.id = jobs.worker "
                          "WHERE jobs.status = 'running' AND jobs.heartbeat < ?", (now - STALE,)).fetchall()
        for job in stale:
            if job["attempts"] >= MAX_ATTEMPTS:
                db.update(c, "jobs", "id", job["id"], {"status": "failed", "stage": "failed", "finished": now, "worker": None,
                                                       "error": f"its workers stopped responding {job['attempts']} times"})
            else:
                db.update(c, "jobs", "id", job["id"], {"status": "queued", "stage": "waiting for a worker (the last one stopped responding)",
                                                       "worker": None, "too_small_mb": max(job["too_small_mb"], job["worker_mem"] or 0)})


def expire_enrollments(now):
    with db.tx() as c:
        c.execute("DELETE FROM enrollments WHERE expires < ?", (now,))


def recheck_owners(now):
    """A worker parses only while its owner could still sign in: owners are asked about again every
    OWNER_RECHECK, with the refresh token their last sign-in left"""
    owners = db.rows("SELECT DISTINCT users.* FROM workers JOIN users ON users.uid = workers.owner_uid "
                     "WHERE workers.paused = 0 AND users.checked < ?", now - OWNER_RECHECK)
    for owner in owners:
        member, status, refresh = None, 401, owner["refresh_token"]
        if refresh:
            status, grant = auth.token({"grant_type": "refresh_token", "refresh_token": refresh})
            if status == 200:
                refresh = grant.get("refresh_token", refresh)
                status, member = auth.guild_member(grant["access_token"])
        if status in (0, 429) or status >= 500:
            # a refresh can hand back a new refresh token in place of the old one
            if refresh != owner["refresh_token"]:
                with db.tx() as c:
                    db.update(c, "users", "uid", owner["uid"], {"refresh_token": refresh})
            continue
        with db.tx() as c:
            db.update(c, "users", "uid", owner["uid"], {"checked": now, "refresh_token": refresh})
            if not auth.allowed(member):
                c.execute("UPDATE workers SET paused = 1, pause_reason = ? WHERE owner_uid = ? AND paused = 0",
                          ("paused: its owner no longer has access, or needs to sign in again", owner["uid"]))


def tidy(now):
    requeue_stale(now)
    expire_enrollments(now)
    jobs.expire(now)
    recheck_owners(now)
