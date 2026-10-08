const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');

const { client } = require('./client_dom.cjs');

test('mobile opening and reopening a preselected chat lands on latest messages without observer timing', async () => {
  const app = client({ mobile: true, viewportAware: true });
  await app.run(`(async () => {
    for (let index = 0; index < 6; index++) await appendMessage(7,
      { id: String(index), kind: 'theirs', text: 'message ' + index, createdAt: index + 1 }, false);
  })()`);
  const list = app.nodes.get('#message-list');
  assert.equal(list.scrollHeight, 0);
  assert.equal(app.run('renderedChatId'), undefined);
  app.nodes.get('#chat-list').children[0].children[0].emit('click');
  assert.equal(list.scrollTop, 500);
  const original = [...list.children];
  list.scrollTop = 100;
  list.emit('scroll');
  app.nodes.get('#back-to-chats').emit('click');
  list.emit('scroll');
  app.resizeMessages();
  assert.equal(app.run('followResizedMessages'), false);
  await app.run("appendMessage(7, { id: 'new', kind: 'theirs', text: 'latest', createdAt: 7 }, false)");
  app.nodes.get('#chat-list').children[0].children[0].emit('click');
  assert.equal(list.scrollTop, 600);
  assert.equal(list.children[6].messageContent.textContent, 'latest');
  assert.equal(list.children[0], original[0]);
  list.scrollTop = 100;
  list.emit('scroll');
  await app.run("appendMessage(7, { id: 'later', kind: 'theirs', text: 'later', createdAt: 8 }, false)");
  app.run('selectChat(7);');
  app.resizeMessages();
  assert.equal(list.scrollTop, 100);
});

test('mobile navigation restores the actual selected chat and Back view in the same tab', () => {
  const app = client({ mobile: true });
  app.run("chats.push({ ...chats[0], id: 8 }); identity.activeInviteChatId = 7; renderChats();");
  app.nodes.get('#chat-list').children[1].children[0].emit('click');
  assert.deepEqual(app.history.state.sidewordChatView,
    { publicId: 'me', chatId: 8, view: 'conversation' });
  assert.doesNotMatch(JSON.stringify(app.history.state), /not-for-display|never-display/);
  const restored = client({ mobile: true, navigationState: app.history.state });
  restored.run('identity.activeInviteChatId = 7; restoreChatView();');
  assert.equal(restored.run('selectedChatId'), 8);
  assert.equal(restored.nodes.get('#client-panel').dataset.mobileView, 'conversation');
  app.nodes.get('#back-to-chats').emit('click');
  const list = client({ mobile: true, navigationState: app.history.state });
  list.run('identity.activeInviteChatId = 7; restoreChatView();');
  assert.equal(list.run('selectedChatId'), 8);
  assert.equal(list.nodes.get('#client-panel').dataset.mobileView, 'chats');
});

test('mobile startup restores a selected chat other than the last invite using encrypted local history', async () => {
  const app = client({ mobile: true, viewportAware: true, navigationState: {
    sidewordChatView: { publicId: 'me', chatId: 8, view: 'conversation' },
  } });
  await app.run(`(async () => {
    identity.activeInviteChatId = 7;
    identity.storageKey = await crypto.subtle.generateKey(
      { name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt']);
    globalThis.records = [];
    putDatabaseValue = async (key, value) => records.push({key, value});
    readHistoryRecords = async () => records;
    await persistHistoryEntry(7, { id: 'other', text: 'other room', createdAt: 1 });
    for (let index = 0; index < 6; index++) await persistHistoryEntry(8,
      { id: 'saved-' + index, kind: 'theirs', text: 'selected ' + index, createdAt: index + 1 });
    messagesByChat.clear(); renderedMessages.clear(); renderedChatId = undefined; selectedChatId = null;
    elements.clientPanel.hidden = true;
    elements.inviteToken.value = '';
    window.isSecureContext = true; window.crypto = crypto; window.indexedDB = {};
    window.setInterval = () => 1;
    readIdentity = async () => identity;
    api = async () => { throw new Error('offline'); };
    showToast = () => {}; connectSocket = () => {};
    await start();
  })()`);
  assert.equal(app.run('selectedChatId'), 8);
  assert.equal(app.nodes.get('#client-panel').dataset.mobileView, 'conversation');
  const list = app.nodes.get('#message-list');
  assert.equal(list.children.length, 6);
  assert.equal(list.scrollTop, 500);
  assert.equal(list.children[5].messageContent.textContent, 'selected 5');
  assert.equal(app.run('pendingReadsByChat.size'), 0);
});

test('opening a different invite ignores remembered mobile navigation and preserves activation', async () => {
  const app = client({ mobile: true, navigationState: {
    sidewordChatView: { publicId: 'me', chatId: 8, view: 'conversation' },
  } });
  await app.run(`(async () => {
    selectedChatId = null; identity.activeInviteChatId = 7;
    identity.activeInviteToken = 'A'.repeat(24); identity.storageKey = {};
    elements.inviteToken.value = 'B'.repeat(24);
    window.isSecureContext = true; window.crypto = crypto; window.indexedDB = {};
    window.setInterval = () => 1;
    readIdentity = async () => identity;
    loadStoredHistory = async () => { throw new Error('must not restore another invite'); };
    api = async () => { throw new Error('must not resume another invite'); };
    await start();
  })()`);
  assert.equal(app.run('selectedChatId'), null);
  assert.equal(app.run('invitePending'), true);
  assert.equal(app.nodes.get('#setup-panel').hidden, false);
  assert.equal(app.nodes.get('#client-panel').hidden, true);
});

test('mobile restore rejects foreign-device or malformed navigation and supports older tabs', () => {
  for (const state of [null,
    { publicId: 'another-device', chatId: 8, view: 'conversation' },
    { publicId: 'me', chatId: '8', view: 'conversation' },
    { publicId: 'me', chatId: -8, view: 'conversation' },
    { publicId: 'me', chatId: 8, view: 'invalid-view' },
    { publicId: 'me', chatId: null, view: 'conversation' },
  ]) {
    const app = client({ mobile: true, navigationState: { sidewordChatView: state } });
    app.run('identity.activeInviteChatId = 7; restoreChatView();');
    assert.equal(app.run('selectedChatId'), 7);
    assert.equal(app.nodes.get('#client-panel').dataset.mobileView, 'conversation');
  }
});

test('missing restored chats return mobile to the list without persisting an unavailable selection', async () => {
  const app = client({ mobile: true, navigationState: {
    sidewordChatView: { publicId: 'me', chatId: 99, view: 'conversation' },
  } });
  app.run(`restoreChatView(); identity.publicKey = 'own-key';
    api = async () => ({ user: { public_id: 'me', public_key: 'own-key' }, chats: chats });
    writeIdentity = async () => {}; observePeerKey = async peer => peer;`);
  await app.run('loadChats();');
  assert.equal(app.nodes.get('#client-panel').dataset.mobileView, 'chats');
  assert.deepEqual(app.history.state.sidewordChatView, { publicId: 'me', chatId: null, view: 'chats' });
});

test('unavailable navigation saves keep mobile chat opening and Back usable', () => {
  const app = client({ mobile: true });
  app.history.replaceState = () => { throw new Error('state save denied'); };
  app.nodes.get('#chat-list').children[0].children[0].emit('click');
  assert.equal(app.nodes.get('#client-panel').dataset.mobileView, 'conversation');
  app.nodes.get('#back-to-chats').emit('click');
  assert.equal(app.nodes.get('#client-panel').dataset.mobileView, 'chats');
});

test('desktop selection does not consume or overwrite mobile navigation state', () => {
  const navigationState = { sidewordChatView: { publicId: 'me', chatId: 8, view: 'conversation' } };
  const app = client({ navigationState });
  app.run('identity.activeInviteChatId = 7; restoreChatView();');
  assert.equal(app.run('selectedChatId'), 7);
  app.nodes.get('#chat-list').children[0].children[0].emit('click');
  assert.deepEqual(app.history.state, navigationState);
});

test('mobile keyboard viewport height updates without interfering with pinch zoom', () => {
  const app = client({ mobile: true });
  assert.equal(app.properties.get('--visual-viewport-height'), '800px');
  app.window.visualViewport.height = 360;
  app.viewportEvents.get('resize')();
  assert.equal(app.properties.get('--visual-viewport-height'), '360px');
  app.window.visualViewport.scale = 2;
  app.window.visualViewport.height = 180;
  app.viewportEvents.get('resize')();
  assert.equal(app.properties.get('--visual-viewport-height'), '360px');
  app.window.visualViewport.scale = 1;
  app.window.visualViewport.height = 800;
  app.viewportEvents.get('resize')();
  assert.equal(app.properties.get('--visual-viewport-height'), '800px');
});

test('message viewport resizing follows latest but keeps older reading positions', async () => {
  const app = client();
  await app.run(`(async () => {
    for (let index = 0; index < 4; index++) await appendMessage(7,
      { id: String(index), kind: 'theirs', text: 'message', createdAt: index + 1 }, false);
  })()`);
  const list = app.nodes.get('#message-list');
  list.scrollTop = list.scrollHeight - list.clientHeight;
  list.emit('scroll');
  list.clientHeight = 60;
  app.resizeMessages();
  assert.equal(list.scrollTop, list.scrollHeight);
  list.scrollTop = 20;
  list.emit('scroll');
  list.clientHeight = 40;
  app.resizeMessages();
  assert.equal(list.scrollTop, 20);
});

test('polling while mobile conversation is hidden preserves older reading position', async () => {
  const app = client({ mobile: true });
  app.nodes.get('#chat-list').children[0].children[0].emit('click');
  await app.run(`(async () => {
    for (let index = 0; index < 4; index++) await appendMessage(7,
      { id: String(index), kind: 'theirs', text: 'message', createdAt: index + 1 }, false);
  })()`);
  const list = app.nodes.get('#message-list');
  list.scrollTop = 20;
  list.emit('scroll');
  app.nodes.get('#back-to-chats').emit('click');
  list.clientHeight = 0;
  app.run('renderMessages();');
  app.resizeMessages();
  assert.equal(list.scrollTop, 20);
});

test('roster refresh retains keyboard focus on the selected chat button', () => {
  const app = client({ mobile: true });
  app.nodes.get('#chat-list').children[0].children[0].focus();
  app.run('selectChat(7);');
  assert.equal(app.document.activeElement, app.nodes.get('#chat-list').children[0].children[0]);
  assert.equal(app.document.activeElement.focusOptions.preventScroll, true);
});

test('participant details expose full values, duplicate identities, unchecked and local-pin states as text', () => {
  const app = client();
  const rows = app.nodes.get('#participant-keys').children;
  assert.equal(rows.length, 3);
  assert.equal(rows[0].tagName, 'details');
  assert.equal(rows[0].children[0].children[0].textContent, 'Alex (this device)');
  assert.equal(rows[1].children[0].children[0].textContent, 'Alex');
  assert.equal(rows[1].children[0].children[2].textContent, 'peer-a');
  assert.equal(rows[1].children[0].children[3].textContent, 'Locally pinned · identity not verified');
  assert.equal(rows[2].children[0].children[3].textContent, 'Unchecked key');
  assert.equal(rows[2].children[0].children[0].textContent, '<b>Alex</b>');
  assert.equal(rows[2].children[0].children[0].children.length, 0);
  const value = rows[1].children[1].children[3].children[0];
  assert.equal(value.textContent, 'ab:'.repeat(31) + 'cd');
  assert.equal(value.tabIndex, 0);
  assert.equal(rows[1].children[1].children[1].children[0].textContent, 'peer-a');
  assert.equal(rows[2].children[1].children[3].children[1].disabled, true);
  assert.doesNotMatch(app.nodes.get('#participant-keys').textContent, /not-for-display|never-display/);
  assert.equal(app.nodes.get('#conversation-title').textContent, '<script>chat</script>');
});

test('copy actions use the complete public ID and fingerprint and provide accessible feedback', async () => {
  const app = client();
  const fields = app.nodes.get('#participant-keys').children[1].children[1].children;
  await fields[1].children[1].emit('click');
  await fields[3].children[1].emit('click');
  assert.deepEqual(app.copied, ['peer-a', 'ab:'.repeat(31) + 'cd']);
  assert.equal(app.nodes.get('#details-copy-status').textContent, 'Fingerprint copied.');
  assert.equal(app.nodes.get('#details-copy-status').hidden, false);
});

test('presence shows accurate counts and discreet participant states, then clears on disconnect', () => {
  const app = client();
  const header = app.nodes.get('#participant-count');
  const states = () => app.nodes.get('#participant-keys').children.map(row => row.children[0].children[1]);
  assert.match(header.textContent, /Presence unknown/);
  assert.equal(states()[0].textContent, 'Presence unknown');
  app.run(`receivePresence({ type: 'presence', valid_for_ms: 35000, chats: [
    { chat_id: 7, participants: ['me', 'peer-a', 'peer-b'], online: ['me', 'peer-a'] }
  ] });`);
  assert.equal(header.textContent, '2 of 3 online');
  assert.equal(states()[1].textContent, 'Online');
  assert.equal(states()[1].className, 'participant-presence online');
  assert.equal(states()[2].textContent, 'Offline');
  app.run('updateConnectionState(false);');
  assert.match(header.textContent, /Presence unknown/);
  assert.equal(states()[2].textContent, 'Presence unknown');
});

test('presence leases expire even when background timers pause, and reconnect requires a fresh snapshot', () => {
  const app = client();
  const snapshot = `receivePresence({valid_for_ms: 35000, chats: [
    {chat_id: 7, participants: ['me', 'peer-a', 'peer-b'], online: ['peer-b']}
  ]});`;
  app.run(snapshot);
  assert.equal(app.nodes.get('#participant-count').textContent, '1 of 3 online');
  app.window.now = 35001;
  app.run('renderPresence();');
  assert.match(app.nodes.get('#participant-count').textContent, /Presence unknown/);
  app.run('clearPresence(); updateConnectionState(true);');
  assert.match(app.nodes.get('#participant-count').textContent, /Presence unknown/);
  app.run(snapshot);
  assert.equal(app.nodes.get('#participant-count').textContent, '1 of 3 online');
  app.timers.at(-1)();
  assert.match(app.nodes.get('#participant-count').textContent, /Presence unknown/);
});

test('presence is unknown for a new roster member, missing chat, or malformed snapshot', () => {
  const app = client();
  app.run(`receivePresence({valid_for_ms: 35000, chats: [
    {chat_id: 7, participants: ['me', 'peer-a'], online: ['me']}
  ]});`);
  assert.match(app.nodes.get('#participant-count').textContent, /Presence unknown/);
  assert.equal(app.run("participantPresence(7, 'peer-a')"), 'offline');
  assert.equal(app.run("participantPresence(7, 'peer-b')"), 'unknown');
  assert.equal(app.run("participantPresence(8, 'me')"), 'unknown');
  app.run(`receivePresence({valid_for_ms: 35000, chats: [
    {chat_id: 7, participants: ['me', 'peer-a', 'peer-b', 'new-peer'], online: ['me']}
  ]});`);
  assert.match(app.nodes.get('#participant-count').textContent, /Presence unknown/);
  app.run('receivePresence({valid_for_ms: 35000, chats: []});');
  assert.equal(app.run("participantPresence(7, 'me')"), 'unknown');
  app.run(`receivePresence({valid_for_ms: 999999, chats: [
    {chat_id: 7, participants: ['me'], online: ['me']}
  ]});`);
  assert.equal(app.run('presenceByChat.size'), 0);
  app.run(`receivePresence({valid_for_ms: 35000, chats: [
    {chat_id: 7, participants: ['me'], online: ['outsider']}
  ]});`);
  assert.equal(app.run('presenceByChat.size'), 0);
  app.run(`navigator.onLine = false; receivePresence({valid_for_ms: 35000, chats: [
    {chat_id: 7, participants: ['me'], online: ['me']}
  ]});`);
  assert.equal(app.run('presenceByChat.size'), 0);
});

test('WebSocket presence bypasses blocked delivery work and ignores replaced connections', () => {
  const app = client();
  app.run(`globalThis.WebSocket = class {
    static OPEN = 1;
    constructor() { this.readyState = 1; this.events = new Map(); this.sent = []; }
    addEventListener(name, handler) {
      if (!this.events.has(name)) this.events.set(name, []);
      this.events.get(name).push(handler);
    }
    emit(name, event) { for (const handler of this.events.get(name) || []) handler(event); }
    send(value) { this.sent.push(value); }
    close() { this.readyState = 3; this.emit('close'); }
  };
  window.location.protocol = 'https:'; window.location.host = 'test';
  connectSocket(); socket.emit('open');
  globalThis.originalSocket = socket;
  inboundQueue = new Promise(() => {});
  globalThis.livePresence = JSON.stringify({type: 'presence', valid_for_ms: 35000,
    chats: [{chat_id: 7, participants: ['me', 'peer-a', 'peer-b'], online: ['me']}]});
  socket.emit('message', {data: livePresence});`);
  assert.equal(app.nodes.get('#participant-count').textContent, '1 of 3 online');
  assert.equal(app.run('JSON.parse(socket.sent[0]).presence'), true);
  app.run(`socket = null; connectSocket();
    originalSocket.emit('message', {data: livePresence});`);
  assert.match(app.nodes.get('#participant-count').textContent, /Presence unknown/);
  app.run(`socket.emit('message', {data: livePresence});
    originalSocket.emit('close');`);
  assert.equal(app.nodes.get('#participant-count').textContent, '1 of 3 online');
  app.run(`socket.emit('message', {data: JSON.stringify({type: 'auth_error'})});`);
  assert.match(app.nodes.get('#participant-count').textContent, /Presence unknown/);
});

test('blocked clipboard selects the full value for manual copying', async () => {
  const app = client({ clipboardBlocked: true });
  const field = app.nodes.get('#participant-keys').children[1].children[1].children[3];
  await field.children[1].emit('click');
  assert.equal(app.selection.value, 'ab:'.repeat(31) + 'cd');
  assert.equal(app.document.activeElement, field.children[0]);
  assert.match(app.nodes.get('#details-copy-status').textContent, /copy it manually/);
});

test('roster refresh preserves expanded participant nodes, focus and updated key states', () => {
  const app = client();
  const peer = app.nodes.get('#participant-keys').children[1];
  peer.open = true;
  peer.children[1].children[3].children[1].focus();
  const focused = app.document.activeElement;
  app.run(`chats[0].participants[1].key_changed = true;
    chats[0].participants[1].local_fingerprint = 'ff:'.repeat(31) + 'ff'; selectChat(7);`);
  assert.equal(app.nodes.get('#participant-keys').children[1], peer);
  assert.equal(peer.open, true);
  assert.equal(app.document.activeElement, focused);
  assert.equal(peer.children[0].children[3].textContent, 'KEY CHANGED; blocked');
  assert.equal(peer.children[1].children[3].children[0].textContent, 'ff:'.repeat(31) + 'ff');
});

test('changed-key warnings and blocked sending remain visible with the details dialog closed', () => {
  const app = client();
  app.run('chats[0].participants[1].key_changed = true; selectChat(7);');
  assert.equal(Boolean(app.nodes.get('#conversation-details').open), false);
  assert.equal(app.nodes.get('#key-change-warning').hidden, false);
  assert.match(app.nodes.get('#key-change-warning').textContent, /Sending and decryption are blocked/);
  assert.equal(app.nodes.get('#message-input').disabled, true);
  assert.equal(app.nodes.get('#send-button').disabled, true);
  app.run('selectChat(null);');
  assert.equal(app.nodes.get('#key-change-warning').hidden, true);
  assert.equal(app.nodes.get('#participant-keys').children.length, 0);
  assert.equal(app.nodes.get('#conversation-details-button').disabled, true);
});

test('a key change during an outgoing attempt keeps the composer blocked after failure', async () => {
  const app = client();
  app.nodes.get('#message-input').value = 'draft';
  app.run(`encryptForRecipient = async () => {
    chats[0].participants[1].key_changed = true;
    selectChat(7);
    throw new ClientError("A participant's encryption key changed and no longer matches the fingerprint saved on this device. Sending and decryption are blocked. Keep this device's history.");
  };`);
  await app.nodes.get('#message-form').emit('submit', { preventDefault() {} });
  assert.equal(app.nodes.get('#send-button').disabled, true);
  assert.equal(app.nodes.get('#message-input').disabled, true);
  assert.equal(app.nodes.get('#message-input').value, 'draft');
  assert.equal(app.nodes.get('#key-change-warning').hidden, false);
});

test('dialogs open modally and restore trigger focus without scrolling', () => {
  const app = client();
  for (const [button, dialog, close] of [
    ['#device-settings-button', '#device-settings', '#close-device-settings'],
    ['#conversation-details-button', '#conversation-details', '#close-conversation-details'],
  ]) {
    app.nodes.get(button).emit('click');
    assert.equal(app.nodes.get(dialog).open, true);
    app.nodes.get(close).emit('click');
    assert.equal(app.nodes.get(dialog).open, false);
    assert.equal(app.document.activeElement, app.nodes.get(button));
    assert.equal(app.document.activeElement.focusOptions.preventScroll, true);
  }
});

test('mobile chat selection, back navigation and polling preserve the selected view and draft', () => {
  const app = client({ mobile: true });
  const input = app.nodes.get('#message-input');
  input.value = 'unfinished\nmessage';
  assert.notEqual(app.document.activeElement, input);
  app.nodes.get('#chat-list').children[0].children[0].emit('click');
  assert.equal(app.nodes.get('#client-panel').dataset.mobileView, 'conversation');
  assert.equal(app.document.activeElement, input);
  assert.equal(input.focusOptions.preventScroll, true);
  app.run('selectChat(7);');
  assert.equal(app.nodes.get('#client-panel').dataset.mobileView, 'conversation');
  app.nodes.get('#back-to-chats').emit('click');
  assert.equal(app.nodes.get('#client-panel').dataset.mobileView, 'chats');
  assert.equal(app.document.activeElement.attributes.get('aria-current'), 'true');
  assert.equal(input.value, 'unfinished\nmessage');
  app.run('selectChat(7);');
  assert.equal(app.nodes.get('#client-panel').dataset.mobileView, 'chats');
});

test('Shift+Enter sends once and other Enter combinations preserve input', () => {
  const app = client();
  const input = app.nodes.get('#message-input');
  const form = app.nodes.get('#message-form');
  for (const extra of [{ shiftKey: false }, { shiftKey: true, isComposing: true },
    { shiftKey: true, ctrlKey: true }, { shiftKey: true, repeat: true }]) {
    input.emit('keydown', { key: 'Enter', preventDefault() {}, ...extra });
    assert.equal(form.submittedWith, undefined);
  }
  input.emit('keydown', { key: 'Enter', shiftKey: true, preventDefault() {} });
  assert.equal(form.submittedWith, app.nodes.get('#send-button'));
});

test('connection and device settings retain complete public ID without displaying credentials', () => {
  const app = client();
  app.run('updateConnectionState(true);');
  assert.equal(app.nodes.get('#connection-label').textContent, 'Live');
  assert.equal(app.nodes.get('#device-public-id').textContent, 'me');
  assert.equal(app.nodes.get('#identity-label').textContent, 'Device me · Live');
  app.run('updateConnectionState(false);');
  assert.equal(app.nodes.get('#connection-label').textContent, 'Polling');
  app.run('invitePending = true; updateConnectionState(false);');
  assert.equal(app.nodes.get('#connection-label').textContent, 'Offline');
  assert.doesNotMatch(app.nodes.get('#identity-label').textContent, /not-for-display|never-display/);
});

test('existing mapped errors remain visible and announced in the workspace and open modal', () => {
  const app = client();
  app.nodes.get('#device-settings-button').emit('click');
  app.run(`showToast(errorMessage(requestError(403, 'The room phrase or password is incorrect.', 'Could not join the room')), true);`);
  const expected = 'Could not join the room (HTTP 403). The room password does not match, or a required password was left empty.';
  assert.equal(app.nodes.get('#error-message').textContent, expected);
  assert.equal(app.nodes.get('#error-message').hidden, false);
  assert.equal(app.nodes.get('#device-error').textContent, expected);
  assert.equal(app.nodes.get('#device-error').hidden, false);
  app.timers[0]();
  assert.equal(app.nodes.get('#device-error').hidden, true);
  app.run('showNameError();');
  assert.equal(app.nodes.get('#display-name-error').textContent, 'Enter your name (1–64 characters).');
  assert.equal(app.nodes.get('#display-name').attributes.get('aria-invalid'), 'true');
  app.run(`showStartupError(new DeviceStorageError());`);
  assert.equal(app.nodes.get('#activation-error').hidden, false);
  assert.match(app.nodes.get('#activation-error').textContent, /Keep existing device data; do not use Reset device/);
});

test('pending retries retain encrypted records, expiry labels and unchanged discard safeguards', async () => {
  const app = client({ mobile: true });
  await app.run(`(async () => {
    identity.storageKey = await crypto.subtle.generateKey({ name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt']);
    globalThis.records = [];
    putDatabaseValue = async (key, value) => records.push({key, value});
    readHistoryRecords = async () => records;
    await persistOutbox({chatId: 7, messageId: 'saved', plaintext: 'pending text', expiresAt: Date.now() + 60000});
    await persistOutbox({chatId: 7, messageId: 'expired', plaintext: 'expired text', expiresAt: 0});
    await renderOutbox();
  })()`);
  const list = app.nodes.get('#pending-send-list');
  assert.equal(app.nodes.get('#pending-sends').hidden, false);
  assert.match(list.children[0].textContent, /Pending: pending text/);
  assert.match(list.children[1].textContent, /Retry expired: expired text/);
  assert.equal(app.run('JSON.stringify(records).includes("pending text")'), false);
  await list.children[0].children[0].emit('click');
  assert.equal(app.confirmations[0], 'Discard this saved retry? The message may already have reached the server. This cannot undo delivery.');
  assert.equal(app.run('records.length'), 2);
  app.window.confirm = () => true;
  app.run(`deviceLock = async (_, action) => action(); removeOutbox = async key => { records = records.filter(record => record.key !== key); };`);
  await list.children[0].children[0].emit('click');
  assert.equal(app.run('records.length'), 1);
  app.run('globalThis.retried = false; synchronize = () => { retried = true; };');
  app.nodes.get('#retry-pending-sends').emit('click');
  assert.equal(app.run('retried'), true);
});

test('device reset keeps its original confirmation and acts only after acceptance', async () => {
  const app = client();
  app.run('globalThis.cleared = false; clearIdentity = async () => { cleared = true; };');
  await app.nodes.get('#reset-button').emit('click');
  assert.equal(app.confirmations[0], "Remove this browser's private key and session? Existing chats cannot be recovered on this device.");
  assert.equal(app.run('cleared'), false);
  app.window.confirm = () => true;
  await app.nodes.get('#reset-button').emit('click');
  assert.equal(app.run('cleared'), true);
  assert.equal(app.window.destination, '/client');
});

for (const mobile of [false, true]) {
  test(`compact ${mobile ? 'mobile' : 'desktop'} messages preserve short, multiline and long content with metadata outside bubbles`, async () => {
    const app = client({mobile});
    if (mobile) app.nodes.get('#chat-list').children[0].children[0].emit('click');
    const texts = ['Hi', 'First line\nSecond line', 'longword'.repeat(100) + '\n' + 'word '.repeat(100)];
    const entries = texts.flatMap((text, index) => ['theirs', 'mine'].map((kind, side) => ({
      id: `${kind}:layout-${index}`, kind, text, senderPublicId: kind === 'mine' ? 'me' : 'peer-b',
      createdAt: Date.UTC(2026,9,7,12) + index * 60000 + side * 10000,
    })));
    await app.run(`persistHistoryEntry = async () => {};
      messagesByChat.set(7, ${JSON.stringify(entries)}); renderMessages();`);
    const rows = app.nodes.get('#message-list').children;
    assert.equal(rows.length, entries.length);
    for (const [index, row] of rows.entries()) {
      const outgoing = entries[index].kind === 'mine';
      assert.deepEqual(row.children, [row.messageHeader, row.messageBubble]);
      assert.deepEqual(row.messageHeader.children, [row.messageHeader.sender, row.messageHeader.dot, row.messageHeader.timestamp]);
      assert.equal(row.messageHeader.timestamp.tagName, 'time');
      assert.equal(row.messageHeader.dot.textContent, '·');
      assert.doesNotMatch(row.textContent, /peer-b|public_id|layout-/);
      assert.equal(row.messageContent.textContent, entries[index].text);
      assert.equal(row.messageContent.children.length, 0);
      assert.equal(row.messageHeader.sender.children.length, 0);
      assert.equal(row.messageHeader.sender.textContent, outgoing ? 'Alex' : '<b>Alex</b>');
      assert.ok(row.className.split(' ').includes(entries[index].kind));
      if (outgoing) {
        assert.deepEqual(row.messageBubble.children, [row.messageContent, row.messageFooter]);
        assert.equal(row.messageFooter.indicator.parent, row.messageFooter);
        row.messageFooter.indicator.emit('click', {stopPropagation(){}});
        assert.equal(app.nodes.get('#message-details').open, true);
        app.nodes.get('#message-details').close();
        assert.equal(app.document.activeElement, row.messageFooter.indicator);
      } else {
        assert.deepEqual(row.messageBubble.children, [row.messageContent]);
        assert.equal(row.messageFooter, undefined);
      }
    }
  });
}

test('consecutive sender grouping respects identity, elapsed time, and intervening messages', async () => {
  const app = client();
  await app.run(`persistHistoryEntry=async()=>{};
    globalThis.base=Date.UTC(2026,9,7,12);
    messagesByChat.set(7,[
      {id:'a',kind:'theirs',senderPublicId:'peer-a',text:'first',createdAt:base},
      {id:'b',kind:'theirs',senderPublicId:'peer-a',text:'same minute',createdAt:base+10000},
      {id:'c',kind:'theirs',senderPublicId:'peer-a',text:'next minute',createdAt:base+60000},
      {id:'d',kind:'mine',text:'same name, other identity',createdAt:base+70000},
      {id:'e',kind:'theirs',senderPublicId:'peer-a',text:'interrupted',createdAt:base+80000},
      {id:'f',kind:'theirs',senderPublicId:'peer-a',text:'later',createdAt:base+400000},
      {id:'g',kind:'theirs',text:'unknown identity',createdAt:base+410000},
      {id:'h',kind:'theirs',text:'also unknown',createdAt:base+420000}
    ]); renderMessages();`);
  const rows=app.nodes.get('#message-list').children;
  assert.equal(rows[1].messageHeader.hidden, true);
  assert.match(rows[1].className, /continuation/);
  assert.equal(rows[2].messageHeader.hidden, false);
  assert.equal(rows[2].messageHeader.sender.hidden, true);
  assert.equal(rows[2].messageHeader.dot.hidden, true);
  for (const index of [0,3,4,5,6,7]) {
    assert.doesNotMatch(rows[index].className, /continuation/);
    assert.equal(rows[index].messageHeader.sender.hidden, false);
  }
  assert.equal(rows[6].messageHeader.sender.textContent, 'Participant');
});

test('late messages recompute adjacent groups without replacing selected content or changing UTC order', async () => {
  const app=client();
  await app.run(`persistHistoryEntry=async()=>{};globalThis.base=Date.UTC(2026,9,7,12);
    messagesByChat.set(7,[
      {id:'a',kind:'theirs',senderPublicId:'peer-a',text:'first',createdAt:base},
      {id:'c',kind:'theirs',senderPublicId:'peer-a',text:'third',createdAt:base+20000}
    ]);renderMessages();`);
  const list=app.nodes.get('#message-list');
  const original=list.children[1]; const content=original.messageContent;
  assert.equal(original.messageHeader.hidden,true);
  await app.run(`appendMessage(7,{id:'b',kind:'theirs',senderPublicId:'peer-b',text:'late second',createdAt:base+10000},false)`);
  assert.deepEqual(list.children.map(row=>row.messageContent.textContent),['first','late second','third']);
  assert.equal(list.children[2],original);
  assert.equal(original.messageContent,content);
  assert.equal(original.messageHeader.hidden,false);
  assert.equal(original.messageHeader.sender.hidden,false);
});

test('unnamed and legacy participants keep readable names and hide routing identifiers in the timeline', async () => {
  const app=client();
  await app.run(`persistHistoryEntry=async()=>{};
    chats[0].participants[1].display_name=null;
    messagesByChat.set(7,[
      {id:'incoming:'+JSON.stringify([7,'peer-a','old']),kind:'theirs',text:'unnamed',meta:'From peer-a',createdAt:1000},
      {id:'incoming:'+JSON.stringify([7,'former-id','old']),kind:'theirs',text:'saved',meta:'From Former Name (former-id)',createdAt:2000},
      {id:'outgoing:old',kind:'mine',senderPublicId:'former-me',text:'saved reply',meta:'Sent by Former Me',createdAt:3000}
    ]); renderMessages();`);
  const rows=app.nodes.get('#message-list').children;
  assert.deepEqual(rows.map(row=>row.messageHeader.sender.textContent),['Participant','Former Name','Former Me']);
  assert.doesNotMatch(rows.map(row=>row.textContent).join(''), /peer-a|former-id|former-me/);
  app.run(`chats[0].participants[0].display_name=' '; messagesByChat.set(7,[{id:'outgoing:empty',kind:'mine',text:'no name',createdAt:4000}]);renderMessages();`);
  assert.equal(app.nodes.get('#message-list').firstChild.messageHeader.sender.textContent,'You');
});

for (const [locale,timeZone] of [['en-US','America/Los_Angeles'],['de-DE','Europe/Berlin']]) {
  test(`message headers use browser ${locale} local time and preserve legacy UTC timestamps`, async () => {
    const app=client({locale,timeZone});
    await app.run(`persistHistoryEntry=async()=>{};
      messagesByChat.set(7,[{id:'utc',kind:'theirs',senderPublicId:'peer-a',text:'UTC instant',createdAt:Date.parse('2026-10-08T00:32:43Z')}]);renderMessages();`);
    const time=app.nodes.get('#message-list').firstChild.messageHeader.timestamp;
    const instant=new Date('2026-10-08T00:32:43Z');
    assert.equal(time.textContent,new Intl.DateTimeFormat(locale,{timeZone,hour:'2-digit',minute:'2-digit'}).format(instant));
    assert.equal(time.title,new Intl.DateTimeFormat(locale,{timeZone,dateStyle:'short',timeStyle:'medium'}).format(instant));
    assert.equal(time.dateTime,instant.toISOString());
    assert.equal(app.run(`serverTimestamp('2026-10-08T00:32:43')`),instant.getTime());
    for (const iso of ['2026-03-08T09:30:00Z','2026-03-08T10:30:00Z']) {
      assert.equal(app.run(`displayTimestamp(Date.parse('${iso}'))`),
        new Intl.DateTimeFormat(locale,{timeZone,hour:'2-digit',minute:'2-digit'}).format(new Date(iso)));
    }
    assert.equal(app.run('displayTimestamp(0,true)'), 'Time unavailable');
  });
}

for (const [dialogId,buttonId] of [['#conversation-details','#conversation-details-button'],['#device-settings','#device-settings-button']]) {
  test(`${dialogId} closes on one backdrop click or native Escape and restores focus`, () => {
    const app=client(); const button=app.nodes.get(buttonId); const dialog=app.nodes.get(dialogId);
    button.emit('click'); assert.equal(dialog.open,true);
    dialog.emit('click',{target:dialog,clientX:50,clientY:50}); assert.equal(dialog.open,true);
    dialog.emit('click',{target:dialog,clientX:200,clientY:50}); assert.equal(dialog.open,false);
    assert.equal(app.document.activeElement,button);
    button.emit('click'); dialog.emit('cancel'); assert.equal(dialog.open,false);
    assert.equal(app.document.activeElement,button);
  });
}
