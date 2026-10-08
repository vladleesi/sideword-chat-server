const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const crypto = require('node:crypto').webcrypto;

function client(shared = {}) {
  const element = { addEventListener() {} };
  const context = vm.createContext({ TextEncoder, TextDecoder, DOMException, crypto, btoa,
    document: { querySelector: () => element, addEventListener() {} },
    window: { addEventListener() {} }, ...shared });
  vm.runInContext(fs.readFileSync('app/static/client-protocol.js', 'utf8'), context);
  vm.runInContext(fs.readFileSync('app/static/client.js', 'utf8')
    .replace(/start\(\)\.catch[^\n]+/, ''), context);
  vm.runInContext('renderMessages = () => {};', context);
  return code => vm.runInContext(code, context);
}

function inviteClient() {
  const handlers = new Map();
  const nodes = new Map();
  const run = client({ document: {
    querySelector(selector) {
      if (!nodes.has(selector)) nodes.set(selector, {
        value: '', hidden: false, disabled: false,
        addEventListener(event, handler) { handlers.set(`${selector}:${event}`, handler); },
        classList: { toggle() {} }, setAttribute(name, value) { this[name] = value; }, focus() {},
        querySelector() { return this; },
      });
      return nodes.get(selector);
    },
    addEventListener() {},
  } });
  run(`
    globalThis.events = [];
    elements.loadingPanel.hidden = false;
    elements.setupPanel.hidden = true;
    elements.clientPanel.hidden = true;
    globalThis.saved = { publicId: 'alice', publicKey: 'device-public-key',
      privateKey: { device: true }, storageKey: { history: true }, token: 'old-access',
      activeInviteToken: 'A'.repeat(24), activeInviteChatId: 7 };
    globalThis.deviceKey = saved.privateKey;
    globalThis.savedStorageKey = saved.storageKey;
    window.isSecureContext = true;
    window.crypto = crypto;
    window.indexedDB = {};
    window.location = { protocol: 'https:', host: 'example.test' };
    window.setInterval = () => 1;
    globalThis.history = { replaceState() { events.push('url-cleared'); } };
    globalThis.WebSocket = class {
      static OPEN = 1;
      constructor() { this.readyState = 0; events.push('socket'); }
      addEventListener() {}
    };
    readIdentity = async () => saved;
    writeIdentity = async (value) => { saved = { ...value }; events.push('saved'); };
    loadStoredHistory = async () => { events.push('history-loaded'); };
    clearIdentity = async () => { throw new Error('must not erase device'); };
    readHistoryRecords = async () => { events.push('outbox-read'); return []; };
    deviceLock = async (_, action) => action();
    renderOutbox = async () => {};
    renderMessages = () => {};
    observePeerKey = async peer => peer;
    selectChat = id => { selectedChatId = id; events.push('selected:' + id); };
    showToast = () => {};
    globalThis.failActivation = false;
    globalThis.nextPublicId = 'alice';
    globalThis.activationRequests = [];
    api = async (path, options) => {
      events.push(path);
      if (path.includes('/links/')) {
        activationRequests.push({ path, ...options });
        if (failActivation) throw new Error('Invite expired or password incorrect');
        return { token: 'new-access', user: { public_id: nextPublicId }, chat: { id: 8 },
          session_id: 'new-session', access_expires_at: '2030-01-01T00:00:00Z',
          session_expires_at: '2030-01-02T00:00:00Z' };
      }
      if (path === '/api/v1/me') return { user: { public_id: nextPublicId,
        public_key: 'device-public-key' }, chats: [...(nextPublicId === 'alice'
          ? [{ id: 7, participants: [] }] : []),
        { id: 8, participants: [] }], send_retry_window_seconds: 3600 };
      if (path === '/api/v1/poll') return { messages: [], read_receipts: [] };
      throw new Error('Unexpected request ' + path);
    };
    elements.inviteToken.value = 'B'.repeat(24);
    elements.displayName.value = 'Test participant';
  `);
  return { run, submit: () => handlers.get('#activation-form:submit')({ preventDefault() {} }) };
}

test('activation 401 clears rejected bearer and preserves device and resume state for retry', async () => {
  const requests = [];
  const run = client({ Headers, fetch: async (_, options) => {
    requests.push(options);
    return requests.length === 1
      ? { ok: false, status: 401, json: async () => ({ detail: 'invalid or revoked session' }) }
      : { ok: true, json: async () => ({ token: 'recovered-access' }) };
  } });
  run(`
    globalThis.deviceKey = { device: true };
    globalThis.admission = { resume: 'saved-resume', session: 'saved-session' };
    identity = { publicId: 'alice', token: 'revoked-access', privateKey: deviceKey,
      activationCredentials: admission };
    globalThis.saved = null;
    writeIdentity = async value => { saved = { ...value }; };
    updateSessionCountdown = () => {};
    updateIdentityUi = () => {};
    elements.inviteToken.focus = () => {};
    window.clearTimeout = () => {};
    window.clearInterval = () => {};
  `);
  const action = 'api("/api/v1/links/activate", { method: "POST", body: "{}" })';
  await assert.rejects(run(action), /server rejected the saved login \(HTTP 401\)/);
  assert.equal(requests[0].headers.get('Authorization'), 'Bearer revoked-access');
  assert.equal(run('saved.token'), null);
  assert.equal(run('saved.privateKey === deviceKey && saved.activationCredentials === admission'), true);
  assert.equal((await run(action)).token, 'recovered-access');
  assert.equal(requests[1].headers.has('Authorization'), false);
});

test('activation errors explain HTTP failures without rendering server inputs or exception data', async () => {
  const privateMarker = 'synthetic-private-value';
  for (const [status, detail, expected] of [
    [403, 'The room phrase or password is incorrect.', /room password does not match/],
    [410, 'link expired', /invite has expired/],
    [410, 'room sealed; reconnect with your saved session', /full or closed to new devices/],
    [409, 'This room uses retired encryption. Create a new invite.', /old test encryption/],
    [429, 'Too many failed attempts. Try again later.', /Password checks.*temporarily blocked/],
    [422, [{ loc: ['body', 'public_key'], input: privateMarker, msg: privateMarker }], /rejected this device's public encryption key/],
    [422, [{ loc: ['body', 'token'], input: privateMarker, msg: privateMarker }], /request fields as invalid/],
    [500, privateMarker, /server error/],
    [400, { input: privateMarker }, /data does not match the chat state/],
  ]) {
    const run = client({ Headers, fetch: async () => ({ ok: false, status,
      statusText: privateMarker, json: async () => ({ detail }) }) });
    run('identity = null');
    await assert.rejects(run('api("/api/v1/links/activate", { method: "POST", body: "{}" })'), error => {
      const text = run('errorMessage')(error);
      assert.match(text, /Could not join the room/);
      assert.match(text, new RegExp(`HTTP ${status}`));
      assert.match(text, expected);
      assert.equal(text.includes(privateMarker), false);
      return true;
    });
  }
});

test('network, unreadable responses, and unknown exceptions have safe understandable explanations', async () => {
  for (const [fetch, expected] of [
    [async () => { throw new Error('synthetic-private-value'); }, /network connection failed/],
    [async () => ({ ok: true, json: async () => { throw new Error('synthetic-private-value'); } }), /response this client could not read/],
    [async () => ({ ok: false, status: 502, json: async () => { throw new Error('synthetic-private-value'); } }), /HTTP 502.*server error/],
  ]) {
    const run = client({ Headers, fetch });
    run('identity = null');
    await assert.rejects(run('api("/api/v1/links/activate")'), error => {
      const text = run('errorMessage')(error);
      assert.match(text, expected);
      assert.equal(text.includes('synthetic-private-value'), false);
      return true;
    });
  }
  const run = client();
  assert.equal(run('errorMessage(new Error("synthetic-private-value"))'),
    'An unexpected client error prevented this operation.');
  assert.equal(run('errorMessage("synthetic-private-value")'),
    'An unexpected client error prevented this operation.');
  assert.match(run('errorMessage(new DOMException("synthetic-private-value", "NotSupportedError"))'), /does not support the P-256/);
  assert.match(run('errorMessage(storageFailure(new DOMException("synthetic-private-value", "QuotaExceededError"), "fallback"))'), /no space available/);
});

for (const scenario of ['different invite', 'legacy identity', 'fresh device']) {
  test(`${scenario} opens requested invite without resuming the previous chat`, async () => {
    const { run } = inviteClient();
    if (scenario === 'legacy identity') run('delete saved.activeInviteToken;');
    if (scenario === 'fresh device') run('saved = null;');
    await run('start();');
    assert.equal(run('elements.setupPanel.hidden'), false);
    assert.equal(run('elements.clientPanel.hidden'), true);
    assert.equal(run('document.querySelector("#reconnect-session").hidden'), true);
    assert.equal(run('selectedChatId'), null);
    await run('synchronize(); connectSocket(); scheduleReconnect(); flushOutbox();');
    assert.deepEqual(Array.from(run('events')), []);
    if (scenario !== 'fresh device') {
      assert.equal(run('saved.token'), 'old-access');
      assert.equal(run('saved.privateKey === deviceKey && saved.storageKey === savedStorageKey'), true);
    }
  });
}

for (const invite of ['', 'A'.repeat(24)]) {
  test(`${invite ? 'same invite' : 'plain client URL'} resumes its saved room`, async () => {
    const { run } = inviteClient();
    run(`elements.inviteToken.value = ${JSON.stringify(invite)};`);
    await run('start();');
    assert.equal(run('elements.setupPanel.hidden'), true);
    assert.equal(run('elements.clientPanel.hidden'), false);
    assert.equal(run('selectedChatId'), 7);
    assert.equal(run('events.includes("socket")'), true);
    assert.equal(run('events.some(value => value.includes("/links/"))'), false);
  });
}

test('saved-device restoration keeps activation hidden during slow key and history reads', async () => {
  const { run } = inviteClient();
  run(`elements.inviteToken.value = '';
    readIdentity = () => new Promise(resolve => { globalThis.finishIdentity = resolve; });
    globalThis.historyEntered = new Promise(resolve => { globalThis.enterHistory = resolve; });
    loadStoredHistory = () => {
      if (globalThis.finishHistory) return Promise.resolve();
      enterHistory();
      return new Promise(resolve => { globalThis.finishHistory = resolve; });
    };
    globalThis.starting = start();`);
  assert.equal(run('elements.setupPanel.hidden'), true);
  assert.equal(run('elements.clientPanel.hidden'), true);
  assert.equal(run('elements.loadingPanel.hidden'), false);
  run('finishIdentity(saved);');
  await run('historyEntered');
  assert.equal(run('elements.setupPanel.hidden'), true);
  assert.equal(run('elements.loadingPanel.hidden'), false);
  run('finishHistory();');
  await run('starting');
  assert.equal(run('elements.setupPanel.hidden'), true);
  assert.equal(run('elements.clientPanel.hidden'), false);
  assert.equal(run('elements.loadingPanel.hidden'), true);
  assert.equal(run('selectedChatId'), 7);
});

test('fresh devices show activation only after the saved-device check completes', async () => {
  const { run } = inviteClient();
  run(`readIdentity = () => new Promise(resolve => { globalThis.finishIdentity = resolve; });
    globalThis.starting = start();`);
  assert.equal(run('elements.setupPanel.hidden'), true);
  assert.equal(run('elements.loadingPanel.hidden'), false);
  run('finishIdentity(null);');
  await run('starting');
  assert.equal(run('elements.setupPanel.hidden'), false);
  assert.equal(run('elements.clientPanel.hidden'), true);
  assert.equal(run('elements.loadingPanel.hidden'), true);
});

for (const changedParticipant of [false, true]) {
  test(`new invite selects its room and preserves storage${changedParticipant ? ' for a different participant' : ''}`, async () => {
    const { run, submit } = inviteClient();
    await run('start();');
    if (changedParticipant) run("nextPublicId = 'returning-participant';");
    await submit();
    assert.equal(run('elements.activationError.hidden'), true);
    assert.equal(run('elements.setupPanel.hidden'), true);
    assert.equal(run('elements.clientPanel.hidden'), false);
    assert.equal(run('selectedChatId'), 8);
    assert.equal(run('elements.clientPanel["data-mobile-view"]'), 'conversation');
    assert.equal(run('events.includes("selected:7")'), false);
    assert.equal(run('saved.activeInviteToken'), 'B'.repeat(24));
    assert.equal(run('saved.activeInviteChatId'), 8);
    assert.equal(run('saved.privateKey === deviceKey && saved.storageKey === savedStorageKey'), true);
    assert.equal(run('events.includes("history-loaded")'), true);
    assert.equal(run('events.includes("url-cleared")'), true);
    assert.equal(run('events.includes("socket")'), true);
  });
}

test('new invite after revoked login cannot expose old membership and preserves local history', async () => {
  const { run, submit } = inviteClient();
  await run('start();');
  run(`
    globalThis.persistedHistory = new Map([['history:alice:7', 'encrypted-old-message']]);
    globalThis.admit = api;
    api = async (path, options) => {
      if (path === '/api/v1/links/activate' && identity.token === 'old-access') {
        identity.suspendedToken = identity.token;
        identity.token = null;
        await writeIdentity(identity, true);
        throw new SessionExpiredError('invalid or revoked session');
      }
      return admit(path, options);
    };
  `);
  await submit();
  assert.equal(run('elements.activationError.hidden'), false);
  assert.equal(run('saved.token'), null);
  assert.equal(run('events.includes("/api/v1/me")'), false);
  run("nextPublicId = 'fresh-participant';");
  await submit();
  assert.equal(run('elements.activationError.hidden'), true);
  assert.equal(run('saved.publicId'), 'fresh-participant');
  assert.deepEqual(JSON.parse(run('JSON.stringify(chats.map(chat => chat.id))')), [8]);
  assert.equal(run('saved.privateKey === deviceKey && saved.storageKey === savedStorageKey'), true);
  assert.equal(run('persistedHistory.get("history:alice:7")'), 'encrypted-old-message');
  assert.equal(run('saved.inviteCredentials["B".repeat(24)] != null'), true);
});

test('failed invite stays on the form and a retry reuses the saved admission credentials', async () => {
  const { run, submit } = inviteClient();
  await run('start();');
  run('failActivation = true;');
  await submit();
  assert.equal(run('elements.activationError.hidden'), false);
  assert.equal(run('elements.setupPanel.hidden'), false);
  assert.equal(run('elements.clientPanel.hidden'), true);
  assert.equal(run('saved.activeInviteToken'), 'A'.repeat(24));
  const credential = run('saved.activationCredentials["B".repeat(24)]');
  assert.equal(run('events.includes("url-cleared") || events.includes("socket")'), false);
  run('failActivation = false;');
  await submit();
  assert.equal(run('saved.refreshCredential'), credential);
  assert.equal(run('selectedChatId'), 8);
  const requests = JSON.parse(run('JSON.stringify(activationRequests)'));
  assert.equal(requests.length, 2);
  for (const request of requests) {
    assert.equal(request.path, '/api/v1/links/activate');
    assert.equal(request.method, 'POST');
    assert.equal(request.path.includes('B'.repeat(24)), false);
    assert.equal(JSON.parse(request.body).token, 'B'.repeat(24));
    assert.equal(JSON.parse(request.body).session_credential, credential);
  }
  assert.equal(JSON.parse(requests[0].body).resume_credential,
    JSON.parse(requests[1].body).resume_credential);
});

test('unrestorable device storage blocks fresh invite admission and leaves retry credentials intact', async () => {
  const { run, submit } = inviteClient();
  run('saved = null;');
  await run('start();');
  run('readIdentity = async () => null;');
  await submit();
  assert.equal(run('activationRequests.length'), 0);
  assert.equal(run('elements.setupPanel.hidden'), false);
  assert.equal(run('elements.clientPanel.hidden'), true);
  assert.match(run('elements.activationError.textContent'), /could not read your saved chat keys/);
  assert.equal(run('saved.publicId'), null);
  assert.equal(run('saved.token'), null);
  assert.ok(run('saved.activationCredentials["B".repeat(24)]'));
  assert.equal(run('events.includes("socket") || events.includes("url-cleared")'), false);
  // The same device and admission proposals can continue if storage becomes usable.
  run('readIdentity = async () => saved;');
  await submit();
  assert.equal(run('activationRequests.length'), 1);
  assert.equal(run('elements.activationError.hidden'), true);
});

test('unreadable saved storage leaves a persistent startup error without reconnecting', async () => {
  const { run } = inviteClient();
  run('readIdentity = async () => { throw new DeviceStorageError(); };');
  await run('start().catch(showStartupError)');
  assert.equal(run('elements.activationError.hidden'), false);
  assert.match(run('elements.activationError.textContent'), /Keep existing device data/);
  assert.equal(run('elements.setupPanel.hidden'), false);
  assert.equal(run('elements.clientPanel.hidden'), true);
  assert.equal(run('elements.loadingPanel.hidden'), true);
  assert.deepEqual(Array.from(run('events')), []);
});

for (const failed of [false, true]) {
  test(`send ${failed ? 'failure' : 'success'} updates pending retries after upload and focuses without scrolling`, async () => {
    const handlers = new Map();
    const nodes = new Map();
    const run = client({ document: {
      querySelector(selector) {
        if (!nodes.has(selector)) nodes.set(selector, {
          addEventListener(event, handler) { handlers.set(`${selector}:${event}`, handler); },
          focus(options) { this.focusOptions = options; },
        });
        return nodes.get(selector);
      },
      addEventListener() {},
    } });
    run(`
      identity = { publicId: 'alice', retryWindowSeconds: 3600 };
      selectedChatId = 7;
      chats = [{ id: 7, participants: [{ public_id: 'bob' }] }];
      elements.messageInput.value = 'hello';
      globalThis.events = [];
      encryptForRecipient = async () => 'ciphertext';
      deviceLock = async (_, action) => action();
      readHistoryRecords = async () => [];
      persistOutbox = async () => events.push('saved');
      storeOutgoing = async () => {};
      flushOutbox = async () => { events.push('upload'); ${failed ? "throw new Error('offline');" : ''} };
      renderOutbox = async () => events.push('render');
      showToast = () => events.push('error');
    `);
    await handlers.get('#message-form:submit')({ preventDefault() {} });
    assert.deepEqual(JSON.parse(run('JSON.stringify(events)')),
      failed ? ['saved', 'upload', 'render', 'error'] : ['saved', 'upload', 'render']);
    assert.equal(nodes.get('#message-input').focusOptions.preventScroll, true);
    assert.equal(nodes.get('#send-button').disabled, false);
  });
}

test('closed-room retries remain encrypted and paused while other-room retries and reopening work', async () => {
  const run = client();
  await run(`(async () => {
    identity = {token:'test-token', publicId:'alice', storageKey:
      await crypto.subtle.generateKey({name:'AES-GCM', length:256}, false, ['encrypt','decrypt'])};
    chats = [{id:7, closed_at:'2026-10-08T00:00:00Z', participants:[]},
      {id:8, closed_at:null, participants:[]}];
    deviceLock = async (_, action) => action();
    globalThis.records = new Map(); globalThis.requests = [];
    putDatabaseValue = async (key,value) => records.set(key,value);
    readHistoryRecords = async () => [...records].filter(([key]) => key.startsWith('outbox:'))
      .map(([key,value]) => ({key,value}));
    removeOutbox = async key => records.delete(key);
    api = async (path, options) => { requests.push({path,body:JSON.parse(options.body)});
      return {created_at:'2026-10-08T00:00:00Z'}; };
    for (const chatId of [7,8]) await persistOutbox({chatId, messageId:'stable-'+chatId,
      envelopes:[{recipient_public_id:'peer', ciphertext:'stable-ciphertext'}],
      plaintext:'private pending text', createdAt:Date.now(), expiresAt:Date.now()+60000});
    await flushOutbox();
  })()`);
  const requests = () => JSON.parse(run('JSON.stringify(requests)'));
  assert.deepEqual(requests().map(request => request.path), ['/api/v1/chats/8/messages']);
  assert.equal(run('[...records.keys()].filter(key => key.startsWith("outbox:")).length'), 1);
  assert.equal(run('JSON.stringify([...records]).includes("private pending text")'), false);
  await run('chats[0].closed_at = null; flushOutbox();');
  assert.deepEqual(requests().map(request => request.path),
    ['/api/v1/chats/8/messages', '/api/v1/chats/7/messages']);
  assert.equal(requests()[1].body.client_message_id, 'stable-7');
  assert.equal(requests()[1].body.envelopes[0].ciphertext, 'stable-ciphertext');
  assert.equal(run('[...records.keys()].filter(key => key.startsWith("outbox:")).length'), 0);
});

test('encrypted outbox survives upload response loss and history-save failure', async () => {
  const run = client();
  await run(`(async () => {
    identity = { token: 'test-token', publicId: 'alice', storageKey:
      await crypto.subtle.generateKey({ name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt']) };
    deviceLock = async (_, action) => action();
    globalThis.records = new Map();
    putDatabaseValue = async (key, value) => records.set(key, value);
    readHistoryRecords = async () => [...records].filter(([key]) => key.startsWith('outbox:')).map(([key, value]) => ({ key, value }));
    removeOutbox = async key => records.delete(key);
    globalThis.requests = [];
    api = async (_, options) => { requests.push(options.body); throw new Error('response lost'); };
    await persistOutbox({ chatId: 7, messageId: 'stable', envelopes: [{ciphertext:'already encrypted'}],
      plaintext: 'private plaintext', createdAt: Date.now(), expiresAt: Date.now() + 60000 });
  })()`);
  assert.equal(run('JSON.stringify([...records]).includes("private plaintext")'), false);
  await assert.rejects(run('flushOutbox()'), /response lost/);
  assert.equal(run('[...records.keys()].filter(key => key.startsWith("outbox:")).length'), 1);
  await run(`api = async (_, options) => { requests.push(options.body); return {created_at:'2026-09-27T00:00:00Z'}; };
    persistHistoryEntry = async () => { throw new Error('disk full'); };`);
  await assert.rejects(run('flushOutbox()'), /disk full/);
  assert.equal(run('[...records.keys()].filter(key => key.startsWith("outbox:")).length'), 1);
  await run('persistHistoryEntry = async () => {}; flushOutbox()');
  assert.equal(run('[...records.keys()].filter(key => key.startsWith("outbox:")).length'), 0);
  assert.equal(run('new Set(requests).size'), 1);
  assert.equal(run('messagesByChat.get(7).filter(e => e.kind === "mine").length'), 1);
});

test('expired outgoing records stay saved without unsafe retransmission', async () => {
  const run = client();
  await run(`(async () => {
    identity = {token:'token', publicId:'alice', storageKey:
      await crypto.subtle.generateKey({name:'AES-GCM',length:256}, false, ['encrypt','decrypt'])};
    deviceLock = async (_, action) => action();
    globalThis.record = null;
    putDatabaseValue = async (key, value) => { record = {key,value}; };
    readHistoryRecords = async () => [record];
    api = async () => { throw new Error('must not upload'); };
    await persistOutbox({chatId:7,messageId:'old',expiresAt:0,createdAt:1,envelopes:[],plaintext:'old'});
  })()`);
  await assert.rejects(run('flushOutbox()'), /retry expired/);
  assert.equal(run('record !== null'), true);
});

test('concurrent tabs reuse persisted refresh proposals after a lost response', async () => {
  let state = { token: 'old-access', publicId: 'alice', refreshCredential: 'old-refresh', tokenExpiresAt: 0 };
  let tail = Promise.resolve();
  const requests = [];
  const shared = {
    navigator: { locks: { request: (_, action) => {
      const result = tail.then(action); tail = result.catch(() => {}); return result;
    } } },
    load: async () => structuredClone(state),
    save: async value => { state = structuredClone(value); },
    fetch: async (_, options) => {
      requests.push(JSON.parse(options.body));
      if (requests.length === 1) throw new Error('lost response');
      return { ok: true, json: async () => ({ token: 'new-access', session_id: 'session',
        access_expires_at: new Date(Date.now() + 900000).toISOString(),
        session_expires_at: new Date(Date.now() + 86400000).toISOString() }) };
    },
  };
  const a = client(shared), b = client(shared);
  for (const run of [a,b]) await run(`(async () => { identity = await load();
    readIdentity = load; writeIdentity = save; connectSocket = () => {}; })()`);
  await assert.rejects(a('ensureFreshSession()'), /network connection failed/);
  assert.ok(state.pendingRefreshCredential);
  await Promise.all([a('ensureFreshSession()'), b('ensureFreshSession()')]);
  assert.equal(requests.length, 2);
  assert.deepEqual(requests[0], requests[1]);
  assert.equal(state.refreshCredential, requests[1].next_credential);
  assert.equal(state.pendingRefreshCredential, undefined);
});
