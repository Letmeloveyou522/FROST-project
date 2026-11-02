
from flask import Flask, send_from_directory, request, jsonify
from pathlib import Path
import json
import time
import math
import re

app = Flask(__name__, static_folder="static")

# Project directories
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

# Automatically create the data directory
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Measurement metadata
META_FIELDS = [
    "session",
    "sizeGB",
    "durationMs",
    "startedAt",
    "userAgent"
]


def validate(data):
    """Validate incoming measurement data."""

    if not isinstance(data, dict):
        return "expected a JSON object"

    if "label" not in data or "trace" not in data:
        return "expected {label, trace}"

    label = data["label"]

    if not isinstance(label, str) or not label.strip():
        return "label must be a non-empty string"

    # Prevent unsafe filenames and path traversal
    if not re.fullmatch(r"[A-Za-z0-9_-]+", label):
        return "label must contain only letters, numbers, _ or -"

    trace = data["trace"]

    if not isinstance(trace, list) or len(trace) == 0:
        return "trace must be a non-empty list"

    if not all(
        isinstance(x, (int, float))
        and not isinstance(x, bool)
        and math.isfinite(x)
        for x in trace
    ):
        return "trace must contain only finite numbers"

    return None


@app.after_request
def add_coop_coep_headers(response):
    # Preserve high-resolution timing configuration
    response.headers[
        "Cross-Origin-Opener-Policy"
    ] = "same-origin"

    response.headers[
        "Cross-Origin-Embedder-Policy"
    ] = "require-corp"

    return response


@app.route("/")
def index():
    return send_from_directory(
        app.static_folder,
        "index.html"
    )


@app.route("/attacker.js")
def attacker_js():
    return send_from_directory(
        app.static_folder,
        "attacker.js"
    )


@app.route("/worker.js")
def worker_js():
    return send_from_directory(
        app.static_folder,
        "worker.js"
    )


@app.route("/submit", methods=["POST"])
def submit():
    data = request.get_json(silent=True)

    error = validate(data)

    if error:
        return jsonify({"error": error}), 400

    # Preserve original measurement data
    record = {
        "label": data["label"],
        "trace": data["trace"],
        "ts": time.time(),
    }

    # Preserve measurement metadata
    for field in META_FIELDS:
        record[field] = data.get(field)

    # Automatically select a file based on label
    label = record["label"]

    trace_file = DATA_DIR / f"{label}.jsonl"

    # Append measurements without overwriting old runs
    with trace_file.open(
        "a",
        encoding="utf-8"
    ) as f:
        f.write(
            json.dumps(
                record,
                ensure_ascii=False
            ) + "\n"
        )

    print(
        f"[saved] label={record['label']} "
        f"session={record['session']} "
        f"sizeGB={record['sizeGB']} "
        f"samples={len(record['trace'])}"
    )

    print(f"[file] {trace_file}")

    return jsonify({
        "status": "ok",
        "label": label,
        "samples": len(record["trace"]),
        "file": f"data/{label}.jsonl"
    })


if __name__ == "__main__":
    print("Server running at http://localhost:8000")
    print(f"Trace directory: {DATA_DIR}")

    app.run(
        port=8000,
        debug=True
    )
