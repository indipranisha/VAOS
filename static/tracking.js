(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[char]));
  let rules = [];
  let editingId = null;
  let nextBeforeId = null;
  let eventBusy = false;

  async function api(path, options = {}) {
    const response = await fetch(path, {
      credentials: 'same-origin', cache: 'no-store',
      ...options,
      headers: { ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...(options.headers || {}) },
    });
    const text = await response.text();
    let body = {};
    try { body = text ? JSON.parse(text) : {}; } catch { body = { detail: text }; }
    if (!response.ok) throw new Error(body.detail || `Request failed (${response.status}).`);
    return body;
  }

  function message(text, isError = false) {
    $('tracking-message').textContent = text;
    $('tracking-message').classList.toggle('error', isError);
  }

  function fillCameraSelect(select, cameras) {
    const current = select.value;
    select.innerHTML = '<option value="">All active cameras</option>' + cameras.map((camera) =>
      `<option value="${escapeHtml(camera.token)}">${escapeHtml(camera.name || 'Camera')}</option>`).join('');
    if ([...select.options].some((option) => option.value === current)) select.value = current;
  }

  async function loadCameras() {
    const cameras = await api('/api/admin/cameras');
    const active = Array.isArray(cameras) ? cameras.filter((camera) => camera.active) : [];
    fillCameraSelect($('tracking-camera'), active);
    fillCameraSelect($('tracking-event-camera'), active);
    return active;
  }

  function ruleSummary(rule) {
    const region = rule.region || { x: 0, y: 0, width: 1, height: 1 };
    const coverage = `${Math.round(region.x * 100)}–${Math.round((region.x + region.width) * 100)}% wide · ${Math.round(region.y * 100)}–${Math.round((region.y + region.height) * 100)}% high`;
    const trigger = rule.rule_type === 'dwell_in_region'
      ? `Remains for ${Number(rule.dwell_seconds).toLocaleString()} sec`
      : `At least ${rule.threshold_count} in the region`;
    return `${trigger} · ${Math.round(Number(rule.min_confidence) * 100)}% confidence · ${rule.min_consecutive_frames} confirming frames · ${rule.cooldown_seconds} sec cooldown · ${coverage}`;
  }

  function renderRules() {
    $('tracking-rule-count').textContent = `${rules.length} / 100 rules`;
    $('tracking-rules').innerHTML = rules.length ? rules.map((rule) => `
      <article class="tracking-rule">
        <div class="tracking-card-head"><div><h3>${escapeHtml(rule.name)}</h3><p class="tracking-meta">${escapeHtml(rule.class_name)} · ${escapeHtml(rule.camera_name || 'All active cameras')} · ${rule.enabled ? 'Enabled' : 'Paused'}</p></div><span class="subtle-tag">${rule.rule_type === 'dwell_in_region' ? 'Dwell alert' : 'Count alert'}</span></div>
        <p class="tracking-meta">${escapeHtml(ruleSummary(rule))}</p>
        <div class="tracking-rule-actions"><button class="button button-secondary button-small" type="button" data-rule-action="toggle" data-rule-id="${Number(rule.id)}">${rule.enabled ? 'Pause' : 'Enable'}</button><button class="button button-secondary button-small" type="button" data-rule-action="edit" data-rule-id="${Number(rule.id)}">Edit</button><button class="button button-secondary button-small" type="button" data-rule-action="delete" data-rule-id="${Number(rule.id)}">Delete</button></div>
      </article>`).join('') : '<div class="empty-state">No rules saved. Create a rule to start tracking counts or dwell time.</div>';
    document.querySelectorAll('[data-rule-action]').forEach((button) => button.addEventListener('click', () => handleRuleAction(button)));
  }

  async function loadRules() {
    const result = await api('/api/admin/tracking/rules');
    rules = Array.isArray(result.rules) ? result.rules : [];
    renderRules();
  }

  function showTypeFields() {
    const dwell = $('tracking-type').value === 'dwell_in_region';
    $('tracking-dwell-field').classList.toggle('hidden', !dwell);
    $('tracking-count-field').classList.toggle('hidden', dwell);
  }

  function resetForm() {
    $('tracking-form').reset();
    $('tracking-left').value = '0'; $('tracking-top').value = '0';
    $('tracking-right').value = '100'; $('tracking-bottom').value = '100';
    $('tracking-type').value = 'count_in_region';
    $('tracking-confidence').value = '0.35';
    $('tracking-count').value = '2'; $('tracking-frames').value = '3';
    $('tracking-cooldown').value = '60'; $('tracking-dwell').value = '10';
    editingId = null;
    $('tracking-form-title').textContent = 'Create a camera rule';
    $('tracking-save').textContent = 'Save rule';
    $('tracking-cancel-edit').classList.add('hidden');
    showTypeFields();
  }

  function editRule(rule) {
    const region = rule.region || { x: 0, y: 0, width: 1, height: 1 };
    $('tracking-name').value = rule.name;
    $('tracking-class').value = rule.class_name;
    $('tracking-camera').value = rule.camera_token || '';
    $('tracking-type').value = rule.rule_type;
    $('tracking-confidence').value = String(rule.min_confidence);
    $('tracking-count').value = String(rule.threshold_count);
    $('tracking-frames').value = String(rule.min_consecutive_frames);
    $('tracking-cooldown').value = String(rule.cooldown_seconds);
    $('tracking-dwell').value = String(rule.dwell_seconds || 10);
    $('tracking-left').value = String(Math.round(region.x * 100));
    $('tracking-top').value = String(Math.round(region.y * 100));
    $('tracking-right').value = String(Math.round((region.x + region.width) * 100));
    $('tracking-bottom').value = String(Math.round((region.y + region.height) * 100));
    $('tracking-enabled').checked = rule.enabled;
    editingId = rule.id;
    $('tracking-form-title').textContent = `Edit rule · ${rule.name}`;
    $('tracking-save').textContent = 'Save changes';
    $('tracking-cancel-edit').classList.remove('hidden');
    showTypeFields();
    $('tracking-name').focus();
  }

  async function handleRuleAction(button) {
    const rule = rules.find((item) => Number(item.id) === Number(button.dataset.ruleId));
    if (!rule) return;
    const action = button.dataset.ruleAction;
    try {
      if (action === 'edit') { editRule(rule); return; }
      if (action === 'toggle') {
        await api(`/api/admin/tracking/rules/${rule.id}`, { method: 'PATCH', body: JSON.stringify({ enabled: !rule.enabled }) });
        await loadRules(); message(`Rule ${rule.enabled ? 'paused' : 'enabled'}.`); return;
      }
      if (action === 'delete' && confirm(`Delete “${rule.name}”? Previously recorded alerts will remain.`)) {
        await api(`/api/admin/tracking/rules/${rule.id}`, { method: 'DELETE' });
        if (Number(editingId) === Number(rule.id)) resetForm();
        await loadRules(); message('Rule deleted. Past alert records were retained.');
      }
    } catch (error) { message(error.message, true); }
  }

  async function saveRule(event) {
    event.preventDefault();
    const left = Number($('tracking-left').value); const top = Number($('tracking-top').value);
    const right = Number($('tracking-right').value); const bottom = Number($('tracking-bottom').value);
    if (!(left >= 0 && top >= 0 && right <= 100 && bottom <= 100 && right > left && bottom > top)) {
      message('Region edges must be valid percentages, with right greater than left and bottom greater than top.', true); return;
    }
    const payload = {
      name: $('tracking-name').value.trim(), class_name: $('tracking-class').value.trim(),
      camera_token: $('tracking-camera').value || null, rule_type: $('tracking-type').value,
      min_confidence: Number($('tracking-confidence').value), threshold_count: Number($('tracking-count').value),
      min_consecutive_frames: Number($('tracking-frames').value), cooldown_seconds: Number($('tracking-cooldown').value),
      dwell_seconds: Number($('tracking-dwell').value), enabled: $('tracking-enabled').checked,
      region: { x: left / 100, y: top / 100, width: (right - left) / 100, height: (bottom - top) / 100 },
    };
    $('tracking-save').disabled = true;
    try {
      const result = editingId
        ? await api(`/api/admin/tracking/rules/${editingId}`, { method: 'PATCH', body: JSON.stringify(payload) })
        : await api('/api/admin/tracking/rules', { method: 'POST', body: JSON.stringify(payload) });
      resetForm(); await loadRules(); message(result.message || 'Rule saved.');
    } catch (error) { message(error.message, true); }
    finally { $('tracking-save').disabled = false; }
  }

  function renderEvents(events, append) {
    const target = $('tracking-events');
    const html = events.map((event) => `<article class="tracking-event"><div class="tracking-card-head"><h3>${escapeHtml(event.rule_name)}</h3><span class="subtle-tag">${Number(event.object_count)} object${Number(event.object_count) === 1 ? '' : 's'}</span></div><p>${escapeHtml(event.message)}</p><p class="tracking-meta">${escapeHtml(event.camera_name || 'Camera')} · ${escapeHtml(event.class_name)} · ${escapeHtml(event.rule_type.replaceAll('_', ' '))} · ${escapeHtml(new Date(event.timestamp).toLocaleString())}</p></article>`).join('');
    if (!append) target.innerHTML = html || '<div class="empty-state">No rule alerts yet. Alerts appear when matching camera frames arrive.</div>';
    else if (html) target.insertAdjacentHTML('beforeend', html);
    nextBeforeId = events.length ? Number(events[events.length - 1].id) : nextBeforeId;
    $('tracking-more-events').classList.toggle('hidden', events.length < 100 || !nextBeforeId);
  }

  async function loadEvents(append = false) {
    if (eventBusy) return;
    eventBusy = true;
    try {
      const query = new URLSearchParams({ limit: '100' });
      const camera = $('tracking-event-camera').value;
      if (camera) query.set('camera_token', camera);
      if (append && nextBeforeId) query.set('before_id', String(nextBeforeId));
      const result = await api(`/api/admin/tracking/events?${query}`);
      renderEvents(Array.isArray(result.events) ? result.events : [], append);
    } catch (error) { if (!append) $('tracking-events').innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`; }
    finally { eventBusy = false; }
  }

  async function refresh() {
    $('tracking-refresh').disabled = true;
    try {
      await loadCameras(); await loadRules(); await loadEvents(false);
      message('Tracking rules and recent alerts are up to date.');
    } catch (error) { message(error.message, true); }
    finally { $('tracking-refresh').disabled = false; }
  }

  $('tracking-type').addEventListener('change', showTypeFields);
  $('tracking-form').addEventListener('submit', saveRule);
  $('tracking-cancel-edit').addEventListener('click', resetForm);
  $('tracking-refresh').addEventListener('click', refresh);
  $('tracking-event-camera').addEventListener('change', () => { nextBeforeId = null; loadEvents(false); });
  $('tracking-more-events').addEventListener('click', () => loadEvents(true));
  showTypeFields();
  refresh();
  setInterval(() => loadEvents(false), 10000);
})();
