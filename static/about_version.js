/* Installed files can advance while a training service remains running. */
(async () => {
  const status = document.getElementById('version-status');
  if (!status) return;
  const running = status.dataset.runningVersion;
  try {
    const response = await fetch('/static/version.json', {cache: 'no-store', credentials: 'same-origin'});
    if (!response.ok) throw new Error('Version unavailable');
    const {version} = await response.json();
    if (typeof version !== 'string' || !/^\d+\.\d+\.\d+$/.test(version)) throw new Error('Invalid version');
    document.getElementById('installed-version').textContent = `Installed v${version}`;
    document.getElementById('about-version').textContent = `VAOS v${version} (installed)`;
    status.textContent = running === version ? `Installed and running: v${version}.` : `Installed: v${version} · Running service: v${running}. Restart VAOS after active jobs finish to load the installed version.`;
  } catch (_) {
    status.textContent = `Running service: v${running}. Installed version could not be checked. Reload this page to retry.`;
  }
})();
