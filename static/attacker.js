// Main-thread orchestrator. The actual OPFS reads/writes happen inside
// worker.js, because createSyncAccessHandle() only works in a Worker.

// [수정] session 인자를 추가했다. 같은 날·같은 세션에서 모은 데이터를 묶기 위한 값이다.
function runAttack({ label, sizeGB, durationMs, session, onStatus }) {
  return new Promise((resolve, reject) => {
    // [수정] 측정 시작 시각을 기록한다. 나중에 측정 순서나 시간대를 확인할 때 쓴다.
    const startedAt = new Date().toISOString();
    const worker = new Worker("/worker.js");

    worker.onmessage = async (e) => {
      const msg = e.data;

      if (msg.type === "status") {
        onStatus(msg.msg);
        return;
      }

      if (msg.type === "error") {
        worker.terminate();
        reject(new Error(msg.message));
        return;
      }

      if (msg.type === "done") {
        worker.terminate();
        onStatus("Uploading...");
        try {
          const res = await fetch("/submit", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            // [수정] 측정 조건과 브라우저 정보를 함께 보낸다.
            // userAgent는 같은 기기·브라우저에서 모은 데이터인지 구분하는 데 쓴다.
            body: JSON.stringify({
              label: msg.label,
              trace: msg.trace,
              sizeGB: msg.sizeGB,
              durationMs: msg.durationMs,
              session,
              startedAt,
              userAgent: navigator.userAgent,
            }),
          });
          const result = await res.json();
          // [수정] 서버가 400을 돌려주면 (잘못된 데이터) 에러로 처리해서 화면에 보여준다.
          if (!res.ok) throw new Error(result.error || "upload failed");
          onStatus(`Done. Saved ${result.samples} samples under label "${msg.label}".`);
          resolve(msg.trace);
        } catch (err) {
          reject(err);
        }
      }
    };

    worker.onerror = (e) => {
      worker.terminate();
      reject(new Error(e.message || "worker error"));
    };

    worker.postMessage({ label, sizeGB, durationMs });
  });
}

window.runAttack = runAttack;
