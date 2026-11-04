# BAR Rewind server: the site, and the API under /api:
#   GET  /api/login, /api/callback   Discord sign-in; only members holding an allowed role get a session
#   GET  /api/logout
#   GET  /api/me                     who is signed in
#   GET  /api/jobs                   every job, newest first
#   POST /api/jobs {"replay": ...}   queue a replay (a bar-rts.com link or its id)
#   GET  /api/jobs/<id>/watch        a signed download URL for a parsed replay, and its barrewind:// link
#   GET  /api/jobs/<id>/log          the latest attempt's log, as text
import mimetypes, re
from pathlib import Path

from . import auth, jobs
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
