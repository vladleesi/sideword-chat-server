const { test } = require('node:test');
const assert = require('node:assert/strict');
const { client } = require('./client_dom.cjs');

async function outgoing(options = {}) {
  const app = client(options);
  await app.run(`(async () => {
    persistHistoryEntry = async () => {};
    globalThis.message = {id:'outgoing:stable', clientMessageId:'stable', kind:'mine',
      statusVersion:1, recipients:['peer-a','peer-b'], text:'selectable body', createdAt:1000};
    await appendMessage(7, message);
    globalThis.receipt = {chat_id:7, client_message_id:'stable', reader_public_id:'peer-a',
      delivery_id:'c'.repeat(32), message_delivery_id:'a'.repeat(32),
      created_at:'2026-10-07T12:00:00Z', view_confirmed:true};
    globalThis.accept = async () => storeAcceptance(7, 'stable', {created_at:'2026-10-07T11:00:00Z',
      recipients:['peer-a','peer-b'], deliveries:{'peer-a':'a'.repeat(32), 'peer-b':'b'.repeat(32)}});
  })()`);
  return app;
}

function state(app) { return JSON.parse(app.run('JSON.stringify(outgoingStatus(7, message))')); }

test('pending, sending, failed, server acceptance and group aggregate transitions', async () => {
  const app = await outgoing();
  assert.equal(state(app).status, 'pending');
  app.run('outgoingAttempts.set(metadataKey(7,"stable"), "sending"); renderMessages();');
  assert.equal(state(app).status, 'sending');
  assert.equal(app.nodes.get('#message-list').firstChild.messageFooter.indicator.dataset.icon, 'clock');
  await app.run('appendMessage(7, {id:"send_failed:stable", kind:"send_failed", clientMessageId:"stable"})');
  assert.equal(state(app).status, 'failed');
  assert.match(app.nodes.get('#message-list').firstChild.messageFooter.indicator.attributes.get('aria-label'), /Failed/);
  await app.run('accept()');
  assert.equal(state(app).status, 'sent');
  assert.equal(app.nodes.get('#message-list').firstChild.messageFooter.indicator.dataset.icon, 'check');
  await app.run('processReceipts([receipt], true)');
  assert.equal(state(app).status, 'sent');
  await app.run('processReceipts([{...receipt, reader_public_id:"peer-b", message_delivery_id:"b".repeat(32), delivery_id:"d".repeat(32)}], true)');
  assert.equal(state(app).status, 'delivered');
  assert.equal(app.nodes.get('#message-list').firstChild.messageFooter.indicator.dataset.icon, 'double-check');
  await app.run('processReceipts([receipt])');
  assert.equal(state(app).status, 'read');
  await app.run('processReceipts([{...receipt, reader_public_id:"peer-b", message_delivery_id:"b".repeat(32), delivery_id:"e".repeat(32)}])');
  assert.equal(state(app).status, 'read');
  assert.match(app.nodes.get('#message-list').firstChild.messageFooter.indicator.className, /status-read/);
  assert.equal(app.nodes.get('#message-list').children.length, 1);
});

test('personal complete confirmations use one original recipient and never presence', async () => {
  const app = await outgoing();
  await app.run(`(async () => {message.recipients = ['peer-a'];
    await storeAcceptance(7, 'stable', {recipients:['peer-a'], deliveries:{'peer-a':'a'.repeat(32)}});})()`);
  app.run('presenceByChat.set(7, {online:new Set(["peer-a"])});');
  assert.equal(state(app).status, 'sent');
  await app.run('processReceipts([receipt], true)');
  assert.equal(state(app).status, 'delivered');
  await app.run('processReceipts([receipt])');
  assert.equal(state(app).status, 'read');
});

test('original recipient sets survive roster changes, partial details and duplicate names', async () => {
  const app = await outgoing();
  await app.run('accept(); processReceipts([receipt])');
  app.run(`chats[0].participants = [{public_id:'peer-a', display_name:'Alex'},
    {public_id:'peer-b', display_name:'Alex'}, {public_id:'new-peer',display_name:'New'}];
    showMessageDetails(7,message,elements.messageList.firstChild);`);
  assert.equal(state(app).status, 'read');
  const groups = app.nodes.get('#message-details-recipients').children;
  assert.match(groups[0].textContent, /Alex \(peer-a\)/);
  assert.match(groups[2].textContent, /Alex \(peer-b\)/);
  assert.doesNotMatch(groups.map(row => row.textContent).join(''), /New/);
  app.run('chats[0].participants = []; refreshMessageDetails();');
  assert.match(app.nodes.get('#message-details-recipients').children[0].textContent, /peer-a/);
});

for (const mismatch of ['chat', 'message', 'peer', 'delivery']) {
  test(`receipt ${mismatch} mismatches cannot upgrade the original outgoing message`, async () => {
    const app = await outgoing(); await app.run('accept()');
    const change = {chat:'chat_id:8', message:'client_message_id:"other"',
      peer:'reader_public_id:"other"', delivery:'message_delivery_id:"f".repeat(32)'}[mismatch];
    await app.run(`processReceipts([{...receipt, ${change}}])`);
    assert.equal(state(app).status, 'sent');
    assert.equal(state(app).states.some(peer => peer.read), false);
  });
}

test('read before delivered, duplicates and missing times preserve only confirmed facts', async () => {
  const app = await outgoing(); await app.run('accept()');
  await app.run(`(async () => {await processReceipts([{...receipt, created_at:null}]);
    await processReceipts([{...receipt, delivery_id:'d'.repeat(32), created_at:'2026-10-08T12:00:00Z'}]);
    await processReceipts([receipt], true);})()`);
  assert.equal(state(app).states[0].read, true);
  assert.equal(state(app).states[0].readAt, null);
  assert.equal(state(app).states[0].deliveredAt, Date.parse('2026-10-07T12:00:00Z'));
  assert.equal(app.run('metadataFor(7,"stable").receipts.size'), 2);
  assert.equal(app.run('pendingReceiptIds.size'), 2);
});

test('immutable encrypted acceptance and per-recipient confirmations survive reload and other tabs', async () => {
  const a = await outgoing(), b = await outgoing();
  await a.run(`(async () => {identity.storageKey = await crypto.subtle.generateKey({name:'AES-GCM',length:256},false,['encrypt','decrypt']);})()`);
  const records = new Map();
  // Restore the production persistence function, with a shared IndexedDB adapter.
  const fs = require('node:fs');
  const source = fs.readFileSync('app/static/client.js','utf8');
  const persist = source.slice(source.indexOf('async function persistHistoryEntry('), source.indexOf('async function loadStoredHistory('));
  for (const app of [a,b]) {
    app.run(persist);
    app.run('messagesByChat.clear(); messageMetadata.clear();');
  }
  const key = a.run('identity.storageKey');
  for (const app of [a,b]) {
    app.run('globalThis.storageAdapter = {};');
    const adapter = app.run('storageAdapter');
    adapter.key = key; adapter.put = async (key,value) => records.set(key,value);
    adapter.read = async () => [...records].map(([key,value]) => ({key,value}));
    app.run('identity.storageKey = storageAdapter.key; putDatabaseValue=storageAdapter.put; readHistoryRecords=storageAdapter.read;');
  }
  await a.run('(async () => {await appendMessage(7,message); await accept();})()');
  await b.run('(async () => {await loadStoredHistory(); await processReceipts([receipt]);})()');
  await a.run('loadStoredHistory();');
  assert.equal(state(a).states[0].read, true);
  assert.equal(state(a).status, 'read');
  assert.equal(a.run('identity.storageKey.extractable'), false);
  assert.equal(JSON.stringify([...records]).includes('selectable body'), false);
  assert.equal([...records.keys()].some(key => key.includes('peer-a') || key.includes('peer-b')), false);
  await a.run('processReceipts([receipt]);');
  assert.equal(a.run('messagesByChat.get(7).filter(entry => entry.kind === "mine").length'), 1);
});

test('status recovery restores receipts already acknowledged in another tab', async () => {
  const app = await outgoing();
  await app.run(`(async () => {identity.receiptRetentionSeconds=86400; message.createdAt=Date.now();
    api=async () => ({statuses:[{client_message_id:'stable',created_at:'2026-10-07T12:00:00Z',
      recipients:[{public_id:'peer-a',delivery_id:'a'.repeat(32),delivered_at:'2026-10-07T12:00:01Z',read_at:'2026-10-07T12:00:02Z'},
      {public_id:'peer-b',delivery_id:'b'.repeat(32),delivered_at:null,read_at:null}]}]});
    await recoverMessageStatuses(); await recoverMessageStatuses();})()`);
  assert.equal(state(app).states[0].read, true);
  assert.equal(state(app).status, 'read');
  assert.equal(app.run('metadataFor(7,"stable").receipts.size'), 2);
  assert.equal(app.run('pendingReceiptIds.size'), 0);
});

for (const mobile of [false,true]) {
  test(`outgoing ${mobile ? 'mobile' : 'desktop'} details support keyboard, touch, selection, copying and focus`, async () => {
    const app = await outgoing({mobile});
    if (mobile) app.nodes.get('#chat-list').children[0].children[0].emit('click');
    const bubble = app.nodes.get('#message-list').firstChild;
    let prevented = false;
    bubble.emit('keydown', {target:bubble,key:'Enter',preventDefault(){prevented=true;}});
    assert.equal(prevented,true);
    assert.equal(app.nodes.get('#message-details').open,true);
    assert.equal(app.document.activeElement,app.nodes.get('#close-message-details'));
    const technical = app.nodes.get('#message-details-technical');
    assert.match(technical.textContent,/stable/);
    await technical.children[1].children[1].emit('click');
    assert.deepEqual(app.copied,['stable']);
    app.nodes.get('#close-message-details').emit('click');
    assert.equal(app.document.activeElement,bubble);
    app.selection.selectedText='selected';
    bubble.emit('click',{target:bubble});
    assert.equal(app.nodes.get('#message-details').open,false);
    app.selection.selectedText='';
    bubble.messageFooter.indicator.emit('click',{stopPropagation(){}});
    assert.equal(app.nodes.get('#message-details').open,true);
    app.nodes.get('#message-details').close();
    assert.equal(app.document.activeElement,bubble.messageFooter.indicator);
  });
}

async function incoming(options = {}) {
  const app = client({observeVisibility:true,...options});
  await app.run(`persistHistoryEntry=async()=>{}; decryptMessage=async message=>message.ciphertext;
    globalThis.calls=[]; api=async(path,options)=>calls.push({path,body:JSON.parse(options.body)});
    globalThis.incoming={delivery_id:'a'.repeat(32),chat_id:7,sender_public_id:'peer-a',
      client_message_id:'in',ciphertext:'durable',created_at:'2026-10-07T12:00:00Z'};
    processIncoming({messages:[incoming]})`);
  return app;
}

test('durable incoming delivery is immediate and viewing requires continuous visible dwell', async () => {
  const app = await incoming(); const bubble=app.nodes.get('#message-list').firstChild;
  assert.equal(bubble.messageFooter,undefined);
  assert.equal(app.run('calls[0].body.confirm_delivery'),true);
  assert.equal(app.run('pendingReadsByChat.size'),0);
  app.intersect(bubble);
  assert.equal(app.run('pendingReadsByChat.size'),0);
  await app.fireViews();
  const calls=JSON.parse(app.run('JSON.stringify(calls)'));
  assert.equal(calls.length,2);
  assert.match(calls[1].path,/read\/exact$/);
  assert.equal(calls[1].body.viewed,true);
  app.intersect(bubble); await app.fireViews();
  assert.equal(app.run('calls.length'),2);
});

for (const hidden of ['document','focus','viewport','chat','dialog','mobile']) {
  test(`viewing fails closed when ${hidden} obscures a message and resets dwell`, async () => {
    const app = await incoming({mobile:hidden==='mobile'});
    if (hidden==='mobile') app.nodes.get('#chat-list').children[0].children[0].emit('click');
    const bubble=app.nodes.get('#message-list').firstChild;
    app.intersect(bubble);
    if (hidden==='document') {app.document.visibilityState='hidden';app.documentEvents.get('visibilitychange')();}
    if (hidden==='focus') {app.document.focused=false;app.windowEvents.get('blur')();}
    if (hidden==='viewport') {bubble.rect={top:110,bottom:160,left:0,right:100,width:100,height:50};app.nodes.get('#message-list').emit('scroll');}
    if (hidden==='chat') app.run('selectedChatId=8;renderMessages();');
    if (hidden==='dialog') app.nodes.get('#conversation-details-button').emit('click');
    if (hidden==='mobile') app.nodes.get('#back-to-chats').emit('click');
    await app.fireViews();
    assert.equal(app.run('calls.length'),1);
    assert.equal(app.run('pendingReadsByChat.size'),0);
  });
}

test('viewed confirmation retries after reload without retaining ciphertext or auto-reading new deliveries', async () => {
  const app=await incoming(); const bubble=app.nodes.get('#message-list').firstChild;
  app.run('api=async()=>{throw new Error("offline")}; showToast=()=>{};');
  app.intersect(bubble); await app.fireViews();
  assert.equal(app.run('pendingReadsByChat.get(7).size'),1);
  app.run(`pendingReadsByChat.clear();
    for(const entry of messagesByChat.get(7)) indexMessageMetadata(7,entry);
    api=async(path,options)=>calls.push({path,body:JSON.parse(options.body)});`);
  await app.run('flushAcknowledgements()');
  assert.equal(app.run('pendingReadsByChat.size'),0);
  await app.run(`processIncoming({messages:[{...incoming,delivery_id:'b'.repeat(32)}]})`);
  assert.equal(app.run('messagesByChat.get(7).filter(e=>e.kind==="theirs").length'),1);
  assert.equal(app.run('pendingReadsByChat.size'),0);
});

test('missing observation support never infers viewing from decrypted or stored history', async () => {
  const app=await incoming({observeVisibility:false});
  app.run('refreshVisibleReads();');
  assert.equal(app.run('viewTimers.size'),0);
  assert.equal(app.run('calls.length'),1);
});


test('a receipt persistence failure cannot delay acknowledgements for durable incoming messages', async () => {
  const app=await incoming();
  await app.run(`persistHistoryEntry=async(_,entry)=>{
    if(entry.kind==='receipt') throw new Error('receipt storage failed');
  };`);
  await assert.rejects(app.run(`processIncoming({messages:[{...incoming,delivery_id:'b'.repeat(32),
    client_message_id:'another'}],read_receipts:[{delivery_id:'c'.repeat(32),chat_id:7,
    reader_public_id:'peer-b',client_message_id:'stable',view_confirmed:true}]})`),/receipt storage failed/);
  assert.equal(app.run('acknowledgedDeliveries.has("b".repeat(32))'),true);
  assert.equal(app.run('pendingReceiptIds.size'),0);
  assert.equal(app.run('pendingReadsByChat.size'),0);
});


test('message details retain selectable full IDs when clipboard access is blocked', async () => {
  const app=await outgoing({clipboardBlocked:true});
  app.run('showMessageDetails(7,message,elements.messageList.firstChild)');
  const detail=app.nodes.get('#message-details-technical').children[1];
  await detail.children[1].emit('click');
  assert.equal(app.selection.value,'stable');
  assert.equal(app.document.activeElement,detail.children[0]);
  assert.match(app.nodes.get('#message-details-copy-status').textContent,/copy it manually/);
});


test('lost local ACK-marker persistence retries the exact delivery without duplicate messages', async () => {
  const app=client();
  await app.run(`decryptMessage=async message=>message.ciphertext;
    globalThis.calls=[]; api=async(path,options)=>calls.push(JSON.parse(options.body));
    persistHistoryEntry=async(_,entry)=>{if(entry.kind==='delivery_ack') throw new Error('marker failed');};
    globalThis.incoming={delivery_id:'a'.repeat(32),chat_id:7,sender_public_id:'peer-a',
      client_message_id:'in',ciphertext:'durable'};`);
  await assert.rejects(app.run('processIncoming({messages:[incoming]})'),/marker failed/);
  assert.equal(app.run('pendingDeliveries.size'),1);
  await app.run('persistHistoryEntry=async()=>{};processIncoming({messages:[incoming]})');
  assert.equal(app.run('pendingDeliveries.size'),0);
  assert.equal(app.run('messagesByChat.get(7).filter(entry=>entry.kind==="theirs").length'),1);
  assert.equal(app.run('JSON.stringify(calls[0])===JSON.stringify(calls[1])'),true);
  assert.equal(app.run('pendingReadsByChat.size'),0);
});


for (const advertised of [undefined,0,'86400',86400]) {
  test(`confirmation capability comes from the current backend: ${String(advertised)}`, async () => {
    const app=client({observeVisibility:true});
    const supported=advertised===86400;
    const field=advertised===undefined?'':`,receipt_retention_seconds:${JSON.stringify(advertised)}`;
    await app.run(`(async()=>{
      identity.publicKey='own-key'; identity.receiptRetentionSeconds=86400;
      writeIdentity=async()=>{}; observePeerKey=async peer=>peer;
      persistHistoryEntry=async()=>{}; decryptMessage=async message=>message.ciphertext;
      globalThis.requests=[];
      api=async(path,options)=>{
        if(path==='/api/v1/me') return {user:{public_id:'me',public_key:'own-key'},chats:chats${field}};
        const body=JSON.parse(options.body); requests.push({path,body});
        if(!${supported} && ('confirm_delivery' in body || 'viewed' in body || path.endsWith('/read/exact')))
          throw new Error('HTTP 422: unsupported fields');
        return {};
      };
      await loadChats();
      globalThis.incoming={delivery_id:'a'.repeat(32),chat_id:7,sender_public_id:'peer-a',
        client_message_id:'mixed-version',ciphertext:'safely stored'};
      await processIncoming({messages:[incoming]});
    })()`);
    assert.equal(app.run('messageConfirmationsSupported'),supported);
    assert.equal(app.run('acknowledgedDeliveries.has(incoming.delivery_id)'),true);
    assert.equal(app.run('Object.hasOwn(requests[0].body,"confirm_delivery")'),supported);
    const bubble=app.nodes.get('#message-list').firstChild;
    app.intersect(bubble); await app.fireViews();
    assert.equal(app.run('requests.length'),supported?2:1);
    if(!supported) {
      await app.run(`appendMessage(7,{id:'viewed:'+incoming.delivery_id,kind:'viewed',delivery:incoming});
        flushAcknowledgements(); recoverMessageStatuses();`);
      assert.equal(app.run('requests.length'),1);
      assert.equal(app.run('pendingReadsByChat.get(7).size'),1);
    }
  });
}

test('saved capabilities cannot enable read confirmations before current-server negotiation', async () => {
  const app=client({observeVisibility:true});
  await app.run(`messageConfirmationsSupported=false;identity.receiptRetentionSeconds=86400;
    persistHistoryEntry=async()=>{};decryptMessage=async message=>message.ciphertext;
    globalThis.requests=[];api=async(path,options)=>requests.push({path,body:JSON.parse(options.body)});
    processIncoming({messages:[{delivery_id:'a'.repeat(32),chat_id:7,sender_public_id:'peer-a',
      client_message_id:'not-negotiated',ciphertext:'durable'}]})`);
  app.intersect(app.nodes.get('#message-list').firstChild); await app.fireViews();
  assert.equal(app.run('requests.length'),1);
  assert.equal(app.run('Object.hasOwn(requests[0].body,"confirm_delivery")'),false);
  assert.equal(app.run('pendingReadsByChat.size'),0);
});


test('one confirmed reader shows orange double checks while other original recipients remain pending', async () => {
  const app=await outgoing();
  await app.run(`(async()=>{
    message.recipients=['peer-a','peer-b','peer-c'];
    await storeAcceptance(7,'stable',{recipients:message.recipients,
      deliveries:{'peer-a':'a'.repeat(32),'peer-b':'b'.repeat(32),'peer-c':'f'.repeat(32)}});
    await processReceipts([receipt]);
    showMessageDetails(7,message,elements.messageList.firstChild);
  })()`);
  const status=state(app);
  assert.equal(status.status,'read');
  assert.equal(status.readCount,1);
  assert.equal(status.allRead,false);
  assert.equal(status.states.filter(peer=>!peer.delivered).length,2);
  const indicator=app.nodes.get('#message-list').firstChild.messageFooter.indicator;
  assert.equal(indicator.dataset.icon,'double-check');
  assert.match(indicator.className,/status-read/);
  assert.equal(indicator.attributes.get('aria-label'),'Read by 1 of 3 recipients. Open message details');
  assert.equal(app.nodes.get('#message-details-status').textContent,'Read by 1 of 3 recipients');
  assert.equal(app.nodes.get('#message-details-recipients').children[2].children[1].children.length,2);
});


test('partial reads keep status recovery active until all original recipients have read', async () => {
  const app=await outgoing();
  await app.run(`(async()=>{
    message.createdAt=Date.now(); identity.receiptRetentionSeconds=86400;
    globalThis.requests=0;
    api=async()=>{
      requests++;
      return {statuses:[{client_message_id:'stable',created_at:'2026-10-07T12:00:00Z',
        recipients:[{public_id:'peer-a',delivery_id:'a'.repeat(32),
          delivered_at:'2026-10-07T12:00:01Z',read_at:'2026-10-07T12:00:02Z'},
        {public_id:'peer-b',delivery_id:'b'.repeat(32),delivered_at:null,
          read_at:requests>1?'2026-10-07T12:00:03Z':null}]}]};
    };
    await recoverMessageStatuses();
  })()`);
  assert.equal(state(app).status,'read');
  assert.equal(state(app).allRead,false);
  await app.run('recoverMessageStatuses()');
  assert.equal(app.run('requests'),2);
  assert.equal(state(app).readCount,2);
  assert.equal(state(app).allRead,true);
  await app.run('recoverMessageStatuses()');
  assert.equal(app.run('requests'),2);
});


test('legacy automatic-read receipts never produce orange checks with recipients still pending', async () => {
  const app=await outgoing();
  await app.run('(async()=>{await accept(); await processReceipts([{...receipt,view_confirmed:false}]);})()');
  assert.equal(state(app).status,'sent');
  assert.equal(state(app).readCount,0);
  const indicator=app.nodes.get('#message-list').firstChild.messageFooter.indicator;
  assert.equal(indicator.dataset.icon,'check');
  assert.doesNotMatch(indicator.className,/status-read/);
});

test('delivery icons use one compact SVG; delivered and read share the same overlapping silhouette', async () => {
  const app=await outgoing(); const row=app.nodes.get('#message-list').firstChild;
  const indicator=row.messageFooter.indicator;
  assert.equal(indicator.dataset.icon,'warning');
  app.run('outgoingAttempts.set(metadataKey(7,"stable"),"sending");renderMessages();');
  assert.equal(indicator.dataset.icon,'clock');
  await app.run('accept()');
  assert.equal(indicator.dataset.icon,'check');
  assert.equal(indicator.firstChild.children.length,1);
  await app.run(`processReceipts([receipt,{...receipt,reader_public_id:'peer-b',message_delivery_id:'b'.repeat(32),delivery_id:'d'.repeat(32)}],true)`);
  const svg=indicator.firstChild;
  assert.equal(indicator.children.length,1);
  assert.equal(svg.tagName,'svg');
  assert.equal(svg.attributes.get('viewBox'),'0 0 24 18');
  assert.equal(svg.attributes.get('aria-hidden'),'true');
  assert.equal(svg.attributes.get('stroke'),'currentColor');
  assert.equal(svg.children.length,2);
  assert.equal(svg.children.filter(node=>node.tagName==='svg').length,0);
  assert.deepEqual(svg.children.map(path=>path.attributes.get('d')),['M2 10l5 5L18 4','M13 14l2 2 7-7']);
  await app.run('processReceipts([receipt])');
  assert.equal(indicator.firstChild,svg);
  assert.match(indicator.className,/status-read/);
  assert.match(indicator.attributes.get('aria-label'),/Read by 1 of 2 recipients/);
  assert.equal(indicator.textContent,'');
});

for (const [locale,timeZone] of [['en-US','America/Los_Angeles'],['de-DE','Europe/Berlin']]) {
  test(`message details and confirmation times use the same browser ${locale} formatting as headers`, async () => {
    const app=await outgoing({locale,timeZone});
    await app.run('accept();'); await app.run('processReceipts([receipt])');
    const row=app.nodes.get('#message-list').firstChild;
    row.emit('click',{target:row});
    const expected=new Intl.DateTimeFormat(locale,{timeZone,dateStyle:'short',timeStyle:'medium'});
    assert.equal(row.messageHeader.timestamp.title,expected.format(new Date('2026-10-07T11:00:00Z')));
    assert.equal(app.nodes.get('#message-details-time').textContent,row.messageHeader.timestamp.title);
    assert.match(app.nodes.get('#message-details-recipients').textContent,
      new RegExp(expected.format(new Date('2026-10-07T12:00:00Z')).replace(/[.*+?^${}()|[\]\\]/g,'\\$&')));
  });
}

function rectangle(left,top,width,height) { return {left,top,width,height,right:left+width,bottom:top+height}; }

test('desktop details align below a message and flip above near the bottom while clamping both edges', async () => {
  const app=await outgoing(); const row=app.nodes.get('#message-list').firstChild;
  const dialog=app.nodes.get('#message-details'); dialog.rect=rectangle(0,0,352,300);
  row.rect=rectangle(700,100,180,60);
  row.emit('click',{target:row});
  assert.equal(dialog.style.left,'528px'); assert.equal(dialog.style.top,'168px');
  dialog.close(); row.rect=rectangle(950,680,100,60);
  row.emit('click',{target:row});
  assert.equal(dialog.style.left,'636px'); assert.equal(dialog.style.top,'372px');
  dialog.close(); row.rect=rectangle(0,680,100,60);
  row.emit('click',{target:row});
  assert.equal(dialog.style.left,'12px'); assert.equal(dialog.style.top,'372px');
  dialog.rect=rectangle(0,0,352,500); dialog.emit('toggle');
  assert.equal(dialog.style.top,'172px');
});

test('desktop details respond to viewport resize, pan, and technical expansion within visible bounds', async () => {
  const app=await outgoing(); const row=app.nodes.get('#message-list').firstChild;
  const dialog=app.nodes.get('#message-details'); dialog.rect=rectangle(0,0,352,700);
  row.rect=rectangle(850,620,100,60); row.emit('click',{target:row});
  assert.equal(dialog.style.top,'12px');
  app.window.visualViewport.width=500; app.window.visualViewport.height=500;
  app.window.visualViewport.offsetLeft=100; app.window.visualViewport.offsetTop=50;
  app.viewportEvents.get('resize')();
  assert.equal(dialog.style.left,'236px'); assert.equal(dialog.style.top,'62px');
  assert.equal(dialog.style.maxWidth,'476px'); assert.equal(dialog.style.maxHeight,'476px');
  app.window.visualViewport.offsetLeft=150;
  app.viewportEvents.get('scroll')(); assert.equal(dialog.style.left,'286px');
  app.window.visualViewport.scale=2; app.window.visualViewport.width=400;
  app.viewportEvents.get('resize')(); assert.equal(dialog.style.left,'186px');
  assert.equal(app.properties.get('--visual-viewport-height'),'500px');
  app.window.visualViewport.scale=1; app.window.visualViewport.width=500;
  app.viewportEvents.get('resize')();
  app.window.innerWidth=900; app.windowEvents.get('resize')();
  assert.equal(dialog.style.left,'286px');
});

test('mobile details reset desktop positioning and use the accessible native sheet', async () => {
  const app=await outgoing({mobile:true});
  app.nodes.get('#chat-list').children[0].children[0].emit('click');
  const row=app.nodes.get('#message-list').firstChild;
  const dialog=app.nodes.get('#message-details');
  Object.assign(dialog.style,{left:'400px',top:'600px',maxWidth:'300px',maxHeight:'400px'});
  row.emit('keydown',{target:row,key:' ',preventDefault(){}});
  for (const property of ['left','top','maxWidth','maxHeight']) assert.equal(dialog.style[property],'');
  assert.equal(app.document.activeElement,app.nodes.get('#close-message-details'));
  dialog.emit('cancel'); assert.equal(dialog.open,false);
  assert.equal(app.document.activeElement,row);
});

test('message details close on one outside click or native Escape while preserving inside interactions', async () => {
  const app=await outgoing(); const row=app.nodes.get('#message-list').firstChild;
  const dialog=app.nodes.get('#message-details');
  row.emit('click',{target:row});
  dialog.emit('click',{target:dialog,clientX:50,clientY:50}); assert.equal(dialog.open,true);
  dialog.emit('click',{target:app.nodes.get('#message-details-technical'),clientX:200,clientY:200});
  assert.equal(dialog.open,true);
  dialog.emit('click',{target:dialog,clientX:150,clientY:50}); assert.equal(dialog.open,false);
  assert.equal(app.document.activeElement,row);
  row.messageFooter.indicator.emit('click',{stopPropagation(){}});
  dialog.emit('cancel'); assert.equal(dialog.open,false);
  assert.equal(app.document.activeElement,row.messageFooter.indicator);
});

test('a visible sender header without visible message content cannot confirm human reading', async () => {
  const app=await incoming(); const row=app.nodes.get('#message-list').firstChild;
  row.rect=rectangle(0,75,100,100);
  row.messageBubble.rect=rectangle(0,101,100,74);
  app.intersect(row); await app.fireViews();
  assert.equal(app.run('calls.length'),1);
  row.messageBubble.rect=rectangle(0,40,100,60);
  app.nodes.get('#message-list').emit('scroll'); await app.fireViews();
  assert.equal(app.run('calls.length'),2);
});
