(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  let previewFile = null;

  async function request(url, options = {}) {
    const response = await fetch(url, {credentials: 'same-origin', ...options});
    const contentType = response.headers.get('content-type') || '';
    const payload = contentType.includes('application/json') ? await response.json() : await response.text();
    if (!response.ok) {
      const detail = payload && typeof payload === 'object' ? payload.detail : payload;
      const error = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail || `Request failed (${response.status})`));
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function summarizePreview(preview) {
    const settingRows = Object.entries(preview.engine_settings || {}).map(([key, value]) => `<li><strong>${escapeHtml(key.replaceAll('_',' '))}</strong> · ${escapeHtml(value)}</li>`).join('');
    const datasetRows = (preview.dataset_references || []).map(item => `<li>${escapeHtml(item.name)} · ${escapeHtml(item.format)} · ${Number(item.train_count||0).toLocaleString()} train / ${Number(item.val_count||0).toLocaleString()} validation · ${Number(item.class_count||0)} classes</li>`).join('') || '<li>No dataset references</li>';
    const modelRows = (preview.model_references || []).map(item => `<li>${escapeHtml(item.name)} · ${escapeHtml(item.kind)}</li>`).join('') || '<li>No model references</li>';
    const missing = [...(preview.missing_dataset_ids||[]).map(id=>`dataset ${id}`),...(preview.missing_model_ids||[]).map(id=>`model ${id}`)];
    return `<h3>${escapeHtml(preview.name)}</h3><p>${escapeHtml(preview.description||'No description')}</p><div class="project-ref-list"><strong>Settings that can be applied</strong><ul>${settingRows}</ul><strong>Dataset references (not included)</strong><ul>${datasetRows}</ul><strong>Model references (weights not included)</strong><ul>${modelRows}</ul>${missing.length?`<strong>Not found on this VAOS installation</strong><ul>${missing.map(item=>`<li>${escapeHtml(item)}</li>`).join('')}</ul>`:''}</div><p>${escapeHtml(preview.apply_note||'Only listed settings can be applied. Resource references are informational.')}</p>`;
  }

  async function refreshBundles() {
    const list = $('project-list');
    list.innerHTML = '<p class="helper">Loading saved bundles…</p>';
    try {
      const data = await request('/api/admin/projects');
      $('project-count').textContent = `${Number(data.count||0)} saved`;
      if (!data.bundles?.length) {
        list.innerHTML = '<div class="empty-state">No saved project bundles yet. Export this setup or preview a bundle from another VAOS installation.</div>';
        return;
      }
      list.innerHTML = data.bundles.map(bundle => `<article class="project-bundle-row" data-bundle-id="${escapeHtml(bundle.id)}"><div class="project-row-head"><div><h3>${escapeHtml(bundle.name)}</h3><p>${escapeHtml(bundle.description||'Configuration bundle')} · VAOS ${escapeHtml(bundle.producer_version)} · ${Number(bundle.bytes||0).toLocaleString()} bytes</p></div><div class="inline-actions"><button class="button button-secondary button-small" type="button" data-project-review>Review settings</button><button class="button button-danger button-small" type="button" data-project-delete>Remove bundle</button></div></div><div class="project-saved-preview hidden" aria-live="polite"></div><div class="inline-actions"><button class="button button-primary button-small hidden" type="button" data-project-apply disabled>Apply reviewed settings</button></div></article>`).join('');
    } catch (error) {
      list.innerHTML = `<div class="empty-state">Could not load saved bundles: ${escapeHtml(error.message)}</div>`;
    }
  }

  $('project-export')?.addEventListener('click', async () => {
    const status = $('project-export-status');
    status.textContent = 'Preparing a configuration-only bundle…';
    try {
      const name = $('project-export-name').value.trim() || 'VAOS project';
      const response = await fetch(`/api/admin/projects/export?name=${encodeURIComponent(name)}`, {credentials:'same-origin'});
      if (!response.ok) {
        let message = `Request failed (${response.status})`;
        try { const body = await response.json(); message = body.detail || message; } catch (_) {}
        throw new Error(message);
      }
      const blob = await response.blob();
      const disposition = response.headers.get('content-disposition') || '';
      const filename = disposition.match(/filename="([^"]+)"/)?.[1] || 'vaos-project.vaosproject';
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a'); link.href = url; link.download = filename; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      status.textContent = 'Bundle downloaded. It contains settings and local resource names only.';
    } catch (error) { status.textContent = `Export failed: ${error.message}`; }
  });

  $('project-import-form')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const file = $('project-file').files?.[0];
    const status = $('project-import-status');
    const preview = $('project-preview');
    const save = $('project-save-import');
    if (!file) { status.textContent = 'Choose a .vaosproject file first.'; return; }
    status.textContent = 'Checking the bundle without changing settings…';
    save.disabled = true; save.classList.add('hidden'); preview.classList.add('hidden'); previewFile = null;
    try {
      const form = new FormData(); form.append('file', file, file.name);
      const result = await request('/api/admin/projects/import?dry_run=true', {method:'POST', body:form});
      preview.innerHTML = summarizePreview(result.preview);
      preview.classList.remove('hidden');
      previewFile = file; save.disabled = false; save.classList.remove('hidden');
      status.textContent = `Preview passed · ${result.manifest.producer_version} · ${file.size.toLocaleString()} bytes. No files or settings changed.`;
    } catch (error) { status.textContent = `Could not preview this bundle: ${error.message}`; }
  });

  $('project-save-import')?.addEventListener('click', async () => {
    if (!previewFile) return;
    const status = $('project-import-status');
    const form = new FormData(); form.append('file', previewFile, previewFile.name);
    status.textContent = 'Saving the reviewed configuration bundle…';
    try {
      const result = await request('/api/admin/projects/import?dry_run=false', {method:'POST', body:form});
      status.textContent = result.message;
      previewFile = null; $('project-save-import').disabled = true; $('project-save-import').classList.add('hidden');
      $('project-import-form').reset(); await refreshBundles();
    } catch (error) { status.textContent = `Bundle was not saved: ${error.message}`; }
  });

  $('project-refresh')?.addEventListener('click', refreshBundles);
  $('project-list')?.addEventListener('click', async (event) => {
    const row = event.target.closest('[data-bundle-id]');
    if (!row) return;
    const id = row.dataset.bundleId;
    if (event.target.closest('[data-project-review]')) {
      const target = row.querySelector('.project-saved-preview');
      const apply = row.querySelector('[data-project-apply]');
      target.classList.remove('hidden'); target.innerHTML = '<p class="helper">Checking the saved bundle…</p>';
      apply.disabled = true; apply.classList.add('hidden');
      try {
        const data = await request(`/api/admin/projects/${encodeURIComponent(id)}`);
        target.innerHTML = summarizePreview(data.preview);
        apply.disabled = false; apply.classList.remove('hidden');
      } catch (error) { target.innerHTML = `<p class="error">Could not read this bundle: ${escapeHtml(error.message)}</p>`; }
      return;
    }
    if (event.target.closest('[data-project-apply]')) {
      if (!confirm('Apply only the engine settings shown in the reviewed bundle? This will not choose or download datasets, change the live model, or start training.')) return;
      const button = event.target.closest('[data-project-apply]'); button.disabled = true;
      try {
        const result = await request(`/api/admin/projects/${encodeURIComponent(id)}/apply`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirm:true})});
        alert(`${result.message}\n\nMissing local dataset references: ${result.missing_dataset_ids.length}\nMissing local model references: ${result.missing_model_ids.length}`);
      } catch (error) { alert(`Settings were not applied: ${error.message}`); button.disabled = false; }
      return;
    }
    if (event.target.closest('[data-project-delete]')) {
      if (!confirm('Remove this saved configuration bundle from this VAOS installation? Referenced dataset folders and model files will not be deleted.')) return;
      try { const result = await request(`/api/admin/projects/${encodeURIComponent(id)}`, {method:'DELETE'}); await refreshBundles(); alert(result.message); }
      catch (error) { alert(`Bundle was not removed: ${error.message}`); }
    }
  });

  refreshBundles();
})();
