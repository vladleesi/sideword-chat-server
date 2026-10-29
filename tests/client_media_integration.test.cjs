const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { client } = require('./client_dom.cjs');

async function previewClient() {
  const app = client();
  const p = app.run('SidewordProtocol');
  const fixture = p.filePayload('image.png', 'image/png', fs.readFileSync('tests/fixtures/media/valid.png'));
  app.run('value => { globalThis.fixture = value; }')(fixture);
  app.run(`
    globalThis.created = 0; globalThis.revoked = [];
    globalThis.setTimeout = window.setTimeout;
    globalThis.clearTimeout = window.clearTimeout;
    globalThis.Blob = class { constructor(parts, options) { this.parts = parts; this.type = options.type; } };
    globalThis.URL = {createObjectURL() { return 'blob:synthetic-' + (++created); },
      revokeObjectURL(url) { revoked.push(url); }};
    persistHistoryEntry = async () => {};
  `);
  const create = app.document.createElement;
  app.document.createElement = tag => {
    const node = create(tag);
    if (tag === 'img') {
      node.naturalWidth = node.naturalHeight = 2;
      node.decode = async () => {};
      node.removeAttribute = name => { if (name === 'src') node.src = ''; };
    }
    return node;
  };
  await app.run('appendMessage(7, {id:"image", kind:"theirs", attachment:fixture}, false)');
  const card = app.nodes.get('#message-list').children[0].messageBubble;
  const preview = card.children.find(node => node.className?.split(' ').includes('media-preview'));
  return {...app, preview};
}

async function flushPreviews(app) {
  app.run('scheduleVisiblePreviews()');
  for (const timer of app.timers.filter(timer => timer.delay === 0)) timer();
  await new Promise(resolve => setImmediate(resolve));
}

test('visible images preview automatically without a preview button and release off-screen URLs', async () => {
  const app=await previewClient();
  assert.equal(app.preview.children.some(node=>node.tagName==='button'),false);
  await flushPreviews(app);
  assert.equal(app.run('created'),1);
  const node=app.nodes.get('#message-list').children[0];
  node.rect={top:1000,bottom:1100,left:0,right:100,width:100,height:100};
  await flushPreviews(app);
  assert.equal(app.run('revoked.length'),1);
  assert.equal(app.preview.children.some(node=>node.tagName==='img'),false);
  assert.match(app.preview.className,/media-frame/);
  node.rect={top:0,bottom:100,left:0,right:100,width:100,height:100};
  await flushPreviews(app);
  await flushPreviews(app);
  assert.equal(app.run('created'),2);
});

test('small scroll reversals retain the decoded image instead of blinking through a loader', async () => {
  const app=await previewClient();
  await flushPreviews(app);
  const image=app.preview.children.at(-1);
  const node=app.nodes.get('#message-list').children[0];
  for(let i=0;i<4;i++) {
    node.rect={top:101,bottom:201,left:0,right:100,width:100,height:100};
    await flushPreviews(app);
    node.rect={top:0,bottom:100,left:0,right:100,width:100,height:100};
    await flushPreviews(app);
  }
  assert.equal(app.preview.children.at(-1),image);
  assert.equal(app.run('created'),1);
  assert.equal(app.run('revoked.length'),0);
  assert.equal(app.preview.children[0].textContent,'');
});

test('restored image history reserves stable frames before any lazy decoding starts', async () => {
  const app=await previewClient();
  await app.run(`(async()=>{
    for(let i=0;i<8;i++) await appendMessage(7,{id:'restored-image-'+i,kind:'theirs',attachment:fixture},false);
  })()`);
  const frames=app.nodes.get('#message-list').children.map(node=>node.attachmentUi.preview);
  assert.equal(app.run('created'),0);
  for(const frame of frames) assert.match(frame.className,/media-frame/);
  await flushPreviews(app);
  assert.equal(app.run('created'),3);
  app.run('mediaPreviews.clear()');
  for(const frame of frames) assert.match(frame.className,/media-frame/);
});

test('nearby retention never prevents a newly visible image from getting a bounded slot', async () => {
  const app=await previewClient();
  await app.run(`(async()=>{
    for(let i=1;i<3;i++) await appendMessage(7,{id:'image-'+i,kind:'theirs',attachment:fixture},false);
  })()`);
  await flushPreviews(app);
  assert.equal(app.run('created'),3);
  for(const node of app.nodes.get('#message-list').children) {
    node.rect={top:101,bottom:201,left:0,right:100,width:100,height:100};
  }
  await app.run("appendMessage(7,{id:'new-visible',kind:'theirs',attachment:fixture},false)");
  await flushPreviews(app);
  const latest=app.nodes.get('#message-list').children.at(-1);
  assert.equal(latest.attachmentUi.preview.children.at(-1).tagName,'img');
  assert.equal(app.run('created-revoked.length'),3);
  assert.equal(app.run('created'),4);
});

test('loader remains until native decoding finishes, then the image replaces it', async () => {
  const app=await previewClient();
  const create=app.document.createElement;
  let finish;
  app.document.createElement=tag=>{
    const node=create(tag);
    if(tag==='img') node.decode=()=>new Promise(resolve=>{finish=resolve;});
    return node;
  };
  await flushPreviews(app);
  assert.match(app.preview.children[0].className,/media-loading/);
  assert.equal(app.preview.children.some(node=>node.tagName==='img'),false);
  finish();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(app.preview.children[0].textContent,'');
  assert.equal(app.preview.children.at(-1).tagName,'img');
  app.run('mediaPreviews.clear()');
});

test('unsupported files and failed image signatures cannot starve a valid automatic preview', async () => {
  const app=await previewClient();
  await app.run(`(async()=>{
    for(let i=0;i<3;i++) await appendMessage(7,{id:'pdf-'+i,kind:'theirs',
      attachment:SidewordProtocol.filePayload('synthetic.pdf','application/pdf',new Uint8Array())},false);
    for(let i=0;i<3;i++) await appendMessage(7,{id:'forged-'+i,kind:'theirs',
      attachment:SidewordProtocol.filePayload('synthetic.png','image/png',new Uint8Array([1,2]))},false);
  })()`);
  await flushPreviews(app);
  await flushPreviews(app);
  assert.equal(app.preview.children.at(-1).tagName,'img');
  assert.equal(app.run('created'),1);
  app.run('mediaPreviews.clear()');
});

for (const action of ['chat', 'removal', 'single-removal', 'hidden', 'pagehide', 'session']) {
  test(`rendered previews are reused and released on ${action}`, async () => {
    const app = await previewClient();
    assert.equal(app.run('created'), 0);
    app.run('scheduleVisiblePreviews()');
    for (const timer of app.timers.filter(timer => timer.delay === 0)) timer();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(app.run('created'), 1);
    assert.equal(app.preview.children.at(-1).tagName, 'img');
    app.run('renderMessages(); renderMessages();');
    assert.equal(app.run('created'), 1);
    if (action === 'chat') app.run('selectChat(null)');
    if (action === 'removal') app.run('messagesByChat.set(7, []); renderMessages();');
    if (action === 'single-removal') await app.run(`(async () => {
      await appendMessage(7, {id:'retained-text', kind:'theirs', text:'keep this'}, false);
      messagesByChat.set(7, messagesByChat.get(7).filter(entry => entry.id !== 'image'));
      renderMessages();
    })()`);
    if (action === 'hidden') {
      app.document.visibilityState = 'hidden';
      app.documentEvents.get('visibilitychange')();
    }
    if (action === 'pagehide') app.windowEvents.get('pagehide')();
    if (action === 'session') await app.run(`(async () => {
      writeIdentity = async () => { throw new Error('synthetic storage failure'); };
      try { await invalidateSession(); } catch {}
    })()`);
    assert.equal(app.run('revoked.length'), 1);
    assert.equal(app.preview.children.some(node => node.tagName === 'img'), false);
  });
}

test('image delivery persists and ACKs without starting a preview decoder', async () => {
  const app = await previewClient();
  await app.run(`(async () => {
    globalThis.savedImage = false; globalThis.acks = [];
    decryptMessage = async () => SidewordProtocol.encodePayload(SidewordProtocol.fileReference(
      fixture.name,fixture.mime,fixture.size,'incoming-image.media'));
    persistHistoryEntry = async (chat, entry) => {
      if (entry.attachment) savedImage = entry.attachment.type === 'attachment-ref' && entry.attachment.size === fixture.size;
    };
    api = async (path, options) => {
      if (!savedImage || created) throw new Error('Preview must not precede durable ACK');
      acks.push(JSON.parse(options.body));
    };
    await processIncoming({messages:[{chat_id:7, sender_public_id:'peer-a',
      client_message_id:'incoming-image', delivery_id:'a'.repeat(32),
      created_at:'2026-10-29T12:00:00Z'}]});
  })()`);
  assert.equal(app.run('savedImage'), true);
  assert.equal(app.run('created'), 0);
  assert.equal(app.run('acks[0].messages[0].delivery_id'), 'a'.repeat(32));
});

test('SVG bytes disguised as a PNG remain a download without an image URL', async () => {
  const app = await previewClient();
  app.run(`fixture.data = btoa('<svg onload="alert(1)"></svg>');
    fixture.size = atob(fixture.data).length;`);
  app.run('scheduleVisiblePreviews()');
  for (const timer of app.timers.filter(timer => timer.delay === 0)) timer();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(app.run('created'), 0);
  assert.equal(app.preview.children.some(node => node.tagName === 'img'), false);
  assert.equal(app.preview.children[0].textContent, '');
  assert.equal(app.nodes.get('#message-list').children[0].messageBubble.children
    .some(node => node.className === 'button subtle attachment-download'), true);
});
