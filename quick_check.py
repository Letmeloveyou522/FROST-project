"""Quick visual check: plot all collected traces to see if they're
distinguishable by eye. Run this after collecting a few traces with
different labels (e.g. 'idle' vs 'youtube').

Usage:
    python quick_check.py
"""
import json
import sys

try:
    import matplotlib.pyplot as plt
except ImportError:
    print("matplotlib not installed. Run: pip install matplotlib --break-system-packages")
    sys.exit(1)

traces = []
try:
    with open("traces.jsonl") as f:
        for line in f:
            traces.append(json.loads(line))
except FileNotFoundError:
    print("traces.jsonl not found yet. Run at least one measurement first.")
    sys.exit(1)

if not traces:
    print("traces.jsonl is empty.")
    sys.exit(1)

print(f"Loaded {len(traces)} trace(s):")
for t in traces:
    print(f"  - label={t['label']!r}, samples={len(t['trace'])}")

plt.figure(figsize=(10, 5))
for t in traces:
    plt.plot(t["trace"], label=t["label"], alpha=0.7, linewidth=0.8)
plt.legend()
plt.ylabel("Read latency (ms)")
plt.xlabel("Sample index")
plt.title("OPFS random-read latency per trace")
plt.tight_layout()
plt.savefig("comparison.png", dpi=150)
print("Saved comparison.png")
