/* Read-only class-count snapshots; no training or review mutation endpoints. */
(function (root) {
  'use strict';
  const sources = {
    reviewed: {title: 'Reviewed samples', unit: 'reviewed crops'},
    model: {title: 'Last trained model', unit: 'training annotation instances'},
  };
  function rowsFrom(entries) {
    const pairs = Array.isArray(entries) ? entries.map(item => [item.label, item.count]) : Object.entries(entries);
    return pairs.map(([label, count]) => {
      if (typeof label !== 'string' || !label.trim() || typeof count !== 'number' || !Number.isSafeInteger(count) || count < 0) throw Error('Class counts are invalid. Refresh the distribution.');
      return {class_label: label, count};
    }).sort((a, b) => b.count - a.count || a.class_label.localeCompare(b.class_label));
  }
  function snapshot(data, observedAt) {
    if (!Array.isArray(data.confirmed_class_counts)) throw Error('Reviewed class counts are unavailable.');
    const reviewed = rowsFrom(data.confirmed_class_counts);
    if (new Set(reviewed.map(row => row.class_label)).size !== reviewed.length) throw Error('Duplicate reviewed class labels were returned.');
    const expected = Number(data.counts?.confirmed || 0) + Number(data.counts?.corrected || 0);
    if (reviewed.reduce((sum, row) => sum + row.count, 0) !== expected) throw Error('Reviewed totals do not match. Refresh the distribution.');
    const trained = data.trained_model || {}, meta = trained.metadata || {};
    let model = [], modelNote = '';
    if (!trained.installed) modelNote = 'No trained model available.';
    else if (!meta.class_counts || typeof meta.class_counts !== 'object' || Array.isArray(meta.class_counts)) modelNote = 'Counts were not recorded for this model.';
    else { model = rowsFrom(meta.class_counts); if (!model.length) modelNote = 'Counts were not recorded for this model.'; }
    return {observed_at_utc: observedAt || new Date().toISOString(), reviewed, model, modelNote,
      run_id: typeof meta.run_id === 'string' ? meta.run_id : '',
      trained_at: typeof meta.trained_at === 'string' ? meta.trained_at : ''};
  }
  function view(snap, source, search) {
    if (!sources[source]) throw Error('Unknown distribution source.');
    const all = snap[source], total = all.reduce((sum, row) => sum + row.count, 0);
    const query = (search || '').trim().toLocaleLowerCase();
    return {total, all, rows: all.filter(row => row.class_label.toLocaleLowerCase().includes(query)).map(row => ({...row,
      percentage_of_source_total: total ? Number((row.count * 100 / total).toFixed(4)) : 0}))};
  }
  function exportData(snap, source, search, filtered, stale) {
    const selected = view(snap, source, filtered ? search : '');
    return {schema_version: 1, source: sources[source].title, count_unit: sources[source].unit,
      observed_at_utc: snap.observed_at_utc, stale: Boolean(stale),
      run_id: source === 'model' ? snap.run_id : null,
      trained_at: source === 'model' ? snap.trained_at : null,
      source_total: selected.total, export_scope: filtered ? 'filtered classes' : 'all classes',
      class_search: filtered ? search : '', rows: selected.rows};
  }
  function csvCell(value) {
    let text = String(value ?? '');
    if (/^[\s\u0000-\u001f]*[=+@-]/.test(text) || /^[\t\r\n]/.test(text)) text = "'" + text;
    return '"' + text.replace(/"/g, '""') + '"';
  }
  function csv(data) {
    const fields = ['source','class_label','count','percentage_of_source_total','source_total','observed_at_utc','run_id','count_unit','stale'];
    return fields.join(',') + '\r\n' + data.rows.map(row => fields.map(key => csvCell(key in row ? row[key] : data[key])).join(',')).join('\r\n') + '\r\n';
  }
  const api = {snapshot, view, exportData, csv};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.VaosClassDistribution = api;
  if (typeof document === 'undefined') return;
  const $ = id => document.getElementById(id), dialog = $('class-distribution-modal');
  if (!dialog) return;
  let latest = null, current = null, stale = false, latestFailed = false, controller = null, focusBefore = null, scrollStyles = null;
  const source = () => $('distribution-source').value;
  const search = () => $('distribution-search').value;
  function status(message, error) { $('distribution-status').textContent = message; $('distribution-status').classList.toggle('error', Boolean(error)); }
  function render() {
    const tbody = $('distribution-rows'); tbody.replaceChildren();
    const valid = Boolean(current), which = source();
    let selected = {rows: [], all: [], total: 0};
    if (valid) selected = view(current, which, search());
    const missing = which === 'model' && valid && current.modelNote;
    $('distribution-csv').disabled = $('distribution-json').disabled = !valid || Boolean(missing);
    $('distribution-description').textContent = which === 'reviewed' ? 'Confirmed and corrected crops across all review pages. Pending, rejected, later and unknown samples are excluded.' : 'Annotation instances recorded for the saved model. These are not the current reviewed-crop inventory or a model-quality score.';
    $('distribution-meta').textContent = valid ? `${stale ? 'Stale snapshot · ' : 'Snapshot · '}${current.observed_at_utc}${which === 'model' ? ` · Run: ${current.run_id || 'not recorded'} · Trained: ${current.trained_at || 'not recorded'}` : ''}` : 'No snapshot available.';
    $('distribution-total').textContent = !valid || missing ? 'Counts unavailable' : `${selected.total.toLocaleString()} ${sources[which].unit} · ${selected.rows.length} of ${selected.all.length} classes shown`;
    $('distribution-empty').hidden = Boolean(selected.rows.length);
    $('distribution-empty').textContent = missing || (!valid ? 'Distribution unavailable. Use Refresh distribution to try again.' : selected.all.length ? 'No matching classes.' : which === 'reviewed' ? 'No reviewed samples yet. Approve or correct detections to populate this view.' : 'No annotation counts were recorded for this run.');
    for (const row of selected.rows) {
      const tr = document.createElement('tr');
      for (const value of [row.class_label, row.count.toLocaleString(), `${row.percentage_of_source_total.toFixed(2)}%`]) {
        const td = document.createElement('td'); td.textContent = value; tr.append(td);
      }
      const barCell = document.createElement('td'), meter = document.createElement('progress');
      meter.max = 100; meter.value = row.percentage_of_source_total; meter.setAttribute('aria-label', `${row.class_label}: ${row.percentage_of_source_total.toFixed(2)} percent of ${sources[which].unit}`);
      barCell.append(meter); tr.append(barCell); tbody.append(tr);
    }
    const rows = valid ? view(current, which, $('distribution-export-scope').value === 'filtered' ? search() : '').rows.length : 0;
    $('distribution-export-count').textContent = `${rows} rows to export · ${$('distribution-export-scope').value === 'filtered' ? 'filtered classes' : 'all classes'}`;
  }
  api.accept = function (data) {
    try {
      latest = snapshot(data); latestFailed = false;
      $('class-distribution-summary').textContent = `${latest.reviewed.length} classes · counts available`;
    } catch (error) { api.unavailable(); }
  };
  api.unavailable = function () {
    latestFailed = true;
    $('class-distribution-summary').textContent = latest ? 'Last known counts · open to refresh' : 'Counts unavailable · open to retry';
    if (dialog.open && current) { stale = true; render(); status('Background refresh failed. This timestamped snapshot may be stale.', true); }
  };
  async function refresh() {
    if (controller) return;
    controller = new AbortController(); const request = controller;
    const timeout = setTimeout(() => request.abort(), 12000);
    $('distribution-refresh').disabled = true;
    status('Loading class counts…');
    try {
      const response = await fetch('/api/admin/ml/samples?limit=1&offset=0', {credentials:'same-origin', cache:'no-store', signal:request.signal});
      if (!response.ok) throw Error(`Could not load distribution (HTTP ${response.status}). Sign in or retry.`);
      const next = snapshot(await response.json());
      if (!dialog.open || controller !== request) return;
      current = next; latest = next; latestFailed = false; stale = false;
      $('class-distribution-summary').textContent = `${next.reviewed.length} classes · counts available`;
      render(); status('Distribution refreshed. Exports use this displayed snapshot.');
    } catch (error) {
      if (!dialog.open || controller !== request) return;
      stale = Boolean(current); render();
      status(error.name === 'AbortError' ? 'The request timed out. Retry with Refresh distribution.' : error.message, true);
    } finally { clearTimeout(timeout); if (controller === request) { controller = null; $('distribution-refresh').disabled = false; } }
  }
  $('class-distribution-open').addEventListener('click', () => {
    focusBefore = document.activeElement;
    current = latest ? JSON.parse(JSON.stringify(latest)) : null; stale = latestFailed;
    $('distribution-source').value = 'reviewed'; $('distribution-search').value = '';
    $('distribution-export-scope').value = 'all';
    render(); status(current ? (stale ? 'Last successful snapshot; refresh before relying on current counts.' : 'Snapshot held steady while this modal is open. Use Refresh distribution for newer counts.') : 'Loading class counts…', stale);
    const main = document.querySelector('main');
    scrollStyles = {main, mainOverflow: main.style.overflowY, bodyOverflow: document.body.style.overflow};
    main.style.overflowY = 'hidden'; document.body.style.overflow = 'hidden';
    dialog.showModal(); $('distribution-close').focus();
    if (!current) refresh();
  });
  $('distribution-close').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => {
    if (controller) { controller.abort(); controller = null; $('distribution-refresh').disabled = false; }
    if (scrollStyles) { scrollStyles.main.style.overflowY = scrollStyles.mainOverflow; document.body.style.overflow = scrollStyles.bodyOverflow; scrollStyles = null; }
    focusBefore?.focus();
  });
  $('distribution-source').addEventListener('change', render);
  $('distribution-search').addEventListener('input', render);
  $('distribution-export-scope').addEventListener('change', render);
  $('distribution-refresh').addEventListener('click', refresh);
  for (const format of ['csv','json']) $('distribution-' + format).addEventListener('click', () => {
    if (!current) return;
    try {
      const data = exportData(current, source(), search(), $('distribution-export-scope').value === 'filtered', stale);
      const blob = new Blob([format === 'csv' ? '\ufeff' + csv(data) : JSON.stringify(data, null, 2)], {type: format === 'csv' ? 'text/csv;charset=utf-8' : 'application/json'});
      const url = URL.createObjectURL(blob), anchor = document.createElement('a');
      anchor.href = url; anchor.download = `vaos-class-distribution-${source()}-${current.observed_at_utc.replace(/[^0-9TZ]/g,'')}.${format}`;
      document.body.append(anchor); anchor.click(); anchor.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
      status(`Export requested: ${data.rows.length} rows as ${format.toUpperCase()}${stale ? ' (stale snapshot)' : ''}. Check browser downloads.`);
    } catch (error) { status('Export could not be prepared. Retry the download.', true); }
  });
})(typeof globalThis !== 'undefined' ? globalThis : this);
