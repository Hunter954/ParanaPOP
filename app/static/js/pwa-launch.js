// Run in the head: detect the installed app before the first page paint.
(() => {
  const root = document.documentElement;
  const appMode = ['standalone', 'fullscreen', 'minimal-ui'].some((mode) =>
    window.matchMedia(`(display-mode: ${mode})`).matches) || navigator.standalone === true;
  if (!appMode) return;
  root.classList.add('pwa-app');
  let firstPage = true;
  try {
    firstPage = sessionStorage.getItem('paranapop-app-session-v2') !== '1';
    sessionStorage.setItem('paranapop-app-session-v2', '1');
  } catch (_) {
    // Storage can be unavailable. Avoid replaying on internal news links.
    try { firstPage = !document.referrer || new URL(document.referrer).origin !== location.origin; } catch (_) { /* First entry. */ }
  }
  if (firstPage) root.classList.add('pwa-launching');
})();
