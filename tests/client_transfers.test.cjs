const { test } = require('node:test');
const assert = require('node:assert/strict');
const { client } = require('./client_dom.cjs');

async function fixture({size = 3} = {}) {
  const app = client();
  await app.run(`(async () => {
    globalThis.sender = await generateIdentity(); sender.publicId = 'sender';
    globalThis.receiver = await generateIdentity(); receiver.publicId = 'receiver';
    receiver.token = sender.token = 'synthetic';
    sender.retryWindowSeconds = receiver.retryWindowSeconds = 3600;
    identity = receiver; selectedChatId = 7;
    chats = [{id:7,participants:[sender,receiver].map(value => ({public_id:value.publicId,public_key:value.publicKey}))}];
    assertTrustedPeer = async () => {};
    deviceLock = async (_, action) => action();
    globalThis.records = new Map();
    putDatabaseValue = async (key, value, removeKey, allowed) => {
      if (allowed && !allowed()) return false;
      if (!removeKey || !records.has(key)) records.set(key,value);
      if (removeKey) records.delete(removeKey);
    };
    readHistoryRecords = async (prefix='history:') => [...records].filter(([key]) => key.startsWith(prefix))
      .map(([key,value]) => ({key,value}));
    removeOutbox = async key => records.delete(key);
    globalThis.file = SidewordProtocol.filePayload('synthetic-private-name.png','image/png',
      Uint8Array.from({length:${size}}, (_,i) => i%256));
    globalThis.reference = SidewordProtocol.fileReference(file.name,file.mime,file.size,'card.media','Caption <script>');
    globalThis.card = {chat_id:7,client_message_id:'card',sender_public_id:'sender',delivery_id:'a'.repeat(32),
      created_at:'2026-10-29T00:00:00Z',ciphertext:await SidewordProtocol.encryptForRecipient(sender,
        SidewordProtocol.encodePayload(reference),7,'card',chats[0].participants[1])};
    globalThis.mediaMessage = {...card,client_message_id:'card.media',delivery_id:'b'.repeat(32),
      ciphertext:await SidewordProtocol.encryptForRecipient(sender,
        SidewordProtocol.encodePayload(file),7,'card.media',chats[0].participants[1])};
  })()`);
  const requests = [];
  let read = async () => app.run('mediaMessage');
  let acknowledge = async () => ({deleted_messages:1});
  app.run('fn => { api = fn; }')(async (path, options) => {
    const body = options?.body ? JSON.parse(options.body) : {};
    requests.push({path,body});
    if (path.endsWith('/media/read')) return read(options);
    if (path === '/api/v1/ack/exact') return acknowledge(body);
    return {recipients:['receiver'],deliveries:{receiver:'a'.repeat(32)},content_expires_at:new Date(Date.now()+60000).toISOString()};
  });
  return {...app,requests,setRead:value=>{read=value;},setAck:value=>{acknowledge=value;},
    receive:()=>app.run('processIncoming({messages:[card]})'),
    transfer:()=>app.run('transferMedia()'),
    mediaAcks:()=>requests.filter(request=>request.path==='/api/v1/ack/exact'
      && request.body.messages?.some(message=>message.client_message_id==='card.media'))};
}

test('metadata and text remain durable and ACKed while a media download is stalled', {timeout:5000}, async () => {
  const app = await fixture();
  await app.receive();
  assert.equal(app.requests.some(request=>request.path.endsWith('/media/read')),false);
  assert.equal(app.requests[0].body.messages[0].client_message_id,'card');
  const node = app.nodes.get('#message-list').children[0];
  assert.equal(node.messageContent.textContent,'Caption <script>');
  assert.equal(node.messageContent.children.length,0);
  assert.equal(node.messageBubble.textContent.includes('synthetic-private-name'),false);
  assert.match(node.messageBubble.textContent,/Waiting for media/);
  let resolve, started;
  const waiting = new Promise(done=>{started=done;});
  app.setRead(()=>new Promise(done=>{resolve=done;started();}));
  const transfer = app.transfer();
  await waiting;
  await app.run(`(async () => {
    const next = {...card,client_message_id:'text',delivery_id:'c'.repeat(32),
      ciphertext:await SidewordProtocol.encryptForRecipient(sender,
        SidewordProtocol.encodePayload(SidewordProtocol.textPayload('Text stays fast')),7,'text',chats[0].participants[1])};
    await processIncoming({messages:[next]});
  })()`);
  assert.ok(app.requests.some(request=>request.body.messages?.some(message=>message.client_message_id==='text')));
  assert.equal(app.mediaAcks().length,0);
  resolve(app.run('mediaMessage'));
  await transfer;
  assert.equal(app.mediaAcks().length,1);
  assert.equal(app.nodes.get('#message-list').children[0],node);
  assert.equal(app.run("messagesByChat.get(7).find(entry=>entry.kind==='theirs').attachment.data"),'AAEC');
});

test('media authenticates and persists encrypted locally before its separate exact ACK', async () => {
  const app = await fixture();
  await app.receive();
  app.setAck(async body=>{
    if (body.messages?.some(message=>message.client_message_id==='card.media')) {
      assert.equal(app.run("[...records.keys()].some(key=>key.includes('media_ready:'))"),true);
      assert.equal(app.run("[...records.values()].every(value=>Object.prototype.toString.call(value.ciphertext)==='[object ArrayBuffer]')"),true);
      assert.equal(app.run("JSON.stringify([...records]).includes('synthetic-private-name')"),false);
    }
    return {deleted_messages:1};
  });
  await app.transfer();
  assert.equal(app.mediaAcks().length,1);
  assert.equal(app.requests.find(request=>request.path.endsWith('/media/read')).body.sender_public_id,'sender');
  app.run('messagesByChat.clear(); receivedMedia.clear(); loadedHistoryKeys.clear(); seenMessageIds.clear();');
  await app.run('loadStoredHistory()');
  assert.equal(app.run("messagesByChat.get(7).find(entry=>entry.kind==='theirs').attachment.data"),'AAEC');
});

test('media storage failure withholds only the media ACK and remains retryable', async () => {
  const app = await fixture();
  await app.receive();
  app.run(`globalThis.save = putDatabaseValue;
    putDatabaseValue = async (key,...args) => {if(key.includes('media_ready:')) throw new Error('synthetic quota');return save(key,...args);};`);
  await app.transfer();
  assert.equal(app.mediaAcks().length,0);
  assert.equal(app.run("messagesByChat.get(7).find(entry=>entry.kind==='theirs').attachment.type"),'attachment-ref');
  app.run("putDatabaseValue = save; mediaFailures.clear(); for(const entry of messagesByChat.get(7)) entry.mediaTransfer=null;");
  await app.transfer();
  assert.equal(app.mediaAcks().length,1);
});

test('normal session reload with cloned non-exportable keys does not cancel media delivery', async () => {
  const app=await fixture();
  await app.receive();
  const restored=structuredClone(app.run('identity'));
  app.setRead(async()=>{
    app.run('value=>{identity=value;}')(restored);
    return app.run('mediaMessage');
  });
  await app.transfer();
  assert.equal(app.mediaAcks().length,1);
});

test('an identity key change during download cannot persist or ACK old media', async () => {
  const app=await fixture();
  await app.receive();
  app.setRead(async()=>{
    app.run('identity={...identity,publicKey:sender.publicKey};');
    return app.run('mediaMessage');
  });
  await app.transfer();
  assert.equal(app.mediaAcks().length,0);
  assert.equal(app.run("[...records.keys()].some(key=>key.includes('media_ready:'))"),false);
});

for (const change of ['ciphertext','chat','sender','id','name','size','mime']) {
  test(`media ${change} substitution fails closed without deletion ACK`, async () => {
    const app = await fixture();
    await app.receive();
    if (change==='ciphertext') app.run('mediaMessage.ciphertext = card.ciphertext;');
    if (change==='chat') app.run('mediaMessage.chat_id=8;');
    if (change==='sender') app.run("mediaMessage.sender_public_id='foreign';");
    if (change==='id') app.run("mediaMessage.client_message_id='different.media';");
    if (['name','size','mime'].includes(change)) {
      await app.run(`(async()=>{
        const altered = {...file, ${change}: ${change==='size'? 2 : "'changed'"}};
        ${change==='size'? "altered.data='AAE=';" : ''}
        mediaMessage.ciphertext = await SidewordProtocol.encryptForRecipient(sender,
          SidewordProtocol.encodePayload(altered),7,'card.media',chats[0].participants[1]);
      })()`);
    }
    await app.transfer();
    assert.equal(app.mediaAcks().length,0);
    assert.equal(app.run("messagesByChat.get(7).find(entry=>entry.kind==='theirs').attachment.type"),'attachment-ref');
  });
}

test('lost media ACK retries from durable history without downloading the file again', async () => {
  const app = await fixture();
  await app.receive();
  app.setAck(async body=>{if(body.messages?.some(message=>message.client_message_id==='card.media')) throw new Error('response lost');return {};});
  await app.transfer();
  assert.equal(app.mediaAcks().length,1);
  app.setAck(async()=>({}));
  await app.run('processIncoming({})');
  assert.equal(app.mediaAcks().length,2);
  assert.equal(app.requests.filter(request=>request.path.endsWith('/media/read')).length,1);
});

test('page lifecycle cancellation cannot persist or ACK late media', async () => {
  const app = await fixture();
  await app.receive();
  let resolve, started;
  const waiting = new Promise(done=>{started=done;});
  app.setRead(()=>new Promise(done=>{resolve=done;started();}));
  const transfer=app.transfer();
  await waiting;
  app.windowEvents.get('pagehide')();
  resolve(app.run('mediaMessage'));
  await transfer;
  assert.equal(app.mediaAcks().length,0);
  assert.equal(app.run("[...records.keys()].some(key=>key.includes('media_ready:'))"),false);
});

test('cancellation during local encryption cannot write, render or ACK stale media', async () => {
  const app=await fixture();
  await app.receive();
  await app.run(`globalThis.releaseSave=null;globalThis.saveStarted=null;
    globalThis.saving=new Promise(resolve=>{saveStarted=resolve;});
    globalThis.originalPersist=persistHistoryEntry;
    persistHistoryEntry=async(...args)=>{
      if(args[1].kind==='media_ready') {
        saveStarted();await new Promise(resolve=>{releaseSave=resolve;});
      }
      return originalPersist(...args);
    };`);
  const transfer=app.transfer();
  await app.run('saving');
  app.run('stopMediaTransfers();');
  app.run('releaseSave();');
  await transfer;
  assert.equal(app.mediaAcks().length,0);
  assert.equal(app.run("[...records.keys()].some(key=>key.includes('media_ready:'))"),false);
  assert.equal(app.run("messagesByChat.get(7).find(entry=>entry.kind==='theirs').attachment.type"),'attachment-ref');
});

test('media download retries stop automatically at six attempts and stay bounded', async () => {
  const app=await fixture();
  await app.receive();
  app.setRead(async()=>{throw new Error('unavailable');});
  for(let i=0;i<8;i++) {
    app.run('for(const value of mediaFailures.values()) value.nextAt=0;');
    await app.transfer();
  }
  assert.equal(app.requests.filter(request=>request.path.endsWith('/media/read')).length,6);
  assert.equal(app.mediaAcks().length,0);
  assert.match(app.nodes.get('#message-list').children[0].messageBubble.textContent,/Retry download/);
});

test('media uploads persist immutable ciphertext and retry without blocking or resending metadata', async () => {
  const app = await fixture();
  await app.run(`(async()=>{
    identity=sender;
    await persistOutbox({chatId:7,messageId:'card',createdAt:Date.now(),expiresAt:Date.now()+60000,
      payload:reference,media:file,envelopes:[{recipient_public_id:'receiver',ciphertext:'metadata-only'}]});
    await flushOutbox();
  })()`);
  assert.equal(app.requests.filter(request=>request.path.endsWith('/messages')).length,1);
  assert.equal(app.run("[...records.keys()].some(key=>key.startsWith('outbox:'))"),false);
  const wires=[];
  app.run('fn => { api=fn; }')(async(path,options)=>{wires.push(options.body);throw new Error('lost response');});
  await app.transfer();
  app.run('mediaFailures.clear();');
  app.run('fn => { api=fn; }')(async(path,options)=>{wires.push(options.body);return {};});
  await app.transfer();
  assert.equal(wires.length,2);
  assert.equal(wires[0],wires[1]);
  assert.equal(JSON.parse(wires[0]).channel,'media');
  assert.equal(app.run("[...records.keys()].some(key=>key.startsWith('mediaoutbox:'))"),false);
});

test('interrupted atomic handoff retries metadata without losing or duplicating the media outbox', async () => {
  const app=await fixture();
  await app.run(`(async()=>{
    identity=sender;
    await persistOutbox({chatId:7,messageId:'card',createdAt:Date.now(),expiresAt:Date.now()+60000,
      payload:reference,media:file,envelopes:[{recipient_public_id:'receiver',ciphertext:'metadata-only'}]});
    globalThis.save=putDatabaseValue;
    putDatabaseValue=async(key,value,removeKey)=>{if(removeKey)throw new Error('interrupted');return save(key,value);};
  })()`);
  await assert.rejects(app.run('flushOutbox()'),/interrupted/);
  assert.equal(app.run("[...records.keys()].some(key=>key.startsWith('outbox:'))"),true);
  assert.equal(app.run("[...records.keys()].some(key=>key.startsWith('mediaoutbox:'))"),false);
  app.run('putDatabaseValue=save;');
  await app.run('flushOutbox()');
  const messages=app.requests.filter(request=>request.path.endsWith('/messages'));
  assert.deepEqual(messages[0].body,messages[1].body);
  assert.equal(app.run("[...records.keys()].filter(key=>key.startsWith('mediaoutbox:')).length"),1);
});

test('media response streaming stops at its byte budget before parsing oversized JSON', async () => {
  const app=client();
  app.run(`globalThis.cancelled=false;
    globalThis.fetch=async()=>({ok:true,status:200,body:{getReader:()=>({
      read:async()=>({done:false,value:new Uint8Array(SidewordProtocol.MAX_WIRE_CHARS+2049)}),
      cancel:async()=>{cancelled=true;},releaseLock(){}
    })}});`);
  await assert.rejects(app.run("fetchClient('/api/v1/chats/7/media/read',{})"),/could not read/);
  assert.equal(app.run('cancelled'),true);
});

test('attachment references stay text-sized regardless of file size and reject ambiguous fields', () => {
  const app=client(),p=app.run('SidewordProtocol');
  const small=p.fileReference('private.png','image/png',0,'card.media','Caption');
  const large=p.fileReference('private.png','image/png',1048576,'card.media','Caption');
  assert.ok(Buffer.byteLength(p.encodePayload(large))<512);
  assert.ok(Buffer.byteLength(p.encodePayload(large))-Buffer.byteLength(p.encodePayload(small))<10);
  for(const update of [{data:'AA=='},{media_id:'https://invalid.example/file'},{media_id:'../file.media'},
    {text:'x'.repeat(16001)},{size:1048577},{size:-1},{size:'1'},{name:''},{mime:'image/png\n'}]) {
    assert.throws(()=>p.validatePayload({...large,...update}));
  }
});

test('media response reader releases its lock for valid chunked JSON', async () => {
  const app=client();
  app.run(`globalThis.released=false;globalThis.parts=[encoder.encode('{"ok":'),encoder.encode('true}')];
    globalThis.fetch=async()=>({ok:true,status:200,body:{getReader:()=>({
      read:async()=>parts.length?{done:false,value:parts.shift()}:{done:true},
      cancel:async()=>{},releaseLock(){released=true;}
    })}});`);
  const result=await app.run("fetchClient('/api/v1/chats/7/media/read',{})");
  assert.equal(result.body.ok,true);
  assert.equal(app.run('released'),true);
});

test('normal media sends keep recovery controls hidden until user action is needed', async () => {
  const app=await fixture();
  await app.run(`(async()=>{
    identity=sender;
    await persistOutbox({chatId:7,messageId:'card.media',payload:file,
      expiresAt:Date.now()+60000},'mediaoutbox');
    await renderOutbox();
  })()`);
  assert.equal(app.nodes.get('#pending-sends').hidden,true);
  assert.equal(app.run("[...records.keys()].filter(key=>key.startsWith('mediaoutbox:')).length"),1);
  await app.run(`(async()=>{
    const key=[...records.keys()].find(key=>key.startsWith('mediaoutbox:'));
    mediaFailures.set(key,{attempts:6});
    await renderOutbox();
  })()`);
  assert.equal(app.nodes.get('#pending-sends').hidden,false);
  assert.match(app.nodes.get('#pending-send-list').textContent,/Could not send/);
  app.run('mediaFailures.clear();');
  await app.run('renderOutbox()');
  assert.equal(app.nodes.get('#pending-sends').hidden,true);
  assert.equal(app.run("[...records.keys()].filter(key=>key.startsWith('mediaoutbox:')).length"),1);
});
