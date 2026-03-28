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

  const body = document.body;
  const menuToggle = document.querySelector('[data-mobile-menu-toggle]');
  const drawer = document.getElementById('mobileMenuDrawer');
  const menuCloser = document.querySelectorAll('[data-mobile-menu-close]');

  const closeMenu = () => {
    body.classList.remove('mobile-menu-open');
    if (drawer) drawer.setAttribute('aria-hidden', 'true');
    if (menuToggle) menuToggle.setAttribute('aria-expanded', 'false');
  };

  menuToggle?.addEventListener('click', () => {
    const willOpen = !body.classList.contains('mobile-menu-open');
    body.classList.toggle('mobile-menu-open', willOpen);
    if (drawer) drawer.setAttribute('aria-hidden', String(!willOpen));
    menuToggle.setAttribute('aria-expanded', String(willOpen));
  });

  menuCloser.forEach((item) => item.addEventListener('click', closeMenu));
  drawer?.querySelectorAll('a').forEach((link) => link.addEventListener('click', closeMenu));

  const pipRoot = document.getElementById('livecamPip');
  const pipWindow = pipRoot?.querySelector('[data-livecam-window]');
  const dragBar = pipRoot?.querySelector('[data-livecam-drag]');
  const openButtons = Array.from(document.querySelectorAll('[data-livecam-open]'));
  const closeBtn = pipRoot?.querySelector('[data-livecam-close]');
  const sizeBtn = pipRoot?.querySelector('[data-livecam-size]');
  const video = document.getElementById('livecam');
  const clockEl = document.getElementById('liveclock');
  const locBox = document.getElementById('live-loc');
  const locText = document.getElementById('loc-text');

  if (pipRoot && pipWindow && dragBar && video) {
    const STREAM_URL = 'https://video04.logicahost.com.br/portovelhomamore/fozpontedaamizadesentidobrasil.stream/playlist.m3u8';
    const TZ = 'America/Sao_Paulo';
    const locations = ['Ponte Internacional da Amizade', 'Ciudad Del Este - PY'];

    let posX = window.innerWidth <= 767 ? 14 : Math.max(12, window.innerWidth - 438);
    let posY = window.innerWidth <= 767 ? 94 : 108;
    let activePointerId = null;
    let startX = 0;
    let startY = 0;
    let originX = posX;
    let originY = posY;
    let liveHls = null;
    let liveInitialized = false;
    let rotationIndex = 0;
    let locationTimer = null;

    const ensurePlay = () => {
      video.play().catch(() => {});
    };

    const tickClock = () => {
      if (!clockEl) return;
      try {
        clockEl.textContent = new Intl.DateTimeFormat('pt-BR', {
          timeZone: TZ,
          hour12: false,
          year: 'numeric',
          month: '2-digit',
          day: '2-digit',
          hour: '2-digit',
          minute: '2-digit',
          second: '2-digit'
        }).format(new Date());
      } catch (error) {
        const now = new Date();
        const pad = (value) => String(value).padStart(2, '0');
        clockEl.textContent = `${pad(now.getDate())}/${pad(now.getMonth() + 1)}/${now.getFullYear()} ${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
      }
    };

    const startLocationRotation = () => {
      if (!locBox || !locText || locationTimer) return;
      locationTimer = window.setInterval(() => {
        locBox.classList.add('fade');
        window.setTimeout(() => {
          rotationIndex = (rotationIndex + 1) % locations.length;
          locText.textContent = locations[rotationIndex];
          locBox.classList.remove('fade');
        }, 450);
      }, 5000);
    };

    const initLivecam = () => {
      if (liveInitialized) {
        ensurePlay();
        return;
      }

      tickClock();
      window.setInterval(tickClock, 1000);
      startLocationRotation();

      document.addEventListener('click', ensurePlay, { once: true });
      document.addEventListener('touchstart', ensurePlay, { once: true });

      if (window.Hls && window.Hls.isSupported()) {
        liveHls = new window.Hls({ enableWorker: true });
        liveHls.loadSource(STREAM_URL);
        liveHls.attachMedia(video);
        liveHls.on(window.Hls.Events.MEDIA_ATTACHED, ensurePlay);
      } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
        video.src = STREAM_URL;
        video.addEventListener('canplay', ensurePlay, { once: true });
      }

      liveInitialized = true;
    };

    const clamp = () => {
      const maxX = Math.max(12, window.innerWidth - pipWindow.offsetWidth - 12);
      const maxY = Math.max(76, window.innerHeight - pipWindow.offsetHeight - 12);
      posX = Math.min(Math.max(12, posX), maxX);
      posY = Math.min(Math.max(76, posY), maxY);
    };

    const paint = () => {
      clamp();
      pipWindow.style.left = `${posX}px`;
      pipWindow.style.top = `${posY}px`;
    };

    const openPip = () => {
      initLivecam();
      pipRoot.hidden = false;
      pipRoot.setAttribute('aria-hidden', 'false');
      paint();
      ensurePlay();
    };

    const closePip = () => {
      pipRoot.hidden = true;
      pipRoot.setAttribute('aria-hidden', 'true');
      video.pause();
    };

    openButtons.forEach((button) => button.addEventListener('click', (event) => {
      event.preventDefault();
      openPip();
    }));
    closeBtn?.addEventListener('click', closePip);
    sizeBtn?.addEventListener('click', () => {
      pipWindow.classList.toggle('is-large');
      paint();
    });

    dragBar.addEventListener('pointerdown', (event) => {
      if (event.target.closest('button')) return;
      activePointerId = event.pointerId;
      startX = event.clientX;
      startY = event.clientY;
      originX = posX;
      originY = posY;
      dragBar.setPointerCapture(activePointerId);
    });

    dragBar.addEventListener('pointermove', (event) => {
      if (activePointerId !== event.pointerId) return;
      posX = originX + (event.clientX - startX);
      posY = originY + (event.clientY - startY);
      paint();
    });

    const releaseDrag = (event) => {
      if (activePointerId !== event.pointerId) return;
      try {
        dragBar.releasePointerCapture(activePointerId);
      } catch (error) {
        // ignore
      }
      activePointerId = null;
    };

    dragBar.addEventListener('pointerup', releaseDrag);
    dragBar.addEventListener('pointercancel', releaseDrag);
    window.addEventListener('resize', paint);
    paint();
  }
});


(function () {
  const cfg = window.__SITE_ANALYTICS__;
  if (!cfg || !cfg.endpoint || !window.fetch || !window.localStorage || !window.sessionStorage) return;

  const uid = () => `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;
  const visitorKey = 'pp_visitor_id';
  const sessionKey = 'pp_session_id';
  const startedAtKey = 'pp_session_started_at';

  let visitorId = localStorage.getItem(visitorKey);
  let isNewUser = false;
  if (!visitorId) {
    visitorId = uid();
    localStorage.setItem(visitorKey, visitorId);
    isNewUser = true;
  }

  let sessionId = sessionStorage.getItem(sessionKey);
  if (!sessionId) {
    sessionId = uid();
    sessionStorage.setItem(sessionKey, sessionId);
    sessionStorage.setItem(startedAtKey, String(Date.now()));
  }

  const send = (eventName, durationSeconds = 0) => {
    const payload = {
      event: eventName,
      session_id: sessionId,
      visitor_id: visitorId,
      page_path: cfg.path || window.location.pathname,
      referrer: document.referrer || '',
      duration_seconds: durationSeconds,
      is_new_user: isNewUser,
    };
    const body = JSON.stringify(payload);
    if (navigator.sendBeacon && eventName === 'heartbeat') {
      navigator.sendBeacon(cfg.endpoint, new Blob([body], { type: 'application/json' }));
      return;
    }
    fetch(cfg.endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body,
      keepalive: true,
      credentials: 'same-origin',
    }).catch(() => {});
  };

  send('pageview', 0);

  const flushDuration = () => {
    const startedAt = parseInt(sessionStorage.getItem(startedAtKey) || String(Date.now()), 10);
    const seconds = Math.max(0, Math.round((Date.now() - startedAt) / 1000));
    send('heartbeat', seconds);
  };

  window.addEventListener('beforeunload', flushDuration);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') flushDuration();
  });
})();


(function(){
  const roots=document.querySelectorAll('[data-guide-search]');
  roots.forEach((root)=>{
    const endpoint=root.getAttribute('data-autocomplete-url');
    const input=root.querySelector('[data-guide-input]');
    const results=root.querySelector('[data-guide-results]');
    if(!endpoint || !input || !results || !window.fetch) return;
    let timer=null;
    const render=(items)=>{
      if(!items.length){results.hidden=true;results.innerHTML='';return;}
      results.innerHTML=items.map((item)=>`<a href="${item.url}" class="guide-autocomplete-item"><strong>${item.name}</strong><span>${item.category}${item.address ? ' • ' + item.address : ''}</span></a>`).join('');
      results.hidden=false;
    };
    input.addEventListener('input',()=>{
      const q=input.value.trim();
      clearTimeout(timer);
      if(q.length<2){results.hidden=true;return;}
      timer=setTimeout(()=>{
        fetch(`${endpoint}?q=${encodeURIComponent(q)}`)
          .then((r)=>r.json())
          .then((data)=>render(Array.isArray(data.items)?data.items:[]))
          .catch(()=>{results.hidden=true;});
      },180);
    });
    document.addEventListener('click',(event)=>{if(!root.contains(event.target)){results.hidden=true;}});
    input.addEventListener('focus',()=>{if(results.children.length){results.hidden=false;}});
  });
})();
