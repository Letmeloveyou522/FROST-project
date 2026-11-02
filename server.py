from flask import Flask, send_from_directory, request, jsonify
import json
import time
import os

app = Flask(__name__, static_folder="static")

TRACE_FILE = "traces.jsonl"

# [수정] 측정 조건을 기록하는 필드 목록. 나중에 세션별, 파일 크기별로 데이터를 나누기 위해 필요하다.
# 브라우저가 이 값을 보내지 않으면 None으로 저장된다.
META_FIELDS = ["session", "sizeGB", "durationMs", "startedAt", "userAgent"]


# [수정] 요청 데이터가 올바른지 확인한다. 문제가 있으면 에러 메시지를, 없으면 None을 돌려준다.
def validate(data):
    if not data or "label" not in data or "trace" not in data:
        return "expected {label, trace}"
    if not isinstance(data["label"], str) or not data["label"].strip():
        return "label must be a non-empty string"
    trace = data["trace"]
    if not isinstance(trace, list) or len(trace) == 0:
        return "trace must be a non-empty list"
    # 지연값은 숫자여야 한다. 문자열이 섞이면 분석 단계에서 깨진다.
    if not all(isinstance(x, (int, float)) for x in trace):
        return "trace must contain only numbers"
    return None


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
    # [수정] silent=True: JSON이 아니면 예외 대신 None을 돌려줘서 아래 검사에서 400으로 처리된다.
    data = request.get_json(silent=True)
    error = validate(data)
    if error:
        return jsonify({"error": error}), 400

    record = {
        "label": data["label"],
        "trace": data["trace"],
        "ts": time.time(),
    }
    # [수정] 측정 조건 메타데이터를 같이 저장한다.
    for field in META_FIELDS:
        record[field] = data.get(field)

    with open(TRACE_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")

    print(f"[saved] label={record['label']} session={record['session']} "
          f"sizeGB={record['sizeGB']} samples={len(record['trace'])}")
    return jsonify({"status": "ok", "samples": len(record["trace"])})


if __name__ == "__main__":
    print("Server running at http://localhost:8000")
    print(f"Traces will be appended to {os.path.abspath(TRACE_FILE)}")
    app.run(port=8000, debug=True)
