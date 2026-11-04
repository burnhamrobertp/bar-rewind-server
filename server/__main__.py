# python -m server: serves the site and API on HOST:PORT (default 127.0.0.1:8080, where cloudflared
# reaches it), keeping state in $DB, and tidies jobs and workers once a minute.
import os, sys, threading, time, traceback, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import app, db, settings, storage, workers
from .web import Request, respond

MAX_BODY = 1 << 20


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 60

    def handle_any(self):
        url = urllib.parse.urlsplit(self.path)
        if self.headers.get("transfer-encoding"):
            self.close_connection = True

            return self.send(respond(411, {"error": "send a Content-Length, not a chunked body"}))
        length = self.headers.get("content-length") or "0"
        if not length.isdigit() or (int(length) > MAX_BODY and not (url.path.startswith("/api/dev-store/") and isinstance(storage.store, storage.Folder))):
            self.close_connection = True

            return self.send(respond(413, {"error": "too large"}))
        body = self.rfile.read(int(length)) if int(length) else b""
        headers = {k.lower(): v for k, v in self.headers.items()}
        headers["cookie"] = "; ".join(self.headers.get_all("cookie") or [])
        req = Request(self.command, url.path, dict(urllib.parse.parse_qsl(url.query)), headers, body)
        try:
            response = app.route(req)
        except Exception:
            traceback.print_exc()
            response = respond(500, {"error": "the server hit a problem"})
        self.send(response)

    def send(self, response):
        self.send_response(response.status)
        for k, v in response.headers.items():
            self.send_header(k, v)
        for c in response.cookies:
            self.send_header("set-cookie", c)
        self.send_header("content-length", str(len(response.body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(response.body)

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = handle_any

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))


def tidy():
    while True:
        time.sleep(60)
        try:
            workers.tidy(int(time.time()))
        except Exception:
            traceback.print_exc()


def main():
    db.init(os.environ.get("DB", "bar-rewind.db"))
    storage.store = storage.Folder(settings.get("dev-store")) if settings.get("dev-store") else storage.R2()
    threading.Thread(target=tidy, daemon=True).start()
    server = ThreadingHTTPServer((os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8080"))), Handler)
    server.daemon_threads = True
    print(f"serving on {server.server_address[0]}:{server.server_address[1]}", flush=True)
    server.serve_forever()


main()
