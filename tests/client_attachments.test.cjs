const { test } = require('node:test');
const assert = require('node:assert/strict');
const { client } = require('./client_dom.cjs');

function protocol() {
  const app = client();
  return { app, p: app.run('SidewordProtocol') };
}

test('versioned text is deterministic even when the user types attachment-shaped JSON', () => {
  const { p } = protocol();
  const text = '{"v":1,"type":"attachment","name":"example"}';
  assert.equal(p.decodePayload(p.encodePayload(p.textPayload(text))).text, text);
  assert.throws(() => p.decodePayload(text));
  assert.throws(() => p.decodePayload('plain text'));
  assert.throws(() => p.textPayload('x'.repeat(16001)));
  assert.throws(() => p.textPayload('🌍'.repeat(4001)));
  assert.equal(p.textPayload('🌍'.repeat(4000)).text.length, 8000);
});

test('file payloads preserve exact bytes at zero, binary and 1 MiB boundaries', () => {
  const { p } = protocol();
  for (const size of [0, 257, 1048576]) {
    const bytes = Uint8Array.from({length: size}, (_, i) => i % 256);
    const payload = p.decodePayload(p.encodePayload(p.filePayload('fixture.bin', '', bytes)));
    assert.deepEqual(Buffer.from(p.attachmentBytes(payload)), Buffer.from(bytes));
  }
  assert.throws(() => p.filePayload('large.bin', '', new Uint8Array(1048577)), /1 MiB/);
});

test('invalid types, fields, versions, lengths and noncanonical base64 fail closed', () => {
  const { p } = protocol();
  const good = p.filePayload('fixture.bin', 'application/octet-stream', new Uint8Array([255]));
  for (const update of [ {v: 2}, {type: 'image'}, {name: ''}, {name: 'x'.repeat(256)},
    {mime: '\ntext/html'}, {mime: 'x'.repeat(128)}, {size: -1}, {size: 0}, {size: 1.5},
    {size: '1'}, {size: 1048577}, {data: '_w=='}, {data: '/x=='}, {data: '/w'},
    {data: ' /w=='}, {data: '!!!!'}, {data: null}, {extra: true} ]) {
    assert.throws(() => p.decodePayload(JSON.stringify({...good, ...update})), JSON.stringify(update));
  }
  for (const value of [null, [], 1, {}, {v:1,type:'text',text:17}, {v:1,type:'text',text:''}]) {
    assert.throws(() => p.decodePayload(JSON.stringify(value)));
  }
  assert.throws(() => p.decodePayload(' '.repeat(1400001)));
});

async function devices(app) {
  await app.run(`(async () => {
    globalThis.devices = [];
    for (let i = 0; i < 3; i++) {
      const value = await generateIdentity(); value.publicId = 'fixture-' + i; devices.push(value);
    }
    identity = devices[0];
    chats = [{id:7, participants: devices.map(value => ({public_id:value.publicId, public_key:value.publicKey}))}];
    observePeerKey = async peer => ({...peer, key_changed:false});
  })()`);
}

test('real per-recipient encryption preserves attachments, isolates recipients and rejects tampering', async () => {
  const { app } = protocol();
  await devices(app);
  await app.run(`(async () => {
    globalThis.payload = SidewordProtocol.filePayload('synthetic-secret.bin', 'text/html',
      Uint8Array.from({length:1048576}, (_, i) => i % 256));
    globalThis.wires = [];
    for (const peer of chats[0].participants.slice(1)) wires.push(await encryptForRecipient(
      SidewordProtocol.encodePayload(payload), 7, 'fixture', peer));
    identity = devices[1];
    globalThis.message = {chat_id:7, client_message_id:'fixture', sender_public_id:devices[0].publicId, ciphertext:wires[0]};
  })()`);
  const original = app.run('payload.data');
  assert.equal(await app.run('decryptMessage(message).then(SidewordProtocol.decodePayload).then(p => p.data)'), original);
  assert.equal(app.run('wires[0].includes("synthetic-secret.bin")'), false);
  assert.notEqual(app.run('wires[0]'), app.run('wires[1]'));
  await assert.rejects(app.run('decryptMessage({...message, ciphertext:wires[1]})'));
  await assert.rejects(app.run('decryptMessage({...message, chat_id:8})'));
  await assert.rejects(app.run('decryptMessage({...message, client_message_id:"changed"})'));
  await app.run(`globalThis.envelope = JSON.parse(decoder.decode(SidewordProtocol.base64ToBytes(wires[0])));
    const ct = SidewordProtocol.base64ToBytes(envelope.ct); ct[0] ^= 1;
    envelope.ct = SidewordProtocol.bytesToBase64(ct);`);
  await assert.rejects(app.run(`decryptMessage({...message, ciphertext:SidewordProtocol.bytesToBase64(encoder.encode(JSON.stringify(envelope)))})`));
  app.run('identity = devices[2];');
  assert.equal(await app.run('decryptMessage({...message,ciphertext:wires[1]}).then(SidewordProtocol.decodePayload).then(p => p.data)'), original);
});

test('worst-case metadata fits complete wire budgets; full-size group fanout is four peers', async () => {
  const { app, p } = protocol();
  await devices(app);
  const wire = await app.run(`encryptForRecipient(SidewordProtocol.encodePayload(
    SidewordProtocol.filePayload('\\u0000'.repeat(255), '"'.repeat(127), new Uint8Array(1048576))),
    7, 'fixture', chats[0].participants[1])`);
  assert.equal(Buffer.from(wire, 'base64').length, 1866845);
  assert.equal(wire.length, 2489128);
  assert.ok(p.attachmentSendBytes(1048576,4) < p.MAX_SEND_BYTES);
  assert.ok(p.attachmentSendBytes(1048576,5) > p.MAX_SEND_BYTES);
});

async function storage(app) {
  await app.run(`(async () => {
    globalThis.records = new Map(); globalThis.uploads = [];
    identity = devices[1]; identity.token = 'synthetic'; identity.retryWindowSeconds = 3600;
    messageProfileReady = true;
    renderMessages = () => {}; refreshVisibleReads = () => {};
    putDatabaseValue = async (key, value, removeKey) => {
      if (!removeKey || !records.has(key)) records.set(key,value);
      if (removeKey) records.delete(removeKey);
    };
    readHistoryRecords = async prefix => [...records].filter(([key]) => key.startsWith(prefix || 'history:'))
      .map(([key,value]) => ({key,value}));
    deviceLock = async (_, action) => action();
    removeOutbox = async key => records.delete(key);
    api = async (path, options) => { uploads.push({path, body:JSON.parse(options.body)});
      return {recipients:[],deliveries:{},created_at:'2026-10-28T00:00:00Z'}; };
  })()`);
}

test('serialized WS/poll metadata duplicates persist before one deletion ACK; reload retains the reference', async () => {
  const { app } = protocol();
  await devices(app);
  await app.run(`(async () => { globalThis.wire = await encryptForRecipient(SidewordProtocol.encodePayload(
    SidewordProtocol.fileReference('private-name.bin', '', 3, 'one-file.media')),
    7, 'one-file', chats[0].participants[1]); })()`);
  await storage(app);
  app.run(`globalThis.message = {delivery_id:'a'.repeat(32),chat_id:7,client_message_id:'one-file',
    sender_public_id:devices[0].publicId,ciphertext:wire,created_at:'2026-10-28T00:00:00Z'};`);
  await app.run('Promise.all([enqueueIncoming(() => processIncoming({messages:[message]})), enqueueIncoming(() => processIncoming({messages:[message]}))])');
  assert.equal(app.run('messagesByChat.get(7).filter(e => e.kind === "theirs").length'), 1);
  assert.equal(app.run('uploads.filter(x => x.path === "/api/v1/ack/exact").length'), 1);
  assert.equal(app.run('pendingReadsByChat.size'), 0);
  assert.equal(app.run('JSON.stringify([...records]).includes("private-name.bin")'), false);
  assert.equal(app.run('[...records.values()].every(v => Object.prototype.toString.call(v.ciphertext) === "[object ArrayBuffer]")'), true);
  app.run('messagesByChat.clear(); seenMessageIds.clear(); loadedHistoryKeys.clear();');
  await app.run('loadStoredHistory()');
  assert.equal(app.run('messagesByChat.get(7).find(e => e.attachment).attachment.media_id'), 'one-file.media');
});

test('quota failure and invalid payload never ACK; valid storage retry remains possible', async () => {
  const { app } = protocol();
  await devices(app); await storage(app);
  app.run(`globalThis.message = {delivery_id:'a'.repeat(32),chat_id:7,client_message_id:'one',sender_public_id:devices[0].publicId};
    decryptMessage = async () => SidewordProtocol.encodePayload(SidewordProtocol.fileReference('empty.bin','',0,'one.media'));
    globalThis.save = putDatabaseValue;
    putDatabaseValue = async () => { throw new DOMException('synthetic quota', 'QuotaExceededError'); };`);
  await app.run('processIncoming({messages:[message]})');
  assert.equal(app.run('uploads.length'), 0);
  assert.equal(app.run('seenMessageIds.size'), 0);
  app.run('putDatabaseValue = save;');
  await app.run('processIncoming({messages:[message]})');
  assert.equal(app.run('uploads.length'), 1);
  app.run(`globalThis.decrypts = 0; decryptMessage = async () => { decrypts++; return '{"v":2,"type":"attachment"}'; };
    message = {...message, delivery_id:'b'.repeat(32),client_message_id:'invalid'};`);
  await app.run('processIncoming({messages:[message]});');
  await app.run('processIncoming({messages:[message]});');
  assert.equal(app.run('uploads.length'), 1);
  assert.equal(app.run('decrypts'), 1);
});

test('encrypted immutable attachment outbox retries after reload and response loss without duplicate bubbles', async () => {
  const { app } = protocol();
  await devices(app); await storage(app);
  await app.run(`(async () => {
    await persistOutbox({chatId:7,messageId:'retry-file',createdAt:Date.now(),expiresAt:Date.now()+60000,
      payload:SidewordProtocol.fileReference('retry.bin','',3,'retry-file.media'),
      media:SidewordProtocol.filePayload('retry.bin','',new Uint8Array([7,8,9])),
      envelopes:[{recipient_public_id:'peer',ciphertext:'immutable-envelope'}]});
    globalThis.accept = api;
    api = async (path,options) => { await accept(path,options); throw new Error('response lost'); };
  })()`);
  await assert.rejects(app.run('flushOutbox()'), /response lost/);
  app.run('messagesByChat.clear(); loadedHistoryKeys.clear(); api = accept;');
  await app.run('loadStoredHistory();');
  await app.run('flushOutbox();');
  assert.equal(app.run('JSON.stringify(uploads[0]) === JSON.stringify(uploads[1])'), true);
  assert.equal(app.run('messagesByChat.get(7).filter(e => e.attachment).length'), 1);
  assert.equal(app.run('[...records.keys()].some(k => k.startsWith("outbox:"))'), false);
});

test('selection rejects oversize before reading; cancel preserves draft; changed peer blocks sending', async () => {
  const { app } = protocol();
  app.run(`globalThis.reads = 0; globalThis.file = {name:'test.bin', type:'', size:1048577,
    arrayBuffer: async () => { reads++; return new ArrayBuffer(0); }};`);
  assert.throws(() => app.run('selectAttachment(file)'), /1 MiB/);
  assert.equal(app.run('reads'), 0);
  app.run('file.size=0; selectAttachment(file); elements.messageInput.value="keep draft";');
  app.nodes.get('#attachment-cancel').emit('click');
  assert.equal(app.run('selectedFile'), null);
  assert.equal(app.nodes.get('#message-input').value, 'keep draft');
  await devices(app);
  app.run('selectedChatId=7; selectAttachment(file); observePeerKey = async peer => ({...peer,key_changed:true});');
  await app.nodes.get('#message-form').emit('submit', {preventDefault(){}});
  assert.equal(app.run('reads'), 0);
  assert.match(app.nodes.get('#composer-error').textContent, /key changed/);
});

test('safe card and explicit generic Blob download never embed content; URLs are cleaned up', async () => {
  const { app, p } = protocol();
  const name = '../\\evil\u202e\u0000<script>.html';
  assert.equal(p.safeDownloadName(name), 'evil<script>.html');
  const payload = p.filePayload(name, 'text/html', new Uint8Array([60,62]));
  app.run('value => { globalThis.fixture = value; }')(payload);
  await app.run('appendMessage(7,{id:"file",kind:"theirs",attachment:fixture},false)');
  const card = app.nodes.get('#message-list').children[0].messageBubble;
  const info = card.children.find(node => node.className === 'attachment-info');
  const download = card.children.find(node => node.tagName === 'button');
  const error = card.children.find(node => node.className === 'field-error');
  assert.equal(info.children[0].children.length, 0);
  assert.equal(info.children[0].textContent, 'File · 2 B');
  assert.equal(info.children[1].textContent, 'Saved on this device');
  assert.equal(download.textContent, 'Download');
  assert.equal(download.attributes.get('aria-label'), 'Download attachment');
  app.run(`globalThis.blobs = []; globalThis.revoked = []; globalThis.clicks = [];
    globalThis.Blob = class { constructor(bytes,options) { this.bytes=bytes; this.type=options.type; } };
    globalThis.URL = {createObjectURL(blob) { blobs.push(blob); return 'blob:synthetic'; }, revokeObjectURL(url) { revoked.push(url); }};`);
  app.document.body = app.document.createElement('body');
  const create = app.document.createElement;
  app.document.createElement = tag => {
    const node = create(tag);
    node.click = () => app.run('value => clicks.push(value)')({href:node.href,download:node.download});
    return node;
  };
  assert.equal(app.run('blobs.length'), 0);
  download.emit('click', {stopPropagation(){}});
  assert.equal(app.run('blobs[0].type'), 'application/octet-stream');
  assert.equal(app.run('clicks[0].download'), 'evil<script>.html');
  assert.equal(app.run('downloadUrls.size'), 1);
  assert.equal(app.document.body.children.length, 0);
  app.windowEvents.get('pagehide')();
  assert.equal(app.run('revoked.length'), 1);
  assert.equal(app.run('downloadUrls.size'), 0);
  app.run('URL.createObjectURL = () => {throw new Error("synthetic");};');
  download.emit('click', {stopPropagation(){}});
  assert.match(error.textContent, /could not be downloaded/);
});

test('send persists the encrypted file before upload; quota failure retains selection and prevents upload', async () => {
  for (const quota of [false, true]) {
    const { app } = protocol();
    await devices(app); await storage(app);
    app.run(`selectedChatId=7; elements.messageInput.value='keep this text draft';
      globalThis.file = {name:'empty.bin',type:'',size:0,arrayBuffer:async () => new ArrayBuffer(0)};
      selectAttachment(file);
      globalThis.events=[];
      const save = putDatabaseValue;
      putDatabaseValue = async (key,value) => {
        events.push('persist');
        ${quota ? "throw new DOMException('quota','QuotaExceededError');" : 'return save(key,value);'}
      };
      const upload = api;
      api = async (path,options) => { events.push('upload'); return upload(path,options); };
    `);
    await app.nodes.get('#message-form').emit('submit', {preventDefault(){}});
    if (quota) {
      assert.equal(app.run('uploads.length'), 0);
      assert.equal(app.run('selectedFile.name'), 'empty.bin');
      assert.match(app.nodes.get('#composer-error').textContent, /space/);
    } else {
      assert.equal(app.run('uploads.length'), 1);
      assert.ok(app.run('events.indexOf("persist") < events.indexOf("upload")'));
      assert.equal(app.run('messagesByChat.get(7).find(e => e.attachment).attachment.data'), '');
      assert.equal(app.run('selectedFile'), null);
    }
    assert.equal(app.nodes.get('#message-input').value, quota ? 'keep this text draft' : '');
    if (!quota) assert.equal(app.run('messagesByChat.get(7).find(e => e.attachment).text'), 'keep this text draft');
  }
});

test('group size rejects a maximal file before reading or encrypting', async () => {
  const { app } = protocol();
  await devices(app);
  app.run(`selectedChatId=7; elements.messageInput.value='';
    chats[0].participants = [...chats[0].participants, ...chats[0].participants.slice(1), ...chats[0].participants.slice(1)];
    globalThis.reads=0; globalThis.encryptions=0;
    encryptForRecipient = async () => {encryptions++;};
    selectAttachment({name:'large.bin',type:'',size:1048576,arrayBuffer:async () => {reads++;}});`);
  await app.nodes.get('#message-form').emit('submit', {preventDefault(){}});
  assert.equal(app.run('reads'), 0);
  assert.equal(app.run('encryptions'), 0);
  assert.match(app.nodes.get('#composer-error').textContent, /transport limit/);
});


test('file selection makes sending explicit, preserves drafts and restores keyboard focus on removal', () => {
  const { app } = protocol();
  const input = app.nodes.get('#attachment-input');
  app.nodes.get('#message-input').value = 'unsent draft';
  input.files = [{name: 'notes.txt', type: 'text/plain', size: 1024}];
  input.emit('change');
  assert.equal(app.nodes.get('#attachment-label').textContent, 'notes.txt');
  assert.equal(app.nodes.get('#attachment-size').textContent, '1.0 KiB · File attachment');
  assert.equal(app.nodes.get('#message-input').hidden, false);
  assert.equal(app.nodes.get('#send-button').textContent, 'Send file');
  assert.equal(app.document.activeElement, app.nodes.get('#send-button'));
  assert.equal(app.nodes.get('#attachment-button').attributes.get('aria-label'), 'Replace attached file');
  input.files = []; // Cancelling a replacement picker must keep the staged file.
  input.emit('change');
  assert.equal(app.run('selectedFile.name'), 'notes.txt');
  app.nodes.get('#attachment-cancel').emit('click');
  assert.equal(app.nodes.get('#attachment-preview').hidden, true);
  assert.equal(app.nodes.get('#message-input').hidden, false);
  assert.equal(app.nodes.get('#message-input').value, 'unsent draft');
  assert.equal(app.nodes.get('#send-button').textContent, 'Send');
  assert.equal(app.document.activeElement, app.nodes.get('#message-input'));
});

test('paste and drop stage exact File objects for review without reading, uploading or changing the text draft', () => {
  for (const gesture of ['paste', 'drop']) {
    const { app } = protocol();
    const file = {name:'clipboard.png', type:'image/png', size:1048576,
      arrayBuffer() { throw new Error('must not read before Send'); }};
    const event = {preventDefault() { this.prevented = true; }};
    event[gesture === 'paste' ? 'clipboardData' : 'dataTransfer'] = {
      files:[file], types:['Files','text/html'], getData() { throw new Error('must not read HTML'); },
    };
    app.nodes.get('#message-input').value = 'keep this draft';
    app.nodes.get('#conversation').emit(gesture, event);
    assert.equal(event.prevented, true);
    assert.equal(app.run('selectedFile'), file);
    assert.equal(app.nodes.get('#attachment-preview').hidden, false);
    assert.equal(app.nodes.get('#send-button').textContent, 'Send file');
    assert.equal(app.nodes.get('#message-input').value, 'keep this draft');
    assert.equal(app.run('sendingMessage'), false);
    assert.equal(app.run('messagesByChat.get(7)?.length || 0'), 0);
  }
});

test('transfer inputs enforce one file and 1 MiB, including zero bytes, without reading contents', () => {
  for (const gesture of ['paste','drop']) {
    const { app } = protocol();
    const key = gesture === 'paste' ? 'clipboardData' : 'dataTransfer';
    const send = files => app.nodes.get('#conversation').emit(gesture, {
      [key]: {files, types:['Files']}, preventDefault(){},
    });
    const file = {name:'empty.bin',type:'',size:0,arrayBuffer() { throw new Error('must not read'); }};
    send([file]);
    assert.equal(app.run('selectedFile'), file);
    send([file,file]);
    assert.equal(app.run('selectedFile'), file);
    assert.match(app.nodes.get('#composer-error').textContent, /one file/);
    send([{...file,size:1048577}]);
    assert.match(app.nodes.get('#composer-error').textContent, /1 MiB/);
    assert.equal(app.run('selectedFile'), null);
    send([]);
    assert.match(app.nodes.get('#composer-error').textContent, /No file/);
  }
});

test('text and URL transfers keep their native behavior without fetching or attaching remote images', () => {
  const { app } = protocol();
  for (const gesture of ['paste','drop','dragover']) {
    const event = {preventDefault(){ throw new Error('text default must remain'); }};
    event[gesture === 'paste' ? 'clipboardData' : 'dataTransfer'] = {
      files:[], types:['text/html','text/uri-list','text/plain'],
      getData() { throw new Error('must not parse or fetch remote content'); },
    };
    app.nodes.get('#conversation').emit(gesture,event);
    assert.equal(app.run('selectedFile'), null);
  }
});

test('closed chats, changed keys and active sends block gesture attachment replacement', () => {
  for (const blocked of ['chats[0].closed_at="2026-10-28"; selectChat(7);',
    'chats[0].participants[1].key_changed=true; selectChat(7);', 'sendingMessage=true;']) {
    for (const gesture of ['paste','drop']) {
      const { app } = protocol();
      app.run('selectAttachment({name:"retained.bin",type:"",size:0});');
      app.run(blocked);
      const event = {preventDefault(){this.prevented=true;}};
      event[gesture === 'paste' ? 'clipboardData' : 'dataTransfer'] = {
        files:[{name:'blocked.png',type:'image/png',size:0}], types:['Files'],
      };
      app.nodes.get('#conversation').emit(gesture,event);
      assert.equal(event.prevented,true);
      assert.equal(app.run('selectedFile.name'),'retained.bin');
      assert.match(app.nodes.get('#composer-error').textContent,/unavailable|finish sending/);
    }
  }
});

test('protected drag data enables a drop target without file reads and resets on leave or drop', () => {
  const { app } = protocol();
  const conversation = app.nodes.get('#conversation');
  const transfer = {types:['Files'],files:[]}; // Files are unreadable until drop.
  conversation.emit('dragover',{dataTransfer:transfer,preventDefault(){}});
  assert.equal(transfer.dropEffect,'copy');
  assert.equal(conversation.attributes.get('file-drag-over'),true);
  conversation.contains = node => node === app.nodes.get('#message-input');
  conversation.emit('dragleave',{relatedTarget:app.nodes.get('#message-input')});
  assert.equal(conversation.attributes.get('file-drag-over'),true);
  conversation.emit('dragleave',{relatedTarget:null});
  assert.equal(conversation.attributes.get('file-drag-over'),false);
  app.run('sendingMessage=true;');
  conversation.emit('dragover',{dataTransfer:transfer,preventDefault(){}});
  assert.equal(transfer.dropEffect,'none');
  assert.equal(conversation.attributes.get('file-drag-over'),false);
});
