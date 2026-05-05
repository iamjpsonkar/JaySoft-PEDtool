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
function clearOutput() { setOutput(''); }

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

/* --- JSON Viewer (smart format — handles non-JSON too) --- */
function jsonView() {
    const raw = inputEl.value.trim();
    if (!raw) { showToast('Input is empty', 'error'); return; }

    // Try direct parse first
    try {
        var parsed = JSON.parse(raw);
        setOutput(JSON.stringify(parsed, null, 2));
        showToast('Valid JSON — formatted', 'success');
        return;
    } catch (e) { /* not valid JSON, try recovery */ }

    // Try extracting JSON from surrounding text
    var result = '';
    var found = 0;
    var errors = [];

    // Attempt 1: find embedded JSON objects/arrays
    var braceStart = raw.indexOf('{');
    var bracketStart = raw.indexOf('[');
    var starts = [];
    if (braceStart >= 0) starts.push(braceStart);
    if (bracketStart >= 0) starts.push(bracketStart);
    starts.sort(function(a,b) { return a - b; });

    for (var s = 0; s < starts.length; s++) {
        var startIdx = starts[s];
        var openChar = raw[startIdx];
        var closeChar = openChar === '{' ? '}' : ']';
        var depth = 0;
        var inStr = false;
        var escaped = false;
        for (var i = startIdx; i < raw.length; i++) {
            var ch = raw[i];
            if (escaped) { escaped = false; continue; }
            if (ch === '\\') { escaped = true; continue; }
            if (ch === '"') { inStr = !inStr; continue; }
            if (inStr) continue;
            if (ch === openChar) depth++;
            if (ch === closeChar) depth--;
            if (depth === 0) {
                var candidate = raw.substring(startIdx, i + 1);
                try {
                    var obj = JSON.parse(candidate);
                    if (result) result += '\n\n';
                    result += JSON.stringify(obj, null, 2);
                    found++;
                } catch (e2) {
                    errors.push('Failed to parse at position ' + startIdx + ': ' + e2.message);
                }
                break;
            }
        }
    }

    // Attempt 2: try common fixes (single quotes, trailing commas, unquoted keys)
    if (found === 0) {
        var fixed = raw
            .replace(/'/g, '"')                           // single quotes
            .replace(/,\s*([\]}])/g, '$1')                // trailing commas
            .replace(/(\{|,)\s*(\w+)\s*:/g, '$1"$2":');  // unquoted keys
        try {
            var obj2 = JSON.parse(fixed);
            result = JSON.stringify(obj2, null, 2);
            found = 1;
            errors.push('Note: Input was not valid JSON but was auto-corrected');
        } catch (e3) {
            errors.push('Auto-correction also failed: ' + e3.message);
        }
    }

    if (found > 0) {
        var header = '';
        if (errors.length > 0) {
            header = '// ' + errors.join('\n// ') + '\n\n';
        }
        setOutput(header + result);
        showToast(found + ' JSON block(s) found and formatted', 'success');
    } else {
        setOutput('// Not valid JSON. Errors:\n// ' + errors.join('\n// ') + '\n\n// Raw input:\n' + raw);
        showToast('Could not parse as JSON', 'error');
    }
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
    const output = outputEl.value.trim();
    if (!input) { showToast('Put first JSON in Input pane', 'error'); return; }
    if (!output) { showToast('Put second JSON in Output pane to compare', 'error'); return; }

    try {
        const res = await post('/ped/diff', { a: input, b: output });
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
