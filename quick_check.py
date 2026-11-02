"""Quick check: plot and summarize collected traces.

[수정] 이전 버전과 달라진 점:
  - 스파이크 처리 추가 (논문 방식: 1ms 초과 값을 주변 100개 평균으로 대체)
  - 트레이스 길이를 가장 짧은 것에 맞춤 (논문 방식)
  - 파일 크기(ANALYSIS_SIZE_GB)가 맞는 기록만 분석
  - [수정] 그래프를 라벨별 평균 두 개로 단순화 (idle vs youtube)
  - [수정] 라벨별 요약 표 추가 (run 평균 기준)
  - 원본 traces.jsonl은 건드리지 않음 (분석할 때만 처리)

Usage:
    python quick_check.py
"""
import json
import sys

try:
    import matplotlib.pyplot as plt
    import numpy as np
except ImportError:
    print("Missing package. Run: pip install matplotlib numpy --break-system-packages")
    sys.exit(1)

# 논문 기준: 1ms(=1000us)를 넘는 값을 스파이크로 본다.
# 트레이스 단위는 ms이므로 threshold도 ms 단위다.
SPIKE_THRESHOLD_MS = 1.0
# 논문 기준: 스파이크 주변 100개 값의 평균으로 바꾼다. 앞뒤 50개씩을 사용한다.
SPIKE_HALF_WINDOW = 50
# 분석할 파일 크기. 이 값과 다른 sizeGB의 기록은 비교 대상에서 뺀다.
# 파일 크기가 다르면 같은 조건의 비교가 안 되기 때문이다.
ANALYSIS_SIZE_GB = 64
# [수정] 위쪽 그래프를 부드럽게 만들 이동 평균 창 크기 (샘플 수).
SMOOTH_WINDOW = 500
# [수정] 라벨별로 그릴 색. 라벨 이름이 바뀌어도 같은 색을 유지하기 위해 고정했다.
LABEL_COLORS = {"idle": "tab:blue", "youtube": "tab:red"}


def filter_spikes(trace):
    """1ms를 넘는 값을 주변 값들의 평균으로 바꾼다. 바뀐 개수도 함께 돌려준다."""
    arr = np.asarray(trace, dtype=float)
    out = arr.copy()
    spike_idx = np.where(arr > SPIKE_THRESHOLD_MS)[0]
    for i in spike_idx:
        lo = max(0, i - SPIKE_HALF_WINDOW)
        hi = min(len(arr), i + SPIKE_HALF_WINDOW + 1)
        # 자기 자신은 제외하고 평균을 낸다.
        neighbors = np.delete(arr[lo:hi], i - lo)
        out[i] = neighbors.mean()
    return out, len(spike_idx)


def moving_average(arr, window):
    """이동 평균. 그래프를 부드럽게 만들 때 쓴다."""
    kernel = np.ones(window) / window
    return np.convolve(arr, kernel, mode="valid")


traces = []
try:
    with open("traces.jsonl") as f:
        for line in f:
            t = json.loads(line)
            # 기록에 sizeGB가 없는 옛날 데이터도 있으므로 get()으로 확인한다.
            if t.get("sizeGB") == ANALYSIS_SIZE_GB:
                traces.append(t)
except FileNotFoundError:
    print("traces.jsonl not found yet. Run at least one measurement first.")
    sys.exit(1)

if not traces:
    print(f"No traces with sizeGB={ANALYSIS_SIZE_GB} found in traces.jsonl.")
    sys.exit(1)

# 모든 트레이스를 가장 짧은 길이에 맞춘다. 논문과 같은 방식이다.
min_len = min(len(t["trace"]) for t in traces)

print(f"Loaded {len(traces)} trace(s). Truncating all to {min_len} samples.\n")

# [수정] 라벨별로 실행 결과를 모아 둔다. 그래프와 요약 표가 여기서 나온다.
by_label = {}
print(f"{'label':<10} {'session':<12} {'sizeGB':<7} {'median':>8} {'p95':>8} {'spikes':>7}")
print("-" * 58)

for t in traces:
    raw = t["trace"][:min_len]
    filtered, n_spikes = filter_spikes(raw)

    session = t.get("session") or "-"
    size = t.get("sizeGB") or "-"
    median = float(np.median(filtered))
    p95 = float(np.percentile(filtered, 95))
    print(f"{t['label']:<10} {str(session):<12} {str(size):<7} "
          f"{median:>8.4f} {p95:>8.4f} {n_spikes:>7}")

    by_label.setdefault(t["label"], []).append({
        "filtered": filtered, "median": median, "p95": p95, "spikes": n_spikes,
    })

# [수정] 라벨별 요약: 실행 여러 번의 평균과 범위를 함께 보여준다.
print(f"\n{'label':<10} {'runs':>5} {'p95 mean':>10} {'p95 min~max':>20} {'spikes mean':>12}")
print("-" * 62)
for label, runs in by_label.items():
    p95s = [r["p95"] for r in runs]
    spikes = [r["spikes"] for r in runs]
    print(f"{label:<10} {len(runs):>5} {np.mean(p95s):>10.4f} "
          f"{min(p95s):>9.4f}~{max(p95s):<9.4f} {np.mean(spikes):>12.1f}")

# [수정] 그래프는 라벨별로 "여러 실행의 평균" 한 줄씩만 그린다.
fig, (ax_trace, ax_hist) = plt.subplots(2, 1, figsize=(10, 8))

for label, runs in by_label.items():
    color = LABEL_COLORS.get(label)
    # 같은 번째 샘플끼리 평균을 낸다 (길이는 이미 맞춰져 있다).
    mean_curve = np.mean([r["filtered"] for r in runs], axis=0)
    ax_trace.plot(moving_average(mean_curve, SMOOTH_WINDOW), color=color,
                  label=f"{label} (mean of {len(runs)} runs)", linewidth=1.5)

    # 분포는 실행들을 모두 합쳐서 비율(density)로 그린다. 측정 횟수가 달라도 비교 가능하다.
    all_values = np.concatenate([r["filtered"] for r in runs])
    ax_hist.hist(all_values, bins=np.linspace(0, 0.3, 61), density=True,
                 histtype="step", color=color, linewidth=1.5,
                 label=f"{label} ({len(runs)} runs)")

ax_trace.set_title(f"Mean read latency over time (moving average, window={SMOOTH_WINDOW})")
ax_trace.set_xlabel("Sample index")
ax_trace.set_ylabel("Read latency (ms)")
ax_trace.legend()

ax_hist.set_title("Latency distribution (0~0.3ms, all runs combined)")
ax_hist.set_xlabel("Read latency (ms)")
ax_hist.set_ylabel("Share of samples")
ax_hist.legend()

plt.tight_layout()
plt.savefig("comparison.png", dpi=150)
print("\nSaved comparison.png")
