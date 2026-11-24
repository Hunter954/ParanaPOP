(() => {
  function controls(dialog) {
    let timer;
    const close = () => { if (dialog.open) dialog.close(); };
    dialog.querySelector('[data-popup-close]').addEventListener('click', close);
    dialog.addEventListener('click', event => { if (event.target === dialog) close(); });
    dialog.addEventListener('close', () => clearTimeout(timer));
    return function open(seconds, endsAt = Infinity) {
      if (dialog.open) return false;
      const deadline = Math.min(endsAt, seconds > 0 ? Date.now() + seconds * 1000 : Infinity);
      if (deadline <= Date.now()) return false;
      dialog.showModal();
      if (Number.isFinite(deadline)) {
        const schedule = () => {
          const remaining = deadline - Date.now();
          if (remaining <= 0) close();
          else timer = setTimeout(schedule, Math.min(remaining, 2147483647));
        };
        schedule();
      }
      return true;
    };
  }

  const banner = document.querySelector('[data-ad-popup]');
  if (banner && typeof banner.showModal === 'function') {
    const open = controls(banner);
    const sessionKey = 'paranapop.popup';
    const { image, seconds, frequency, revision, endsAt } = banner.dataset;
    const seen = () => {
      try { return frequency === 'session' && sessionStorage.getItem(sessionKey) === revision; }
      catch { return false; }
    };
    const deadline = endsAt ? Date.parse(endsAt) : Infinity;
    if (!seen() && Date.now() < deadline) {
      const art = banner.querySelector('img');
      art.addEventListener('load', () => {
        if (document.querySelector('dialog[open]') || seen() || !open(Number(seconds), deadline)) return;
        if (frequency === 'session') {
          try { sessionStorage.setItem(sessionKey, revision); } catch { /* Storage unavailable. */ }
        }
      }, { once: true });
      art.src = image;
    }
  }

  const preview = document.querySelector('[data-popup-preview]');
  if (!preview || typeof preview.showModal !== 'function') return;
  const open = controls(preview);
  const form = document.querySelector('[data-popup-form]');
  const thumbnail = document.querySelector('[data-popup-thumbnail]');
  const empty = document.querySelector('[data-popup-empty]');
  const trigger = document.querySelector('[data-popup-test]');
  const input = form.elements.image;
  let uploadURL;
  function update() {
    if (uploadURL) URL.revokeObjectURL(uploadURL);
    uploadURL = input.files.length ? URL.createObjectURL(input.files[0]) : '';
    const source = uploadURL || (!form.elements.remove_image?.checked ? thumbnail.dataset.currentImage : '');
    empty.hidden = Boolean(source);
    thumbnail.hidden = trigger.disabled = !source;
    if (source) thumbnail.src = source;
    else thumbnail.removeAttribute('src');
  }
  input.addEventListener('change', update);
  form.elements.remove_image?.addEventListener('change', update);
  trigger.addEventListener('click', () => {
    const art = preview.querySelector('img');
    art.src = thumbnail.src;
    art.decode().then(() => open(Math.min(300, Math.max(0, Number(form.elements.seconds.value))))).catch(() => {});
  });
  update();
})();
