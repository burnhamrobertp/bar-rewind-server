# Settings: SSM parameters under $PARAMS on the server (deploy.sh puts them there), or BAR_REWIND_*
# environment variables for a run on your own machine (BAR_REWIND_SITE_URL -> site-url).
import os

_cache = None


def load():
    global _cache
    if _cache is None:
        _cache = from_ssm(os.environ["PARAMS"]) if os.environ.get("PARAMS") else from_env(os.environ)

    return _cache


def from_ssm(path):
    import boto3
    ssm = boto3.client("ssm")
    values, kwargs = {}, {"Path": path, "WithDecryption": True}
    while True:
        page = ssm.get_parameters_by_path(**kwargs)
        values.update({p["Name"].rsplit("/", 1)[-1]: p["Value"] for p in page["Parameters"]})
        if "NextToken" not in page:
            return values
        kwargs["NextToken"] = page["NextToken"]


def from_env(env):
    prefix = "BAR_REWIND_"

    return {k[len(prefix):].lower().replace("_", "-"): v for k, v in env.items() if k.startswith(prefix)}


def get(name, default=None):
    return load().get(name, default)


def number(name, default):
    return int(get(name, default))


def ids(name):
    return {i.strip() for i in get(name, "").split(",") if i.strip()}


def site():
    return get("site-url").rstrip("/")
