// Main-thread orchestrator. The actual OPFS reads/writes happen inside
// worker.js, because createSyncAccessHandle() only works in a Worker.

function runAttack({ label, sizeGB, durationMs, onStatus }) {
  return new Promise((resolve, reject) => {
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
            body: JSON.stringify({ label: msg.label, trace: msg.trace }),
          });
          const result = await res.json();
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
