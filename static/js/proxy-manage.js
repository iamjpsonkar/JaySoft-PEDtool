/* ------------------------------------------------------------------ */
/*  proxy_manage.html — Proxy Management Dashboard JS                 */
/* ------------------------------------------------------------------ */

function toggleSection(bodyId, chevronId) {
    var body = document.getElementById(bodyId);
    var chevron = document.getElementById(chevronId);
    body.classList.toggle('collapsed');
    chevron.classList.toggle('collapsed');
}

/* ------------------------------------------------------------------ */
/*  Tab switching for Inspector panel                                  */
/* ------------------------------------------------------------------ */

function switchManageTab(name, el) {
    document.querySelectorAll('.tab-bar-item').forEach(function(t) { t.classList.remove('active'); });
    document.querySelectorAll('.tab-panel').forEach(function(p) { p.classList.remove('active'); });
    if (el) el.classList.add('active');
    var panel = document.getElementById('tab-' + name);
    if (panel) panel.classList.add('active');
    // Auto-load data if proxy is selected
    if (_selectedProxyId && document.getElementById('historyProxyId').value) {
        _loadInspectorTab(name);
    }
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
    container.innerHTML = '<div class="loading-state"><span class="spinner"></span> Loading proxies...</div>';
    try {
        var data = await api('/proxy/list/', 'GET');
        var proxies = data.proxies || data || [];
        if (!Array.isArray(proxies)) proxies = [];
        if (proxies.length === 0) {
            container.innerHTML = '<div class="empty-state"><p>No proxies registered yet.</p><a href="/proxy/" class="btn btn-blue btn-sm" style="margin-top:10px;">Create your first proxy &rarr;</a></div>';
            return;
        }
        _proxyList = proxies;
        _renderProxyTable(proxies);
    } catch (err) {
        handleApiError(err);
        container.innerHTML = '<div class="empty-state"><p>Failed to load proxy list.</p></div>';
    }
}

var _proxyList = [];
var _selectedProxyId = '';

function _renderProxyTable(proxies) {
    var container = document.getElementById('proxyListContainer');
    if (proxies.length === 0) {
        container.innerHTML = '<div class="empty-state"><p>No proxies match your search.</p></div>';
        return;
    }
    var html = '<div class="table-responsive"><table class="mock-table"><thead><tr>' +
        '<th>Identifier</th><th>API Domain</th><th>Mocks</th><th>Created</th><th>Actions</th>' +
        '</tr></thead><tbody>';
    proxies.forEach(function(p) {
        var id = escapeAttr(p.identifier || p.id || '');
        var domain = escapeHtml(p.api_domain || p.domain || '-');
        var mockCount = p.mock_count !== undefined ? p.mock_count : '-';
        var created = p.created_at ? new Date(p.created_at).toLocaleDateString() : '-';
        var selected = (id === _selectedProxyId) ? ' proxy-row-selected' : '';
        html += '<tr class="clickable-row' + selected + '" onclick="selectProxy(\'' + id + '\')">' +
            '<td><strong>' + escapeHtml(id) + '</strong></td>' +
            '<td class="mock-preview">' + domain + '</td>' +
            '<td>' + mockCount + '</td>' +
            '<td>' + escapeHtml(created) + '</td>' +
            '<td style="white-space:nowrap;" onclick="event.stopPropagation()">' +
                '<button class="btn btn-blue btn-sm" onclick="viewMocks(\'' + id + '\')">Mocks</button> ' +
                '<button class="btn btn-outline btn-sm" onclick="fillClone(\'' + id + '\')">Clone</button> ' +
                '<button class="btn btn-green btn-sm" onclick="exportProxy(\'' + id + '\')">Export</button> ' +
                '<button class="btn btn-outline btn-sm" onclick="exportPostman(\'' + id + '\')" title="Postman">PM</button> ' +
                '<span id="del-' + id + '">' +
                    '<button class="btn btn-red btn-sm" onclick="confirmDelete(\'' + id + '\')">Del</button>' +
                '</span>' +
            '</td>' +
            '</tr>';
    });
    html += '</tbody></table></div>';
    container.innerHTML = html;
}

function selectProxy(id) {
    _selectedProxyId = id;
    // Fill inspector input
    document.getElementById('historyProxyId').value = id;
    // Re-render table to show selection
    _renderProxyTable(_proxyList);
    // Load the active tab's data
    var activeTab = document.querySelector('.tab-bar-item.active');
    var tabName = activeTab ? activeTab.dataset.tab : 'history';
    _loadInspectorTab(tabName);
    showToast('Selected: ' + id, 'success');
}

function _loadInspectorTab(name) {
    switch(name) {
        case 'history': loadHistory(); break;
        case 'snapshots': loadSnapshots(); break;
        case 'analytics': loadAnalytics(); break;
        case 'health': checkProxyHealth(); break;
        case 'storage': loadStorageInfo(); break;
        case 'state': loadState(); break;
        case 'users': loadUsers(); break;
    }
}

function filterProxyList() {
    var query = (document.getElementById('proxySearchInput') || {}).value || '';
    query = query.trim().toLowerCase();
    if (!query) {
        _renderProxyTable(_proxyList);
        return;
    }
    var filtered = _proxyList.filter(function(p) {
        var id = (p.identifier || p.id || '').toLowerCase();
        var domain = (p.api_domain || '').toLowerCase();
        return id.indexOf(query) >= 0 || domain.indexOf(query) >= 0;
    });
    _renderProxyTable(filtered);
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
        var data = await api('/proxy/get/' + encodeURIComponent(id) + '/', 'GET');
        document.getElementById('mocksModalBody').textContent = formatJson(data.mocked_requests || data);
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
        var data = await api('/proxy/export/all/', 'GET');
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

function exportPostman(id) {
    var url = '/proxy/export/' + encodeURIComponent(id) + '/postman/';
    // Open in new tab — browser will download the file due to Content-Disposition header
    window.open(url, '_blank');
    showToast('Downloading Postman collection for "' + id + '"...', 'success');
}

/* ------------------------------------------------------------------ */
/*  Request History with Filters (Feature 4)                          */
/* ------------------------------------------------------------------ */

// Store last loaded history for replay
var _historyEntries = [];

async function loadHistory() {
    var id = document.getElementById('historyProxyId').value.trim();
    if (!id) {
        showToast('Enter a proxy identifier.', 'error');
        return;
    }
    var container = document.getElementById('historyContainer');
    container.innerHTML = '<div class="empty-state"><p>Loading history...</p></div>';

    // Build query params from filter inputs
    var params = new URLSearchParams();
    var filterMethod = document.getElementById('historyFilterMethod');
    var filterEndpoint = document.getElementById('historyFilterEndpoint');
    var filterSource = document.getElementById('historyFilterSource');
    var filterStatusMin = document.getElementById('historyFilterStatusMin');
    var filterStatusMax = document.getElementById('historyFilterStatusMax');
    if (filterMethod && filterMethod.value) params.set('method', filterMethod.value);
    if (filterEndpoint && filterEndpoint.value.trim()) params.set('endpoint', filterEndpoint.value.trim());
    if (filterSource && filterSource.value) params.set('source', filterSource.value);
    if (filterStatusMin && filterStatusMin.value) params.set('status_min', filterStatusMin.value);
    if (filterStatusMax && filterStatusMax.value) params.set('status_max', filterStatusMax.value);

    var qs = params.toString();
    var url = '/proxy/history/' + encodeURIComponent(id) + '/' + (qs ? '?' + qs : '');

    try {
        var data = await api(url, 'GET');
        var history = data.history || data.requests || data || [];
        if (!Array.isArray(history)) history = [];
        _historyEntries = history;
        if (history.length === 0) {
            container.innerHTML = '<div class="empty-state"><p>No request history found.</p></div>';
            return;
        }
        var html = '<table class="mock-table"><thead><tr>' +
            '<th>Timestamp</th><th>Method</th><th>Endpoint</th><th>Source</th><th>Status</th><th>Duration</th><th>Actions</th>' +
            '</tr></thead><tbody>';
        history.forEach(function(entry, idx) {
            var method = entry.method || 'GET';
            var source = (entry.source || 'unknown').toLowerCase();
            var sourceBadgeClass = 'source-' + source;
            var tsRaw = entry.created_at || entry.timestamp;
            var ts = tsRaw ? new Date(tsRaw).toLocaleString() : '-';
            var status = entry.response_status || entry.status || entry.status_code || '-';
            var duration = entry.duration_ms !== undefined ? entry.duration_ms : '-';

            html += '<tr class="clickable-row" onclick="toggleHistoryDetail(' + idx + ')">' +
                '<td>' + escapeHtml(ts) + '</td>' +
                '<td><span class="method-badge method-' + escapeAttr(method) + '">' + escapeHtml(method) + '</span></td>' +
                '<td class="mock-preview">' + escapeHtml(entry.endpoint || '-') + '</td>' +
                '<td><span class="source-badge ' + escapeAttr(sourceBadgeClass) + '">' + escapeHtml(source) + '</span></td>' +
                '<td>' + escapeHtml(String(status)) + '</td>' +
                '<td>' + escapeHtml(String(duration)) + 'ms</td>' +
                '<td><button class="btn btn-blue btn-sm" onclick="event.stopPropagation();replayRequest(' + idx + ')" title="Replay this request">Replay</button></td>' +
                '</tr>';

            html += '<tr class="history-detail" id="historyDetail-' + idx + '"><td colspan="7">';
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

/* ------------------------------------------------------------------ */
/*  Request Replay (Feature 2)                                        */
/* ------------------------------------------------------------------ */

async function replayRequest(idx) {
    var entry = _historyEntries[idx];
    if (!entry) return;
    var id = document.getElementById('historyProxyId').value.trim();
    var endpoint = entry.endpoint || '';
    var method = entry.method || 'GET';
    var cleanEp = endpoint.startsWith('/') ? endpoint.slice(1) : endpoint;
    var url = '/proxy/' + encodeURIComponent(id) + '/' + cleanEp;
    if (entry.query_params) url += '?' + entry.query_params;

    var body = undefined;
    if (method !== 'GET' && method !== 'HEAD' && entry.request_body) {
        body = typeof entry.request_body === 'string' ? JSON.parse(entry.request_body) : entry.request_body;
    }

    showToast('Replaying ' + method + ' ' + endpoint + '...', 'success');
    try {
        var data = await api(url, method, body);
        showToast('Replay succeeded', 'success');
        showResponse('mainResponse', data);
    } catch (e) {
        showToast('Replay returned ' + (e.status || 'error'), 'error');
        showResponse('mainResponse', e.data || e);
    }
}

async function clearHistory() {
    var id = document.getElementById('historyProxyId').value.trim();
    if (!id) {
        showToast('Enter a proxy identifier.', 'error');
        return;
    }
    if (!confirm('Clear all request history for "' + id + '"?')) return;
    try {
        await api('/proxy/history/' + encodeURIComponent(id) + '/clear/', 'POST');
        showToast('History cleared for "' + id + '".', 'success');
        document.getElementById('historyContainer').innerHTML = '<div class="empty-state"><p>History cleared.</p></div>';
    } catch (err) {
        handleApiError(err);
    }
}

/* ------------------------------------------------------------------ */
/*  State Snapshots (Feature 6)                                       */
/* ------------------------------------------------------------------ */

async function saveSnapshot() {
    var id = document.getElementById('historyProxyId').value.trim();
    if (!id) { showToast('Enter a proxy identifier first.', 'error'); return; }
    var name = prompt('Snapshot name:', 'snapshot_' + new Date().toISOString().slice(0,16).replace(/[:-]/g,''));
    if (!name) return;
    try {
        var data = await api('/proxy/state/' + encodeURIComponent(id) + '/snapshot/', 'POST', { name: name });
        showToast('Snapshot "' + name + '" saved.', 'success');
        loadSnapshots(id);
    } catch (err) { handleApiError(err); }
}

async function loadSnapshots(id) {
    if (!id) id = document.getElementById('historyProxyId').value.trim();
    if (!id) return;
    var container = document.getElementById('snapshotsContainer');
    if (!container) return;
    try {
        var data = await api('/proxy/state/' + encodeURIComponent(id) + '/snapshots/', 'GET');
        var snaps = data.snapshots || [];
        if (snaps.length === 0) {
            container.innerHTML = '<div class="empty-state"><p>No snapshots.</p></div>';
            return;
        }
        var html = '<table class="mock-table"><thead><tr><th>Name</th><th>Created</th><th>Actions</th></tr></thead><tbody>';
        snaps.forEach(function(s) {
            html += '<tr><td>' + escapeHtml(s.name) + '</td>' +
                '<td>' + escapeHtml(s.created_at || '-') + '</td>' +
                '<td><button class="btn btn-blue btn-sm" onclick="restoreSnapshot(' + s.id + ')">Restore</button> ' +
                '<button class="btn btn-red btn-sm" onclick="deleteSnapshot(' + s.id + ')">Delete</button></td></tr>';
        });
        html += '</tbody></table>';
        container.innerHTML = html;
    } catch (err) { container.innerHTML = '<div class="empty-state"><p>Failed to load snapshots.</p></div>'; }
}

async function restoreSnapshot(snapId) {
    if (!confirm('Restore state from this snapshot?')) return;
    try {
        var data = await api('/proxy/state/restore/' + snapId + '/', 'POST');
        showToast('State restored from snapshot.', 'success');
        showResponse('mainResponse', data);
    } catch (err) { handleApiError(err); }
}

async function deleteSnapshot(snapId) {
    if (!confirm('Delete this snapshot?')) return;
    try {
        await api('/proxy/state/snapshot/' + snapId + '/', 'DELETE');
        showToast('Snapshot deleted.', 'success');
        var id = document.getElementById('historyProxyId').value.trim();
        loadSnapshots(id);
    } catch (err) { handleApiError(err); }
}

/* ------------------------------------------------------------------ */
/*  Analytics (Feature 14)                                            */
/* ------------------------------------------------------------------ */

async function loadAnalytics() {
    var id = document.getElementById('historyProxyId').value.trim();
    if (!id) { showToast('Enter a proxy identifier.', 'error'); return; }
    try {
        var data = await api('/proxy/analytics/' + encodeURIComponent(id) + '/', 'GET');
        var html = '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:16px;">';
        html += '<div class="stat-card"><div class="stat-value">' + (data.total_requests || 0) + '</div><div class="stat-label">Total Requests</div></div>';
        html += '<div class="stat-card"><div class="stat-value">' + (data.avg_latency_ms != null ? data.avg_latency_ms + 'ms' : '-') + '</div><div class="stat-label">Avg Latency</div></div>';
        html += '<div class="stat-card"><div class="stat-value">' + (data.error_rate || 0) + '%</div><div class="stat-label">Error Rate</div></div>';
        html += '<div class="stat-card"><div class="stat-value">' + (data.stale_mocks ? data.stale_mocks.length : 0) + '</div><div class="stat-label">Stale Mocks</div></div>';
        html += '</div>';
        if (data.by_source) {
            html += '<div class="history-detail-label">By Source</div><pre>' + escapeHtml(formatJson(data.by_source)) + '</pre>';
        }
        if (data.top_endpoints && data.top_endpoints.length) {
            html += '<div class="history-detail-label">Top Endpoints</div>';
            html += '<table class="mock-table"><thead><tr><th>Endpoint</th><th>Method</th><th>Hits</th></tr></thead><tbody>';
            data.top_endpoints.forEach(function(ep) {
                html += '<tr><td>' + escapeHtml(ep.endpoint) + '</td><td>' + escapeHtml(ep.method) + '</td><td>' + ep.hits + '</td></tr>';
            });
            html += '</tbody></table>';
        }
        showResponse('mainResponse', data);
        var container = document.getElementById('analyticsContainer');
        if (container) container.innerHTML = html;
    } catch (err) { handleApiError(err); }
}

/* ------------------------------------------------------------------ */
/*  Storage Info                                                      */
/* ------------------------------------------------------------------ */

async function loadStorageInfo() {
    var container = document.getElementById('storageContainer');
    if (container) container.innerHTML = '<div class="loading-state"><span class="spinner"></span> Checking storage...</div>';
    try {
        var data = await api('/proxy/storage/', 'GET');
        var pct = Math.min(100, (data.db_size_bytes / (500 * 1024 * 1024)) * 100);
        var meterClass = pct < 60 ? 'meter-ok' : (pct < 85 ? 'meter-warn' : 'meter-danger');
        var html = '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px;margin-bottom:16px;">';
        html += '<div class="stat-card"><div class="stat-value">' + data.db_size_mb + ' MB</div><div class="stat-label">DB Size</div></div>';
        html += '<div class="stat-card"><div class="stat-value">' + data.history_count + '</div><div class="stat-label">History Rows</div></div>';
        html += '<div class="stat-card"><div class="stat-value">' + data.mock_count + '</div><div class="stat-label">Mocks</div></div>';
        html += '<div class="stat-card"><div class="stat-value">' + data.proxy_count + '</div><div class="stat-label">Proxies</div></div>';
        html += '<div class="stat-card"><div class="stat-value">' + data.snapshot_count + '</div><div class="stat-label">Snapshots</div></div>';
        html += '</div>';
        html += '<label style="font-size:0.78rem;">Storage usage (of 500 MB)</label>';
        html += '<div class="storage-meter"><div class="storage-meter-fill ' + meterClass + '" style="width:' + Math.max(1, pct) + '%;"></div></div>';
        html += '<span style="font-size:0.72rem;color:var(--text-muted);">' + pct.toFixed(1) + '% used &mdash; History limit: ' + data.history_limit + ' rows/proxy</span>';
        if (container) container.innerHTML = html;
        showToast('DB: ' + data.db_size_mb + ' MB (' + pct.toFixed(1) + '% of 500 MB)', 'success');
    } catch (err) { handleApiError(err); }
}

async function runCleanup() {
    var days = prompt('Delete history older than (days):', '7');
    if (days === null) return;
    try {
        var data = await api('/proxy/storage/cleanup/', 'POST', { keep_days: parseInt(days), vacuum: true });
        showToast('Cleaned up! Saved ' + data.saved_mb + ' MB', 'success');
        showResponse('mainResponse', data);
    } catch (err) { handleApiError(err); }
}

/* ------------------------------------------------------------------ */
/*  Proxy Health (Feature 17)                                         */
/* ------------------------------------------------------------------ */

async function checkProxyHealth(id) {
    if (!id) id = document.getElementById('historyProxyId').value.trim();
    if (!id) { showToast('Enter a proxy identifier.', 'error'); return; }
    var container = document.getElementById('healthContainer');
    if (container) container.innerHTML = '<div class="loading-state"><span class="spinner"></span> Checking upstream...</div>';
    try {
        var data = await api('/proxy/health/' + encodeURIComponent(id) + '/', 'GET');
        var status = data.upstream_status || 'unknown';
        var dotClass = 'health-' + status;
        var html = '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;">';
        html += '<div class="stat-card"><div class="stat-value"><span class="health-dot ' + dotClass + '"></span> ' + escapeHtml(status) + '</div><div class="stat-label">Upstream</div></div>';
        html += '<div class="stat-card"><div class="stat-value">' + (data.upstream_latency_ms != null ? data.upstream_latency_ms + 'ms' : '-') + '</div><div class="stat-label">Latency</div></div>';
        html += '<div class="stat-card"><div class="stat-value">' + (data.mock_count || 0) + '</div><div class="stat-label">Mocks</div></div>';
        html += '<div class="stat-card"><div class="stat-value">' + (data.history_count || 0) + '</div><div class="stat-label">History</div></div>';
        html += '</div>';
        if (data.error) html += '<p style="color:var(--danger);margin-top:10px;font-size:0.82rem;">' + escapeHtml(data.error) + '</p>';
        html += '<p style="color:var(--text-muted);margin-top:10px;font-size:0.78rem;">Domain: ' + escapeHtml(data.api_domain || '-') + '</p>';
        if (container) container.innerHTML = html;
        showToast(id + ': ' + status + (data.upstream_latency_ms ? ' (' + data.upstream_latency_ms + 'ms)' : ''), status === 'healthy' ? 'success' : 'error');
    } catch (err) {
        handleApiError(err);
        if (container) container.innerHTML = '<div class="empty-state"><p>Failed to check health.</p></div>';
    }
}

/* ------------------------------------------------------------------ */
/*  State Viewer/Editor (Phase 2A)                                    */
/* ------------------------------------------------------------------ */

async function loadState() {
    var id = document.getElementById('historyProxyId').value.trim();
    if (!id) return;
    var container = document.getElementById('stateContainer');
    if (!container) return;
    container.innerHTML = '<div class="loading-state"><span class="spinner"></span> Loading state...</div>';
    try {
        var data = await api('/proxy/state/' + encodeURIComponent(id) + '/', 'GET');
        var state = data.state || {};
        var isEmpty = Object.keys(state).length === 0;
        var html = '<div class="inline-form" style="margin-bottom:12px;">';
        html += '<button class="btn btn-blue btn-sm" onclick="editStateModal()">Edit</button>';
        html += '<button class="btn btn-outline btn-sm" onclick="loadState()">Refresh</button>';
        html += '<button class="btn btn-red btn-sm" onclick="clearStateConfirm()">Clear</button>';
        html += '</div>';
        if (isEmpty) {
            html += '<div class="empty-state"><p>No state stored for this proxy. Use <code>_store</code> in a mock response to persist data.</p></div>';
        } else {
            html += '<pre style="background:var(--code-bg);color:var(--code-text);padding:12px;border-radius:var(--radius-sm);font-size:0.78rem;max-height:300px;overflow:auto;white-space:pre-wrap;word-break:break-word;">' + escapeHtml(JSON.stringify(state, null, 2)) + '</pre>';
        }
        container.innerHTML = html;
    } catch (err) {
        container.innerHTML = '<div class="empty-state"><p>Failed to load state.</p></div>';
    }
}

function editStateModal() {
    var id = document.getElementById('historyProxyId').value.trim();
    if (!id) return;
    var modal = document.getElementById('stateModal');
    var body = document.getElementById('stateModalBody');
    var header = modal.querySelector('.modal-header h3');
    header.textContent = 'Edit State — ' + id;
    var html = '<textarea id="stateEditArea" style="width:100%;min-height:200px;font-family:var(--mono);font-size:0.82rem;padding:10px;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--surface);color:var(--text);resize:vertical;">Loading...</textarea>';
    html += '<div style="margin-top:10px;display:flex;gap:8px;">';
    html += '<button class="btn btn-blue btn-sm" onclick="_saveState(\'PUT\')">Replace (PUT)</button>';
    html += '<button class="btn btn-outline btn-sm" onclick="_saveState(\'PATCH\')">Merge (PATCH)</button>';
    html += '<button class="btn btn-outline btn-sm" onclick="_formatStateEdit()">Format</button>';
    html += '</div>';
    body.innerHTML = html;
    modal.classList.add('visible');
    // Load current state into textarea
    api('/proxy/state/' + encodeURIComponent(id) + '/', 'GET').then(function(data) {
        document.getElementById('stateEditArea').value = JSON.stringify(data.state || {}, null, 2);
    });
}

function closeStateModal() {
    document.getElementById('stateModal').classList.remove('visible');
}

function _formatStateEdit() {
    var ta = document.getElementById('stateEditArea');
    try { ta.value = JSON.stringify(JSON.parse(ta.value), null, 2); showToast('Formatted', 'success'); }
    catch (e) { showToast('Invalid JSON', 'error'); }
}

async function _saveState(method) {
    var id = document.getElementById('historyProxyId').value.trim();
    var ta = document.getElementById('stateEditArea');
    var data;
    try { data = JSON.parse(ta.value); } catch (e) { showToast('Invalid JSON', 'error'); return; }
    try {
        await api('/proxy/state/' + encodeURIComponent(id) + '/', method, data);
        showToast('State ' + (method === 'PUT' ? 'replaced' : 'merged'), 'success');
        closeStateModal();
        loadState();
    } catch (err) { handleApiError(err); }
}

function clearStateConfirm() {
    var id = document.getElementById('historyProxyId').value.trim();
    if (!confirm('Clear all state for "' + id + '"? This cannot be undone.')) return;
    api('/proxy/state/' + encodeURIComponent(id) + '/', 'DELETE').then(function() {
        showToast('State cleared', 'success');
        loadState();
    }).catch(handleApiError);
}

/* ------------------------------------------------------------------ */
/*  User Management (Phase 2B)                                        */
/* ------------------------------------------------------------------ */

async function loadUsers() {
    var id = document.getElementById('historyProxyId').value.trim();
    if (!id) return;
    var container = document.getElementById('usersContainer');
    if (!container) return;
    container.innerHTML = '<div class="loading-state"><span class="spinner"></span> Loading users...</div>';
    try {
        var data = await api('/proxy/users/' + encodeURIComponent(id) + '/', 'GET');
        var users = data.users || [];
        var html = '<div class="inline-form" style="margin-bottom:12px;">';
        html += '<input type="text" id="newUsername" placeholder="Username" style="width:140px;" />';
        html += '<input type="password" id="newPassword" placeholder="Password" style="width:140px;" />';
        html += '<button class="btn btn-blue btn-sm" onclick="addUser()">Add User</button>';
        html += '</div>';
        html += '<p style="font-size:0.75rem;color:var(--text-muted);margin-bottom:10px;">These are mock simulation credentials used by <code>verify_password()</code> in snippet expressions.</p>';
        if (users.length === 0) {
            html += '<div class="empty-state"><p>No users configured for this proxy.</p></div>';
        } else {
            html += '<table class="mock-table"><thead><tr><th>Username</th><th>Actions</th></tr></thead><tbody>';
            users.forEach(function(u) {
                html += '<tr><td><strong>' + escapeHtml(u.username) + '</strong></td>';
                html += '<td><button class="btn btn-red btn-sm" onclick="deleteUser(\'' + escapeAttr(u.username) + '\')">Delete</button></td></tr>';
            });
            html += '</tbody></table>';
        }
        container.innerHTML = html;
    } catch (err) {
        container.innerHTML = '<div class="empty-state"><p>Failed to load users.</p></div>';
    }
}

async function addUser() {
    var id = document.getElementById('historyProxyId').value.trim();
    var username = document.getElementById('newUsername').value.trim();
    var password = document.getElementById('newPassword').value;
    if (!username || !password) { showToast('Username and password required', 'error'); return; }
    try {
        await api('/proxy/users/' + encodeURIComponent(id) + '/', 'POST', { username: username, password: password });
        showToast('User "' + username + '" added', 'success');
        loadUsers();
    } catch (err) { handleApiError(err); }
}

async function deleteUser(username) {
    var id = document.getElementById('historyProxyId').value.trim();
    if (!confirm('Delete user "' + username + '"?')) return;
    try {
        await api('/proxy/users/' + encodeURIComponent(id) + '/' + encodeURIComponent(username) + '/', 'DELETE');
        showToast('User deleted', 'success');
        loadUsers();
    } catch (err) { handleApiError(err); }
}

/* ------------------------------------------------------------------ */
/*  Init on load                                                      */
/* ------------------------------------------------------------------ */

document.addEventListener('DOMContentLoaded', function() {
    checkHealth();
    loadProxyList();
});
