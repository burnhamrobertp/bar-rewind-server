#!/usr/bin/env python3
# BAR Rewind parse worker, one Fargate task: claims queued jobs from the jobs table, parses each replay
# with BAR-Rewind's pipeline, uploads the gzipped .curves to S3 and exits once the queue has stayed
# empty for IDLE_EXIT seconds. Engines, games and maps stay on the task's disk between its jobs.
# Env: TABLE, BUCKET, IDLE_EXIT (default 600).
#   worker.py --local <replay id>   parse one replay into /work/out, no AWS
import gzip, json, os, shutil, signal, subprocess, sys, threading, time, urllib.parse, urllib.request
from pathlib import Path

sys.path.insert(0, "/opt/bar-rewind/tools")
import pipeline  # noqa: E402

BAR = Path("/bar")
WORK = Path("/work")
CFG = {"bar_data": str(BAR), "out_dir": str(WORK / "out")}
REPLAY_API = "https://api.bar-rts.com/replays/"
DEMOS = "https://storage.uk.cloud.ovh.net/v1/AUTH_10286efc0d334efd917d476d7183232e/BAR/demos/"
ENGINES = "https://github.com/beyond-all-reason/RecoilEngine/releases/download/{0}/recoil_{0}_amd64-linux.7z"
HEARTBEAT = 30
PROGRESS_EVERY = 10
STALE = 300
MAX_ATTEMPTS = 3
USER_AGENT = "bar-rewind-server (https://github.com/burnhamrobertp/bar-rewind-server)"


class Interrupted(Exception):
    pass


def fetch(url, dest=None):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as r:
        if dest is None:
            return r.read()
        with open(dest, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)


# one job -------------------------------------------------------------------------------------------

def install_engine(version, report):
    if (BAR / "engine" / f"recoil_{version}").is_dir():
        return
    report("downloading engine " + version, 0)
    archive = WORK / "engine.7z"
    fetch(ENGINES.format(version), archive)
    subprocess.run(["7z", "x", "-y", f"-o{BAR / 'engine' / ('recoil_' + version)}", str(archive)],
                   check=True, stdout=subprocess.DEVNULL)
    archive.unlink()


def process(replay_id, report):
    """Parses one replay; returns the .curves path. `report(stage, pct)` is called as it goes."""
    report("looking up the replay", 0)
    info = json.loads(fetch(REPLAY_API + replay_id))
    demo = WORK / "demos" / info["fileName"]
    demo.parent.mkdir(parents=True, exist_ok=True)
    report("downloading the replay", 0)
    fetch(DEMOS + urllib.parse.quote(info["fileName"]), demo)
    install_engine(info["engineVersion"], report)

    missing = pipeline.demo_missing(str(BAR), pipeline.demo_info(demo))
    if missing:
        for event in pipeline.download(str(BAR), missing):
            if event[0] == "progress":
                report(event[2], event[1])
    for event in pipeline.parse(CFG, demo):
        if event[0] == "progress":
            report(event[2], event[1])
    demo.unlink()

    return pipeline.curves_path(CFG, demo)


def local(replay_id):
    BAR.mkdir(parents=True, exist_ok=True)
    started = time.time()
    curves = process(replay_id, lambda stage, pct: print(f"{time.time() - started:6.0f}s {pct:3}% {stage}", flush=True))
    print(f"{curves} {curves.stat().st_size >> 20} MB in {time.time() - started:.0f} s")


# the queue -----------------------------------------------------------------------------------------

class Queue:
    def __init__(self):
        import boto3
        self.table = boto3.resource("dynamodb").Table(os.environ["TABLE"])
        self.s3 = boto3.client("s3")
        self.bucket = os.environ["BUCKET"]
        self.task = os.environ.get("ECS_CONTAINER_METADATA_URI_V4", "local").rsplit("/", 1)[-1]

    def jobs(self, status):
        from boto3.dynamodb.conditions import Attr
        items, kwargs = [], {"FilterExpression": Attr("status").eq(status)}
        while True:
            page = self.table.scan(**kwargs)
            items += page["Items"]
            if "LastEvaluatedKey" not in page:
                return items
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def update(self, job_id, condition=None, **fields):
        names = {f"#{k}": k for k in fields}
        values = {f":{k}": v for k, v in fields.items()}
        kwargs = {"Key": {"id": job_id}, "UpdateExpression": "SET " + ", ".join(f"#{k} = :{k}" for k in fields),
                  "ExpressionAttributeNames": names, "ExpressionAttributeValues": values}
        if condition:
            kwargs["ConditionExpression"] = condition[0]
            names.update(condition[1])
            values.update(condition[2])
        try:
            self.table.update_item(**kwargs)
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            return False

        return True

    def requeue_stale(self):
        """Jobs whose worker stopped heartbeating (a spot reclaim, a crash) go back in the queue."""
        now = int(time.time())
        for job in self.jobs("running"):
            if now - int(job.get("heartbeat", 0)) < STALE:
                continue
            attempts = int(job.get("attempts", 1))
            stale = ("#status = :running AND #heartbeat = :seen", {"#status": "status", "#heartbeat": "heartbeat"},
                     {":running": "running", ":seen": job.get("heartbeat", 0)})
            if attempts >= MAX_ATTEMPTS:
                self.update(job["id"], stale, status="failed", error=f"the worker stopped responding {attempts} times")
            else:
                self.update(job["id"], stale, status="queued", stage="waiting for a worker (retrying)")

    def claim(self):
        for job in sorted(self.jobs("queued"), key=lambda j: j["created"]):
            now = int(time.time())
            if self.update(job["id"], ("#status = :queued", {"#status": "status"}, {":queued": "queued"}),
                           status="running", worker=self.task, started=now, heartbeat=now, progress_at=now,
                           attempts=int(job.get("attempts", 0)) + 1, stage="starting", pct=0):
                return job

        return None

    def run(self, job):
        job_id = job["id"]
        mine = ("#worker = :me", {"#worker": "worker"}, {":me": self.task})
        stop = threading.Event()

        def beat():
            while not stop.wait(HEARTBEAT):
                self.update(job_id, mine, heartbeat=int(time.time()))
        threading.Thread(target=beat, daemon=True).start()

        last = {"stage": None, "at": 0}

        def report(stage, pct):
            now = time.time()
            if stage == last["stage"] and now - last["at"] < PROGRESS_EVERY:
                return
            last.update(stage=stage, at=now)
            self.update(job_id, mine, stage=stage, pct=max(int(pct), 0), progress_at=int(now))

        try:
            curves = process(job_id, report)
            report("uploading", 100)
            packed = curves.with_suffix(".curves.gz")
            with open(curves, "rb") as src, gzip.open(packed, "wb", 6) as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            self.s3.upload_file(str(packed), self.bucket, f"curves/{job_id}.curves", ExtraArgs={
                "ContentEncoding": "gzip", "ContentType": "application/octet-stream",
                "ContentDisposition": f'attachment; filename="{job_id}.curves"'})
            self.update(job_id, mine, status="done", stage="done", pct=100, finished=int(time.time()),
                        size=curves.stat().st_size, packed_size=packed.stat().st_size)
            curves.unlink()
            packed.unlink()
        except Interrupted:
            self.update(job_id, mine, status="queued", stage="waiting for a worker (the last one was stopped)")
            raise
        except Exception as e:
            self.update(job_id, mine, status="failed", stage="failed", error=str(e)[:2000], finished=int(time.time()))
        finally:
            stop.set()
            log = WORK / "out" / "curves" / "dump.log"
            if log.exists():
                self.s3.upload_file(str(log), self.bucket, f"logs/{job_id}.log", ExtraArgs={"ContentType": "text/plain"})
                log.unlink()


def serve():
    queue = Queue()
    BAR.mkdir(parents=True, exist_ok=True)

    def interrupted(*_):
        raise Interrupted()
    signal.signal(signal.SIGTERM, interrupted)

    idle_exit = int(os.environ.get("IDLE_EXIT", "600"))
    idle_since = time.time()
    try:
        while time.time() - idle_since < idle_exit:
            queue.requeue_stale()
            job = queue.claim()
            if job is None:
                time.sleep(15)
                continue
            print(f"job {job['id']}", flush=True)
            queue.run(job)
            idle_since = time.time()
    except Interrupted:
        print("stopped by ECS", flush=True)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--local":
        local(sys.argv[2])
    else:
        serve()
