# Parse widgets: extra .lua widgets every worker runs in every parse. They're files in a folder beside the
# database, readable only by the server's user and outside the database, so its backup in R2 never holds
# them; a rebuilt server starts without them. Workers get them through their authenticated API only.
# A worker without the current set takes no jobs; a parse made with another set shows as missing them.
import hashlib, os, re, shutil, tempfile, threading
from pathlib import Path

from . import auth, db
from .web import respond

NAME = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}\.lua")
MAX_BYTES = 1 << 20
_swap = threading.Lock()


def folder():
    return Path(db._path).resolve().parent / "widgets"


def files():
    with _swap:
        return _read(folder())


def _read(here):
    if not here.is_dir():
        return []

    return [{"name": p.name, "body": p.read_text(encoding="utf-8")} for p in sorted(here.iterdir(), key=lambda p: p.name.encode())
            if p.is_file() and NAME.fullmatch(p.name)]


def digest(found=None):
    """What a worker reports once it has the set: empty for no widgets"""
    found = files() if found is None else found
    if not found:
        return ""
    listing = "".join(f"{f['name']}\n{hashlib.sha256(f['body'].encode()).hexdigest()}\n" for f in found)

    return hashlib.sha256(listing.encode()).hexdigest()


def store(sent):
    """Swaps in the new set whole, so a worker never fetches half of one"""
    here = folder()
    staging = Path(tempfile.mkdtemp(prefix=".widgets-", dir=here.parent))
    os.chmod(staging, 0o700)
    for f in sent:
        (staging / f["name"]).write_text(f["text"], encoding="utf-8")
    old = here.with_name(".widgets-old")
    shutil.rmtree(old, ignore_errors=True)
    with _swap:
        if here.exists():
            here.rename(old)
        staging.rename(here)
    shutil.rmtree(old, ignore_errors=True)


def listing(req, user):
    if not auth.is_admin(user):
        return respond(403, {"error": "admins only"})
    found = files()

    return respond(200, {"hash": digest(found), "files": [{"name": f["name"], "size": len(f["body"].encode())} for f in found]})


def add(req, user):
    """Adds the files sent to the set, replacing any of the same name"""
    if not auth.is_admin(user):
        return respond(403, {"error": "admins only"})
    sent = req.json().get("files")
    if not isinstance(sent, list) or not all(isinstance(f, dict) and isinstance(f.get("name"), str) and isinstance(f.get("text"), str) for f in sent):
        return respond(400, {"error": "send files: [{name, text}]"})
    if any(not NAME.fullmatch(f["name"]) for f in sent) or len({f["name"] for f in sent}) != len(sent):
        return respond(400, {"error": "widget names are distinct, end in .lua, use letters, digits, _ . - and don't start with a dot"})
    kept = {f["name"]: f["body"] for f in files()}
    kept.update({f["name"]: f["text"] for f in sent})
    if sum(len(text.encode()) for text in kept.values()) > MAX_BYTES:
        return respond(400, {"error": "the widgets would add up to more than 1 MB"})
    store([{"name": name, "text": text} for name, text in kept.items()])

    return listing(req, user)


def remove(req, user, name):
    if not auth.is_admin(user):
        return respond(403, {"error": "admins only"})
    kept = [{"name": f["name"], "text": f["body"]} for f in files() if f["name"] != name]
    store(kept)

    return listing(req, user)


def for_worker(req, worker):
    found = files()

    return respond(200, {"hash": digest(found), "files": [{"name": f["name"], "text": f["body"]} for f in found]})
