from flask import Flask, request, jsonify
from pathlib import Path
from urllib.parse import quote, urlparse
import argparse
import json
import math
import os
import re
import shlex
import shutil
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
    "durationMs",
    "startedAt",
    "userAgent",
    # Auto-mode fields (None for manual runs)
    "trial",
    "trials",
    "url",
    "openDelayMs",
    "bufferMs",
    "cacheReset",
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

# COMMAND TO RUN THIS ON WINDOWS
# C:\tools\RAMMap64.exe -accepteula
# set FROST_CACHE_CMD=C:\tools\RAMMap64.exe -Et
# .venv\Scripts\python server.py --launch-browser
# 
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

    record = {"label": data["label"], "trace": data["trace"], "ts": time.time()}
    for field in META_FIELDS:
        record[field] = data.get(field)

    # Recording is over: make sure the trial's tab is closed, then attach the
    # server-measured open/close timing
    trial_id = data.get("autoTrialId")
    trial = _close_link(trial_id, remove=True) if trial_id else None
    if trial:
        for key in TIMING_FIELDS:
            record[key] = trial.get(key)

    # Append, never overwrite, one file per label
    label = record["label"]
    trace_file = DATA_DIR / f"{label}.jsonl"
    with trace_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    trial_info = f" trial={record['trial']}/{record['trials']}" if record["trial"] else ""
    print(
        f"[saved] label={label} session={record['session']} sizeGB={record['sizeGB']} "
        f"samples={len(record['trace'])}{trial_info} -> {trace_file}"
    )

    return jsonify({"samples": len(record["trace"])})


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
