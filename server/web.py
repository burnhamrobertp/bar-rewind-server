# Requests and responses without a framework: the HTTP handler turns each request into a Request,
# routes it, and writes back the Response it gets.
import json


class Request:
    def __init__(self, method, path, query=None, headers=None, body=b""):
        self.method = method
        self.path = path
        self.query = query or {}
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.body = body
        self.cookies = dict(c.strip().split("=", 1) for c in self.headers.get("cookie", "").split(";") if "=" in c)

    def json(self):
        try:
            value = json.loads(self.body or b"{}")
        except ValueError:
            return {}

        return value if isinstance(value, dict) else {}


class Response:
    def __init__(self, status, body=b"", headers=None, cookies=()):
        self.status = status
        self.body = body
        self.headers = headers or {}
        self.cookies = list(cookies)

    def json(self):
        return json.loads(self.body)


def respond(status, body=None, cookies=()):
    return Response(status, json.dumps(body if body is not None else {}).encode(),
                    {"content-type": "application/json", "cache-control": "no-store"}, cookies)


def text(status, body):
    return Response(status, body.encode(), {"content-type": "text/plain; charset=utf-8", "cache-control": "no-store"})


def redirect(location, cookies=()):
    return Response(302, b"", {"location": location, "cache-control": "no-store"}, cookies)


def cookie(name, value, max_age):
    return f"{name}={value}; Path=/; Max-Age={max_age}; HttpOnly; Secure; SameSite=Lax"
