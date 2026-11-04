# Parsed replays and parse logs in R2, through its S3 API. The server holds the only key: viewers and
# workers get presigned URLs for one object each.
import hashlib, hmac, json, time, urllib.parse
from pathlib import Path

from . import settings
from .web import Response, respond

store = None


class R2:
    def __init__(self):
        import boto3
        from botocore.config import Config
        self.bucket = settings.get("r2-bucket")
        self.s3 = boto3.client(
            "s3", endpoint_url=settings.get("r2-endpoint"), aws_access_key_id=settings.get("r2-access-key-id"),
            aws_secret_access_key=settings.get("r2-secret-access-key"),
            config=Config(signature_version="s3v4", region_name="auto"))

    def get_url(self, key, seconds):
        return self.s3.generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=seconds)

    def put_url(self, key, seconds, headers):
        """A presigned PUT and the headers the uploader must send with it, which the signature covers"""
        names = {"Content-Type": "ContentType", "Content-Encoding": "ContentEncoding", "Content-Disposition": "ContentDisposition"}
        params = {"Bucket": self.bucket, "Key": key, **{names[h]: v for h, v in headers.items()}}

        return self.s3.generate_presigned_url("put_object", Params=params, ExpiresIn=seconds)

    def size(self, key):
        try:
            return self.s3.head_object(Bucket=self.bucket, Key=key)["ContentLength"]
        except self.s3.exceptions.ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return None
            raise

    def read(self, key, first=0, last=None):
        span = {} if first == 0 and last is None else {"Range": f"bytes={first}-" + ("" if last is None else str(last))}

        return self.s3.get_object(Bucket=self.bucket, Key=key, **span)["Body"].read()

    def delete(self, key):
        self.s3.delete_object(Bucket=self.bucket, Key=key)


class Folder:
    """A folder standing in for R2 on a machine of your own (the dev-store setting): its presigned URLs
    point back at this server, under /api/dev-store/"""

    def __init__(self, path):
        self.root = Path(path).resolve()

    def sign(self, method, key, expires):
        message = f"{method} {key} {expires}".encode()

        return hmac.new(settings.get("session-key").encode(), message, hashlib.sha256).hexdigest()

    def url(self, method, key, seconds):
        expires = int(time.time()) + seconds
        query = urllib.parse.urlencode({"expires": expires, "sig": self.sign(method, key, expires)})

        return f"{settings.site()}/api/dev-store/{urllib.parse.quote(key)}?{query}"

    def get_url(self, key, seconds):
        return self.url("GET", key, seconds)

    def put_url(self, key, seconds, headers):
        return self.url("PUT", key, seconds)

    def path(self, key):
        path = (self.root / key).resolve()
        if self.root not in path.parents:
            raise ValueError("outside the store")

        return path

    def size(self, key):
        path = self.path(key)

        return path.stat().st_size if path.is_file() else None

    def read(self, key, first=0, last=None):
        with open(self.path(key), "rb") as f:
            f.seek(first)

            return f.read() if last is None else f.read(last + 1 - first)

    def delete(self, key):
        self.path(key).unlink(missing_ok=True)

    def serve(self, req):
        key = urllib.parse.unquote(req.path.removeprefix("/api/dev-store/"))
        expires = req.query.get("expires", "0")
        method = "GET" if req.method == "HEAD" else req.method
        if not expires.isdigit() or int(expires) < time.time() or not hmac.compare_digest(req.query.get("sig", ""), self.sign(method, key, expires)):
            return respond(403, {"error": "bad or expired signature"})
        path = self.path(key)
        kept = path.with_name(path.name + ".headers.json")
        if method == "PUT":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(req.body)
            kept.write_text(json.dumps({k: req.headers[k] for k in ("content-type", "content-encoding", "content-disposition") if k in req.headers}))

            return Response(200)
        if not path.is_file():
            return respond(404, {"error": "no such object"})

        return Response(200, path.read_bytes(), json.loads(kept.read_text()) if kept.is_file() else {})
