"""Real Chaoxing transport. Construction tests never require a network connection.

The CLI sends real requests by default; --dry-run explicitly previews offline.
Local elapsed playback is not proof of server-credited learning time.
"""
from __future__ import annotations

import hashlib
import http.cookiejar
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from .simulator import ContractError, EVENTS, atomic_json, finite
except ImportError:  # Direct CLI invocation.
    from simulator import ContractError, EVENTS, atomic_json, finite


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CAPTURE = ROOT / "analysis" / "private"
DEFAULT_COOKIE = ROOT / "cookie.txt"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/156.0.0.0 Safari/537.36"


class RemoteError(ContractError):
    pass


def authorized_url(url: str) -> str:
    try:
        p = urllib.parse.urlsplit(url)
        host = p.hostname or ""
        valid = (p.scheme == "https" and (host == "chaoxing.com" or host.endswith(".chaoxing.com"))
                 and p.port in (None, 443) and not p.username and not p.password
                 and not p.fragment and not any(c in url for c in "\r\n"))
    except ValueError:
        valid = False
    if not valid:
        raise RemoteError("URL must be an HTTPS Chaoxing endpoint")
    return url


class RejectRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise RemoteError("Server redirected the request; refresh the login/capture context")


def cookie_jar(path: Path) -> http.cookiejar.CookieJar:
    return cookie_jar_from_text(path.read_text(encoding="utf-8-sig"))


def cookie_jar_from_text(raw: str) -> http.cookiejar.CookieJar:
    """Parse supported credential formats into an in-memory CookieJar."""
    raw = raw.lstrip("\ufeff").strip()
    entries = []
    try:
        if raw.startswith(("[", "{")):
            data = json.loads(raw)
            entries = data if isinstance(data, list) else data.get("cookies", [])
        elif "\t" in raw:
            for line in raw.splitlines():
                if line.startswith("#HttpOnly_"):
                    line = line[len("#HttpOnly_"):]
                elif line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) == 7:
                    domain, _, cookie_path, _, _, name, value = parts
                    entries.append({"name": name, "value": value, "domain": domain, "path": cookie_path})
        else:
            raw = re.sub(r"^Cookie\s*:\s*", "", raw, flags=re.I)
            for part in raw.replace("\r", "").replace("\n", "").split(";"):
                if "=" in part:
                    name, value = part.strip().split("=", 1)
                    entries.append({"name": name, "value": value})
        jar = http.cookiejar.CookieJar(http.cookiejar.DefaultCookiePolicy(
            strict_ns_domain=http.cookiejar.DefaultCookiePolicy.DomainStrictNonDomain))
        for c in entries:
            domain = c.get("domain", ".chaoxing.com").lower()
            if not (domain.lstrip(".") == "chaoxing.com" or domain.lstrip(".").endswith(".chaoxing.com")):
                continue
            name, value = str(c["name"]), str(c["value"])
            if not name or any(x in name + value for x in "\r\n"):
                raise RemoteError("Invalid cookie format")
            jar.set_cookie(http.cookiejar.Cookie(
                0, name, value, None, False, domain, domain.startswith("."), domain.startswith("."),
                c.get("path", "/"), True, True, None, True, None, None, {}))
        if not len(jar):
            raise RemoteError("No usable Chaoxing cookies found")
        return jar
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise RemoteError("Cannot parse cookie file; contents are not logged") from exc


class Transport:
    def __init__(self, cookies: Path | None = None, *, opener=None, sleeper=time.sleep, jar=None):
        self.opener = opener if opener is not None else urllib.request.build_opener(
            urllib.request.ProxyHandler({}), RejectRedirect(),
            urllib.request.HTTPCookieProcessor(jar if jar is not None else cookie_jar(cookies or DEFAULT_COOKIE)))
        self.sleeper = sleeper

    def get(self, url: str, referer: str, *, mutating: bool = False, as_json: bool = True):
        authorized_url(url)
        authorized_url(referer)
        # Progress GETs mutate state. Their idempotency is unknown: never retry them.
        attempts = 1 if mutating else 3
        for attempt in range(attempts):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA,
                    "Referer": referer, "Accept": "application/json, text/html;q=0.9",
                    "Content-Type": "application/json"})
                with self.opener.open(req, timeout=20) as response:
                    if response.status != 200:
                        raise RemoteError(f"Unexpected HTTP {response.status}")
                    authorized_url(response.geturl())
                    data = response.read(4_000_001)
                    if len(data) > 4_000_000:
                        raise RemoteError("Server response exceeds size limit")
                text = data.decode("utf-8-sig", errors="replace")
                if not as_json:
                    return text
                try:
                    result = json.loads(text)
                except ValueError as exc:
                    raise RemoteError("Expected JSON, received another format; login/verification may be required") from exc
                if not isinstance(result, dict):
                    raise RemoteError("Expected a JSON response object")
                return result
            except urllib.error.HTTPError as exc:
                code = exc.code
                exc.close()
                if code < 500 or attempt == attempts - 1:
                    hint = "; login, permission or study limit requires review" if code in (401, 403) else ""
                    raise RemoteError(f"Remote HTTP {code}{hint}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
                if attempt == attempts - 1:
                    raise RemoteError("Remote transport failed; progress outcome may be unknown; no automatic replay") from None
            self.sleeper(0.25 * (2 ** attempt))
        raise AssertionError("unreachable")


def js_string(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def card_setting(html: str) -> dict:
    for match in re.finditer(r"\bmArg\s*=\s*", html):
        try:
            value, _ = json.JSONDecoder().raw_decode(html[match.end():])
            if isinstance(value, dict) and isinstance(value.get("attachments"), list) and "defaults" in value:
                return value
        except ValueError:
            continue
    raise RemoteError("No JSON mArg card configuration found; login/verification or page format changed")


def video_attachment(setting: dict, object_id: str | None = None) -> dict:
    choices = [a for a in setting.get("attachments", [])
               if a.get("type") == "video" and (object_id is None or a.get("objectId") == object_id)]
    if len(choices) != 1:
        raise RemoteError("Expected exactly one matching video attachment")
    return choices[0]


def validate_setting(setting: dict) -> None:
    defaults = setting["defaults"]
    if not setting.get("control") or defaults.get("state") in (1, "1") or defaults.get("isFiled") in (1, "1"):
        raise RemoteError("Card configuration disables progress reporting")
    if defaults.get("chapterCapture") in (1, "1") or any(defaults.get(k) for k in
            ("supportHeartbeat", "supportVideoPluginV1", "requestConfigUrl", "isSupportFace")):
        raise RemoteError("This card requires a verification/plugin flow not implemented by this client")


class CourseContext:
    def __init__(self, setting: dict, media: dict, study: dict, object_id: str | None = None):
        self.setting, self.media, self.study = setting, media, study
        self.attachment = video_attachment(setting, object_id)
        self.validate()

    @classmethod
    def load(cls, directory: Path):
        def read(name):
            return json.loads((directory / name).read_text(encoding="utf-8"))
        return cls(read("attachment-setting.json"), read("media-status-with-referer.json"),
                   read("study-context.json"))

    def validate(self):
        validate_setting(self.setting)
        duration = finite(self.media.get("duration"), "media duration", True)
        if not duration.is_integer() or self.media.get("status") != "success" or not self.media.get("dtoken"):
            raise RemoteError("Expected successful media status with integer duration and token")
        self.duration = int(duration)
        if self.media.get("objectid") and self.media["objectid"] != self.attachment["objectId"]:
            raise RemoteError("Media status belongs to another video")
        self.interval = finite(self.setting["defaults"].get("reportTimeInterval") or 60, "interval", True)
        if self.attachment.get("randomFaceCaptureTimeList"):
            raise RemoteError("Interactive face verification is required; use the normal player")
        self.report_base()  # Validate destination before loading credentials or connecting.
        authorized_url(self.study["url"])

    @property
    def fingerprint(self) -> str:
        d = self.setting["defaults"]
        fields = [d["clazzId"], d["userid"], d["courseid"], d["knowledgeid"],
                  self.attachment["objectId"], self.attachment.get("property", {}).get("jobid")
                  or self.attachment.get("jobid", "")]
        return hashlib.sha256(json.dumps(fields).encode()).hexdigest()

    def report_base(self) -> str:
        base = self.setting["defaults"]["reportUrl"]
        if base.startswith("http://"):
            base = "https://" + base[7:]
        authorized_url(base)
        p = urllib.parse.urlsplit(base)
        if p.query or not re.fullmatch(r"/mooc-ans/multimedia/log/[A-Za-z0-9_/-]+", p.path):
            raise RemoteError("Unexpected reportUrl path in card configuration")
        return base.rstrip("/")

    @property
    def player_referer(self) -> str:
        return urllib.parse.urljoin(self.report_base(), "/ananas/modules/video/index.html")

    def cards_url(self) -> str:
        if self.study.get("cards_url"):
            return authorized_url(self.study["cards_url"])
        d = self.setting["defaults"]
        query = {"clazzid": d["clazzId"], "courseid": d["courseid"],
                 "knowledgeid": d["knowledgeid"], "num": "0", "ut": "s",
                 "cpi": d.get("cpi") or self.study["cpi"], "mooc2": "1"}
        return urllib.parse.urljoin(self.report_base(), "/mooc-ans/knowledge/cards") + "?" + urllib.parse.urlencode(query)

    def refresh(self, transport: Transport) -> None:
        object_id = self.attachment["objectId"]
        setting = card_setting(transport.get(self.cards_url(), self.study["url"], as_json=False))
        att = video_attachment(setting, object_id)
        validate_setting(setting)
        d = setting["defaults"]
        base = urllib.parse.urljoin(self.report_base(), "/ananas/status/" + urllib.parse.quote(object_id, safe=""))
        media = transport.get(base + "?" + urllib.parse.urlencode({"k": d["fid"], "flag": "normal", "ro": "0"}),
                              self.player_referer)
        old_fingerprint = self.fingerprint
        candidate = CourseContext(setting, media, self.study, object_id)
        if candidate.fingerprint != old_fingerprint:
            raise RemoteError("Refreshed card identity changed; refusing to combine contexts")
        self.setting, self.media, self.attachment = setting, media, att
        self.validate()

    def build(self, event: str, position: float, *, timestamp_ms: int | None = None) -> tuple[str, dict]:
        if event not in EVENTS:
            raise RemoteError("Unsupported event")
        position = finite(position, "playingTime")
        if not 0 <= position <= self.duration:
            raise RemoteError("Position outside media")
        position = int(position)
        if event == "pause" and position == self.duration:
            raise RemoteError("The observed player suppresses pause at the media end")
        d, a = self.setting["defaults"], self.attachment
        prop = a.get("property", {})
        start = prop.get("v_begin", prop.get("vbegin", 0)) or 0
        end = prop.get("v_end", prop.get("vend", self.duration)) or self.duration
        if float(start) != 0 or float(end) != self.duration:
            raise RemoteError("Partial media clips are not implemented by this playback scheduler")
        clip = js_string(start) + "_" + js_string(end)
        job = prop.get("jobid") or a.get("jobid") or ""
        values = [d["clazzId"], d["userid"], job, a["objectId"], position * 1000,
                  "d_yHJ!$pdA~5", self.duration * 1000, clip]
        enc = hashlib.md5("".join("[" + js_string(v) + "]" for v in values).encode("utf-8")).hexdigest()
        params = {"clazzId": d["clazzId"], "playingTime": position, "duration": self.duration,
                  "clipTime": clip, "objectId": a["objectId"], "otherInfo": a["otherInfo"],
                  "jobid": job, "userid": d["userid"], "isdrag": EVENTS[event], "view": "pc",
                  "enc": enc, "rt": prop.get("rt") or 0.9, "videoFaceCaptureEnc": a.get("videoFaceCaptureEnc", ""),
                  "dtype": "Video", "_t": timestamp_ms if timestamp_ms is not None else time.time_ns() // 1_000_000,
                  "attDuration": a.get("attDuration", 0), "attDurationEnc": a.get("attDurationEnc", ""),
                  "courseEngineInfo": False}
        # The observed player concatenates values directly. In particular,
        # otherInfo can contain '&courseId=...', which becomes a separate field.
        # Encoding otherInfo as one form value changes the server-visible request.
        raw_query = "&".join(k + "=" + js_string(v) for k, v in params.items())
        query = urllib.parse.quote(raw_query, safe="!$&'()*+,-./:;=?@_~%[]")
        url = self.report_base() + "/" + urllib.parse.quote(str(self.media["dtoken"]), safe="") + "?" + query
        return authorized_url(url), params


def checked_response(response: dict) -> dict:
    # No eval: unknown JavaScript object literals are rejected, not executed.
    passed = response.get("isPassed")
    if type(passed) is not bool:
        raise RemoteError("Response does not contain a boolean isPassed; progress outcome is unknown")
    result = {"isPassed": passed, "hasJobLimit": response.get("hasJobLimit") is True,
              "videoTimeLimit": response.get("videoTimeLimit") is True}
    return result


def preview(context: CourseContext) -> dict:
    url, _ = context.build("play", 0)
    return {"mode": "remote-preview-no-network", "method": "GET",
            "endpoint": "https://" + urllib.parse.urlsplit(context.report_base()).netloc
                        + "/mooc-ans/multimedia/log/a/{userid}/{dtoken}",
            "duration": context.duration, "reportInterval": context.interval,
            "parameterNames": [key for key, _ in urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query,
                                                                        keep_blank_values=True)],
            "readsCookie": False, "serverCreditedSeconds": None}


def run_remote(context: CourseContext, transport: Transport, folder: Path, *, resume: bool = False,
               interval: float | None = None, pause_at: float | None = None, pause_for: float = 2,
               stop_at: float | None = None, clock=time.monotonic, sleeper=time.sleep, quiet=False,
               stop_event=None, on_update=None) -> dict:
    if stop_event is not None and stop_event.is_set():
        return {"stopped": True, "serverIsPassed": None}
    context.refresh(transport)
    interval = context.interval if interval is None else finite(interval, "interval", True)
    if interval < context.interval:
        raise RemoteError("Report interval must not be shorter than the card configuration")
    pause_for = finite(pause_for, "pause-for", True)
    for name, value in (("pause-at", pause_at), ("stop-at", stop_at)):
        if value is not None and (not finite(value, name).is_integer() or not 0 < value < context.duration):
            raise RemoteError(f"{name} must be an integer position strictly inside the media")
    path = folder / "remote-checkpoint.json"
    state = {"version": 1, "backend": "chaoxing", "fingerprint": context.fingerprint,
             "duration": context.duration, "lastResponsePosition": 0, "serverIsPassed": None,
             "serverCreditedSeconds": None, "reportsWithValidResponse": 0, "pendingEvent": None}
    if path.exists():
        if not resume:
            raise RemoteError("Remote checkpoint exists; use --resume or another --output")
        saved = json.loads(path.read_text(encoding="utf-8"))
        if any(saved.get(k) != state[k] for k in ("version", "backend", "fingerprint", "duration")):
            raise RemoteError("Remote checkpoint belongs to another media/context")
        position = finite(saved.get("lastResponsePosition"), "checkpoint position")
        if not position.is_integer() or not 0 <= position <= context.duration:
            raise RemoteError("Invalid remote checkpoint position")
        state = saved
    elif resume:
        raise RemoteError("No remote checkpoint to resume")
    if state.get("serverIsPassed") is True:
        return state
    position = state["lastResponsePosition"]
    if position >= context.duration:
        raise RemoteError("End was reported without completion confirmation; inspect the normal course page")
    state["pendingEvent"] = None  # Resume replays time from last valid response, not from an uncertain request.
    atomic_json(path, state)

    def publish(event, value):
        nonlocal state
        url, _ = context.build(event, value)
        state["pendingEvent"] = {"event": event, "position": int(value)}
        atomic_json(path, state)
        if on_update:
            on_update(dict(state))
        body = transport.get(url, context.player_referer, mutating=True)
        result = checked_response(body)
        state.update(lastResponsePosition=int(value), serverIsPassed=result["isPassed"], pendingEvent=None,
                     lastEvent=event, lastResponseAt=time.time_ns() // 1_000_000,
                     reportsWithValidResponse=state["reportsWithValidResponse"] + 1)
        atomic_json(path, state)
        if on_update:
            on_update(dict(state))
        with (folder / "remote-events.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps({"event": event, "position": int(value), "response": result,
                                  "serverCreditedSeconds": None}, ensure_ascii=False) + "\n")
        if not quiet:
            print(f"{event:7s} local-position={int(value)}/{context.duration}s "
                  f"server-isPassed={result['isPassed']} server-credited-seconds=unknown", flush=True)
        if result["hasJobLimit"] or result["videoTimeLimit"]:
            raise RemoteError("Server reported a study/task limit; stopped")

    paused_once = pause_at is None or position >= pause_at
    try:
        if stop_event is not None and stop_event.is_set():
            state["stopped"] = True
            atomic_json(path, state)
            return state
        publish("play", position)
        while state["serverIsPassed"] is not True and position < context.duration:
            if stop_event is not None and stop_event.is_set():
                if state.get("lastEvent") != "pause":
                    publish("pause", position)
                state["stopped"] = True
                atomic_json(path, state)
                return state
            if stop_at is not None and position >= stop_at:
                publish("pause", position)
                break
            if not paused_once and position >= pause_at:
                publish("pause", position)
                sleeper(pause_for)
                publish("play", position)
                paused_once = True
                if state["serverIsPassed"] is True:
                    break
            target = min(context.duration, position + int(math.ceil(interval)))
            if stop_at is not None:
                target = min(target, stop_at)
            if not paused_once:
                target = min(target, pause_at)
            deadline = clock() + target - position
            started = clock()
            while clock() < deadline:
                if stop_event is not None and stop_event.is_set():
                    elapsed = min(target - position, max(0, int(clock() - started)))
                    stopped_at = min(context.duration - 1, position + elapsed)
                    publish("pause", stopped_at)
                    state["stopped"] = True
                    atomic_json(path, state)
                    return state
                if on_update:
                    on_update({**state, "localPosition": min(target, position + clock() - started)})
                sleeper(max(0.0, min(0.5, deadline - clock())))
            publish("ended" if target == context.duration else "playing", target)
            position = state["lastResponsePosition"]
    except KeyboardInterrupt:
        # If a network operation was interrupted, its outcome is unknown: preserve
        # the pending marker, do not blindly retry or send a conflicting pause.
        acknowledged = state["lastResponsePosition"]
        if (state["pendingEvent"] is None and state["serverIsPassed"] is not True
                and acknowledged < context.duration and state.get("lastEvent") != "pause"):
            publish("pause", acknowledged)
        if not quiet:
            print("Stopped; remote checkpoint saved. Use --resume to replay from the last valid response.", flush=True)
    return state
