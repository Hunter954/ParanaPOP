document.addEventListener('DOMContentLoaded', () => {
  const mobileMenuDrawer = document.getElementById('mobileMenuDrawer');
  const mobileMenuOverlay = document.querySelector('.mobile-menu-overlay');
  const mobileMenuToggle = document.querySelector('[data-mobile-menu-toggle]');
  const mobileMenuClosers = document.querySelectorAll('[data-mobile-menu-close]');

  if (mobileMenuDrawer && mobileMenuOverlay && mobileMenuToggle) {
    const closeMobileMenu = () => {
      document.body.classList.remove('mobile-menu-open');
      mobileMenuDrawer.classList.remove('is-open');
      mobileMenuOverlay.classList.remove('is-open');
      mobileMenuOverlay.hidden = true;
      mobileMenuDrawer.setAttribute('aria-hidden', 'true');
      mobileMenuToggle.setAttribute('aria-expanded', 'false');
    };

    const openMobileMenu = () => {
      document.body.classList.add('mobile-menu-open');
      mobileMenuDrawer.classList.add('is-open');
      mobileMenuOverlay.classList.add('is-open');
      mobileMenuOverlay.hidden = false;
      mobileMenuDrawer.setAttribute('aria-hidden', 'false');
      mobileMenuToggle.setAttribute('aria-expanded', 'true');
    };

    mobileMenuToggle.addEventListener('click', () => {
      if (mobileMenuDrawer.classList.contains('is-open')) closeMobileMenu();
      else openMobileMenu();
    });

    mobileMenuClosers.forEach((button) => {
      button.addEventListener('click', closeMobileMenu);
    });

    mobileMenuDrawer.querySelectorAll('a').forEach((link) => {
      link.addEventListener('click', closeMobileMenu);
    });

    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') closeMobileMenu();
    });
  }

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
});
