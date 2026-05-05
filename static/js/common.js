/* ------------------------------------------------------------------ */
/*  Common utilities shared across all pages                          */
/* ------------------------------------------------------------------ */

function showToast(msg, type) {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.className = 'toast toast-' + type + ' visible';
    setTimeout(() => t.classList.remove('visible'), 3000);
}

async function api(url, method, body) {
    const opts = { method, credentials: 'same-origin', headers: { 'Content-Type': 'application/json' } };
    if (body) opts.body = JSON.stringify(body);
    const res = await fetch(url, opts);
    let data;
    try { data = await res.json(); } catch (_) { data = { raw: await res.clone().text().catch(() => 'No response body') }; }
    if (!res.ok) throw { status: res.status, data };
    return data;
}

function showResponse(elId, data) {
    const el = document.getElementById(elId);
    el.innerHTML = '<button class="copy-btn" onclick="copyResponse(this)">Copy</button>' +
                    escapeHtml(JSON.stringify(data, null, 2));
    el.classList.add('visible');
    el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function escapeHtml(s) {
    const d = document.createElement('div');
    d.appendChild(document.createTextNode(s));
    return d.innerHTML;
}

function escapeAttr(s) {
    return s.replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;');
}

function copyResponse(btn) {
    const text = btn.parentElement.textContent.replace('Copy', '').trim();
    navigator.clipboard.writeText(text);
    btn.textContent = 'Copied!';
    setTimeout(() => btn.textContent = 'Copy', 1500);
}

/* --- Button loading states --- */
function setButtonLoading(btn, text) {
    if (!btn) return;
    btn.dataset.originalText = btn.textContent;
    btn.textContent = text || 'Loading...';
    btn.disabled = true;
    btn.style.opacity = '0.7';
}
function resetButton(btn) {
    if (!btn) return;
    btn.textContent = btn.dataset.originalText || btn.textContent;
    btn.disabled = false;
    btn.style.opacity = '';
}

/* --- Modal focus trap --- */
var _previousFocus = null;
function trapFocus(modalEl) {
    _previousFocus = document.activeElement;
    var focusable = modalEl.querySelectorAll('button, input, textarea, select, [tabindex]:not([tabindex="-1"])');
    if (focusable.length > 0) focusable[0].focus();
    modalEl._focusTrap = function(e) {
        if (e.key !== 'Tab') return;
        var first = focusable[0];
        var last = focusable[focusable.length - 1];
        if (e.shiftKey) {
            if (document.activeElement === first) { e.preventDefault(); last.focus(); }
        } else {
            if (document.activeElement === last) { e.preventDefault(); first.focus(); }
        }
    };
    modalEl.addEventListener('keydown', modalEl._focusTrap);
}
function releaseFocus(modalEl) {
    if (modalEl._focusTrap) modalEl.removeEventListener('keydown', modalEl._focusTrap);
    if (_previousFocus) _previousFocus.focus();
}

/* --- Mobile menu --- */
function toggleMobileMenu() {
    var menu = document.getElementById('mobileMenu');
    var overlay = document.getElementById('mobileOverlay');
    if (menu) menu.classList.toggle('visible');
    if (overlay) overlay.classList.toggle('visible');
}

function handleApiError(err) {
    if (err && err.status === 401) {
        showToast('Session expired. Please log in again.', 'error');
        setTimeout(() => { window.location.href = '/login'; }, 2000);
        return;
    }
    const msg = (err && err.data && (err.data.error || err.data.message)) || 'An error occurred';
    showToast(msg, 'error');
    if (err && err.data) showResponse('mainResponse', err.data);
}
