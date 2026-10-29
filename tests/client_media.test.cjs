const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { deflateSync } = require('node:zlib');

const fixtures = name => fs.readFileSync(`tests/fixtures/media/${name}`);
const source = fs.readFileSync('app/static/client-media.js', 'utf8');
function media({ pending = false, fail = false, width = 2, height = 2 } = {}) {
  const urls = new Map(), timers = new Map(), images = [], decoders = [], blobs = [];
  let created = 0, sequence = 0;
  class Node {
    constructor(tag) { this.tagName = tag; this.children = []; this.events = new Map(); this.attrs = new Map(); this.isConnected = true; this.style = {}; }
    append(...nodes) { for (const node of nodes) { node.parent = this; this.children.push(node); } }
    remove() { if (this.parent) this.parent.children = this.parent.children.filter(node => node !== this); }
    setAttribute(name, value) { this.attrs.set(name, value); }
    removeAttribute(name) { this.attrs.delete(name); if (name === 'src') this.src = ''; }
    addEventListener(name, action) { this.events.set(name, action); }
    click() { return this.events.get('click')({stopPropagation(){}}); }
  }
  const document = { createElement(tag) {
    const node = new Node(tag);
    if (tag === 'img') {
      node.naturalWidth = width; node.naturalHeight = height;
      node.decode = () => {
        if (pending) return new Promise((resolve, reject) => decoders.push({resolve, reject}));
        return fail ? Promise.reject(new Error('synthetic decoder error')) : Promise.resolve();
      };
      images.push(node);
    }
    return node;
  }};
  const context = vm.createContext({ Uint8Array, Uint32Array, DataView, Blob, document,
    URL: {
      createObjectURL(blob) { const url = `blob:synthetic-${++created}`; urls.set(url, blob); blobs.push(blob); return url; },
      revokeObjectURL(url) { urls.delete(url); },
    },
    setTimeout(action) { const id = ++sequence; timers.set(id, action); return id; },
    clearTimeout(id) { timers.delete(id); },
    fetch() { throw new Error('Preview must never fetch'); },
    console: new Proxy({}, {get() { throw new Error('Preview must never log'); }}),
  });
  vm.runInContext(source, context);
  const api = context.SidewordMedia, manager = api.createManager();
  function attach(bytes = fixtures('valid.png'), mime = 'image/png') {
    const host = new Node('div'), dispose = manager.attach(host, () => bytes, mime);
    return { host, dispose, status: host.children[0] };
  }
  return {api, manager, attach, urls, timers, images, decoders, blobs, document, context, created: () => created};
}

for (const [name, mime] of [['valid.jpg', 'image/jpeg'], ['progressive.jpg', 'image/jpeg'],
  ['valid.png', 'image/png'], ['valid.webp', 'image/webp'], ['lossless.webp', 'image/webp'],
  ['alpha.webp', 'image/webp']]) {
  test(`valid ${name} has verified dimensions, exact MIME and unchanged original bytes`, async () => {
    const app = media(), bytes = fixtures(name), original = Buffer.from(bytes);
    const result = app.api.inspect(bytes, mime);
    assert.ok(result);
    assert.deepEqual([result.width, result.height, result.mime], [2, 2, mime]);
    const card = app.attach(bytes, mime);
    assert.equal(app.created(), 0);
    await card.dispose.load();
    assert.equal(app.urls.size, 1);
    assert.equal(app.blobs[0].type, mime);
    assert.deepEqual(Buffer.from(await app.blobs[0].arrayBuffer()), original);
    assert.deepEqual(bytes, original);
    assert.equal(card.host.children.at(-1).tagName, 'img');
    app.manager.clear();
    assert.equal(app.urls.size, 0);
  });
}

test('signatures govern generic files; forged declared types cannot select a decoder', () => {
  const {api} = media();
  assert.equal(api.inspect(fixtures('valid.png'), '').mime, 'image/png');
  assert.equal(api.inspect(fixtures('valid.png'), 'application/octet-stream').mime, 'image/png');
  for (const mime of ['image/jpeg', 'image/svg+xml', 'text/html', 'application/xml', 'application/pdf', 'application/x-msdownload']) {
    assert.equal(api.inspect(fixtures('valid.png'), mime), null);
  }
  for (const bytes of ['<svg onload="alert(1)">', '<html><script>alert(1)</script>',
    '<?xml version="1.0"?>', '%PDF-1.7', 'MZ synthetic executable']) {
    for (const mime of ['image/png', 'image/jpeg', 'image/webp', '']) {
      assert.equal(api.inspect(Buffer.from(bytes), mime), null);
    }
  }
});

test('every truncated prefix and trailing polyglot payload fails structural validation', () => {
  const {api} = media();
  for (const name of ['valid.png', 'valid.jpg', 'valid.webp', 'lossless.webp']) {
    const bytes = fixtures(name);
    for (let length = 0; length < bytes.length; length++) {
      assert.equal(api.inspect(bytes.subarray(0, length)), null, `${name}: ${length}`);
    }
    assert.equal(api.inspect(Buffer.concat([bytes, Buffer.from('<svg/>')])), null);
  }
});

function crc32(bytes) {
  let value = 0xffffffff;
  for (const byte of bytes) {
    value ^= byte;
    for (let bit = 0; bit < 8; bit++) value = value >>> 1 ^ (value & 1 ? 0xedb88320 : 0);
  }
  return (value ^ 0xffffffff) >>> 0;
}
function pngDimensions(width, height) {
  const bytes = Buffer.from(fixtures('valid.png'));
  bytes.writeUInt32BE(width, 16); bytes.writeUInt32BE(height, 20);
  bytes.writeUInt32BE(crc32(bytes.subarray(12, 29)), 29);
  return bytes;
}

function pngChunk(type, payload) {
  const chunk = Buffer.alloc(payload.length + 12);
  chunk.writeUInt32BE(payload.length); chunk.write(type, 4);
  payload.copy(chunk, 8);
  chunk.writeUInt32BE(crc32(chunk.subarray(4, -4)), chunk.length - 4);
  return chunk;
}

test('ordinary PNG backgrounds and macOS-style metadata preview without decoding metadata or changing originals', async () => {
  const original = fixtures('valid.png');
  const metadata = [
    pngChunk('bKGD', Buffer.from([0, 255, 0, 255, 0, 255])),
    pngChunk('iCCP', Buffer.concat([Buffer.from('Synthetic\0\0'), deflateSync(Buffer.alloc(200000))])),
    pngChunk('cICP', Buffer.from([1, 13, 0, 1])),
    pngChunk('eXIf', Buffer.from([73, 73, 42, 0, 8, 0, 0, 0])),
    pngChunk('iTXt', Buffer.from('Comment\0\0\0\0\0<svg onload="synthetic"/>')),
    pngChunk('iDOT', Buffer.alloc(28)),
    pngChunk('zTXt', Buffer.concat([Buffer.from('Comment\0\0'), deflateSync(Buffer.alloc(200000))])),
  ];
  const bytes = Buffer.concat([original.subarray(0, 33), ...metadata, original.subarray(33)]);
  const unchanged = Buffer.from(bytes), app = media(), card = app.attach(bytes);
  assert.ok(app.api.inspect(bytes, 'image/png'));
  await card.dispose.load();
  assert.equal(card.host.children.at(-1).tagName, 'img');
  assert.deepEqual(Buffer.from(await app.blobs[0].arrayBuffer()), original);
  assert.deepEqual(bytes, unchanged);
  app.manager.clear();
  assert.equal(app.urls.size, 0);
});

test('PNG metadata CRC/bounds and background values still fail closed', () => {
  const {api} = media(), original = fixtures('valid.png');
  const insert = chunk => Buffer.concat([original.subarray(0, 33), chunk, original.subarray(33)]);
  const valid = pngChunk('iCCP', Buffer.from('opaque synthetic metadata'));
  const corrupt = Buffer.from(valid); corrupt[8] ^= 1;
  assert.equal(api.inspect(insert(corrupt)), null);
  assert.equal(api.inspect(insert(valid.subarray(0, -1))), null);
  for (const payload of [Buffer.from([0]), Buffer.from([1, 0, 0, 0, 0, 0])]) {
    assert.equal(api.inspect(insert(pngChunk('bKGD', payload))), null);
  }
  assert.equal(api.inspect(insert(pngChunk('aBCD', Buffer.from([1])))), null);
  assert.equal(api.inspect(insert(pngChunk('acTL', Buffer.alloc(8)))), null);
});

test('late PNG validation failures never expose partially prepared preview bytes', async () => {
  const original = fixtures('valid.png');
  const metadata = pngChunk('iCCP', Buffer.from('discarded synthetic metadata'));
  const valid = Buffer.concat([original.subarray(0, 33), metadata, original.subarray(33)]);
  const badEnd = Buffer.from(valid); badEnd[badEnd.length - 1] ^= 1;
  const extra = Buffer.concat([valid, Buffer.from('<html>synthetic</html>')]);
  for (const bytes of [badEnd, extra, valid.subarray(0, -1)]) {
    const app = media(), card = app.attach(bytes);
    await card.dispose.load();
    assert.equal(app.created(), 0);
    assert.equal(app.blobs.length, 0);
    assert.equal(app.images.some(image => image.src), false);
    assert.equal(card.status.textContent, '');
    assert.equal(card.dispose.available(), false);
  }
});

test('zero, oversized and excessive-pixel headers are rejected before URL creation', async () => {
  const app = media();
  for (const [width, height] of [[0, 2], [2, 0], [4097, 1], [1, 4097], [3000, 3000], [0xffffffff, 2]]) {
    const bytes = pngDimensions(width, height);
    assert.equal(app.api.inspect(bytes), null);
    const card = app.attach(bytes);
    await card.dispose.load();
    assert.equal(card.status.textContent, '');
    assert.equal(card.dispose.available(), false);
  }
  assert.equal(app.created(), 0);
  assert.equal(app.api.inspect(new Uint8Array(1048577)), null);
  assert.equal(app.api.inspect(null), null);
});

test('PNG CRC, headers, palette and unsupported animation chunks fail closed', () => {
  const {api} = media();
  for (const offset of [0, 8, 12, 24, 25, 26, 27, 28, 33, 40]) {
    const bytes = Buffer.from(fixtures('valid.png')); bytes[offset] ^= 255;
    assert.equal(api.inspect(bytes), null);
  }
  const bytes = Buffer.from(fixtures('valid.png'));
  bytes[25] = 3; // Palette image without PLTE; repair IHDR CRC so this tests structure.
  bytes.writeUInt32BE(crc32(bytes.subarray(12, 29)), 29);
  assert.equal(api.inspect(bytes), null);
  const data = Buffer.from(fixtures('valid.png'));
  data.write('acTL', 37);
  const length = data.readUInt32BE(33);
  data.writeUInt32BE(crc32(data.subarray(37, 41 + length)), 41 + length);
  assert.equal(api.inspect(data), null);
});

test('JPEG frame dimensions, table corruption and mismatched scan headers fail closed', () => {
  const {api} = media(), original = fixtures('valid.jpg');
  const frame = original.indexOf(Buffer.from([255, 192]));
  assert.ok(frame >= 0);
  for (const [width, height] of [[0, 2], [2, 0], [4097, 1], [3000, 3000]]) {
    const bytes = Buffer.from(original);
    bytes.writeUInt16BE(height, frame + 5); bytes.writeUInt16BE(width, frame + 7);
    assert.equal(api.inspect(bytes), null);
  }
  const scan = original.indexOf(Buffer.from([255, 218]));
  for (const offset of [frame + 2, frame + 4, frame + 9, scan + 2, scan + 4, scan + 5]) {
    const bytes = Buffer.from(original); bytes[offset] = 255;
    assert.equal(api.inspect(bytes), null);
  }
});

function riff(chunks) {
  const data = Buffer.concat(chunks.map(([type, body]) => {
    const header = Buffer.alloc(8); header.write(type); header.writeUInt32LE(body.length, 4);
    return Buffer.concat([header, body, ...(body.length & 1 ? [Buffer.from([0])] : [])]);
  }));
  const header = Buffer.alloc(12); header.write('RIFF'); header.writeUInt32LE(data.length + 4, 4); header.write('WEBP', 8);
  return Buffer.concat([header, data]);
}

test('WebP bounds, reserved bits, animation and conflicting canvases fail closed', () => {
  const {api} = media(), original = fixtures('valid.webp');
  for (const offset of [0, 4, 8, 12, 16, 20, 23]) {
    const bytes = Buffer.from(original); bytes[offset] ^= 255;
    assert.equal(api.inspect(bytes), null);
  }
  for (const [width, height] of [[0, 2], [2, 0], [4097, 1], [3000, 3000]]) {
    const bytes = Buffer.from(original);
    bytes.writeUInt16LE(width, 26); bytes.writeUInt16LE(height, 28);
    assert.equal(api.inspect(bytes), null);
  }
  const chunk = original.subarray(20, 20 + original.readUInt32LE(16));
  for (const flag of [1, 2, 4, 8, 32, 64, 128]) {
    const canvas = Buffer.alloc(10); canvas[0] = flag; canvas[4] = canvas[7] = 1;
    assert.equal(api.inspect(riff([['VP8X', canvas], ['VP8 ', chunk]])), null);
  }
  const mismatch = Buffer.alloc(10); mismatch[4] = 2; mismatch[7] = 1;
  assert.equal(api.inspect(riff([['VP8X', mismatch], ['VP8 ', chunk]])), null);
  assert.equal(api.inspect(riff([['VP8 ', chunk], ['ANIM', Buffer.alloc(6)]])), null);
  assert.equal(api.inspect(riff([['VP8 ', chunk], ['VP8 ', chunk]])), null);
  const lossless = Buffer.from(fixtures('lossless.webp'));
  lossless[24] |= 224;
  assert.equal(api.inspect(lossless), null);
});

test('native decode errors and dishonest decoded dimensions leave the download fallback', async () => {
  for (const options of [{fail:true}, {width:8000}, {width:3}, {height:0}]) {
    const app = media(options), card = app.attach();
    await card.dispose.load();
    assert.equal(app.urls.size, 0);
    assert.equal(card.status.textContent, '');
    assert.equal(card.dispose.available(), false);
    assert.equal(card.host.children.some(node => node.tagName === 'img'), false);
    assert.equal(app.timers.size, 0);
  }
});

test('repeated attachment renders reuse controls and URL until explicit hide or cleanup', async () => {
  const app = media(), card = app.attach();
  const dispose = app.manager.attach(card.host, () => {throw new Error('Repeated render');}, 'image/png');
  assert.equal(dispose, card.dispose);
  assert.equal(card.host.children.length, 1);
  await card.dispose.load();
  app.manager.attach(card.host, () => fixtures('valid.png'), 'image/png');
  assert.equal(app.created(), 1);
  assert.equal(app.urls.size, 1);
  card.dispose();
  assert.equal(app.urls.size, 0);
  assert.equal(card.host.children.some(node => node.tagName === 'img'), false);
  await card.dispose.load();
  card.dispose(); card.dispose(); app.manager.clear();
  assert.equal(app.urls.size, 0);
});

test('active and cancelled decoders share a hard cap; timeout cannot open unlimited slots', async () => {
  const app = media({pending:true}), cards = Array.from({length:4}, () => app.attach());
  const pending = cards.slice(0, 3).map(card => card.dispose.load());
  await cards[3].dispose.load();
  assert.equal(app.created(), 3);
  assert.equal(app.urls.size, 3);
  assert.equal(cards[3].status.textContent, '');
  for (const callback of [...app.timers.values()]) callback();
  assert.equal(app.urls.size, 0);
  await cards[3].dispose.load();
  assert.equal(app.created(), 3); // Native promises haven't settled: do not start more decoders.
  app.decoders.forEach(decoder => decoder.resolve());
  await Promise.all(pending);
  assert.equal(cards.every(card => !card.host.children.some(node => node.tagName === 'img')), true);
});

test('completed previews stay within the cap and disposing one immediately frees its slot', async () => {
  const app = media(), cards = Array.from({length:4}, () => app.attach());
  for (const card of cards) await card.dispose.load();
  assert.equal(app.urls.size, 3);
  assert.equal(app.created(), 3);
  cards[0].dispose();
  assert.equal(app.urls.size, 2);
  await cards[3].dispose.load();
  assert.equal(app.urls.size, 3);
  app.manager.clear();
  assert.equal(app.urls.size, 0);
  assert.equal(app.timers.size, 0);
});

test('late decoding after removal or clear cannot resurrect a preview', async () => {
  for (const action of ['remove', 'clear', 'dispose']) {
    const app = media({pending:true}), card = app.attach();
    const loading = card.dispose.load();
    if (action === 'remove') card.host.isConnected = false;
    else if (action === 'clear') app.manager.clear();
    else card.dispose();
    app.decoders[0].resolve();
    await loading;
    assert.equal(app.urls.size, 0);
    assert.equal(card.host.children.some(node => node.tagName === 'img'), false);
  }
});

test('byte-provider and object-URL failures remain local and release reservations', async () => {
  const app = media();
  const host = app.document.createElement('div');
  const dispose = app.manager.attach(host, () => { throw new Error('synthetic-private'); }, 'image/png');
  await dispose.load();
  assert.equal(host.children[0].textContent, '');
  assert.equal(dispose.available(), false);
  assert.doesNotMatch(host.children[0].textContent, /synthetic-private/);
  const create = app.context.URL.createObjectURL;
  app.context.URL.createObjectURL = () => { throw new Error('synthetic-private'); };
  const card = app.attach();
  card.dispose();
  assert.equal(app.urls.size, 0);
  app.context.URL.createObjectURL = create;
  const next = app.attach();
  await next.dispose.load();
  assert.equal(app.urls.size, 1);
  app.manager.clear();
});
