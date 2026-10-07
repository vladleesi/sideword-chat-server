const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('docs/theme.js', 'utf8');
const storageKey = 'sideword-site-theme';

function page({ dark = false, storage = new Map(), readBlocked = false,
  writeBlocked = false, loading = true, mediaAvailable = true } = {}) {
  const buttonEvents = new Map();
  const documentEvents = new Map();
  const mediaEvents = new Map();
  const root = { dataset: {} };
  const metadata = [{ content: '#F1F0EA' }, { content: '#111210' }];
  const button = { hidden: true, attributes: new Map(),
    setAttribute(name, value) { this.attributes.set(name, value); },
    addEventListener(name, handler) { buttonEvents.set(name, handler); },
  };
  const document = { documentElement: root, readyState: loading ? 'loading' : 'complete',
    querySelectorAll: () => metadata,
    querySelector: () => document.readyState === 'loading' ? null : button,
    addEventListener(name, handler) { documentEvents.set(name, handler); },
  };
  const media = { matches: dark,
    addEventListener(name, handler) { mediaEvents.set(name, handler); },
  };
  const window = { localStorage: {
    getItem(key) {
      if (readBlocked) throw new Error('Storage blocked');
      return storage.get(key) ?? null;
    },
    setItem(key, value) {
      if (writeBlocked) throw new Error('Storage full or blocked');
      storage.set(key, value);
    },
  } };
  if (mediaAvailable) window.matchMedia = () => media;
  vm.runInNewContext(source, { document, window });
  return { root, button, metadata, storage,
    ready() {
      document.readyState = 'complete';
      documentEvents.get('DOMContentLoaded')?.();
      documentEvents.delete('DOMContentLoaded');
    },
    click() { buttonEvents.get('click')(); },
    systemChange(dark) {
      media.matches = dark;
      mediaEvents.get('change')?.({ matches: dark });
    },
  };
}

test('system theme is applied before DOM ready and the native toggle is progressively enabled', () => {
  for (const dark of [false, true]) {
    const app = page({ dark });
    assert.equal(app.root.dataset.theme, dark ? 'dark' : 'light');
    assert.equal(app.button.hidden, true);
    assert.equal(app.storage.size, 0);
    app.ready();
    assert.equal(app.button.hidden, false);
    assert.equal(app.button.textContent, dark ? 'Light' : 'Dark');
    assert.equal(app.button.attributes.get('aria-label'), `Switch to ${dark ? 'light' : 'dark'} theme`);
    assert.equal(app.button.title, `Switch to ${dark ? 'light' : 'dark'} theme`);
    assert.equal(app.metadata.every(meta => meta.content === (dark ? '#111210' : '#F1F0EA')), true);
  }
});

test('saved light and dark preferences override the opposite system preference', () => {
  for (const preference of ['light', 'dark']) {
    const storage = new Map([[storageKey, preference]]);
    const app = page({ storage, dark: preference === 'light', loading: false });
    assert.equal(app.root.dataset.theme, preference);
    assert.equal(app.button.textContent, preference === 'dark' ? 'Light' : 'Dark');
    app.systemChange(preference !== 'dark');
    assert.equal(app.root.dataset.theme, preference);
  }
});

test('clicking toggles immediately, updates browser metadata and survives reload', () => {
  const app = page({ loading: false });
  app.click();
  assert.equal(app.root.dataset.theme, 'dark');
  assert.equal(app.button.textContent, 'Light');
  assert.equal(app.button.attributes.get('aria-label'), 'Switch to light theme');
  assert.equal(app.metadata.every(meta => meta.content === '#111210'), true);
  assert.equal(app.storage.get(storageKey), 'dark');
  const reloaded = page({ storage: app.storage, loading: false });
  assert.equal(reloaded.root.dataset.theme, 'dark');
  reloaded.click();
  assert.equal(reloaded.root.dataset.theme, 'light');
  assert.equal(reloaded.button.textContent, 'Dark');
  assert.equal(reloaded.button.attributes.get('aria-label'), 'Switch to dark theme');
  assert.equal(reloaded.metadata.every(meta => meta.content === '#F1F0EA'), true);
  assert.equal(page({ storage: app.storage, dark: true }).root.dataset.theme, 'light');
});

test('system changes are followed until the user explicitly chooses a theme', () => {
  const app = page({ loading: false });
  app.systemChange(true);
  assert.equal(app.root.dataset.theme, 'dark');
  app.systemChange(false);
  assert.equal(app.root.dataset.theme, 'light');
  app.click();
  app.systemChange(false);
  assert.equal(app.root.dataset.theme, 'dark');
  app.click();
  app.systemChange(true);
  assert.equal(app.root.dataset.theme, 'light');
});

test('unrecognized stored values fall back to the system without disabling switching', () => {
  const app = page({ storage: new Map([[storageKey, 'unexpected']]), dark: true, loading: false });
  assert.equal(app.root.dataset.theme, 'dark');
  app.systemChange(false);
  assert.equal(app.root.dataset.theme, 'light');
  app.click();
  assert.equal(app.storage.get(storageKey), 'dark');
});

test('blocked storage reads or writes cannot break switching or revert a manual choice', () => {
  for (const options of [{ readBlocked: true, writeBlocked: true }, { writeBlocked: true }]) {
    const app = page({ ...options, loading: false });
    app.click();
    assert.equal(app.root.dataset.theme, 'dark');
    assert.equal(app.button.textContent, 'Light');
    assert.equal(app.storage.size, 0);
    app.systemChange(false);
    assert.equal(app.root.dataset.theme, 'dark');
    app.click();
    assert.equal(app.root.dataset.theme, 'light');
  }
});

test('without a system-theme API the control still switches from the light default', () => {
  const app = page({ mediaAvailable: false, loading: false });
  assert.equal(app.root.dataset.theme, 'light');
  app.click();
  assert.equal(app.root.dataset.theme, 'dark');
});

test('HTML applies the theme before styles and provides a native labeled toggle', () => {
  const html = fs.readFileSync('docs/index.html', 'utf8');
  const script = /<script\b[^>]*src="\.\/theme\.js[^\"]*"[^>]*>/.exec(html);
  assert.ok(script);
  assert.equal(/\b(?:async|defer)\b/.test(script[0]), false);
  assert.ok(script.index < html.indexOf('<link rel="stylesheet"'));
  const button = /<button\b[^>]*id="theme-toggle"[^>]*>/.exec(html);
  assert.ok(button);
  assert.match(button[0], /type="button"/);
  assert.match(button[0], /aria-label="Switch to dark theme"/);
  assert.doesNotMatch(button[0], /aria-pressed=/);
  assert.match(button[0], /\bhidden\b/);
});
