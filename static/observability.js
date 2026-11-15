(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const state = { period: 'lifetime', after: '', before: '', logCursor: null, sessionCursor: null, auditCursor: null, logBusy: false, logRequestId: 0, logController: null, logQueryKey: '', logAppliedQuery: '', logTailActive: false, logTailTimer: null, sessionBusy: false, sessionRequestId: 0, sessionController: null, auditBusy: false, auditRequestId: 0, auditController: null, auditQueryKey: '', busy: false, refreshPending: false, refreshPendingHistory: false, metrics: [], metricExpanded: new Map(), reportTypes: [], savedLogFilters: [], seriesBusy: false, seriesRequestId: 0, seriesController: null, seriesQueryKey: '', seriesView: 'chart', seriesGroupBy: null, reportBusy: false, integrationBusy: false, editingDestinationId: null, editingTransport: null, retentionPolicy: null, retentionBusy: false, retentionDirty: false, retentionRequestId: 0 };
  const kpis = [
    ['application_starts', 'Application starts'], ['clean_stops', 'Clean stops'], ['uptime', 'Known installation uptime'],
    ['training_jobs', 'Training jobs'], ['training_epochs_completed', 'Epochs completed'],
    ['api_requests', 'API requests'], ['inference_frames', 'Inference frames'], ['energy_wh', 'Estimated GPU energy'],
  ];

  async function api(path, options = {}) {
    const response = await fetch(path, { ...options, credentials: 'same-origin', cache: 'no-store' });
    let payload;
    try { payload = await response.json(); } catch { payload = {}; }
    if (!response.ok) throw new Error(payload.detail || `VAOS returned HTTP ${response.status}.`);
    return payload;
  }
  function formatUtc(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? 'Unknown time' : `${date.toLocaleString(undefined, { timeZone: 'UTC' })} UTC`;
  }
  function formatValue(item) {
    if (!item || item.available !== true || item.value === null || item.value === undefined) return 'Not recorded';
    if (item.unit === 'seconds') {
      const total = Math.max(0, Number(item.value) || 0), days = Math.floor(total / 86400), hours = Math.floor(total % 86400 / 3600), minutes = Math.floor(total % 3600 / 60);
      return days ? `${days}d ${hours}h ${minutes}m` : hours ? `${hours}h ${minutes}m` : `${minutes}m`;
    }
    if (item.unit === 'Wh') {
      const energy = Number(item.value) || 0;
      return Math.abs(energy) >= 1000
        ? `${(energy / 1000).toLocaleString(undefined, { maximumFractionDigits: 2 })} kWh`
        : `${energy.toLocaleString(undefined, { maximumFractionDigits: Math.abs(energy) < 1 ? 6 : 3 })} Wh`;
    }
    return `${Number(item.value).toLocaleString(undefined, { maximumFractionDigits: 2 })}${item.unit === '%' ? '%' : ` ${item.unit || ''}`}`.trim();
  }
  function replaceTableMessage(bodyId, colspan, message) {
    const body = $(bodyId); body.replaceChildren();
    const row = body.insertRow(), cell = row.insertCell(); cell.colSpan = colspan; cell.textContent = message;
  }
  function isAbortError(error) { return error?.name === 'AbortError'; }
  function customIso(value) {
    if (!value) return '';
    const local = new Date(value);
    return Number.isNaN(local.getTime()) ? '' : local.toISOString();
  }
  function localDateInput(date) {
    const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
    return local.toISOString().slice(0, 16);
  }
  function selectedRange() {
    if (state.period === 'custom') return { after: customIso(state.after), before: customIso(state.before) };
    const hours = { '24h': 24, '7d': 168, '30d': 720 }[state.period];
    if (!hours) return { after: '', before: '' };
    const end = new Date(); return { after: new Date(end.getTime() - hours * 3600000).toISOString(), before: end.toISOString() };
  }
  function periodQuery() {
    const params = new URLSearchParams({ period: state.period });
    const range = selectedRange();
    if (state.period === 'custom') { params.set('after', range.after); params.set('before', range.before); }
    return params.toString();
  }
  function setStatus(message, error = false) {
    const status = $('obs-status'); status.textContent = message; status.classList.toggle('error', error);
  }
  function renderOverview(data) {
    $('obs-installation-note').textContent = data.installation
      ? `Installation ${data.installation.installation_id} · first recorded start ${formatUtc(data.installation.first_started_at)} · all ranges are UTC.`
      : 'No runtime start has been recorded in this installation yet.';
    $('obs-updated').textContent = `Updated ${new Date(data.updated_at).toLocaleTimeString()}`;
    const container = $('obs-kpis'); container.replaceChildren();
    for (const [key, label] of kpis) {
      let item = data.kpis?.[key] || { available: false };
      let valueText = formatValue(item);
      let noteText = item.available === false ? (item.reason || 'This source has not been instrumented yet.') : (item.quality === 'lower_bound_unknown_sessions' ? `Known duration only · ${item.unknown_sessions} session(s) have unknown shutdowns.` : `Measured range: ${data.range?.after || 'installation lifetime'} to ${data.range?.before_exclusive || 'now'} UTC.`);
      if (key === 'training_jobs') {
        const jobs = [
          ['Started', data.kpis?.training_jobs_started],
          ['Completed', data.kpis?.training_jobs_completed],
          ['Failed', data.kpis?.training_jobs_failed],
        ];
        item = { available: jobs.some(([, value]) => value?.available === true) };
        valueText = jobs.map(([name, value]) => `${name} ${formatValue(value)}`).join(' · ');
        noteText = jobs.some(([, value]) => value?.available !== true)
          ? 'Started · completed · failed. Not recorded does not mean zero.'
          : `Started · completed · failed · measured range: ${data.range?.after || 'installation lifetime'} to ${data.range?.before_exclusive || 'now'} UTC.`;
      }
      if (key === 'energy_wh') {
        noteText = item.available === true
          ? item.coverage_seconds !== null && item.coverage_seconds !== undefined
            ? `Integrated GPU board power · ${(Number(item.coverage_seconds) / 3600).toLocaleString(undefined, { maximumFractionDigits: 2 })} covered GPU-device hours · ${item.skipped_gap_count || 0} skipped long gap(s). Not wall energy or per-training-job attribution.`
            : `Sum of ${item.interval_count || 0} estimated-energy intervals derived from measured power · ${item.skipped_gap_count || 0} skipped long gap(s). Detailed intervals are retained for 30 days; not wall energy or per-training-job attribution.`
          : (item.reason || 'No validated GPU power readings are available for this range.');
      }
      const card = document.createElement('article'); card.className = 'panel observability-kpi'; card.dataset.kpi = key;
      const heading = document.createElement('span'); heading.className = 'metric-label'; heading.textContent = label;
      const value = document.createElement('strong'); value.textContent = valueText;
      const note = document.createElement('small'); note.textContent = noteText;
      card.append(heading, value, note); container.append(card);
    }
  }
  function renderCounterQueueStatus(data) {
    const panel = $('obs-counter-queue-status');
    const stateLabel = data.state === 'no_backlog_observed' ? 'No backlog observed'
      : data.state === 'recovery_pending' ? 'Recovery pending'
      : data.state === 'degraded' ? 'Degraded' : 'Unknown';
    const fallback = data.fallback || {}, primary = data.primary || {}, memory = data.memory || {};
    const securityFallback = data.security_event_fallback || {};
    const pending = Number(fallback.pending_increments || 0), rows = Number(fallback.pending_rows || 0);
    const memoryPending = Number(memory.pending_increments || 0);
    const overflow = Number(fallback.overflow_increments || 0) + Number(memory.overflow_increments || 0);
    const databaseMiB = (Number(fallback.database_bytes || 0) / (1024 * 1024)).toLocaleString(undefined, { maximumFractionDigits: 1 });
    const databaseLimitMiB = (Number(fallback.database_limit_bytes || 0) / (1024 * 1024)).toLocaleString(undefined, { maximumFractionDigits: 0 });
    const securityQueueState = securityFallback.available === true
      ? `${Number(securityFallback.pending_rows || 0).toLocaleString()} pending · ${Number(securityFallback.dropped_events || 0).toLocaleString()} dropped`
      : 'unavailable';
    const details = `Primary buffer: ${Number(primary.pending_increments || 0).toLocaleString()} count(s) awaiting normal flush · recovery queue: ${pending.toLocaleString()} count(s) in ${rows.toLocaleString()} row(s) · sidecar ${databaseMiB}/${databaseLimitMiB} MiB · authentication audit fallback: ${securityQueueState}.`;
    let message = `${stateLabel} · ${details} This is an admin-process snapshot; it does not verify storage writes or camera-worker memory.`;
    if (data.state === 'recovery_pending') message += ' VAOS will replay queued counts and authentication audit events automatically when the primary store is available.';
    if (data.state === 'degraded') message += ` A primary or recovery store is unavailable, or an authentication audit was dropped; ${memoryPending.toLocaleString()} count(s) in bounded admin-process memory may be lost if the service exits before storage recovers.`;
    if (overflow) message += ` ${overflow.toLocaleString()} count(s) have mixed dimensions because a queue reached its detail limit.`;
    panel.textContent = message;
    panel.classList.toggle('error', data.state === 'degraded');
    panel.dataset.queueState = data.state || 'unknown';
  }
  function renderCounterQueueUnavailable() {
    const panel = $('obs-counter-queue-status');
    panel.textContent = 'Counter recovery status could not be checked. Other statistics may still load. Select Refresh to try again; this warning does not confirm data loss.';
    panel.classList.add('error'); panel.dataset.queueState = 'unknown';
  }
  function renderMetrics(items) {
    state.metrics = items;
    document.querySelectorAll('.observability-metric-group').forEach((details) => {
      const name = details.dataset.metricGroup;
      if (name) state.metricExpanded.set(name, details.open);
    });
    const search = $('obs-metric-search').value.trim().toLocaleLowerCase();
    const groups = new Map();
    for (const metric of items) {
      const searchable = `${metric.metric_key} ${metric.display_name} ${metric.description} ${metric.source}`.toLocaleLowerCase();
      if (search && !searchable.includes(search)) continue;
      const category = metric.metric_key.split('.')[0] || 'other';
      if (!groups.has(category)) groups.set(category, []);
      groups.get(category).push(metric);
    }
    const host = $('obs-metric-groups'); host.replaceChildren();
    if (!groups.size) { const empty = document.createElement('p'); empty.className = 'empty-state'; empty.textContent = 'No metric definitions match this search.'; host.append(empty); return; }
    for (const [name, metrics] of groups) {
      const details = document.createElement('details'); details.className = 'observability-metric-group'; details.dataset.metricGroup = name; details.open = state.metricExpanded.has(name) ? state.metricExpanded.get(name) : true;
      details.addEventListener('toggle', () => state.metricExpanded.set(name, details.open));
      const summary = document.createElement('summary'); summary.textContent = `${name} · ${metrics.length} metrics`;
      const list = document.createElement('div'); list.className = 'observability-metric-list';
      for (const metric of metrics) {
        const row = document.createElement('article'); row.className = 'observability-metric-row';
        const title = document.createElement('div');
        const nameEl = document.createElement('strong'); nameEl.textContent = metric.display_name;
        const key = document.createElement('code'); key.textContent = metric.metric_key;
        const desc = document.createElement('p'); desc.textContent = metric.description;
        title.append(nameEl, key, desc);
        const meta = document.createElement('dl'); meta.className = 'observability-metric-meta';
        for (const [term, value] of [['Unit', metric.unit], ['Type', metric.metric_type], ['Measurement', metric.estimated ? 'Estimate from sampled readings' : 'Measured'], ['Source', metric.source], ['Scope', metric.measurement_scope], ['Aggregation', metric.aggregation], ['Dimensions', (metric.dimensions || []).join(', ') || 'None']]) {
          const dt = document.createElement('dt'); dt.textContent = term; const dd = document.createElement('dd'); dd.textContent = value || '—'; meta.append(dt, dd);
        }
        const availability = document.createElement('span'); availability.className = `observability-availability ${metric.available ? 'is-available' : ''}`; availability.textContent = metric.available ? 'Recorded' : 'Not recorded';
        row.append(title, meta, availability); list.append(row);
      }
      details.append(summary, list); host.append(details);
    }
  }
  async function loadSessions(append = false) {
    if (append && state.sessionBusy) return;
    const params = new URLSearchParams({ limit: '50' });
    const range = selectedRange(); if (range.after) params.set('after', range.after); if (range.before) params.set('before', range.before);
    if (append) {
      if (!state.sessionCursor) return;
      params.set('cursor', state.sessionCursor);
    }
    const requestId = ++state.sessionRequestId;
    if (!append) state.sessionController?.abort();
    const controller = new AbortController(); state.sessionController = controller; state.sessionBusy = true;
    const olderButton = $('obs-session-older'); olderButton.disabled = true;
    const body = $('obs-session-rows');
    if (!append) replaceTableMessage('obs-session-rows', 6, 'Loading runtime sessions…');
    $('obs-session-message').textContent = append ? 'Loading older runtime sessions…' : 'Loading runtime sessions…';
    try {
      const data = await api(`/api/admin/observability/lifecycle?${params}`, { signal: controller.signal });
      if (requestId !== state.sessionRequestId) return;
      const items = data.items || [];
      if (!append) body.replaceChildren();
      if (!items.length && !append) replaceTableMessage('obs-session-rows', 6, 'No runtime sessions recorded yet.');
      for (const item of items) {
        const row = body.insertRow();
        const durationEnd = item.clean_shutdown_at || item.last_heartbeat_at;
        const duration = Math.max(0, (Date.parse(durationEnd) - Date.parse(item.started_at)) / 1000);
        for (const value of [formatUtc(item.started_at), item.component, item.app_version, formatUtc(item.last_heartbeat_at), item.exit_reason === 'clean' ? 'Clean stop' : item.exit_reason === 'running' ? 'Running' : 'Unknown', formatValue({ available: true, value: duration, unit: 'seconds' })]) {
          const cell = row.insertCell(); cell.textContent = value;
        }
      }
      state.sessionCursor = data.next_cursor || null;
      olderButton.disabled = !data.has_more;
      $('obs-session-message').textContent = `${items.length} runtime session(s) shown${data.has_more ? ' · older sessions available' : ''}.`;
    } catch (error) {
      if (requestId !== state.sessionRequestId || isAbortError(error)) return;
      if (!append) { state.sessionCursor = null; replaceTableMessage('obs-session-rows', 6, 'Runtime sessions could not be loaded.'); }
      $('obs-session-message').textContent = `Runtime sessions could not be loaded: ${error.message || 'request failed'}`;
      throw error;
    } finally {
      if (requestId === state.sessionRequestId) {
        state.sessionBusy = false; state.sessionController = null;
        olderButton.disabled = !state.sessionCursor;
      }
    }
  }
  function renderEvents(items, append = false) {
    const body = $('obs-log-rows'); if (!append) body.replaceChildren();
    if (!items.length && !append) { const row = body.insertRow(); const cell = row.insertCell(); cell.colSpan = 4; cell.textContent = 'No events match these filters.'; }
    for (const item of items) {
      const row = body.insertRow();
      row.insertCell().textContent = formatUtc(item.timestamp);
      const level = row.insertCell(); const badge = document.createElement('span'); badge.className = `observability-level level-${String(item.level).toLowerCase()}`; badge.textContent = item.level; level.append(badge);
      const component = row.insertCell(); component.textContent = `${item.component} · ${item.event_type}`;
      const detailCell = row.insertCell(); const details = document.createElement('details'); const summary = document.createElement('summary'); summary.textContent = item.message || 'View event details'; const pre = document.createElement('pre');
      const eventJson = { event_id: item.event_id, timestamp: item.timestamp, event_type: item.event_type, component: item.component, level: item.level, session_id: item.session_id || null, correlation_id: item.correlation_id || null, message: item.message, details: item.details || {} };
      pre.textContent = JSON.stringify(eventJson, null, 2); const copy = document.createElement('button'); copy.type = 'button'; copy.className = 'button button-secondary button-small'; copy.textContent = 'Copy JSON'; copy.setAttribute('aria-label', `Copy ${item.event_type || 'event'} as JSON`);
      copy.addEventListener('click', async () => {
        try {
          if (!navigator.clipboard?.writeText) throw new Error('Clipboard access is unavailable in this browser context.');
          await navigator.clipboard.writeText(JSON.stringify(eventJson, null, 2));
          copy.textContent = 'Copied'; window.setTimeout(() => { copy.textContent = 'Copy JSON'; }, 1200);
        } catch (error) { $('obs-log-message').textContent = error.message || 'Event JSON could not be copied.'; }
      });
      const permalink = document.createElement('a'); permalink.className = 'button button-secondary button-small'; permalink.href = `/api/admin/observability/events/${encodeURIComponent(item.event_id)}`; permalink.target = '_blank'; permalink.rel = 'noopener'; permalink.textContent = 'Open record'; permalink.setAttribute('aria-label', `Open ${item.event_type || 'event'} record in a new tab`);
      details.append(summary, pre); detailCell.append(details, copy, permalink);
    }
  }
  function eventQueryParams({ limit = 50, cursor = '' } = {}) {
    const params = new URLSearchParams({ limit: String(limit) });
    if (cursor) params.set('cursor', cursor);
    const range = selectedRange(); if (range.after) params.set('after', range.after); if (range.before) params.set('before', range.before);
    const level = $('obs-log-level').value, component = $('obs-log-component').value.trim(), type = $('obs-log-type').value.trim(), sessionId = $('obs-log-session-id').value.trim(), correlationId = $('obs-log-correlation-id').value.trim(), trainingJobId = $('obs-log-job-id').value.trim(), datasetId = $('obs-log-dataset-id').value.trim(), modelId = $('obs-log-model-id').value.trim(), search = $('obs-log-search').value.trim();
    if (level) params.set('level', level); if (component) params.set('component', component); if (type) params.set('event_type', type); if (sessionId) params.set('session_id', sessionId); if (correlationId) params.set('correlation_id', correlationId); if (trainingJobId) params.set('training_job_id', trainingJobId); if (datasetId) params.set('dataset_id', datasetId); if (modelId) params.set('model_id', modelId); if (search) params.set('search', search);
    return params;
  }
  function currentSavedLogFilter() {
    const filters = { period: state.period };
    const range = selectedRange();
    if (state.period === 'custom') { filters.after = range.after; filters.before = range.before; }
    for (const [key, id] of [['level', 'obs-log-level'], ['component', 'obs-log-component'], ['event_type', 'obs-log-type'], ['session_id', 'obs-log-session-id'], ['correlation_id', 'obs-log-correlation-id'], ['training_job_id', 'obs-log-job-id'], ['dataset_id', 'obs-log-dataset-id'], ['model_id', 'obs-log-model-id'], ['search', 'obs-log-search']]) {
      const value = $(id).value.trim(); if (value) filters[key] = value;
    }
    return filters;
  }
  async function loadSavedLogFilters(selectedId = '') {
    const data = await api('/api/admin/observability/log-filters');
    state.savedLogFilters = data.items || [];
    const select = $('obs-saved-log-filters'); select.replaceChildren();
    const placeholder = document.createElement('option'); placeholder.value = ''; placeholder.textContent = state.savedLogFilters.length ? 'Choose a saved filter' : 'No saved filters'; select.append(placeholder);
    for (const item of state.savedLogFilters) {
      const option = document.createElement('option'); option.value = item.filter_id; option.textContent = item.name; select.append(option);
    }
    if (selectedId && state.savedLogFilters.some((item) => item.filter_id === selectedId)) select.value = selectedId;
    const selected = Boolean(select.value);
    $('obs-apply-log-filter').disabled = !selected; $('obs-delete-log-filter').disabled = !selected;
    $('obs-saved-log-filter-status').textContent = `${state.savedLogFilters.length} of ${data.maximum || 20} saved filters · private to this authenticated account.`;
  }
  async function saveCurrentLogFilter() {
    const nameInput = $('obs-saved-log-filter-name'), name = nameInput.value.trim();
    if (!name) { $('obs-saved-log-filter-status').textContent = 'Enter a name for this filter first.'; nameInput.focus(); return; }
    const button = $('obs-save-log-filter'); button.disabled = true;
    try {
      const result = await api('/api/admin/observability/log-filters', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, filters: currentSavedLogFilter() }) });
      nameInput.value = ''; await loadSavedLogFilters(result.item.filter_id);
      $('obs-saved-log-filter-status').textContent = `Saved “${result.item.name}” for this authenticated account.`;
    } catch (error) { $('obs-saved-log-filter-status').textContent = error.message || 'This filter could not be saved.'; }
    finally { button.disabled = false; }
  }
  async function applySavedLogFilter() {
    const item = state.savedLogFilters.find((filter) => filter.filter_id === $('obs-saved-log-filters').value);
    if (!item) { $('obs-saved-log-filter-status').textContent = 'Choose a saved filter to apply.'; return; }
    const filters = item.filters || {};
    state.period = filters.period || 'lifetime'; $('obs-period').value = state.period;
    $('obs-custom-range').classList.toggle('hidden', state.period !== 'custom');
    state.after = state.period === 'custom' && filters.after ? localDateInput(new Date(filters.after)) : '';
    state.before = state.period === 'custom' && filters.before ? localDateInput(new Date(filters.before)) : '';
    $('obs-after').value = state.after; $('obs-before').value = state.before;
    $('obs-log-level').value = filters.level || '';
    $('obs-log-component').value = filters.component || '';
    $('obs-log-type').value = filters.event_type || '';
    $('obs-log-session-id').value = filters.session_id || '';
    $('obs-log-correlation-id').value = filters.correlation_id || '';
    $('obs-log-job-id').value = filters.training_job_id || '';
    $('obs-log-dataset-id').value = filters.dataset_id || '';
    $('obs-log-model-id').value = filters.model_id || '';
    $('obs-log-search').value = filters.search || '';
    state.logCursor = null; state.sessionCursor = null; state.auditCursor = null;
    $('obs-saved-log-filter-status').textContent = `Applied “${item.name}”.`;
    await refresh();
  }
  async function deleteSelectedLogFilter() {
    const id = $('obs-saved-log-filters').value;
    const item = state.savedLogFilters.find((filter) => filter.filter_id === id);
    if (!item) return;
    const button = $('obs-delete-log-filter'); button.disabled = true;
    try {
      await api(`/api/admin/observability/log-filters/${encodeURIComponent(id)}`, { method: 'DELETE' });
      await loadSavedLogFilters(); $('obs-saved-log-filter-status').textContent = `Deleted “${item.name}”.`;
    } catch (error) { $('obs-saved-log-filter-status').textContent = error.message || 'This saved filter could not be deleted.'; }
    finally { button.disabled = !$('obs-saved-log-filters').value; }
  }
  async function loadEvents({ next = false } = {}) {
    if (next && (state.logBusy || !state.logCursor)) return;
    const queryKey = eventQueryParams({ limit: 50 }).toString();
    if (!next && state.logBusy && queryKey === state.logQueryKey) return;
    const requestId = ++state.logRequestId;
    if (!next) state.logController?.abort();
    const controller = new AbortController(); state.logController = controller; state.logBusy = true;
    state.logQueryKey = queryKey;
    if (!next) state.logCursor = null;
    const nextButton = $('obs-log-next'); nextButton.disabled = true;
    if (queryKey !== state.logAppliedQuery) replaceTableMessage('obs-log-rows', 4, 'Loading events for the selected filters…');
    $('obs-log-message').textContent = 'Loading filtered events…';
    try {
      const params = eventQueryParams({ limit: 50, cursor: next ? state.logCursor : '' });
      const result = await api(`/api/admin/observability/events?${params}`, { signal: controller.signal });
      if (requestId !== state.logRequestId) return;
      renderEvents(result.items || [], next); state.logCursor = result.next_cursor || null; state.logAppliedQuery = queryKey;
      nextButton.disabled = !result.has_more;
      $('obs-log-message').textContent = `${(result.items || []).length} redacted event(s) shown${state.logCursor ? ' · more events available' : ''}. Log search is literal text; regular expressions are not enabled.`;
    } catch (error) {
      if (requestId !== state.logRequestId || isAbortError(error)) return;
      state.logCursor = null; nextButton.disabled = true;
      replaceTableMessage('obs-log-rows', 4, 'Events could not be loaded. Previous results were cleared to avoid mixing filters.');
      $('obs-log-message').textContent = error.message || 'Events could not be loaded for the current filters.';
      throw error;
    } finally {
      if (requestId === state.logRequestId) { state.logBusy = false; state.logController = null; }
    }
  }
  async function createLogReport(format) {
    const range = selectedRange(), now = new Date();
    const filters = {
      after: range.after || new Date(now.getTime() - 90 * 86400000).toISOString(),
      before: range.before || now.toISOString(),
    };
    const level = $('obs-log-level').value, component = $('obs-log-component').value.trim(), eventType = $('obs-log-type').value.trim(), sessionId = $('obs-log-session-id').value.trim(), correlationId = $('obs-log-correlation-id').value.trim(), trainingJobId = $('obs-log-job-id').value.trim(), datasetId = $('obs-log-dataset-id').value.trim(), modelId = $('obs-log-model-id').value.trim(), search = $('obs-log-search').value.trim();
    if (level) filters.level = level; if (component) filters.component = component; if (eventType) filters.event_type = eventType; if (sessionId) filters.session_id = sessionId; if (correlationId) filters.correlation_id = correlationId; if (trainingJobId) filters.training_job_id = trainingJobId; if (datasetId) filters.dataset_id = datasetId; if (modelId) filters.model_id = modelId; if (search) filters.search = search;
    const button = $(`obs-log-export-${format}`); button.disabled = true;
    $('obs-log-message').textContent = 'Preparing this filtered export with the central report service…';
    try {
      const job = await api('/api/admin/observability/reports/jobs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ report_type: 'events', format, filters }) });
      $('obs-log-message').textContent = `${format.toUpperCase()} report queued · ${job.job_id}. Its progress and download link are in Reports & exports below.`;
      await loadReportJobs();
    } catch (error) { $('obs-log-message').textContent = error.message || 'The filtered report could not be queued.'; }
    finally { button.disabled = false; }
  }
  async function downloadLogNdjson() {
    const button = $('obs-log-export-ndjson'); button.disabled = true;
    $('obs-log-message').textContent = 'Preparing up to 1,000 matching events…';
    try {
      const records = []; let cursor = '', more = false;
      while (records.length < 1000) {
        const page = await api(`/api/admin/observability/events?${eventQueryParams({ limit: Math.min(200, 1000 - records.length), cursor })}`);
        records.push(...(page.items || []));
        more = Boolean(page.has_more && page.next_cursor);
        if (!more) break;
        cursor = page.next_cursor;
      }
      if (more) throw new Error('More than 1,000 events match. Narrow the time range or filters, then export again.');
      const body = records.map((item) => JSON.stringify(item)).join('\n') + (records.length ? '\n' : '');
      const url = URL.createObjectURL(new Blob([body], { type: 'application/x-ndjson;charset=utf-8' }));
      const link = document.createElement('a'); link.href = url; link.download = `vaos-events-${new Date().toISOString().replace(/[:.]/g, '-')}.ndjson`; link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      $('obs-log-message').textContent = `${records.length} filtered redacted event(s) downloaded as NDJSON.`;
    } catch (error) { $('obs-log-message').textContent = error.message || 'NDJSON export could not be prepared.'; }
    finally { button.disabled = false; }
  }
  function setLogTail(active) {
    const wasActive = state.logTailActive;
    state.logTailActive = active;
    if (state.logTailTimer) window.clearInterval(state.logTailTimer);
    state.logTailTimer = null;
    if (!active && wasActive) {
      // A completed in-flight poll must not repaint the table after the UI says it is paused.
      state.logRequestId += 1;
      state.logController?.abort();
      state.logController = null;
      state.logBusy = false;
    }
    const button = $('obs-log-tail-toggle');
    button.textContent = active ? 'Pause live tail' : 'Start live tail';
    button.setAttribute('aria-pressed', String(active));
    $('obs-log-live-status').textContent = active ? 'Live tail is running · newest filtered events refresh every 3 seconds.' : 'Live tail is paused. It polls the newest filtered events every 3 seconds while active.';
    if (active) {
      state.logCursor = null;
      loadEvents().catch((error) => { $('obs-log-message').textContent = error.message || 'Live event refresh failed.'; });
      state.logTailTimer = window.setInterval(() => {
        state.logCursor = null;
        loadEvents().catch((error) => { $('obs-log-message').textContent = error.message || 'Live event refresh failed.'; });
      }, 3000);
    }
  }
  async function loadSecurityAudit({ next = false } = {}) {
    if (next && (state.auditBusy || !state.auditCursor)) return;
    const params = new URLSearchParams({ limit: '50' });
    if (next && state.auditCursor) params.set('cursor', state.auditCursor);
    const range = selectedRange(); if (range.after) params.set('after', range.after); if (range.before) params.set('before', range.before);
    for (const [key, id] of [['search', 'obs-audit-search'], ['event_type', 'obs-audit-type'], ['outcome', 'obs-audit-outcome'], ['component', 'obs-audit-component'], ['actor_ref', 'obs-audit-actor-ref']]) { const value = $(id).value.trim(); if (value) params.set(key, value); }
    const queryKey = params.toString();
    if (!next && state.auditBusy && queryKey === state.auditQueryKey) return;
    const requestId = ++state.auditRequestId;
    if (!next) state.auditController?.abort();
    const controller = new AbortController(); state.auditController = controller; state.auditBusy = true; state.auditQueryKey = queryKey;
    const nextButton = $('obs-audit-next'); nextButton.disabled = true;
    if (!next) { state.auditCursor = null; replaceTableMessage('obs-audit-rows', 6, 'Loading security events…'); }
    $('obs-audit-message').textContent = 'Loading filtered security events…';
    try {
      const result = await api(`/api/admin/observability/security-audit/events?${params}`, { signal: controller.signal });
      if (requestId !== state.auditRequestId) return;
      const body = $('obs-audit-rows'); body.replaceChildren();
      if (!result.items?.length) replaceTableMessage('obs-audit-rows', 6, 'No security events match these filters.');
      for (const item of result.items || []) {
        const row = body.insertRow(); row.insertCell().textContent = formatUtc(item.timestamp); row.insertCell().textContent = item.outcome;
        row.insertCell().textContent = `${item.component} · ${item.event_type}`; row.insertCell().textContent = item.actor_ref || '—';
        const detailsCell = row.insertCell(), details = document.createElement('details'), summary = document.createElement('summary'), pre = document.createElement('pre');
        summary.textContent = 'View redacted details'; pre.textContent = JSON.stringify(item.details || {}, null, 2); details.append(summary, pre); detailsCell.append(details);
        const hash = row.insertCell(); hash.textContent = String(item.integrity_hash || '').slice(0, 16) + '…'; hash.title = item.integrity_hash || '';
      }
      state.auditCursor = result.next_cursor || null; nextButton.disabled = !result.has_more;
      $('obs-audit-message').textContent = `${(result.items || []).length} append-only audit record(s) shown${result.has_more ? ' · more records available' : ''}.`;
    } catch (error) {
      if (requestId !== state.auditRequestId || isAbortError(error)) return;
      state.auditCursor = null; nextButton.disabled = true;
      replaceTableMessage('obs-audit-rows', 6, 'Security events could not be loaded. Previous results were cleared.');
      $('obs-audit-message').textContent = error.message || 'Security events could not be loaded for the current filters.';
      throw error;
    } finally {
      if (requestId === state.auditRequestId) { state.auditBusy = false; state.auditController = null; }
    }
  }
  function updateReportMetricOptions() {
    const select = $('obs-report-metric'), current = select.value;
    const available = state.metrics.filter((metric) => metric.available && ['gauge', 'histogram', 'counter'].includes(metric.metric_type));
    select.replaceChildren();
    for (const metric of available) { const option = document.createElement('option'); option.value = metric.metric_key; option.textContent = `${metric.display_name} (${metric.unit})`; select.append(option); }
    if (!available.length) { const option = document.createElement('option'); option.value = ''; option.textContent = 'No recorded metrics available'; select.append(option); }
    select.value = available.some((metric) => metric.metric_key === current) ? current : (available[0]?.metric_key || '');
  }
  function renderReportJobs(items) {
    const body = $('obs-report-rows'); body.replaceChildren();
    if (!items.length) { const row = body.insertRow(); const cell = row.insertCell(); cell.colSpan = 7; cell.textContent = 'No report jobs yet. Generate a report above.'; return; }
    for (const item of items) {
      const row = body.insertRow();
      row.insertCell().textContent = formatUtc(item.requested_at);
      const definition = state.reportTypes.find((entry) => entry.report_type === item.report_type);
      row.insertCell().textContent = `${definition?.label || item.report_type} · ${String(item.format || '').toUpperCase()}`;
      row.insertCell().textContent = Object.entries(item.filters || {}).map(([key, value]) => `${key}: ${value}`).join(' · ') || 'All available records';
      row.insertCell().textContent = item.status === 'failed' ? `Failed · ${item.error_code || 'generation error'}` : item.status;
      row.insertCell().textContent = `${Number(item.progress_percent) || 0}%`;
      row.insertCell().textContent = formatUtc(item.expires_at);
      const actions = row.insertCell();
      if (item.status === 'complete') {
        const link = document.createElement('a'); link.className = 'button button-secondary button-small'; link.href = `/api/admin/observability/reports/jobs/${encodeURIComponent(item.job_id)}/download`; link.textContent = 'Download'; link.setAttribute('download', ''); actions.append(link);
      } else if (item.status === 'queued') {
        const button = document.createElement('button'); button.className = 'button button-secondary button-small'; button.type = 'button'; button.textContent = 'Cancel';
        button.addEventListener('click', async () => { button.disabled = true; try { await api(`/api/admin/observability/reports/jobs/${encodeURIComponent(item.job_id)}/cancel`, { method: 'POST' }); await loadReportJobs(); } catch (error) { $('obs-report-message').textContent = error.message || 'Report cancellation failed.'; } }); actions.append(button);
      } else if (item.status === 'running') actions.textContent = 'Working in background…';
      else actions.textContent = '—';
    }
  }
  async function loadReportJobs() {
    if (state.reportBusy) return null;
    state.reportBusy = true;
    try { const result = await api('/api/admin/observability/reports?limit=50'); renderReportJobs(result.items || []); return true; }
    catch (error) { $('obs-report-message').textContent = error.message || 'Report status could not be loaded.'; return false; }
    finally { state.reportBusy = false; }
  }
  async function loadReportTypes() {
    const result = await api('/api/admin/observability/reports/types');
    state.reportTypes = result.items || [];
    const select = $('obs-report-type'), current = select.value;
    select.replaceChildren();
    for (const definition of state.reportTypes) {
      const option = document.createElement('option');
      option.value = definition.report_type;
      option.textContent = definition.label;
      option.title = `${definition.coverage} Source: ${definition.source}`;
      select.append(option);
    }
    const requestedReport = new URLSearchParams(window.location.search).get('report');
    const requestedDefinition = state.reportTypes.find((item) => item.report_type === requestedReport);
    if (requestedDefinition) {
      select.value = requestedDefinition.report_type;
      window.requestAnimationFrame(() => {
        $('obs-report-section').scrollIntoView({ block: 'start' });
        select.focus({ preventScroll: true });
      });
    } else if (state.reportTypes.some((item) => item.report_type === current)) select.value = current;
    updateReportForm();
    if (requestedDefinition) {
      $('obs-report-message').textContent = `${requestedDefinition.label} is ready. ${reportPrompt(requestedDefinition.report_type)}`;
    }
  }
  function resetIntegrationForm() {
    $('obs-integration-form').reset(); $('obs-integration-enabled').checked = false;
    state.editingDestinationId = null; state.editingTransport = null;
    $('obs-integration-save').textContent = 'Save destination';
    $('obs-integration-cancel').classList.add('hidden');
    $('obs-integration-edit-help').textContent = 'When editing, re-enter the complete receiver URL. VAOS does not reveal stored URL paths or credential names. Leave the credential field empty to keep its current environment-variable reference when the delivery type is unchanged.';
    updateIntegrationForm();
  }
  function updateIntegrationForm() {
    const transport = $('obs-integration-transport').value;
    const urlLabel = $('obs-integration-url-label'), url = $('obs-integration-url');
    const secretLabel = $('obs-integration-secret-label'), secret = $('obs-integration-secret-ref');
    const formatLabel = $('obs-integration-event-format-label'), eventFormat = $('obs-integration-event-format');
    formatLabel.classList.toggle('hidden', transport === 'otlp');
    if (transport === 'otlp') eventFormat.value = 'json';
    if (transport === 'syslog_tls') {
      urlLabel.firstChild.textContent = 'Syslog TLS receiver'; url.placeholder = 'syslog-tls://logs.example.com:6514';
      secretLabel.classList.add('hidden'); secret.required = false; secret.value = '';
    } else if (transport === 'otlp') {
      urlLabel.firstChild.textContent = 'OTLP/HTTP logs endpoint'; url.placeholder = 'https://collector.example.com/v1/logs';
      secretLabel.firstChild.textContent = 'Optional bearer-token environment variable'; secret.placeholder = 'VAOS_OTLP_BEARER_TOKEN';
      secretLabel.classList.remove('hidden'); secret.required = transport === 'https_json' && !(state.editingDestinationId && state.editingTransport === transport);
    } else {
      urlLabel.firstChild.textContent = 'HTTPS webhook URL'; url.placeholder = 'https://siem.example.com/webhook';
      secretLabel.firstChild.textContent = 'HMAC key environment variable'; secret.placeholder = 'VAOS_SIEM_HMAC_KEY';
      secretLabel.classList.remove('hidden'); secret.required = !(state.editingDestinationId && state.editingTransport === transport);
    }
  }
  async function loadIntegrations() {
    if (state.integrationBusy) return null;
    state.integrationBusy = true;
    try {
      const result = await api('/api/admin/observability/integrations'), body = $('obs-integration-rows'); body.replaceChildren();
      const items = result.items || [];
      $('obs-integration-summary').textContent = items.length ? `${items.filter((item) => item.enabled).length} enabled · ${items.length} configured` : 'No destinations configured';
      if (!items.length) { const row = body.insertRow(); const cell = row.insertCell(); cell.colSpan = 6; cell.textContent = 'Outbound delivery is off. Add an external destination only if your organization needs it.'; }
      for (const item of items) {
        const row = body.insertRow(); row.insertCell().textContent = `${item.display_name} · ${item.endpoint_host || 'unknown host'}`;
        const transportName = ({ https_json: 'Signed HTTPS', syslog_tls: 'Syslog TLS', otlp: 'OTLP/HTTP logs' })[item.transport] || `${item.transport} · unsupported`;
        row.insertCell().textContent = item.transport === 'otlp' ? transportName : `${transportName} · ${String(item.event_format || 'json').toUpperCase()}`;
        const credentialState = item.transport === 'syslog_tls' ? 'certificate-validated TLS' : item.transport === 'otlp' ? (item.credential_present ? `bearer token ${item.secret_configured ? 'available' : 'missing'}` : 'no bearer token') : `HMAC key ${item.secret_configured ? 'available' : 'missing'}`;
        row.insertCell().textContent = `${item.enabled ? 'Enabled' : 'Disabled'} · ${credentialState}`;
        row.insertCell().textContent = `${item.pending || 0} pending · ${item.dead_letter || 0} dead-letter · ${item.dropped_events || 0} dropped`;
        row.insertCell().textContent = item.last_success_at ? formatUtc(item.last_success_at) : 'No successful delivery';
        const actions = row.insertCell();
        const edit = document.createElement('button'); edit.className = 'button button-secondary button-small'; edit.type = 'button'; edit.textContent = 'Edit'; edit.setAttribute('aria-label', `Edit ${item.display_name} destination`);
        edit.addEventListener('click', () => {
          state.editingDestinationId = item.destination_id; state.editingTransport = item.transport;
          $('obs-integration-name').value = item.display_name;
          $('obs-integration-transport').value = item.transport;
          $('obs-integration-event-format').value = item.event_format || 'json';
          $('obs-integration-url').value = '';
          $('obs-integration-enabled').checked = Boolean(item.enabled);
          $('obs-integration-secret-ref').value = '';
          $('obs-integration-save').textContent = 'Update destination';
          $('obs-integration-cancel').classList.remove('hidden');
          $('obs-integration-edit-help').textContent = `Editing ${item.display_name}: re-enter its complete receiver URL. The stored credential reference is ${item.credential_present ? 'kept when the delivery type is unchanged and this field is left blank' : 'not configured'}. Enter a new environment-variable name only to change the credential; never paste a secret value. Updates apply to pending and future events; no test event is sent.`;
          updateIntegrationForm();
          $('obs-integration-url').focus();
        });
        const toggle = document.createElement('button'); toggle.className = 'button button-secondary button-small'; toggle.type = 'button'; toggle.textContent = item.enabled ? 'Disable' : 'Enable';
        toggle.addEventListener('click', async () => { toggle.disabled = true; try { await api(`/api/admin/observability/integrations/${encodeURIComponent(item.destination_id)}/enabled`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: !item.enabled }) }); $('obs-integration-message').textContent = `Destination ${item.enabled ? 'disabled' : 'enabled'}. Security-event delivery updates in the background.`; await loadIntegrations(); } catch (error) { $('obs-integration-message').textContent = error.message || 'Could not change destination state.'; } });
        const test = document.createElement('button'); test.className = 'button button-secondary button-small'; test.type = 'button'; test.textContent = 'Send test event'; test.title = 'Sends one signed test event to this external endpoint now.';
        test.addEventListener('click', async () => { if (!window.confirm(`Send a test event over ${item.transport} to ${item.endpoint_host}?`)) return; test.disabled = true; $('obs-integration-message').textContent = 'Sending one test event to the configured public endpoint…'; try { const sent = await api(`/api/admin/observability/integrations/${encodeURIComponent(item.destination_id)}/test`, { method: 'POST' }); $('obs-integration-message').textContent = sent.message || (sent.ok ? 'The endpoint accepted the test.' : `Test failed: ${sent.error_code || 'unknown error'}`); } catch (error) { $('obs-integration-message').textContent = error.message || 'The test event could not be sent.'; } finally { test.disabled = false; await loadIntegrations(); } });
        const remove = document.createElement('button'); remove.className = 'button button-secondary button-small'; remove.type = 'button'; remove.textContent = 'Remove'; remove.setAttribute('aria-label', `Remove ${item.display_name} destination`);
        remove.addEventListener('click', async () => {
          if (!window.confirm(`Remove “${item.display_name}” (${item.endpoint_host})? Future queued deliveries will stop and the destination will be hidden. Local event and delivery history is retained. One send already in progress may still complete.`)) return;
          remove.disabled = true;
          try {
            await api(`/api/admin/observability/integrations/${encodeURIComponent(item.destination_id)}`, { method: 'DELETE' });
            $('obs-integration-message').textContent = 'Destination removed. Existing event and delivery history remains in VAOS.';
            await loadIntegrations();
          } catch (error) { $('obs-integration-message').textContent = error.message || 'Destination could not be removed.'; remove.disabled = false; }
        });
        actions.append(edit, toggle, test, remove);
      }
      return true;
    } catch (error) { $('obs-integration-message').textContent = error.message || 'Integration status could not be loaded.'; return false; }
    finally { state.integrationBusy = false; }
  }
  async function loadRetentionPolicy() {
    if (state.retentionBusy) return;
    const requestId = ++state.retentionRequestId;
    const policy = await api('/api/admin/observability/retention');
    if (state.retentionBusy || requestId !== state.retentionRequestId) return;
    state.retentionPolicy = policy;
    if (!state.retentionDirty) {
      $('obs-retention-events').value = String(policy.event_details_days);
      $('obs-retention-metrics').value = String(policy.metric_observations_max_days);
      $('obs-retention-deliveries').value = String(policy.delivery_history_days);
      $('obs-retention-summary').textContent = `Events ${policy.event_details_days}d · Metrics ≤${policy.metric_observations_max_days}d · Delivery ${policy.delivery_history_days}d`;
      $('obs-retention-message').textContent = 'Current policy loaded. Shorter periods require an extra confirmation.';
    } else {
      $('obs-retention-summary').textContent = 'Unsaved retention changes';
      $('obs-retention-message').textContent = 'Your unsaved selections are preserved. Save them before applying cleanup.';
    }
    syncRetentionControls();
  }
  function syncRetentionControls() {
    const unavailable = !state.retentionPolicy;
    const busy = state.retentionBusy || unavailable;
    for (const id of ['obs-retention-events', 'obs-retention-metrics', 'obs-retention-deliveries', 'obs-retention-save']) $(id).disabled = busy;
    $('obs-retention-apply').disabled = busy || state.retentionDirty;
  }
  function markRetentionPolicyDirty() {
    if (!state.retentionPolicy) return;
    state.retentionDirty = true;
    $('obs-retention-summary').textContent = 'Unsaved retention changes';
    $('obs-retention-message').textContent = 'Your selections are not saved yet. Save them before applying cleanup.';
    syncRetentionControls();
  }
  async function saveRetentionPolicy(event) {
    event.preventDefault();
    if (state.retentionBusy || !state.retentionPolicy) return;
    const next = {
      event_details_days: Number($('obs-retention-events').value),
      metric_observations_max_days: Number($('obs-retention-metrics').value),
      delivery_history_days: Number($('obs-retention-deliveries').value),
    };
    const shorter = ['event_details_days', 'metric_observations_max_days', 'delivery_history_days']
      .some((key) => next[key] < state.retentionPolicy[key]);
    if (shorter && !window.confirm('A shorter retention period can permanently redact or remove older operational details during cleanup. Lifetime counters and the append-only security audit remain. Save this shorter period?')) {
      $('obs-retention-message').textContent = 'No retention settings changed.';
      return;
    }
    state.retentionRequestId += 1;
    state.retentionBusy = true; syncRetentionControls();
    $('obs-retention-message').textContent = 'Saving retention settings…';
    try {
      const result = await api('/api/admin/observability/retention', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...next, confirm_shorter: shorter }),
      });
      state.retentionPolicy = result;
      state.retentionDirty = false;
      $('obs-retention-events').value = String(result.event_details_days);
      $('obs-retention-metrics').value = String(result.metric_observations_max_days);
      $('obs-retention-deliveries').value = String(result.delivery_history_days);
      $('obs-retention-summary').textContent = `Events ${result.event_details_days}d · Metrics ≤${result.metric_observations_max_days}d · Delivery ${result.delivery_history_days}d`;
      $('obs-retention-message').textContent = result.message || 'Retention settings saved.';
    } catch (error) {
      $('obs-retention-message').textContent = error.message || 'Retention settings could not be saved.';
    } finally {
      state.retentionBusy = false; syncRetentionControls();
    }
  }
  async function applyRetentionNow() {
    if (state.retentionBusy) return;
    if (state.retentionDirty) {
      $('obs-retention-message').textContent = 'Save your retention changes before applying cleanup. Cleanup uses the saved policy.';
      return;
    }
    if (!window.confirm('Apply the saved cleanup policy now? Expired metric samples and completed delivery history may be removed, and expired event details may be permanently redacted. Lifetime counters and the append-only security audit are preserved. Generated CSV/PDF reports are separate copies and remain available until their own 24-hour expiry.')) return;
    state.retentionRequestId += 1;
    state.retentionBusy = true; syncRetentionControls();
    $('obs-retention-message').textContent = 'Applying the saved cleanup policy…';
    try {
      const result = await api('/api/admin/observability/retention', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirm_expired_data_redaction: true }),
      });
      $('obs-retention-message').textContent = `${result.message} Removed ${result.metric_observations_removed} metric sample(s), redacted ${result.event_details_redacted} event detail record(s), and removed ${result.delivery_events_removed} completed delivery record(s).`;
    } catch (error) {
      $('obs-retention-message').textContent = error.message || 'Retention cleanup could not be applied.';
    } finally {
      state.retentionBusy = false; syncRetentionControls();
    }
  }
  async function saveIntegration(event) {
    event.preventDefault();
    const name = $('obs-integration-name').value.trim(), endpoint = $('obs-integration-url').value.trim(), transport = $('obs-integration-transport').value, eventFormat = transport === 'otlp' ? 'json' : $('obs-integration-event-format').value, secretRef = $('obs-integration-secret-ref').value.trim();
    const keepingExistingReference = Boolean(state.editingDestinationId && state.editingTransport === transport && !secretRef);
    if (!name || !endpoint || (transport === 'https_json' && !secretRef && !keepingExistingReference)) { $('obs-integration-message').textContent = 'Enter a name and the complete valid receiver URL. Signed HTTPS needs an HMAC environment-variable name unless an existing reference is being kept. Never paste a secret value.'; return; }
    const payload = { display_name: name, transport, event_format: eventFormat, endpoint, enabled: $('obs-integration-enabled').checked };
    if (secretRef) payload.secret_ref = secretRef;
    else if (!state.editingDestinationId || state.editingTransport !== transport) payload.secret_ref = '';
    const button = $('obs-integration-save'); button.disabled = true;
    try {
      const editing = Boolean(state.editingDestinationId);
      const path = editing ? `/api/admin/observability/integrations/${encodeURIComponent(state.editingDestinationId)}` : '/api/admin/observability/integrations';
      await api(path, { method: editing ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
      resetIntegrationForm(); $('obs-integration-message').textContent = editing ? 'Destination updated. No test event was sent; outbound delivery follows the saved enable setting.' : 'Destination saved. No test event was sent; outbound delivery only occurs when you enable it or choose “Send test event”.';
      await loadIntegrations();
    } catch (error) { $('obs-integration-message').textContent = error.message || 'Destination could not be saved.'; }
    finally { button.disabled = false; }
  }
  async function createReport(event) {
    event.preventDefault();
    const reportType = $('obs-report-type').value, filters = {};
    if (reportType !== 'lifetime-statistics') {
      const after = customIso($('obs-report-after').value), before = customIso($('obs-report-before').value);
      if (!after || !before || Date.parse(after) >= Date.parse(before) || Date.parse(before) - Date.parse(after) > 366 * 86400000) { $('obs-report-message').textContent = 'Choose a valid range up to 366 days. The end time is exclusive.'; return; }
      filters.after = after; filters.before = before;
    }
    if (reportType === 'metric-series') {
      const metric = $('obs-report-metric').value;
      if (!metric) { $('obs-report-message').textContent = 'Choose a metric that has recorded history.'; return; }
      filters.metric = metric;
    } else {
      const level = $('obs-report-level').value, component = $('obs-report-component').value.trim(), eventType = $('obs-report-event-type').value.trim(), correlationId = $('obs-report-correlation-id').value.trim(), search = $('obs-report-search').value.trim();
      const outcome = $('obs-report-outcome').value.trim();
      const actorRef = $('obs-report-actor-ref').value.trim();
      if (level && reportType === 'events') filters.level = level;
      if (component && ['events', 'security-events'].includes(reportType)) filters.component = component;
      if (eventType && ['events', 'security-events', 'training', 'models', 'mcp-activity'].includes(reportType)) filters.event_type = eventType;
      if (correlationId && reportType === 'events') filters.correlation_id = correlationId;
      if (search && ['events', 'security-events', 'training', 'datasets', 'models', 'cameras', 'tracking-events', 'mcp-activity'].includes(reportType)) filters.search = search;
      if (outcome && reportType === 'security-events') filters.outcome = outcome;
    if (actorRef && reportType === 'security-events') filters.actor_ref = actorRef;
    }
    const button = $('obs-report-form').querySelector('button[type="submit"]'); button.disabled = true;
    $('obs-report-message').textContent = 'Submitting report to the local background worker…';
    try {
      const job = await api('/api/admin/observability/reports/jobs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ report_type: reportType, format: $('obs-report-format').value, filters }) });
      $('obs-report-message').textContent = `Report queued · ${job.job_id}. The page will show status and a download link when it is ready.`;
      await loadReportJobs();
    } catch (error) { $('obs-report-message').textContent = error.message || 'The report could not be queued.'; }
    finally { button.disabled = false; }
  }
  function setSeriesView(view) {
    state.seriesView = view === 'table' ? 'table' : 'chart';
    const chartVisible = state.seriesView === 'chart';
    $('obs-series-chart-panel').classList.toggle('hidden', !chartVisible);
    $('obs-series-table-panel').classList.toggle('hidden', chartVisible);
    for (const [id, active] of [['obs-series-view-chart', chartVisible], ['obs-series-view-table', !chartVisible]]) {
      const button = $(id);
      button.setAttribute('aria-pressed', String(active));
      button.className = `button button-${active ? 'primary' : 'secondary'} button-small`;
    }
  }
  function clearSeries(message) {
    state.seriesController?.abort();
    state.seriesRequestId += 1;
    state.seriesController = null;
    state.seriesBusy = false;
    state.seriesQueryKey = '';
    $('obs-chart-title').textContent = 'Recorded metric history';
    $('obs-chart-desc').textContent = message;
    $('obs-chart-content').replaceChildren();
    $('obs-series-legend').replaceChildren();
    $('obs-series-status').textContent = message;
    replaceTableMessage('obs-series-rows', 4, message);
  }
  function renderSeriesMetricOptions() {
    const select = $('obs-series-metric'), current = select.value;
    const available = state.metrics.filter((metric) => metric.available && ['gauge', 'histogram', 'counter'].includes(metric.metric_type));
    select.replaceChildren();
    if (!available.length) {
      const option = document.createElement('option'); option.value = ''; option.textContent = 'No recorded metrics yet'; select.append(option); select.disabled = true;
      updateSeriesDimensionOptions();
      updateSeriesGroupAggregationOptions();
      clearSeries('No recorded metrics are available to chart yet.');
      return;
    }
    select.disabled = false;
    for (const metric of available) { const option = document.createElement('option'); option.value = metric.metric_key; option.textContent = `${metric.display_name} (${metric.unit})`; select.append(option); }
    select.value = available.some((metric) => metric.metric_key === current) ? current : available[0].metric_key;
    updateSeriesDimensionOptions();
    updateSeriesGroupAggregationOptions();
  }
  function updateSeriesDimensionOptions() {
    const select = $('obs-dimension-key'), previous = select.value;
    const metric = state.metrics.find((item) => item.metric_key === $('obs-series-metric').value);
    select.replaceChildren();
    const all = document.createElement('option'); all.value = ''; all.textContent = 'No dimension filter'; select.append(all);
    for (const key of metric?.dimensions || []) { const option = document.createElement('option'); option.value = key; option.textContent = key; select.append(option); }
    select.value = (metric?.dimensions || []).includes(previous) ? previous : '';
    select.disabled = !metric?.dimensions?.length;
    $('obs-dimension-value').disabled = !select.value;
    if (!select.value) $('obs-dimension-value').value = '';
  }
  function updateSeriesGroupAggregationOptions() {
    const metric = state.metrics.find((item) => item.metric_key === $('obs-series-metric').value);
    const group = $('obs-series-group-by'), previousGroup = state.seriesGroupBy;
    group.replaceChildren();
    const noGroup = document.createElement('option'); noGroup.value = ''; noGroup.textContent = 'All values together'; group.append(noGroup);
    for (const key of metric?.dimensions || []) { const option = document.createElement('option'); option.value = key; option.textContent = key; group.append(option); }
    group.value = (metric?.dimensions || []).includes(previousGroup) ? previousGroup : ((metric?.dimensions || [])[0] || '');
    state.seriesGroupBy = group.value;
    group.disabled = !metric?.dimensions?.length;
    const aggregation = $('obs-series-aggregation'), previousAggregation = aggregation.value || 'raw';
    const choices = [['raw', 'Raw points'], ...(metric?.series_aggregations || []).map((name) => [name, `${name[0].toUpperCase()}${name.slice(1)} per bucket`])];
    aggregation.replaceChildren();
    for (const [value, label] of choices) { const option = document.createElement('option'); option.value = value; option.textContent = label; aggregation.append(option); }
    aggregation.value = choices.some(([value]) => value === previousAggregation) ? previousAggregation : 'raw';
    aggregation.disabled = !metric;
    $('obs-series-interval').disabled = !metric || aggregation.value === 'raw';
  }
  function drawSeries(items, metric, { after, before, dimensions = {}, groupBy = '', aggregation = 'raw', interval = 'hour', total = null, groupCount = null, truncated = false } = {}) {
    const group = $('obs-chart-content'); group.replaceChildren();
    const legend = $('obs-series-legend'); legend.replaceChildren();
    const desc = $('obs-chart-desc');
    const title = $('obs-chart-title');
    const metricName = metric?.display_name || 'Recorded metric';
    const seriesLabel = aggregation === 'raw' ? 'Raw points' : `${aggregation} per ${interval}`;
    title.textContent = `${metricName} history · ${seriesLabel}`;
    const rangeText = after && before ? ` from ${formatUtc(after)} through ${formatUtc(before)}` : '';
    const dimensionText = Object.keys(dimensions).length ? `, filtered by ${JSON.stringify(dimensions)}` : '';
    const groupSeries = new Map();
    if (!items.length) {
      desc.textContent = `${metricName}: no observations were recorded${rangeText}${dimensionText}.`;
      const text = document.createElementNS('http://www.w3.org/2000/svg', 'text'); text.setAttribute('x', '450'); text.setAttribute('y', '122'); text.setAttribute('text-anchor', 'middle'); text.setAttribute('class', 'chart-empty'); text.textContent = 'No observations in this period'; group.append(text); return;
    }
    const points = items.map((item) => {
      const time = Date.parse(item.timestamp), value = Number(item.value);
      return Number.isFinite(time) && Number.isFinite(value) ? { time, value, item } : null;
    }).filter(Boolean);
    if (!points.length) {
      desc.textContent = `${metricName}: observations were returned${rangeText}${dimensionText}, but none had both a usable timestamp and numeric value to plot.`;
      const text = document.createElementNS('http://www.w3.org/2000/svg', 'text'); text.setAttribute('x', '450'); text.setAttribute('y', '122'); text.setAttribute('text-anchor', 'middle'); text.setAttribute('class', 'chart-empty'); text.textContent = 'No plottable observations'; group.append(text); return;
    }
    points.sort((left, right) => left.time - right.time);
    for (const point of points) {
      const rawName = groupBy ? point.item.dimensions?.[groupBy] : null;
      const name = groupBy ? String(rawName ?? '(not set)') : 'All values together';
      if (!groupSeries.has(name)) groupSeries.set(name, []);
      groupSeries.get(name).push(point);
    }
    const allSeries = [...groupSeries.entries()].sort((left, right) => right[1].length - left[1].length || left[0].localeCompare(right[0]));
    const displayedSeries = allSeries.slice(0, 8), omittedSeries = Math.max(0, allSeries.length - displayedSeries.length);
    const parsedGroupCount = groupCount === null || groupCount === undefined ? Number.NaN : Number(groupCount);
    const allGroupCount = Number.isFinite(parsedGroupCount) ? parsedGroupCount : allSeries.length;
    const omittedFromPage = Math.max(0, allGroupCount - allSeries.length);
    const displayedPoints = displayedSeries.flatMap(([, seriesPoints]) => seriesPoints);
    const values = displayedPoints.map((point) => point.value);
    const min = Math.min(...values), max = Math.max(...values), pad = Math.max((max - min) * 0.12, Math.abs(max) * 0.02, 0.000001);
    const low = min - pad, high = max + pad, x0 = 60, x1 = 880, y0 = 24, y1 = 194;
    for (let i = 0; i <= 4; i++) {
      const y = y0 + (y1 - y0) * i / 4, line = document.createElementNS('http://www.w3.org/2000/svg', 'line'); line.setAttribute('x1', String(x0)); line.setAttribute('x2', String(x1)); line.setAttribute('y1', String(y)); line.setAttribute('y2', String(y)); line.setAttribute('class', 'chart-grid'); group.append(line);
      const label = document.createElementNS('http://www.w3.org/2000/svg', 'text'); label.setAttribute('x', '52'); label.setAttribute('y', String(y + 4)); label.setAttribute('text-anchor', 'end'); label.setAttribute('class', 'chart-axis'); label.textContent = (high - (high - low) * i / 4).toPrecision(4); group.append(label);
    }
    const times = displayedPoints.map((point) => point.time), t0 = Math.min(...times), t1 = Math.max(...times);
    for (const point of displayedPoints) {
      point.x = t1 === t0 ? (x0 + x1) / 2 : x0 + (point.time - t0) / (t1 - t0) * (x1 - x0);
      point.y = y1 - (point.value - low) / (high - low) * (y1 - y0);
    }
    const seriesNames = displayedSeries.map(([name]) => name);
    displayedSeries.forEach(([name, seriesPoints], index) => {
      const colorClass = `chart-series-color-${index % 8}`;
      if (seriesPoints.length > 1) { const line = document.createElementNS('http://www.w3.org/2000/svg', 'polyline'); line.setAttribute('points', seriesPoints.map((point) => `${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(' ')); line.setAttribute('class', `chart-line ${colorClass}`); group.append(line); }
      for (const point of seriesPoints) { const dot = document.createElementNS('http://www.w3.org/2000/svg', 'circle'); dot.setAttribute('cx', String(point.x)); dot.setAttribute('cy', String(point.y)); dot.setAttribute('r', '3.5'); dot.setAttribute('class', `chart-point ${colorClass}`); const pointTitle = document.createElementNS('http://www.w3.org/2000/svg', 'title'); pointTitle.textContent = `${name} · ${formatUtc(point.item.timestamp)} · ${point.value} ${metric.unit}`; dot.append(pointTitle); group.append(dot); }
      const entry = document.createElement('span'); entry.className = 'observability-chart-legend-item'; entry.setAttribute('role', 'listitem'); const swatch = document.createElement('span'); swatch.className = `observability-chart-swatch ${colorClass}`; swatch.setAttribute('aria-hidden', 'true'); const label = document.createElement('span'); label.textContent = `${name} (${seriesPoints.length})`; entry.append(swatch, label); legend.append(entry);
    });
    if (omittedSeries) { const note = document.createElement('span'); note.className = 'observability-chart-legend-item'; note.textContent = `${omittedSeries} lower-count group(s) omitted from chart; use an exact filter to inspect them.`; legend.append(note); }
    const omittedText = omittedSeries ? `; ${omittedSeries} lower-count group(s) omitted from this chart, selected from the returned points` : '';
    const parsedTotal = total === null || total === undefined ? Number.NaN : Number(total);
    const pageText = truncated ? `; the response is limited to ${points.length} of ${Number.isFinite(parsedTotal) ? parsedTotal : points.length} matching points or buckets` : '';
    const omittedPageGroupsText = omittedFromPage ? `; ${omittedFromPage} query group(s) have no points on this returned page` : '';
    desc.textContent = `${metricName}: ${displayedPoints.length} plotted ${aggregation === 'raw' ? 'observation(s)' : 'time bucket(s)'} in ${seriesNames.length} displayed series${omittedText}; ${points.length} valid points were returned in total across ${allGroupCount} query group(s)${omittedPageGroupsText}; ${seriesLabel}; displayed values range from ${min} to ${max} ${metric.unit}; displayed data runs from ${formatUtc(t0)} through ${formatUtc(t1)}${groupBy ? `; grouped by ${groupBy}` : ''}${rangeText}${dimensionText}${pageText}.`;
  }
  async function loadSeries() {
    const metricKey = $('obs-series-metric').value;
    if (!metricKey) { clearSeries('Select a metric to load its history.'); return; }
    const params = new URLSearchParams();
    let range = selectedRange();
    if (state.period === 'lifetime') { const end = new Date(); range = { after: new Date(end.getTime() - 366 * 86400000).toISOString(), before: end.toISOString() }; }
    params.set('metric', metricKey); params.set('after', range.after); params.set('before', range.before); params.set('limit', '1000');
    const aggregation = $('obs-series-aggregation').value || 'raw', interval = $('obs-series-interval').value || 'hour', groupBy = $('obs-series-group-by').value;
    params.set('aggregation', aggregation); params.set('interval', interval); if (groupBy) params.set('group_by', groupBy);
    const dimensionKey = $('obs-dimension-key').value, dimensionValue = $('obs-dimension-value').value.trim();
    if (dimensionKey && dimensionValue) params.set('dimensions', JSON.stringify({ [dimensionKey]: dimensionValue }));
    const queryKey = params.toString(), requestId = ++state.seriesRequestId;
    state.seriesController?.abort();
    const controller = new AbortController(); state.seriesController = controller; state.seriesBusy = true;
    if (queryKey !== state.seriesQueryKey) {
      $('obs-chart-content').replaceChildren();
      $('obs-series-legend').replaceChildren();
      replaceTableMessage('obs-series-rows', 4, 'Loading metric history for the selected controls…');
    }
    const selectedMetric = state.metrics.find((item) => item.metric_key === metricKey);
    $('obs-chart-title').textContent = `${selectedMetric?.display_name || 'Recorded metric'} history`;
    $('obs-chart-desc').textContent = 'Loading observations for the selected metric and filters.';
    state.seriesQueryKey = queryKey; $('obs-series-status').textContent = 'Loading recorded metric history…';
    try {
      const result = await api(`/api/admin/observability/series?${params}`, { signal: controller.signal });
      if (requestId !== state.seriesRequestId) return;
      const metric = result.metric;
      drawSeries(result.items || [], metric, { after: range.after, before: range.before, dimensions: result.dimensions || {}, groupBy: result.group_by || '', aggregation: result.aggregation || 'raw', interval: result.interval || interval, total: result.total, groupCount: result.group_count, truncated: result.truncated });
      const rows = $('obs-series-rows'); rows.replaceChildren();
      for (const item of (result.items || []).slice(-20).reverse()) {
        const row = rows.insertRow();
        for (const value of [formatUtc(item.timestamp), `${Number(item.value).toLocaleString(undefined, { maximumFractionDigits: 4 })} ${metric.unit}`, JSON.stringify(item.dimensions || {}), `${item.provenance} · ${item.measurement_scope}${item.estimated ? ' · estimated' : ' · measured'}`]) { const cell = row.insertCell(); cell.textContent = value; }
      }
      if (!result.items?.length) { const row = rows.insertRow(); const cell = row.insertCell(); cell.colSpan = 4; cell.textContent = 'No observations for this metric in the selected period.'; }
      $('obs-series-status').textContent = `${result.count} of ${result.total ?? result.count} ${aggregation === 'raw' ? 'recorded point(s)' : 'time bucket(s)'} · ${aggregation === 'raw' ? 'raw values' : `${aggregation} per ${interval}`} · ${result.group_count || 0} group(s)${groupBy ? ` by ${groupBy}` : ''}${Object.keys(result.dimensions || {}).length ? ` · filtered by ${JSON.stringify(result.dimensions)}` : ''}${result.truncated ? ' · showing the newest 1,000 points; narrow the range for full history' : ''}. Latest 20 are listed below.`;
    } catch (error) {
      if (requestId !== state.seriesRequestId) return null;
      if (isAbortError(error)) return null;
      $('obs-chart-desc').textContent = 'Metric history could not be loaded. Previous results were cleared.';
      $('obs-chart-content').replaceChildren(); $('obs-series-legend').replaceChildren(); replaceTableMessage('obs-series-rows', 4, 'Metric history could not be loaded. Previous results were cleared.');
      $('obs-series-status').textContent = error.message || 'Metric history could not be loaded.';
      return false;
    } finally {
      if (requestId === state.seriesRequestId) { state.seriesBusy = false; state.seriesController = null; }
    }
    return true;
  }
  async function createMetricSeriesReport(format) {
    const button = $(`obs-series-export-${format}`);
    button.disabled = true;
    $('obs-series-export-status').textContent = `Preparing the current metric view as a ${format.toUpperCase()} report…`;
    try {
      const metric = $('obs-series-metric').value;
      if (!metric) throw new Error('Choose a metric with recorded history first.');
      let range = selectedRange();
      if (state.period === 'lifetime') { const end = new Date(); range = { after: new Date(end.getTime() - 366 * 86400000).toISOString(), before: end.toISOString() }; }
      const filters = {
        metric, after: range.after, before: range.before,
        aggregation: $('obs-series-aggregation').value || 'raw',
        interval: $('obs-series-interval').value || 'hour',
      };
      const groupBy = $('obs-series-group-by').value, dimensionKey = $('obs-dimension-key').value, dimensionValue = $('obs-dimension-value').value.trim();
      if (groupBy) filters.group_by = groupBy;
      if (dimensionKey && dimensionValue) filters.dimensions = { [dimensionKey]: dimensionValue };
      const job = await api('/api/admin/observability/reports/jobs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ report_type: 'metric-series', format, filters }) });
      $('obs-series-export-status').textContent = `${format.toUpperCase()} report queued · ${job.job_id}. Check Reports & exports below for progress and download.`;
      await loadReportJobs();
    } catch (error) {
      $('obs-series-export-status').textContent = error.message || 'The metric report could not be queued.';
    } finally { button.disabled = false; }
  }
  async function refresh({ history = true } = {}) {
    if (state.busy) { state.refreshPending = true; state.refreshPendingHistory = state.refreshPendingHistory || history; return; }
    state.busy = true; $('obs-refresh').disabled = true; setStatus('Refreshing locally stored statistics…');
    try {
      const queueStatusRequest = api('/api/admin/observability/counter-queue')
        .then(renderCounterQueueStatus)
        .catch(() => renderCounterQueueUnavailable());
      const [overview, metrics] = await Promise.all([
        api(`/api/admin/observability/overview?${periodQuery()}`),
        api('/api/admin/observability/metrics?limit=200'),
      ]);
      await queueStatusRequest;
      renderOverview(overview); renderMetrics(metrics.items || []); renderSeriesMetricOptions(); updateReportMetricOptions();
      const jobs = [
        ['integrations', () => loadIntegrations()],
        ['retention policy', () => loadRetentionPolicy()],
        ['metric history', () => loadSeries()],
      ];
      if (history) jobs.unshift(
        ['runtime sessions', () => loadSessions(false)],
        ['log events', () => loadEvents()],
        ['security audit', () => loadSecurityAudit()],
        ['reports', () => loadReportJobs()],
      );
      const settled = await Promise.allSettled(jobs.map(([, run]) => run()));
      const failures = settled.flatMap((result, index) => result.status === 'rejected' || result.value === false ? [jobs[index][0]] : []);
      const suffix = failures.length ? ` Some sections could not refresh: ${failures.join(', ')}.` : '';
      setStatus(`Statistics refreshed · ${overview.period} · UTC. Older history that VAOS did not record is not reconstructed.${suffix}`, failures.length > 0);
    } catch (error) { setStatus(error.message || 'Observability data could not be loaded.', true); }
    finally {
      $('obs-refresh').disabled = false; state.busy = false;
      if (state.refreshPending) {
        const pendingHistory = state.refreshPendingHistory; state.refreshPending = false; state.refreshPendingHistory = false;
        window.setTimeout(() => refresh({ history: pendingHistory }), 0);
      }
    }
  }
  $('obs-period').addEventListener('change', () => {
    state.period = $('obs-period').value; $('obs-custom-range').classList.toggle('hidden', state.period !== 'custom');
    if (state.period !== 'custom') { state.logCursor = null; state.sessionCursor = null; state.auditCursor = null; refresh(); }
  });
  $('obs-apply-range').addEventListener('click', () => {
    state.after = $('obs-after').value; state.before = $('obs-before').value;
    if (!state.after || !state.before || Date.parse(customIso(state.after)) >= Date.parse(customIso(state.before))) { setStatus('Enter a valid UTC range. The start must be earlier than the exclusive end.', true); return; }
    state.logCursor = null; state.sessionCursor = null; state.auditCursor = null; refresh();
  });
  $('obs-refresh').addEventListener('click', refresh);
  let metricSearchTimer;
  $('obs-metric-search').addEventListener('input', () => { window.clearTimeout(metricSearchTimer); metricSearchTimer = window.setTimeout(() => renderMetrics(state.metrics), 140); });
  function setMetricGroupsExpanded(expanded) {
    const categories = new Set(state.metrics.map((metric) => metric.metric_key.split('.')[0] || 'other'));
    categories.forEach((name) => state.metricExpanded.set(name, expanded));
    document.querySelectorAll('.observability-metric-group').forEach((details) => { details.open = expanded; });
  }
  $('obs-expand-metric-groups').addEventListener('click', () => setMetricGroupsExpanded(true));
  $('obs-collapse-metric-groups').addEventListener('click', () => setMetricGroupsExpanded(false));
  $('obs-series-metric').addEventListener('change', () => { updateSeriesDimensionOptions(); updateSeriesGroupAggregationOptions(); loadSeries(); });
  $('obs-series-view-chart').addEventListener('click', () => setSeriesView('chart'));
  $('obs-series-view-table').addEventListener('click', () => setSeriesView('table'));
  $('obs-series-group-by').addEventListener('change', () => { state.seriesGroupBy = $('obs-series-group-by').value; loadSeries(); });
  $('obs-series-aggregation').addEventListener('change', () => { $('obs-series-interval').disabled = $('obs-series-aggregation').value === 'raw'; loadSeries(); });
  $('obs-series-interval').addEventListener('change', loadSeries);
  $('obs-dimension-key').addEventListener('change', () => { $('obs-dimension-value').disabled = !$('obs-dimension-key').value; if (!$('obs-dimension-key').value) $('obs-dimension-value').value = ''; loadSeries(); });
  $('obs-dimension-value').addEventListener('change', loadSeries);
  $('obs-series-export-csv').addEventListener('click', () => createMetricSeriesReport('csv'));
  $('obs-series-export-pdf').addEventListener('click', () => createMetricSeriesReport('pdf'));
  $('obs-log-filters').addEventListener('submit', (event) => { event.preventDefault(); state.logCursor = null; loadEvents().catch((error) => { $('obs-log-message').textContent = error.message; }); });
  $('obs-save-log-filter').addEventListener('click', saveCurrentLogFilter);
  $('obs-saved-log-filters').addEventListener('change', () => { const selected = Boolean($('obs-saved-log-filters').value); $('obs-apply-log-filter').disabled = !selected; $('obs-delete-log-filter').disabled = !selected; });
  $('obs-apply-log-filter').addEventListener('click', () => applySavedLogFilter().catch((error) => { $('obs-saved-log-filter-status').textContent = error.message || 'Saved filter could not be applied.'; }));
  $('obs-delete-log-filter').addEventListener('click', deleteSelectedLogFilter);
  $('obs-log-tail-toggle').addEventListener('click', () => setLogTail(!state.logTailActive));
  $('obs-log-export-csv').addEventListener('click', () => createLogReport('csv'));
  $('obs-log-export-pdf').addEventListener('click', () => createLogReport('pdf'));
  $('obs-log-export-ndjson').addEventListener('click', downloadLogNdjson);
  $('obs-log-next').addEventListener('click', () => { if (state.logTailActive) setLogTail(false); loadEvents({ next: true }).catch((error) => { $('obs-log-message').textContent = error.message; }); });
  $('obs-audit-filters').addEventListener('submit', (event) => { event.preventDefault(); state.auditCursor = null; loadSecurityAudit().catch((error) => { $('obs-audit-message').textContent = error.message; }); });
  $('obs-audit-next').addEventListener('click', () => loadSecurityAudit({ next: true }).catch((error) => { $('obs-audit-message').textContent = error.message; }));
  $('obs-audit-verify').addEventListener('click', async () => { const button = $('obs-audit-verify'); button.disabled = true; $('obs-audit-integrity').textContent = 'Verifying the stored hash chain…'; try { const result = await api('/api/admin/observability/security-audit/integrity'); $('obs-audit-integrity').textContent = result.valid ? `Integrity valid · ${result.checked} record(s) checked.` : `Integrity check failed at record ${result.failed_sequence} after ${result.checked} row(s).`; } catch (error) { $('obs-audit-integrity').textContent = error.message || 'Integrity could not be checked.'; } finally { button.disabled = false; } });
  $('obs-session-older').addEventListener('click', () => loadSessions(true).catch((error) => { setStatus(error.message, true); }));
  function reportPrompt(type) {
    return type === 'lifetime-statistics'
      ? 'Choose a format, then select Generate report.'
      : 'Choose a date range and format, then select Generate report.';
  }
  function updateReportForm() {
    const type = $('obs-report-type').value, metricReport = type === 'metric-series', lifetime = type === 'lifetime-statistics';
    const definition = state.reportTypes.find((entry) => entry.report_type === type);
    $('obs-report-coverage').textContent = definition ? `${definition.coverage} Source: ${definition.source}. Reports include a provenance manifest and never contain raw camera frames.` : 'Report coverage information is unavailable.';
    $('obs-report-message').textContent = reportPrompt(type);
    const eventTypeReport = ['events', 'security-events', 'training', 'models', 'mcp-activity'].includes(type);
    const searchReport = ['events', 'security-events', 'training', 'datasets', 'models', 'cameras', 'tracking-events', 'mcp-activity'].includes(type);
    $('obs-report-metric-wrap').classList.toggle('hidden', !metricReport);
    for (const id of ['obs-report-after', 'obs-report-before']) { const input = $(id); input.required = !lifetime; input.closest('label').classList.toggle('hidden', lifetime); }
    $('obs-report-level').closest('label').classList.toggle('hidden', type !== 'events');
    $('obs-report-outcome-wrap').classList.toggle('hidden', type !== 'security-events');
    $('obs-report-actor-ref-wrap').classList.toggle('hidden', type !== 'security-events');
    $('obs-report-component').closest('label').classList.toggle('hidden', !['events', 'security-events'].includes(type));
    $('obs-report-event-type').closest('label').classList.toggle('hidden', !eventTypeReport);
    $('obs-report-correlation-wrap').classList.toggle('hidden', type !== 'events');
    $('obs-report-search').closest('label').classList.toggle('hidden', !searchReport);
  }
  $('obs-report-type').addEventListener('change', updateReportForm);
  $('obs-report-form').addEventListener('submit', createReport);
  $('obs-integration-form').addEventListener('submit', saveIntegration);
  $('obs-retention-form').addEventListener('submit', saveRetentionPolicy);
  for (const id of ['obs-retention-events', 'obs-retention-metrics', 'obs-retention-deliveries']) $(id).addEventListener('change', markRetentionPolicyDirty);
  $('obs-retention-apply').addEventListener('click', applyRetentionNow);
  $('obs-integration-transport').addEventListener('change', updateIntegrationForm);
  $('obs-integration-cancel').addEventListener('click', () => { resetIntegrationForm(); $('obs-integration-message').textContent = 'Edit cancelled. No destination settings changed.'; });
  updateIntegrationForm();
  const now = new Date(); $('obs-report-before').value = localDateInput(now); $('obs-report-after').value = localDateInput(new Date(now.getTime() - 24 * 3600000));
  updateReportForm();
  loadReportTypes().catch((error) => { $('obs-report-coverage').textContent = error.message || 'Report types could not be loaded.'; });
  loadSavedLogFilters().catch((error) => { $('obs-saved-log-filter-status').textContent = error.message || 'Saved filters could not be loaded.'; });
  refresh();
  window.setInterval(() => { if (!document.hidden) refresh({ history: false }); }, 30000);
  window.setInterval(() => { if (!document.hidden) loadReportJobs(); }, 5000);
})();
