# BAR Rewind server API, one Lambda behind CloudFront at /api/*:
#   GET  /api/login, /api/callback   Discord sign-in; only members holding an allowed role get a session
#   GET  /api/me                     who is signed in
#   GET  /api/jobs                   every job, newest first
#   POST /api/jobs {"replay": ...}   queue a replay (a bar-rts.com link or its id) and start a worker
#   GET  /api/jobs/<id>/watch        a signed download URL for a parsed replay, and its barrewind:// link
#   GET  /api/jobs/<id>/log          the latest attempt's log, as text
# Settings live in SSM under PARAMS (see deploy.sh); the rest comes from the template's environment.
import base64, hashlib, hmac, json, os, re, secrets, time, urllib.error, urllib.parse, urllib.request

import boto3
from boto3.dynamodb.conditions import Attr

TABLE = boto3.resource("dynamodb").Table(os.environ["TABLE"])
ECS = boto3.client("ecs")
SSM = boto3.client("ssm")
S3 = boto3.client("s3")
DISCORD = "https://discord.com/api/v10"
REPLAY_API = "https://api.bar-rts.com/replays/"
USER_AGENT = "bar-rewind-server (https://github.com/burnhamrobertp/bar-rewind-server, 1)"
SESSION_SECONDS = 12 * 3600
LINK_SECONDS = 3600
DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "30"))
QUEUE_LIMIT = int(os.environ.get("QUEUE_LIMIT", "50"))
MAX_MINUTES = int(os.environ.get("MAX_MINUTES", "150"))
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "60"))
# the .curves format the deployed worker writes; jobs parsed in an older one are parsed again
CURVES_VERSION = int(os.environ.get("CURVES_VERSION", "0"))
# a worker on someone's own machine (worker.py STAY=1) keeps this record fresh; jobs wait for it
LOCAL_WORKER = "_worker_local"
LOCAL_FRESH = 120
REPLAY_ID = re.compile(r"\b([0-9a-f]{32})\b")

_params = None


def params():
    global _params
    if _params is None:
        _params = {}
        kwargs = {"Path": os.environ["PARAMS"], "WithDecryption": True}
        while True:
            page = SSM.get_parameters_by_path(**kwargs)
            _params.update({p["Name"].rsplit("/", 1)[-1]: p["Value"] for p in page["Parameters"]})
            if "NextToken" not in page:
                break
            kwargs["NextToken"] = page["NextToken"]

    return _params


# responses ---------------------------------------------------------------------------------------

def respond(status, body=None, cookies=(), headers=None):
    return {"statusCode": status, "headers": {"content-type": "application/json", "cache-control": "no-store", **(headers or {})},
            "cookies": list(cookies), "body": json.dumps(body if body is not None else {})}


def redirect(location, cookies=()):
    return {"statusCode": 302, "headers": {"location": location, "cache-control": "no-store"}, "cookies": list(cookies), "body": ""}


def cookie(name, value, max_age):
    return f"{name}={value}; Path=/api; Max-Age={max_age}; HttpOnly; Secure; SameSite=Lax"


# sessions: base64(json) + "." + HMAC, in an HttpOnly cookie --------------------------------------

def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(payload):
    body = b64(json.dumps(payload, separators=(",", ":")).encode())
    mac = hmac.new(params()["session-key"].encode(), body.encode(), hashlib.sha256).digest()

    return body + "." + b64(mac)


def verify(token):
    body, _, mac = token.partition(".")
    want = hmac.new(params()["session-key"].encode(), body.encode(), hashlib.sha256).digest()
    if not mac or not hmac.compare_digest(unb64(mac), want):
        return None
    payload = json.loads(unb64(body))

    return payload if payload.get("exp", 0) > time.time() else None


def cookies_of(event):
    return dict(c.split("=", 1) for c in event.get("cookies") or [] if "=" in c)


def session(event):
    token = cookies_of(event).get("s")

    return verify(token) if token else None


# Discord -----------------------------------------------------------------------------------------

def http(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, None


def site():
    return params()["site-url"].rstrip("/")


def member_roles(access_token):
    """The member's role ids in the guild, or None when they are not in it or the token is dead."""
    status, member = http(f"{DISCORD}/users/@me/guilds/{params()['guild-id']}/member",
                          headers={"Authorization": "Bearer " + access_token})

    return (member or {}).get("roles", []) if status == 200 else None, member


def allowed(roles):
    return bool(set(roles or []) & {r.strip() for r in params()["role-ids"].split(",")})


def login(event):
    state = secrets.token_urlsafe(24)
    query = urllib.parse.urlencode({"response_type": "code", "client_id": params()["discord-client-id"],
                                    "scope": "identify guilds.members.read", "redirect_uri": site() + "/api/callback",
                                    "state": state, "prompt": "none"})

    return redirect("https://discord.com/oauth2/authorize?" + query, [cookie("st", state, 600)])


def callback(event):
    q = event.get("queryStringParameters") or {}
    if not q.get("code") or not hmac.compare_digest(q.get("state", ""), cookies_of(event).get("st", "")):
        return redirect("/?signin=failed")
    form = urllib.parse.urlencode({"client_id": params()["discord-client-id"], "client_secret": params()["discord-client-secret"],
                                   "grant_type": "authorization_code", "code": q["code"],
                                   "redirect_uri": site() + "/api/callback"}).encode()
    status, token = http(f"{DISCORD}/oauth2/token", form, {"Content-Type": "application/x-www-form-urlencoded"})
    if status != 200:
        return redirect("/?signin=failed")
    roles, member = member_roles(token["access_token"])
    if not allowed(roles):
        return redirect("/?signin=denied", [cookie("st", "", 0)])
    user = member["user"]
    payload = {"uid": user["id"], "name": member.get("nick") or user.get("global_name") or user["username"],
               "tok": token["access_token"], "exp": int(time.time()) + min(SESSION_SECONDS, int(token.get("expires_in", SESSION_SECONDS)))}

    return redirect("/", [cookie("st", "", 0), cookie("s", sign(payload), SESSION_SECONDS)])


# jobs ----------------------------------------------------------------------------------------------

PUBLIC = ("id", "status", "stage", "pct", "created", "started", "finished", "heartbeat", "progress_at", "user", "map",
          "duration_ms", "players", "size", "packed_size", "error", "attempts", "start_time")


def all_jobs():
    items, kwargs = [], {}
    while True:
        page = TABLE.scan(**kwargs)
        items += page["Items"]
        if "LastEvaluatedKey" not in page:
            return [i for i in items if re.fullmatch(r"[0-9a-f]{32}", i["id"])]
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def outdated(job):
    return job["status"] == "done" and int(job.get("curves_version", 0)) < CURVES_VERSION


def curves_key(replay_id):
    return f"curves/{replay_id}.curves"


def stored(replay_id):
    """Whether the parsed file is still in S3: the lifecycle rule can delete it before DynamoDB
    gets round to expiring its job."""
    try:
        S3.head_object(Bucket=os.environ["BUCKET"], Key=curves_key(replay_id))
    except S3.exceptions.ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return False
        raise

    return True


def public(job):
    fields = {k: (int(v) if hasattr(v, "as_integer_ratio") and not isinstance(v, bool) else v) for k, v in job.items() if k in PUBLIC}

    return {**fields, "outdated": outdated(job), "has_log": bool(job.get("log"))}


LOG_TAIL = 256 * 1024


def job_log(replay_id):
    """The latest attempt's log as plain text: its head (the worker's account of the job) and, when
    longer, the end of the engine's output, which is where a failure shows"""
    job = TABLE.get_item(Key={"id": replay_id}).get("Item")
    if not job or not job.get("log"):
        return respond(404, {"error": "no log for that replay yet"})
    obj = S3.get_object(Bucket=os.environ["BUCKET"], Key=job["log"])
    data = obj["Body"].read()
    if len(data) > 2 * LOG_TAIL:
        data = data[:LOG_TAIL // 4] + b"\n\n[... %d bytes left out ...]\n\n" % (len(data) - LOG_TAIL - LOG_TAIL // 4) + data[-LOG_TAIL:]

    return {"statusCode": 200, "headers": {"content-type": "text/plain; charset=utf-8", "cache-control": "no-store"},
            "body": data.decode(errors="replace")}


def ensure_worker(jobs):
    """Starts a worker when jobs wait and none is running. Also called on every listing, which
    restarts the queue when a worker exited just as a job arrived."""
    now = time.time()
    if not any(j["status"] == "queued" for j in jobs):
        return
    local = TABLE.get_item(Key={"id": LOCAL_WORKER}).get("Item")
    if local and now - int(local.get("seen", 0)) < LOCAL_FRESH:
        return
    running = ECS.list_tasks(cluster=os.environ["CLUSTER"], family=os.environ["TASK_FAMILY"], desiredStatus="RUNNING")["taskArns"]
    if running:
        return
    ECS.run_task(cluster=os.environ["CLUSTER"], taskDefinition=os.environ["TASK_FAMILY"], count=1,
                 capacityProviderStrategy=[{"capacityProvider": "FARGATE_SPOT", "weight": 1}],
                 networkConfiguration={"awsvpcConfiguration": {"subnets": os.environ["SUBNETS"].split(","),
                                                               "securityGroups": [os.environ["SECURITY_GROUP"]],
                                                               "assignPublicIp": "ENABLED"}},
                 propagateTags="TASK_DEFINITION", startedBy="api-%d" % now)


def list_jobs(user):
    jobs = all_jobs()
    ensure_worker(jobs)

    return respond(200, {"jobs": [public(j) for j in sorted(jobs, key=lambda j: -int(j["created"]))]})


def submit(event, user):
    try:
        text = json.loads(event.get("body") or "{}").get("replay", "")
    except ValueError:
        text = ""
    m = REPLAY_ID.search(text.lower())
    if not m:
        return respond(400, {"error": "paste a bar-rts.com replay link or its 32-character id"})
    replay_id = m.group(1)

    roles, _ = member_roles(user["tok"])
    if roles is None:
        return respond(401, {"error": "your Discord sign-in expired: sign in again"})
    if not allowed(roles):
        return respond(403, {"error": "your Discord account no longer has a role that may queue replays"})

    jobs = all_jobs()
    existing = next((j for j in jobs if j["id"] == replay_id), None)
    reusable = existing and existing["status"] != "failed" and not (
        existing["status"] == "done" and (outdated(existing) or not stored(replay_id)))
    if reusable:
        ensure_worker(jobs)
        return respond(200, {"job": public(existing)})
    now = int(time.time())
    if sum(1 for j in jobs if j.get("uid") == user["uid"] and int(j["created"]) > now - 86400) >= DAILY_LIMIT:
        return respond(429, {"error": f"you queued {DAILY_LIMIT} replays in the last day: try again later"})
    if sum(1 for j in jobs if j["status"] in ("queued", "running")) >= QUEUE_LIMIT:
        return respond(429, {"error": "the queue is full: try again later"})

    status, info = http(REPLAY_API + replay_id)
    if status != 200 or not info or not info.get("fileName"):
        return respond(404, {"error": "bar-rts.com has no replay " + replay_id})
    if int(info.get("durationMs") or 0) > MAX_MINUTES * 60000:
        return respond(400, {"error": f"replays over {MAX_MINUTES} minutes are not parsed here"})
    players = [p["name"] for team in info.get("AllyTeams") or [] for p in team.get("Players") or []]
    # a day before the lifecycle rule deletes its file, so a listed job rarely outlives it
    job = {"id": replay_id, "status": "queued", "stage": "waiting for a worker", "pct": 0, "created": now,
           "expires": now + (RETENTION_DAYS - 1) * 86400, "uid": user["uid"], "user": user["name"], "attempts": 0,
           "map": (info.get("Map") or {}).get("scriptName", ""), "duration_ms": int(info.get("durationMs") or 0),
           "players": players[:64], "start_time": info.get("startTime", "")}
    TABLE.put_item(Item=job)
    ensure_worker(jobs + [job])

    return respond(202, {"job": public(job)})


# CloudFront signed URLs (canned policy, RSA-SHA1), without a crypto library --------------------------

def der(data, i):
    """(tag, value, next index) of the DER element at i"""
    tag, length = data[i], data[i + 1]
    i += 2
    if length & 0x80:
        n = length & 0x7F
        length, i = int.from_bytes(data[i:i + n], "big"), i + n

    return tag, data[i:i + length], i + length


def rsa_key(pem):
    """(n, d) of a PKCS#1 or PKCS#8 RSA private key"""
    body = base64.b64decode("".join(l for l in pem.strip().splitlines() if not l.startswith("-----")))
    _, seq, _ = der(body, 0)
    fields, i = [], 0
    while i < len(seq):
        tag, value, i = der(seq, i)
        fields.append((tag, value))
    if fields[1][0] == 0x30:  # PKCS#8 wraps the PKCS#1 key in an OCTET STRING
        return rsa_key_from_pkcs1(fields[2][1])

    return rsa_key_from_pkcs1(body)


def rsa_key_from_pkcs1(body):
    _, seq, _ = der(body, 0)
    ints, i = [], 0
    while i < len(seq) and len(ints) < 4:
        _, value, i = der(seq, i)
        ints.append(int.from_bytes(value, "big"))

    return ints[1], ints[3]


def rsa_sha1(message, n, d):
    digest_info = bytes.fromhex("3021300906052b0e03021a05000414") + hashlib.sha1(message).digest()
    k = (n.bit_length() + 7) // 8
    block = b"\x00\x01" + b"\xff" * (k - len(digest_info) - 3) + b"\x00" + digest_info

    return pow(int.from_bytes(block, "big"), d, n).to_bytes(k, "big")


def signed_url(url, expires):
    policy = json.dumps({"Statement": [{"Resource": url, "Condition": {"DateLessThan": {"AWS:EpochTime": expires}}}]},
                        separators=(",", ":")).encode()
    n, d = rsa_key(params()["cloudfront-private-key"])
    signature = base64.b64encode(rsa_sha1(policy, n, d)).decode().translate(str.maketrans("+=/", "-_~"))

    return f"{url}?Expires={expires}&Signature={signature}&Key-Pair-Id={os.environ['KEY_PAIR_ID']}"


def watch(replay_id):
    job = TABLE.get_item(Key={"id": replay_id}).get("Item")
    if not job or job["status"] != "done":
        return respond(404, {"error": "that replay is not parsed"})
    if outdated(job):
        return respond(409, {"error": "parsed for an older player: queue it again"})
    if not stored(replay_id):
        return respond(404, {"error": "that replay's parse has expired: queue it again"})
    url = signed_url(f"{site()}/{curves_key(replay_id)}", int(time.time()) + LINK_SECONDS)

    return respond(200, {"url": url, "link": "barrewind://play?url=" + urllib.parse.quote(url, safe="")})


# routing ---------------------------------------------------------------------------------------------

def handler(event, context):
    if not hmac.compare_digest(event.get("headers", {}).get("x-origin-secret", ""), params()["origin-secret"]):
        return respond(403, {"error": "forbidden"})
    method = event["requestContext"]["http"]["method"]
    path = event.get("rawPath", "")
    if path == "/api/login":
        return login(event)
    if path == "/api/callback":
        return callback(event)

    user = session(event)
    if user is None:
        return respond(401, {"error": "sign in"})
    if path == "/api/me":
        return respond(200, {"name": user["name"]})
    if path == "/api/logout":
        return redirect("/", [cookie("s", "", 0)])
    if path == "/api/jobs" and method == "GET":
        return list_jobs(user)
    if path == "/api/jobs" and method == "POST":
        return submit(event, user)
    m = re.fullmatch(r"/api/jobs/([0-9a-f]{32})/(watch|log)", path)
    if m and method == "GET":
        return watch(m.group(1)) if m.group(2) == "watch" else job_log(m.group(1))

    return respond(404, {"error": "no such endpoint"})
