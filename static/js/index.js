/* ------------------------------------------------------------------ */
/*  index.html — PED Tools main page JS                               */
/* ------------------------------------------------------------------ */

const inputEl = document.getElementById('inputEditor');
const outputEl = document.getElementById('outputEditor');

/* --- Status bar updates --- */
function updateInputStats() {
    var val = inputEl.value;
    document.getElementById('inputStatus').textContent = 'A: ' + val.length + ' chars';
    document.getElementById('inputLines').textContent = (val ? val.split('\n').length : 0) + ' lines';
}

function updateOutputStats() {
    var val = outputEl.value;
    document.getElementById('outputStatus').textContent = 'B: ' + val.length + ' chars';
    document.getElementById('outputLines').textContent = (val ? val.split('\n').length : 0) + ' lines';
}

inputEl.addEventListener('input', updateInputStats);

var _workspaceUserEdited = false;

function setOutput(text) {
    // If user has manually edited the workspace, don't overwrite — show in result modal instead
    if (_workspaceUserEdited && outputEl.value.trim()) {
        _showResultModal(text);
        return;
    }
    _workspaceUserEdited = false;
    outputEl.value = text;
    updateOutputStats();
    // Auto-detect: if output is valid JSON object/array, show tree view;
    // otherwise show text view (encrypt/decrypt/errors/plain text)
    _outputIsJson = false;
    _treeViewData = null;
    try {
        var parsed = JSON.parse(text);
        if (parsed !== null && typeof parsed === 'object') {
            _showTreeView(parsed);
            return;
        }
    } catch (e) { /* not JSON */ }
    showTextView();
}

function _showResultModal(text) {
    var modal = document.getElementById('diffModal');
    var body = document.getElementById('diffModalBody');
    var header = modal.querySelector('.modal-header h3');
    header.textContent = 'Result';
    var html = '<pre style="background:var(--code-bg);color:var(--code-text);padding:16px;border-radius:var(--radius);font-family:var(--mono);font-size:0.82rem;white-space:pre-wrap;word-break:break-word;max-height:60vh;overflow:auto;margin:0;">';
    html += _esc(text);
    html += '</pre>';
    html += '<div style="margin-top:10px;display:flex;gap:8px;">';
    html += '<button class="btn btn-blue btn-sm" onclick="navigator.clipboard.writeText(document.getElementById(\'diffModalBody\').querySelector(\'pre\').textContent);showToast(\'Copied\',\'success\')">Copy</button>';
    html += '<button class="btn btn-outline btn-sm" onclick="_moveResultToWorkspace()">Move to Panel B</button>';
    html += '</div>';
    body.innerHTML = html;
    modal.classList.add('visible');
}

function _moveResultToWorkspace() {
    var pre = document.getElementById('diffModalBody').querySelector('pre');
    if (pre) {
        _workspaceUserEdited = false;
        outputEl.value = pre.textContent;
        updateOutputStats();
        // Auto-detect tree view
        _outputIsJson = false;
        _treeViewData = null;
        try {
            var parsed = JSON.parse(pre.textContent);
            if (parsed !== null && typeof parsed === 'object') {
                _showTreeView(parsed);
            } else { showTextView(); }
        } catch(e) { showTextView(); }
    }
    closeDiffModal();
    showToast('Moved to Panel B', 'success');
}

/* --- API helper --- */
async function post(url, body) {
    const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
    });
    return res.json();
}

/* --- Pane actions --- */
function formatInput() {
    try {
        const parsed = JSON.parse(inputEl.value);
        inputEl.value = JSON.stringify(parsed, null, 2);
        updateInputStats();
        // Auto-show tree if it's an object/array
        _inputIsJson = false;
        _inputTreeData = null;
        if (parsed !== null && typeof parsed === 'object') {
            _inputIsJson = true;
            _inputTreeData = parsed;
            _showInputTreeView(parsed);
        } else {
            showInputTextView();
        }
        showToast('Formatted', 'success');
    } catch {
        _inputIsJson = false;
        _inputTreeData = null;
        showInputTextView();
        showToast('Not valid JSON', 'error');
    }
}

function copyInput() {
    if (!inputEl.value) { showToast('Nothing to copy', 'error'); return; }
    navigator.clipboard.writeText(inputEl.value);
    showToast('Copied to clipboard', 'success');
}

function moveToOutput() {
    _workspaceUserEdited = false;
    outputEl.value = inputEl.value;
    updateOutputStats();
    showTextView();
    showToast('Moved to Panel B', 'success');
}

function clearInput() { inputEl.value = ''; updateInputStats(); showInputTextView(); }
function clearOutput() { outputEl.value = ''; _workspaceUserEdited = false; updateOutputStats(); showTextView(); }

/* --- Input pane tree view --- */
var _inputTreeData = null;
var _isInputTreeActive = false;
var _inputIsJson = false;

function _showInputTreeView(data) {
    _inputTreeData = data;
    _isInputTreeActive = true;
    _inputIsJson = true;
    var tree = document.getElementById('inputTreeView');
    _jtCounter = 0;
    tree.innerHTML = _renderNode(data, null, false, 0);
    tree.style.display = 'block';
    inputEl.style.display = 'none';
    document.getElementById('btnInputExpandAll').style.visibility = 'visible';
    document.getElementById('btnInputCollapseAll').style.visibility = 'visible';
    var toggle = document.getElementById('btnInputViewToggle');
    toggle.style.visibility = 'visible';
    toggle.textContent = 'Text';
}

function showInputTextView() {
    _isInputTreeActive = false;
    var tree = document.getElementById('inputTreeView');
    tree.style.display = 'none';
    inputEl.style.display = '';
    document.getElementById('btnInputExpandAll').style.visibility = 'hidden';
    document.getElementById('btnInputCollapseAll').style.visibility = 'hidden';
    var toggle = document.getElementById('btnInputViewToggle');
    if (_inputIsJson) {
        toggle.style.visibility = 'visible';
        toggle.textContent = 'Tree';
    } else {
        toggle.style.visibility = 'hidden';
    }
}

function toggleInputView() {
    if (_isInputTreeActive) {
        showInputTextView();
    } else if (_inputIsJson && _inputTreeData) {
        _showInputTreeView(_inputTreeData);
    }
}

function inputTreeExpandAll() {
    document.querySelectorAll('#inputTreeView .jt-children.collapsed').forEach(function(el) { el.classList.remove('collapsed'); });
    document.querySelectorAll('#inputTreeView .jt-toggle.collapsed').forEach(function(el) { el.classList.remove('collapsed'); });
    document.querySelectorAll('#inputTreeView .jt-summary').forEach(function(el) { el.style.display = 'none'; });
}

function inputTreeCollapseAll() {
    document.querySelectorAll('#inputTreeView .jt-children').forEach(function(el) { el.classList.add('collapsed'); });
    document.querySelectorAll('#inputTreeView .jt-toggle').forEach(function(el) { el.classList.add('collapsed'); });
    document.querySelectorAll('#inputTreeView .jt-summary').forEach(function(el) { el.style.display = ''; });
}

function formatOutput() {
    try {
        const parsed = JSON.parse(outputEl.value);
        outputEl.value = JSON.stringify(parsed, null, 2);
        updateOutputStats();
        showToast('Formatted', 'success');
    } catch {
        showToast('Not valid JSON', 'error');
    }
}

async function pasteOutput() {
    try {
        const text = await navigator.clipboard.readText();
        outputEl.value = text;
        updateOutputStats();
        showTextView();
        showToast('Pasted from clipboard', 'success');
    } catch {
        showToast('Clipboard access denied', 'error');
    }
}

async function pasteInput() {
    try {
        const text = await navigator.clipboard.readText();
        inputEl.value = text;
        updateInputStats();
        // Auto-detect JSON for tree view
        _inputIsJson = false;
        _inputTreeData = null;
        try {
            var parsed = JSON.parse(text);
            if (parsed !== null && typeof parsed === 'object') {
                _inputIsJson = true;
                _inputTreeData = parsed;
                _showInputTreeView(parsed);
            }
        } catch (e) { showInputTextView(); }
        showToast('Pasted from clipboard', 'success');
    } catch {
        showToast('Clipboard access denied', 'error');
    }
}

function copyOutput() {
    if (!outputEl.value) { showToast('Nothing to copy', 'error'); return; }
    navigator.clipboard.writeText(outputEl.value);
    showToast('Copied to clipboard', 'success');
}

function moveToInput() {
    inputEl.value = outputEl.value;
    updateInputStats();
    showInputTextView();
    showToast('Moved to Panel A', 'success');
}

/* --- Prettify --- */
async function prettify() {
    const data = inputEl.value.trim();
    if (!data) { showToast('Input is empty', 'error'); return; }
    try {
        const res = await post('/ped/prettify', {
            data: data,
            processEscape: document.getElementById('processEscape').checked
        });
        if (res.prettified) {
            setOutput(res.prettified);
            showToast('Prettified', 'success');
        } else {
            setOutput('Error: ' + (res.error || 'Unknown error'));
            showToast('Prettify failed', 'error');
        }
    } catch (e) {
        showToast('Request failed', 'error');
    }
}

/* --- Minify --- */
async function minifyJson() {
    const data = inputEl.value.trim();
    if (!data) { showToast('Input is empty', 'error'); return; }
    try {
        const res = await post('/ped/minify', { data: data });
        if (res.minified) {
            setOutput(res.minified);
            showToast('Minified (' + res.original_length + ' -> ' + res.minified_length + ' chars)', 'success');
        } else {
            setOutput('Error: ' + (res.error || 'Unknown error'));
            showToast('Minify failed', 'error');
        }
    } catch (e) { showToast('Request failed', 'error'); }
}

/* --- JSON smart parser (shared by jsonView and tree view) --- */
function _smartParseJson(raw) {
    // Strip wrapping quotes/backticks
    var cleaned = raw;
    if (/^['"`].*['"`]$/.test(cleaned) && cleaned.length >= 2) {
        var inner = cleaned.slice(1, -1);
        if (/^\s*[\[{]/.test(inner)) cleaned = inner;
    }

    // Try direct parse
    var candidates = [raw];
    if (cleaned !== raw) candidates.push(cleaned);
    for (var ci = 0; ci < candidates.length; ci++) {
        try { return { data: JSON.parse(candidates[ci]), corrected: false }; }
        catch (e) { /* continue */ }
    }

    var work = cleaned;
    var errors = [];

    // Attempt 1: extract embedded JSON
    var braceStart = work.indexOf('{');
    var bracketStart = work.indexOf('[');
    var starts = [];
    if (braceStart >= 0) starts.push(braceStart);
    if (bracketStart >= 0) starts.push(bracketStart);
    starts.sort(function(a,b) { return a - b; });

    for (var s = 0; s < starts.length; s++) {
        var startIdx = starts[s];
        var openChar = work[startIdx];
        var closeChar = openChar === '{' ? '}' : ']';
        var depth = 0, inStr = false, escaped = false;
        for (var i = startIdx; i < work.length; i++) {
            var ch = work[i];
            if (escaped) { escaped = false; continue; }
            if (ch === '\\') { escaped = true; continue; }
            if (ch === '"') { inStr = !inStr; continue; }
            if (inStr) continue;
            if (ch === openChar) depth++;
            if (ch === closeChar) depth--;
            if (depth === 0) {
                try {
                    return { data: JSON.parse(work.substring(startIdx, i + 1)), corrected: false };
                } catch (e2) {
                    errors.push(e2.message);
                }
                break;
            }
        }
    }

    // Attempt 2: auto-correct
    var fixed = work;
    fixed = fixed.replace(/'/g, '"');
    fixed = fixed.replace(/,\s*([\]}])/g, '$1');
    fixed = fixed.replace(/([{,])\s*(\w+)\s*:/g, '$1"$2":');
    fixed = fixed.replace(/\bTrue\b/g, 'true').replace(/\bFalse\b/g, 'false').replace(/\bNone\b/g, 'null');
    fixed = fixed.replace(/(")\s+(?=["\d{\[tfn-])/g, '$1: ');
    fixed = fixed.replace(/([{,])\s*([a-zA-Z_]\w*)\s+(?=["\d{\[tfn-])/g, '$1"$2": ');
    fixed = fixed.replace(/:\s*([a-zA-Z_]\w*)(\s*[,}\]])/g, function(m, val, after) {
        if (/^(true|false|null)$/.test(val)) return m;
        return ': "' + val + '"' + after;
    });
    try {
        return { data: JSON.parse(fixed), corrected: true };
    } catch (e3) {
        errors.push(e3.message);
    }

    return { data: null, errors: errors };
}

/* --- JSON View — renders interactive tree in output pane --- */
function jsonView() {
    const raw = inputEl.value.trim();
    if (!raw) { showToast('Input is empty', 'error'); return; }

    var result = _smartParseJson(raw);
    if (result.data !== null && result.data !== undefined) {
        // setOutput auto-detects JSON and shows tree view
        setOutput(JSON.stringify(result.data, null, 2));
        var msg = result.corrected ? 'Auto-corrected and rendered' : 'Valid JSON';
        showToast(msg, 'success');
    } else {
        setOutput('// Not valid JSON. Errors:\n// ' + (result.errors || []).join('\n// ') + '\n\n// Raw input:\n' + raw);
        showToast('Could not parse as JSON', 'error');
    }
}

/* --- Tree view rendering --- */
var _treeViewData = null;

var _isTreeViewActive = false;
var _outputIsJson = false;

function _showTreeView(data) {
    _treeViewData = data;
    _isTreeViewActive = true;
    _outputIsJson = true;
    var tree = document.getElementById('jsonTreeView');
    var editor = document.getElementById('outputEditor');
    _jtCounter = 0;
    tree.innerHTML = _renderNode(data, null, false, 0);
    tree.style.display = 'block';
    editor.style.display = 'none';
    document.getElementById('btnExpandAll').style.visibility = 'visible';
    document.getElementById('btnCollapseAll').style.visibility = 'visible';
    var toggle = document.getElementById('btnViewToggle');
    toggle.style.visibility = 'visible';
    toggle.textContent = 'Text';
}

function showTextView() {
    _isTreeViewActive = false;
    var tree = document.getElementById('jsonTreeView');
    var editor = document.getElementById('outputEditor');
    tree.style.display = 'none';
    editor.style.display = '';
    document.getElementById('btnExpandAll').style.visibility = 'hidden';
    document.getElementById('btnCollapseAll').style.visibility = 'hidden';
    var toggle = document.getElementById('btnViewToggle');
    if (_outputIsJson) {
        toggle.style.visibility = 'visible';
        toggle.textContent = 'Tree';
    } else {
        toggle.style.visibility = 'hidden';
    }
}

function toggleOutputView() {
    if (_isTreeViewActive) {
        showTextView();
    } else if (_outputIsJson && _treeViewData) {
        _showTreeView(_treeViewData);
    }
}

function _renderNode(val, key, isLast, depth) {
    var type = _jsonType(val);
    var comma = isLast ? '' : '<span class="jt-comma">,</span>';
    var keyHtml = key !== null ? '<span class="jt-key">"' + _esc(key) + '"</span><span class="jt-colon">: </span>' : '';

    if (type === 'object' || type === 'array') {
        var isObj = type === 'object';
        var entries = isObj ? Object.keys(val) : val;
        var count = isObj ? Object.keys(val).length : val.length;
        var open = isObj ? '{' : '[';
        var close = isObj ? '}' : ']';
        var autoCollapse = depth >= 3 && count > 0;
        var id = 'jt-' + (++_jtCounter);

        var html = '<div class="jt-node">';
        html += '<div class="jt-row">';
        if (count > 0) {
            html += '<span class="jt-toggle' + (autoCollapse ? ' collapsed' : '') + '" onclick="_jtToggle(\'' + id + '\',this)" onkeydown="_jtKeyNav(event,\'' + id + '\',this)" tabindex="0" role="button" aria-expanded="' + (!autoCollapse) + '" aria-label="Toggle ' + (isObj ? 'object' : 'array') + ' with ' + count + ' items">&#9660;</span>';
        } else {
            html += '<span class="jt-spacer"></span>';
        }
        html += keyHtml;
        html += '<span class="jt-bracket">' + open + '</span>';
        if (count > 0) {
            html += '<span class="jt-summary" id="' + id + '-sum"' + (autoCollapse ? '' : ' style="display:none"') + '>' + count + (isObj ? ' keys' : ' items') + '</span>';
        }
        if (count === 0) {
            html += '<span class="jt-bracket">' + close + '</span>' + comma;
        }
        html += '<span class="jt-type-tag">' + type + '</span>';
        html += '</div>';

        if (count > 0) {
            html += '<div class="jt-children' + (autoCollapse ? ' collapsed' : '') + '" id="' + id + '">';
            if (isObj) {
                var keys = Object.keys(val);
                keys.forEach(function(k, i) {
                    html += _renderNode(val[k], k, i === keys.length - 1, depth + 1);
                });
            } else {
                val.forEach(function(item, i) {
                    html += _renderNode(item, null, i === val.length - 1, depth + 1);
                });
            }
            html += '</div>';
            html += '<div class="jt-row"><span class="jt-spacer"></span><span class="jt-bracket">' + close + '</span>' + comma + '</div>';
        }
        html += '</div>';
        return html;
    }

    // Leaf value
    var valHtml = '';
    var typeTag = type;
    if (type === 'string') {
        var display = val.length > 120 ? val.substring(0, 120) + '...' : val;
        valHtml = '<span class="jt-value jt-string" onclick="_jtCopyVal(this)" title="Click to copy">"' + _esc(display) + '"</span>';
    } else if (type === 'number') {
        valHtml = '<span class="jt-value jt-number" onclick="_jtCopyVal(this)" title="Click to copy">' + val + '</span>';
    } else if (type === 'boolean') {
        valHtml = '<span class="jt-value jt-boolean" onclick="_jtCopyVal(this)" title="Click to copy">' + val + '</span>';
    } else {
        valHtml = '<span class="jt-value jt-null" onclick="_jtCopyVal(this)" title="Click to copy">null</span>';
    }

    return '<div class="jt-node"><div class="jt-row"><span class="jt-spacer"></span>' +
        keyHtml + valHtml + comma +
        '<span class="jt-type-tag">' + typeTag + '</span>' +
        '</div></div>';
}

var _jtCounter = 0;

function _jsonType(val) {
    if (val === null) return 'null';
    if (Array.isArray(val)) return 'array';
    return typeof val;
}

function _esc(s) {
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function _jtToggle(id, arrow) {
    var el = document.getElementById(id);
    var sum = document.getElementById(id + '-sum');
    if (!el) return;
    var collapsed = el.classList.toggle('collapsed');
    arrow.classList.toggle('collapsed', collapsed);
    arrow.setAttribute('aria-expanded', !collapsed);
    if (sum) sum.style.display = collapsed ? '' : 'none';
}

function _jtKeyNav(e, id, arrow) {
    if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        _jtToggle(id, arrow);
    } else if (e.key === 'ArrowRight') {
        // Expand if collapsed
        var el = document.getElementById(id);
        if (el && el.classList.contains('collapsed')) { e.preventDefault(); _jtToggle(id, arrow); }
    } else if (e.key === 'ArrowLeft') {
        // Collapse if expanded
        var el = document.getElementById(id);
        if (el && !el.classList.contains('collapsed')) { e.preventDefault(); _jtToggle(id, arrow); }
    } else if (e.key === 'ArrowDown') {
        e.preventDefault();
        var next = arrow.closest('.jt-node').querySelector('.jt-node .jt-toggle');
        if (next) next.focus();
    } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        var parent = arrow.closest('.jt-node').parentElement.closest('.jt-node');
        if (parent) { var toggle = parent.querySelector(':scope > .jt-row > .jt-toggle'); if (toggle) toggle.focus(); }
    }
}

function _jtCopyVal(el) {
    var text = el.textContent;
    // Strip surrounding quotes for strings
    if (text.startsWith('"') && text.endsWith('"')) text = text.slice(1, -1);
    navigator.clipboard.writeText(text);
    showToast('Copied: ' + (text.length > 40 ? text.substring(0, 40) + '...' : text), 'success');
}

function treeExpandAll() {
    document.querySelectorAll('#jsonTreeView .jt-children.collapsed').forEach(function(el) {
        el.classList.remove('collapsed');
    });
    document.querySelectorAll('#jsonTreeView .jt-toggle.collapsed').forEach(function(el) {
        el.classList.remove('collapsed');
    });
    document.querySelectorAll('#jsonTreeView .jt-summary').forEach(function(el) {
        el.style.display = 'none';
    });
}

function treeCollapseAll() {
    document.querySelectorAll('#jsonTreeView .jt-children').forEach(function(el) {
        el.classList.add('collapsed');
    });
    document.querySelectorAll('#jsonTreeView .jt-toggle').forEach(function(el) {
        el.classList.add('collapsed');
    });
    document.querySelectorAll('#jsonTreeView .jt-summary').forEach(function(el) {
        el.style.display = '';
    });
}

/* --- JSON Path Query --- */
async function jsonPathQuery() {
    const data = inputEl.value.trim();
    if (!data) { showToast('Input is empty', 'error'); return; }
    const path = prompt('Enter dot-separated path (e.g. user.address.city):\n\nMultiple paths: comma-separated (e.g. name,age,address.city)');
    if (!path) return;

    const paths = path.split(',').map(p => p.trim()).filter(Boolean);
    const body = { data: data };
    if (paths.length === 1) {
        body.path = paths[0];
    } else {
        body.paths = paths;
    }

    try {
        const res = await post('/ped/jsonpath', body);
        if (res.error) {
            setOutput('Error: ' + res.error);
            showToast('Query failed', 'error');
        } else {
            setOutput(JSON.stringify(res, null, 2));
            showToast('Query complete', 'success');
        }
    } catch (e) { showToast('Request failed', 'error'); }
}

/* --- JSON Diff --- */
async function jsonDiffTool() {
    const input = inputEl.value.trim();
    if (!input) { showToast('Put first JSON in Input pane', 'error'); return; }

    var second = outputEl.value.trim();
    if (!second) {
        showToast('Paste second JSON in Panel B, then click Diff again', 'error');
        outputEl.focus();
        return;
    }

    try {
        const res = await post('/ped/diff', { a: input, b: second });
        if (res.error) {
            showToast('Diff failed: ' + res.error, 'error');
            return;
        }
        // Show diff in a popup modal — both panes stay untouched
        _showDiffModal(res);
        showToast(res.identical ? 'Documents are identical' : res.change_count + ' change(s) found',
                  res.identical ? 'success' : 'success');
    } catch (e) { showToast('Request failed', 'error'); }
}

function _showDiffModal(res) {
    var modal = document.getElementById('diffModal');
    var body = document.getElementById('diffModalBody');

    var html = '';
    if (res.identical) {
        html += '<div class="diff-identical" style="padding:20px;text-align:center;font-size:1rem;">No differences — documents are identical</div>';
    } else {
        html += '<div class="diff-inline-view">';
        html += '<div class="diff-header">';
        html += '<strong>' + res.change_count + ' change(s)</strong> between Panel A and Panel B';
        html += '</div>';
        res.changes.forEach(function(c) {
            if (c.type === 'removed') {
                html += '<div class="diff-line diff-line-removed">';
                html += '<span class="diff-sign">-</span>';
                html += '<span class="diff-path-label">' + _esc(c.path) + '</span>';
                html += '<span class="diff-val">' + _esc(JSON.stringify(c.old)) + '</span>';
                html += '</div>';
            } else if (c.type === 'added') {
                html += '<div class="diff-line diff-line-added">';
                html += '<span class="diff-sign">+</span>';
                html += '<span class="diff-path-label">' + _esc(c.path) + '</span>';
                html += '<span class="diff-val">' + _esc(JSON.stringify(c.new)) + '</span>';
                html += '</div>';
            } else {
                html += '<div class="diff-line diff-line-removed">';
                html += '<span class="diff-sign">-</span>';
                html += '<span class="diff-path-label">' + _esc(c.path) + '</span>';
                html += '<span class="diff-val">' + _esc(JSON.stringify(c.old)) + '</span>';
                html += '</div>';
                html += '<div class="diff-line diff-line-added">';
                html += '<span class="diff-sign">+</span>';
                html += '<span class="diff-path-label">' + _esc(c.path) + '</span>';
                html += '<span class="diff-val">' + _esc(JSON.stringify(c.new)) + '</span>';
                html += '</div>';
            }
        });
        html += '</div>';
    }

    body.innerHTML = html;
    modal.classList.add('visible');
}

function closeDiffModal() {
    document.getElementById('diffModal').classList.remove('visible');
}

/* --- JSON Schema Validate --- */
async function jsonSchemaValidate() {
    const data = inputEl.value.trim();
    if (!data) { showToast('Put JSON data in Input pane', 'error'); return; }
    const schemaStr = prompt('Enter JSON Schema (or paste in Output pane first and click OK):');
    let schema;
    if (schemaStr) {
        try { schema = JSON.parse(schemaStr); }
        catch { showToast('Invalid schema JSON', 'error'); return; }
    } else {
        const outputVal = outputEl.value.trim();
        if (!outputVal) { showToast('Enter schema in prompt or put it in Output pane', 'error'); return; }
        try { schema = JSON.parse(outputVal); }
        catch { showToast('Output pane does not contain valid JSON schema', 'error'); return; }
    }

    try {
        const res = await post('/ped/validate-schema', { data: data, schema: schema });
        if (res.error) {
            setOutput('Error: ' + res.error);
            showToast('Validation failed', 'error');
        } else {
            let text = '=== SCHEMA VALIDATION ===\n';
            text += 'Valid: ' + res.valid + '\n';
            if (res.errors && res.errors.length > 0) {
                text += 'Errors (' + res.errors.length + '):\n';
                res.errors.forEach((e, i) => {
                    text += '  ' + (i+1) + '. [' + e.path + '] ' + e.message + '\n';
                });
            } else {
                text += 'No validation errors.\n';
            }
            setOutput(text);
            showToast(res.valid ? 'Schema valid' : res.errors.length + ' error(s)', res.valid ? 'success' : 'error');
        }
    } catch (e) { showToast('Request failed', 'error'); }
}

/* --- JSON Transform --- */
async function jsonTransform() {
    const data = inputEl.value.trim();
    if (!data) { showToast('Input is empty', 'error'); return; }
    const opsStr = prompt(
        'Enter transform operations as JSON array. Examples:\n\n' +
        '[{"op":"pick","fields":["name","age"]}]\n' +
        '[{"op":"omit","fields":["password"]}]\n' +
        '[{"op":"rename","from":"old_name","to":"name"}]\n' +
        '[{"op":"flatten"}]\n' +
        '[{"op":"sort_keys"}]\n' +
        '[{"op":"set","path":"meta.processed","value":true}]\n' +
        '[{"op":"wrap","key":"data"}]\n' +
        '[{"op":"defaults","values":{"status":"active"}}]'
    );
    if (!opsStr) return;
    let ops;
    try { ops = JSON.parse(opsStr); }
    catch { showToast('Invalid JSON for operations', 'error'); return; }

    try {
        const res = await post('/ped/transform', { data: data, operations: ops });
        if (res.error) {
            setOutput('Error: ' + res.error);
            showToast('Transform failed', 'error');
        } else {
            setOutput(JSON.stringify(res.result, null, 2));
            let msg = 'Applied ' + res.applied + ' operation(s)';
            if (res.errors && res.errors.length > 0) {
                msg += ', ' + res.errors.length + ' error(s)';
            }
            showToast(msg, res.errors && res.errors.length > 0 ? 'error' : 'success');
        }
    } catch (e) { showToast('Request failed', 'error'); }
}

/* --- Encrypt --- */
async function encryptData() {
    const data = inputEl.value.trim();
    const encIv = document.getElementById('encIv').value.trim();
    const secret = document.getElementById('secret').value.trim();

    if (!encIv || !secret) { showToast('Provide both IV and Secret', 'error'); return; }
    if (!data) { showToast('Input is empty', 'error'); return; }

    try {
        const res = await post('/ped/encrypt', { enc_iv: encIv, secret: secret, data: data });
        if (res.encrypted) {
            setOutput(res.encrypted);
            showToast('Encrypted', 'success');
        } else {
            setOutput('Error: ' + (res.error || 'Unknown error'));
            showToast('Encryption failed', 'error');
        }
    } catch (e) {
        showToast('Request failed', 'error');
    }
}

/* --- Decrypt --- */
async function decryptData() {
    const encryptedData = inputEl.value.trim();
    const encIv = document.getElementById('encIv').value.trim();
    const secret = document.getElementById('secret').value.trim();

    if (!encIv || !secret) { showToast('Provide both IV and Secret', 'error'); return; }
    if (!encryptedData) { showToast('Input is empty', 'error'); return; }

    try {
        const res = await post('/ped/decrypt', { enc_iv: encIv, secret: secret, encryptedData: encryptedData });
        if (res.decrypted) {
            try {
                const parsed = JSON.parse(res.decrypted);
                setOutput(JSON.stringify(parsed, null, 2));
            } catch {
                setOutput(res.decrypted);
            }
            showToast('Decrypted', 'success');
        } else {
            setOutput('Error: ' + (res.error || 'Unknown error'));
            showToast('Decryption failed', 'error');
        }
    } catch (e) {
        showToast('Request failed', 'error');
    }
}

/* --- Resize handle --- */
(function() {
    var handle = document.getElementById('resizeHandle');
    if (!handle) return;
    var editors = document.querySelector('.editors');
    var panes = editors.querySelectorAll('.editor-pane');
    var dragging = false;

    handle.addEventListener('mousedown', function(e) {
        e.preventDefault();
        dragging = true;
        handle.classList.add('active');
        document.body.style.cursor = 'col-resize';
        document.body.style.userSelect = 'none';
    });

    document.addEventListener('mousemove', function(e) {
        if (!dragging) return;
        var rect = editors.getBoundingClientRect();
        var pct = ((e.clientX - rect.left) / rect.width) * 100;
        pct = Math.max(20, Math.min(80, pct));
        panes[0].style.flex = 'none';
        panes[0].style.width = pct + '%';
        panes[1].style.flex = '1';
    });

    document.addEventListener('mouseup', function() {
        if (!dragging) return;
        dragging = false;
        handle.classList.remove('active');
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
    });
})();

/* --- Find & Replace --- */
var _findState = { A: { idx: -1 }, B: { idx: -1 } };

function _getEditor(panel) {
    return panel === 'A' ? inputEl : outputEl;
}

function toggleFindReplace(panel) {
    var bar = document.getElementById('findBar' + panel);
    if (bar.style.display === 'none') {
        bar.style.display = 'flex';
        document.getElementById('findInput' + panel).focus();
    } else {
        bar.style.display = 'none';
    }
}

function findNext(panel) {
    var editor = _getEditor(panel);
    var query = document.getElementById('findInput' + panel).value;
    if (!query) return;

    var text = editor.value;
    var startPos = (_findState[panel].idx >= 0) ? _findState[panel].idx + 1 : 0;
    var idx = text.indexOf(query, startPos);
    if (idx === -1 && startPos > 0) idx = text.indexOf(query, 0); // wrap around

    // Count total matches
    var count = 0;
    var pos = 0;
    while ((pos = text.indexOf(query, pos)) !== -1) { count++; pos += query.length; }
    document.getElementById('findCount' + panel).textContent = count + ' found';

    if (idx >= 0) {
        editor.focus();
        editor.setSelectionRange(idx, idx + query.length);
        _findState[panel].idx = idx;
    } else {
        _findState[panel].idx = -1;
        showToast('Not found', 'error');
    }
}

function replaceOne(panel) {
    var editor = _getEditor(panel);
    var query = document.getElementById('findInput' + panel).value;
    var replacement = document.getElementById('replaceInput' + panel).value;
    if (!query) return;

    var start = editor.selectionStart;
    var end = editor.selectionEnd;
    var selected = editor.value.substring(start, end);

    if (selected === query) {
        editor.value = editor.value.substring(0, start) + replacement + editor.value.substring(end);
        editor.setSelectionRange(start, start + replacement.length);
        _findState[panel].idx = start + replacement.length - 1;
        if (panel === 'A') updateInputStats(); else updateOutputStats();
        findNext(panel);
    } else {
        findNext(panel);
    }
}

function replaceAll(panel) {
    var editor = _getEditor(panel);
    var query = document.getElementById('findInput' + panel).value;
    var replacement = document.getElementById('replaceInput' + panel).value;
    if (!query) return;

    var count = 0;
    var result = editor.value;
    while (result.indexOf(query) !== -1) {
        result = result.replace(query, replacement);
        count++;
        if (count > 100000) break; // safety
    }
    editor.value = result;
    if (panel === 'A') updateInputStats(); else updateOutputStats();
    document.getElementById('findCount' + panel).textContent = count + ' replaced';
    showToast(count + ' replacement(s) made', 'success');
}

/* --- Sort Lines --- */
function sortLines(panel) {
    var editor = _getEditor(panel);
    var lines = editor.value.split('\n');
    lines.sort();
    editor.value = lines.join('\n');
    if (panel === 'A') updateInputStats(); else updateOutputStats();
    showToast('Lines sorted', 'success');
}

/* --- Deduplicate Lines --- */
function dedupLines(panel) {
    var editor = _getEditor(panel);
    var lines = editor.value.split('\n');
    var seen = new Set();
    var unique = [];
    var removed = 0;
    lines.forEach(function(line) {
        if (!seen.has(line)) {
            seen.add(line);
            unique.push(line);
        } else {
            removed++;
        }
    });
    editor.value = unique.join('\n');
    if (panel === 'A') updateInputStats(); else updateOutputStats();
    showToast(removed + ' duplicate line(s) removed', 'success');
}

/* --- Wrap/Unwrap in Array --- */
function wrapUnwrap(panel) {
    var editor = _getEditor(panel);
    var text = editor.value.trim();
    if (!text) return;

    // If it's already an array, unwrap (extract first element or join)
    try {
        var parsed = JSON.parse(text);
        if (Array.isArray(parsed)) {
            // Unwrap: if single element, extract it; if multiple, show comma-separated
            if (parsed.length === 1) {
                editor.value = JSON.stringify(parsed[0], null, 2);
            } else {
                editor.value = parsed.map(function(item) { return JSON.stringify(item, null, 2); }).join('\n');
            }
            if (panel === 'A') updateInputStats(); else updateOutputStats();
            showToast('Unwrapped from array', 'success');
            return;
        }
    } catch(e) { /* not parseable as array, wrap it */ }

    // Wrap: try to parse as JSON first, else wrap the raw text as a string element
    try {
        var obj = JSON.parse(text);
        editor.value = JSON.stringify([obj], null, 2);
    } catch(e) {
        // Try wrapping each line as an array element
        var lines = text.split('\n').filter(function(l) { return l.trim(); });
        var items = lines.map(function(l) {
            try { return JSON.parse(l.trim()); }
            catch(e2) { return l.trim(); }
        });
        editor.value = JSON.stringify(items, null, 2);
    }
    if (panel === 'A') updateInputStats(); else updateOutputStats();
    showToast('Wrapped in array', 'success');
}

/* --- Pane overflow menu --- */
function togglePaneMenu(panel) {
    var menu = document.getElementById('paneMenu' + panel);
    menu.classList.toggle('visible');
}
// Close pane menus on outside click
document.addEventListener('click', function(e) {
    if (!e.target.closest('.pane-more-btn') && !e.target.closest('.pane-overflow-menu')) {
        document.querySelectorAll('.pane-overflow-menu.visible').forEach(function(m) { m.classList.remove('visible'); });
    }
});

/* --- Toolbar dropdown + AES reveal --- */
function toggleToolDropdown() {
    document.getElementById('toolsDropdown').classList.toggle('visible');
}
// Close dropdown when clicking outside
document.addEventListener('click', function(e) {
    var dd = document.getElementById('toolsDropdown');
    if (dd && dd.classList.contains('visible') && !e.target.closest('.toolbar-dropdown-wrap')) {
        dd.classList.remove('visible');
    }
});

function showAesAndEncrypt() {
    document.getElementById('aesInputs').style.display = 'flex';
    var iv = document.getElementById('encIv').value.trim();
    var secret = document.getElementById('secret').value.trim();
    if (iv && secret) { encryptData(); } else { document.getElementById('encIv').focus(); }
}
function showAesAndDecrypt() {
    document.getElementById('aesInputs').style.display = 'flex';
    var iv = document.getElementById('encIv').value.trim();
    var secret = document.getElementById('secret').value.trim();
    if (iv && secret) { decryptData(); } else { document.getElementById('encIv').focus(); }
}

/* --- Suggestion Box --- */
function toggleSuggestionBox() {
    var box = document.getElementById('suggestionBox');
    if (box.classList.contains('visible')) {
        box.classList.remove('visible');
        box.style.display = 'none';
    } else {
        box.style.display = 'flex';
        box.classList.add('visible');
    }
}

async function submitSuggestion() {
    var name = document.getElementById('suggestionName').value.trim() || 'Anonymous';
    var message = document.getElementById('suggestionMessage').value.trim();
    if (!message) { showToast('Please enter a suggestion', 'error'); return; }
    try {
        var res = await post('/suggestions/', { name: name, message: message });
        showToast(res.message || 'Suggestion sent!', 'success');
        document.getElementById('suggestionMessage').value = '';
        toggleSuggestionBox();
    } catch (e) {
        showToast('Failed to send suggestion', 'error');
    }
}

/* --- Keyboard shortcuts --- */
document.addEventListener('keydown', (e) => {
    // Escape = close diff modal + find bars
    if (e.key === 'Escape') {
        closeDiffModal();
        document.getElementById('findBarA').style.display = 'none';
        document.getElementById('findBarB').style.display = 'none';
    }
    // Ctrl/Cmd + F = open find in focused panel
    if ((e.ctrlKey || e.metaKey) && e.key === 'f') {
        e.preventDefault();
        if (document.activeElement === outputEl) {
            toggleFindReplace('B');
        } else {
            toggleFindReplace('A');
        }
    }
    // Ctrl/Cmd + Enter = Prettify
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        e.preventDefault();
        prettify();
    }
    // Ctrl/Cmd + Shift + E = Encrypt
    if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key === 'E') {
        e.preventDefault();
        encryptData();
    }
    // Ctrl/Cmd + Shift + D = Decrypt
    if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key === 'D') {
        e.preventDefault();
        decryptData();
    }
    // Ctrl/Cmd + Shift + V = JSON View
    if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key === 'V') {
        e.preventDefault();
        jsonView();
    }
});

/* --- Init --- */
updateInputStats();
updateOutputStats();
outputEl.addEventListener('input', function() {
    updateOutputStats();
    _workspaceUserEdited = true;
});
