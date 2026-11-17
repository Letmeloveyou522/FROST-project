
"""Quick check: summarize and plot website-specific traces.

Usage:
    python quick_check.py
    python quick_check.py GPT
    python quick_check.py figma

Input:
    data/GPT.jsonl
    data/figma.jsonl

Output:
    img/GPT.png
    img/figma.png

Original JSONL files are never modified.
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
DATA_DIR = BASE_DIR / "data"
IMG_DIR = BASE_DIR / "img"

SPIKE_THRESHOLD_MS = 1.0
SPIKE_HALF_WINDOW = 50
SMOOTH_WINDOW_MS = 100   # moving-average window for the time plot
TIME_BIN_MS = 1.0        # resolution of the common time grid

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


def trace_to_time_grid(raw, values, total_ms, times=None):
    """Bin a trace onto a common time grid.

    With `times` (the recorded start time of each read, in ms), those are
    used directly. Without them, samples are treated as back-to-back reads,
    so each sample's elapsed time is estimated as the cumulative sum of the
    raw latencies before it; this ignores the loop's own overhead, so the
    estimate runs slow. Returns the mean of `values` (e.g. the
    spike-filtered trace)
    per TIME_BIN_MS bin over [0, total_ms); empty bins are interpolated.
    """
    raw = np.asarray(raw, dtype=float)
    arr = np.asarray(values, dtype=float)
    if times is not None:
        t = np.asarray(times, dtype=float)
    else:
        t = np.cumsum(raw) - raw  # estimated start time of each read (ms)

    n_bins = int(total_ms // TIME_BIN_MS)
    edges = np.arange(n_bins + 1) * TIME_BIN_MS

    sums, _ = np.histogram(t, bins=edges, weights=arr)
    counts, _ = np.histogram(t, bins=edges)

    out = np.full(n_bins, np.nan)
    has = counts > 0
    out[has] = sums[has] / counts[has]

    if not np.all(has) and np.any(has):
        idx = np.arange(n_bins)
        out[~has] = np.interp(idx[~has], idx[has], out[has])

    return out


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

def valid_times(times, n):
    """Recorded per-read start times (ms) as an array, or None.

    Older records have no `times`; anything malformed is treated the same,
    so the trace still shows up in the estimated-time plot.
    """
    if not isinstance(times, list) or len(times) != n:
        return None
    try:
        arr = np.asarray(times, dtype=float)
    except (ValueError, TypeError):
        return None
    if arr.ndim != 1 or not np.all(np.isfinite(arr)) or np.any(np.diff(arr) < 0):
        return None
    return arr


def load_traces(file_path):
    traces = []

    with file_path.open("r", encoding="utf-8") as f:
        for line_number, line in enumerate(f, start=1):
            if not line.strip():
                continue

            try:
                t = json.loads(line)
            except json.JSONDecodeError:
                print(
                    f"Skipping invalid JSON at "
                    f"{file_path.name}:{line_number}"
                )
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

            if arr.ndim != 1 or not np.all(np.isfinite(arr)):
                continue

            size = normalize_size(t.get("sizeGB"))

            if ANALYSIS_SIZE_GB is not None:
                if size != normalize_size(ANALYSIS_SIZE_GB):
                    continue

            t["_size"] = size
            t["_label"] = label.strip()
            t["_times"] = valid_times(t.get("times"), len(arr))

            traces.append(t)

    return traces


# =========================
# Real-time plot
# =========================

def plot_real_times(ax, by_label, labels, label_colors, size):
    """Mean latency over time using the recorded per-read times.

    Only runs that have `times` are included; the legend shows how many.
    """
    real_runs = {
        label: [r for r in by_label[label] if r["times"] is not None]
        for label in labels
    }

    # Common span: the shortest recording, measured to its last read
    total_ms = min(
        float(r["times"][-1])
        for runs in real_runs.values()
        for r in runs
    )
    smooth_bins = max(1, int(SMOOTH_WINDOW_MS / TIME_BIN_MS))

    for label in labels:
        runs = real_runs[label]
        if not runs:
            continue

        mean_curve = np.mean(
            [
                trace_to_time_grid(
                    r["raw_full"], r["filtered_full"], total_ms, r["times"]
                )
                for r in runs
            ],
            axis=0
        )
        smoothed = moving_average(mean_curve, smooth_bins)

        # Centre each smoothed point on its window, in seconds
        offset = (len(mean_curve) - len(smoothed)) / 2
        times_s = (
            (np.arange(len(smoothed)) + offset + 0.5)
            * TIME_BIN_MS / 1000
        )

        total = len(by_label[label])
        count = f"{len(runs)} runs" if len(runs) == total else f"{len(runs)}/{total} runs"
        ax.plot(
            times_s,
            smoothed,
            color=label_colors[label],
            label=f"{label} ({count})",
            linewidth=1.5
        )

    # The server measures the open from when it receives /auto/trial-start,
    # a few ms after the worker's clock starts; close enough at this scale.
    open_times = [
        r["open_ms"]
        for runs in real_runs.values()
        for r in runs
        if isinstance(r["open_ms"], (int, float))
    ]
    if open_times:
        ax.axvline(
            np.mean(open_times) / 1000,
            color="gray",
            linestyle="--",
            linewidth=1,
            label="page opened"
        )

    ax.set_title(
        f"Mean read latency over time (sizeGB={size}, recorded times)"
    )
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Read latency (ms)")
    ax.legend()
    ax.grid(alpha=0.3)


# =========================
# Analyze a website
# =========================

def analyze_file(file_path):
    traces = load_traces(file_path)

    if not traces:
        print(f"No matching traces found in {file_path.name}.")
        return

    print(f"\nLoaded {len(traces)} trace(s) from {file_path.name}.")

    # Group by sizeGB
    by_size = defaultdict(list)

    for t in traces:
        by_size[t["_size"]].append(t)

    print("\nDetected file sizes:")
    for size, records in by_size.items():
        print(f"  {size} GB: {len(records)} trace(s)")

    IMG_DIR.mkdir(parents=True, exist_ok=True)

    # Analyze each size group
    for size, records in sorted(
        by_size.items(), key=lambda item: str(item[0])
    ):
        print("\n" + "=" * 75)
        print(f"ANALYSIS: {file_path.stem}, sizeGB={size}")
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
            filtered_full, _ = filter_spikes(t["trace"])
            filtered, n_spikes = filter_spikes(t["trace"][:min_len])

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
                "raw_full": t["trace"],
                "filtered_full": filtered_full,
                "times": t["_times"],
                "open_ms": t.get("openedOffsetMs"),
                "median": median,
                "p95": p95,
                "spikes": n_spikes,
            })

        # Summary by label
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

        # Automatic label colors
        labels = sorted(by_label.keys())

        cmap = plt.get_cmap("tab20")

        label_colors = {
            label: cmap(i % 20)
            for i, label in enumerate(labels)
        }

        # A third panel uses the recorded per-read times, when any run has them
        has_real_times = any(
            r["times"] is not None
            for runs in by_label.values()
            for r in runs
        )

        # Plot graphs
        if has_real_times:
            fig, (ax_trace, ax_hist, ax_real) = plt.subplots(
                3, 1, figsize=(11, 12)
            )
        else:
            fig, (ax_trace, ax_hist) = plt.subplots(
                2, 1, figsize=(11, 8)
            )
            ax_real = None

        # Common time span: the shortest trace's total elapsed time
        total_ms = min(
            float(np.sum(r["raw_full"]))
            for runs in by_label.values()
            for r in runs
        )
        smooth_bins = max(1, int(SMOOTH_WINDOW_MS / TIME_BIN_MS))

        for label in labels:
            runs = by_label[label]
            color = label_colors[label]

            mean_curve = np.mean(
                [
                    trace_to_time_grid(
                        r["raw_full"], r["filtered_full"], total_ms
                    )
                    for r in runs
                ],
                axis=0
            )

            smoothed = moving_average(
                mean_curve,
                smooth_bins
            )

            # Centre each smoothed point on its window, in seconds
            offset = (len(mean_curve) - len(smoothed)) / 2
            times_s = (
                (np.arange(len(smoothed)) + offset + 0.5)
                * TIME_BIN_MS / 1000
            )

            ax_trace.plot(
                times_s,
                smoothed,
                color=color,
                label=f"{label} ({len(runs)} runs)",
                linewidth=1.5
            )

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
            f"Mean read latency over time (sizeGB={size}, "
            f"time estimated from latencies)"
        )
        open_times = [
            r["open_ms"]
            for runs in by_label.values()
            for r in runs
            if isinstance(r["open_ms"], (int, float))
        ]
        if open_times:
            ax_trace.axvline(
                np.mean(open_times) / 1000,
                color="gray",
                linestyle="--",
                linewidth=1,
                label="page opened"
            )

        ax_trace.set_xlabel("Time (s)")
        ax_trace.set_ylabel("Read latency (ms)")
        ax_trace.legend()
        ax_trace.grid(alpha=0.3)

        if ax_real is not None:
            plot_real_times(ax_real, by_label, labels, label_colors, size)

        ax_hist.set_title(
            "Latency distribution (0-0.3ms)"
        )
        ax_hist.set_xlabel("Read latency (ms)")
        ax_hist.set_ylabel("Probability density")
        ax_hist.legend()
        ax_hist.grid(alpha=0.3)

        plt.tight_layout()

        # Use simple filename for a single size group
        if len(by_size) == 1:
            filename = f"{safe_filename(file_path.stem)}.png"
        else:
            filename = (
                f"{safe_filename(file_path.stem)}_"
                f"{safe_filename(size)}GB.png"
            )

        output_path = IMG_DIR / filename

        plt.savefig(output_path, dpi=150)
        plt.close(fig)

        print(f"\nSaved {output_path}")


# =========================
# Main
# =========================

def main():
    if not DATA_DIR.exists():
        print("data/ directory not found.")
        sys.exit(1)

    # Optional website label
    if len(sys.argv) > 2:
        print("Usage: python quick_check.py [label]")
        sys.exit(1)

    if len(sys.argv) == 2:
        label = sys.argv[1]

        if not re.fullmatch(r"[A-Za-z0-9_-]+", label):
            print("Invalid label.")
            sys.exit(1)

        files = [DATA_DIR / f"{label}.jsonl"]
    else:
        files = sorted(DATA_DIR.glob("*.jsonl"))

    if not files:
        print("No JSONL files found in data/.")
        sys.exit(1)

    for file_path in files:
        if not file_path.exists():
            print(f"File not found: {file_path}")
            continue

        analyze_file(file_path)

    print("\nAll analyses completed!")


if __name__ == "__main__":
    main()
