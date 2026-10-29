"""Playback test CLI: real Chaoxing requests by default, or explicit dry-run/local mode."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable


EVENTS = {"playing": 0, "pause": 2, "play": 3, "ended": 4}
IDENTITY = {"clazzId": "demo-class", "userid": "test-user",
            "jobid": "demo-job", "objectId": "demo-video"}
# Deliberately independent from the observed platform signature.
TEST_KEY = b"loopback-playback-simulator-only-v1"


class ContractError(ValueError):
    pass


def finite(value: object, name: str, positive: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ContractError(f"{name} must be numeric") from exc
    if not math.isfinite(number) or (positive and number <= 0):
        raise ContractError(f"{name} must be finite" + (" and positive" if positive else ""))
    return number


def canonical(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def sign(payload: dict) -> str:
    body = {key: value for key, value in payload.items() if key != "enc"}
    return hmac.new(TEST_KEY, canonical(body), hashlib.sha256).hexdigest()


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


@dataclass
class PlaybackState:
    """Authoritative mock state; only received, valid playback advances earn credit."""
    duration: float
    clock: Callable[[], float] = time.monotonic
    position: float = 0.0
    watched: float = 0.0
    completed: bool = False
    active: bool = False
    last_at: float = 0.0
    seq: int = 0
    last_payload: bytes | None = None
    last_result: dict | None = None

    def __post_init__(self) -> None:
        self.duration = finite(self.duration, "duration", positive=True)
        self.last_at = self.clock()

    def snapshot(self) -> dict:
        return {"duration": self.duration, "position": round(self.position, 6),
                "watched": round(self.watched, 6), "completed": self.completed,
                "active": self.active, "seq": self.seq}

    def restore(self, saved: dict) -> None:
        if saved.get("version") != 1 or saved.get("identity") != IDENTITY:
            raise ContractError("incompatible checkpoint")
        if saved.get("duration") != self.duration:
            raise ContractError("checkpoint duration differs; use the original --duration")
        position = finite(saved.get("position"), "checkpoint position")
        watched = finite(saved.get("watched"), "checkpoint watched")
        if not 0 <= position <= self.duration or abs(watched - position) > 1e-5:
            raise ContractError("invalid checkpoint progress")
        completed = saved.get("completed")
        if not isinstance(completed, bool) or (completed and position != self.duration):
            raise ContractError("invalid checkpoint completion")
        self.position, self.watched, self.completed = position, watched, completed
        # A process restart never earns playback credit for the downtime.
        self.new_session()

    def new_session(self) -> None:
        self.active = False
        self.last_at = self.clock()
        self.seq = 0
        self.last_payload = self.last_result = None

    def accept(self, payload: dict) -> dict:
        if not isinstance(payload.get("enc"), str) or not hmac.compare_digest(payload["enc"], sign(payload)):
            raise ContractError("invalid test signature")
        if any(payload.get(key) != value for key, value in IDENTITY.items()):
            raise ContractError("wrong synthetic identity")
        if payload.get("duration") != self.duration or payload.get("dtype") != "Video":
            raise ContractError("wrong media contract")
        seq = payload.get("seq")
        if type(seq) is not int:
            raise ContractError("seq must be an integer")
        body = canonical(payload)
        if seq == self.seq and self.last_payload == body:
            return dict(self.last_result)  # Retry of the same request is idempotent.
        if seq != self.seq + 1:
            raise ContractError("out-of-order or modified duplicate request")
        event = payload.get("event")
        if event not in EVENTS or payload.get("isdrag") != EVENTS[event]:
            raise ContractError("unsupported playback event")
        position = finite(payload.get("playingTime"), "playingTime")
        if not 0 <= position <= self.duration:
            raise ContractError("position outside media")
        delta = position - self.position
        if delta < -1e-6:
            raise ContractError("backward position is unsupported")
        now = self.clock()
        elapsed = max(0.0, now - self.last_at)
        # Rounding tolerance only; no accumulated allowance that could fund a jump.
        if delta > (elapsed if self.active else 0.0) + 0.00001:
            raise ContractError("position exceeds elapsed active playback")
        if event == "play" and (self.active or delta > 1e-6):
            raise ContractError("play must resume the acknowledged position")
        if event != "play" and not self.active:
            raise ContractError("play is required before progress/pause/end")
        if self.completed:
            raise ContractError("media already completed")
        watched = min(self.duration, self.watched + max(0.0, delta))
        if event == "ended" and (position != self.duration or watched < self.duration - 1e-5):
            raise ContractError("ended before full playback")
        self.position, self.watched = position, watched
        self.active = event in ("play", "playing")
        self.completed = event == "ended"
        self.last_at, self.seq = now, seq
        self.last_payload = body
        self.last_result = self.snapshot()
        return dict(self.last_result)


class MockService:
    def __init__(self, folder: Path, duration: float, scale: float = 1,
                 resume: bool = False, fail_first: bool = False):
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True)
        self.scale = finite(scale, "time-scale", positive=True)
        self.lock = threading.RLock()
        self.state = PlaybackState(duration, lambda: time.monotonic() * self.scale)
        self.state_path = folder / "server-progress.json"
        if self.state_path.exists():
            if not resume:
                raise ContractError("state exists; pass --resume or choose a new --output directory")
            self.state.restore(json.loads(self.state_path.read_text(encoding="utf-8")))
        elif resume:
            raise ContractError("--resume requires an existing server-progress.json")
        self.token = ""
        self.fail_first = fail_first
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def save(self) -> None:
        atomic_json(self.state_path, {"version": 1, "identity": IDENTITY,
                                    **self.state.snapshot()})

    def _handler(self):
        service = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def reply(self, code: int, body: dict) -> None:
                data = canonical(body)
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                with service.lock:
                    if self.path == "/status":
                        self.reply(200, {"mock": True, "identity": IDENTITY,
                                         "timeScale": service.scale, **service.state.snapshot()})
                    elif self.path == "/progress":
                        self.reply(200, service.state.snapshot())
                    elif self.path.startswith("/mock/multimedia/log/"):
                        self.report()
                    else:
                        self.reply(404, {"error": "unknown local route"})

            def do_POST(self):
                with service.lock:
                    if self.path != "/session" or self.headers.get("Content-Length", "0") != "0":
                        self.reply(400, {"error": "only empty session creation is supported"})
                        return
                    service.state.new_session()
                    service.token = secrets.token_hex(16)
                    service.save()
                    self.reply(200, {"token": service.token, **service.state.snapshot()})

            def report(self):
                parsed = urllib.parse.urlsplit(self.path)
                if parsed.path != "/mock/multimedia/log/" + service.token or not service.token:
                    self.reply(403, {"error": "invalid local session token"})
                    return
                if len(parsed.query) > 8192:
                    self.reply(413, {"error": "query too large"})
                    return
                try:
                    query = urllib.parse.parse_qs(parsed.query, strict_parsing=True, max_num_fields=2)
                    if set(query) != {"payload"} or len(query["payload"]) != 1:
                        raise ContractError("expected one payload")
                    payload = json.loads(query["payload"][0])
                    if not isinstance(payload, dict):
                        raise ContractError("expected an object")
                    # Fail *after* committing once, to test retries of lost acknowledgements.
                    result = service.state.accept(payload)
                    service.save()
                    with (service.folder / "events.jsonl").open("a", encoding="utf-8") as log:
                        log.write(json.dumps({"request": payload, "result": result}, ensure_ascii=False) + "\n")
                    if service.fail_first:
                        service.fail_first = False
                        self.reply(503, {"error": "injected lost acknowledgement; safe to retry"})
                    else:
                        self.reply(200, result)
                except (ContractError, ValueError, TypeError, KeyError, OverflowError) as exc:
                    self.reply(400, {"error": str(exc)})

        return Handler

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise ContractError("redirects are disabled")


class LocalClient:
    def __init__(self, base: str, retries: int = 3):
        parsed = urllib.parse.urlsplit(base)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
                or parsed.username or parsed.password or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment or not parsed.port):
            raise ContractError("only explicit 127.0.0.1 HTTP endpoints are supported")
        self.base, self.retries = base.rstrip("/"), retries
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.retry_count = 0
        self.token = ""
        self.seq = 0

    def request(self, path: str, method: str = "GET") -> dict:
        if not path.startswith("/") or path.startswith("//"):
            raise ContractError("local relative route required")
        for attempt in range(self.retries + 1):
            try:
                req = urllib.request.Request(self.base + path, method=method,
                                             data=b"" if method == "POST" else None)
                with self.opener.open(req, timeout=3) as response:
                    raw = response.read(65537)
                    if len(raw) > 65536:
                        raise ContractError("oversized response")
                    result = json.loads(raw)
                    if not isinstance(result, dict) or "error" in result:
                        raise ContractError("invalid acknowledgement")
                    return result
            except urllib.error.HTTPError as exc:
                detail = exc.read(4096).decode("utf-8", errors="replace")
                if exc.code < 500 or attempt == self.retries:
                    raise ContractError(f"local HTTP {exc.code}: {detail}") from exc
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
                if attempt == self.retries:
                    raise
            self.retry_count += 1
            time.sleep(0.05 * 2 ** attempt)
        raise AssertionError("unreachable")

    def session(self) -> dict:
        result = self.request("/session", "POST")
        self.token, self.seq = result["token"], 0
        return result

    def send(self, event: str, position: float, duration: float) -> dict:
        payload = {**IDENTITY, "seq": self.seq + 1, "event": event,
                   "isdrag": EVENTS[event], "playingTime": round(position, 6),
                   "duration": duration, "dtype": "Video", "view": "pc",
                   "_t": time.time_ns() // 1_000_000}
        payload["enc"] = sign(payload)
        query = urllib.parse.urlencode({"payload": canonical(payload).decode("utf-8")})
        result = self.request("/mock/multimedia/log/" + self.token + "?" + query)
        if result.get("seq") != payload["seq"] or abs(result.get("position", -1) - payload["playingTime"]) > 1e-5:
            raise ContractError("acknowledgement differs from sent progress")
        self.seq = result["seq"]
        return result


def run(folder: Path, duration: float = 20, interval: float = 5,
        scale: float = 1, resume: bool = False, pause_at: float | None = None,
        pause_for: float = 2, stop_at: float | None = None,
        fail_first: bool = False, quiet: bool = False) -> dict:
    duration, interval = finite(duration, "duration", True), finite(interval, "interval", True)
    scale, pause_for = finite(scale, "time-scale", True), finite(pause_for, "pause-for", True)
    for name, value in (("pause-at", pause_at), ("stop-at", stop_at)):
        if value is not None and not 0 < finite(value, name) < duration:
            raise ContractError(f"{name} must be strictly between 0 and duration")
    with MockService(folder, duration, scale, resume, fail_first) as server:
        client = LocalClient(server.url)
        status = client.request("/status")
        if status.get("mock") is not True or status.get("identity") != IDENTITY:
            raise ContractError("unexpected local service")
        current = client.session()
        position = current["position"]
        paused_once = pause_at is None or position >= pause_at
        anchor = time.monotonic()
        anchor_position = position

        def publish(event: str, value: float) -> dict:
            result = client.send(event, value, duration)
            checked = client.request("/progress")
            if checked != result:
                raise ContractError("server progress check differs from acknowledgement")
            atomic_json(folder / "client-checkpoint.json", {"version": 1, **checked})
            if not quiet:
                print(f"{event:7s} {checked['position']:7.3f}/{duration:g}s "
                      f"({100 * checked['position'] / duration:5.1f}%) "
                      f"credited={checked['watched']:.3f}s checked", flush=True)
            return checked

        try:
            if not current["completed"]:
                current = publish("play", position)
                anchor, anchor_position = time.monotonic(), position
            while not current["completed"]:
                if stop_at is not None and position >= stop_at:
                    current = publish("pause", position)
                    break
                if not paused_once and position >= pause_at:
                    current = publish("pause", position)
                    before = client.request("/progress")
                    time.sleep(pause_for / scale)
                    if client.request("/progress") != before:
                        raise ContractError("progress changed while paused")
                    current = publish("play", position)
                    anchor, anchor_position = time.monotonic(), position
                    paused_once = True
                target = min(duration, position + interval)
                if not paused_once:
                    target = min(target, pause_at)
                if stop_at is not None:
                    target = min(target, stop_at)
                deadline = anchor + (target - anchor_position) / scale
                time.sleep(max(0, deadline - time.monotonic()))
                current = publish("ended" if target == duration else "playing", target)
                position = current["position"]
                # Start the next interval after its acknowledgement, so network
                # and file I/O time cannot make progress outrun the mock clock.
                anchor, anchor_position = time.monotonic(), position
        except KeyboardInterrupt:
            # SIGINT can arrive after the server commits but before the client
            # receives its acknowledgement. Reconcile sequence and position first.
            current = client.request("/progress")
            client.seq = current["seq"]
            if current["active"] and not current["completed"]:
                current = publish("pause", current["position"])
            else:
                atomic_json(folder / "client-checkpoint.json", {"version": 1, **current})
            if not quiet:
                print("Interrupted: acknowledged position saved; restart with --resume.", flush=True)
        summary = {"mockOnly": True, "timeScale": scale, "retries": client.retry_count,
                   "progressChecks": client.seq, **current}
        summary.pop("token", None)
        atomic_json(folder / "summary.json", summary)
        return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Chaoxing request client. Default: send real requests. Use --dry-run to preview.")
    parser.add_argument("--backend", choices=("chaoxing", "local"), default="chaoxing")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="preview remote endpoint without loading cookies or connecting")
    mode.add_argument("--send", action="store_true", help="compatibility flag; real requests are already the default")
    parser.add_argument("--cookie-file", type=Path, default=Path(__file__).parent.parent / "cookie.txt")
    parser.add_argument("--capture-dir", type=Path, default=Path(__file__).parent.parent / "analysis" / "private")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--duration", type=float, help="local backend only; real duration comes from media status")
    parser.add_argument("--interval", type=float, help="real mode defaults to card configuration; local mode to 5s")
    parser.add_argument("--time-scale", type=float, default=1, help="local client AND mock clock multiplier")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--pause-at", type=float)
    parser.add_argument("--pause-for", type=float, default=2)
    parser.add_argument("--stop-at", type=float, help="pause and exit for a later --resume")
    parser.add_argument("--fail-first", action="store_true", help="inject one lost acknowledgement (503)")
    args = parser.parse_args()
    try:
        if args.backend == "chaoxing":
            from chaoxing_client import CourseContext, Transport, preview, run_remote
            if args.duration is not None or args.time_scale != 1 or args.fail_first:
                raise ContractError("Real mode uses media duration and 1x wall time; local fault injection is unavailable")
            context = CourseContext.load(args.capture_dir)
            if not args.dry_run:
                result = run_remote(context, Transport(args.cookie_file),
                    args.output or Path(__file__).parent / "chaoxing-state", resume=args.resume,
                    interval=args.interval, pause_at=args.pause_at, pause_for=args.pause_for, stop_at=args.stop_at)
            else:
                result = preview(context)
        else:
            if args.send or args.dry_run:
                raise ContractError("--send and --dry-run are for the chaoxing backend")
            result = run(folder=args.output or Path(__file__).parent / "run-state",
                         duration=20 if args.duration is None else args.duration,
                         interval=5 if args.interval is None else args.interval,
                         scale=args.time_scale, resume=args.resume, pause_at=args.pause_at,
                         pause_for=args.pause_for, stop_at=args.stop_at, fail_first=args.fail_first)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ContractError, OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
