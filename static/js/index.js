/* ------------------------------------------------------------------ */
/*  index.html — PED Tools main page JS                               */
/* ------------------------------------------------------------------ */

const inputEl = document.getElementById('inputEditor');
const outputEl = document.getElementById('outputEditor');

/* --- Status bar updates --- */
function updateInputStats() {
    var val = inputEl.value;
    document.getElementById('inputStatus').textContent = 'Input: ' + val.length + ' chars';
    document.getElementById('inputLines').textContent = (val ? val.split('\n').length : 0) + ' lines';
}

function updateOutputStats() {
    var val = outputEl.value;
    document.getElementById('outputStatus').textContent = 'Output: ' + val.length + ' chars';
    document.getElementById('outputLines').textContent = (val ? val.split('\n').length : 0) + ' lines';
}

inputEl.addEventListener('input', updateInputStats);

function setOutput(text) {
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
        showToast('Formatted', 'success');
    } catch {
        showToast('Not valid JSON', 'error');
    }
}

function clearInput() { inputEl.value = ''; updateInputStats(); }
function clearOutput() { setOutput(''); showTextView(); }

async function pasteInput() {
    try {
        const text = await navigator.clipboard.readText();
        inputEl.value = text;
        updateInputStats();
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
    showToast('Moved to input', 'success');
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
    document.getElementById('btnExpandAll').style.display = '';
    document.getElementById('btnCollapseAll').style.display = '';
    var toggle = document.getElementById('btnViewToggle');
    toggle.style.display = '';
    toggle.textContent = 'Text View';
}

function showTextView() {
    _isTreeViewActive = false;
    var tree = document.getElementById('jsonTreeView');
    var editor = document.getElementById('outputEditor');
    tree.style.display = 'none';
    editor.style.display = '';
    document.getElementById('btnExpandAll').style.display = 'none';
    document.getElementById('btnCollapseAll').style.display = 'none';
    var toggle = document.getElementById('btnViewToggle');
    if (_outputIsJson) {
        // Output is JSON — keep the button visible so user can switch back to tree
        toggle.style.display = '';
        toggle.textContent = 'Object View';
    } else {
        toggle.style.display = 'none';
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
            html += '<span class="jt-toggle' + (autoCollapse ? ' collapsed' : '') + '" onclick="_jtToggle(\'' + id + '\',this)" title="Click to expand/collapse">&#9660;</span>';
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
    if (sum) sum.style.display = collapsed ? '' : 'none';
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

    // Try output pane first; if empty or showing tree view, prompt for second JSON
    var second = outputEl.value.trim();
    if (!second) {
        second = prompt('Paste the second JSON to compare against:');
        if (!second) return;
    }

    try {
        const res = await post('/ped/diff', { a: input, b: second });
        if (res.error) {
            setOutput('Error: ' + res.error);
            showToast('Diff failed', 'error');
        } else {
            let text = '=== JSON DIFF ===\n';
            text += 'Identical: ' + res.identical + '\n';
            text += 'Changes: ' + res.change_count + '\n\n';
            if (res.changes && res.changes.length > 0) {
                res.changes.forEach(c => {
                    if (c.type === 'added') {
                        text += '+ ' + c.path + ': ' + JSON.stringify(c.new) + '\n';
                    } else if (c.type === 'removed') {
                        text += '- ' + c.path + ': ' + JSON.stringify(c.old) + '\n';
                    } else {
                        text += '~ ' + c.path + ': ' + JSON.stringify(c.old) + ' -> ' + JSON.stringify(c.new) + '\n';
                    }
                });
            } else {
                text += 'No differences found.\n';
            }
            setOutput(text);
            showToast(res.identical ? 'Documents are identical' : res.change_count + ' change(s) found', 'success');
        }
    } catch (e) { showToast('Request failed', 'error'); }
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

/* --- Keyboard shortcuts --- */
document.addEventListener('keydown', (e) => {
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
