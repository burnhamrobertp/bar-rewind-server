# Discord sign-in: only members of the guild holding an allowed role (or listed users) get a session,
# a base64(json) + "." + HMAC token in an HttpOnly cookie. Each session asks Discord again at most every
# ROLE_RECHECK seconds, so access ends when the role does, not when the session does.
import base64, hashlib, hmac, json, secrets, time, urllib.error, urllib.parse, urllib.request

from . import db, settings
from .web import cookie, redirect, respond

DISCORD = "https://discord.com/api/v10"
USER_AGENT = "bar-rewind-server (https://github.com/burnhamrobertp/bar-rewind-server, 2)"
SESSION_SECONDS = 12 * 3600
ROLE_RECHECK = 300


def http(url, data=None, headers=None):
    """(status, parsed JSON), status 0 when the other end couldn't be reached or made no sense"""
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, None
    except (OSError, ValueError):
        return 0, None


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(payload):
    body = b64(json.dumps(payload, separators=(",", ":")).encode())
    mac = hmac.new(settings.get("session-key").encode(), body.encode(), hashlib.sha256).digest()

    return body + "." + b64(mac)


def verify(token):
    body, _, mac = token.partition(".")
    want = hmac.new(settings.get("session-key").encode(), body.encode(), hashlib.sha256).digest()
    try:
        if not mac or not hmac.compare_digest(unb64(mac), want):
            return None
        payload = json.loads(unb64(body))
    except ValueError:
        return None

    return payload if payload.get("exp", 0) > time.time() else None


def guild_member(access_token):
    """(status, the guild member behind a token): 401 for a dead token, 404 for someone not in the guild"""
    status, member = http(f"{DISCORD}/users/@me/guilds/{settings.get('guild-id')}/member",
                          headers={"Authorization": "Bearer " + access_token})

    return (status, member) if status != 200 or isinstance(member, dict) else (0, None)


def allowed(member):
    if member is None:
        return False

    return bool(set(member.get("roles") or []) & settings.ids("role-ids")) or member.get("user", {}).get("id") in settings.ids("user-ids")


def is_admin(user):
    return user["uid"] in settings.ids("admin-ids") or db.one("SELECT 1 FROM admins WHERE uid = ?", user["uid"]) is not None


def local_path(text):
    """Only a path on this site, never somewhere else: //host or /\\host would leave it"""
    return text if text.startswith("/") and not text.startswith(("//", "/\\")) else "/"


def login(req):
    state = secrets.token_urlsafe(24)
    query = urllib.parse.urlencode({"response_type": "code", "client_id": settings.get("discord-client-id"),
                                    "scope": "identify guilds.members.read", "redirect_uri": settings.site() + "/api/callback",
                                    "state": state, "prompt": "none"})

    next_path = urllib.parse.quote(local_path(req.query.get("next", "/")), safe="")

    return redirect("https://discord.com/oauth2/authorize?" + query, [cookie("st", state, 600), cookie("nx", next_path, 600)])


def token(form):
    form = {"client_id": settings.get("discord-client-id"), "client_secret": settings.get("discord-client-secret"), **form}

    return http(f"{DISCORD}/oauth2/token", urllib.parse.urlencode(form).encode(), {"Content-Type": "application/x-www-form-urlencoded"})


def callback(req):
    code, state = req.query.get("code", ""), req.query.get("state", "")
    if not code or not hmac.compare_digest(state, req.cookies.get("st", "")):
        return redirect("/signin.html?signin=failed")
    status, grant = token({"grant_type": "authorization_code", "code": code, "redirect_uri": settings.site() + "/api/callback"})
    if status != 200:
        return redirect("/signin.html?signin=failed")
    status, member = guild_member(grant["access_token"])
    if status not in (200, 404):
        return redirect("/signin.html?signin=failed")
    if not allowed(member):
        return redirect("/signin.html?signin=denied", [cookie("st", "", 0)])
    user = member["user"]
    name = member.get("nick") or user.get("global_name") or user["username"]
    now = int(time.time())
    # kept so a worker's owner can be checked again while they are not signed in
    with db.tx() as c:
        c.execute("INSERT INTO users (uid, name, refresh_token, checked) VALUES (?, ?, ?, ?) "
                  "ON CONFLICT (uid) DO UPDATE SET name = excluded.name, refresh_token = excluded.refresh_token, checked = excluded.checked",
                  (user["id"], name, grant.get("refresh_token"), now))
    payload = {"uid": user["id"], "name": name, "tok": grant["access_token"], "chk": now,
               "exp": now + min(SESSION_SECONDS, int(grant.get("expires_in", SESSION_SECONDS)))}

    back = local_path(urllib.parse.unquote(req.cookies.get("nx", "/")))

    return redirect(back, [cookie("st", "", 0), cookie("nx", "", 0), cookie("s", sign(payload), SESSION_SECONDS)])


def signed_in(req, force_check=False):
    """(user, cookies to set), or (None, the response to send instead)"""
    user = verify(req.cookies["s"]) if req.cookies.get("s") else None
    if user is None:
        return None, respond(401, {"error": "sign in"})
    now = int(time.time())
    if not force_check and now - int(user.get("chk", 0)) < ROLE_RECHECK:
        return user, []
    status, member = guild_member(user["tok"])
    if status == 401:
        return None, respond(401, {"error": "your Discord sign-in expired: sign in again"}, [cookie("s", "", 0)])
    if status not in (200, 404):
        return None, respond(503, {"error": "couldn't reach Discord to check your access: try again in a minute"})
    if not allowed(member):
        return None, respond(403, {"error": "your Discord account no longer has access"}, [cookie("s", "", 0)])
    user["chk"] = now

    return user, [cookie("s", sign(user), max(0, int(user["exp"]) - now))]
