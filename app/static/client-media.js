"use strict";

// Local preview policy, independent of payload decoding and cryptography.
(() => {
  const MAX_BYTES = 1024 * 1024;
  const MAX_DIMENSION = 4096;
  const MAX_PIXELS = 4_000_000;
  const MAX_ACTIVE = 3;
  const TYPES = new Set(["image/jpeg", "image/png", "image/webp"]);
  // Optional metadata is CRC/boundary checked, then omitted from the decoder's copy.
  const PNG_METADATA = new Set(["bKGD", "iCCP", "cICP", "eXIf", "iTXt", "iDOT", "tEXt", "zTXt"]);
  const generic = mime => mime === "" || mime === "application/octet-stream";
  const dimensions = (width, height) => width > 0 && height > 0
    && width <= MAX_DIMENSION && height <= MAX_DIMENSION && width * height <= MAX_PIXELS;
  const text = (bytes, offset, length) => String.fromCharCode(...bytes.subarray(offset, offset + length));
  const crcTable = Uint32Array.from({ length: 256 }, (_, n) => {
    for (let i = 0; i < 8; i++) n = n & 1 ? 0xedb88320 ^ (n >>> 1) : n >>> 1;
    return n >>> 0;
  });
  function crc(bytes, start, end) {
    let value = 0xffffffff;
    for (let i = start; i < end; i++) value = crcTable[(value ^ bytes[i]) & 255] ^ (value >>> 8);
    return (value ^ 0xffffffff) >>> 0;
  }

  function png(bytes, view, preview) {
    if (text(bytes, 0, 8) !== "\x89PNG\r\n\x1a\n") return null;
    preview?.push(bytes.subarray(0, 8));
    let offset = 8, width, height, color, depth, palette = 0, data = 0, ended = false;
    const seen = new Set(), zlib = [];
    for (let count = 0; count < 1024 && offset + 12 <= bytes.length; count++) {
      const size = view.getUint32(offset), type = text(bytes, offset + 4, 4);
      const start = offset + 8, end = start + size;
      if (end + 4 > bytes.length || !/^[A-Za-z]{2}[A-Z][A-Za-z]$/.test(type)
          || crc(bytes, offset + 4, end) !== view.getUint32(end)) return null;
      if (count === 0 && type !== "IHDR") return null;
      if (type === "IHDR") {
        if (seen.has(type) || size !== 13) return null;
        width = view.getUint32(start); height = view.getUint32(start + 4);
        depth = bytes[start + 8]; color = bytes[start + 9];
        const depths = { 0: [1, 2, 4, 8, 16], 2: [8, 16], 3: [1, 2, 4, 8], 4: [8, 16], 6: [8, 16] };
        if (!dimensions(width, height) || !depths[color]?.includes(depth)
            || bytes[start + 10] || bytes[start + 11] || bytes[start + 12] > 1) return null;
      } else if (type === "PLTE") {
        if (seen.has(type) || data || ![2, 3, 6].includes(color)
            || !size || size % 3 || size > 768 || (color === 3 && size / 3 > 2 ** depth)) return null;
        palette = size / 3;
      } else if (type === "IDAT") {
        if (ended || (color === 3 && !palette)) return null;
        for (let i = start; i < end && zlib.length < 2; i++) zlib.push(bytes[i]);
        data += size;
      } else if (type === "IEND") {
        if (size || end + 4 !== bytes.length || data < 6 || zlib.length !== 2
            || (zlib[0] & 15) !== 8 || (zlib[0] >>> 4) > 7
            || (zlib[1] & 32) || ((zlib[0] << 8) + zlib[1]) % 31) return null;
        preview?.push(bytes.subarray(offset, end + 4));
        return { mime: "image/png", width, height };
      } else if (PNG_METADATA.has(type)) {
        if (!size || (seen.has(type) && !["iTXt", "tEXt", "zTXt"].includes(type))) return null;
        if (type === "bKGD") {
          if (data || size !== (color === 3 ? 1 : [0, 4].includes(color) ? 2 : 6)) return null;
          if (color === 3) { if (!palette || bytes[start] >= palette) return null; }
          else for (let at = start; at < end; at += 2) {
            if (view.getUint16(at) >= 2 ** depth) return null;
          }
        }
      } else {
        // Conservative decoder allowlist: no APNG or unknown chunks.
        const sizes = { gAMA: 4, cHRM: 32, sRGB: 1, pHYs: 9 };
        if (seen.has(type) || data) return null;
        if (type === "tRNS") {
          if (!(color === 0 && size === 2 || color === 2 && size === 6
              || color === 3 && palette && size > 0 && size <= palette)) return null;
        } else if (sizes[type] !== size || type === "gAMA" && !view.getUint32(start)
            || type === "sRGB" && bytes[start] > 3
            || type === "pHYs" && bytes[start + 8] > 1) return null;
      }
      if (!PNG_METADATA.has(type)) preview?.push(bytes.subarray(offset, end + 4));
      if (data && type !== "IDAT") ended = true;
      seen.add(type);
      offset = end + 4;
    }
    return null;
  }

  function jpeg(bytes, view) {
    if (bytes[0] !== 255 || bytes[1] !== 216) return null;
    let offset = 2, frame = null, scans = 0, restart = 0;
    const quant = new Set(), huffman = new Set();
    for (let count = 0; count < 1024 && offset < bytes.length; count++) {
      if (bytes[offset++] !== 255) return null;
      while (bytes[offset] === 255) offset++;
      const marker = bytes[offset++];
      if (marker === 217) return frame && scans && offset === bytes.length
        ? { mime: "image/jpeg", width: frame.width, height: frame.height } : null;
      if (offset + 2 > bytes.length) return null;
      const size = view.getUint16(offset), start = offset + 2, end = offset + size;
      if (size < 2 || end > bytes.length) return null;
      if (marker === 192 || marker === 194) {
        if (frame || size < 8 || bytes[start] !== 8) return null;
        const height = view.getUint16(start + 1), width = view.getUint16(start + 3);
        const components = bytes[start + 5], ids = new Map();
        let blocks = 0;
        if (!dimensions(width, height) || ![1, 3].includes(components) || size !== 8 + components * 3) return null;
        for (let i = start + 6; i < end; i += 3) {
          const sample = bytes[i + 1];
          if (ids.has(bytes[i]) || !(sample >> 4) || (sample >> 4) > 4
              || !(sample & 15) || (sample & 15) > 4 || bytes[i + 2] > 3) return null;
          ids.set(bytes[i], bytes[i + 2]);
          blocks += (sample >> 4) * (sample & 15);
        }
        if (blocks > 10) return null;
        frame = { width, height, ids, progressive: marker === 194 };
      } else if (marker === 219) {
        let at = start;
        while (at < end) {
          const info = bytes[at++], step = info >> 4 ? 2 : 1;
          if (info >> 4 > 1 || (info & 15) > 3 || at + 64 * step > end) return null;
          for (let i = 0; i < 64; i++, at += step) {
            if (!(step === 1 ? bytes[at] : view.getUint16(at))) return null;
          }
          quant.add(info & 15);
        }
        if (at === start) return null;
      } else if (marker === 196) {
        let at = start;
        while (at < end) {
          const info = bytes[at++];
          if ((info >> 4) > 1 || (info & 15) > 3 || at + 16 > end) return null;
          let symbols = 0, codes = 1;
          for (let i = 0; i < 16; i++) {
            const n = bytes[at++]; symbols += n; codes = codes * 2 - n;
            if (codes < 0) return null;
          }
          if (!symbols || symbols > 256 || at + symbols > end) return null;
          at += symbols;
          huffman.add(info);
        }
        if (at === start) return null;
      } else if (marker === 221) {
        if (size !== 4) return null;
        restart = view.getUint16(start);
      } else if (marker === 218) {
        if (!frame || size < 6) return null;
        const n = bytes[start], selected = new Set();
        const first = bytes[end - 3], last = bytes[end - 2], approx = bytes[end - 1];
        if (!n || n > frame.ids.size || size !== 6 + 2 * n || first > last || last > 63
            || (approx >> 4) > 13 || (approx & 15) > 13
            || ((approx >> 4) && (approx >> 4) !== (approx & 15) + 1)
            || (!frame.progressive && (first || last !== 63 || approx))
            || (frame.progressive && (first === 0 ? last !== 0 : n !== 1))) return null;
        for (let i = start + 1; i < end - 3; i += 2) {
          const id = bytes[i], tables = bytes[i + 1];
          if (!frame.ids.has(id) || selected.has(id) || !quant.has(frame.ids.get(id))
              || (tables >> 4) > 3 || (tables & 15) > 3
              || (first === 0 && !huffman.has(tables >> 4))
              || (last > 0 && !huffman.has(16 + (tables & 15)))) return null;
          selected.add(id);
        }
        offset = end;
        let entropy = 0;
        while (offset < bytes.length) {
          if (bytes[offset] !== 255) { offset++; entropy++; continue; }
          const mark = offset++;
          while (bytes[offset] === 255) offset++;
          const code = bytes[offset];
          if (code === 0) { offset++; entropy++; }
          else if (code >= 208 && code <= 215 && restart) offset++;
          else { offset = mark; break; }
        }
        if (!entropy) return null;
        scans++;
        continue;
      } else if (!(marker >= 224 && marker <= 239 || marker === 254)) return null;
      offset = end;
    }
    return null;
  }

  function webp(bytes, view) {
    if (text(bytes, 0, 4) !== "RIFF" || text(bytes, 8, 4) !== "WEBP"
        || view.getUint32(4, true) + 8 !== bytes.length) return null;
    let offset = 12, canvas = null, image = null, alpha = false;
    for (let count = 0; count < 1024 && offset + 8 <= bytes.length; count++) {
      const type = text(bytes, offset, 4), size = view.getUint32(offset + 4, true);
      const start = offset + 8, end = start + size, next = end + (size & 1);
      if (!size || next > bytes.length || (size & 1 && bytes[end] !== 0)) return null;
      if (type === "VP8X") {
        if (offset !== 12 || size !== 10 || bytes[start] & ~16
            || bytes[start + 1] || bytes[start + 2] || bytes[start + 3]) return null;
        const u24 = at => bytes[at] + bytes[at + 1] * 256 + bytes[at + 2] * 65536;
        canvas = { width: u24(start + 4) + 1, height: u24(start + 7) + 1, alpha: !!(bytes[start] & 16) };
        if (!dimensions(canvas.width, canvas.height)) return null;
      } else if (type === "ALPH") {
        if (!canvas?.alpha || alpha || image || size < 2 || bytes[start] & 192
            || (bytes[start] & 3) > 1 || (bytes[start] >> 4 & 3) > 1) return null;
        alpha = true;
      } else if (type === "VP8 " || type === "VP8L") {
        if (image) return null;
        let width, height, transparency = false;
        if (type === "VP8 ") {
          if (size <= 10 || (bytes[start] & 1) || !(bytes[start] & 16)
              || (bytes[start] >> 1 & 7) > 3 || text(bytes, start + 3, 3) !== "\x9d\x01\x2a"
              || bytes[start + 7] & 192 || bytes[start + 9] & 192) return null;
          const partition = (bytes[start] + bytes[start + 1] * 256 + bytes[start + 2] * 65536) >>> 5;
          if (!partition || partition > size - 10) return null;
          width = view.getUint16(start + 6, true); height = view.getUint16(start + 8, true);
          transparency = alpha;
        } else {
          if (size <= 5 || bytes[start] !== 47 || alpha) return null;
          const header = view.getUint32(start + 1, true);
          if (header >>> 29) return null;
          width = (header & 16383) + 1; height = (header >>> 14 & 16383) + 1;
          transparency = !!(header & 0x10000000);
        }
        if (!dimensions(width, height) || (canvas && (canvas.width !== width
            || canvas.height !== height || canvas.alpha !== transparency))) return null;
        image = { mime: "image/webp", width, height };
      } else return null; // Includes animation, metadata and unknown chunk layouts.
      offset = next;
    }
    return offset === bytes.length ? image : null;
  }

  function inspect(bytes, mime = "", preview) {
    try {
      if (!(bytes instanceof Uint8Array) || bytes.length < 12 || bytes.length > MAX_BYTES
          || typeof mime !== "string") return null;
      mime = mime.toLowerCase();
      if (!TYPES.has(mime) && !generic(mime)) return null;
      const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
      const result = png(bytes, view, preview) || jpeg(bytes, view) || webp(bytes, view);
      return result && (generic(mime) || mime === result.mime) ? Object.freeze(result) : null;
    } catch { return null; }
  }

  function createManager(onAvailable = () => {}) {
    const active = new Map();
    const controls = new WeakMap();
    function release(record) {
      record.cancelled = true;
      clearTimeout(record.timer);
      record.image?.removeAttribute("src");
      record.image?.remove();
      if (record.url) { URL.revokeObjectURL(record.url); record.url = null; }
      record.status.textContent = "";
      record.status.className = "media-status";
      if (record.settled && active.delete(record.host)) onAvailable();
    }
    function clear() { for (const record of active.values()) release(record); }
    function attach(host, bytesProvider, mime) {
      if (controls.has(host)) return controls.get(host);
      if (typeof mime !== "string" || (!TYPES.has(mime.toLowerCase()) && !generic(mime))) {
        return Object.assign(() => {}, { load: async () => {}, available: () => false });
      }
      const status = document.createElement("span");
      status.className = "media-status"; status.setAttribute("role", "status");
      status.textContent = "";
      if (TYPES.has(mime.toLowerCase())) host.className = "media-preview media-frame";
      const state = { failed: false };
      const unavailable = () => {
        state.failed = true;
        status.className = "media-status";
        status.textContent = "";
        host.className = "media-preview";
        onAvailable();
      };
      const load = async () => {
        if (state.failed || active.has(host) || host.isConnected === false) return;
        if (active.size >= MAX_ACTIVE) { status.textContent = ""; return; }
        let record;
        try {
          const supplied = bytesProvider();
          if (!(supplied instanceof Uint8Array) || supplied.length > MAX_BYTES) {
            unavailable(); return;
          }
          // Inspect the same private snapshot passed to Blob, even for shared input buffers.
          const bytes = new Uint8Array(supplied), preview = [];
          const info = inspect(bytes, mime, preview);
          const image = document.createElement("img");
          if (!info || typeof image.decode !== "function") { unavailable(); return; }
          record = { host, status, state, image, url: null, cancelled: false, settled: false };
          active.set(host, record);
          // A stable frame exists before decoding, including after history reload.
          host.className = "media-preview media-frame";
          image.alt = "Attachment preview"; image.className = "media-image";
          image.decoding = "async"; image.draggable = false;
          // No Blob is created until the entire snapshot passes validation.
          record.url = URL.createObjectURL(new Blob(preview.length ? preview : [bytes], { type: info.mime }));
          status.className = "media-status media-loading";
          status.textContent = "Loading image…";
          image.src = record.url;
          record.timer = setTimeout(() => { release(record); unavailable(); }, 10000);
          try {
            await image.decode();
            if (record.cancelled || host.isConnected === false) { release(record); return; }
            const width = image.naturalWidth, height = image.naturalHeight;
            if (!dimensions(width, height) || !(width === info.width && height === info.height
                || info.mime === "image/jpeg" && width === info.height && height === info.width)) {
              throw new Error("Invalid preview dimensions");
            }
            host.append(image);
            status.className = "media-status";
            status.textContent = "";
          } finally { clearTimeout(record.timer); }
        } catch {
          const cancelled = record?.cancelled;
          if (record) release(record);
          if (!cancelled) unavailable();
        } finally {
          if (record) {
            record.settled = true;
            if (record.cancelled && active.delete(host)) onAvailable();
          }
        }
      };
      host.append(status);
      const dispose = () => { if (active.has(host)) release(active.get(host)); };
      dispose.load = load;
      dispose.available = () => !state.failed;
      controls.set(host, dispose);
      return dispose;
    }
    return Object.freeze({ attach, clear });
  }
  globalThis.SidewordMedia = Object.freeze({ inspect: (bytes, mime) => inspect(bytes, mime), createManager,
    MAX_BYTES, MAX_DIMENSION, MAX_PIXELS, MAX_ACTIVE });
})();
