// Run with: node --test tests/pwa-client.test.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../app/static/js/pwa.js'), 'utf8');
const swSource = fs.readFileSync(path.join(__dirname, '../app/static/pwa/sw.js'), 'utf8');
const launchSource = fs.readFileSync(path.join(__dirname, '../app/static/js/pwa-launch.js'), 'utf8');
const settle = () => new Promise((resolve) => setImmediate(resolve));

function widget({ ios = false, installed = false, push = true, saveFails = false, mode = 'standalone', legacyIOS = false } = {}) {
  const elements = {};
  const events = {};
  const get = (id) => elements[id] ||= { hidden: true, textContent: '', innerHTML: '', disabled: false,
    classList: { toggle() {} },
    events: {}, addEventListener(name, handler) { this.events[name] = handler; },
    showModal() { this.open = true; }, close() { this.open = false; } };
  let enabled = false;
  const sub = { toJSON: () => ({ endpoint: 'test' }), unsubscribe: async () => { enabled = false; return true; } };
  const reg = { pushManager: { getSubscription: async () => enabled ? sub : null,
    subscribe: async () => { enabled = true; return sub; } } };
  const window = { isSecureContext: true, matchMedia: (query) => ({ matches: installed && query === `(display-mode: ${mode})` }),
    addEventListener: (name, handler) => { events[name] = handler; }, ...(push ? { PushManager: {} } : {}) };
  const context = { window, document: { getElementById: get },
    navigator: { standalone: legacyIOS, userAgent: ios ? 'iPhone' : 'Chrome', platform: ios ? 'iPhone' : 'Linux',
      serviceWorker: { register: async () => reg, ready: Promise.resolve(reg) } },
    Uint8Array, atob: (value) => Buffer.from(value, 'base64').toString('binary'),
    fetch: async (url) => ({ ok: !saveFails || url === '/api/pwa/config', json: async () => ({ publicKey: 'YQ' }) }) };
  if (push) { context.Notification = { permission: 'default', requestPermission: async () => 'granted' }; window.Notification = context.Notification; }
  vm.runInNewContext(source, context);
  return { get, events, context };
}

test('iOS without Notification API still offers install guidance without crashing', async () => {
  const app = widget({ ios: true, push: false });
  await settle();
  assert.equal(app.get('pwa-open').hidden, false);
  await app.get('pwa-install').events.click();
  assert.match(app.get('pwa-instructions').innerHTML, /Adicionar à Tela de Início/);
  assert.equal(app.get('pwa-alerts').hidden, true);
});

test('native install prompt is triggered from the install button once', async () => {
  const app = widget();
  let prompted = 0;
  app.events.beforeinstallprompt({ preventDefault() {}, prompt: async () => { prompted++; }, userChoice: Promise.resolve({ outcome: 'accepted' }) });
  await app.get('pwa-install').events.click();
  assert.equal(prompted, 1);
  await app.get('pwa-install').events.click();
  assert.equal(prompted, 1);
  assert.equal(app.get('pwa-instructions').hidden, false);
});

test('notification opt-in and opt-out report confirmed server state', async () => {
  const app = widget({ installed: true });
  await settle();
  assert.equal(app.get('pwa-install').hidden, true);
  assert.equal(app.get('pwa-open').hidden, true);
  assert.equal(app.get('pwa-manage-notifications').hidden, false);
  app.get('pwa-manage-notifications').events.click();
  assert.equal(app.get('pwa-dialog').open, true);
  assert.equal(app.get('pwa-notify').disabled, false);
  await app.get('pwa-notify').events.click();
  assert.match(app.get('pwa-notify').textContent, /Desativar/);
  await app.get('pwa-notify').events.click();
  assert.match(app.get('pwa-notify').textContent, /Ativar/);
});

for (const mode of ['standalone', 'fullscreen', 'minimal-ui']) {
  test(`installed ${mode} app never shows the download launcher`, async () => {
    const app = widget({ installed: true, mode });
    await settle();
    assert.equal(app.get('pwa-open').hidden, true);
    assert.equal(app.get('pwa-install').hidden, true);
    app.events.beforeinstallprompt({ preventDefault() {} });
    assert.equal(app.get('pwa-open').hidden, true);
  });
}

test('iOS legacy standalone hides install even without Push API', async () => {
  const app = widget({ ios: true, legacyIOS: true, push: false });
  await settle();
  assert.equal(app.get('pwa-open').hidden, true);
  assert.equal(app.get('pwa-install').hidden, true);
});

test('installation completion removes the floating button', async () => {
  const app = widget();
  app.events.appinstalled();
  await settle();
  assert.equal(app.get('pwa-open').hidden, true);
});

test('logo opening plays once per app session and never in the browser', () => {
  const storage = new Map();
  function run(installed) {
    const classes = new Set();
    vm.runInNewContext(launchSource, {
      window: { matchMedia: (query) => ({ matches: installed && query === '(display-mode: standalone)' }) },
      navigator: {}, document: { documentElement: { classList: { add: (value) => classes.add(value) } } },
      sessionStorage: { getItem: (key) => storage.get(key), setItem: (key, value) => storage.set(key, value) },
    });
    return classes;
  }
  assert.equal(run(false).size, 0);
  assert.equal(run(true).has('pwa-launching'), true);
  const nextArticle = run(true);
  assert.equal(nextArticle.has('pwa-app'), true);
  assert.equal(nextArticle.has('pwa-launching'), false);
});

test('failed server subscription remains retryable', async () => {
  const app = widget({ saveFails: true });
  await settle();
  await app.get('pwa-notify').events.click();
  assert.match(app.get('pwa-notify').textContent, /Ativar/);
  assert.match(app.get('pwa-status').textContent, /Não foi possível/);
});

function worker() {
  const events = {};
  const notices = [];
  const self = { location: { origin: 'https://paranapop.com.br' },
    addEventListener: (name, fn) => { events[name] = fn; },
    registration: { showNotification: async (title, options) => { notices.push({ title, ...options }); } } };
  vm.runInNewContext(swSource, { self, URL, caches: {}, fetch() {} });
  return { events, notices };
}

test('service worker does not intercept admin, APIs or media', () => {
  const app = worker();
  for (const pathname of ['/admin/login', '/api/pwa/config', '/media/photo.jpg', '/geradorpop']) {
    let intercepted = false;
    app.events.fetch({ request: { url: `https://paranapop.com.br${pathname}`, method: 'GET', mode: 'navigate' }, respondWith: () => { intercepted = true; } });
    assert.equal(intercepted, false);
  }
});

test('offline screen logo is available in the service worker offline cache', () => {
  const html = fs.readFileSync(path.join(__dirname, '../app/static/pwa/offline.html'), 'utf8');
  const logo = html.match(/<img src="([^"]+)"/)[1];
  const assets = vm.runInNewContext(swSource + '\nASSETS;', {
    self: { addEventListener() {} }, URL, caches: {}, fetch() {},
  });
  assert.ok(assets.includes(logo));
});

test('push notifications restrict destinations to the portal', async () => {
  const app = worker();
  let wait;
  app.events.push({ data: { json: () => ({ title: 'Notícia', url: 'https://evil.test' }) }, waitUntil: (promise) => { wait = promise; } });
  await wait;
  assert.equal(app.notices[0].data.url, 'https://paranapop.com.br/');
  app.events.push({ data: { json: () => ({ url: '/p/nova-noticia' }) }, waitUntil: (promise) => { wait = promise; } });
  await wait;
  assert.equal(app.notices[1].data.url, 'https://paranapop.com.br/p/nova-noticia');
});
