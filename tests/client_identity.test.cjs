const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto, createHash } = require('node:crypto');

// Serialize readwrite transactions like IndexedDB, and commit only on completion.
function storage() {
  const records = new Map();
  let tail = Promise.resolve(), fail = false;
  const database = { close() {}, transaction() {
    const tx = {};
    let request, key, write;
    tx.objectStore = () => ({
      get(value) { key = value; request = {}; return request; },
      put(value, target) { write = [target, value]; },
    });
    tail = tail.then(() => new Promise(resolve => setImmediate(() => {
      request.result = records.get(key);
      request.onsuccess();
      if (fail) { tx.error = new Error('storage unavailable'); tx.onabort(); }
      else { if (write) records.set(...write); tx.oncomplete(); }
      resolve();
    })));
    return tx;
  } };
  return { records, database, fail(value) { fail = value; } };
}
function client(backend) {
  const element = { addEventListener() {} };
  const context = vm.createContext({
    TextEncoder, TextDecoder, DOMException, crypto: webcrypto, atob, btoa,
    document: { querySelector: () => element, addEventListener() {} },
    window: { addEventListener() {} }, database: backend.database,
  });
  vm.runInContext(fs.readFileSync('app/static/client-protocol.js', 'utf8'), context);
  vm.runInContext(fs.readFileSync('app/static/client.js', 'utf8')
    .replace(/start\(\)\.catch[^\n]+/, ''), context);
  const run = code => vm.runInContext(code, context);
  run(`openDatabase = async () => database;
    identity = { publicKey: 'local-device', publicId: 'me' };
    peer = { public_id: 'alice', public_key: btoa('a'.repeat(32)), key_fingerprint: 'server-lie' };
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

test('fingerprints are local; first-use pins survive reload and never silently change', async () => {
  const backend = storage();
  const run = client(backend);
  run("peer.display_name = 'Alice';");
  const observed = await run('observePeerKey(peer)');
  assert.equal(observed.display_name, 'Alice');
  assert.equal(observed.local_fingerprint, createHash('sha256').update('a'.repeat(32)).digest('hex'));
  assert.equal(observed.key_changed, false);
  const reload = client(backend);
  assert.equal((await reload('observePeerKey(peer)')).key_changed, false);
  reload("peer.public_key = btoa('b'.repeat(32));");
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
  await assert.rejects(a('assertTrustedPeer(peer)'), /storage unavailable/);
  assert.equal(backend.records.size, 0);
  backend.fail(false);
  b("peer.public_key = btoa('b'.repeat(32));");
  const results = await Promise.allSettled([a('assertTrustedPeer(peer)'), b('assertTrustedPeer(peer)')]);
  assert.deepEqual(results.map(r => r.status).sort(), ['fulfilled', 'rejected']);
  assert.equal(backend.records.size, 1);
});

test('changed sender keys cannot cause decryption, history persistence, or deletion acknowledgements', async () => {
  const run = client(storage());
  await run('assertTrustedPeer(peer)');
  run(`peer.public_key = btoa('b'.repeat(32));
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
  run("identity.publicKey = btoa('d'.repeat(32)); peer.public_id = identity.publicId;");
  const observed = await run('observePeerKey(peer)');
  assert.equal(observed.local_fingerprint, createHash('sha256').update('d'.repeat(32)).digest('hex'));
  assert.equal(observed.key_changed, true);
});
