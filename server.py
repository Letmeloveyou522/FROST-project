from flask import Flask, send_from_directory, request, jsonify
import json
import time
import os

app = Flask(__name__, static_folder="static")

TRACE_FILE = "traces.jsonl"


@app.after_request
def add_coop_coep_headers(response):
    # These headers are required to unlock high-resolution performance.now()
    # Without them, Chrome/Firefox round timings to ~100us-1ms, which hides
    # the SSD latency differences we're trying to measure.
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Embedder-Policy"] = "require-corp"
    return response


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/attacker.js")
def attacker_js():
    return send_from_directory(app.static_folder, "attacker.js")


@app.route("/worker.js")
def worker_js():
    return send_from_directory(app.static_folder, "worker.js")


@app.route("/submit", methods=["POST"])
def submit():
    data = request.get_json()
    if not data or "label" not in data or "trace" not in data:
        return jsonify({"error": "expected {label, trace}"}), 400

    record = {
        "label": data["label"],
        "trace": data["trace"],
        "ts": time.time(),
    }
    with open(TRACE_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")

    print(f"[saved] label={record['label']} samples={len(record['trace'])}")
    return jsonify({"status": "ok", "samples": len(record["trace"])})


if __name__ == "__main__":
    print("Server running at http://localhost:8000")
    print(f"Traces will be appended to {os.path.abspath(TRACE_FILE)}")
    app.run(port=8000, debug=True)
