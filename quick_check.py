
"""Quick check: automatically summarize and plot collected traces.

Features:
- Automatically detect all labels (idle, figma, youtube, etc.)
- Group traces by sizeGB, then by label
- Replace spikes above 1ms using nearby non-spike samples
- Truncate traces to the shortest length within each size group
- Summarize median, p95 and spike counts
- Plot mean traces and latency distributions
- Preserve the original traces.jsonl

Usage:
    python quick_check.py
"""

import json
import re
import sys
from pathlib import Path
from collections import defaultdict

try:
    import matplotlib.pyplot as plt
    import numpy as np
except ImportError:
    print("Missing packages. Run: python -m pip install matplotlib numpy")
    sys.exit(1)


# =========================
# Configuration
# =========================

BASE_DIR = Path(__file__).resolve().parent
TRACE_FILE = BASE_DIR / "traces.jsonl"

SPIKE_THRESHOLD_MS = 1.0
SPIKE_HALF_WINDOW = 50
SMOOTH_WINDOW = 500

# None: analyze every file size separately
# 64: analyze only records with sizeGB=64
ANALYSIS_SIZE_GB = None


# =========================
# Helper functions
# =========================

def filter_spikes(trace):
    """Replace spikes using nearby non-spike values."""
    arr = np.asarray(trace, dtype=float)
    out = arr.copy()

    spike_idx = np.where(arr > SPIKE_THRESHOLD_MS)[0]

    for i in spike_idx:
        lo = max(0, i - SPIKE_HALF_WINDOW)
        hi = min(len(arr), i + SPIKE_HALF_WINDOW + 1)

        neighbors = arr[lo:hi]
        valid = neighbors[
            (neighbors <= SPIKE_THRESHOLD_MS)
            & np.isfinite(neighbors)
        ]

        if len(valid) > 0:
            out[i] = np.mean(valid)

    return out, len(spike_idx)


def moving_average(arr, window):
    """Smooth a trace using a moving average."""
    window = min(window, len(arr))

    if window <= 1:
        return arr

    kernel = np.ones(window) / window
    return np.convolve(arr, kernel, mode="valid")


def normalize_size(value):
    """Normalize numeric size values."""
    if value is None:
        return "unknown"

    try:
        return float(value)
    except (ValueError, TypeError):
        return str(value)


def safe_filename(value):
    """Make a safe output filename."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(value))


# =========================
# Load traces
# =========================

if not TRACE_FILE.exists():
    print("traces.jsonl not found.")
    sys.exit(1)

traces = []

with TRACE_FILE.open("r", encoding="utf-8") as f:
    for line_number, line in enumerate(f, start=1):
        if not line.strip():
            continue

        try:
            t = json.loads(line)
        except json.JSONDecodeError:
            print(f"Skipping invalid JSON at line {line_number}")
            continue

        if not isinstance(t, dict):
            continue

        label = t.get("label")
        trace = t.get("trace")

        if not isinstance(label, str) or not label.strip():
            continue

        if not isinstance(trace, list) or not trace:
            continue

        try:
            arr = np.asarray(trace, dtype=float)
        except (ValueError, TypeError):
            continue

        if not np.all(np.isfinite(arr)):
            continue

        size = normalize_size(t.get("sizeGB"))

        if ANALYSIS_SIZE_GB is not None:
            if size != normalize_size(ANALYSIS_SIZE_GB):
                continue

        t["_size"] = size
        t["_label"] = label.strip()
        traces.append(t)

if not traces:
    print("No matching traces found in traces.jsonl.")
    sys.exit(1)

print(f"\nLoaded {len(traces)} trace(s).")


# =========================
# Group by sizeGB
# =========================

by_size = defaultdict(list)

for t in traces:
    by_size[t["_size"]].append(t)

print("\nDetected file sizes:")
for size, records in by_size.items():
    print(f"  {size} GB: {len(records)} trace(s)")


# =========================
# Analyze each size group
# =========================

for size, records in sorted(
    by_size.items(), key=lambda item: str(item[0])
):
    print("\n" + "=" * 75)
    print(f"ANALYSIS: sizeGB={size}")
    print("=" * 75)

    min_len = min(len(t["trace"]) for t in records)

    print(f"Truncating traces to {min_len} samples.\n")

    by_label = defaultdict(list)

    print(
        f"{'label':<16}"
        f"{'session':<16}"
        f"{'median':>10}"
        f"{'p95':>10}"
        f"{'spikes':>10}"
    )
    print("-" * 62)

    for t in records:
        raw = t["trace"][:min_len]

        filtered, n_spikes = filter_spikes(raw)

        median = float(np.median(filtered))
        p95 = float(np.percentile(filtered, 95))

        label = t["_label"]
        session = str(t.get("session") or "-")

        print(
            f"{label:<16}"
            f"{session:<16}"
            f"{median:>10.4f}"
            f"{p95:>10.4f}"
            f"{n_spikes:>10}"
        )

        by_label[label].append({
            "filtered": filtered,
            "median": median,
            "p95": p95,
            "spikes": n_spikes,
        })

    # =========================
    # Summary by label
    # =========================

    print("\nSUMMARY BY LABEL")
    print("-" * 85)

    print(
        f"{'label':<16}"
        f"{'runs':>6}"
        f"{'median mean':>14}"
        f"{'p95 mean':>12}"
        f"{'p95 min':>12}"
        f"{'p95 max':>12}"
        f"{'spikes mean':>13}"
    )

    for label, runs in sorted(by_label.items()):
        medians = [r["median"] for r in runs]
        p95s = [r["p95"] for r in runs]
        spikes = [r["spikes"] for r in runs]

        print(
            f"{label:<16}"
            f"{len(runs):>6}"
            f"{np.mean(medians):>14.4f}"
            f"{np.mean(p95s):>12.4f}"
            f"{min(p95s):>12.4f}"
            f"{max(p95s):>12.4f}"
            f"{np.mean(spikes):>13.1f}"
        )

    # =========================
    # Automatic label colors
    # =========================

    labels = sorted(by_label.keys())

    cmap = plt.get_cmap("tab20")

    label_colors = {
        label: cmap(i % 20)
        for i, label in enumerate(labels)
    }

    # =========================
    # Plot graphs
    # =========================

    fig, (ax_trace, ax_hist) = plt.subplots(
        2, 1, figsize=(11, 8)
    )

    for label in labels:
        runs = by_label[label]
        color = label_colors[label]

        # Mean of repeated runs
        mean_curve = np.mean(
            [r["filtered"] for r in runs],
            axis=0
        )

        smoothed = moving_average(
            mean_curve,
            SMOOTH_WINDOW
        )

        ax_trace.plot(
            smoothed,
            color=color,
            label=f"{label} ({len(runs)} runs)",
            linewidth=1.5
        )

        # Distribution
        all_values = np.concatenate(
            [r["filtered"] for r in runs]
        )

        ax_hist.hist(
            all_values,
            bins=np.linspace(0, 0.3, 61),
            density=True,
            histtype="step",
            color=color,
            linewidth=1.5,
            label=f"{label} ({len(runs)} runs)"
        )

    ax_trace.set_title(
        f"Mean read latency over time (sizeGB={size})"
    )
    ax_trace.set_xlabel("Sample index")
    ax_trace.set_ylabel("Read latency (ms)")
    ax_trace.legend()
    ax_trace.grid(alpha=0.3)

    ax_hist.set_title(
        "Latency distribution (0-0.3ms)"
    )
    ax_hist.set_xlabel("Read latency (ms)")
    ax_hist.set_ylabel("Probability density")
    ax_hist.legend()
    ax_hist.grid(alpha=0.3)

    plt.tight_layout()

    filename = (
        f"comparison_{safe_filename(size)}GB.png"
    )

    output_path = BASE_DIR / filename

    plt.savefig(output_path, dpi=150)
    plt.close(fig)

    print(f"\nSaved {filename}")

print("\nAll analyses completed!")
