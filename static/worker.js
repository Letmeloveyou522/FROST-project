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

async function createLargeFile(sizeGB, onProgress) {
  const root = await navigator.storage.getDirectory();
  const handle = await root.getFileHandle(FILE_NAME, { create: true });
  const accessHandle = await handle.createSyncAccessHandle();

  const targetSize = sizeGB * 1024 * 1024 * 1024;
  const existingSize = accessHandle.getSize();

  if (existingSize >= targetSize) {
    return accessHandle; // already big enough, reuse as-is
  }

  const totalChunks = Math.ceil(targetSize / CHUNK_SIZE);
  const buffer = new Uint8Array(CHUNK_SIZE);
  let seed = Date.now() & 0xffffffff;

  for (let i = 0; i < totalChunks; i++) {
    seed = fillPseudoRandom(buffer, seed ^ i);
    accessHandle.write(buffer, { at: i * CHUNK_SIZE });
    if (onProgress && i % 100 === 0) {
      onProgress(i / totalChunks);
    }
  }
  accessHandle.flush();
  return accessHandle;
}

function measureTrace(accessHandle, durationMs, onTick) {
  const trace = [];
  const fileSize = accessHandle.getSize();
  const buf = new Uint8Array(READ_SIZE);
  const start = performance.now();

  while (performance.now() - start < durationMs) {
    const maxOffset = fileSize - READ_SIZE;
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
  const { label, sizeGB, durationMs } = e.data;
  try {
    self.postMessage({ type: "status", msg: `Preparing ${sizeGB} GB file (slow only on first run)...` });
    const accessHandle = await createLargeFile(sizeGB, (p) =>
      self.postMessage({ type: "status", msg: `Filling file: ${(p * 100).toFixed(0)}%` })
    );

    self.postMessage({ type: "status", msg: `Measuring (${durationMs / 1000}s)...` });
    const trace = measureTrace(accessHandle, durationMs, (n) =>
      self.postMessage({ type: "status", msg: `Measuring... ${n} samples` })
    );
    accessHandle.close();

    self.postMessage({ type: "done", label, trace });
  } catch (err) {
    self.postMessage({ type: "error", message: err.message });
  }
};
