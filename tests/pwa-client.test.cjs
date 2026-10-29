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

function widget({ ios = false, installed = false, push = true, saveFails = false, mode = 'standalone', legacyIOS = false, manifestStorage = new Map(), permission = 'default', existingSubscription = false, userAgent, serviceWorker = true, clipboard = true } = {}) {
  const elements = {};
  const events = {};
  const requests = [];
  const copiedLinks = [];
  const timers = [];
  let permissionRequests = 0;
  let manifestReplaced = 0;
  const manifestLink = { href: '/manifest.webmanifest', cloneNode() { return { href: this.href }; }, replaceWith(next) {
    assert.equal(next.href, '/manifest.webmanifest'); manifestReplaced++;
  } };
  const get = (id) => elements[id] ||= { hidden: true, textContent: '', innerHTML: '', disabled: false,
    classList: { toggle() {} },
    events: {}, addEventListener(name, handler) { this.events[name] = handler; },
    showModal() { this.open = true; }, close() { this.open = false; this.events.close?.(); } };
  let enabled = existingSubscription;
  const sub = { toJSON: () => ({ endpoint: 'test' }), unsubscribe: async () => { enabled = false; return true; } };
  const reg = { pushManager: { getSubscription: async () => enabled ? sub : null,
    subscribe: async () => { enabled = true; return sub; } } };
  const window = { isSecureContext: true, matchMedia: (query) => ({ matches: installed && query === `(display-mode: ${mode})` }),
    setTimeout: (fn, delay) => { timers.push({fn, delay}); return timers.length; },
    addEventListener: (name, handler) => { events[name] = handler; }, ...(push ? { PushManager: {} } : {}) };
  const context = { window, document: { getElementById: get, querySelector: () => manifestLink },
    localStorage: { getItem: (key) => manifestStorage.get(key), setItem: (key, value) => manifestStorage.set(key, value) },
    navigator: { standalone: legacyIOS, userAgent: userAgent || (ios ? 'Mozilla/5.0 (iPhone) Version/18.0 Mobile/15E148 Safari/604.1' : 'Chrome'), platform: ios ? 'iPhone' : 'Linux',
      serviceWorker: { register: async () => reg, ready: Promise.resolve(reg) } },
    Uint8Array, atob: (value) => Buffer.from(value, 'base64').toString('binary'),
    fetch: async (url, options) => { requests.push({url, options}); return { ok: !saveFails || url === '/api/pwa/config',
      json: async () => url === '/manifest.webmanifest' ? { id: '/', icons: [{src: '/static/pwa/icon-v3-512.png'}] } : { publicKey: 'YQ' } }; } };
  if (push) { context.Notification = { permission, requestPermission: async () => { permissionRequests++; context.Notification.permission = 'granted'; return 'granted'; } }; window.Notification = context.Notification; }
  if (!serviceWorker) delete context.navigator.serviceWorker;
  if (clipboard) context.navigator.clipboard = { writeText: async (value) => copiedLinks.push(value) };
  vm.runInNewContext(source, context);
  return { get, events, context, requests, manifestStorage, copiedLinks, manifestReplaced: () => manifestReplaced,
    permissionRequests: () => permissionRequests, runTimers: () => timers.splice(0).forEach((t) => t.fn()) };
}

test('first app opening offers news alerts after launch and requests native permission only on tap', async () => {
  const app = widget({ installed: true });
  await settle();
  assert.notEqual(app.get('pwa-dialog').open, true);
  app.runTimers();
  assert.equal(app.get('pwa-dialog').open, true);
  assert.equal(app.get('pwa-notify-later').hidden, false);
  assert.equal(app.permissionRequests(), 0);
  await app.get('pwa-notify').events.click();
  assert.equal(app.permissionRequests(), 1);
  assert.ok(app.requests.some((r) => r.url === '/api/pwa/subscribe'));
  assert.match(app.get('pwa-status').textContent, /receberá alertas/);
});

test('declining the first invitation does not ask permission and does not repeat next opening', async () => {
  const storage = new Map();
  const app = widget({ installed: true, manifestStorage: storage });
  await settle(); app.runTimers();
  app.get('pwa-notify-later').events.click();
  assert.equal(app.get('pwa-dialog').open, false);
  assert.equal(app.permissionRequests(), 0);
  const next = widget({ installed: true, manifestStorage: storage });
  await settle(); next.runTimers();
  assert.notEqual(next.get('pwa-dialog').open, true);
});

test('browser visitors, blocked readers and existing subscribers are not prompted automatically', async () => {
  for (const options of [{ installed: false }, { installed: true, permission: 'denied' },
    { installed: true, permission: 'granted', existingSubscription: true }]) {
    const app = widget(options);
    await settle(); app.runTimers();
    assert.notEqual(app.get('pwa-dialog').open, true);
    assert.equal(app.permissionRequests(), 0);
  }
});

test('the iPhone installed app also invites and requests permission directly in the tap handler', async () => {
  const app = widget({ ios: true, legacyIOS: true });
  await settle(); app.runTimers();
  assert.equal(app.get('pwa-dialog').open, true);
  assert.equal(app.permissionRequests(), 0);
  await app.get('pwa-notify').events.click();
  assert.equal(app.permissionRequests(), 1);
  assert.ok(app.requests.some((r) => r.url === '/api/pwa/subscribe'));
});

test('refreshes the old browser manifest cache once without changing app identity', async () => {
  const storage = new Map();
  const app = widget({ manifestStorage: storage });
  await settle();
  const request = app.requests.find((r) => r.url === '/manifest.webmanifest');
  assert.equal(request.options.cache, 'reload');
  assert.equal(app.manifestReplaced(), 1);
  const nextVisit = widget({ manifestStorage: storage });
  await settle();
  assert.equal(nextVisit.requests.some((r) => r.url === '/manifest.webmanifest'), false);
});

test('failed refresh remains retryable and does not block reading or installation', async () => {
  const app = widget({ saveFails: true });
  await settle();
  assert.equal(app.manifestReplaced(), 0);
  assert.equal(app.manifestStorage.size, 0);
  assert.equal(app.get('pwa-open').hidden, false);
});

test('iOS without Notification API still offers install guidance without crashing', async () => {
  const app = widget({ ios: true, push: false });
  await settle();
  assert.equal(app.get('pwa-open').hidden, false);
  app.get('pwa-open').events.click();
  assert.match(app.get('pwa-instructions').innerHTML, /Adicionar à Tela de Início/);
  assert.equal(app.get('pwa-alerts').hidden, true);
});

test('iPhone first tap immediately shows the Home Screen steps, with no false download button', async () => {
  const app = widget({ ios: true, push: false });
  await settle();
  app.get('pwa-open').events.click();
  assert.equal(app.get('pwa-dialog').open, true);
  assert.equal(app.get('pwa-instructions').hidden, false);
  assert.equal(app.get('pwa-install').hidden, true);
  assert.match(app.get('pwa-instructions').innerHTML, /Compartilhar/);
  assert.match(app.get('pwa-instructions').innerHTML, /Abrir como App da Web/);
  assert.match(app.get('pwa-instructions').innerHTML, /Editar Ações/);
  assert.equal(app.get('pwa-copy-link').hidden, true);
});

test('iPhone embedded browsers without service workers still provide Safari help and copyable link', async () => {
  for (const userAgent of ['Mozilla/5.0 (iPhone) Instagram', 'Mozilla/5.0 (iPhone) AppleWebKit/605.1.15 Mobile/15E148']) {
    const app = widget({ ios: true, push: false, serviceWorker: false, userAgent });
    await settle();
    assert.equal(app.get('pwa-open').hidden, false);
    app.get('pwa-open').events.click();
    assert.match(app.get('pwa-title').textContent, /Safari/);
    assert.equal(app.get('pwa-copy-link').hidden, false);
    await app.get('pwa-copy-link').events.click();
    assert.deepEqual(app.copiedLinks, ['https://paranapop.com.br/']);
  }
});

test('iPhone copy fallback exposes the address when clipboard is unavailable', async () => {
  const app = widget({ ios: true, push: false, clipboard: false, userAgent: 'iPhone WhatsApp' });
  await settle(); app.get('pwa-open').events.click();
  await app.get('pwa-copy-link').events.click();
  assert.match(app.get('pwa-status').textContent, /paranapop.com.br/);
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
