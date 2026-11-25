#!/usr/bin/env python3
"""
연속 녹화(data/recordings/<label>.jsonl)를 슬라이딩 윈도우로 분석해
'언제 어떤 사이트가 열렸나'를 탐지하고, 정답(visits)과 비교해 채점/그래프를 만든다.

사용법:
  python analyze_recording.py [recording_label]
  (생략하면 data/recordings/ 안 가장 최근 파일)
"""
import json, glob, os, sys
import numpy as np
from sklearn.ensemble import ExtraTreesClassifier

SITES = ['idle', 'youtube', 'coupang', 'figma', 'google', 'chatgpt', 'instagram', 'navermap']
W, BIN, SPIKE = 3.0, 0.1, 1.0
NBW = int(round(W / BIN))
HOP, TH, MATCH = 0.5, 0.5, 3.0   # 윈도우 이동(s), visit 임계, 정답매칭 허용오차(s)

# ---------- 단일탭 학습 데이터 ----------
def load_singletab():
    out = []
    for f in sorted(glob.glob('data/**/*.jsonl', recursive=True)):
        if '/recordings/' in f:
            continue
        for line in open(f, encoding='utf-8'):
            if not line.strip():
                continue
            r = json.loads(line); lab = r.get('label')
            if not isinstance(lab, str):
                continue
            if lab.endswith('_bg_blank'):
                lab = lab[:-9]
            if lab not in SITES:
                continue
            tr = np.asarray(r['trace'], float); tm = r.get('times')
            if not (isinstance(tm, list) and len(tm) == len(tr)):
                continue
            out.append((lab, tr, np.asarray(tm, float) / 1000.0))
    return out

def wfeat(lat, tsec):
    clip = np.clip(lat, 0, SPIKE)
    idx = np.clip((tsec / BIN).astype(int), 0, NBW - 1)
    mean = np.zeros(NBW); rate = np.zeros(NBW); p90 = np.zeros(NBW)
    for b in range(NBW):
        s = clip[idx == b]; rate[b] = len(s) / BIN
        if len(s):
            mean[b] = s.mean(); p90[b] = np.percentile(s, 90)
    n = len(lat)
    g = np.array([n, np.log1p(n), clip.mean(), np.median(clip) if n else 0,
                  np.percentile(clip, 90) if n else 0, (lat > 0.3).mean() if n else 0,
                  rate.mean(), rate.std()])
    return np.concatenate([mean, p90, rate, g])

def train_window_model(data):
    X, y = [], []
    for lab, lat, t in data:
        lo = 2.0 if lab != 'idle' else 0.0
        s = lo
        while s + W <= min(t.max(), 10.0):
            m = (t >= s) & (t < s + W)
            if m.sum() > 20:
                X.append(wfeat(lat[m], t[m] - s)); y.append(lab)
            s += 1.0
    clf = ExtraTreesClassifier(n_estimators=400, random_state=0, n_jobs=-1).fit(np.stack(X), np.array(y))
    return clf

# ---------- 녹화 분석 ----------
def analyze(clf, rec):
    cls = list(clf.classes_); ii = cls.index('idle')
    lat = np.asarray(rec['trace'], float)
    tm = rec.get('times')
    t = (np.asarray(tm, float) / 1000.0) if isinstance(tm, list) and len(tm) == len(lat) \
        else np.arange(len(lat)) * (rec.get('durationMs', 10000) / 1000) / len(lat)
    total = (rec.get('durationMs') or t.max() * 1000) / 1000.0

    times, scores, preds = [], [], []
    s = 0.0
    while s + W <= total:
        m = (t >= s) & (t < s + W)
        if m.sum() > 20:
            p = clf.predict_proba(wfeat(lat[m], t[m] - s).reshape(1, -1))[0]
            times.append(s + W / 2); scores.append(1.0 - p[ii])
            pp = p.copy(); pp[ii] = -1; preds.append(cls[int(np.argmax(pp))])
        s += HOP
    times = np.array(times); scores = np.array(scores); preds = np.array(preds)

    # 임계 넘는 연속구간의 최고점 = 검출된 visit
    detected = []; i = 0
    while i < len(scores):
        if scores[i] >= TH:
            j = i
            while j < len(scores) and scores[j] >= TH:
                j += 1
            k = i + int(np.argmax(scores[i:j]))
            detected.append((float(times[k]), str(preds[k]), float(scores[k])))
            i = j
        else:
            i += 1
    return times, scores, detected, total

def score_vs_truth(rec, detected):
    truth = []
    for v in (rec.get('visits') or []):
        to = v.get('openedOffsetMs', v.get('openMs'))
        if to is not None:
            truth.append((v.get('site'), to / 1000.0))
    used = [False] * len(detected); rows = []; hit = sitehit = 0
    for site, to in truth:
        best = None
        for di, (td, ps, sc) in enumerate(detected):
            if used[di] or abs(td - to) > MATCH:
                continue
            if best is None or abs(td - to) < abs(detected[best][0] - to):
                best = di
        if best is None:
            rows.append((to, site, None, None, None)); continue
        used[best] = True; hit += 1; td, ps, sc = detected[best]
        ok = ps == site; sitehit += ok
        rows.append((to, site, td, ps, sc))
    fa = [detected[i] for i, u in enumerate(used) if not u]
    return truth, rows, fa, hit, sitehit

def plot(label, times, scores, detected, truth, rows, fa, total, out):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.ticker import PercentFormatter
    fig, ax = plt.subplots(figsize=(14, 5))
    ax.plot(times, scores, lw=1.4, color='#2a78d6')
    ax.axhline(TH, ls='--', c='gray', lw=1); ax.text(0.5, TH + 0.02, 'threshold', color='gray', fontsize=9)
    ax.set_ylim(-0.05, 1.2); ax.set_xlim(0, total)
    ax.yaxis.set_major_formatter(PercentFormatter(1))
    ax.set_ylabel('visit score (1 - P(idle))'); ax.set_xlabel('time (s)')
    ax.set_title(f'{label}: detected (top) vs real visits (bottom)')
    matched_td = {r[2] for r in rows if r[2] is not None}
    for td, ps, sc in detected:
        ok = any(r[2] == td and r[3] == r[1] for r in rows)
        fa_mark = td in {d[0] for d in fa}
        col = 'red' if fa_mark else ('green' if ok else 'orange')
        ax.annotate(f'{ps}\n{sc:.0%}', xy=(td, 1.0), xytext=(td, 1.15), ha='center', fontsize=8,
                    color=col, arrowprops=dict(arrowstyle='-|>', color=col))
    for site, to in truth:
        ax.annotate(f'{site}', xy=(to, 0.0), xytext=(to, -0.16), ha='center', fontsize=8, color='#333',
                    arrowprops=dict(arrowstyle='-|>', color='#333'))
    plt.subplots_adjust(top=0.80, bottom=0.22)
    plt.savefig(out, dpi=130, bbox_inches='tight')
    print('graph ->', out)

def main():
    label = sys.argv[1] if len(sys.argv) > 1 else None
    files = sorted(glob.glob('data/recordings/*.jsonl'), key=os.path.getmtime, reverse=True)
    if label:
        path = f'data/recordings/{label}.jsonl'
    elif files:
        path = files[0]
    else:
        print('data/recordings/ 에 녹화 파일이 없습니다. record.html로 먼저 녹화하세요.'); return
    label = os.path.basename(path)[:-6]
    recs = [json.loads(l) for l in open(path) if l.strip()]
    print(f'녹화 파일: {path}  ({len(recs)}개 녹화)')

    print('윈도우 모델 학습 중...')
    clf = train_window_model(load_singletab())

    os.makedirs('figures', exist_ok=True)
    for ri, rec in enumerate(recs):
        times, scores, detected, total = analyze(clf, rec)
        truth, rows, fa, hit, sitehit = score_vs_truth(rec, detected)
        tag = f'{label}' + (f'_{ri+1}' if len(recs) > 1 else '')
        print(f'\n=== {tag}  (길이 {total:.0f}s, 정답 {len(truth)}개) ===')
        print(' real(s)  real site   found(s) found site  score')
        for to, site, td, ps, sc in rows:
            if td is None:
                print(f'  {to:6.1f}  {str(site):<10}  (missed)')
            else:
                print(f'  {to:6.1f}  {str(site):<10}  {td:6.1f}  {ps:<10} {sc:.0%}'
                      + ('' if ps == site else '  <- wrong'))
        for td, ps, sc in fa:
            print(f'  (none)              {td:6.1f}  {ps:<10} {sc:.0%}  <- false alarm')
        print(f'  >>> {hit}/{len(truth)} 검출, 사이트 {sitehit}개 정답, 오탐 {len(fa)}개')
        plot(tag, times, scores, detected, truth, rows, fa, total, f'figures/{tag}_detection.png')

if __name__ == '__main__':
    main()
