/* Shared admin release notice. No installation, credentials or local data sent to GitHub. */
(() => {
  async function check(manual = false) {
    const status = document.getElementById('release-check-status');
    const button = document.getElementById('check-releases');
    if (button) button.disabled = true;
    if (manual && status) status.textContent = 'Checking published releases…';
    try {
      const response = await fetch('/api/admin/releases', {credentials:'same-origin', cache:'no-store', signal:AbortSignal.timeout(6000)});
      if (!response.ok) throw Error('Check unavailable');
      const data = await response.json();
      const messages = {ok:'No newer stable release is available.', disabled:'Automatic update checks are disabled.', no_release:'No published stable release is available yet.', unavailable:'Could not reach GitHub. VAOS continues normally; try again later.'};
      if (status) status.textContent = data.update_available ? `VAOS ${data.latest} is available.` : messages[data.status] || messages.unavailable;
      if (!data.update_available || !/^\d+\.\d+\.\d+$/.test(data.latest)) return;
      const key = `vaos-dismiss-release-${data.latest}`;
      try { if (!manual && sessionStorage.getItem(key)) return; } catch (_) {}
      document.getElementById('release-update-banner')?.remove();
      const banner = document.createElement('section'); banner.id='release-update-banner'; banner.className='release-update-banner'; banner.setAttribute('aria-label','Software update');
      const text = document.createElement('span'); text.textContent=`VAOS ${data.latest} is available. `;
      const link = document.createElement('a'); link.href='https://github.com/indipranisha/VAOS/releases'; link.target='_blank'; link.rel='noopener noreferrer'; link.textContent='View release and update instructions ↗';
      const note = document.createElement('small'); note.textContent='Manual installation · finish active jobs and preserve local changes before updating.';
      const dismiss = document.createElement('button'); dismiss.type='button'; dismiss.textContent='Dismiss'; dismiss.addEventListener('click',()=>{banner.remove();try{sessionStorage.setItem(key,'1');}catch(_){}});
      banner.append(text,link,note,dismiss); document.querySelector('main')?.prepend(banner);
    } catch (_) { if (status) status.textContent='Update checks are unavailable in this running service. Restart after active jobs finish, or view GitHub release notes.'; }
    finally { if (button) button.disabled=false; }
  }
  document.getElementById('check-releases')?.addEventListener('click',()=>check(true));
  check();
})();
