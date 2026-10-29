(() => {
  'use strict';
  let installPrompt = null;
  let installed = window.matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
  const ios = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const inApp = /Instagram|FBAN|FBAV|; wv\)/i.test(navigator.userAgent);
  const fab = document.getElementById('pwa-open');
  const dialog = document.getElementById('pwa-dialog');
  if (!fab || !dialog || !window.isSecureContext || !('serviceWorker' in navigator)) return;
  const install = document.getElementById('pwa-install');
  const notify = document.getElementById('pwa-notify');
  const alerts = document.getElementById('pwa-alerts');
  const instructions = document.getElementById('pwa-instructions');
  const status = document.getElementById('pwa-status');
  const canPush = 'PushManager' in window && 'Notification' in window && (!ios || installed);
  let subscription = null;
  let ready = false;
  const registration = navigator.serviceWorker.register('/sw.js', { scope: '/', updateViaCache: 'none' })
    .then(() => navigator.serviceWorker.ready);
  registration.catch(() => { status.textContent = 'Não foi possível preparar o aplicativo. Atualize a página e tente novamente.'; });
  function updateUI() {
    fab.hidden = installed && !canPush;
    document.getElementById('pwa-fab-label').textContent = installed ? 'Notificações' : 'Instalar aplicativo';
    install.hidden = installed;
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
    fab.hidden = installed && !canPush;
    document.getElementById('pwa-fab-label').textContent = installed ? 'Notificações' : 'Instalar aplicativo';
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
    }).catch(() => { status.textContent = 'Não foi possível conectar os alertas. Atualize a página para tentar novamente.'; });
    notify.addEventListener('click', async () => {
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
        }
      } catch (_) {
        status.textContent = 'Não foi possível atualizar seus alertas. Verifique a conexão e tente novamente.';
      } finally { refresh(); }
    });
  }
  refresh();
})();
