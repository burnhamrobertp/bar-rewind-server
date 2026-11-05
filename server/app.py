# BAR Rewind server: the site, and the API under /api:
#   GET  /api/login, /api/callback   Discord sign-in; only members holding an allowed role get a session
#   GET  /api/logout
#   GET  /api/me                     who is signed in
#   GET  /api/jobs                   every job, newest first
#   POST /api/jobs {"replay": ...}   queue a replay (a bar-rts.com link or its id)
#   GET  /api/jobs/<id>/watch        a signed download URL for a parsed replay, and its barrewind:// link
#   GET  /api/jobs/<id>/log          the latest attempt's log, as text
#   POST /api/jobs/<id>/cancel       take a queued or running job out (whoever queued it, or an admin)
#   GET, POST /api/admins {uid}, DELETE /api/admins/<uid>   (admins) who else is an admin
#   GET, POST /api/widgets {files: [{name, text}]}   (admins) the widgets every parse runs: names and sizes, or a new set
# and, under /api/worker/, the workers' own API.
import mimetypes, re
from pathlib import Path

from . import admins, auth, jobs, storage, widgets, workers
from .web import Response, cookie, redirect, respond

SITE = Path(__file__).resolve().parent.parent / "site"


def route(req):
    if not req.path.startswith("/api/"):
        return static(req)
    if req.path == "/api/login":
        return auth.login(req)
    if req.path == "/api/callback":
        return auth.callback(req)
    if req.path == "/api/logout":
        return redirect("/", [cookie("s", "", 0)])
    if req.path.startswith("/api/worker/"):
        return worker_route(req)
    if req.path.startswith("/api/dev-store/") and isinstance(storage.store, storage.Folder):
        return storage.store.serve(req)

    submitting = req.path == "/api/jobs" and req.method == "POST"
    user, extra = auth.signed_in(req, force_check=submitting)
    if user is None:
        return extra
    response = signed_in_route(req, user)
    response.cookies += extra

    return response


def signed_in_route(req, user):
    if req.path == "/api/me":
        return respond(200, {"name": user["name"], "admin": auth.is_admin(user)})
    if req.path == "/api/jobs" and req.method == "GET":
        return jobs.list_jobs(req, user)
    if req.path == "/api/jobs" and req.method == "POST":
        return jobs.submit(req, user)
    m = re.fullmatch(r"/api/jobs/([0-9a-f]{32})/(watch|log)", req.path)
    if m and req.method == "GET":
        return (jobs.watch if m.group(2) == "watch" else jobs.job_log)(req, user, m.group(1))
    m = re.fullmatch(r"/api/jobs/([0-9a-f]{32})/cancel", req.path)
    if m and req.method == "POST":
        return jobs.cancel(req, user, m.group(1))
    if req.path == "/api/overview" and req.method == "GET":
        return workers.overview(req, user)
    m = re.fullmatch(r"/api/link/([A-Za-z-]{1,16})", req.path)
    if m and req.method == "GET":
        return workers.link_info(req, user, m.group(1))
    if req.path == "/api/link" and req.method == "POST":
        return workers.approve(req, user)
    if req.path == "/api/workers" and req.method == "GET":
        return workers.listing(req, user)
    m = re.fullmatch(r"/api/workers/([0-9a-f-]{36})", req.path)
    if m and req.method == "POST":
        return workers.change(req, user, m.group(1))
    if m and req.method == "DELETE":
        return workers.remove(req, user, m.group(1))
    if req.path == "/api/admins":
        return admins.listing(req, user) if req.method == "GET" else admins.add(req, user)
    m = re.fullmatch(r"/api/admins/(\d{1,20})", req.path)
    if m and req.method == "DELETE":
        return admins.remove(req, user, m.group(1))
    if req.path == "/api/widgets":
        return widgets.listing(req, user) if req.method == "GET" else widgets.replace(req, user)
    if req.path == "/api/pins":
        return workers.get_pins(req, user) if req.method == "GET" else workers.set_pins(req, user)

    return respond(404, {"error": "no such endpoint"})


def worker_route(req):
    if req.method != "POST":
        return respond(405, {"error": "workers POST"})
    if req.path == "/api/worker/enroll":
        return workers.enroll(req)
    if req.path == "/api/worker/enroll/poll":
        return workers.poll(req)
    worker = workers.authenticate(req)
    if worker is None:
        return respond(401, {"error": "unknown worker: enroll it again"})
    if req.path == "/api/worker/heartbeat":
        return workers.heartbeat(req, worker)
    if req.path == "/api/worker/widgets":
        return widgets.for_worker(req, worker)
    if req.path == "/api/worker/claim":
        return workers.claim(req, worker)
    m = re.fullmatch(r"/api/worker/jobs/([0-9a-f]{32})/(progress|uploads|done|fail)", req.path)
    if m:
        handler = {"progress": workers.progress, "uploads": workers.fresh_uploads, "done": workers.finish, "fail": workers.fail}[m.group(2)]

        return handler(req, worker, m.group(1))

    return respond(404, {"error": "no such endpoint"})


def static(req):
    if req.method not in ("GET", "HEAD"):
        return respond(405, {"error": "method not allowed"})
    name = req.path.lstrip("/") or "index.html"
    path = (SITE / name).resolve()
    if SITE not in path.parents or not path.is_file():
        return Response(404, b"not found", {"content-type": "text/plain"})
    kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    return Response(200, path.read_bytes(), {"content-type": kind, "cache-control": "no-cache"})
