from flask import Flask, request, jsonify
from pathlib import Path
from urllib.parse import quote, urlparse
import argparse
import base64
import json
import math
import os
import random
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid

# static_url_path="" serves static/attacker.js and static/worker.js at the root
app = Flask(__name__, static_folder="static", static_url_path="")

SERVER_PORT = 8000

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Measurement metadata copied from the upload into each record
META_FIELDS = [
    "session",
    "sizeGB",
    "fileSizeBytes",  # real size of the measured file, verified by the worker
    "durationMs",
    "preload",  # True: whole file was read into RAM right before recording
    "startedAt",
    "userAgent",
    # Auto-mode fields (None for manual runs)
    "trial",
    "trials",
    "url",
    "openDelayMs",
    "bufferMs",
    "cacheReset",
    # Wall-clock time (ms since 1970) the recording started; times[i] is relative to it
    "startEpochMs",
]

# Server-measured timing attached to auto-mode records
TIMING_FIELDS = ["openCmdOffsetMs", "openedOffsetMs", "closedOffsetMs", "openError"]


def validate(data):
    """Validate incoming measurement data. Returns an error string or None."""
    if not isinstance(data, dict):
        return "expected a JSON object"

    label = data.get("label")
    # Also prevents unsafe filenames and path traversal
    if not isinstance(label, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", label):
        return "label must be a non-empty string of letters, numbers, _ or -"

    trace = data.get("trace")
    if not isinstance(trace, list) or len(trace) == 0:
        return "trace must be a non-empty list"

    if not all(
        isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
        for x in trace
    ):
        return "trace must contain only finite numbers"

    # Optional (older clients don't send it): start time of each read in ms
    # since recording began, one per trace sample, never going backwards.
    times = data.get("times")
    if times is not None:
        if not isinstance(times, list) or len(times) != len(trace):
            return "times must be a list with one entry per trace sample"
        if not all(
            isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
            for x in times
        ):
            return "times must contain only finite numbers"
        if any(b < a for a, b in zip(times, times[1:])):
            return "times must be non-decreasing"

    # The worker reports the real size of the file it measured. Reject a run
    # whose file did not match the requested size.
    size_gb = data.get("sizeGB")
    file_size = data.get("fileSizeBytes")
    if file_size is not None:
        if not isinstance(file_size, int) or isinstance(file_size, bool) or file_size <= 0:
            return "fileSizeBytes must be a positive integer"
        if not isinstance(size_gb, (int, float)) or round(size_gb * 1024**3) != file_size:
            return f"measured file was {file_size} bytes, which does not match sizeGB={size_gb}"

    start_epoch = data.get("startEpochMs")
    if start_epoch is not None and (
        not isinstance(start_epoch, (int, float)) or isinstance(start_epoch, bool)
        or not math.isfinite(start_epoch)
    ):
        return "startEpochMs must be a finite number"

    return None


# ---------------------------------------------------------------------------
# Auto mode (follows the FROST paper's procedure, Sec. 6.2 / 6.3):
#   - restore the page-cache state and let the system settle,
#   - record for N seconds, opening the website `openDelayMs` after recording
#     starts (paper: 10 s traces, website opened at 2 s),
#   - close it when recording ends, repeat.
#
# The website is opened as a new tab in the SAME Chrome instance that runs the
# measurement page (no new browser process per trial). Tabs are opened and
# closed through Chrome's DevTools HTTP endpoint, which works identically on
# macOS, Windows and Linux. Chrome only exposes it when started with
# --remote-debugging-port, and (since Chrome 136) only for a non-default
# profile, so start the measurement browser with:
#     python server.py --launch-browser
# ---------------------------------------------------------------------------

CDP_PORT = int(os.environ.get("FROST_CDP_PORT", "9222"))
CDP_BASE = f"http://127.0.0.1:{CDP_PORT}"

# Chrome profile used by the measurement browser. Persists between runs, so
# you log in to sites (instagram, figma, gpt, ...) once and stay logged in.
AUTO_PROFILE_DIR = BASE_DIR / ".auto_profile"


def find_browser():
    """Locate a Chromium-based browser executable. Override with FROST_BROWSER."""
    override = os.environ.get("FROST_BROWSER")
    if override:
        return override if Path(override).exists() else shutil.which(override)

    candidates = []
    if sys.platform == "darwin":
        for app_name in ("Google Chrome", "Chromium", "Microsoft Edge", "Brave Browser"):
            candidates.append(f"/Applications/{app_name}.app/Contents/MacOS/{app_name}")
            candidates.append(
                str(Path.home() / f"Applications/{app_name}.app/Contents/MacOS/{app_name}")
            )
    elif sys.platform.startswith("win"):
        for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            root = os.environ.get(env)
            if root:
                candidates.append(os.path.join(root, r"Google\Chrome\Application\chrome.exe"))
                candidates.append(os.path.join(root, r"Microsoft\Edge\Application\msedge.exe"))
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            path = shutil.which(name)
            if path:
                candidates.append(path)

    for path in candidates:
        if Path(path).exists():
            return path
    return None


def launch_browser():
    """Start ONE long-lived Chrome with the DevTools endpoint and open the page."""
    browser = find_browser()
    if not browser:
        print("[auto] no Chrome/Edge/Chromium found; set FROST_BROWSER")
        return
    subprocess.Popen(
        [
            browser,
            f"--user-data-dir={AUTO_PROFILE_DIR}",
            f"--remote-debugging-port={CDP_PORT}",
            "--no-first-run",
            "--no-default-browser-check",
            f"http://localhost:{SERVER_PORT}/",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"[auto] launched measurement browser: {browser}")


def _cdp(path, method="GET"):
    """Call Chrome's DevTools HTTP endpoint. Returns parsed JSON, or text."""
    req = urllib.request.Request(CDP_BASE + path, method=method)
    with urllib.request.urlopen(req, timeout=5) as resp:
        body = resp.read().decode("utf-8", "replace")
    try:
        return json.loads(body)
    except ValueError:
        return body


def _find_measurement_tab():
    """Id of the tab showing this server's page, or None."""
    for tab in _cdp("/json/list"):
        u = urlparse(tab.get("url", ""))
        if (
            tab.get("type") == "page"
            and u.hostname in ("localhost", "127.0.0.1")
            and u.port == SERVER_PORT
        ):
            return tab["id"]
    return None


# --- page-cache reset ------------------------------------------------------
#
# The paper restores the page-cache state between repeated measurements so
# that one trial's cached files do not speed up the next one. We flush the OS
# file cache (macOS: purge, Linux: drop_caches), which needs passwordless sudo
# for that single command. Override with FROST_CACHE_CMD, e.g. on Windows
# (server run from an Administrator terminal):
#   set FROST_CACHE_CMD=C:\path\to\RAMMap64.exe -Et

# COMMAND TO RUN THIS ON WINDOWS (in an admin console)
# C:\tools\RAMMap64.exe -accepteula
# set FROST_CACHE_CMD=C:\tools\RAMMap64.exe -Et
# .venv\Scripts\python server.py --launch-browser


SUDO_HINT = (
    "Allow it without a password: "
    f"echo '{os.environ.get('USER', '<you>')} ALL=(root) NOPASSWD: /usr/sbin/purge' "
    "| sudo tee /etc/sudoers.d/frost-purge"
)


def _cache_reset_cmd():
    override = os.environ.get("FROST_CACHE_CMD")
    if override:
        return shlex.split(override, posix=not sys.platform.startswith("win"))
    if sys.platform == "darwin":
        return ["sudo", "-n", "/usr/sbin/purge"]
    if sys.platform.startswith("linux"):
        return ["sudo", "-n", "sh", "-c", "sync; echo 3 > /proc/sys/vm/drop_caches"]
    return None


def reset_page_cache():
    """Flush the OS page cache. Returns seconds taken; raises on failure."""
    cmd = _cache_reset_cmd()
    if cmd is None:
        raise RuntimeError("no page-cache reset command for this OS; set FROST_CACHE_CMD")
    t0 = time.perf_counter()
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        msg = (result.stderr or result.stdout).strip()
        if "password" in msg.lower():
            msg = "sudo needs a password. " + (SUDO_HINT if sys.platform == "darwin" else "")
        raise RuntimeError(f"page-cache reset failed: {msg}")
    return round(time.perf_counter() - t0, 2)


# --- per-trial open/close --------------------------------------------------
#
# The lock is held during the DevTools calls (a few ms each). Only one trial
# runs at a time, so this costs nothing, and it guarantees a close never
# overlaps an open and the timing is final before a record is written.

_trials = {}
_trials_lock = threading.Lock()


def _ms_since(t0):
    return round((time.perf_counter() - t0) * 1000, 1)


def _open_link(trial_id):
    with _trials_lock:
        trial = _trials.get(trial_id)
        if trial is None or trial["state"] != "scheduled":
            return
        trial["openCmdOffsetMs"] = _ms_since(trial["t0"])
        try:
            tab = _cdp("/json/new?" + quote(trial["url"], safe=""), method="PUT")
            trial["tabId"] = tab["id"]
            trial["state"] = "open"
        except Exception as e:
            trial["state"] = "error"
            trial["openError"] = str(e)
        trial["openedOffsetMs"] = _ms_since(trial["t0"])

    if trial["state"] == "open":
        print(f"[auto] opened {trial['url']} at +{trial['openedOffsetMs']} ms")
    else:
        print(f"[auto] failed to open link: {trial['openError']}")


def _close_link(trial_id, remove=False):
    """Close the trial's tab if open (idempotent). Returns the trial dict."""
    with _trials_lock:
        trial = _trials.pop(trial_id, None) if remove else _trials.get(trial_id)
        if trial is None:
            return None
        for timer in trial["timers"]:
            timer.cancel()
        if trial["state"] == "open":
            try:
                _cdp(f"/json/close/{trial['tabId']}")
            except Exception as e:
                print(f"[auto] failed to close tab: {e}")
            trial["closedOffsetMs"] = _ms_since(trial["t0"])
            # Bring the measurement tab back to the front
            if trial["measurementTab"]:
                try:
                    _cdp(f"/json/activate/{trial['measurementTab']}")
                except Exception:
                    pass
            print(f"[auto] closed tab at +{trial['closedOffsetMs']} ms")
        trial["state"] = "closed"
        return trial


@app.route("/auto/config")
def auto_config():
    try:
        return jsonify({"cdp": True, "measurementTab": _find_measurement_tab() is not None})
    except Exception:
        return jsonify({"cdp": False, "measurementTab": False})


@app.route("/auto/reset-cache", methods=["POST"])
def auto_reset_cache():
    try:
        seconds = reset_page_cache()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    print(f"[auto] page cache reset ({seconds}s)")
    return jsonify({"status": "ok"})


@app.route("/auto/trial-start", methods=["POST"])
def auto_trial_start():
    """Called by the page the instant recording starts."""
    t0 = time.perf_counter()
    data = request.get_json(silent=True) or {}

    url = data.get("url")
    open_delay_ms = data.get("openDelayMs", 0)
    duration_ms = data.get("durationMs")

    if not isinstance(url, str) or not re.match(r"^https?://\S+$", url):
        return jsonify({"error": "url must start with http:// or https://"}), 400
    if not isinstance(open_delay_ms, (int, float)) or open_delay_ms < 0:
        return jsonify({"error": "openDelayMs must be >= 0"}), 400
    if not isinstance(duration_ms, (int, float)) or duration_ms <= 0:
        return jsonify({"error": "durationMs must be > 0"}), 400
    if open_delay_ms >= duration_ms:
        return jsonify({"error": "open delay must be shorter than the duration"}), 400

    trial_id = uuid.uuid4().hex
    open_timer = threading.Timer(open_delay_ms / 1000, _open_link, args=[trial_id])
    close_timer = threading.Timer(duration_ms / 1000, _close_link, args=[trial_id])

    with _trials_lock:
        _trials[trial_id] = {
            "t0": t0,
            "url": url,
            "state": "scheduled",
            "timers": [open_timer, close_timer],
            "measurementTab": None,
        }

    open_timer.start()
    close_timer.start()

    # Looked up after the timers start so it never delays the open
    try:
        mtab = _find_measurement_tab()
    except Exception:
        mtab = None
    with _trials_lock:
        if trial_id in _trials:
            _trials[trial_id]["measurementTab"] = mtab

    return jsonify({"trialId": trial_id})


@app.route("/auto/trial-end", methods=["POST"])
def auto_trial_end():
    """Safety net for failed trials (successful ones are closed by /submit)."""
    data = request.get_json(silent=True) or {}
    _close_link(data.get("trialId"), remove=True)
    return jsonify({"status": "ok"})


@app.route("/auto/stop", methods=["POST"])
def auto_stop():
    with _trials_lock:
        ids = list(_trials)
    for trial_id in ids:
        _close_link(trial_id, remove=True)
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Long recording mode: one trace that runs until the user presses Stop, while
# the user browses by hand. To check the model's detected visits against the
# truth, the server logs the exact moment every website visit starts.
#
# Visit times come from Chrome itself through the DevTools protocol: the server
# attaches to every tab and logs each top-level page request
# (Network.requestWillBeSent for the tab's main frame) with Chrome's wall-clock
# time. That is the moment the browser starts loading the page, the same moment
# auto mode's "open" marks. The page sends the wall-clock time recording
# started (startEpochMs), so each visit is saved as ms since recording start,
# on the same clock as the trace's `times`.
#
# Besides visits, the server logs tab events (a tab opened, with its URL, even
# chrome:// pages such as the new-tab page; a tab closed), to explain detections
# that are not visits. Tab switches cannot be seen through DevTools.
#
# Optionally the server opens the sites itself, at random moments, the way auto
# mode does (new tab, closed again after `openS` seconds). Those visits are the
# same kind the models were trained on, so they test the long-trace scan on its
# own, separately from hand browsing. Like auto mode, it can reset the OS page
# cache: once before recording starts, and after every visit (then settle for
# `settleS` before the quiet time), so each site loads from the SSD, not from
# RAM. Each reset is logged as a "cache_reset" event, since its disk burst shows
# up in the trace.
# ---------------------------------------------------------------------------


class _WebSocket:
    """Minimal WebSocket client (RFC 6455), enough for Chrome's local DevTools endpoint."""

    def __init__(self, url, timeout=5):
        u = urlparse(url)
        self.sock = socket.create_connection((u.hostname, u.port), timeout=timeout)
        self.send_lock = threading.Lock()
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            (
                f"GET {u.path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        self.rfile = self.sock.makefile("rb")
        status = self.rfile.readline()
        if b" 101 " not in status:
            raise ConnectionError(f"DevTools WebSocket handshake failed: {status!r}")
        while self.rfile.readline() not in (b"\r\n", b""):
            pass
        self.sock.settimeout(None)  # the reader thread blocks until a message or close()

    def send(self, obj):
        data = json.dumps(obj).encode()
        n = len(data)
        if n < 126:
            head = bytes([0x81, 0x80 | n])
        elif n < 65536:
            head = bytes([0x81, 0x80 | 126]) + n.to_bytes(2, "big")
        else:
            head = bytes([0x81, 0x80 | 127]) + n.to_bytes(8, "big")
        mask = os.urandom(4)  # client frames must be masked
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        with self.send_lock:
            self.sock.sendall(head + mask + body)

    def _read(self, n):
        data = self.rfile.read(n)
        if len(data) < n:
            raise ConnectionError("DevTools connection closed")
        return data

    def recv(self):
        """Next message as parsed JSON, or None when Chrome closes the connection."""
        message = b""
        while True:
            b0, b1 = self._read(2)
            opcode, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = int.from_bytes(self._read(2), "big")
            elif n == 127:
                n = int.from_bytes(self._read(8), "big")
            mask = self._read(4) if b1 & 0x80 else None
            payload = self._read(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:  # close
                return None
            if opcode >= 0x8:  # ping/pong: Chrome does not need an answer
                continue
            message += payload
            if b0 & 0x80:  # final fragment
                return json.loads(message)

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


def _is_own_page(url):
    u = urlparse(url or "")
    return u.hostname in ("localhost", "127.0.0.1") and u.port == SERVER_PORT


class VisitWatcher:
    """Logs the start of every top-level page load, and tab opens/closes, in the measurement browser."""

    def __init__(self):
        self.visits = []  # {"epochMs": wall-clock ms, "url": ...}
        self.events = []  # {"epochMs": ..., "kind": "tab_open" | "tab_close", "url": ...}
        self.sessions = {}  # DevTools session id -> tab (target) id
        self.initial = {}  # tab id -> (url, epoch ms) of its first page load, logged on attach
        self.lock = threading.Lock()
        self.next_id = 0
        self.ws = _WebSocket(_cdp("/json/version")["webSocketDebuggerUrl"])
        threading.Thread(target=self._run, daemon=True).start()
        # Attach to every open tab and every tab opened later. New tabs wait
        # until Network is enabled in them, so their first page load is not missed.
        self._call(
            "Target.setAutoAttach",
            {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True},
        )

    def _call(self, method, params=None, session=None):
        with self.lock:
            self.next_id += 1
            msg = {"id": self.next_id, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        self.ws.send(msg)

    def _run(self):
        try:
            while (msg := self.ws.recv()) is not None:
                self._handle(msg)
        except (OSError, ConnectionError, ValueError):
            pass  # closed by stop(), or Chrome went away

    def _handle(self, msg):
        method, p = msg.get("method"), msg.get("params", {})
        if method == "Target.attachedToTarget":
            info, sid = p["targetInfo"], p["sessionId"]
            if info.get("type") == "page" and not _is_own_page(info.get("url")):
                self.sessions[sid] = info["targetId"]
                self._call("Network.enable", session=sid)
                url = info.get("url", "")
                if p.get("waitingForDebugger"):  # a tab opened while we watch (not one already open)
                    now = time.time() * 1000
                    self._event(now, "tab_open", url)
                    # A tab opened straight onto a site (link to a new tab, the
                    # server's open) has already sent its page request when we
                    # attach, ~20-30 ms after it opened. Log the visit now instead.
                    if url.startswith(("http://", "https://")):
                        self.initial[info["targetId"]] = (url, now)
                        self._log(now, url)
            if p.get("waitingForDebugger"):
                self._call("Runtime.runIfWaitingForDebugger", session=sid)
            if sid not in self.sessions:  # workers, the measurement page, ...
                self._call("Target.detachFromTarget", {"sessionId": sid})
        elif method == "Target.detachedFromTarget":
            if self.sessions.pop(p.get("sessionId"), None) is not None:
                self._event(time.time() * 1000, "tab_close", "")
        elif method == "Network.requestWillBeSent":
            tab = self.sessions.get(msg.get("sessionId"))
            url = p.get("request", {}).get("url", "")
            if (
                tab is not None
                and p.get("type") == "Document"
                and p.get("frameId") == tab  # the tab's main frame, not an iframe
                and p.get("requestId") == p.get("loaderId")  # a navigation
                and "redirectResponse" not in p  # not a redirect hop of the same visit
                and url.startswith(("http://", "https://"))
                and not _is_own_page(url)
                and not self._logged_on_open(tab, url, p["wallTime"] * 1000)
            ):
                self._log(p["wallTime"] * 1000, url)

    def _logged_on_open(self, tab, url, epoch_ms):
        """True if this request is the tab's first page load, already logged on attach."""
        first = self.initial.pop(tab, None)
        return first is not None and first[0] == url and epoch_ms - first[1] < 2000

    def _log(self, epoch_ms, url):
        with self.lock:
            self.visits.append({"epochMs": epoch_ms, "url": url})
        print(f"[long] visit {time.strftime('%H:%M:%S')} {url}")

    def _event(self, epoch_ms, kind, url):
        with self.lock:
            self.events.append({"epochMs": epoch_ms, "kind": kind, "url": url})
        print(f"[long] {kind} {time.strftime('%H:%M:%S')} {url}")

    def stop(self):
        """Stop watching; returns (visits, tab events)."""
        self.ws.close()
        with self.lock:
            return list(self.visits), list(self.events)


class SiteOpener:
    """Opens sites at random moments during a long recording, the way auto mode does.

    Waits a random quiet time (quiet_s = (min, max) seconds), opens the next site
    in a new tab, closes it open_s seconds later, and repeats until stopped. Every
    site is opened once per round, in a random order, so the sites stay balanced.
    With cache_reset, the OS page cache is reset after each visit and the system
    settles for settle_s before the next quiet time starts.
    """

    def __init__(self, urls, quiet_s, open_s, cache_reset=False, settle_s=0):
        self.urls, self.quiet_s, self.open_s = list(urls), quiet_s, open_s
        self.cache_reset, self.settle_s = cache_reset, settle_s
        self.opens = []  # {"epochMs": time the open command was sent, "url": ...}
        self.events = []  # {"epochMs", "kind": "cache_reset" | "cache_reset_failed", "url": "", "durS"}
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        try:
            self.measurement_tab = _find_measurement_tab()
        except Exception:
            self.measurement_tab = None

    def start(self):
        """Called the moment recording starts."""
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        # the cache was reset right before recording started: let the system settle first
        if self.cache_reset and self.stopped.wait(self.settle_s):
            return
        order = []
        while not self.stopped.wait(random.uniform(*self.quiet_s)):
            if not order:
                order = random.sample(self.urls, len(self.urls))
            url = order.pop()
            sent = time.time() * 1000  # like auto mode's "open at 2 s": when the command is sent
            try:
                tab = _cdp("/json/new?" + quote(url, safe=""), method="PUT")
            except Exception as e:
                print(f"[long] failed to open {url}: {e}")
                continue
            with self.lock:
                self.opens.append({"epochMs": sent, "url": url})
            print(f"[long] server opened {url}")
            self.stopped.wait(self.open_s)
            try:
                _cdp(f"/json/close/{tab['id']}")
                if self.measurement_tab:
                    _cdp(f"/json/activate/{self.measurement_tab}")
            except Exception as e:
                print(f"[long] failed to close tab: {e}")
            if self.cache_reset and not self.stopped.is_set():
                self._reset_cache()
                if self.stopped.wait(self.settle_s):
                    return

    def _reset_cache(self):
        began = time.time() * 1000
        try:
            seconds = reset_page_cache()
            kind = "cache_reset"
            print(f"[long] page cache reset ({seconds}s)")
        except Exception as e:
            seconds = round(time.time() - began / 1000, 2)
            kind = "cache_reset_failed"
            print(f"[long] {e}")
        with self.lock:
            self.events.append({"epochMs": began, "kind": kind, "url": "", "durS": seconds})

    def stop(self):
        """Stop opening sites; returns (opens, cache-reset events)."""
        self.stopped.set()
        with self.lock:
            return list(self.opens), list(self.events)


_long = {}  # long-recording id -> {"watcher": VisitWatcher, "opener": SiteOpener or None}
_long_lock = threading.Lock()


def _stop_long(long_id):
    """Stop a long recording's watcher and opener. Returns (visits, events) or None.

    visits: [{"epochMs", "url", "by": "server" | "user"}], sorted by time
    events: [{"epochMs", "kind", "url", ("durS")}] tab opens/closes and cache resets, sorted by time
    """
    with _long_lock:
        entry = _long.pop(long_id, None)
    if entry is None:
        return None
    opens, resets = entry["opener"].stop() if entry["opener"] else ([], [])
    seen, events = entry["watcher"].stop()
    events = sorted(events + resets, key=lambda e: e["epochMs"])
    # The watcher also sees the server's opens; keep the server's own (exact) time for those
    user = [
        v for v in seen
        if not any(o["url"] == v["url"] and 0 <= v["epochMs"] - o["epochMs"] < 1500 for o in opens)
    ]
    visits = [{**o, "by": "server"} for o in opens] + [{**v, "by": "user"} for v in user]
    return sorted(visits, key=lambda v: v["epochMs"]), events


@app.route("/long/start", methods=["POST"])
def long_start():
    """Called by the page right before a long recording starts.

    Optional body: {"urls": [...], "quietS": [min, max], "openS": s,
    "cacheReset": bool, "settleS": s} makes the server open those sites itself
    during the recording, resetting the OS page cache before recording starts and
    after every visit when cacheReset is true.
    """
    data = request.get_json(silent=True) or {}
    urls = data.get("urls") or []
    opener = None
    if urls:
        quiet_s, open_s = data.get("quietS"), data.get("openS")
        if not all(isinstance(u, str) and re.match(r"^https?://\S+$", u) for u in urls):
            return jsonify({"error": "every site must start with http:// or https://"}), 400
        if (
            not isinstance(quiet_s, list) or len(quiet_s) != 2
            or not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in quiet_s)
            or not 2 <= quiet_s[0] <= quiet_s[1]
        ):
            return jsonify({"error": "quiet time must be min <= max, and at least 2 s (the baseline)"}), 400
        if not isinstance(open_s, (int, float)) or isinstance(open_s, bool) or open_s <= 0:
            return jsonify({"error": "open time must be > 0"}), 400
        cache_reset, settle_s = bool(data.get("cacheReset")), data.get("settleS", 0)
        if not isinstance(settle_s, (int, float)) or isinstance(settle_s, bool) or settle_s < 0:
            return jsonify({"error": "settle time must be >= 0"}), 400
        if cache_reset:
            # Start from a reset cache too, like auto mode before each trial. This
            # also checks the reset works before the recording begins.
            try:
                seconds = reset_page_cache()
            except Exception as e:
                return jsonify({"error": str(e)}), 500
            print(f"[long] page cache reset before recording ({seconds}s)")
        opener = SiteOpener(urls, tuple(quiet_s), open_s, cache_reset, settle_s)
    try:
        watcher = VisitWatcher()
    except Exception as e:
        return jsonify({
            "error": f"cannot watch the browser's tabs ({e}). "
                     "Run python server.py --launch-browser and use the page in that Chrome window."
        }), 500
    long_id = uuid.uuid4().hex
    with _long_lock:
        _long[long_id] = {"watcher": watcher, "opener": opener}
    print("[long] watching tabs for website visits" + (f"; server will open {len(urls)} site(s)" if opener else ""))
    return jsonify({"longId": long_id})


@app.route("/long/measuring", methods=["POST"])
def long_measuring():
    """Called by the page the instant recording starts: starts the server's site opens."""
    data = request.get_json(silent=True) or {}
    with _long_lock:
        entry = _long.get(data.get("longId"))
    if entry and entry["opener"]:
        entry["opener"].start()
    return jsonify({"status": "ok"})


@app.route("/long/stop-opening", methods=["POST"])
def long_stop_opening():
    """Called the moment the user presses Stop: no more site opens or cache resets
    while the trace uploads (a site still open is closed). /submit collects the log."""
    data = request.get_json(silent=True) or {}
    with _long_lock:
        entry = _long.get(data.get("longId"))
    if entry and entry["opener"]:
        entry["opener"].stopped.set()
    return jsonify({"status": "ok"})


@app.route("/long/abort", methods=["POST"])
def long_abort():
    """Safety net for failed long recordings (successful ones are stopped by /submit)."""
    data = request.get_json(silent=True) or {}
    _stop_long(data.get("longId"))
    return jsonify({"status": "ok"})


@app.after_request
def add_coop_coep_headers(response):
    # Required for high-resolution performance.now() timers
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Embedder-Policy"] = "require-corp"
    return response


@app.route("/")
def index():
    return app.send_static_file("index.html")


@app.route("/submit", methods=["POST"])
def submit():
    data = request.get_json(silent=True)

    error = validate(data)
    if error:
        return jsonify({"error": error}), 400

    record = {
        "label": data["label"],
        "trace": data["trace"],
        "times": data.get("times"),  # None for records from older clients
        "ts": time.time(),
    }
    for field in META_FIELDS:
        record[field] = data.get(field)

    # Recording is over: make sure the trial's tab is closed, then attach the
    # server-measured open/close timing
    trial_id = data.get("autoTrialId")
    trial = _close_link(trial_id, remove=True) if trial_id else None
    if trial:
        for key in TIMING_FIELDS:
            record[key] = trial.get(key)

    # Long recording: attach the logged visits and tab events as ms since recording start
    long_id = data.get("longId")
    logged = _stop_long(long_id) if long_id else None
    if logged is not None:
        start = record["startEpochMs"]
        if start is None:
            return jsonify({"error": "long recordings need startEpochMs"}), 400
        end = record["times"][-1] if record["times"] else math.inf
        visits, events = logged
        record["visits"] = [
            {"t": round(v["epochMs"] - start, 1), "url": v["url"], "by": v["by"]}
            for v in visits if 0 <= v["epochMs"] - start <= end
        ]
        record["events"] = [
            {"t": round(e["epochMs"] - start, 1), "kind": e["kind"], "url": e["url"],
             **({"durS": e["durS"]} if "durS" in e else {})}
            for e in events if 0 <= e["epochMs"] - start <= end
        ]

    # Append, never overwrite, one file per label
    label = record["label"]
    trace_file = DATA_DIR / f"{label}.jsonl"
    with trace_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    trial_info = f" trial={record['trial']}/{record['trials']}" if record["trial"] else ""
    visit_info = f" visits={len(record['visits'])}" if "visits" in record else ""
    print(
        f"[saved] label={label} session={record['session']} sizeGB={record['sizeGB']} "
        f"samples={len(record['trace'])}{trial_info}{visit_info} -> {trace_file}"
    )

    return jsonify({
        "samples": len(record["trace"]),
        "visits": record.get("visits"),
        "events": len(record["events"]) if "events" in record else None,
    })


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--launch-browser",
        action="store_true",
        help="start the measurement Chrome (with DevTools endpoint) and open the page",
    )
    args = parser.parse_args()

    print(f"Server running at http://localhost:{SERVER_PORT}")
    print(f"Trace directory: {DATA_DIR}")

    if args.launch_browser:
        # Give Flask a moment to start before Chrome loads the page
        threading.Timer(1.0, launch_browser).start()

    app.run(
        port=SERVER_PORT,
        debug=True,
        # The reloader would restart the server (and kill running trials)
        # whenever a file changes during a long collection run.
        use_reloader=False,
    )
