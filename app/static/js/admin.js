(() => {
  const sidebar = document.getElementById('adminSidebar');
  const openBtn = document.querySelector('[data-admin-sidebar-open]');
  const closeBtns = document.querySelectorAll('[data-admin-sidebar-close]');
  const overlay = document.querySelector('.admin-overlay');

  const openSidebar = () => {
    if (!sidebar) return;
    sidebar.classList.add('is-open');
    overlay?.classList.add('is-open');
  };

  const closeSidebar = () => {
    sidebar?.classList.remove('is-open');
    overlay?.classList.remove('is-open');
  };

  openBtn?.addEventListener('click', openSidebar);
  closeBtns.forEach((btn) => btn.addEventListener('click', closeSidebar));
})();
