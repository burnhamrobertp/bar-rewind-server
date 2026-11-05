# Parse widgets: extra .lua widgets every worker runs in every parse. They live only in the database (and
# so its backup), reach workers through their authenticated API, and are never in a repo or a public URL.
# A worker without the current set takes no jobs; a parse made with another set shows as missing them.
import hashlib, re

from . import auth, db
from .web import respond

NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}\.lua")
MAX_BYTES = 1 << 20


def files():
    return db.rows("SELECT name, body FROM widgets ORDER BY name")


def digest(found=None):
    """What a worker reports once it has the set: empty for no widgets"""
    found = files() if found is None else found
    if not found:
        return ""
    listing = "".join(f"{f['name']}\n{hashlib.sha256(f['body'].encode()).hexdigest()}\n" for f in found)

    return hashlib.sha256(listing.encode()).hexdigest()


def listing(req, user):
    if not auth.is_admin(user):
        return respond(403, {"error": "admins only"})
    found = files()

    return respond(200, {"hash": digest(found), "files": [{"name": f["name"], "size": len(f["body"].encode())} for f in found]})


def replace(req, user):
    if not auth.is_admin(user):
        return respond(403, {"error": "admins only"})
    sent = req.json().get("files")
    if not isinstance(sent, list) or not all(isinstance(f, dict) and isinstance(f.get("name"), str) and isinstance(f.get("text"), str) for f in sent):
        return respond(400, {"error": "send files: [{name, text}]"})
    if any(not NAME.fullmatch(f["name"]) for f in sent) or len({f["name"] for f in sent}) != len(sent):
        return respond(400, {"error": "widget names are distinct, end in .lua and use letters, digits, _ . -"})
    if sum(len(f["text"].encode()) for f in sent) > MAX_BYTES:
        return respond(400, {"error": "the widgets add up to more than 1 MB"})
    with db.tx() as c:
        c.execute("DELETE FROM widgets")
        c.executemany("INSERT INTO widgets (name, body) VALUES (?, ?)", [(f["name"], f["text"]) for f in sent])

    return listing(req, user)


def for_worker(req, worker):
    found = files()

    return respond(200, {"hash": digest(found), "files": [{"name": f["name"], "text": f["body"]} for f in found]})
