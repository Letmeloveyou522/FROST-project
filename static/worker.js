// This MUST run inside a Worker. createSyncAccessHandle() is only
// available in a dedicated worker context, not on the main thread.

const FILE_NAME = "bigfile.bin";
const CHUNK_SIZE = 1024 * 1024; // 1 MB per write chunk
const READ_SIZE = 4096; // 4 KB reads

// crypto.getRandomValues() refuses to fill more than 65536 bytes per call,
// and calling it once per 64KB sub-chunk to fill many GB would be very slow.
// We don't need cryptographically secure randomness here -- we just need
// data that isn't all-zero/identical, so the OS/browser doesn't silently
// deduplicate or compress it. A fast PRNG (xorshift32) is enough and is
// orders of magnitude faster.
function fillPseudoRandom(buffer, seed) {
  // Fill as 32-bit words for speed, xorshift32 PRNG.
  const words = new Uint32Array(buffer.buffer, buffer.byteOffset, buffer.length >> 2);
  let x = seed || 0x9e3779b9;
  for (let i = 0; i < words.length; i++) {
    x ^= x << 13;
    x ^= x >>> 17;
    x ^= x << 5;
    words[i] = x;
  }
  // Fill any trailing bytes not covered by the 32-bit words.
  for (let i = words.length * 4; i < buffer.length; i++) {
    x ^= x << 13;
    x ^= x >>> 17;
    x ^= x << 5;
    buffer[i] = x & 0xff;
  }
  return x;
}

const BYTES_PER_GB = 1024 * 1024 * 1024;

// Makes bigfile.bin EXACTLY sizeGB long. A file left over from an earlier run
// with a different size is never reused as-is: a larger file is truncated
// down, a smaller one is extended. Without this, asking for 4 GB after a 30 GB
// run silently measured the old 30 GB file.
async function createLargeFile(sizeGB, onProgress) {
  const root = await navigator.storage.getDirectory();
  const handle = await root.getFileHandle(FILE_NAME, { create: true });
  const accessHandle = await handle.createSyncAccessHandle();

  try {
    const targetSize = Math.round(sizeGB * BYTES_PER_GB);
    const existingSize = accessHandle.getSize();

    if (existingSize > targetSize) {
      // Too big: cut it down. The remaining prefix is already fully written.
      accessHandle.truncate(targetSize);
    } else if (existingSize < targetSize) {
      // Too small (new file, smaller earlier run, or an interrupted write).
      // The file is always written front to back, so everything before the
      // last full chunk is valid; resume from there instead of starting over.
      const startChunk = Math.floor(existingSize / CHUNK_SIZE);
      const totalChunks = Math.ceil(targetSize / CHUNK_SIZE);
      const buffer = new Uint8Array(CHUNK_SIZE);
      let seed = Date.now() & 0xffffffff;

      for (let i = startChunk; i < totalChunks; i++) {
        const at = i * CHUNK_SIZE;
        const len = Math.min(CHUNK_SIZE, targetSize - at);
        seed = fillPseudoRandom(buffer, seed ^ i);
        const written = accessHandle.write(buffer.subarray(0, len), { at });
        if (written !== len) {
          throw new Error(`Short write at ${at}: wrote ${written} of ${len} bytes (disk full?)`);
        }
        if (onProgress && (i - startChunk) % 100 === 0) {
          onProgress((i - startChunk) / (totalChunks - startChunk));
        }
      }
    }
    accessHandle.flush();

    verifyFile(accessHandle, targetSize);
    return accessHandle;
  } catch (err) {
    accessHandle.close();
    throw err;
  }
}

// Refuses to measure unless the file is exactly the requested size and its
// first and last blocks are actually readable.
function verifyFile(accessHandle, targetSize) {
  const actualSize = accessHandle.getSize();
  if (actualSize !== targetSize) {
    throw new Error(
      `File size check failed: expected ${targetSize} bytes, got ${actualSize}`
    );
  }
  if (actualSize < READ_SIZE * 2) {
    throw new Error(`File is too small to measure (${actualSize} bytes)`);
  }
  const buf = new Uint8Array(READ_SIZE);
  for (const at of [0, actualSize - READ_SIZE]) {
    const n = accessHandle.read(buf, { at });
    if (n !== READ_SIZE) {
      throw new Error(`File read check failed at offset ${at}: read ${n} of ${READ_SIZE} bytes`);
    }
  }
}

// Reads the whole file front to back so the OS caches it in RAM. In auto mode
// this runs after the page-cache reset and buffer, right before recording.
const PRELOAD_CHUNK = 8 * 1024 * 1024;
function preloadFile(accessHandle, onProgress) {
  const size = accessHandle.getSize();
  const buf = new Uint8Array(PRELOAD_CHUNK);
  const t0 = performance.now();
  for (let at = 0; at < size; at += PRELOAD_CHUNK) {
    accessHandle.read(buf, { at });
    if (onProgress && (at / PRELOAD_CHUNK) % 64 === 0) onProgress(at / size);
  }
  return performance.now() - t0;
}

function measureTrace(accessHandle, durationMs, onTick) {
  const trace = [];
  const fileSize = accessHandle.getSize();
  const maxOffset = fileSize - READ_SIZE;
  const buf = new Uint8Array(READ_SIZE);
  const start = performance.now();

  while (performance.now() - start < durationMs) {
    const offset = Math.floor(Math.random() * maxOffset);
    const t0 = performance.now();
    accessHandle.read(buf, { at: offset });
    const t1 = performance.now();
    trace.push(+(t1 - t0).toFixed(4));
    if (onTick && trace.length % 200 === 0) onTick(trace.length);
  }
  return trace;
}

self.onmessage = async (e) => {
  const { label, sizeGB, durationMs, preload } = e.data;
  // [수정] 에러가 나도 핸들을 닫을 수 있도록 밖으로 뺐다.
  let accessHandle = null;
  try {
    // [수정] 잘못된 입력(빈칸, 음수, 문자 등)은 측정 전에 막는다.
    if (!(sizeGB > 0) || !(durationMs > 0)) {
      throw new Error("File size and duration must be positive numbers");
    }

    self.postMessage({ type: "status", msg: `Preparing ${sizeGB} GB file (slow only on first run)...` });
    accessHandle = await createLargeFile(sizeGB, (p) =>
      self.postMessage({ type: "status", msg: `Filling file: ${(p * 100).toFixed(0)}%` })
    );

    if (preload) {
      const ms = preloadFile(accessHandle, (p) =>
        self.postMessage({ type: "status", msg: `Preloading file into RAM: ${(p * 100).toFixed(0)}%` })
      );
      const mbps = accessHandle.getSize() / (1024 * 1024) / (ms / 1000);
      self.postMessage({ type: "status", msg: `Preloaded in ${(ms / 1000).toFixed(1)}s (${mbps.toFixed(0)} MB/s)` });
    }

    // Recorded with the trace so every saved run states the size it really used.
    const fileSizeBytes = accessHandle.getSize();
    self.postMessage({
      type: "status",
      msg: `Verified ${(fileSizeBytes / BYTES_PER_GB).toFixed(2)} GB file. Measuring (${durationMs / 1000}s)...`,
    });
    // Tells the page the recording window starts now (used by auto mode to
    // open the target link at a fixed offset from this moment).
    self.postMessage({ type: "measuring" });
    const trace = measureTrace(accessHandle, durationMs, (n) =>
      self.postMessage({ type: "status", msg: `Measuring... ${n} samples` })
    );
    accessHandle.close();
    accessHandle = null;

    // [수정] 측정 조건(sizeGB, durationMs)을 메인 스레드로 다시 돌려보낸다. 서버 저장에 쓰인다.
    self.postMessage({ type: "done", label, trace, sizeGB, fileSizeBytes, durationMs, preload: !!preload });
  } catch (err) {
    // [수정] 에러 시에도 핸들을 닫아서, 다음 측정에서 파일이 잠겨 있는 문제를 막는다.
    if (accessHandle) accessHandle.close();
    self.postMessage({ type: "error", message: err.message });
  }
};
