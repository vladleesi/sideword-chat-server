const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto, createPrivateKey, createPublicKey, diffieHellman, hkdfSync,
  createCipheriv, createDecipheriv, createECDH } = require('node:crypto');

function client(crypto = webcrypto) {
  const element = { addEventListener() {} };
  const context = vm.createContext({
    TextEncoder, TextDecoder, DOMException, crypto, btoa, atob,
    document: { querySelector: () => element, addEventListener() {} },
    window: { addEventListener() {} },
  });
  vm.runInContext(fs.readFileSync('app/static/client-protocol.js', 'utf8'), context);
  vm.runInContext(fs.readFileSync('app/static/client.js', 'utf8')
    .replace(/start\(\)\.catch[^\n]+/, ''), context);
  // Storage behavior has separate tests; exercise actual encryption here.
  vm.runInContext('assertTrustedPeer = async () => {};', context);
  return { context, run: code => vm.runInContext(code, context) };
}

// Synthetic fixed P-256 keys only. Independent OpenSSL operations catch
// wire drift in both directions; production keys use the platform CSPRNG.
function importPoint(raw) {
  const point = Buffer.from(raw, 'base64');
  return createPublicKey({ format: 'jwk', key: { kty: 'EC', crv: 'P-256',
    x: point.subarray(1, 33).toString('base64url'),
    y: point.subarray(33).toString('base64url') } });
}
function pair(byte) {
  const key = createECDH('prime256v1');
  const scalar = Buffer.alloc(32, byte);
  key.setPrivateKey(scalar);
  const point = key.getPublicKey();
  const privateKey = createPrivateKey({ format: 'jwk', key: { kty: 'EC', crv: 'P-256',
    x: point.subarray(1, 33).toString('base64url'),
    y: point.subarray(33).toString('base64url'), d: scalar.toString('base64url') } });
  return { privateKey, publicKey: createPublicKey(privateKey),
    der: privateKey.export({ format: 'der', type: 'pkcs8' }), raw: point.toString('base64') };
}
const alice = pair(1), bob = pair(2), ephemeral = pair(3);
const info = Buffer.from('sideword-web-p256-v1|7|fixture-message|alice|bob');
const salt = Buffer.alloc(16, 4), iv = Buffer.alloc(12, 5);
const text = 'P-256 message: hello 🌍';
function key(sender, recipient, epk, saltValue, context) {
  const secrets = Buffer.concat([
    diffieHellman({ privateKey: sender.privateKey, publicKey: recipient.publicKey }),
    diffieHellman({ privateKey: epk.privateKey, publicKey: recipient.publicKey }),
  ]);
  return hkdfSync('sha256', secrets, saltValue, context, 32);
}
function fixture() {
  const cipher = createCipheriv('aes-256-gcm', key(alice, bob, ephemeral, salt, info), iv);
  cipher.setAAD(info);
  const ct = Buffer.concat([cipher.update(text, 'utf8'), cipher.final(), cipher.getAuthTag()]);
  return { v: 1, alg: 'P256-2DH-HKDF-SHA256-AES256GCM', epk: ephemeral.raw,
    salt: salt.toString('base64'), iv: iv.toString('base64'), ct: ct.toString('base64') };
}
function message(envelope = fixture()) {
  return { chat_id: 7, client_message_id: 'fixture-message', sender_public_id: 'alice',
    ciphertext: Buffer.from(JSON.stringify(envelope)).toString('base64') };
}
async function receiver() {
  const app = client();
  app.context.recipient = { privateKey: await webcrypto.subtle.importKey('pkcs8', bob.der,
    { name: 'ECDH', namedCurve: 'P-256' }, false, ['deriveBits']), publicId: 'bob', publicKey: bob.raw };
  app.context.sender = { public_id: 'alice', public_key: alice.raw };
  app.run('identity = recipient; chats = [{ id: 7, participants: [sender] }];');
  return app;
}

test('P-256 decrypts an independent envelope and rejects context/header/body tampering', async () => {
  const app = await receiver();
  app.context.message = message();
  assert.equal(await app.run('decryptMessage(message)'), text);
  for (const field of ['ct', 'salt', 'iv', 'epk']) {
    const envelope = fixture();
    const data = Buffer.from(envelope[field], 'base64'); data[0] ^= 1;
    envelope[field] = data.toString('base64');
    app.context.message = message(envelope);
    await assert.rejects(app.run('decryptMessage(message)'));
  }
  for (const change of [{ client_message_id: 'other' }, { chat_id: 8 },
    { sender_public_id: 'mallory' }]) {
    app.context.message = { ...message(), ...change };
    await assert.rejects(app.run('decryptMessage(message)'));
  }
  app.context.message = message({ ...fixture(), v: 2 });
  await assert.rejects(app.run('decryptMessage(message)'), /Unsupported/);
  app.context.message = message({ ...fixture(), alg: 'X25519-2DH-HKDF-SHA256-AES256GCM' });
  await assert.rejects(app.run('decryptMessage(message)'), /Unsupported/);
  app.context.message = message();
  app.run('identity.publicId = "other-recipient";');
  await assert.rejects(app.run('decryptMessage(message)'));
});

test('invalid curves, malformed points, and invalid envelope lengths fail closed', async () => {
  const app = await receiver();
  for (const raw of [Buffer.alloc(32), Buffer.concat([Buffer.from([4]), Buffer.alloc(64)]),
    Buffer.concat([Buffer.from([2]), Buffer.alloc(64)])]) {
    app.context.badKey = raw.toString('base64');
    await assert.rejects(app.run('SidewordProtocol.publicKeyFingerprint(badKey)'));
    app.context.sender = { public_id: 'alice', public_key: app.context.badKey };
    app.context.message = message();
    await assert.rejects(app.run('SidewordProtocol.decryptMessage(identity, message, sender)'));
  }
  app.context.sender = { public_id: 'alice', public_key: alice.raw };
  for (const [field, length] of [['salt', 0], ['salt', 15], ['iv', 16], ['ct', 15]]) {
    app.context.message = message({ ...fixture(), [field]: Buffer.alloc(length).toString('base64') });
    await assert.rejects(app.run('SidewordProtocol.decryptMessage(identity, message, sender)'), /Invalid ciphertext/);
  }
});

test('ephemeral P-256 private keys are generated non-exportable', async () => {
  const privateKeys = [];
  const subtle = new Proxy(webcrypto.subtle, { get(target, name) {
    if (name === 'generateKey') return async (...args) => {
      const result = await target.generateKey(...args);
      if (args[0].name === 'ECDH') privateKeys.push(result.privateKey);
      return result;
    };
    const value = target[name];
    return typeof value === 'function' ? value.bind(target) : value;
  } });
  const app = client({ subtle, getRandomValues: webcrypto.getRandomValues.bind(webcrypto) });
  await app.run('generateIdentity().then(value => { identity = value; identity.publicId = "alice"; })');
  app.context.peer = { public_id: 'bob', public_key: bob.raw };
  await app.run('encryptForRecipient("hello", 7, "fixture-message", peer)');
  assert.equal(privateKeys.length, 2);
  for (const key of privateKeys) {
    assert.equal(key.extractable, false);
    await assert.rejects(webcrypto.subtle.exportKey('pkcs8', key));
  }
});

test('P-256 envelopes match independent OpenSSL decryption; private keys stay non-exportable', async () => {
  const app = client();
  app.context.peer = { public_id: 'bob', public_key: bob.raw };
  await app.run('generateIdentity().then(value => { identity = value; identity.publicId = "alice"; })');
  const own = app.run('identity');
  assert.equal(own.privateKey.extractable, false);
  assert.equal(own.privateKey.algorithm.name, 'ECDH');
  assert.equal(own.privateKey.algorithm.namedCurve, 'P-256');
  assert.equal(Buffer.from(own.publicKey, 'base64').length, 65);
  assert.equal(own.storageKey.extractable, false);
  await assert.rejects(webcrypto.subtle.exportKey('pkcs8', own.privateKey));
  const wire = await app.run('encryptForRecipient("hello", 7, "fixture-message", peer)');
  const env = JSON.parse(Buffer.from(wire, 'base64'));
  assert.equal(env.v, 1);
  assert.equal(env.alg, 'P256-2DH-HKDF-SHA256-AES256GCM');
  assert.equal(Buffer.from(env.epk, 'base64').length, 65);
  const secrets = Buffer.concat([own.publicKey, env.epk].map(raw => diffieHellman({
    privateKey: bob.privateKey, publicKey: importPoint(raw),
  })));
  const aes = hkdfSync('sha256', secrets, Buffer.from(env.salt, 'base64'), info, 32);
  const decipher = createDecipheriv('aes-256-gcm', aes, Buffer.from(env.iv, 'base64'));
  decipher.setAAD(info);
  const ct = Buffer.from(env.ct, 'base64'); decipher.setAuthTag(ct.subarray(-16));
  assert.equal(Buffer.concat([decipher.update(ct.subarray(0, -16)), decipher.final()]).toString(), 'hello');
});

test('group fanout survives identity reload, recipient isolation, and out-of-order delivery', async () => {
  const app = client();
  const devices = [];
  for (let i = 0; i < 3; i++) {
    const device = await app.run('generateIdentity()');
    device.publicId = `device-${i}`;
    devices.push(structuredClone(device));
  }
  app.context.devices = devices;
  app.run('identity = devices[0]');
  const messages = [];
  for (let i = 1; i <= 2; i++) {
    app.context.peer = { public_id: devices[i].publicId, public_key: devices[i].publicKey };
    messages.push(await app.run('encryptForRecipient("group message", 7, "group-id", peer)'));
  }
  for (let i = 2; i >= 1; i--) {
    app.context.recipient = devices[i];
    app.context.message = { chat_id: 7, client_message_id: 'group-id',
      sender_public_id: devices[0].publicId, ciphertext: messages[i - 1] };
    app.run(`identity = recipient;
      chats = [{ id: 7, participants: [{ public_id: devices[0].publicId, public_key: devices[0].publicKey }] }];`);
    assert.equal(await app.run('decryptMessage(message)'), 'group message');
    app.context.message.ciphertext = messages[2 - i];
    await assert.rejects(app.run('decryptMessage(message)'));
  }
});

test('existing encrypted local history and non-exportable storage keys survive reload unchanged', async () => {
  const app = client();
  const storageBytes = Buffer.alloc(32, 9); // Synthetic local-history fixture.
  app.context.storedIdentity = structuredClone({ publicId: 'bob', storageKey:
    await webcrypto.subtle.importKey('raw', storageBytes, 'AES-GCM', false, ['encrypt', 'decrypt']) });
  const historyKey = 'history:bob:7:incoming-fixture';
  const entry = { id: 'incoming-fixture', kind: 'theirs', text: 'saved before upgrade', createdAt: 1000 };
  const cipher = createCipheriv('aes-256-gcm', storageBytes, iv);
  cipher.setAAD(Buffer.from(historyKey));
  const ciphertext = Buffer.concat([cipher.update(JSON.stringify(entry)), cipher.final(), cipher.getAuthTag()]);
  app.context.records = [{ key: historyKey, value: { iv, ciphertext } }];
  app.run('identity = storedIdentity; readHistoryRecords = async () => records;');
  await app.run('loadStoredHistory()');
  assert.equal(app.run('messagesByChat.get(7)[0].text'), entry.text);
  assert.equal(app.run('identity.storageKey.extractable'), false);
  app.run('messagesByChat.clear(); records[0].key = "history:bob:8:incoming-fixture";');
  await app.run('loadStoredHistory()');
  assert.equal(app.run('messagesByChat.size'), 0);
});

test('an aborted history transaction rejects persistence instead of leaving delivery stuck', async () => {
  const app = client();
  await app.run('generateIdentity().then(value => { identity = value; identity.publicId = "bob"; })');
  app.context.database = { close() {}, transaction() {
    const tx = { error: new Error('storage unavailable') };
    tx.objectStore = () => ({ put() { setImmediate(() => tx.onabort?.()); } });
    return tx;
  } };
  app.run('openDatabase = async () => database;');
  let timer;
  try {
    await assert.rejects(Promise.race([
      app.run('persistHistoryEntry(7, { id: "entry", text: "hello" })'),
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('persistence hung')), 200); }),
    ]), /could not save local chat history/);
  } finally { clearTimeout(timer); }
});
