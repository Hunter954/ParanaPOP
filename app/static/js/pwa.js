(() => {
  'use strict';
  let installPrompt = null;
  let installed = ['standalone', 'fullscreen', 'minimal-ui'].some((mode) => window.matchMedia(`(display-mode: ${mode})`).matches) || navigator.standalone === true;
  const openedAsApp = installed;
  const ios = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const inApp = /Instagram|FBAN|FBAV|; wv\)/i.test(navigator.userAgent);
  const fab = document.getElementById('pwa-open');
  const dialog = document.getElementById('pwa-dialog');
  if (!fab || !dialog || !window.isSecureContext || !('serviceWorker' in navigator)) return;
  // Old installations may still have the original max-age=3600 manifest in
  // Chrome's HTTP cache. Bypass that cached copy and replace it, keeping the
  // manifest URL and app identity stable. Do not clear user data/subscriptions.
  (async () => {
    const refreshKey = 'paranapop-manifest-refreshed-v4';
    try { if (localStorage.getItem(refreshKey) === '1') return; } catch (_) { /* Storage is optional. */ }
    const response = await fetch('/manifest.webmanifest', { cache: 'reload', credentials: 'same-origin' });
    if (!response.ok) return;
    const manifest = await response.json();
    if (manifest.id !== '/' || !Array.isArray(manifest.icons) || !manifest.icons.some((icon) => icon.src === '/static/pwa/icon-v3-512.png')) return;
    const link = document.querySelector('link[rel="manifest"]');
    // Reattach the same link so Chrome processes the refreshed metadata too.
    if (link) link.replaceWith(link.cloneNode());
    try { localStorage.setItem(refreshKey, '1'); } catch (_) { /* Retry safely on a later visit. */ }
  })().catch(() => { /* Offline/error: keep the reader working and retry next visit. */ });
  const install = document.getElementById('pwa-install');
  const notify = document.getElementById('pwa-notify');
  const alerts = document.getElementById('pwa-alerts');
  const instructions = document.getElementById('pwa-instructions');
  const status = document.getElementById('pwa-status');
  const manage = document.getElementById('pwa-manage-notifications');
  const later = document.getElementById('pwa-notify-later');
  const consentKey = 'paranapop-news-consent-v1';
  let welcome = false;
  function rememberConsentInvite() {
    try { localStorage.setItem(consentKey, '1'); } catch (_) { /* Browser privacy mode may disable storage. */ }
  }
  const canPush = 'PushManager' in window && 'Notification' in window && (!ios || installed);
  let subscription = null;
  let ready = false;
  const registration = navigator.serviceWorker.register('/sw.js', { scope: '/', updateViaCache: 'none' })
    .then(() => navigator.serviceWorker.ready);
  registration.catch(() => { status.textContent = 'Não foi possível preparar o aplicativo. Atualize a página e tente novamente.'; });
  function updateUI() {
    alerts.hidden = !canPush;
    notify.disabled = !ready || Notification.permission === 'denied';
    notify.textContent = subscription ? 'Desativar notificações' : 'Ativar notificações';
    if (canPush && Notification.permission === 'denied') {
      document.getElementById('pwa-alerts-description').textContent = 'As notificações estão bloqueadas. Para ativar, permita os alertas nas configurações deste site no seu navegador ou celular.';
    }
  }
  // Access Notification only on platforms where the API exists.
  if (!('Notification' in window)) alerts.hidden = true;
  function refresh() {
    fab.hidden = installed;
    if (manage) manage.hidden = !installed || !canPush;
    dialog.classList.toggle('pwa-dialog-installed', installed);
    if (installed) {
      document.getElementById('pwa-title').textContent = welcome ? 'Fique por dentro das notícias' : 'Suas notificações';
      document.getElementById('pwa-description').textContent = welcome ? 'Quer saber quando sair uma notícia nova? Ative os alertas do Paraná Pop neste celular.' : 'Escolha se deseja receber alertas das notícias do Paraná Pop neste aparelho.';
      instructions.hidden = true;
    }
    if (later) later.hidden = !welcome;
    install.hidden = installed;
    if (canPush) updateUI();
  }
  function showInstructions() {
    instructions.hidden = false;
    if (inApp) {
      instructions.innerHTML = '<strong>Abra no navegador do celular</strong><p>Use o menu deste aplicativo e escolha “Abrir no navegador”. Depois, volte a tocar em Instalar aplicativo.</p>';
    } else if (ios) {
      instructions.innerHTML = '<strong>Instale no seu iPhone ou iPad</strong><ol><li>Toque em <strong>Compartilhar</strong> no navegador.</li><li>Escolha <strong>Adicionar à Tela de Início</strong> e confirme em <strong>Adicionar</strong>.</li><li>Abra o Paraná Pop pelo novo ícone para ativar as notificações.</li></ol>';
    } else {
      instructions.innerHTML = '<strong>Adicione o Paraná Pop à sua tela</strong><ol><li>Abra o menu do navegador <strong>⋮</strong> ou <strong>⋯</strong>.</li><li>Escolha <strong>Instalar aplicativo</strong> ou <strong>Adicionar à tela inicial</strong>.</li><li>Confirme a instalação. Se a opção não aparecer, abra este site no Chrome ou Edge atualizado.</li></ol>';
    }
  }
  window.addEventListener('beforeinstallprompt', (event) => {
    event.preventDefault();
    installPrompt = event;
    refresh();
  });
  window.addEventListener('appinstalled', () => {
    installed = true; installPrompt = null; instructions.hidden = true;
    status.textContent = 'Aplicativo instalado! Abra o Paraná Pop pelo ícone na sua tela inicial.';
    refresh();
  });
  fab.addEventListener('click', () => { dialog.showModal(); });
  if (manage) manage.addEventListener('click', () => { rememberConsentInvite(); welcome = false; refresh(); dialog.showModal(); });
  if (later) later.addEventListener('click', () => { rememberConsentInvite(); welcome = false; refresh(); dialog.close(); });
  dialog.addEventListener('close', () => { welcome = false; if (later) later.hidden = true; });
  document.getElementById('pwa-close').addEventListener('click', () => dialog.close());
  dialog.addEventListener('click', (event) => { if (event.target === dialog) {
    const rect = dialog.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
  } });
  install.addEventListener('click', async () => {
    if (!installPrompt) { showInstructions(); return; }
    const prompt = installPrompt; installPrompt = null;
    try {
      await prompt.prompt();
      const choice = await prompt.userChoice;
      if (choice.outcome === 'dismissed') status.textContent = 'Tudo bem! Você pode instalar quando quiser.';
    } catch (_) { showInstructions(); }
  });
  async function saveSubscription(path, value) {
    const response = await fetch(path, { method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-PWA-Request': '1' }, body: JSON.stringify(value.toJSON()) });
    if (!response.ok) throw new Error('subscription failed');
  }
  if (canPush) {
    registration.then(async (reg) => {
      subscription = await reg.pushManager.getSubscription();
      // Reconcile registrations after a temporary server/network failure.
      if (subscription) await saveSubscription('/api/pwa/subscribe', subscription);
      ready = true; refresh();
      if (openedAsApp && !subscription && Notification.permission !== 'denied') {
        // Invite once, after the launch animation. The native permission dialog
        // still requires the reader to tap the activation button (including iOS).
        window.setTimeout(() => {
          try { if (localStorage.getItem(consentKey) === '1') return; } catch (_) { /* Best effort. */ }
          if (dialog.open || subscription || Notification.permission === 'denied') return;
          welcome = true; refresh(); dialog.showModal(); rememberConsentInvite();
        }, 1300);
      }
    }).catch(() => { status.textContent = 'Não foi possível conectar os alertas. Atualize a página para tentar novamente.'; });
    notify.addEventListener('click', async () => {
      rememberConsentInvite();
      notify.disabled = true; status.textContent = '';
      try {
        if (subscription) {
          await saveSubscription('/api/pwa/unsubscribe', subscription);
          await subscription.unsubscribe(); subscription = null;
          status.textContent = 'Notificações desativadas neste aparelho.';
        } else {
          // Request synchronously in the click gesture, as required by iOS.
          const permission = await Notification.requestPermission();
          if (permission !== 'granted') {
            status.textContent = 'Os alertas não foram ativados. Você pode continuar lendo normalmente.';
            return;
          }
          const reg = await registration;
          const response = await fetch('/api/pwa/config', { cache: 'no-store' });
          if (!response.ok) throw new Error('config failed');
          const { publicKey } = await response.json();
          const key = Uint8Array.from(atob(publicKey.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - publicKey.length % 4) % 4)), (c) => c.charCodeAt(0));
          const candidate = await reg.pushManager.getSubscription() || await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
          await saveSubscription('/api/pwa/subscribe', candidate);
          subscription = candidate;
          status.textContent = 'Pronto! Você receberá alertas das próximas notícias publicadas.';
          welcome = false;
        }
      } catch (_) {
        status.textContent = 'Não foi possível atualizar seus alertas. Verifique a conexão e tente novamente.';
      } finally { refresh(); }
    });
  }
  refresh();
})();
