# bar-rewind-server

A small AWS service that parses [Beyond All Reason](https://www.beyondallreason.info/) replays with
[BAR-Rewind](https://github.com/burnhamrobertp/BAR-Rewind) on demand, so moderators can rewind and scrub a
match without parsing it themselves. Paste a bar-rts.com replay link, wait a few minutes, click Watch.

Sign-in is Discord, and only members of the BAR Discord holding an allowed role get in.

## How it works

```
browser --CloudFront--> /          site/index.html (S3)
                        /api/*     api/app.py (Lambda): Discord sign-in, jobs, signed download links
                        /curves/*  parsed replays (S3, signed URLs only)

POST /api/jobs --> DynamoDB jobs table --> Fargate Spot task (worker/), started when jobs wait
                                           fetches the demo, engine, game and map, runs BAR-Rewind's
                                           parse, gzips the .curves into S3 and reports progress
```

- One worker at a time drains the queue and exits after 2 idle minutes. Nothing runs while nothing is queued.
- Jobs report a stage, a percentage and a heartbeat; the page flags a job that stops moving. A job whose
  worker disappears (a Spot reclaim, a crash) goes back in the queue, up to 3 tries.
- Parsed replays, their logs and their jobs expire after 60 days (S3 lifecycle, DynamoDB TTL).
- Queueing a replay that is already parsed returns it as is, unless it failed, its file has expired, or
  it was parsed in an older .curves format than the deployed worker writes; those are parsed again.
- `.curves` are stored gzipped and served with `Content-Encoding: gzip`: about 40% of the size crosses the wire.
- Watch hands BAR-Rewind's player (Windows and Linux builds, served by the site) a one-hour signed link. The
  first link from this site asks the moderator whether to trust it; the player downloads from trusted hosts only.

## What it costs

Rough us-east-1 prices, idle and per use:

| | |
|---|---|
| Idle | under $1/month: S3 storage, the worker image in ECR |
| A parse | Fargate Spot, 2 vCPU / 16 GB, a few cents an hour of parsing |
| Downloads | CloudFront's free tier covers 1 TB/month |
| Lambda, DynamoDB, SSM | inside their free tiers at this scale |

There is no NAT gateway and no always-on instance. A monthly AWS Budgets alarm (default $10) emails you.

## Deploy

The site is served at `https://$DOMAIN/` (default bar-rewind.robotofham.com), a host of its own.

1. Make a Discord application at https://discord.com/developers/applications. Its client id is the
   template's `DiscordClientId`; put its secret in SSM:
   ```sh
   aws ssm put-parameter --name /bar-rewind/discord-client-secret --type SecureString --value <secret>
   ```
2. With the AWS CLI signed in, Docker running, Zig 0.17 and Python 3 on PATH, and BAR-Rewind checked out
   next to this repo, run `./deploy.sh you@example.com`. On the first run it asks for a DNS record that
   proves the domain to AWS's certificate service, and waits until it is in place.
3. Point the host at the CloudFront host it prints (a CNAME, DNS only if Cloudflare manages the domain),
   and add the redirect URL it prints to the Discord application's OAuth2 redirects.

Run `./deploy.sh you@example.com` again after any change. Who may sign in is `ROLE_IDS` (Discord roles) and
`USER_IDS` (Discord users, whatever their roles), comma-separated, set at the top of `deploy.sh` or in the
environment. Each request checks with Discord again at most every 5 minutes, so losing the role ends access
within that time. `./teardown.sh` deletes everything, parsed replays included.

## A worker on your own machine

A desktop CPU parses several times faster than Fargate (Esker Creek, a 39-minute team game: about 9
minutes on a Ryzen 7 5800X against 32 on Fargate). `./local-worker.sh` runs the same worker image under
Docker on this machine; while it runs, the API leaves Fargate alone, and it takes the queue back about two
minutes after the local worker stops. The container sees only its own access key (`~/.aws/bar-rewind-worker`,
limited to the jobs table and the bucket's curves and logs) and keeps engines, games and maps in the Docker
volume `bar-rewind-bar`. `./local-worker.sh stop` stops it; `./local-worker.sh key` replaces its access key.

`./local-worker.sh widgets <folder>` runs the `.lua` widgets in that folder in every parse (remembered across
restarts; `widgets none` forgets it). Deploying with `CLOUD_WORKER=off` stops Fargate parsing altogether:
queued jobs then wait for a local worker (`CLOUD_WORKER=on` turns it back on).

AI disclosure: written with assistance from Claude Code.
