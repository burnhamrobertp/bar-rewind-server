# bar-rewind-server

The server behind BAR Rewind's Watch links: moderators paste a bar-rts.com replay link, a worker parses it
with [BAR-Rewind](https://github.com/burnhamrobertp/BAR-Rewind), and they click Watch to rewind and scrub
the match in their own BAR. Workers are machines moderators run themselves,
[bar-rewind-worker](https://github.com/burnhamrobertp/bar-rewind-worker).

Sign-in is Discord, and only members of the BAR Discord holding an allowed role get in.

## How it works

```
browser --Cloudflare--> tunnel --> python -m server (one EC2 box, no open ports)
                                     the site, the API, the worker API; state in SQLite
worker  --Cloudflare--> tunnel -->   enroll, heartbeat, claim, report
worker, player --> R2               presigned uploads and downloads only: nobody but the server holds a key
SQLite --Litestream--> R2           continuous backup, restored on a fresh box
```

- A worker enrolls once: it shows a code and opens the site, where its owner approves it. After that it
  needs no settings of its own: the server tells it what to run (the pinned parser and worker releases),
  whether it's paused and the longest replay it takes.
- A worker gets the oldest queued job that fits it: not longer than its limit, and, once five parses have
  reported their peak memory, not bigger than its memory. A job whose worker went quiet goes back in the
  queue, preferring a machine with more memory, up to 3 tries.
- A worker that fails 5 parses in a row pauses. One whose owner loses their Discord role pauses at the next
  check, every 6 hours.
- Queueing a replay that is already parsed returns it as is, unless it failed, its file has expired, or it
  was parsed in an older .curves format than the pinned parser writes.
- Parsed replays are stored gzipped and served with `Content-Encoding: gzip`, which the player unpacks.

## Deploy

Once:

1. A Discord application at https://discord.com/developers/applications, with
   `https://$DOMAIN/api/callback` (default bar-rewind.robotofham.com) as an OAuth2 redirect.
2. An R2 bucket, an Object Read & Write API token for it, and a lifecycle rule deleting `curves/` and
   `logs/` after 60 days (leave `db/` alone: it's the database backup).
3. A Cloudflare Tunnel (Zero Trust > Networks > Tunnels) whose public hostname is `$DOMAIN`, pointing at
   `http://localhost:8080`.

Then, with the AWS CLI signed in:

```sh
DISCORD_CLIENT_SECRET=... CLOUDFLARED_TOKEN=... R2_ENDPOINT=https://<account>.eu.r2.cloudflarestorage.com \
R2_BUCKET=... R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... ./deploy.sh you@example.com
```

The secrets go into SSM, so later runs need only `./deploy.sh you@example.com`. A deploy ships the
commit checked out here, pushed first: the box fetches it from GitHub. Who may sign in is `ROLE_IDS`
(Discord roles) and `USER_IDS` (Discord users), comma-separated, set at the top of `deploy.sh` or in the
environment; `ADMIN_IDS` may pin releases. `./teardown.sh` deletes the box and its settings; the R2 bucket
stays.

Pinning releases, as an admin, signed in:
`POST /api/pins {"parser": "v0.5.0", "worker": "v0.1.0", "curves_version": "11"}`. Workers on another
parser get no jobs until they've updated.

## Running it yourself

```sh
BAR_REWIND_SESSION_KEY=... BAR_REWIND_SITE_URL=http://localhost:8080 BAR_REWIND_R2_ENDPOINT=... \
BAR_REWIND_R2_BUCKET=... BAR_REWIND_R2_ACCESS_KEY_ID=... BAR_REWIND_R2_SECRET_ACCESS_KEY=... python -m server
python -m unittest discover -s tests -t .
```

Settings are `BAR_REWIND_*` environment variables (`BAR_REWIND_SITE_URL` is `site-url`) instead of SSM.
The server needs Python 3 and boto3; the tests need only Python.

## What it costs

A t4g.small (about $12/month) and its 20 GB disk (about $2). R2 storage is about $0.015/GB-month and
downloads are free. A monthly AWS Budgets alarm (default $25) emails you.

AI disclosure: written with assistance from Claude Code.
