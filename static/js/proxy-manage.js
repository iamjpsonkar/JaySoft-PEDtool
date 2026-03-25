/* ------------------------------------------------------------------ */
/*  proxy_manage.html — Proxy Management Dashboard JS                 */
/* ------------------------------------------------------------------ */

function toggleSection(bodyId, chevronId) {
    var body = document.getElementById(bodyId);
    var chevron = document.getElementById(chevronId);
    body.classList.toggle('collapsed');
    chevron.classList.toggle('collapsed');
}

function formatJson(obj) {
    try {
        if (typeof obj === 'string') obj = JSON.parse(obj);
        return JSON.stringify(obj, null, 2);
    } catch (_) {
        return String(obj);
    }
}

/* ------------------------------------------------------------------ */
/*  Health Check                                                      */
/* ------------------------------------------------------------------ */

async function checkHealth() {
    var dot = document.getElementById('healthDot');
    var text = document.getElementById('healthText');
    var rateInfo = document.getElementById('rateLimitInfo');
    try {
        var data = await api('/health', 'GET');
        dot.className = 'status-dot ok';
        text.textContent = data.status || 'Healthy';
        if (data.rate_limit) {
            rateInfo.textContent = 'Rate Limit: ' + (data.rate_limit.remaining || '?') + '/' + (data.rate_limit.limit || '?') + ' remaining';
        } else if (data.proxies_count !== undefined) {
            rateInfo.textContent = 'Proxies: ' + data.proxies_count;
        }
    } catch (err) {
        dot.className = 'status-dot err';
        text.textContent = 'Unhealthy';
        if (err && err.status === 401) handleApiError(err);
    }
}

/* ------------------------------------------------------------------ */
/*  Proxy List                                                        */
/* ------------------------------------------------------------------ */

async function loadProxyList() {
    var container = document.getElementById('proxyListContainer');
    container.innerHTML = '<div class="empty-state"><p>Loading...</p></div>';
    try {
        var data = await api('/proxy/list/', 'GET');
        var proxies = data.proxies || data || [];
        if (!Array.isArray(proxies)) proxies = [];
        if (proxies.length === 0) {
            container.innerHTML = '<div class="empty-state"><p>No proxies registered yet.</p></div>';
            return;
        }
        var html = '<table class="mock-table"><thead><tr>' +
            '<th>Identifier</th><th>API Domain</th><th>Mock Count</th><th>Created At</th><th>Actions</th>' +
            '</tr></thead><tbody>';
        proxies.forEach(function(p) {
            var id = escapeAttr(p.identifier || p.id || '');
            var domain = escapeHtml(p.api_domain || p.domain || '-');
            var mockCount = p.mock_count !== undefined ? p.mock_count : (p.mocks_count !== undefined ? p.mocks_count : '-');
            var created = p.created_at ? new Date(p.created_at).toLocaleString() : '-';
            html += '<tr>' +
                '<td><strong>' + escapeHtml(id) + '</strong></td>' +
                '<td class="mock-preview">' + domain + '</td>' +
                '<td>' + mockCount + '</td>' +
                '<td>' + escapeHtml(created) + '</td>' +
                '<td style="white-space:nowrap;">' +
                    '<button class="btn btn-blue btn-sm" onclick="viewMocks(\'' + id + '\')">View Mocks</button> ' +
                    '<button class="btn btn-outline btn-sm" onclick="fillClone(\'' + id + '\')">Clone</button> ' +
                    '<button class="btn btn-green btn-sm" onclick="exportProxy(\'' + id + '\')">Export</button> ' +
                    '<span id="del-' + id + '">' +
                        '<button class="btn btn-red btn-sm" onclick="confirmDelete(\'' + id + '\')">Delete</button>' +
                    '</span>' +
                '</td>' +
                '</tr>';
        });
        html += '</tbody></table>';
        container.innerHTML = html;
    } catch (err) {
        handleApiError(err);
        container.innerHTML = '<div class="empty-state"><p>Failed to load proxy list.</p></div>';
    }
}

/* ------------------------------------------------------------------ */
/*  Delete Proxy                                                      */
/* ------------------------------------------------------------------ */

function confirmDelete(id) {
    var el = document.getElementById('del-' + id);
    el.innerHTML = '<span class="confirm-delete">' +
        '<span>Are you sure?</span>' +
        '<button class="btn btn-red btn-sm" onclick="deleteProxy(\'' + escapeAttr(id) + '\')">Yes, Delete</button>' +
        '<button class="btn btn-outline btn-sm" onclick="cancelDelete(\'' + escapeAttr(id) + '\')">Cancel</button>' +
        '</span>';
}

function cancelDelete(id) {
    var el = document.getElementById('del-' + id);
    el.innerHTML = '<button class="btn btn-red btn-sm" onclick="confirmDelete(\'' + escapeAttr(id) + '\')">Delete</button>';
}

async function deleteProxy(id) {
    try {
        await api('/proxy/delete/' + encodeURIComponent(id) + '/', 'DELETE');
        showToast('Proxy "' + id + '" deleted successfully.', 'success');
        loadProxyList();
    } catch (err) {
        handleApiError(err);
    }
}

/* ------------------------------------------------------------------ */
/*  View Mocks Modal                                                  */
/* ------------------------------------------------------------------ */

async function viewMocks(id) {
    document.getElementById('mocksModalTitle').textContent = 'Mocks for: ' + id;
    document.getElementById('mocksModalBody').textContent = 'Loading...';
    document.getElementById('mocksModal').classList.add('visible');
    try {
        var data = await api('/proxy/mocks/' + encodeURIComponent(id) + '/', 'GET');
        document.getElementById('mocksModalBody').textContent = formatJson(data);
    } catch (err) {
        if (err && err.status === 401) { closeMocksModal(); handleApiError(err); return; }
        document.getElementById('mocksModalBody').textContent = formatJson(err.data || { error: 'Failed to load mocks' });
    }
}

function closeMocksModal() {
    document.getElementById('mocksModal').classList.remove('visible');
}

function copyMocksModal() {
    var text = document.getElementById('mocksModalBody').textContent;
    navigator.clipboard.writeText(text);
    showToast('Copied to clipboard.', 'success');
}

/* ------------------------------------------------------------------ */
/*  Clone Proxy                                                       */
/* ------------------------------------------------------------------ */

function toggleCloneSection() {
    document.getElementById('cloneSection').classList.toggle('visible');
}

function fillClone(id) {
    document.getElementById('cloneSource').value = id;
    document.getElementById('cloneTarget').value = '';
    document.getElementById('cloneSection').classList.add('visible');
    document.getElementById('cloneTarget').focus();
}

async function cloneProxy() {
    var source = document.getElementById('cloneSource').value.trim();
    var target = document.getElementById('cloneTarget').value.trim();
    if (!source || !target) {
        showToast('Both source and target identifiers are required.', 'error');
        return;
    }
    try {
        var data = await api('/proxy/clone/', 'POST', { source: source, target: target });
        showToast('Proxy cloned successfully.', 'success');
        showResponse('mainResponse', data);
        toggleCloneSection();
        loadProxyList();
    } catch (err) {
        handleApiError(err);
    }
}

/* ------------------------------------------------------------------ */
/*  Import / Export                                                    */
/* ------------------------------------------------------------------ */

function toggleImportSection() {
    document.getElementById('importSection').classList.toggle('visible');
}

async function importProxies() {
    var raw = document.getElementById('importData').value.trim();
    if (!raw) {
        showToast('Please paste import data.', 'error');
        return;
    }
    var parsed;
    try {
        parsed = JSON.parse(raw);
    } catch (_) {
        showToast('Invalid JSON in import data.', 'error');
        return;
    }
    try {
        var data = await api('/proxy/import/', 'POST', parsed);
        showToast('Import successful.', 'success');
        showResponse('mainResponse', data);
        document.getElementById('importData').value = '';
        toggleImportSection();
        loadProxyList();
    } catch (err) {
        handleApiError(err);
    }
}

async function exportAll() {
    try {
        var data = await api('/proxy/export/', 'GET');
        showResponse('exportResponse', data);
        showToast('Export data loaded.', 'success');
    } catch (err) {
        handleApiError(err);
    }
}

async function exportProxy(id) {
    try {
        var data = await api('/proxy/export/' + encodeURIComponent(id) + '/', 'GET');
        showResponse('exportResponse', data);
        showToast('Export data for "' + id + '" loaded.', 'success');
    } catch (err) {
        handleApiError(err);
    }
}

/* ------------------------------------------------------------------ */
/*  Request History                                                   */
/* ------------------------------------------------------------------ */

async function loadHistory() {
    var id = document.getElementById('historyProxyId').value.trim();
    if (!id) {
        showToast('Enter a proxy identifier.', 'error');
        return;
    }
    var container = document.getElementById('historyContainer');
    container.innerHTML = '<div class="empty-state"><p>Loading history...</p></div>';
    try {
        var data = await api('/proxy/history/' + encodeURIComponent(id) + '/', 'GET');
        var history = data.history || data.requests || data || [];
        if (!Array.isArray(history)) history = [];
        if (history.length === 0) {
            container.innerHTML = '<div class="empty-state"><p>No request history found for this proxy.</p></div>';
            return;
        }
        var html = '<table class="mock-table"><thead><tr>' +
            '<th>Timestamp</th><th>Method</th><th>Endpoint</th><th>Source</th><th>Status</th><th>Duration (ms)</th>' +
            '</tr></thead><tbody>';
        history.forEach(function(entry, idx) {
            var method = entry.method || 'GET';
            var source = (entry.source || 'unknown').toLowerCase();
            var sourceBadgeClass = 'source-' + source;
            var ts = entry.timestamp ? new Date(entry.timestamp).toLocaleString() : '-';
            var status = entry.status || entry.status_code || '-';
            var duration = entry.duration_ms !== undefined ? entry.duration_ms : (entry.duration !== undefined ? entry.duration : '-');

            html += '<tr class="clickable-row" onclick="toggleHistoryDetail(' + idx + ')">' +
                '<td>' + escapeHtml(ts) + '</td>' +
                '<td><span class="method-badge method-' + escapeAttr(method) + '">' + escapeHtml(method) + '</span></td>' +
                '<td class="mock-preview">' + escapeHtml(entry.endpoint || entry.path || entry.url || '-') + '</td>' +
                '<td><span class="source-badge ' + escapeAttr(sourceBadgeClass) + '">' + escapeHtml(source) + '</span></td>' +
                '<td>' + escapeHtml(String(status)) + '</td>' +
                '<td>' + escapeHtml(String(duration)) + '</td>' +
                '</tr>';

            // Detail row
            html += '<tr class="history-detail" id="historyDetail-' + idx + '"><td colspan="6">';
            html += '<div class="history-detail-label">Request Headers</div>';
            html += '<pre>' + escapeHtml(formatJson(entry.request_headers || {})) + '</pre>';
            html += '<div class="history-detail-label">Request Body</div>';
            html += '<pre>' + escapeHtml(formatJson(entry.request_body || {})) + '</pre>';
            html += '<div class="history-detail-label">Response Body</div>';
            html += '<pre>' + escapeHtml(formatJson(entry.response_body || {})) + '</pre>';
            html += '</td></tr>';
        });
        html += '</tbody></table>';
        container.innerHTML = html;
    } catch (err) {
        handleApiError(err);
        container.innerHTML = '<div class="empty-state"><p>Failed to load history.</p></div>';
    }
}

function toggleHistoryDetail(idx) {
    var row = document.getElementById('historyDetail-' + idx);
    if (row) row.classList.toggle('visible');
}

async function clearHistory() {
    var id = document.getElementById('historyProxyId').value.trim();
    if (!id) {
        showToast('Enter a proxy identifier.', 'error');
        return;
    }
    if (!confirm('Clear all request history for "' + id + '"?')) return;
    try {
        var data = await api('/proxy/history/' + encodeURIComponent(id) + '/clear/', 'POST');
        showToast('History cleared for "' + id + '".', 'success');
        document.getElementById('historyContainer').innerHTML = '<div class="empty-state"><p>History cleared.</p></div>';
    } catch (err) {
        handleApiError(err);
    }
}

/* ------------------------------------------------------------------ */
/*  Init on load                                                      */
/* ------------------------------------------------------------------ */

document.addEventListener('DOMContentLoaded', function() {
    checkHealth();
    loadProxyList();
});
