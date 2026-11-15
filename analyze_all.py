"""Compare all labels at once and check whether single trials can be classified.

Usage:
    python analyze_all.py

Input:
    data/*.jsonl   (one file per label, e.g. data/idle.jsonl, data/figma.jsonl)

Output:
    img/compare_all.png   4-panel comparison graph
    terminal              per-label summary + classification accuracy

Only numpy and matplotlib are needed. Original JSONL files are never modified.
"""

import json
import sys
from pathlib import Path

try:
    import matplotlib
    matplotlib.use("Agg")
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

SPIKE_MS = 1.0        # latencies above this are treated as outliers
PLOT_BINS = 500       # time bins for the graph (10s / 500 = 20ms)
PLOT_SMOOTH = 15      # moving-average window (in bins) for the graph
FEATURE_BINS = 100    # time bins for classification (10s / 100 = 100ms, mean latency each)
KNN_K = 3             # neighbours used by the kNN classifier
CV_FOLDS = 5          # cross-validation folds
CV_REPEATS = 10       # repeat CV with different random splits
SEED = 0


# =========================
# Load
# =========================

def load_all():
    data = {}
    for f in sorted(DATA_DIR.glob("*.jsonl")):
        trials = []
        with f.open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    trials.append(json.loads(line))
        if trials:
            data[f.stem] = trials
    return data


def bin_trace(trace, n_bins):
    """Average latency and relative read count per time bin."""
    tr = np.clip(np.asarray(trace, dtype=float), 0, SPIKE_MS)
    idx = np.arange(len(tr)) * n_bins // len(tr)
    counts = np.bincount(idx, minlength=n_bins)
    latency = np.bincount(idx, weights=tr, minlength=n_bins) / np.maximum(counts, 1)
    rate = counts / (len(tr) / n_bins)
    return latency, rate


# =========================
# Part 1: comparison graph
# =========================

def plot_comparison(data, labels):
    cmap = plt.get_cmap("tab10")
    color = {l: cmap(i % 10) for i, l in enumerate(labels)}

    fig, axes = plt.subplots(2, 2, figsize=(16, 10))
    ax_time, ax_rate, ax_hist, ax_med = axes.ravel()

    rates, medians = {}, {}
    print(f"\n{'label':<12}{'trials':>7}{'reads/s':>10}{'median ms':>11}")
    print("-" * 40)

    for l in labels:
        curves, r, m, values = [], [], [], []
        for t in data[l]:
            raw = np.asarray(t["trace"], dtype=float)
            duration_s = (t.get("durationMs") or 10000) / 1000
            clean = raw[raw <= SPIKE_MS]

            r.append(len(raw) / duration_s)
            m.append(np.median(clean))
            values.append(clean)
            curves.append(bin_trace(raw, PLOT_BINS)[0])

        mean_curve = np.mean(curves, axis=0)
        smooth = np.convolve(mean_curve, np.ones(PLOT_SMOOTH) / PLOT_SMOOTH, mode="same")
        x = np.linspace(0, 10, PLOT_BINS)
        lw = 2.5 if l == "idle" else 1.5

        ax_time.plot(x, smooth, color=color[l], lw=lw, label=f"{l} ({len(data[l])})")
        ax_hist.hist(np.concatenate(values), bins=np.linspace(0, 0.3, 61), density=True,
                     histtype="step", color=color[l], lw=lw, label=l)
        rates[l], medians[l] = r, m

        print(f"{l:<12}{len(data[l]):>7}{np.mean(r):>10.0f}{np.mean(m):>11.4f}")

    opened = [t["openedOffsetMs"] for l in labels for t in data[l] if t.get("openedOffsetMs")]
    if opened:
        open_s = np.median(opened) / 1000
        ax_time.axvline(open_s, color="gray", ls="--", lw=1)
        ax_time.text(open_s + 0.05, ax_time.get_ylim()[1] * 0.97, "site opened",
                     color="gray", va="top")

    ax_time.set(title="Mean read latency over time", xlabel="Time (s)", ylabel="Latency (ms)")
    ax_time.legend(fontsize=8, ncol=2)
    ax_time.grid(alpha=0.3)

    ax_hist.set(title="Latency distribution (0-0.3 ms)", xlabel="Latency (ms)", ylabel="Density")
    ax_hist.legend(fontsize=8, ncol=2)
    ax_hist.grid(alpha=0.3)

    for ax, values, title, ylabel in [
        (ax_rate, rates, "Reads per second, per trial (lower = more contention)", "Samples / s"),
        (ax_med, medians, "Median latency, per trial", "Latency (ms)"),
    ]:
        bp = ax.boxplot([values[l] for l in labels], patch_artist=True, widths=0.6)
        for patch, l in zip(bp["boxes"], labels):
            patch.set_facecolor(color[l])
            patch.set_alpha(0.6)
        ax.set_xticks(range(1, len(labels) + 1), labels, rotation=30)
        ax.set(title=title, ylabel=ylabel)
        ax.grid(alpha=0.3, axis="y")

    size_gb = data[labels[0]][0].get("sizeGB")
    fig.suptitle(f"FROST traces - all labels (sizeGB={size_gb})", fontsize=14)
    plt.tight_layout()

    IMG_DIR.mkdir(parents=True, exist_ok=True)
    out = IMG_DIR / "compare_all.png"
    plt.savefig(out, dpi=130)
    plt.close(fig)
    print(f"\nSaved {out}")


# =========================
# Part 2: classification check
# =========================

def knn_predict(X_train, y_train, X_test, k):
    mu, sd = X_train.mean(0), X_train.std(0) + 1e-9
    A, B = (X_train - mu) / sd, (X_test - mu) / sd
    dist = ((B[:, None, :] - A[None]) ** 2).sum(-1)
    nearest = np.argsort(dist, axis=1)[:, :k]
    preds = []
    for row in y_train[nearest]:
        vals, counts = np.unique(row, return_counts=True)
        preds.append(vals[counts.argmax()])
    return np.array(preds)


def cross_validate(X, y, labels, rng, repeats=CV_REPEATS):
    """Stratified k-fold CV. Returns mean acc, std acc, confusion matrix (first repeat)."""
    accs = []
    conf = np.zeros((len(labels), len(labels)), dtype=int)
    for rep in range(repeats):
        fold = np.empty(len(y), dtype=int)
        for l in labels:
            ii = rng.permutation(np.where(y == l)[0])
            fold[ii] = np.arange(len(ii)) % CV_FOLDS
        for fo in range(CV_FOLDS):
            te, tr = fold == fo, fold != fo
            pred = knn_predict(X[tr], y[tr], X[te], KNN_K)
            accs.append((pred == y[te]).mean())
            if rep == 0:
                for a, p in zip(y[te], pred):
                    conf[labels.index(a), labels.index(p)] += 1
    return np.mean(accs), np.std(accs), conf


def classification_check(data, labels):
    rng = np.random.default_rng(SEED)

    # Feature = mean latency per time bin only.
    # The relative read-rate per bin (bin_trace()[1]) is ~1.0 everywhere, so after
    # standardisation it is pure noise and drags kNN accuracy down (~0.47 vs ~0.81).
    X, y = [], []
    for l in labels:
        for t in data[l]:
            latency, _ = bin_trace(t["trace"], FEATURE_BINS)
            X.append(latency)
            y.append(l)
    X, y = np.array(X), np.array(y)

    print("\n" + "=" * 70)
    print(f"CLASSIFICATION CHECK (kNN k={KNN_K}, {CV_FOLDS}-fold CV x {CV_REPEATS})")
    print("=" * 70)

    # 1) all labels
    acc, sd, conf = cross_validate(X, y, labels, rng)
    print(f"\n[1] All {len(labels)} labels: accuracy {acc:.3f} +/- {sd:.3f}"
          f"   (random guess {1 / len(labels):.3f})")

    print("\nConfusion matrix (rows = true, cols = predicted):")
    print(" " * 12 + "".join(f"{l[:7]:>8}" for l in labels))
    for i, l in enumerate(labels):
        print(f"{l:<12}" + "".join(f"{v:>8}" for v in conf[i]))

    # 2) websites only
    sites = [l for l in labels if l != "idle"]
    if "idle" in labels and len(sites) >= 2:
        m = y != "idle"
        acc2, sd2, _ = cross_validate(X[m], y[m], sites, rng)
        print(f"\n[2] Websites only ({len(sites)}): accuracy {acc2:.3f} +/- {sd2:.3f}"
              f"   (random guess {1 / len(sites):.3f})")

        # 3) shuffled-label control: should drop to ~random guess
        acc3, _, _ = cross_validate(X[m], rng.permutation(y[m]), sites, rng, repeats=3)
        print(f"[3] Websites, SHUFFLED labels (control): accuracy {acc3:.3f}"
              f"   -> should be close to {1 / len(sites):.3f}")

    # 4) drift check: train on earlier trials, test on later trials
    first_half = np.zeros(len(y), dtype=bool)
    for l in labels:
        ii = np.where(y == l)[0]
        first_half[ii[: len(ii) // 2]] = True
    pred = knn_predict(X[first_half], y[first_half], X[~first_half], KNN_K)
    print(f"[4] Train on first half of trials -> test on second half: "
          f"accuracy {(pred == y[~first_half]).mean():.3f}")

    # measurement time per label (time-of-day confound check)
    print("\nMeasurement time per label (startedAt, UTC):")
    for l in labels:
        ts = [t.get("startedAt") or "" for t in data[l]]
        print(f"  {l:<12}{ts[0][:16]}  ~  {ts[-1][:16]}")


# =========================
# Main
# =========================

def main():
    if not DATA_DIR.exists():
        print("data/ directory not found.")
        sys.exit(1)

    data = load_all()
    if not data:
        print("No JSONL files found in data/.")
        sys.exit(1)

    labels = sorted(data, key=lambda l: (l != "idle", l))  # idle first
    plot_comparison(data, labels)

    if len(labels) >= 2 and min(len(data[l]) for l in labels) >= CV_FOLDS:
        classification_check(data, labels)
    else:
        print(f"\nSkipping classification: need >= 2 labels with >= {CV_FOLDS} trials each.")

    print("\nDone!")


if __name__ == "__main__":
    main()
