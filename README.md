# bar-rewind-server

A small AWS service that parses [Beyond All Reason](https://www.beyondallreason.info/) replays with
[BAR-Rewind](https://github.com/burnhamrobertp/BAR-Rewind) on demand, so moderators can rewind and scrub a
match without parsing it themselves. Paste a bar-rts.com replay link, wait a few minutes, click Watch.

Sign-in is Discord, and only members of the BAR Discord holding an allowed role get in.

## How it works

```
browser --CloudFront--> /bar-rewind/          site/index.html (S3)
                        /bar-rewind/api/*     api/app.py (Lambda): Discord sign-in, jobs, signed download links
                        /bar-rewind/curves/*  parsed replays (S3, signed URLs only)

POST /api/jobs --> DynamoDB jobs table --> Fargate Spot task (worker/), started when jobs wait
                                           fetches the demo, engine, game and map, runs BAR-Rewind's
                                           parse, gzips the .curves into S3 and reports progress
```

- One worker at a time drains the queue and exits after 10 idle minutes. Nothing runs while nothing is queued.
- Jobs report a stage, a percentage and a heartbeat; the page flags a job that stops moving. A job whose
  worker disappears (a Spot reclaim, a crash) goes back in the queue, up to 3 tries.
- Parsed replays, their logs and their jobs expire after 60 days (S3 lifecycle, DynamoDB TTL).
- `.curves` are stored gzipped and served with `Content-Encoding: gzip`: about 40% of the size crosses the wire.
- Watch hands BAR-Rewind's player (`bar-rewind.exe`, served by the site) a one-hour signed link. The player
  only downloads from this site's host, which is built into it by `deploy.sh`.

## What it costs

Rough us-east-1 prices, idle and per use:

| | |
|---|---|
| Idle | under $1/month: S3 storage, the worker image in ECR |
| A parse | Fargate Spot, 2 vCPU / 8 GB, a few cents an hour of parsing |
| Downloads | CloudFront's free tier covers 1 TB/month |
| Lambda, DynamoDB, SSM | inside their free tiers at this scale |

There is no NAT gateway and no always-on instance. A monthly AWS Budgets alarm (default $10) emails you.

## Deploy

The site is served at `https://<DOMAIN>/<BASE_PATH>/` (defaults: robotofham.com, bar-rewind); CloudFront
answers for the whole domain and sends `/` there.

1. Make a Discord application at https://discord.com/developers/applications. Its client id is the
   template's `DiscordClientId`; put its secret in SSM:
   ```sh
   aws ssm put-parameter --name /bar-rewind/discord-client-secret --type SecureString --value <secret>
   ```
2. With the AWS CLI signed in, Docker running, Zig 0.17 and Python 3 on PATH, and BAR-Rewind checked out
   next to this repo, run `./deploy.sh you@example.com`. On the first run it asks for a DNS record that
   proves the domain to AWS's certificate service, and waits until it is in place.
3. Point the domain at the CloudFront host it prints (a CNAME, DNS only if Cloudflare manages the domain),
   and add the redirect URL it prints to the Discord application's OAuth2 redirects.

Run `./deploy.sh you@example.com` again after any change. Allowed roles are the stack's `RoleIds`
parameter (comma-separated). `./teardown.sh` deletes everything, parsed replays included.

AI disclosure: written with assistance from Claude Code.
