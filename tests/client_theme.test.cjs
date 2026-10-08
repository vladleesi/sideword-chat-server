const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/static/appearance.js', 'utf8');
const storageKey = 'sideword-client-theme';

function page({ dark = false, saved, blocked = false, loading = true, mediaAvailable = true } = {}) {
  const storage = new Map(saved === undefined ? [] : [[storageKey, saved]]);
  const documentEvents = new Map(), windowEvents = new Map(), mediaEvents = new Map();
  const root = { dataset: {} };
  const controls = ['light', 'dark', 'system'].map(value => ({
    value, checked: value === 'system', events: new Map(),
    addEventListener(name, handler) { this.events.set(name, handler); },
  }));
  const document = { documentElement: root, readyState: loading ? 'loading' : 'complete',
    querySelectorAll: () => controls,
    addEventListener: (name, handler) => documentEvents.set(name, handler),
  };
  const media = { matches: dark, addEventListener: (name, handler) => mediaEvents.set(name, handler) };
  const window = { addEventListener: (name, handler) => windowEvents.set(name, handler),
    localStorage: {
      getItem(key) { if (blocked) throw new Error('denied'); return storage.get(key); },
      setItem(key, value) { if (blocked) throw new Error('denied'); storage.set(key, value); },
    },
  };
  if (mediaAvailable) window.matchMedia = () => media;
  vm.runInNewContext(source, { document, window });
  return { root, controls, storage,
    ready() { document.readyState = 'complete'; documentEvents.get('DOMContentLoaded')?.(); },
    choose(value) {
      const control = controls.find(control => control.value === value);
      control.checked = true;
      control.events.get('change')();
    },
    systemChange(dark) { media.matches = dark; mediaEvents.get('change')?.({ matches: dark }); },
    storageChange(key, newValue) { windowEvents.get('storage')({ key, newValue }); },
  };
}

test('system preference is applied before DOM ready without writing local data', () => {
  for (const dark of [false, true]) {
    const app = page({ dark });
    assert.equal(app.root.dataset.theme, dark ? 'dark' : 'light');
    assert.equal(app.storage.size, 0);
    app.ready();
    assert.equal(app.controls.find(control => control.checked).value, 'system');
    app.systemChange(!dark);
    assert.equal(app.root.dataset.theme, dark ? 'light' : 'dark');
  }
});

test('all appearance choices apply immediately, persist only preference and survive reload', () => {
  const app = page({ loading: false, dark: true });
  for (const preference of ['light', 'dark', 'system']) {
    app.choose(preference);
    assert.equal(app.root.dataset.theme, preference === 'system' ? 'dark' : preference);
    assert.deepEqual([...app.storage], [[storageKey, preference]]);
    assert.equal(app.controls.filter(control => control.checked).length, 1);
    assert.equal(app.controls.find(control => control.checked).value, preference);
    const restored = page({ dark: true, saved: app.storage.get(storageKey), loading: false });
    assert.equal(restored.root.dataset.theme, app.root.dataset.theme);
    assert.equal(restored.controls.find(control => control.checked).value, preference);
  }
});

test('explicit light and dark ignore OS changes until System is selected', () => {
  for (const preference of ['light', 'dark']) {
    const app = page({ saved: preference, dark: preference === 'light', loading: false });
    assert.equal(app.root.dataset.theme, preference);
    app.systemChange(true);
    assert.equal(app.root.dataset.theme, preference);
    app.choose('system');
    assert.equal(app.root.dataset.theme, 'dark');
    app.systemChange(false);
    assert.equal(app.root.dataset.theme, 'light');
  }
});

test('invalid stored preferences and unavailable media use safe defaults', () => {
  assert.equal(page({ saved: 'credential-or-invalid', dark: true }).root.dataset.theme, 'dark');
  const app = page({ mediaAvailable: false, loading: false });
  assert.equal(app.root.dataset.theme, 'light');
  app.choose('dark');
  assert.equal(app.root.dataset.theme, 'dark');
});

test('blocked storage still permits runtime switching and system changes', () => {
  const app = page({ blocked: true, loading: false });
  app.choose('dark');
  assert.equal(app.root.dataset.theme, 'dark');
  assert.equal(app.storage.size, 0);
  app.choose('system');
  app.systemChange(true);
  assert.equal(app.root.dataset.theme, 'dark');
});

test('appearance stays synchronized across tabs and ignores unrelated storage events', () => {
  const app = page({ dark: true, loading: false });
  app.storageChange(storageKey, 'light');
  assert.equal(app.root.dataset.theme, 'light');
  assert.equal(app.controls[0].checked, true);
  app.storageChange('unrelated', 'dark');
  assert.equal(app.root.dataset.theme, 'light');
  app.storageChange(storageKey, null);
  assert.equal(app.root.dataset.theme, 'dark');
  assert.equal(app.controls[2].checked, true);
  app.systemChange(false);
  assert.equal(app.root.dataset.theme, 'light');
});
