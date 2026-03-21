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
});
