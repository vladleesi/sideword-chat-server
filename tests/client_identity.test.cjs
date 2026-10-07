const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto, createHash, createECDH } = require('node:crypto');

function publicPoint(byte) {
  const key = createECDH('prime256v1');
  key.setPrivateKey(Buffer.alloc(32, byte));
  return key.getPublicKey().toString('base64');
}
const aliceKey = publicPoint(1), changedKey = publicPoint(2);

// Serialize readwrite transactions like IndexedDB, and commit only on completion.
function storage() {
  const records = new Map();
  let tail = Promise.resolve(), fail = false, unreadable = false;
  const database = { close() {}, transaction() {
    const tx = {};
    let request, key, write, aborted = false;
    tx.abort = () => { aborted = true; };
    tx.objectStore = () => ({
      get(value) { key = value; request = {}; return request; },
      put(value, target) { write = [target, structuredClone(value)]; },
    });
    tail = tail.then(() => new Promise(resolve => setImmediate(() => {
      request.result = unreadable && key === 'identity' && records.has(key)
        ? null : structuredClone(records.get(key));
      request.onsuccess();
      if (fail || aborted) {
        tx.error = new Error(fail ? 'storage unavailable' : 'transaction aborted');
        tx.onabort();
      }
      else { if (write) records.set(...write); tx.oncomplete(); }
      resolve();
    })));
    return tx;
  } };
  return { records, database, fail(value) { fail = value; }, unreadable(value) { unreadable = value; } };
}
function client(backend) {
  const element = { addEventListener() {} };
  const context = vm.createContext({
    TextEncoder, TextDecoder, DOMException, crypto: webcrypto, atob, btoa,
    document: { querySelector: () => element, addEventListener() {} },
    window: { addEventListener() {} }, database: backend.database, aliceKey, changedKey,
  });
  vm.runInContext(fs.readFileSync('app/static/client-protocol.js', 'utf8'), context);
  vm.runInContext(fs.readFileSync('app/static/client.js', 'utf8')
    .replace(/start\(\)\.catch[^\n]+/, ''), context);
  const run = code => vm.runInContext(code, context);
  run(`realOpenDatabase = openDatabase; openDatabase = async () => database;
    identity = { publicKey: 'local-device', publicId: 'me' };
    peer = { public_id: 'alice', public_key: aliceKey, key_fingerprint: 'server-lie' };
  `);
  return run;
}

test('routine identity saves cannot overwrite a concurrent credential rotation', async () => {
  const backend = storage();
  backend.records.set('identity', { publicId: 'me', token: 'fresh-access',
    refreshCredential: 'fresh-refresh', pendingRefreshCredential: 'durable-proposal',
    activeInviteToken: 'new-invite', activeInviteChatId: 8 });
  const run = client(backend);
  await run(`writeIdentity({ publicId: 'me', token: 'stale-access',
    refreshCredential: 'stale-refresh', displayName: 'Updated',
    activeInviteToken: 'old-invite', activeInviteChatId: 7 })`);
  assert.equal(backend.records.get('identity').token, 'fresh-access');
  assert.equal(backend.records.get('identity').pendingRefreshCredential, 'durable-proposal');
  assert.equal(backend.records.get('identity').displayName, 'Updated');
  assert.equal(backend.records.get('identity').activeInviteToken, 'new-invite');
  assert.equal(backend.records.get('identity').activeInviteChatId, 8);
});

test('non-exportable device keys survive a committed storage read before admission', async () => {
  const backend = storage();
  const run = client(backend);
  await run(`(async () => {
    identity = await generateIdentity();
    await writeIdentity(identity);
    await verifyIdentityPersistence();
    identity = await readIdentity();
  })()`);
  assert.equal(run('identity.privateKey.extractable'), false);
  assert.equal(run('identity.storageKey.extractable'), false);
  const saved = backend.records.get('identity');
  assert.notEqual(saved, run('identity'));
  await assert.rejects(webcrypto.subtle.exportKey('pkcs8', saved.privateKey));
  await assert.rejects(webcrypto.subtle.exportKey('raw', saved.storageKey));
  assert.equal(saved.privateKey.algorithm.namedCurve, 'P-256');
  const peer = await webcrypto.subtle.generateKey({ name: 'ECDH', namedCurve: 'P-256' }, false, ['deriveBits']);
  const ownPublic = await webcrypto.subtle.importKey('raw', Buffer.from(saved.publicKey, 'base64'),
    { name: 'ECDH', namedCurve: 'P-256' }, false, []);
  const expected = await webcrypto.subtle.deriveBits({ name: 'ECDH', public: ownPublic }, peer.privateKey, 256);
  const restored = await webcrypto.subtle.deriveBits({ name: 'ECDH', public: peer.publicKey }, saved.privateKey, 256);
  assert.deepEqual(Buffer.from(restored), Buffer.from(expected));
});

test('P-256 setup uses a separate database without reading or overwriting prior device records', async () => {
  const backend = storage();
  const legacyRecords = new Map([['identity', null], ['history:old', 'preserved']]);
  const opened = [];
  const run = client(backend);
  const requestDatabase = name => {
    opened.push(name);
    assert.equal(name, 'sideword-test-client-p256');
    const request = {};
    setImmediate(() => { request.result = backend.database; request.onsuccess(); });
    return request;
  };
  run('openDatabase = realOpenDatabase');
  // Only the fresh protocol namespace is provided to the actual IndexedDB opener.
  run('indexedDB = {}');
  const indexedDB = run('indexedDB');
  indexedDB.open = requestDatabase;
  assert.equal(await run('readIdentity()'), null);
  await run('generateIdentity().then(value => writeIdentity(value))');
  assert.equal((await run('readIdentity()')).privateKey.algorithm.namedCurve, 'P-256');
  assert.equal(opened.length, 3);
  assert.deepEqual([...legacyRecords], [['identity', null], ['history:old', 'preserved']]);
});

test('a missing device and WebKit null deserialization have distinct read outcomes', async () => {
  const backend = storage();
  const run = client(backend);
  assert.equal(await run('readIdentity()'), null);
  await run('generateIdentity().then(value => writeIdentity(value))');
  const saved = backend.records.get('identity');
  backend.unreadable(true);
  await assert.rejects(run('readIdentity()'), /could not read your saved chat keys/);
  await assert.rejects(run('writeIdentity({ publicKey: "replacement", publicId: null })'),
    /could not read your saved chat keys/);
  assert.equal(backend.records.get('identity'), saved);
  backend.unreadable(false);
  assert.equal((await run('readIdentity()')).publicKey, saved.publicKey);
});

test('a partially unreadable saved key is preserved instead of regenerated', async () => {
  const backend = storage();
  const run = client(backend);
  for (const field of ['privateKey', 'storageKey']) {
    await run('generateIdentity().then(value => writeIdentity(value))');
    backend.records.get('identity')[field] = null;
    const saved = backend.records.get('identity');
    await assert.rejects(run('readIdentity()'), /could not read your saved chat keys/);
    await assert.rejects(run('writeIdentity({ publicKey: "replacement" })'), /could not read your saved chat keys/);
    assert.equal(backend.records.get('identity'), saved);
    backend.records.clear();
  }
});

test('failed read transactions cannot release a saved session before commit', async () => {
  const backend = storage();
  const run = client(backend);
  await run('generateIdentity().then(value => writeIdentity(value))');
  backend.fail(true);
  await assert.rejects(run('readIdentity()'), /could not finish reading your saved device data/);
});

test('missing storage does not masquerade as a device change during renewal', async () => {
  const run = client(storage());
  run(`identity.token = 'access';
    deviceLock = async (_, action) => action();
    fetch = async () => { throw new Error('must not renew'); };`);
  await assert.rejects(run('ensureFreshSession()'), /Local device data is missing/);
  assert.equal(run('identity.token'), 'access');
});

test('changed participant or public key still blocks renewal without overwriting another device', async () => {
  for (const change of [{ publicId: 'another-device' }, { publicKey: 'another-key' }]) {
    const backend = storage();
    const run = client(backend);
    await run(`identity.token = 'access'; deviceLock = async (_, action) => action();
      writeIdentity(identity, true)`);
    Object.assign(backend.records.get('identity'), change);
    await assert.rejects(run('ensureFreshSession()'), /no longer matches the device this tab loaded/);
    assert.equal(backend.records.get('identity').token, 'access');
  }
});

test('fingerprints are local; first-use pins survive reload and never silently change', async () => {
  const backend = storage();
  const run = client(backend);
  run("peer.display_name = 'Alice';");
  const observed = await run('observePeerKey(peer)');
  assert.equal(observed.display_name, 'Alice');
  assert.equal(observed.local_fingerprint, createHash('sha256').update(Buffer.from(aliceKey, 'base64')).digest('hex'));
  assert.equal(observed.key_changed, false);
  const reload = client(backend);
  assert.equal((await reload('observePeerKey(peer)')).key_changed, false);
  reload("peer.public_key = changedKey;");
  assert.equal((await reload('observePeerKey(peer)')).key_changed, true);
  await assert.rejects(reload('assertTrustedPeer(peer)'), /key changed/i);
  await assert.rejects(reload('encryptForRecipient("secret", 1, "message", peer)'), /key changed/i);
  assert.equal([...backend.records.values()][0], observed.local_fingerprint);
  // Reset/rejoin uses a different public ID; normal new group members remain possible.
  reload("peer.public_id = 'new-identity';");
  await reload('assertTrustedPeer(peer)');
});

test('failed pin persistence blocks use, remains retryable, and concurrent tabs cannot replace a pin', async () => {
  const backend = storage();
  const a = client(backend), b = client(backend);
  backend.fail(true);
  await assert.rejects(a('assertTrustedPeer(peer)'), /could not save a participant/);
  assert.equal(backend.records.size, 0);
  backend.fail(false);
  b("peer.public_key = changedKey;");
  const results = await Promise.allSettled([a('assertTrustedPeer(peer)'), b('assertTrustedPeer(peer)')]);
  assert.deepEqual(results.map(r => r.status).sort(), ['fulfilled', 'rejected']);
  assert.equal(backend.records.size, 1);
});

test('changed sender keys cannot cause decryption, history persistence, or deletion acknowledgements', async () => {
  const run = client(storage());
  await run('assertTrustedPeer(peer)');
  run(`peer.public_key = changedKey;
    chats = [{ id: 7, participants: [peer] }];
    saved = 0; persistHistoryEntry = async () => { saved++; };
    incoming = { id: 1, chat_id: 7, sender_public_id: 'alice', client_message_id: 'x', ciphertext: 'unused' };
  `);
  await run('processMessages([incoming])');
  assert.equal(run('pendingReadsByChat.size'), 0);
  assert.equal(run('seenMessageIds.size'), 0);
  assert.equal(run('saved'), 0);
  assert.match(run('messagesByChat.get(7)[0].text'), /key changed/i);
});

test('missing history storage key cannot acknowledge plaintext that was never persisted', async () => {
  const run = client(storage());
  run(`decryptMessage = async () => 'authenticated plaintext';
    senderKeyIsLoaded = () => true;
    incoming = { id: 1, chat_id: 7, sender_public_id: 'alice', client_message_id: 'x' };
  `);
  await run('processMessages([incoming])');
  assert.equal(run('pendingReadsByChat.size'), 0);
  assert.equal(run('seenMessageIds.size'), 0);
  assert.equal(run('messagesByChat.get(7).filter(e => e.kind === "theirs").length'), 0);
});

test('own fingerprint comes from the local identity even if the server changes the roster entry', async () => {
  const run = client(storage());
  run("identity.publicKey = changedKey; peer.public_id = identity.publicId;");
  const observed = await run('observePeerKey(peer)');
  assert.equal(observed.local_fingerprint,
    createHash('sha256').update(Buffer.from(changedKey, 'base64')).digest('hex'));
  assert.equal(observed.key_changed, true);
});
