/* ------------------------------------------------------------------ */
/*  index.html — PED Tools main page JS                               */
/* ------------------------------------------------------------------ */

const inputEl = document.getElementById('inputEditor');
const outputEl = document.getElementById('outputEditor');

/* --- Status bar updates --- */
inputEl.addEventListener('input', () => {
    document.getElementById('inputStatus').textContent = 'Input: ' + inputEl.value.length + ' chars';
});

function setOutput(text) {
    outputEl.value = text;
    document.getElementById('outputStatus').textContent = 'Output: ' + text.length + ' chars';
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
        showToast('Formatted', 'success');
    } catch {
        showToast('Not valid JSON', 'error');
    }
}

function clearInput() { inputEl.value = ''; inputEl.dispatchEvent(new Event('input')); }
function clearOutput() { setOutput(''); }

async function pasteInput() {
    try {
        const text = await navigator.clipboard.readText();
        inputEl.value = text;
        inputEl.dispatchEvent(new Event('input'));
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
    inputEl.dispatchEvent(new Event('input'));
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
            // Try to format the decrypted JSON
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
});
