#!/usr/bin/env python3
# BAR Rewind parse worker: claims queued jobs from the jobs table, parses each replay with BAR-Rewind's
# pipeline, uploads the gzipped .curves to S3. A Fargate task exits once the queue has stayed empty for
# IDLE_EXIT seconds; with STAY=1 (a worker on someone's own machine) it keeps running and keeps the
# LOCAL_WORKER record fresh, and the API starts no Fargate task while that record is recent.
# Engines, games and maps stay on /bar between jobs.
# Env: TABLE, BUCKET, IDLE_EXIT (default 120), STAY.
#   worker.py --local <replay id>   parse one replay into /work/out, no AWS
import gzip, json, os, re, shutil, signal, subprocess, sys, threading, time, traceback, urllib.parse, urllib.request
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
LOCAL_WORKER = "_worker_local"
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


RECORDING_WIDGETS = {"Replay Dump v2", "Replay Dump Eco"}
BAR_WIDGETS = Path(__file__).with_name("bar-widgets.txt")


def interface_off():
    """BAR's interface widgets draw nothing in a headless parse yet took about a quarter of a long one:
    the worker's own BAR folder gets a widget config with only the recording widgets on, for the
    widgets listed with the worker and any a newer BAR added to the config since."""
    config = BAR / "LuaUI" / "Config" / "BYAR.lua"
    names = {n for n in BAR_WIDGETS.read_text().splitlines() if n}
    if config.exists():
        order = re.search(r"\n\torder\s*=\s*\{(.*?)\n\t\}", config.read_text(errors="replace"), re.S)
        if order:
            names |= set(re.findall(r'\["([^"]+)"\]\s*=\s*-?\d+', order.group(1)))
    entries = "\n".join(f'\t\t[{json.dumps(n)}] = {1 if n in RECORDING_WIDGETS else 0},' for n in sorted(names | RECORDING_WIDGETS))
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(f"return {{\n\tallowUserWidgets = true,\n\torder = {{\n{entries}\n\t}},\n}}\n")


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
    interface_off()
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
        self.stay = os.environ.get("STAY") == "1"

    def alive(self, seen=None):
        if self.stay:
            self.update(LOCAL_WORKER, seen=int(time.time()) if seen is None else seen, worker=self.task)

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
            attempt = int(job.get("attempts", 0)) + 1
            if self.update(job["id"], ("#status = :queued", {"#status": "status"}, {":queued": "queued"}),
                           status="running", worker=self.task, started=now, heartbeat=now, progress_at=now,
                           attempts=attempt, stage="starting", pct=0):
                return {**job, "attempts": attempt, "started": now}

        return None

    def run(self, job):
        job_id = job["id"]
        mine = ("#worker = :me", {"#worker": "worker"}, {":me": self.task})
        stop = threading.Event()

        def beat():
            while not stop.wait(HEARTBEAT):
                self.update(job_id, mine, heartbeat=int(time.time()))
                self.alive()
        threading.Thread(target=beat, daemon=True).start()

        last = {"stage": None, "at": 0}
        stages = []

        def report(stage, pct):
            now = time.time()
            if stage != last["stage"]:
                stages.append(f"{time.strftime('%H:%M:%S', time.gmtime(now))} {stage}")
            if stage == last["stage"] and now - last["at"] < PROGRESS_EVERY:
                return
            last.update(stage=stage, at=now)
            self.update(job_id, mine, stage=stage, pct=max(int(pct), 0), progress_at=int(now))

        outcome = "done"

        try:
            curves = process(job_id, report)
            report("uploading", 100)
            packed = curves.with_suffix(".curves.gz")
            with open(curves, "rb") as src, gzip.open(packed, "wb", 6) as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            # a name per parse: CloudFront and the players' download caches keep a name's first bytes
            key = f"curves/{job_id}-{int(job['started'])}.curves"
            self.s3.upload_file(str(packed), self.bucket, key, ExtraArgs={
                "ContentEncoding": "gzip", "ContentType": "application/octet-stream",
                "ContentDisposition": f'attachment; filename="{job_id}.curves"'})
            self.update(job_id, mine, status="done", stage="done", pct=100, finished=int(time.time()),
                        size=curves.stat().st_size, packed_size=packed.stat().st_size,
                        curves_version=pipeline.curves_version(curves), curves_key=key)
            for old in {job.get("curves_key"), f"curves/{job_id}.curves"} - {None, key}:
                self.s3.delete_object(Bucket=self.bucket, Key=old)
            curves.unlink()
            packed.unlink()
        except Interrupted:
            outcome = "stopped by ECS"
            self.update(job_id, mine, status="queued", stage="waiting for a worker (the last one was stopped)")
            raise
        except Exception as e:
            outcome = "failed\n\n" + traceback.format_exc()
            self.update(job_id, mine, status="failed", stage="failed", error=str(e)[:2000], finished=int(time.time()))
        finally:
            stop.set()
            self.save_log(job, stages, outcome)

    def save_log(self, job, stages, outcome):
        """One log per attempt (logs/<id>/<start time>-<attempt>.log): this worker's account of the job,
        then the engine's and bake's output if the parse got that far. The job keeps the latest one's key."""
        started = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(int(job["started"])))
        key = f"logs/{job['id']}/{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(int(job['started'])))}-{job['attempts']}.log"
        text = (f"replay {job['id']}, attempt {job['attempts']}, worker {self.task}\nstarted {started} UTC\n\n"
                + "\n".join(stages) + f"\n\n{outcome}\n")
        dump = WORK / "out" / "curves" / "dump.log"
        if dump.exists():
            text += "\n---- engine and bake output ----\n" + dump.read_text(errors="replace")
            dump.unlink()
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=text.encode(), ContentType="text/plain; charset=utf-8")
        self.update(job["id"], ("#worker = :me", {"#worker": "worker"}, {":me": self.task}), log=key)


def serve():
    queue = Queue()
    BAR.mkdir(parents=True, exist_ok=True)

    def interrupted(*_):
        raise Interrupted()
    signal.signal(signal.SIGTERM, interrupted)

    idle_exit = int(os.environ.get("IDLE_EXIT", "120"))
    idle_since = time.time()
    try:
        while queue.stay or time.time() - idle_since < idle_exit:
            queue.alive()
            queue.requeue_stale()
            job = queue.claim()
            if job is None:
                time.sleep(15)
                continue
            print(f"job {job['id']}", flush=True)
            queue.run(job)
            idle_since = time.time()
    except Interrupted:
        print("stopped", flush=True)
    finally:
        # gone: the API may start a Fargate worker right away
        queue.alive(seen=0)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--local":
        local(sys.argv[2])
    else:
        serve()
