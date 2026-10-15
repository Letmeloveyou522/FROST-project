# FROST Mini Reproduction — Week 1 PoC

## Run it

```bash
pip install flask matplotlib --break-system-packages
python server.py
```

Open `http://localhost:8000` in your browser (Chrome recommended, needs COOP/COEP headers).

## Week 1 test procedure

1. Label: `idle`, start measurement without doing anything else → wait 10s
2. Open a YouTube video in another tab and run another measurement with label: `youtube`
3. Run `python quick_check.py` → check `comparison.png`
4. If the two lines look visibly different (e.g. `youtube` is noisier or has higher latency spikes), the PoC succeeded

## Notes

- `sizeGB` MUST be larger than your laptop's total RAM. Otherwise the OS page cache will serve every read from memory, and you'll see no SSD contention signal (everything near 0ms).
- The first run takes a while to write the large file to OPFS (tens of seconds to a few minutes depending on disk speed). Later runs reuse the existing file and are fast.
- Measurements keep appending to `traces.jsonl`. You can record the same label multiple times (useful later for training a classifier with more samples).
