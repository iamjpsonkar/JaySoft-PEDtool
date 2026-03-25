/* ------------------------------------------------------------------ */
/*  proxy_server.html — Proxy Server & Mock Builder JS                */
/* ------------------------------------------------------------------ */

/* ------------------------------------------------------------------ */
/*  Section toggle                                                    */
/* ------------------------------------------------------------------ */

function toggleSection(bodyId, chevronId) {
    var body = document.getElementById(bodyId);
    var chevron = document.getElementById(chevronId);
    body.classList.toggle('collapsed');
    chevron.classList.toggle('collapsed');
}

/* ------------------------------------------------------------------ */
/*  View Modal                                                        */
/* ------------------------------------------------------------------ */

// State for the currently viewed mock (used by Edit from modal)
let _viewModalMock = { endpoint: '', method: '', body: '' };

function openViewModal(endpoint, method, bodyObj) {
    _viewModalMock = { endpoint, method, body: bodyObj };
    document.getElementById('viewModalTitle').textContent =
        method + ' ' + endpoint;
    document.getElementById('viewModalBody').textContent =
        JSON.stringify(bodyObj, null, 2);
    document.getElementById('viewModal').classList.add('visible');
    document.body.style.overflow = 'hidden';
}

function closeViewModal() {
    document.getElementById('viewModal').classList.remove('visible');
    document.body.style.overflow = '';
}

function copyViewModal() {
    const text = document.getElementById('viewModalBody').textContent;
    navigator.clipboard.writeText(text);
    showToast('Copied to clipboard', 'success');
}

function editFromViewModal() {
    closeViewModal();
    editMock(_viewModalMock.endpoint, _viewModalMock.method, _viewModalMock.body);
}

// Close on Escape
document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') closeViewModal();
});

/* ------------------------------------------------------------------ */
/*  Tab switching                                                     */
/* ------------------------------------------------------------------ */

function switchTab(name) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    if (name === 'visual') {
        document.querySelectorAll('.tab')[0].classList.add('active');
        document.getElementById('tab-visual').classList.add('active');
    } else {
        document.querySelectorAll('.tab')[1].classList.add('active');
        document.getElementById('tab-raw').classList.add('active');
    }
}

/* ------------------------------------------------------------------ */
/*  Visual mock builder                                               */
/* ------------------------------------------------------------------ */

const TYPE_HINTS = {
    static: 'Any static value (string, number, boolean)',
    headerget: 'Header name, e.g. Authorization',
    jsonget: 'JSON body key, e.g. user_id',
    paramget: 'Query param name, e.g. page',
    pathparamget: 'URL prefix, e.g. orders',
    upper: 'Length, e.g. 8',
    lower: 'Length, e.g. 10',
    chars: 'Length, e.g. 6',
    digit: 'Length, e.g. 5',
    alnum: 'Pattern, e.g. [3,2,4,1]',
    snippet: 'Expression, e.g. len("hello")',
};

const ALL_TYPES = ['static','headerget','jsonget','paramget','pathparamget',
                   'upper','lower','chars','digit','alnum','snippet'];

function onTypeChange(sel) {
    const row = sel.closest('.mock-row');
    const valInput = row.querySelector('.mv');
    const type = sel.value;
    valInput.placeholder = TYPE_HINTS[type] || 'value';
    valInput.value = '';
}

function buildRowHtml(key, valType, val) {
    key = key || '';
    valType = valType || 'static';
    val = val || '';
    const optHtml = ALL_TYPES.map(o =>
        '<option value="' + o + '"' + (o===valType?' selected':'') + '>' +
        (o === 'static' ? 'Static Value' : o+'()') + '</option>'
    ).join('');
    return '<div class="mock-row">' +
        '<input type="text" placeholder="key" class="mk" value="' + escapeAttr(key) + '" oninput="updatePreview()" />' +
        '<select class="mv-type" onchange="onTypeChange(this);updatePreview();">' + optHtml + '</select>' +
        '<input type="text" placeholder="' + (TYPE_HINTS[valType]||'value') + '" class="mv" value="' + escapeAttr(val) + '" oninput="updatePreview()" />' +
        '<button class="btn btn-red btn-sm" onclick="removeRow(this)" title="Remove">&times;</button>' +
        '</div>';
}

function addRow(key, valType, val) {
    document.getElementById('mockRows').insertAdjacentHTML('beforeend', buildRowHtml(key, valType, val));
    updatePreview();
}

function addNestedRow() {
    var key = prompt('Enter the key name for the nested object:', 'details');
    if (!key) return;
    var html = '<div class="mock-row" data-nested="' + escapeAttr(key) + '">' +
        '<input type="text" value="' + escapeAttr(key) + '" class="mk" style="font-weight:700;" disabled />' +
        '<span style="flex:1;font-size:0.8rem;color:var(--gray-text);">(nested object — prefix child keys with "' + escapeAttr(key) + '.")</span>' +
        '<button class="btn btn-red btn-sm" onclick="removeRow(this)" title="Remove">&times;</button>' +
        '</div>';
    document.getElementById('mockRows').insertAdjacentHTML('beforeend', html);
    addRow(key + '.child_key', 'static', '');
}

function removeRow(btn) {
    btn.closest('.mock-row').remove();
    updatePreview();
}

function buildValueString(type, val) {
    if (type === 'static') return val;
    return type + '(' + val + ')';
}

// Parse a value string like "digit(6)" back into {type, val}
function parseValueString(s) {
    if (typeof s !== 'string') return { type: 'static', val: String(s) };
    for (var i = 0; i < ALL_TYPES.length; i++) {
        var t = ALL_TYPES[i];
        if (t !== 'static' && s.startsWith(t + '(') && s.endsWith(')')) {
            return { type: t, val: s.slice(t.length + 1, -1) };
        }
    }
    return { type: 'static', val: s };
}

function getVisualMockJson() {
    var rows = document.querySelectorAll('#mockRows .mock-row');
    var root = {};
    rows.forEach(function(row) {
        var keyInput = row.querySelector('.mk');
        var typeSelect = row.querySelector('.mv-type');
        var valInput = row.querySelector('.mv');
        if (!keyInput || !typeSelect || !valInput) return;
        var key = keyInput.value.trim();
        var type = typeSelect.value;
        var val = valInput.value.trim();
        if (!key) return;

        var value = buildValueString(type, val);
        var parts = key.split('.');
        var target = root;
        for (var i = 0; i < parts.length - 1; i++) {
            if (!(parts[i] in target) || typeof target[parts[i]] !== 'object') {
                target[parts[i]] = {};
            }
            target = target[parts[i]];
        }
        target[parts[parts.length - 1]] = value;
    });
    return root;
}

function updatePreview() {
    var obj = getVisualMockJson();
    document.getElementById('visualPreview').textContent = JSON.stringify(obj, null, 2);
}

// Load a JSON object into the visual builder rows
function loadIntoVisualBuilder(obj, prefix) {
    prefix = prefix || '';
    for (var key in obj) {
        var fullKey = prefix ? prefix + '.' + key : key;
        var val = obj[key];
        if (val && typeof val === 'object' && !Array.isArray(val)) {
            loadIntoVisualBuilder(val, fullKey);
        } else {
            var parsed = parseValueString(val);
            addRow(fullKey, parsed.type, parsed.val);
        }
    }
}

// Initialize
document.querySelectorAll('#mockRows .mk, #mockRows .mv').forEach(function(el) {
    el.addEventListener('input', updatePreview);
});
updatePreview();

/* ------------------------------------------------------------------ */
/*  Sequence Mode                                                     */
/* ------------------------------------------------------------------ */

var _sequenceStepCount = 0;

function onSequenceToggle() {
    var on = document.getElementById('sequenceToggle').checked;
    document.getElementById('sequenceBuilder').style.display = on ? 'block' : 'none';
    if (on && _sequenceStepCount === 0) {
        addSequenceStep();
        addSequenceStep();
    }
}

function addSequenceStep(value) {
    _sequenceStepCount++;
    var idx = _sequenceStepCount;
    var val = value ? (typeof value === 'string' ? value : JSON.stringify(value, null, 2)) : '';
    var html = '<div class="sequence-step" id="seq-step-' + idx + '">' +
        '<span class="step-label">Step ' + idx + '</span>' +
        '<textarea class="seq-response" placeholder=\'{"status": "ok"}\'>' + escapeHtml(val) + '</textarea>' +
        '<button class="btn btn-red btn-sm" onclick="removeSequenceStep(this)" title="Remove">&times;</button>' +
        '</div>';
    document.getElementById('sequenceSteps').insertAdjacentHTML('beforeend', html);
}

function removeSequenceStep(btn) {
    btn.closest('.sequence-step').remove();
    // Re-number steps
    var steps = document.querySelectorAll('#sequenceSteps .sequence-step');
    steps.forEach(function(step, i) {
        step.querySelector('.step-label').textContent = 'Step ' + (i + 1);
    });
    _sequenceStepCount = steps.length;
}

function getSequenceSteps() {
    var steps = [];
    document.querySelectorAll('#sequenceSteps .seq-response').forEach(function(ta) {
        var val = ta.value.trim();
        if (val) {
            try { steps.push(JSON.parse(val)); }
            catch (e) { steps.push(val); }
        }
    });
    return steps;
}

/* ------------------------------------------------------------------ */
/*  Conditional Mode                                                  */
/* ------------------------------------------------------------------ */

var _conditionRowCount = 0;

function onConditionalToggle() {
    var on = document.getElementById('conditionalToggle').checked;
    document.getElementById('conditionalBuilder').style.display = on ? 'block' : 'none';
    if (on && _conditionRowCount === 0) {
        addConditionRow();
    }
}

function addConditionRow() {
    _conditionRowCount++;
    var idx = _conditionRowCount;
    var html = '<div class="condition-row" id="cond-row-' + idx + '">' +
        '<select class="cond-source" style="width:120px;">' +
            '<option value="header">Header</option>' +
            '<option value="json_body">JSON Body</option>' +
            '<option value="query_param">Query Param</option>' +
            '<option value="path">Path</option>' +
            '<option value="method">Method</option>' +
        '</select>' +
        '<input type="text" class="cond-field" placeholder="field name" style="flex:0 0 140px;" />' +
        '<select class="cond-operator" style="width:110px;">' +
            '<option value="equals">equals</option>' +
            '<option value="not_equals">not equals</option>' +
            '<option value="contains">contains</option>' +
            '<option value="starts_with">starts with</option>' +
            '<option value="ends_with">ends with</option>' +
            '<option value="regex">regex</option>' +
            '<option value="exists">exists</option>' +
        '</select>' +
        '<input type="text" class="cond-value" placeholder="value" style="flex:0 0 140px;" />' +
        '<div style="width:100%;margin-top:6px;">' +
            '<label style="font-size:0.78rem;">Then respond with (JSON):</label>' +
            '<textarea class="cond-response" placeholder=\'{"matched": true}\' style="width:100%;min-height:50px;"></textarea>' +
        '</div>' +
        '<button class="btn btn-red btn-sm" onclick="removeConditionRow(this)" title="Remove" style="align-self:flex-start;">&times;</button>' +
        '</div>';
    document.getElementById('conditionRows').insertAdjacentHTML('beforeend', html);
}

function removeConditionRow(btn) {
    btn.closest('.condition-row').remove();
    _conditionRowCount = document.querySelectorAll('#conditionRows .condition-row').length;
}

function getConditions() {
    var conditions = [];
    document.querySelectorAll('#conditionRows .condition-row').forEach(function(row) {
        var source = row.querySelector('.cond-source').value;
        var field = row.querySelector('.cond-field').value.trim();
        var operator = row.querySelector('.cond-operator').value;
        var value = row.querySelector('.cond-value').value.trim();
        var responseText = row.querySelector('.cond-response').value.trim();
        var response;
        try { response = JSON.parse(responseText); }
        catch (e) { response = responseText || {}; }
        conditions.push({
            source: source,
            field: field,
            operator: operator,
            value: value,
            response: response
        });
    });
    var defaultText = document.getElementById('conditionDefault').value.trim();
    var defaultResp;
    try { defaultResp = JSON.parse(defaultText); }
    catch (e) { defaultResp = defaultText || {}; }
    return { conditions: conditions, default: defaultResp };
}

/* ------------------------------------------------------------------ */
/*  Raw JSON placeholder insert                                       */
/* ------------------------------------------------------------------ */

function insertPlaceholder(text) {
    var ta = document.getElementById('mock_json_raw');
    var start = ta.selectionStart;
    var end = ta.selectionEnd;
    var before = ta.value.substring(0, start);
    var after = ta.value.substring(end);
    ta.value = before + text + after;
    ta.selectionStart = ta.selectionEnd = start + text.length;
    ta.focus();
}

/* ------------------------------------------------------------------ */
/*  API calls — Register, Lookup                                      */
/* ------------------------------------------------------------------ */

async function registerProxy() {
    var domain = document.getElementById('api_domain').value.trim();
    var id = document.getElementById('reg_identifier').value.trim();
    if (!domain) { showToast('API Domain is required', 'error'); return; }

    try {
        var data = await api('/proxy/create/', 'POST', {
            api_domain: domain,
            identifier: id || undefined
        });
        showToast('Proxy "' + data.identifier + '" registered!', 'success');
        showResponse('mainResponse', data);
        document.getElementById('mock_proxy_id').value = data.identifier;
        document.getElementById('mocks_proxy_id').value = data.identifier;
        document.getElementById('lookup_id').value = data.identifier;
    } catch (e) {
        showToast((e.data && e.data.error) || 'Registration failed', 'error');
        showResponse('mainResponse', e.data || e);
    }
}

async function lookupProxy() {
    var id = document.getElementById('lookup_id').value.trim();
    if (!id) { showToast('Identifier is required', 'error'); return; }

    try {
        var data = await api('/proxy/get/' + encodeURIComponent(id) + '/', 'GET');
        showResponse('lookupResponse', data);

    } catch (e) {
        showToast('Lookup failed', 'error');
        showResponse('lookupResponse', e.data || e);
    }
}

/* ------------------------------------------------------------------ */
/*  API calls — Mock create / test                                    */
/* ------------------------------------------------------------------ */

function getMockPayload() {
    var proxyId = document.getElementById('mock_proxy_id').value.trim();
    var method = document.getElementById('mock_method').value;
    var endpoint = document.getElementById('mock_endpoint').value.trim();

    if (!proxyId || !endpoint) {
        showToast('Proxy Identifier and Endpoint are required', 'error');
        return null;
    }

    // Check if sequence mode is active
    if (document.getElementById('sequenceToggle').checked) {
        var steps = getSequenceSteps();
        if (steps.length < 2) {
            showToast('Sequence mode needs at least 2 steps', 'error');
            return null;
        }
        var mock = { _sequence: steps };
        mock = applyMockEnhancements(mock);
        return { proxy_identifier: proxyId, end_point: endpoint, method: method, mock: mock };
    }

    // Check if conditional mode is active
    if (document.getElementById('conditionalToggle').checked) {
        var condData = getConditions();
        if (condData.conditions.length === 0) {
            showToast('Conditional mode needs at least 1 condition', 'error');
            return null;
        }
        var mock = { _conditions: condData.conditions, _default: condData.default };
        mock = applyMockEnhancements(mock);
        return { proxy_identifier: proxyId, end_point: endpoint, method: method, mock: mock };
    }

    var isVisual = document.getElementById('tab-visual').classList.contains('active');
    var mock;
    if (isVisual) {
        mock = getVisualMockJson();
    } else {
        var raw = document.getElementById('mock_json_raw').value.trim();
        if (!raw) { showToast('Mock JSON is required', 'error'); return null; }
        try { mock = JSON.parse(raw); }
        catch(err) { showToast('Invalid JSON in mock response', 'error'); return null; }
    }

    mock = applyMockEnhancements(mock);

    return {
        proxy_identifier: proxyId,
        end_point: endpoint,
        method: method,
        mock: mock
    };
}

function applyMockEnhancements(mock) {
    var delay = parseInt(document.getElementById('mock_delay').value, 10);
    var statusCode = parseInt(document.getElementById('mock_status_code').value, 10);
    var headersText = document.getElementById('mock_headers').value.trim();

    if (delay && delay > 0) {
        mock._delay_ms = delay;
    }

    if (headersText) {
        try {
            mock.headers = JSON.parse(headersText);
        } catch (e) {
            // silently ignore invalid JSON
        }
    }

    if (statusCode && statusCode !== 200) {
        mock = { status_code: statusCode, body: mock };
    }

    return mock;
}

async function createMock() {
    var payload = getMockPayload();
    if (!payload) return;

    try {
        var data = await api('/proxy/mock/create/', 'POST', payload);
        showToast('Mock saved for ' + payload.method + ' ' + payload.end_point, 'success');
        showResponse('mainResponse', data);
        if (document.getElementById('mocks_proxy_id').value === payload.proxy_identifier) {
            loadMocks();
        }
    } catch (e) {
        showToast((e.data && e.data.error) || 'Mock creation failed', 'error');
        showResponse('mainResponse', e.data || e);
    }
}

// Replace <param> placeholders with real values via prompt
function resolveEndpointParams(endpoint) {
    var params = endpoint.match(/<(\w+)>/g);
    if (!params) return endpoint;
    var resolved = endpoint;
    for (var i = 0; i < params.length; i++) {
        var paramName = params[i].slice(1, -1); // strip < >
        var value = prompt('Enter value for <' + paramName + '>:', 'test_' + paramName);
        if (value === null) return null; // user cancelled
        resolved = resolved.replace(params[i], encodeURIComponent(value));
    }
    return resolved;
}

async function testMock() {
    var proxyId = document.getElementById('mock_proxy_id').value.trim();
    var method = document.getElementById('mock_method').value;
    var endpoint = document.getElementById('mock_endpoint').value.trim();
    if (!proxyId || !endpoint) {
        showToast('Fill in Proxy Identifier and Endpoint first', 'error');
        return;
    }

    // Prompt for dynamic param values if endpoint has <param> placeholders
    var resolvedEndpoint = resolveEndpointParams(endpoint);
    if (resolvedEndpoint === null) {
        showToast('Test cancelled', 'error');
        return;
    }

    var cleanEndpoint = resolvedEndpoint.startsWith('/') ? resolvedEndpoint.slice(1) : resolvedEndpoint;
    var url = '/proxy/' + encodeURIComponent(proxyId) + '/' + cleanEndpoint;

    try {
        var data = await api(url, method, method !== 'GET' ? { test: true } : undefined);
        showToast('Test request succeeded', 'success');
        showResponse('mainResponse', data);
    } catch (e) {
        showToast('Test request returned ' + (e.status || 'error'), 'error');
        showResponse('mainResponse', e.data || e);
    }
}

/* ------------------------------------------------------------------ */
/*  Mocks table — Load, Render, View, Edit, Delete                    */
/* ------------------------------------------------------------------ */

async function loadMocks() {
    var id = document.getElementById('mocks_proxy_id').value.trim();
    if (!id) { showToast('Identifier is required', 'error'); return; }

    try {
        var data = await api('/proxy/get/' + encodeURIComponent(id) + '/', 'GET');
        renderMocksTable(data.mocked_requests || {});

    } catch (e) {
        showToast('Failed to load mocks', 'error');
    }
}

// Store loaded mocks so modal/edit/delete can reference them by index
var _loadedMocks = [];

function renderMocksTable(mocks) {
    var container = document.getElementById('mocksContainer');
    _loadedMocks = [];
    for (var endpoint in mocks) {
        var methods = mocks[endpoint];
        for (var method in methods) {
            _loadedMocks.push({ endpoint: endpoint, method: method, body: methods[method] });
        }
    }
    if (_loadedMocks.length === 0) {
        container.innerHTML = '<div class="empty-state"><p>No mocks configured for this proxy.</p></div>';
        return;
    }
    var html = '<table class="mock-table">' +
        '<thead><tr><th>Method</th><th>Endpoint</th><th>Response Preview</th><th>Actions</th></tr></thead><tbody>';
    _loadedMocks.forEach(function(e, idx) {
        var preview = JSON.stringify(e.body);
        if (preview.length > 100) preview = preview.substring(0, 100) + '...';
        html += '<tr id="mock-row-' + idx + '">' +
            '<td><span class="method-badge method-' + e.method + '">' + e.method + '</span></td>' +
            '<td style="font-family:var(--mono);font-size:0.82rem;">' + escapeHtml(e.endpoint) + '</td>' +
            '<td class="mock-preview">' + escapeHtml(preview) + '</td>' +
            '<td style="white-space:nowrap;">' +
                '<button class="btn btn-outline btn-sm" onclick="viewMockByIdx(' + idx + ')" title="View full response">View</button> ' +
                '<button class="btn btn-outline btn-sm" onclick="editMockByIdx(' + idx + ')" title="Load into editor">Edit</button> ' +
                '<button class="btn btn-outline btn-sm" id="del-btn-' + idx + '" onclick="confirmDelete(' + idx + ')" title="Delete this mock" style="color:var(--red);border-color:var(--red);">Delete</button>' +
            '</td>' +
        '</tr>';
    });
    html += '</tbody></table>';
    container.innerHTML = html;
}

// --- View ---
function viewMockByIdx(idx) {
    var m = _loadedMocks[idx];
    if (!m) return;
    openViewModal(m.endpoint, m.method, m.body);
}

// --- Edit ---
function editMockByIdx(idx) {
    var m = _loadedMocks[idx];
    if (!m) return;
    editMock(m.endpoint, m.method, m.body);
}

function editMock(endpoint, method, bodyObj) {
    // Fill the form fields — including proxy identifier from the mocks panel
    var sourceId = document.getElementById('mocks_proxy_id').value.trim();
    if (sourceId) document.getElementById('mock_proxy_id').value = sourceId;
    document.getElementById('mock_endpoint').value = endpoint;
    document.getElementById('mock_method').value = method;

    // Reset enhancement fields
    document.getElementById('mock_delay').value = '';
    document.getElementById('mock_status_code').value = '';
    document.getElementById('mock_headers').value = '';
    document.getElementById('sequenceToggle').checked = false;
    onSequenceToggle();
    document.getElementById('conditionalToggle').checked = false;
    onConditionalToggle();

    // Detect enhanced mock structures and populate accordingly
    var mockBody = bodyObj;
    if (bodyObj && typeof bodyObj === 'object') {
        // Detect status_code wrapper
        if (bodyObj.status_code && bodyObj.body) {
            document.getElementById('mock_status_code').value = bodyObj.status_code;
            mockBody = bodyObj.body;
        }
        // Detect delay
        if (mockBody._delay_ms) {
            document.getElementById('mock_delay').value = mockBody._delay_ms;
            var cleaned = Object.assign({}, mockBody);
            delete cleaned._delay_ms;
            mockBody = cleaned;
        }
        // Detect headers
        if (mockBody.headers && typeof mockBody.headers === 'object') {
            document.getElementById('mock_headers').value = JSON.stringify(mockBody.headers, null, 2);
        }
        // Detect sequence
        if (mockBody._sequence && Array.isArray(mockBody._sequence)) {
            document.getElementById('sequenceToggle').checked = true;
            onSequenceToggle();
            document.getElementById('sequenceSteps').innerHTML = '';
            _sequenceStepCount = 0;
            mockBody._sequence.forEach(function(step) {
                addSequenceStep(step);
            });
            // Load into raw JSON tab
            switchTab('raw');
            document.getElementById('mock_json_raw').value = JSON.stringify(bodyObj, null, 2);
            window.scrollTo({ top: document.getElementById('mock_proxy_id').offsetTop - 80, behavior: 'smooth' });
            showToast('Sequence mock loaded into editor', 'success');
            return;
        }
        // Detect conditional
        if (mockBody._conditions && Array.isArray(mockBody._conditions)) {
            document.getElementById('conditionalToggle').checked = true;
            onConditionalToggle();
            document.getElementById('conditionRows').innerHTML = '';
            _conditionRowCount = 0;
            mockBody._conditions.forEach(function(cond) {
                addConditionRow();
                var lastRow = document.querySelector('#conditionRows .condition-row:last-child');
                if (lastRow) {
                    lastRow.querySelector('.cond-source').value = cond.source || 'header';
                    lastRow.querySelector('.cond-field').value = cond.field || '';
                    lastRow.querySelector('.cond-operator').value = cond.operator || 'equals';
                    lastRow.querySelector('.cond-value').value = cond.value || '';
                    lastRow.querySelector('.cond-response').value = typeof cond.response === 'object' ? JSON.stringify(cond.response, null, 2) : (cond.response || '');
                }
            });
            if (mockBody._default) {
                document.getElementById('conditionDefault').value = typeof mockBody._default === 'object' ? JSON.stringify(mockBody._default, null, 2) : mockBody._default;
            }
            switchTab('raw');
            document.getElementById('mock_json_raw').value = JSON.stringify(bodyObj, null, 2);
            window.scrollTo({ top: document.getElementById('mock_proxy_id').offsetTop - 80, behavior: 'smooth' });
            showToast('Conditional mock loaded into editor', 'success');
            return;
        }
    }

    // Load into raw JSON tab
    switchTab('raw');
    document.getElementById('mock_json_raw').value = JSON.stringify(mockBody, null, 2);

    // Also load into visual builder (clear existing rows first)
    var rowsContainer = document.getElementById('mockRows');
    rowsContainer.innerHTML = '';
    if (mockBody && typeof mockBody === 'object' && !Array.isArray(mockBody)) {
        loadIntoVisualBuilder(mockBody);
    }
    updatePreview();

    // Scroll to form
    window.scrollTo({ top: document.getElementById('mock_proxy_id').offsetTop - 80, behavior: 'smooth' });
    showToast('Mock loaded into editor — modify and click "Save Mock" to save', 'success');
}

// --- Delete with confirmation ---
function confirmDelete(idx) {
    var btn = document.getElementById('del-btn-' + idx);
    if (!btn) return;
    var m = _loadedMocks[idx];

    // Replace button with confirm/cancel
    var td = btn.parentElement;
    var original = td.innerHTML;
    td.innerHTML =
        '<div class="confirm-delete">' +
            '<span>Delete?</span>' +
            '<button class="btn btn-red btn-sm" onclick="doDelete(' + idx + ')">Yes</button> ' +
            '<button class="btn btn-outline btn-sm" onclick="cancelDelete(' + idx + ',this)">No</button>' +
        '</div>';

    // Store original HTML for cancel
    td.dataset.originalHtml = original;
}

function cancelDelete(idx, btn) {
    var td = btn.closest('td');
    if (td && td.dataset.originalHtml) {
        td.innerHTML = td.dataset.originalHtml;
    }
}

async function doDelete(idx) {
    var m = _loadedMocks[idx];
    if (!m) return;
    var proxyId = document.getElementById('mocks_proxy_id').value.trim();

    try {
        await api('/proxy/mock/delete/', 'POST', {
            proxy_identifier: proxyId,
            end_point: m.endpoint,
            method: m.method
        });
        showToast(m.method + ' ' + m.endpoint + ' deleted', 'success');
        // Remove row with animation
        var row = document.getElementById('mock-row-' + idx);
        if (row) {
            row.style.transition = 'opacity 0.3s';
            row.style.opacity = '0';
            setTimeout(function() { row.remove(); }, 300);
        }
        // Remove from local array
        _loadedMocks[idx] = null;

        // If all deleted, show empty state
        if (_loadedMocks.every(function(m) { return m === null; })) {
            setTimeout(function() {
                document.getElementById('mocksContainer').innerHTML =
                    '<div class="empty-state"><p>No mocks configured for this proxy.</p></div>';
            }, 350);
        }
    } catch (e) {
        showToast((e.data && e.data.error) || 'Delete failed', 'error');
        // Restore the row buttons
        cancelDelete(idx, document.querySelector('#mock-row-' + idx + ' td:last-child button'));
    }
}
