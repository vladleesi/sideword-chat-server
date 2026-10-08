const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function client(persistHistory = false) {
  const element = { addEventListener() {} };
  const context = vm.createContext({
    TextEncoder, TextDecoder, DOMException,
    crypto: require('node:crypto').webcrypto, btoa,
    document: { querySelector: () => element, addEventListener() {} },
    window: { addEventListener() {} },
  });
  const source = fs.readFileSync('app/static/client.js', 'utf8');
  vm.runInContext(fs.readFileSync('app/static/client-protocol.js', 'utf8'), context);
  vm.runInContext(source.replace(/start\(\)\.catch[^\n]+/, ''), context);
  vm.runInContext(`
    messageConfirmationsSupported = true;
    senderKeyIsLoaded = () => true;
    decryptMessage = async (message) => message.ciphertext;
  `, context);
  if (!persistHistory) vm.runInContext('persistHistoryEntry = async () => {};', context);
  return (code) => vm.runInContext(code, context);
}

function renderingClient(persistHistory = false) {
  const run = client(persistHistory);
  run(`
    class TestNode {
      constructor() { this.dataset = {}; this.children = []; this.scrollTop = 0; this.clientHeight = 100; }
      addEventListener() {}
      setAttribute(name, value) { this[name] = value; }
      get firstChild() { return this.children[0]; }
      get scrollHeight() { return this.children.length * 100; }
      replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
      append(...nodes) { for (const node of nodes) this.insertBefore(node, null); }
      insertBefore(node, reference) {
        node.remove();
        const index = reference ? this.children.indexOf(reference) : this.children.length;
        this.children.splice(index, 0, node);
        node.parent = this;
      }
      remove() {
        if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1);
        this.parent = null;
      }
    }
    document.createElement = () => new TestNode();
    document.createElementNS = () => new TestNode();
    elements.messageList = new TestNode();
    selectedChatId = 7;
    messagesByChat.set(7, [
      { id: 'a', text: 'first', createdAt: 1 },
      { id: 'c', text: 'third', createdAt: 3 },
      { id: 'd', text: 'fourth', createdAt: 4 },
    ]);
    renderMessages();
    globalThis.originalNodes = [...elements.messageList.children];
  `);
  return run;
}

test('polling and live group messages label the matching sender using safe text', async () => {
  const run = renderingClient();
  await run(`(async () => {
    chats = [{ id: 7, chat_type: 'group', participants: [
      { public_id: 'alice', display_name: '<img src=x onerror=alert(1)>' },
      { public_id: 'bob', display_name: '<img src=x onerror=alert(1)>' },
    ] }];
    messagesByChat.clear();
    globalThis.savedEntries = [];
    globalThis.acknowledgements = [];
    persistHistoryEntry = async (chatId, entry) => {
      savedEntries.push(JSON.parse(JSON.stringify(entry)));
      if (pendingReadsByChat.size) throw new Error('acknowledgement queued before persistence');
    };
    api = async (path) => { acknowledgements.push(path); };
    globalThis.pollMessage = { delivery_id: 'a'.repeat(32), chat_id: 7,
      sender_public_id: 'alice', client_message_id: 'same-id', ciphertext: 'first',
      created_at: '2026-10-06T12:00:00Z' };
    await processIncoming({ messages: [pollMessage] });
    await handleSocketPayload({ type: 'message', message: { ...pollMessage,
      delivery_id: 'b'.repeat(32), sender_public_id: 'bob', ciphertext: 'second' } });
    await processIncoming({ messages: [pollMessage] });
  })()`);
  assert.equal(run('messagesByChat.get(7).filter(e => e.kind === "theirs").length'), 2);
  assert.equal(run('savedEntries.filter(e => e.kind === "theirs").length'), 2);
  for (const [index, sender] of ['alice', 'bob'].entries()) {
    const label = `From <img src=x onerror=alert(1)> (${sender})`;
    assert.equal(run(`savedEntries.filter(e => e.kind === "theirs")[${index}].meta`), label);
    assert.equal(run(`elements.messageList.children[${index}].messageHeader.sender.textContent`), '<img src=x onerror=alert(1)>');
    assert.equal(run(`elements.messageList.children[${index}].messageHeader.sender.children.length`), 0);
  }
  assert.equal(run('acknowledgements.length'), 2);
});

test('sender names come from the message chat even when another group is selected', async () => {
  const run = client();
  await run(`(async () => {
    selectedChatId = 8;
    renderMessages = () => {};
    chats = [
      { id: 7, chat_type: 'group', participants: [{ public_id: 'alice', display_name: 'Alice' }] },
      { id: 8, chat_type: 'group', participants: [{ public_id: 'alice', display_name: 'Other label' }] },
    ];
    senderKeyIsLoaded = () => false;
    globalThis.rosterRefreshes = 0;
    loadChats = async () => { rosterRefreshes++; chats[0].participants[0].display_name = 'Fresh Alice'; };
    await processMessages([{ delivery_id: 'a'.repeat(32), chat_id: 7,
      sender_public_id: 'alice', client_message_id: 'm', ciphertext: 'hello' }]);
  })()`);
  assert.equal(run('rosterRefreshes'), 1);
  assert.equal(run('messagesByChat.get(7)[0].meta'), 'From Fresh Alice (alice)');
});

test('personal sender names and unnamed fallbacks use the matching roster', async () => {
  for (const [chatType, name] of [['group', null], ['group', ''], ['group', '   '], ['personal', 'Alice'], ['personal', null]]) {
    const run = client();
    run(`chats = [{ id: 7, chat_type: ${JSON.stringify(chatType)}, participants: [
      { public_id: 'alice', display_name: ${JSON.stringify(name)} },
    ] }];`);
    await run(`processMessages([{ delivery_id: 'a'.repeat(32), chat_id: 7,
      sender_public_id: 'alice', client_message_id: 'm', ciphertext: 'hello' }])`);
    assert.equal(run('messagesByChat.get(7)[0].meta'), name?.trim() ? 'From Alice (alice)' : 'From alice');
  }
});

test('group sender labels survive encrypted history reload without a roster', async () => {
  const run = renderingClient(true);
  await run(`(async () => {
    identity = { publicId: 'recipient', storageKey: await crypto.subtle.generateKey(
      { name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt']) };
    globalThis.records = [];
    putDatabaseValue = async (key, value) => { records.push({ key, value }); };
    readHistoryRecords = async () => records;
    chats = [{ id: 7, chat_type: 'group', participants: [{ public_id: 'alice', display_name: 'Alice' }] }];
    messagesByChat.clear();
    await processMessages([{ delivery_id: 'a'.repeat(32), chat_id: 7,
      sender_public_id: 'alice', client_message_id: 'm', ciphertext: 'hello' }]);
    chats = [];
    messagesByChat.clear();
    renderMessages();
    await loadStoredHistory();
  })()`);
  assert.equal(run('records.length'), 1);
  assert.equal(run('identity.storageKey.extractable'), false);
  assert.equal(run('messagesByChat.get(7)[0].meta'), 'From Alice (alice)');
  assert.equal(run('elements.messageList.firstChild.messageHeader.sender.textContent'), 'Alice');
});

test('existing group history gains roster names without changing nodes or delivery identities', async () => {
  const run = renderingClient();
  await run(`(async () => {
    chats = [];
    messagesByChat.clear();
    globalThis.legacyId = 'incoming:' + JSON.stringify([7, 'alice', 'old-message']);
    await appendMessage(7, { id: legacyId, kind: 'theirs', text: 'old message',
      meta: 'From alice', createdAt: 1 }, false);
    globalThis.legacyNode = elements.messageList.firstChild;
    elements.messageList.scrollTop = 12;
    chats = [{ id: 7, chat_type: 'group', participants: [{ public_id: 'alice', display_name: 'Alice' }] }];
    renderMessages();
  })()`);
  assert.equal(run('elements.messageList.firstChild === legacyNode'), true);
  assert.equal(run('legacyNode.messageHeader.sender.textContent'), 'Alice');
  assert.equal(run('messagesByChat.get(7)[0].id === legacyId'), true);
  assert.equal(run('messagesByChat.get(7)[0].meta'), 'From alice');
  assert.equal(run('pendingReadsByChat.size'), 0);
  run(`chats[0].participants[0].display_name = '<b>New name</b>'; renderMessages();`);
  assert.equal(run('legacyNode.messageHeader.sender.textContent'), '<b>New name</b>');
  assert.equal(run('legacyNode.messageHeader.sender.children.length'), 0);
  run(`chats[0].participants[0].display_name = null; renderMessages();`);
  assert.equal(run('legacyNode.messageHeader.sender.textContent'), 'Participant');
});

test('unrecognized history and other-chat identities keep saved sender labels', () => {
  const run = client();
  run(`chats = [{ id: 7, chat_type: 'group', participants: [{ public_id: 'alice', display_name: 'Alice' }] }];`);
  for (const id of ['incoming-fixture', 'incoming:invalid', 'incoming:null', 'incoming:' + JSON.stringify([8, 'alice', 'm'])]) {
    assert.equal(run(`messageMeta(7, { id: ${JSON.stringify(id)}, kind: 'theirs', meta: 'Saved label' })`), 'Saved label');
  }
  assert.equal(run(`messageMeta(7, { senderPublicId: 'missing', kind: 'theirs', meta: 'Saved label' })`), 'Saved label');
});

test('outgoing sends save the sender name before removing retry state', async () => {
  const run = renderingClient(true);
  await run(`(async () => {
    identity = { token: 'test-access', publicId: 'alice', storageKey: await crypto.subtle.generateKey(
      { name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt']) };
    chats = [{ id: 7, chat_type: 'personal', participants: [
      { public_id: 'alice', display_name: '  <b>Alice</b>  ' },
    ] }];
    messagesByChat.clear();
    globalThis.records = [];
    putDatabaseValue = async (key, value) => { records.push({ key, value }); };
    readHistoryRecords = async () => records.filter(record => record.key.startsWith('outbox:'));
    deviceLock = async (_, action) => action();
    api = async () => ({ created_at: '2026-10-07T12:00:00Z', recipients: [] });
    removeOutbox = async () => {
      if (!records.some(record => record.key.startsWith('history:'))) throw new Error('history not saved');
    };
    await persistOutbox({ chatId: 7, messageId: 'outbound-message-id', plaintext: 'hello',
      createdAt: Date.now(), envelopes: [], expiresAt: Date.now() + 60000 });
    await flushOutbox();
    chats = [];
    messagesByChat.clear();
    readHistoryRecords = async () => records.filter(record => record.key.startsWith('history:'));
    await loadStoredHistory();
  })()`);
  assert.equal(run('messagesByChat.get(7).find(e => e.kind === "mine").meta'), 'Sent by <b>Alice</b>');
  assert.equal(run('messagesByChat.get(7).find(e => e.kind === "mine").senderPublicId'), 'alice');
  assert.equal(run('elements.messageList.firstChild.messageHeader.sender.textContent'), '<b>Alice</b>');
  assert.equal(run('elements.messageList.firstChild.messageHeader.sender.children.length'), 0);
});

test('existing outgoing history resolves names and uses a nontechnical fallback', async () => {
  const run = renderingClient();
  await run(`identity = { publicId: 'alice' };
    chats = [{ id: 7, participants: [{ public_id: 'alice', display_name: 'Alice' }] }];
    messagesByChat.clear();
    appendMessage(7, { id: 'outgoing:old', kind: 'mine', text: 'hello', meta: 'Sent ? old' }, false)`);
  assert.equal(run('elements.messageList.firstChild.messageHeader.sender.textContent'), 'Alice');
  run(`chats[0].participants[0].display_name = '   '; renderMessages();`);
  assert.equal(run('elements.messageList.firstChild.messageHeader.sender.textContent'), 'You');
});

test('polling and live receipts persist safe reader names from their own chat before acknowledgement', async () => {
  const run = renderingClient(true);
  await run(`(async () => {
    identity = { publicId: 'alice', storageKey: await crypto.subtle.generateKey(
      { name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt']) };
    chats = [
      { id: 7, chat_type: 'group', participants: [
        { public_id: 'bob', display_name: '  <b>Bob</b>  ' },
        { public_id: 'carol', display_name: '   ' },
      ] },
      { id: 8, participants: [{ public_id: 'bob', display_name: 'Wrong name' }] },
    ];
    messagesByChat.clear();
    globalThis.records = [];
    putDatabaseValue = async (key, value) => { records.push({ key, value }); };
    api = async () => { if (records.length !== seenReceiptIds.size) throw new Error('history not saved'); };
    const receipt = { chat_id: 7, reader_public_id: 'bob', client_message_id: 'outbound',
      delivery_id: 'a'.repeat(32), created_at: '2026-10-07T12:00:00Z' };
    await processIncoming({ read_receipts: [receipt] });
    await handleSocketPayload({ type: 'read', read: { ...receipt,
      reader_public_id: 'carol', delivery_id: 'b'.repeat(32) } });
    await processIncoming({ read_receipts: [receipt] });
    chats = [];
    messagesByChat.clear();
    readHistoryRecords = async () => records;
    await loadStoredHistory();
  })()`);
  assert.equal(run('records.length'), 2);
  assert.equal(run('pendingReceiptIds.size'), 0);
  assert.equal(run('messagesByChat.get(7).every(e => e.kind === "receipt")'), true);
  assert.equal(run('messagesByChat.get(7)[0].readerPublicId'), 'bob');
  assert.equal(run('elements.messageList.firstChild.textContent'), 'No messages yet.');
  assert.equal(run('outgoingStatus(7, {id: "outgoing:outbound", kind: "mine"}).status'), 'sent');
});

test('legacy receipts migrate to hidden delivery evidence without claiming human reading', async () => {
  const run = renderingClient();
  await run(`messagesByChat.clear();
    appendMessage(7, { id: 'receipt:' + JSON.stringify([7, 'bob', 'm']), kind: 'system',
      text: 'Message m was read by bob.', createdAt: 123 }, false)`);
  assert.equal(run('elements.messageList.firstChild.textContent'), 'No messages yet.');
  assert.equal(run('messagesByChat.get(7)[0].legacyText'), 'Message m was read by bob.');
  assert.equal(run('outgoingStatus(7, {id:"outgoing:m", kind:"mine"}).states[0].delivered'), true);
  assert.equal(run('outgoingStatus(7, {id:"outgoing:m", kind:"mine"}).states[0].read'), false);
  assert.equal(run('outgoingStatus(7, {id:"outgoing:m", kind:"mine"}).status'), 'sent');
  assert.equal(run('pendingReceiptIds.size'), 0);
  for (const id of ['receipt:invalid', 'receipt:null', 'receipt:' + JSON.stringify([8, 'bob', 'm'])]) {
    await run(`appendMessage(7, { id: ${JSON.stringify(id)}, kind: 'system', text: 'Saved receipt' }, false)`);
  }
  assert.equal(run('elements.messageList.firstChild.textContent'), 'No messages yet.');
});

test('incoming messages preserve existing nodes, chronological order, and reading position', async () => {
  const run = renderingClient();
  await run(`elements.messageList.scrollTop = 50;
    appendMessage(7, { id: 'b', text: '<b>plain text</b>', createdAt: 2 })`);
  assert.equal(run('elements.messageList.scrollTop'), 50);
  assert.equal(run('elements.messageList.children[0] === originalNodes[0]'), true);
  assert.equal(run('elements.messageList.children[2] === originalNodes[1]'), true);
  assert.equal(run('elements.messageList.children[3] === originalNodes[2]'), true);
  assert.equal(run('elements.messageList.children[1].messageContent.textContent'), '<b>plain text</b>');
  run('renderMessages()');
  assert.equal(run('elements.messageList.scrollTop'), 50);
  assert.equal(run('elements.messageList.children[0] === originalNodes[0]'), true);
});

test('new messages follow the bottom and switching chats clears previous messages', async () => {
  const run = renderingClient();
  await run(`elements.messageList.scrollTop = 200;
    appendMessage(7, { id: 'e', text: 'latest', createdAt: 5 })`);
  assert.equal(run('elements.messageList.scrollTop'), 400);
  run(`selectedChatId = 8; renderMessages();`);
  assert.equal(run('elements.messageList.children.length'), 1);
  assert.equal(run('elements.messageList.firstChild.textContent'), 'No messages yet.');
  await run(`appendMessage(8, { id: 'a', text: 'different chat', createdAt: 1 })`);
  assert.equal(run('elements.messageList.children.length'), 1);
  assert.equal(run('elements.messageList.firstChild.messageContent.textContent'), 'different chat');
  assert.equal(run('elements.messageList.firstChild === originalNodes[0]'), false);
});

for (const mobile of [false, true]) {
  test(`${mobile ? 'mobile' : 'desktop'} refresh opens encrypted history at the latest message even when synchronization fails`, async () => {
    const run = renderingClient(true);
    await run(`(async () => {
      globalThis.savedIdentity = { publicId: 'recipient', token: 'saved-session', activeInviteChatId: 7,
        storageKey: await crypto.subtle.generateKey(
          { name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt']) };
      identity = savedIdentity;
      globalThis.records = [];
      putDatabaseValue = async (key, value) => { records.push({ key, value }); };
      readHistoryRecords = async () => records;
      for (let i = 0; i < 6; i++) {
        await persistHistoryEntry(7, { id: 'saved-' + i, kind: 'theirs', text: 'message ' + i, createdAt: i + 1 });
      }
      messagesByChat.clear();
      renderedMessages.clear();
      renderedChatId = undefined;
      selectedChatId = null;
      elements.messageList.replaceChildren();
      elements.clientPanel = { hidden: true, dataset: { mobileView: 'chats' },
        setAttribute(name, value) { if (name === 'data-mobile-view') this.dataset.mobileView = value; } };
      window.matchMedia = () => ({ matches: ${mobile} });
      globalThis.history = { state: { sidewordChatView: {
        publicId: 'recipient', chatId: 7, view: 'conversation' } } };
      elements.setupPanel = {};
      elements.inviteToken.value = '';
      elements.connectionState.classList = { toggle() {} };
      // Hidden DOM boxes have no scroll range; visible boxes clamp scrollTop.
      let scrollTop = 0;
      const visible = () => !elements.clientPanel.hidden
        && (!${mobile} || elements.clientPanel.dataset.mobileView === 'conversation');
      Object.defineProperties(elements.messageList, {
        clientHeight: { get: () => visible() ? 100 : 0 },
        scrollHeight: { get: () => visible() ? elements.messageList.children.length * 100 : 0 },
        scrollTop: {
          get: () => visible() ? scrollTop : 0,
          set: value => { scrollTop = Math.max(0, Math.min(value,
            elements.messageList.scrollHeight - elements.messageList.clientHeight)); },
        },
      });
      readIdentity = async () => savedIdentity;
      window.isSecureContext = true;
      window.crypto = crypto;
      window.indexedDB = {};
      window.setInterval = () => 1;
      api = async () => { throw new Error('offline'); };
      showToast = () => {};
      connectSocket = () => {};
      await start();
    })()`);
    assert.equal(run('elements.clientPanel.hidden'), false);
    if (mobile) assert.equal(run('elements.clientPanel.dataset.mobileView'), 'conversation');
    assert.equal(run('elements.messageList.children.length'), 6);
    assert.equal(run('elements.messageList.scrollTop'), 500);
    assert.equal(run('elements.messageList.children[5].messageContent.textContent'), 'message 5');
    await run(`elements.messageList.scrollTop = 100;
      appendMessage(7, { id: 'new-message', text: 'latest', createdAt: 7 }, false)`);
    assert.equal(run('elements.messageList.scrollTop'), 100);
  });
}

test('hidden history rendering preserves the first visible chat transition', () => {
  const run = renderingClient();
  run(`elements.clientPanel = { hidden: true }; selectedChatId = 8;
    messagesByChat.set(8, [
      { id: 'old', text: 'old', createdAt: 1 },
      { id: 'latest', text: 'latest', createdAt: 2 },
    ]); renderMessages();`);
  assert.equal(run('renderedChatId'), 7);
  assert.equal(run('elements.messageList.firstChild === originalNodes[0]'), true);
  run('elements.clientPanel.hidden = false; renderMessages();');
  assert.equal(run('renderedChatId'), 8);
  assert.equal(run('elements.messageList.scrollTop'), 200);
  assert.equal(run('elements.messageList.children[1].messageContent.textContent'), 'latest');
});

test('reused SQLite row IDs do not drop new messages or receipts', async () => {
  const run = client();
  await run(`(async () => {
    for (let i = 0; i < 10; i++) {
      const message = { delivery_id: 'a'.repeat(32), id: 1, chat_id: 7, sender_public_id: 'alice',
        client_message_id: 'uuid-' + i, ciphertext: 'text-' + i,
        created_at: '2026-09-26T02:00:' + String(i).padStart(2, '0') };
      await processMessages([message]);
      await processMessages([message]);
      await processReceipts([{ ...message, reader_public_id: 'bob' }]);
    }
  })()`);
  assert.equal(run('messagesByChat.get(7).filter(e => e.kind === "theirs").length'), 10);
  assert.equal(run('messagesByChat.get(7).filter(e => e.kind === "receipt").length'), 10);
});

test('invite retry credentials persist before admission and stay stable after a failed save', async () => {
  const run = client();
  await run(`(async () => {
    identity = { publicKey: 'device-public-key' };
    writeIdentity = async () => { throw new Error('storage unavailable'); };
    try {
      await prepareInviteResume('example-invite');
      throw new Error('must not continue to admission');
    } catch (error) {
      if (error.message !== 'storage unavailable') throw error;
    }
  })()`);
  const credential = run('identity.inviteCredentials["example-invite"]');
  assert.match(credential, /^[A-Za-z0-9_-]{43}$/);
  await run(`(async () => {
    writeIdentity = async (value) => { globalThis.savedIdentity = structuredCloneForTest(value); };
    globalThis.structuredCloneForTest = (value) => JSON.parse(JSON.stringify(value));
    await prepareInviteResume('example-invite');
  })()`);
  assert.equal(run('savedIdentity.inviteCredentials["example-invite"]'), credential);
  assert.equal(await run('prepareInviteResume("example-invite")'), credential);
  assert.notEqual(await run('prepareInviteResume("another-invite")'), credential);
  assert.equal(run('Object.hasOwn(savedIdentity, "password")'), false);
});

test('naive server UTC and explicit UTC have identical ordering', async () => {
  const run = client();
  assert.equal(run('serverTimestamp("2026-09-26T02:00:00")'), Date.parse('2026-09-26T02:00:00Z'));
  await run(`(async () => {
    await appendMessage(7, { id: 'new', createdAt: serverTimestamp('2026-09-26T02:00:02Z') });
    await appendMessage(7, { id: 'old', createdAt: serverTimestamp('2026-09-26T02:00:01') });
  })()`);
  assert.equal(run('messagesByChat.get(7).map(e => e.id).join(",")'), 'old,new');
});

test('session timer formats days, rounds seconds and clamps expiry', () => {
  const run = client();
  assert.equal(run('formatTimeRemaining(1001)'), '00:00:02');
  assert.equal(run('formatTimeRemaining(90061000)'), '1d 01:01:01');
  assert.equal(run('formatTimeRemaining(-1000)'), '00:00:00');
});

test('failed local persistence can retry without acknowledging an unsaved message', async () => {
  const run = client();
  await run(`(async () => {
    persistHistoryEntry = async () => { throw new Error('storage unavailable'); };
    const message = { delivery_id: 'a'.repeat(32), id: 1, chat_id: 7, sender_public_id: 'alice',
      client_message_id: 'uuid', ciphertext: 'hello', created_at: '2026-09-26T02:00:00Z' };
    await processMessages([message]);
  })()`);
  assert.equal(run('pendingReadsByChat.size'), 0);
  assert.equal(run('seenMessageIds.size'), 0);
  await run(`(async () => {
    persistHistoryEntry = async () => {};
    await processMessages([{ delivery_id: 'a'.repeat(32), id: 1, chat_id: 7, sender_public_id: 'alice',
      client_message_id: 'uuid', ciphertext: 'hello', created_at: '2026-09-26T02:00:00Z' }]);
  })()`);
  assert.equal(run('pendingDeliveries.size'), 1);
  assert.equal(run('messagesByChat.get(7).filter(e => e.kind === "theirs").length'), 1);
});

test('exact acknowledgements retain colliding group identities and retry lost responses', async () => {
  const run = client();
  await run(`(async () => {
    globalThis.calls = [];
    api = async (path, options) => {
      calls.push({ path, body: JSON.parse(options.body) });
      throw new Error('response lost');
    };
    for (const [sender, delivery] of [['alice', 'a'], ['bob', 'b']]) {
      await processMessages([{ id: 1, delivery_id: delivery.repeat(32), chat_id: 7,
        sender_public_id: sender, client_message_id: 'collision', ciphertext: sender }]);
    }
    await processReceipts([{ id: 1, delivery_id: 'c'.repeat(32), chat_id: 7,
      reader_public_id: 'bob', client_message_id: 'outbound' }]);
  })()`);
  await assert.rejects(run('flushAcknowledgements()'), /response lost/);
  assert.equal(run('pendingDeliveries.size'), 2);
  await run(`api = async (path, options) => calls.push({ path, body: JSON.parse(options.body) });
    flushAcknowledgements()`);
  const calls = JSON.parse(run('JSON.stringify(calls)'));
  assert.equal(calls[0].path, '/api/v1/ack/exact');
  assert.deepEqual(calls[1], calls[0]);
  assert.deepEqual(calls[1].body.messages.map(m => m.sender_public_id), ['alice', 'bob']);
  assert.equal(calls[2].path, '/api/v1/ack/exact');
  assert.deepEqual(calls[2].body, { receipts: [{ delivery_id: 'c'.repeat(32),
    chat_id: 7, client_message_id: 'outbound', reader_public_id: 'bob' }] });
  assert.equal(run('pendingReadsByChat.size + pendingReceiptIds.size'), 0);
});

test('batches are bounded and receipt failures keep only unfinished acknowledgements', async () => {
  const run = client();
  await run(`(async () => {
    for (let i = 0; i < 205; i++) {
      await processReceipts([{ id: i, delivery_id: i.toString(16).padStart(32, '0'),
        chat_id: 7, reader_public_id: 'bob', client_message_id: 'm-' + i }]);
    }
    globalThis.calls = [];
    api = async (path, options) => {
      calls.push(JSON.parse(options.body));
      if (calls.length === 2) throw new Error('offline');
    };
  })()`);
  await assert.rejects(run('flushAcknowledgements()'), /offline/);
  assert.equal(run('pendingReceiptIds.size'), 105);
  await run('flushAcknowledgements()');
  assert.deepEqual(JSON.parse(run('JSON.stringify(calls.map(c => c.receipts.length))')), [100, 100, 100, 5]);
  assert.equal(run('pendingReceiptIds.size'), 0);
});

test('older servers cannot trigger fallback to ambiguous deletion endpoints', async () => {
  const run = client();
  await run(`processMessages([{ id: 1, chat_id: 7, sender_public_id: 'alice',
    client_message_id: 'legacy', ciphertext: 'stored' }])`);
  assert.equal(run('pendingReadsByChat.size'), 0);
  await run(`queueRead({ delivery_id: 'a'.repeat(32), chat_id: 7,
    sender_public_id: 'alice', client_message_id: 'm' });
    globalThis.paths = [];
    api = async (path) => { paths.push(path); throw new Error('404'); };`);
  await assert.rejects(run('flushAcknowledgements()'), /404/);
  assert.deepEqual(JSON.parse(run('JSON.stringify(paths)')), ['/api/v1/chats/7/read/exact']);
  assert.equal(run('pendingReadsByChat.get(7).size'), 1);
});
