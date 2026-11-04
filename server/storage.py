# Parsed replays and parse logs in R2, through its S3 API. The server holds the only key: viewers and
# workers get presigned URLs for one object each.
from . import settings

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

    def read(self, key):
        return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def delete(self, key):
        self.s3.delete_object(Bucket=self.bucket, Key=key)
