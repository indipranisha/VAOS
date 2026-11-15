(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const methods = ['get', 'post', 'put', 'patch', 'delete', 'head', 'options'];
  let schema = null;
  let operations = [];
  let selected = null;

  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[char]));

  function showMessage(text, isError = false) {
    const element = $('explorer-message');
    element.textContent = text;
    element.classList.toggle('error', isError);
  }

  function resolveRef(ref) {
    if (!ref?.startsWith('#/')) return {};
    return ref.slice(2).split('/').reduce((value, part) => {
      const key = part.replace(/~1/g, '/').replace(/~0/g, '~');
      return value?.[key];
    }, schema) || {};
  }

  function dereference(node) {
    let value = node || {};
    for (let depth = 0; depth < 8 && value.$ref; depth += 1) value = resolveRef(value.$ref);
    return value;
  }

  function exampleFor(node) {
    const value = dereference(node);
    if (value.example !== undefined) return value.example;
    if (value.default !== undefined) return value.default;
    if (value.enum?.length) return value.enum[0];
    if (value.type === 'array') return [value.items ? exampleFor(value.items) : ''];
    if (value.type === 'object' || value.properties) {
      const result = {};
      for (const [key, child] of Object.entries(value.properties || {})) {
        if (!value.required || value.required.includes(key)) result[key] = exampleFor(child);
      }
      return result;
    }
    if (value.type === 'integer' || value.type === 'number') return 1;
    if (value.type === 'boolean') return false;
    if (value.type === 'string') return value.format === 'date-time' ? '2026-01-01T00:00:00Z' : '';
    return {};
  }

  function requestBody(operation) {
    return dereference(operation.requestBody || {});
  }

  function bodyContract(operation) {
    const content = requestBody(operation).content || {};
    const type = Object.keys(content)[0] || '';
    return { content, type, schema: dereference(content[type]?.schema || {}) };
  }

  function groupFor(path, tags) {
    if (tags?.length) return tags[0];
    const rules = [
      [/\/hardware$/, 'Hardware'],
      [/\/status$|\/health$/, 'System and diagnostics'],
      [/\/camera|\/cameras|\/enrollment-links/, 'Cameras and enrollment'],
      [/\/participants|\/enroll\//, 'People'],
      [/\/events|\/sightings/, 'Activity'],
      [/\/samples|\/review|\/capture/, 'Review samples'],
      [/\/ml\/train|\/ml\/logs|\/ml\/history/, 'Training'],
      [/\/datasets|\/dataset/, 'Datasets'],
      [/\/model|\/huggingface|\/enrichment/, 'Models'],
      [/\/tunnel|\/services|\/engine-settings|\/model-selection/, 'Settings and services'],
      [/\/video-review/, 'Video review'],
      [/\/api-tokens|\/integrations|\/mcp-approvals/, 'API and MCP'],
    ];
    return rules.find(([pattern]) => pattern.test(path))?.[1] || 'Other';
  }

  function renderBodyEditor(operation) {
    const { type, schema: bodySchema } = bodyContract(operation);
    if (!type) return '';
    if (type === 'application/json') {
      return `<label>JSON request body<textarea id="explorer-body" rows="10" spellcheck="false">${esc(JSON.stringify(exampleFor(bodySchema), null, 2))}</textarea></label>`;
    }
    if (type === 'multipart/form-data' || type === 'application/x-www-form-urlencoded') {
      const properties = bodySchema.properties || {};
      const required = bodySchema.required || [];
      return `<fieldset class="explorer-form-fields"><legend>${esc(type)} fields</legend>${Object.entries(properties).map(([name, raw]) => {
        const field = dereference(raw);
        const isFile = field.format === 'binary';
        const isRequired = required.includes(name);
        const input = isFile
          ? `<input type="file" data-form-field="${esc(name)}" ${isRequired ? 'required' : ''}>`
          : `<input data-form-field="${esc(name)}" placeholder="${esc(field.description || 'Optional value')}" ${isRequired ? 'required' : ''}>`;
        return `<label>${esc(name)} ${isRequired ? '<b>required</b>' : ''}${input}</label>`;
      }).join('')}</fieldset>`;
    }
    if (type === 'application/octet-stream' || type.startsWith('image/')) {
      return '<label>File payload <input id="explorer-binary-file" type="file" required></label>';
    }
    return `<p class="helper">Interactive sending for ${esc(type)} is not available yet. The complete request schema remains available above.</p>`;
  }

  function renderDetail() {
    if (!selected) {
      $('explorer-detail').innerHTML = '<div class="explorer-empty">No endpoint selected.</div>';
      return;
    }
    const operation = selected.operation;
    const parameters = (operation.parameters || []).map((item) => {
      const parameter = dereference(item);
      const required = Boolean(parameter.required);
      return `<label>${esc(parameter.name)} · ${esc(parameter.in)} ${required ? '<b>required</b>' : ''}<input data-param-name="${esc(parameter.name)}" data-param-in="${esc(parameter.in)}" placeholder="${esc(parameter.description || 'Optional value')}" ${required && parameter.in !== 'path' ? 'required' : ''}></label>`;
    }).join('');
    const authNote = operation.security?.length
      ? 'Authentication is required. VAOS enforces token scopes and settings on the server.'
      : 'No endpoint-specific authentication is listed.';
    const requestSchema = requestBody(operation);
    const schemaSummary = {
      parameters: (operation.parameters || []).map((parameter) => ({
        name: parameter.name,
        in: parameter.in,
        required: Boolean(parameter.required),
        schema: dereference(parameter.schema || {}),
        description: parameter.description || '',
      })),
      requestBody: requestSchema,
      responses: operation.responses || {},
    };
    const isRead = ['get', 'head', 'options'].includes(selected.method);

    $('explorer-detail').innerHTML = `
      <div class="explorer-detail-head"><div><span class="explorer-verb verb-${esc(selected.method)}">${esc(selected.method.toUpperCase())}</span> <code>${esc(selected.path)}</code><h2>${esc(operation.summary || 'API operation')}</h2></div><span class="subtle-tag">${esc((operation.tags || []).join(' · ') || 'VAOS API')}</span></div>
      <p>${esc(operation.description || 'No additional description is published for this operation.')}</p>
      <p class="helper">${esc(authNote)} Rate limits and feature permissions are enforced by VAOS.</p>
      <details><summary>Request and response contract</summary><pre class="explorer-schema">${esc(JSON.stringify(schemaSummary, null, 2))}</pre></details>
      <div class="explorer-try"><h3>Try this request</h3>
        <div class="explorer-method-note">${isRead ? 'This request reads information.' : 'This request can change VAOS data, start work, or delete information. VAOS will ask you before sending it.'}</div>
        <form id="explorer-form" class="explorer-form">${parameters}${renderBodyEditor(operation)}
          <label>Optional API token <input id="explorer-api-token" type="password" autocomplete="off" spellcheck="false" placeholder="Not saved · leave blank to use dashboard sign-in"><small class="helper">The token stays in this page only and tests that token’s own permissions.</small></label>
          <div class="explorer-actions"><button class="button button-primary" type="submit">Send request</button><button class="button button-secondary" id="copy-curl" type="button">Copy curl example</button><button class="button button-secondary" id="copy-python" type="button">Copy Python example</button></div>
        </form>
        <div id="explorer-response" class="result-box explorer-result" aria-live="polite">Response appears here after you send a request.</div>
      </div>`;

    $('explorer-form').addEventListener('submit', sendRequest);
    $('copy-curl').addEventListener('click', () => copyExample('curl'));
    $('copy-python').addEventListener('click', () => copyExample('python'));
  }

  function renderList() {
    const query = $('explorer-search').value.trim().toLowerCase();
    const includeHealth = $('explorer-include-health').checked;
    const filtered = operations.filter((item) => (includeHealth || item.path !== '/health')
      && (!query || `${item.method} ${item.path} ${item.summary} ${item.description} ${item.tags.join(' ')}`.toLowerCase().includes(query)));
    $('explorer-count').textContent = `${filtered.length} of ${operations.length} endpoints`;
    const groups = new Map();
    filtered.forEach((item) => {
      const group = groupFor(item.path, item.tags);
      if (!groups.has(group)) groups.set(group, []);
      groups.get(group).push(item);
    });
    $('explorer-list').innerHTML = [...groups.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([group, items]) => `
      <section><p class="eyebrow">${esc(group)}</p>${items.map((item) => `
        <button class="explorer-operation" data-op="${esc(item.key)}" aria-current="${selected?.key === item.key}">
          <span class="explorer-verb verb-${esc(item.method)}">${esc(item.method.toUpperCase())}</span>
          <span class="explorer-operation-path">${esc(item.path)}<small>${esc(item.summary || 'No summary in API contract')}</small></span>
        </button>`).join('')}</section>`).join('') || '<div class="empty-state">No endpoint matches this search.</div>';
    document.querySelectorAll('[data-op]').forEach((button) => button.addEventListener('click', () => {
      selected = operations.find((item) => item.key === button.dataset.op);
      renderDetail();
      renderList();
    }));
    if (!selected && filtered.length) {
      selected = filtered[0];
      renderDetail();
      renderList();
    }
  }

  function buildRequest() {
    let path = selected.path;
    const query = new URLSearchParams();
    const headers = {};
    let body = null;
    for (const input of document.querySelectorAll('[data-param-name]')) {
      const value = input.value.trim();
      if (!value) continue;
      const name = input.dataset.paramName;
      if (input.dataset.paramIn === 'path') path = path.replace(`{${name}}`, encodeURIComponent(value));
      else if (input.dataset.paramIn === 'query') query.append(name, value);
      else if (input.dataset.paramIn === 'header') headers[name] = value;
    }
    if (/\{[^}]+\}/.test(path)) throw new Error('Fill in all required path fields first.');
    const { type } = bodyContract(selected.operation);
    if (type === 'application/json' && $('explorer-body')) {
      body = $('explorer-body').value;
      JSON.parse(body);
      headers['Content-Type'] = type;
    } else if (type === 'multipart/form-data' || type === 'application/x-www-form-urlencoded') {
      const form = new FormData();
      for (const field of document.querySelectorAll('[data-form-field]')) {
        if (field.type === 'file') {
          if (field.files[0]) form.append(field.dataset.formField, field.files[0]);
        } else if (field.value !== '') form.append(field.dataset.formField, field.value);
      }
      if (type === 'multipart/form-data') body = form;
      else {
        body = new URLSearchParams();
        for (const [key, value] of form.entries()) if (typeof value === 'string') body.append(key, value);
        headers['Content-Type'] = type;
      }
    } else if (type === 'application/octet-stream' || type.startsWith('image/')) {
      body = $('explorer-binary-file')?.files[0] || null;
      if (!body) throw new Error('Choose a file payload first.');
      headers['Content-Type'] = type;
    } else if (type) {
      throw new Error(`Interactive sending does not yet support ${type}. The request contract is still shown above.`);
    }
    const queryString = query.toString();
    return { path: `/api/v1${path}${queryString ? `?${queryString}` : ''}`, headers, body };
  }

  function pythonLiteral(value) {
    if (value === null) return 'None';
    if (value === true) return 'True';
    if (value === false) return 'False';
    if (typeof value === 'string') return JSON.stringify(value);
    if (Array.isArray(value)) return `[${value.map(pythonLiteral).join(', ')}]`;
    if (typeof value === 'object') return `{${Object.entries(value).map(([key, child]) => `${pythonLiteral(key)}: ${pythonLiteral(child)}`).join(', ')}}`;
    return String(value);
  }

  function headersForExample(headers, kind) {
    const entries = Object.entries(headers).filter(([name]) => name.toLowerCase() !== 'authorization');
    if (kind === 'curl') return entries.map(([name, value]) => ` -H '${name}: ${String(value).replace(/'/g, "'\\''")}'`).join('');
    return entries.length ? `, headers=${pythonLiteral(Object.fromEntries(entries))}` : '';
  }

  async function copyExample(kind) {
    try {
      const request = buildRequest();
      const method = selected.method.toUpperCase();
      const { type } = bodyContract(selected.operation);
      let bodySuffix = '';
      if (typeof request.body === 'string' && type === 'application/json') {
        bodySuffix = kind === 'curl'
          ? ` -H 'Content-Type: application/json' -d '${request.body.replace(/'/g, "'\\''")}'`
          : `, json=${pythonLiteral(JSON.parse(request.body))}`;
      } else if (request.body instanceof FormData) {
        const entries = [...request.body.entries()];
        if (kind === 'curl') {
          bodySuffix = entries.map(([key, value]) => ` -F '${key}=${value instanceof File ? `@/path/to/${value.name}` : String(value).replace(/'/g, "'\\''")}'`).join('');
        } else {
          const data = Object.fromEntries(entries.filter(([, value]) => typeof value === 'string'));
          const files = entries.filter(([, value]) => value instanceof File).map(([key, file]) => `${pythonLiteral(key)}: (${pythonLiteral(file.name)}, open(${pythonLiteral(`/path/to/${file.name}`)}, 'rb'))`);
          bodySuffix = `${Object.keys(data).length ? `, data=${pythonLiteral(data)}` : ''}${files.length ? `, files={${files.join(', ')}}` : ''}`;
        }
      } else if (request.body instanceof File) {
        bodySuffix = kind === 'curl' ? ` --data-binary @/path/to/${request.body.name}` : `, data=open(${pythonLiteral(`/path/to/${request.body.name}`)}, 'rb')`;
      }
      const headers = headersForExample(request.headers, kind);
      const exampleText = kind === 'curl'
        ? `curl -X ${method} '${location.origin}${request.path}'${headers}${bodySuffix}`
        : `import requests\nurl = '${location.origin}${request.path}'\nresponse = requests.request('${method}', url${headers}${bodySuffix})\nprint(response.status_code, response.text)`;
      await navigator.clipboard.writeText(exampleText);
      showMessage(`${kind === 'curl' ? 'curl' : 'Python'} example copied. Add authentication securely; VAOS does not put tokens in examples.`);
    } catch (error) {
      showMessage(error.message || 'Could not prepare this example.', true);
    }
  }

  async function sendRequest(event) {
    event.preventDefault();
    const output = $('explorer-response');
    const method = selected.method.toUpperCase();
    try {
      const request = buildRequest();
      if (!['GET', 'HEAD', 'OPTIONS'].includes(method)
        && !confirm(`${method} ${request.path}\n\nThis may change VAOS data, start work, or delete information. Continue?`)) return;
      const token = $('explorer-api-token')?.value.trim();
      if (token) request.headers.Authorization = `Bearer ${token}`;
      output.textContent = 'Sending request to this VAOS server…';
      const response = await fetch(request.path, {
        method,
        headers: request.headers,
        body: request.body,
        credentials: 'same-origin',
      });
      const raw = await response.text();
      let result = raw;
      try { result = JSON.stringify(JSON.parse(raw), null, 2); } catch { /* Plain text response. */ }
      output.innerHTML = `<strong>HTTP ${response.status} · ${esc(response.statusText)}</strong><pre>${esc(result || '(empty response)')}</pre>`;
    } catch (error) {
      output.textContent = error.message || String(error);
    }
  }

  async function init() {
    try {
      const response = await fetch('/api/openapi.json', { credentials: 'same-origin' });
      if (!response.ok) throw new Error(`API contract returned HTTP ${response.status}. Sign in again and reload.`);
      schema = await response.json();
      operations = [];
      for (const [path, pathItem] of Object.entries(schema.paths || {})) {
        for (const method of methods) {
          const operation = pathItem[method];
          if (operation) operations.push({ key: `${method} ${path}`, method, path, operation, tags: operation.tags || [], summary: operation.summary || '', description: operation.description || '' });
        }
      }
      operations.sort((a, b) => (a.tags[0] || '').localeCompare(b.tags[0] || '') || a.path.localeCompare(b.path) || a.method.localeCompare(b.method));
      $('explorer-search').addEventListener('input', () => { selected = null; renderList(); });
      $('explorer-include-health').addEventListener('change', () => { selected = null; renderList(); });
      renderList();
      showMessage(`Loaded ${operations.length} operations from the live OpenAPI contract. Only requests you choose are sent.`);
    } catch (error) {
      showMessage(error.message || 'Could not load the current API contract.', true);
      $('explorer-list').innerHTML = '<div class="empty-state">Sign in to VAOS, then use Retry.</div><button class="button button-secondary" onclick="location.reload()">Retry</button>';
    }
  }

  window.addEventListener('beforeunload', () => {
    const input = $('explorer-api-token');
    if (input) input.value = '';
  });
  init();
})();
