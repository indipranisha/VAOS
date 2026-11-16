(() => {
  const key = 'vaosTheme';
  const button = document.getElementById('theme-toggle');
  const label = document.getElementById('theme-toggle-label');
  if (!button || !label) return;
  const apply = (mode) => {
    const light = mode === 'light';
    document.body.classList.toggle('theme-light', light);
    document.body.classList.toggle('vaos-dark', !light);
    button.setAttribute('aria-pressed', String(light));
    label.textContent = light ? 'Dark mode' : 'Light mode';
    button.title = `Switch to ${light ? 'dark' : 'light'} mode`;
    localStorage.setItem(key, light ? 'light' : 'dark');
  };
  apply(localStorage.getItem(key) || localStorage.getItem('vaosTrainingTheme') || 'dark');
  button.addEventListener('click', () => apply(document.body.classList.contains('theme-light') ? 'dark' : 'light'));
})();
