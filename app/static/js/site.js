document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('[data-rotative]').forEach((section) => {
    const track = section.querySelector('[data-rotative-track]');
    const title = section.querySelector('.rotative-title');
    const panels = Array.from(section.querySelectorAll('.rotative-panel'));
    if (!track || !title || !panels.length) return;

    let index = 0;

    const syncTitle = () => {
      const panel = panels[index];
      if (panel) title.textContent = panel.dataset.title || 'Categoria';
    };

    const goTo = (nextIndex) => {
      index = (nextIndex + panels.length) % panels.length;
      const offset = panels[index].offsetLeft;
      track.scrollTo({ left: offset, behavior: 'smooth' });
      syncTitle();
    };

    section.querySelector('[data-rotative-prev]')?.addEventListener('click', () => goTo(index - 1));
    section.querySelector('[data-rotative-next]')?.addEventListener('click', () => goTo(index + 1));

    let snapTimeout;
    track.addEventListener('scroll', () => {
      clearTimeout(snapTimeout);
      snapTimeout = setTimeout(() => {
        const width = track.clientWidth || 1;
        index = Math.round(track.scrollLeft / width);
        syncTitle();
      }, 120);
    }, { passive: true });

    syncTitle();
  });

  const drawer = document.getElementById('mobileDrawer');
  const drawerBackdrop = document.querySelector('.mobile-drawer-backdrop');
  const menuOpenBtn = document.querySelector('[data-mobile-menu-open]');
  const menuCloseBtns = document.querySelectorAll('[data-mobile-menu-close]');

  const openDrawer = () => {
    if (!drawer || !drawerBackdrop) return;
    drawer.hidden = false;
    drawerBackdrop.hidden = false;
    requestAnimationFrame(() => {
      drawer.classList.add('is-open');
      drawerBackdrop.classList.add('is-open');
    });
    drawer.setAttribute('aria-hidden', 'false');
    menuOpenBtn?.setAttribute('aria-expanded', 'true');
    document.body.style.overflow = 'hidden';
  };

  const closeDrawer = () => {
    if (!drawer || !drawerBackdrop) return;
    drawer.classList.remove('is-open');
    drawerBackdrop.classList.remove('is-open');
    drawer.setAttribute('aria-hidden', 'true');
    menuOpenBtn?.setAttribute('aria-expanded', 'false');
    window.setTimeout(() => {
      drawer.hidden = true;
      drawerBackdrop.hidden = true;
    }, 240);
    document.body.style.overflow = '';
  };

  menuOpenBtn?.addEventListener('click', openDrawer);
  menuCloseBtns.forEach((btn) => btn.addEventListener('click', closeDrawer));
  drawer?.querySelectorAll('a').forEach((link) => link.addEventListener('click', closeDrawer));

  const pipRoot = document.getElementById('livePip');
  const pipWindow = document.getElementById('livePipWindow');
  const pipBody = document.getElementById('livePipBody');
  const pipHandle = document.getElementById('livePipHandle');
  const liveHost = document.getElementById('liveCamHost');
  const liveOpenBtns = document.querySelectorAll('[data-live-open]');
  const liveCloseBtn = document.querySelector('[data-live-close]');
  const liveResizeBtn = document.querySelector('[data-live-resize]');

  if (!liveHost || !pipRoot || !pipWindow || !pipBody) return;

  const livePlaceholder = document.createElement('div');
  livePlaceholder.style.display = 'none';
  liveHost.parentNode?.insertBefore(livePlaceholder, liveHost);

  const iconSwap = (expanded) => {
    const icon = liveResizeBtn?.querySelector('i');
    if (!icon) return;
    icon.className = expanded ? 'bi bi-arrows-angle-contract' : 'bi bi-arrows-angle-expand';
  };

  const openPip = () => {
    pipRoot.hidden = false;
    pipRoot.setAttribute('aria-hidden', 'false');
    pipBody.appendChild(liveHost);
    pipWindow.style.right = '14px';
    pipWindow.style.left = 'auto';
    pipWindow.style.bottom = '90px';
    pipWindow.style.top = 'auto';
  };

  const closePip = () => {
    if (livePlaceholder.parentNode) {
      livePlaceholder.parentNode.insertBefore(liveHost, livePlaceholder.nextSibling);
    }
    pipRoot.hidden = true;
    pipRoot.setAttribute('aria-hidden', 'true');
  };

  liveOpenBtns.forEach((btn) => btn.addEventListener('click', (event) => {
    event.preventDefault();
    openPip();
  }));

  liveCloseBtn?.addEventListener('click', closePip);

  liveResizeBtn?.addEventListener('click', () => {
    const expanded = pipWindow.classList.toggle('live-pip-window--large');
    iconSwap(expanded);
  });

  let dragState = null;

  const clamp = (value, min, max) => Math.min(Math.max(value, min), max);

  const startDrag = (clientX, clientY) => {
    const rect = pipWindow.getBoundingClientRect();
    pipWindow.classList.add('is-dragging');
    dragState = {
      startX: clientX,
      startY: clientY,
      left: rect.left,
      top: rect.top,
      width: rect.width,
      height: rect.height,
    };
    pipWindow.style.left = `${rect.left}px`;
    pipWindow.style.top = `${rect.top}px`;
    pipWindow.style.right = 'auto';
    pipWindow.style.bottom = 'auto';
  };

  const moveDrag = (clientX, clientY) => {
    if (!dragState) return;
    const margin = 8;
    const maxLeft = window.innerWidth - dragState.width - margin;
    const maxTop = window.innerHeight - dragState.height - margin;
    const nextLeft = clamp(dragState.left + (clientX - dragState.startX), margin, maxLeft);
    const nextTop = clamp(dragState.top + (clientY - dragState.startY), margin, maxTop);
    pipWindow.style.left = `${nextLeft}px`;
    pipWindow.style.top = `${nextTop}px`;
  };

  const endDrag = () => {
    pipWindow.classList.remove('is-dragging');
    dragState = null;
  };

  pipHandle?.addEventListener('pointerdown', (event) => {
    if (event.target.closest('button')) return;
    startDrag(event.clientX, event.clientY);
    pipHandle.setPointerCapture?.(event.pointerId);
  });

  window.addEventListener('pointermove', (event) => moveDrag(event.clientX, event.clientY));
  window.addEventListener('pointerup', endDrag);
  window.addEventListener('pointercancel', endDrag);

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      closeDrawer();
      closePip();
    }
  });
});
