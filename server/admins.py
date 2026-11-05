# Admins: the admin-ids setting's users, who can't be removed here, plus whoever an admin adds from the
# people who have signed in. Checked against the database on every request, so a change applies at once.
import time

from . import auth, db, settings
from .web import respond


def listing(req, user):
    if not auth.is_admin(user):
        return respond(403, {"error": "admins only"})
    owners = settings.ids("admin-ids")
    names = {r["uid"]: r["name"] for r in db.rows("SELECT uid, name FROM users")}
    added = {r["uid"]: r for r in db.rows("SELECT * FROM admins")}
    admins = [{"uid": uid, "name": names.get(uid, uid), "owner": True} for uid in sorted(owners)]
    admins += [{"uid": uid, "name": names.get(uid, uid), "owner": False, "added": r["added"], "added_by": names.get(r["added_by"], r["added_by"])}
               for uid, r in sorted(added.items(), key=lambda kv: kv[1]["added"]) if uid not in owners]
    others = [{"uid": uid, "name": name} for uid, name in sorted(names.items(), key=lambda kv: kv[1].lower())
              if uid not in owners and uid not in added]

    return respond(200, {"admins": admins, "others": others})


def add(req, user):
    if not auth.is_admin(user):
        return respond(403, {"error": "admins only"})
    uid = str(req.json().get("uid", ""))
    if not db.one("SELECT 1 FROM users WHERE uid = ?", uid):
        return respond(404, {"error": "only someone who has signed in here can be made an admin"})
    with db.tx() as c:
        c.execute("INSERT INTO admins (uid, added, added_by) VALUES (?, ?, ?) ON CONFLICT (uid) DO NOTHING", (uid, int(time.time()), user["uid"]))

    return listing(req, user)


def remove(req, user, uid):
    if not auth.is_admin(user):
        return respond(403, {"error": "admins only"})
    if uid in settings.ids("admin-ids"):
        return respond(409, {"error": "the server's settings make them an admin: they can't be removed here"})
    with db.tx() as c:
        c.execute("DELETE FROM admins WHERE uid = ?", (uid,))

    return listing(req, user)
