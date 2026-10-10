const { test } = require('node:test');
const assert = require('node:assert/strict');
const { client } = require('./client_dom.cjs');

function connection({ protocol = 'https:', host = 'one.example.test' } = {}) {
  const app = client();
  const timeouts = new Map(), intervals = new Map();
  let timerId = 0;
  app.window.location = { protocol, host };
  app.window.setTimeout = (handler, delay) => {
    const id = ++timerId;
    timeouts.set(id, { handler, delay });
    return id;
  };
  app.window.clearTimeout = id => timeouts.delete(id);
  app.window.setInterval = (handler, delay) => {
    const id = ++timerId;
    intervals.set(id, { handler, delay });
    return id;
  };
  app.window.clearInterval = id => intervals.delete(id);
  app.run(`
    globalThis.sockets = [];
    globalThis.requests = [];
    globalThis.incoming = [];
    globalThis.errors = [];
    globalThis.WebSocket = class {
      static OPEN = 1;
      constructor(url, protocols) {
        this.url = url; this.protocols = protocols; this.readyState = 0;
        this.events = new Map(); this.sent = []; this.closeCalls = 0;
        sockets.push(this);
      }
      addEventListener(name, handler) {
        if (!this.events.has(name)) this.events.set(name, []);
        this.events.get(name).push(handler);
      }
      emit(name, event) {
        if (name === 'open') this.readyState = 1;
        if (name === 'close') this.readyState = 3;
        for (const handler of this.events.get(name) || []) handler(event);
      }
      send(value) { this.sent.push(value); }
      close() {
        this.closeCalls++;
        this.readyState = 2;
        if (!this.hangClosing) this.emit('close');
      }
    };
    loadChats = async () => {};
    loadStoredHistory = async () => {};
    flushOutbox = async () => {};
    renderOutbox = async () => {};
    processIncoming = async data => { incoming.push(data); };
    recoverMessageStatuses = async () => {};
    api = async path => { requests.push(path); return {}; };
    showToast = text => { errors.push(text); };
    updateConnectionState(false);
  `);
  const latest = () => app.run('sockets[sockets.length - 1]');
  const fire = id => {
    const timer = timeouts.get(id);
    assert.ok(timer, 'expected an active timeout');
    timeouts.delete(id);
    timer.handler();
  };
  return { ...app, latest, fire, timeouts, intervals };
}

for (const [protocol, host, scheme] of [
  ['https:', 'one.example.test', 'wss:'],
  ['https:', 'two.example.test:8443', 'wss:'],
  ['http:', '[::1]:8000', 'ws:'],
]) {
  test(`socket uses the current origin ${host} without credentials in its URL`, () => {
    const app = connection({ protocol, host });
    app.run('connectSocket(); connectSocket();');
    assert.equal(app.run('sockets.length'), 1);
    const ws = app.latest();
    assert.equal(ws.url, `${scheme}//${host}/ws`);
    assert.deepEqual(Array.from(ws.protocols), ['sideword.v1']);
    ws.emit('open');
    assert.deepEqual(JSON.parse(ws.sent[0]), {
      type: 'auth', token: 'not-for-display', presence: true,
    });
    assert.equal(app.nodes.get('#connection-label').textContent, 'Polling');
  });
}

test('connect and auth deadlines release stalled sockets without waiting for close', () => {
  for (const opened of [false, true]) {
    const app = connection();
    app.run('connectSocket()');
    const ws = app.latest();
    ws.hangClosing = true;
    if (opened) ws.emit('open');
    const deadline = app.run('socketAuthTimer');
    assert.equal(app.timeouts.get(deadline).delay, opened ? 7000 : 10000);
    app.fire(deadline);
    assert.equal(app.run('socket'), null);
    assert.equal(ws.closeCalls, 1);
    assert.equal(app.intervals.size, 0);
    assert.equal(app.timeouts.size, 1);
    app.fire(app.run('reconnectTimer'));
    assert.equal(app.run('sockets.length'), 2);
    assert.notEqual(app.run('socket'), ws);
  }
});

test('failed connections back off with bounded jitter and reset after verified initialization', async () => {
  for (const random of [0, 0.999]) {
    const app = connection();
    app.run(`Math.random = () => ${random}; connectSocket();`);
    for (let attempt = 0; attempt < 10; attempt++) {
      app.latest().emit('error');
      app.run('scheduleReconnect()');
      assert.equal(app.timeouts.size, 1);
      const ceiling = Math.min(30000, 1000 * 2 ** attempt);
      const timer = app.run('reconnectTimer');
      assert.ok(app.timeouts.get(timer).delay >= ceiling / 2);
      assert.ok(app.timeouts.get(timer).delay <= ceiling);
      app.fire(timer);
    }
    const ws = app.latest();
    ws.emit('open');
    ws.emit('message', { data: JSON.stringify({ type: 'hello', user: 'me', backlog: {} }) });
    await app.run('inboundQueue');
    assert.equal(app.run('reconnectAttempts'), 0);
    assert.equal(app.nodes.get('#connection-label').textContent, 'Live');
    ws.emit('close');
    assert.ok(app.timeouts.get(app.run('reconnectTimer')).delay <= 1000);
  }
});

test('offline closes the old connection and online reconnects without erasing device history', async () => {
  const app = connection();
  app.run(`globalThis.savedDevice = identity;
    messagesByChat.set(7, [{id: 'saved', text: 'retained history'}]); connectSocket();`);
  const old = app.latest();
  old.emit('open');
  app.run('navigator.onLine = false');
  app.windowEvents.get('offline')();
  assert.equal(app.run('socket'), null);
  assert.equal(app.timeouts.size, 0);
  assert.equal(app.intervals.size, 0);
  await app.run('synchronize(); scheduleReconnect(); connectSocket();');
  assert.equal(app.run('requests.length'), 0);
  assert.equal(app.run('sockets.length'), 1);
  assert.equal(app.run('identity === savedDevice'), true);
  assert.equal(app.run('messagesByChat.get(7).length'), 1);
  app.run('navigator.onLine = true');
  app.windowEvents.get('online')();
  await app.run('inboundQueue');
  assert.equal(app.run('sockets.length'), 2);
  assert.notEqual(app.run('socket'), old);
});

test('navigation releases timers and cached-page restoration reconnects once', async () => {
  const app = connection();
  app.run('connectSocket()');
  app.latest().emit('open');
  app.windowEvents.get('pagehide')();
  assert.equal(app.run('socket'), null);
  assert.equal(app.intervals.size, 0);
  assert.equal(app.timeouts.size, 0);
  await app.run('connectSocket(); scheduleReconnect(); synchronize();');
  assert.equal(app.run('requests.length'), 0);
  assert.equal(app.run('sockets.length'), 1);
  app.windowEvents.get('pageshow')({ persisted: true });
  app.windowEvents.get('pageshow')({ persisted: true });
  assert.equal(app.run('sockets.length'), 2);
});

test('obsolete socket events and heartbeat callbacks cannot affect a replacement', async () => {
  const app = connection();
  app.run('connectSocket()');
  const old = app.latest();
  old.emit('open');
  const heartbeat = app.intervals.get(app.run('heartbeatTimer')).handler;
  app.run('disconnectSocket(); connectSocket();');
  const current = app.latest();
  current.emit('open');
  current.emit('message', { data: JSON.stringify({ type: 'hello', user: 'me', backlog: {} }) });
  await app.run('inboundQueue');
  app.window.now = 120000;
  old.emit('message', { data: JSON.stringify({ type: 'hello', user: 'other-device' }) });
  old.emit('close');
  old.emit('error');
  heartbeat();
  assert.equal(app.run('socket'), current);
  assert.equal(current.closeCalls, 0);
  assert.equal(app.run('reconnectTimer'), null);
  assert.equal(app.nodes.get('#connection-label').textContent, 'Live');
  assert.equal(app.run('errors.length'), 0);
});

test('hello identity mismatch rejects backlog and preserves saved credentials', async () => {
  const app = connection();
  app.run('globalThis.savedDevice = identity; connectSocket();');
  const ws = app.latest();
  ws.emit('open');
  ws.emit('message', { data: JSON.stringify({ type: 'hello', user: 'other-device',
    backlog: { messages: [{ ciphertext: 'must-not-process' }] } }) });
  await app.run('inboundQueue');
  assert.equal(app.run('incoming.length'), 0);
  assert.equal(app.run('socket'), null);
  assert.equal(app.run('identity === savedDevice'), true);
  assert.equal(app.run('identity.token'), 'not-for-display');
  assert.equal(app.nodes.get('#connection-label').textContent, 'Polling');
  assert.match(app.run('errors[0]'), /different device/);
  assert.equal(app.run('errors[0].includes("other-device")'), false);
});

test('Live waits for roster and backlog processing, and failed initialization stays retryable', async () => {
  const app = connection();
  app.run(`loadChats = () => new Promise(resolve => { globalThis.finishRoster = resolve; });
    processIncoming = () => new Promise(resolve => { globalThis.finishBacklog = resolve; });
    connectSocket();`);
  const ws = app.latest();
  ws.emit('open');
  ws.emit('message', { data: JSON.stringify({ type: 'hello', user: 'me', backlog: {} }) });
  await Promise.resolve();
  assert.equal(app.nodes.get('#connection-label').textContent, 'Polling');
  app.run('finishRoster()');
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(app.nodes.get('#connection-label').textContent, 'Polling');
  app.run('finishBacklog()');
  await app.run('inboundQueue');
  assert.equal(app.nodes.get('#connection-label').textContent, 'Live');

  app.run(`disconnectSocket(); loadChats = async () => { throw new ClientError('Roster unavailable'); };
    connectSocket();`);
  app.latest().emit('open');
  app.latest().emit('message', { data: JSON.stringify({ type: 'hello', user: 'me', backlog: {} }) });
  await app.run('inboundQueue');
  await Promise.resolve();
  assert.equal(app.run('socket'), null);
  assert.equal(app.nodes.get('#connection-label').textContent, 'Polling');
  assert.ok(app.run('reconnectTimer'));
});

test('old queued hello cannot mark a replacement live after delayed roster loading', async () => {
  const app = connection();
  app.run(`loadChats = () => new Promise(resolve => { globalThis.finishRoster = resolve; });
    connectSocket();`);
  const old = app.latest();
  old.emit('open');
  old.emit('message', { data: JSON.stringify({ type: 'hello', user: 'me', backlog: {} }) });
  await Promise.resolve();
  app.run('disconnectSocket(); connectSocket(); finishRoster();');
  await app.run('inboundQueue');
  assert.notEqual(app.run('socket'), old);
  assert.equal(app.nodes.get('#connection-label').textContent, 'Polling');
  assert.equal(app.run('incoming.length'), 0);
});

test('HTTP and response-body stalls use a bounded signal and release the polling guard', async () => {
  for (const bodyStalls of [false, true]) {
    const app = connection();
    app.run(`
      globalThis.fetch = async (path, options) => {
        requests.push(path);
        globalThis.requestSignal = options.signal;
        const stalled = () => new Promise((resolve, reject) => {
          options.signal.addEventListener('abort', () => reject(new DOMException('synthetic-private-value', 'AbortError')));
        });
        return ${bodyStalls ? '{ ok: true, json: stalled }' : 'stalled()'};
      };
      loadChats = async () => { await fetchClient('/api/v1/me'); };
      globalThis.pendingSync = synchronize();
    `);
    await Promise.resolve();
    await Promise.resolve();
    assert.equal(app.run('synchronizing'), true);
    const deadline = [...app.timeouts].find(([, timer]) => timer.delay === 15000);
    assert.ok(deadline);
    assert.equal(app.run('requestSignal.aborted'), false);
    app.fire(deadline[0]);
    await app.run('pendingSync');
    assert.equal(app.run('synchronizing'), false);
    assert.equal(app.run('errors.join(" ").includes("synthetic-private-value")'), false);
    assert.equal(app.timeouts.size, 0);
    app.run('pendingSync = synchronize();');
    await Promise.resolve();
    await Promise.resolve();
    assert.equal(app.run('requests.length'), 2);
    app.fire([...app.timeouts].find(([, timer]) => timer.delay === 15000)[0]);
    await app.run('pendingSync');
  }
});

test('successful requests clear their deadline and HTTP 401 does not wait for a body', async () => {
  for (const status of [200, 401]) {
    const app = connection();
    app.run(`globalThis.bodyReads = 0;
      globalThis.fetch = async (path, options) => {
        globalThis.requestSignal = options.signal;
        return {ok: ${status === 200}, status: ${status}, json: async () => {
          bodyReads++; return {value: 'synthetic-response'};
        }};
      };`);
    const result = await app.run('fetchClient("/api/v1/me")');
    assert.equal(result.status, status);
    assert.equal(app.run('bodyReads'), status === 401 ? 0 : 1);
    assert.equal(app.timeouts.size, 0);
    assert.equal(app.run('requestSignal.aborted'), true);
    assert.equal(app.run('identity.token'), 'not-for-display');
  }
});
