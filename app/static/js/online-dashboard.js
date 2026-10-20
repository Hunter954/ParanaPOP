(() => {
  const panel = document.querySelector('[data-online-url]');
  if (!panel) return;
  const count = document.getElementById('online-count');
  const status = document.getElementById('online-status');
  let pending = false;
  const refresh = async () => {
    if (pending || document.visibilityState === 'hidden') return;
    pending = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(panel.dataset.onlineUrl, {
        credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
      });
      if (!response.ok) throw new Error('unavailable');
      const data = await response.json();
      if (!Number.isInteger(data.online) || data.online < 0) throw new Error('invalid');
      count.textContent = data.online.toLocaleString('pt-BR');
      status.textContent = `Atualizado às ${new Date().toLocaleTimeString('pt-BR')} · atualização a cada 15 segundos`;
    } catch (_) {
      count.textContent = '—';
      status.textContent = 'Sem conexão com o contador. Tentando novamente…';
    } finally {
      clearTimeout(timeout);
      pending = false;
    }
  };
  refresh();
  setInterval(refresh, 15000);
  document.addEventListener('visibilitychange', refresh);
})();
