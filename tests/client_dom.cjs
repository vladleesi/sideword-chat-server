const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

// A DOM adapter tests state transitions without browser access.
function client({ mobile = false, clipboardBlocked = false, viewportAware = false,
  navigationState = null, observeVisibility = false, locale, timeZone } = {}) {
  const nodes = new Map();
  let document;
  class Node {
    constructor(tag = 'div') {
      this.tagName = tag;
      this.children = [];
      this.events = new Map();
      this.attributes = new Map();
      this.dataset = {};
      this.style = {};
      this.hidden = false;
      this.scrollTop = 0;
      this.clientHeight = 100;
      this.classList = { toggle: (name, value) => this.attributes.set(name, value) };
    }
    getBoundingClientRect() { return this.rect || (this.className === 'message-bubble' && this.parent?.rect) || {left:0, right:100, top:0, bottom:100, width:100, height:100}; }
    get firstChild() { return this.children[0]; }
    get scrollHeight() { return this.children.length * 100; }
    get textContent() { return this.text || this.children.map(node => node.textContent).join(''); }
    set textContent(value) { this.text = value; this.replaceChildren(); }
    addEventListener(name, handler) { const previous = this.events.get(name); this.events.set(name, previous ? event => { previous(event); return handler(event); } : handler); }
    emit(name, event = {}) {
      const result = this.events.get(name)?.(event);
      // Emulate the native dialog cancel default without a browser.
      if (name === 'cancel' && this.open && !event.defaultPrevented) this.close();
      return result;
    }
    setAttribute(name, value) {
      this.attributes.set(name, value);
      if (name === 'data-mobile-view') this.dataset.mobileView = value;
    }
    append(...nodes) { for (const node of nodes) this.insertBefore(node, null); }
    replaceChildren(...nodes) {
      for (const child of [...this.children]) child.remove();
      this.append(...nodes);
    }
    insertBefore(node, reference) {
      node.remove();
      this.children.splice(reference ? this.children.indexOf(reference) : this.children.length, 0, node);
      node.parent = this;
    }
    remove() {
      if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1);
      this.parent = null;
    }
    focus(options) { document.activeElement = this; this.focusOptions = options; }
    querySelector(selector) {
      for (const child of this.children) {
        if (selector === '.active' && child.className?.split(' ').includes('active')) return child;
        const match = child.querySelector(selector);
        if (match) return match;
      }
      return null;
    }
    showModal() { this.open = true; }
    close() { this.open = false; this.emit('close'); }
    requestSubmit(button) { this.submittedWith = button; }
  }
  const documentEvents = new Map();
  const windowEvents = new Map();
  const cancelledTimers = new Set();
  let visibilityObserver;
  document = {
    visibilityState: "visible", hasFocus: () => document.focused !== false,
    querySelector(selector) {
      if (!nodes.has(selector)) nodes.set(selector, new Node());
      return nodes.get(selector);
    },
    createElement: tag => new Node(tag), createElementNS: (_, tag) => new Node(tag), addEventListener: (event, handler) => documentEvents.set(event, handler),
    createRange: () => ({ selectNodeContents(node) { this.value = node.textContent; } }),
  };
  const timers = [];
  const copied = [];
  const selection = { toString: () => selection.selectedText || "", removeAllRanges() {}, addRange(range) { this.value = range.value; } };
  const confirmations = [];
  const viewportEvents = new Map();
  const properties = new Map();
  document.documentElement = { style: { setProperty: (key, value) => properties.set(key, value) } };
  const window = {
    innerWidth: 1000, innerHeight: 800,
    addEventListener: (event, handler) => windowEvents.set(event, handler), matchMedia: () => ({ matches: mobile }),
    setTimeout: (handler, delay) => { timers.push(handler); handler.delay = delay; return timers.length; },
    clearTimeout: id => cancelledTimers.add(id),
    setInterval() {}, clearInterval() {},
    getSelection: () => selection,
    confirm: message => { confirmations.push(message); return false; },
    location: { assign: path => { window.destination = path; } },
    visualViewport: { height: 800, scale: 1,
      addEventListener: (event, handler) => viewportEvents.set(event, handler) },
  };
  let resizeMessages;
  const history = { state: navigationState,
    replaceState(state) { this.state = structuredClone(state); },
  };
  const context = vm.createContext({ TextEncoder, TextDecoder, DOMException, AbortController,
    Headers,
    performance: { now: () => window.now || 0 },
    Intl: { DateTimeFormat: function (_, options) {
      return new Intl.DateTimeFormat(locale, { ...options, ...(timeZone ? {timeZone} : {}) });
    } },
    crypto: webcrypto, atob, btoa, document, window, history,
    ResizeObserver: class {
      constructor(handler) { resizeMessages = handler; }
      observe() {}
    },
    navigator: { clipboard: { writeText: async value => {
      if (clipboardBlocked) throw new Error('denied');
      copied.push(value);
    } } },
  });
  if (observeVisibility) context.IntersectionObserver = class {
    constructor(handler) { visibilityObserver = handler; }
    observe() {} unobserve() {} disconnect() {}
  };
  vm.runInContext(fs.readFileSync('app/static/client-protocol.js', 'utf8'), context);
  vm.runInContext(fs.readFileSync('app/static/client.js', 'utf8')
    .replace(/start\(\)\.catch[^\n]+/, ''), context);
  const run = code => vm.runInContext(code, context);
  if (viewportAware) {
    const list = nodes.get('#message-list');
    const panel = nodes.get('#client-panel');
    let top = 0, height = 100;
    const visible = () => !panel.hidden && (!mobile || panel.dataset.mobileView === 'conversation');
    Object.defineProperties(list, {
      clientHeight: { get: () => visible() ? height : 0, set: value => { height = value; } },
      scrollHeight: { get: () => visible() ? list.children.length * 100 : 0 },
      scrollTop: { get: () => visible() ? top : 0,
        set: value => { top = Math.max(0, Math.min(value, list.scrollHeight - list.clientHeight)); } },
    });
  }
  run(`messageConfirmationsSupported = true; identity = { publicId: 'me', token: 'not-for-display', privateKey: 'never-display',
    refreshCredential: 'never-display-refresh' };
    chats = [{ id: 7, title: '<script>chat</script>', chat_type: 'group', participants: [
      { public_id: 'me', display_name: 'Alex', local_fingerprint: '01:'.repeat(31) + '01' },
      { public_id: 'peer-a', display_name: 'Alex', local_fingerprint: 'ab:'.repeat(31) + 'cd' },
      { public_id: 'peer-b', display_name: '<b>Alex</b>' },
    ] }];
    elements.clientPanel.dataset.mobileView = 'chats';
    selectChat(7);`);
  return { run, nodes, document, window, timers, copied, selection, confirmations,
    resizeMessages, viewportEvents, properties, history, documentEvents, windowEvents,
    intersect: (node, visible = true) => visibilityObserver([{target: node, isIntersecting: visible}]),
    fireViews: async () => {
      for (let index = 0; index < timers.length; index++) {
        if (timers[index].delay === 750 && !cancelledTimers.has(index + 1)) {
          cancelledTimers.add(index + 1); timers[index]();
        }
      }
      await run('inboundQueue');
    } };
}

module.exports = { client };
