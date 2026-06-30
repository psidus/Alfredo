/**
 * Alfredo SDK – Embeddable Widget Client
 * =========================================================
 * Self-contained JavaScript module (zero dependencies) that
 * creates the `AlfredoClient` global object.  It uses Shadow
 * DOM for complete CSS isolation from the host page.
 *
 * Usage:
 *   <script src="https://your-alfredo-server/sdk/alfredo-sdk.js"></script>
 *   <script>
 *     AlfredoClient.init({
 *       serverUrl: 'https://your-alfredo-server',
 *       apiKey:    'your-api-key'
 *     });
 *   </script>
 */

/* eslint-disable no-inner-declarations */
const AlfredoClient = (() => {
    'use strict';

    // ─── Embedded CSS (avoids extra network request) ───────────────
    const EMBEDDED_CSS = `
/* Alfredo SDK Widget – Shadow DOM Stylesheet */

:host {
    all: initial;
    font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
    font-size: 14px;
    line-height: 1.5;
    color: #e0e0e0;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

.alfredo-widget {
    position: fixed; bottom: 24px; right: 24px;
    width: 380px; min-width: 320px; max-width: 380px; max-height: 85vh;
    background: #1a1a2e; border-radius: 12px;
    box-shadow: 0 8px 32px rgba(0,0,0,0.45), 0 0 0 1px rgba(108,99,255,0.15);
    backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
    display: flex; flex-direction: column; overflow: hidden;
    z-index: 2147483646;
    transition: all 0.35s cubic-bezier(0.4,0,0.2,1);
    font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
    font-size: 14px; line-height: 1.5; color: #e0e0e0;
}
.alfredo-widget.minimized {
    width: 48px; height: 48px; min-width: 48px; max-width: 48px;
    border-radius: 50%; cursor: pointer;
    display: flex; align-items: center; justify-content: center;
    background: linear-gradient(135deg, #6c63ff 0%, #564ff0 100%);
    box-shadow: 0 4px 16px rgba(108,99,255,0.45), 0 0 0 2px rgba(108,99,255,0.2);
    overflow: hidden; padding: 0;
}
.alfredo-widget.minimized:hover {
    transform: scale(1.1);
    box-shadow: 0 6px 24px rgba(108,99,255,0.55), 0 0 0 3px rgba(108,99,255,0.3);
}
.alfredo-widget.minimized .alfredo-header,
.alfredo-widget.minimized .alfredo-body,
.alfredo-widget.minimized .alfredo-status { display: none; }
.alfredo-widget.minimized .alfredo-mini-logo { display: flex; }

.alfredo-mini-logo {
    display: none; width: 48px; height: 48px;
    align-items: center; justify-content: center;
    font-size: 22px; font-weight: 800; color: #fff;
    letter-spacing: -0.5px; user-select: none; pointer-events: none;
}

.alfredo-header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 12px 16px;
    background: linear-gradient(135deg, #16213e 0%, #1a1a2e 100%);
    border-bottom: 1px solid rgba(108,99,255,0.2);
    cursor: grab; user-select: none; flex-shrink: 0;
}
.alfredo-header:active { cursor: grabbing; }
.alfredo-header-title {
    display: flex; align-items: center; gap: 8px;
    font-size: 15px; font-weight: 700; color: #fff; letter-spacing: 0.3px;
}
.alfredo-header-title .alfredo-logo-char {
    display: inline-flex; align-items: center; justify-content: center;
    width: 26px; height: 26px;
    background: linear-gradient(135deg, #6c63ff 0%, #564ff0 100%);
    border-radius: 6px; font-size: 14px; font-weight: 800; color: #fff;
}
.alfredo-header-controls { display: flex; align-items: center; gap: 6px; }
.alfredo-header-controls button {
    background: none; border: none; color: #a0a0b0; cursor: pointer;
    padding: 4px; border-radius: 4px; font-size: 16px; line-height: 1;
    transition: all 0.2s ease; display: flex; align-items: center;
    justify-content: center; width: 28px; height: 28px;
}
.alfredo-header-controls button:hover { background: rgba(108,99,255,0.15); color: #fff; }

.alfredo-body {
    flex: 1 1 auto; overflow-y: auto; overflow-x: hidden;
    padding: 16px; display: flex; flex-direction: column; gap: 16px;
    scrollbar-width: thin; scrollbar-color: rgba(108,99,255,0.3) transparent;
}
.alfredo-body::-webkit-scrollbar { width: 5px; }
.alfredo-body::-webkit-scrollbar-track { background: transparent; }
.alfredo-body::-webkit-scrollbar-thumb { background: rgba(108,99,255,0.3); border-radius: 3px; }
.alfredo-body::-webkit-scrollbar-thumb:hover { background: rgba(108,99,255,0.5); }

.alfredo-section-label {
    font-size: 11px; font-weight: 600; text-transform: uppercase;
    letter-spacing: 1px; color: #a0a0b0; margin-bottom: 6px;
}

.alfredo-workflow-select {
    width: 100%; padding: 10px 14px; background: #16213e;
    border: 1px solid rgba(108,99,255,0.25); border-radius: 8px;
    color: #e0e0e0; font-size: 13px; font-family: inherit; outline: none;
    cursor: pointer; transition: border-color 0.3s ease, box-shadow 0.3s ease;
    appearance: none; -webkit-appearance: none;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath d='M3 5l3 3 3-3' fill='none' stroke='%23a0a0b0' stroke-width='1.5' stroke-linecap='round'/%3E%3C/svg%3E");
    background-repeat: no-repeat; background-position: right 12px center;
    padding-right: 32px;
}
.alfredo-workflow-select:hover { border-color: rgba(108,99,255,0.5); }
.alfredo-workflow-select:focus { border-color: #6c63ff; box-shadow: 0 0 0 3px rgba(108,99,255,0.15); }
.alfredo-workflow-select option { background: #16213e; color: #e0e0e0; }

.alfredo-input-list { display: flex; flex-direction: column; gap: 8px; list-style: none; }
.alfredo-input-item {
    display: flex; align-items: center; gap: 10px; padding: 10px 12px;
    background: #16213e; border: 1px solid rgba(108,99,255,0.15);
    border-radius: 8px; transition: all 0.3s ease;
}
.alfredo-input-item:hover { border-color: rgba(108,99,255,0.35); }
.alfredo-input-item::before {
    content: ''; flex-shrink: 0; width: 8px; height: 8px; border-radius: 50%;
    background: #e74c3c; box-shadow: 0 0 6px rgba(231,76,60,0.5); transition: all 0.3s ease;
}
.alfredo-input-item.mapped::before { background: #2ecc71; box-shadow: 0 0 6px rgba(46,204,113,0.5); }
.alfredo-input-item.unmapped::before { background: #e74c3c; box-shadow: 0 0 6px rgba(231,76,60,0.5); }
.alfredo-input-item-label {
    flex: 1 1 auto; font-size: 13px; color: #e0e0e0;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.alfredo-input-item-selector {
    flex: 0 0 auto; font-size: 11px; color: #a0a0b0; max-width: 110px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    font-family: 'Consolas','Monaco',monospace;
}

.alfredo-picker-btn {
    flex-shrink: 0; padding: 5px 10px;
    background: linear-gradient(135deg, #6c63ff 0%, #564ff0 100%);
    color: #fff; border: none; border-radius: 6px;
    font-size: 12px; font-weight: 600; font-family: inherit;
    cursor: pointer; transition: all 0.3s ease; white-space: nowrap;
}
.alfredo-picker-btn:hover { transform: scale(1.05); box-shadow: 0 2px 10px rgba(108,99,255,0.4); }
.alfredo-picker-btn:active { transform: scale(0.97); }
.alfredo-picker-btn.picking {
    background: linear-gradient(135deg, #f39c12 0%, #e67e22 100%);
    animation: alfredo-pulse 1.5s ease-in-out infinite;
}

.alfredo-output-section, .alfredo-trigger-section {
    padding: 12px; background: #16213e;
    border: 1px solid rgba(108,99,255,0.15); border-radius: 8px;
    display: flex; flex-direction: column; gap: 8px;
}
.alfredo-output-section .alfredo-section-title,
.alfredo-trigger-section .alfredo-section-title {
    font-size: 12px; font-weight: 600; color: #a0a0b0;
    text-transform: uppercase; letter-spacing: 0.5px;
}
.alfredo-mapped-element { display: flex; align-items: center; gap: 8px; font-size: 12px; color: #e0e0e0; }
.alfredo-mapped-element .alfredo-selector-tag {
    background: rgba(108,99,255,0.15); color: #6c63ff;
    padding: 2px 8px; border-radius: 4px;
    font-family: 'Consolas','Monaco',monospace; font-size: 11px;
    max-width: 180px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}

.alfredo-validation {
    padding: 10px 14px; border-radius: 8px; font-size: 12px;
    line-height: 1.6; transition: all 0.3s ease; border: 1px solid transparent;
}
.alfredo-validation.valid { background: rgba(46,204,113,0.1); border-color: rgba(46,204,113,0.3); color: #2ecc71; }
.alfredo-validation.invalid { background: rgba(231,76,60,0.1); border-color: rgba(231,76,60,0.3); color: #e74c3c; }
.alfredo-validation ul { list-style: none; padding-left: 4px; margin-top: 4px; }
.alfredo-validation ul li::before { content: '• '; color: inherit; }
.alfredo-validation .alfredo-warning-item { color: #f39c12; }

.alfredo-actions { display: flex; gap: 8px; flex-shrink: 0; }
.alfredo-actions button {
    flex: 1; padding: 10px 14px; border: none; border-radius: 8px;
    font-size: 13px; font-weight: 600; font-family: inherit;
    cursor: pointer; transition: all 0.3s ease; position: relative; overflow: hidden;
}
.alfredo-actions button:active { transform: scale(0.97); }
.alfredo-btn-save { background: linear-gradient(135deg, #2ecc71 0%, #27ae60 100%); color: #fff; }
.alfredo-btn-save:hover { box-shadow: 0 4px 16px rgba(46,204,113,0.35); transform: translateY(-1px); }
.alfredo-btn-save:disabled { opacity: 0.4; cursor: not-allowed; transform: none !important; box-shadow: none !important; }
.alfredo-btn-test { background: linear-gradient(135deg, #6c63ff 0%, #564ff0 100%); color: #fff; }
.alfredo-btn-test:hover { box-shadow: 0 4px 16px rgba(108,99,255,0.35); transform: translateY(-1px); }
.alfredo-btn-test:disabled { opacity: 0.4; cursor: not-allowed; transform: none !important; box-shadow: none !important; }
.alfredo-btn-clear { background: rgba(231,76,60,0.15); color: #e74c3c; flex: 0 0 auto; width: 40px; padding: 10px; }
.alfredo-btn-clear:hover { background: rgba(231,76,60,0.25); }

.alfredo-status {
    display: flex; align-items: center; gap: 8px; padding: 8px 16px;
    background: #0f1a2e; border-top: 1px solid rgba(108,99,255,0.1);
    font-size: 11px; color: #a0a0b0; flex-shrink: 0;
}
.alfredo-status-dot {
    width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0;
    transition: background 0.3s ease, box-shadow 0.3s ease;
}
.alfredo-status-dot.connected { background: #2ecc71; box-shadow: 0 0 6px rgba(46,204,113,0.6); }
.alfredo-status-dot.loading { background: #f39c12; box-shadow: 0 0 6px rgba(243,156,18,0.6); animation: alfredo-pulse 1.2s ease-in-out infinite; }
.alfredo-status-dot.error { background: #e74c3c; box-shadow: 0 0 6px rgba(231,76,60,0.6); }
.alfredo-status-text { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

.alfredo-overlay {
    position: fixed; top: 0; left: 0; width: 100vw; height: 100vh;
    background: rgba(0,0,0,0.3); z-index: 2147483645;
    cursor: crosshair; transition: opacity 0.25s ease;
}
.alfredo-overlay-banner {
    position: fixed; top: 16px; left: 50%; transform: translateX(-50%);
    background: #1a1a2e; color: #e0e0e0; padding: 10px 24px; border-radius: 8px;
    font-size: 13px; font-weight: 600;
    box-shadow: 0 4px 24px rgba(0,0,0,0.5); z-index: 2147483647;
    pointer-events: none; display: flex; align-items: center; gap: 8px;
    font-family: system-ui, -apple-system, sans-serif;
}
.alfredo-overlay-banner kbd {
    background: rgba(108,99,255,0.2); padding: 2px 6px; border-radius: 4px;
    font-size: 11px; color: #6c63ff; font-family: inherit;
}

.alfredo-highlight {
    position: absolute; pointer-events: none;
    border: 2px dashed #6c63ff; border-radius: 4px;
    background: rgba(108,99,255,0.08);
    box-shadow: 0 0 12px rgba(108,99,255,0.35);
    z-index: 2147483647; transition: all 0.1s ease;
    animation: alfredo-highlight-pulse 1.5s ease-in-out infinite;
}
.alfredo-highlight.input-pick { border-color: #6c63ff; box-shadow: 0 0 12px rgba(108,99,255,0.35); background: rgba(108,99,255,0.08); }
.alfredo-highlight.output-pick { border-color: #2ecc71; box-shadow: 0 0 12px rgba(46,204,113,0.35); background: rgba(46,204,113,0.08); }
.alfredo-highlight.trigger-pick { border-color: #f39c12; box-shadow: 0 0 12px rgba(243,156,18,0.35); background: rgba(243,156,18,0.08); }

.alfredo-spinner {
    display: inline-block; width: 18px; height: 18px;
    border: 2px solid rgba(108,99,255,0.2); border-top-color: #6c63ff;
    border-radius: 50%; animation: alfredo-spin 0.7s linear infinite;
}
.alfredo-spinner.small { width: 14px; height: 14px; border-width: 2px; }
.alfredo-spinner.large { width: 28px; height: 28px; border-width: 3px; }

.alfredo-toast {
    position: fixed; bottom: 90px; right: 24px;
    padding: 12px 20px; border-radius: 8px;
    font-size: 13px; font-weight: 500; color: #fff;
    box-shadow: 0 4px 20px rgba(0,0,0,0.35);
    z-index: 2147483647; opacity: 0;
    transform: translateY(12px) scale(0.95);
    transition: all 0.35s cubic-bezier(0.4,0,0.2,1);
    pointer-events: none; max-width: 340px;
    font-family: system-ui, -apple-system, sans-serif;
}
.alfredo-toast.show { opacity: 1; transform: translateY(0) scale(1); pointer-events: auto; }
.alfredo-toast.success { background: linear-gradient(135deg, #2ecc71 0%, #27ae60 100%); }
.alfredo-toast.error { background: linear-gradient(135deg, #e74c3c 0%, #c0392b 100%); }
.alfredo-toast.warning { background: linear-gradient(135deg, #f39c12 0%, #e67e22 100%); }
.alfredo-toast.info { background: linear-gradient(135deg, #6c63ff 0%, #564ff0 100%); }

.alfredo-test-preview {
    padding: 10px 12px; background: #0f1a2e;
    border: 1px solid rgba(108,99,255,0.2); border-radius: 8px;
    font-size: 12px; font-family: 'Consolas','Monaco',monospace;
    color: #a0a0b0; max-height: 160px; overflow-y: auto;
}
.alfredo-test-preview .preview-row { display: flex; justify-content: space-between; padding: 3px 0; border-bottom: 1px solid rgba(108,99,255,0.07); }
.alfredo-test-preview .preview-key { color: #6c63ff; }
.alfredo-test-preview .preview-value { color: #e0e0e0; max-width: 180px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.alfredo-test-preview .preview-missing { color: #e74c3c; font-style: italic; }
.alfredo-test-preview .preview-ok { color: #2ecc71; }

.alfredo-empty { text-align: center; padding: 24px 16px; color: #a0a0b0; font-size: 13px; }
.alfredo-empty-icon { font-size: 32px; margin-bottom: 8px; opacity: 0.5; }
.alfredo-hidden { display: none !important; }
.alfredo-fade-in { animation: alfredo-slide-up 0.35s cubic-bezier(0.4,0,0.2,1) forwards; }

@keyframes alfredo-spin { to { transform: rotate(360deg); } }
@keyframes alfredo-pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.55; } }
@keyframes alfredo-highlight-pulse {
    0%,100% { box-shadow: 0 0 8px rgba(108,99,255,0.25); }
    50%     { box-shadow: 0 0 20px rgba(108,99,255,0.55); }
}
@keyframes alfredo-slide-up {
    from { opacity: 0; transform: translateY(20px); }
    to   { opacity: 1; transform: translateY(0); }
}
`;

    // ─── Private state ─────────────────────────────────────────────
    let _config = { serverUrl: null, apiKey: null };
    let _workflows = [];
    let _bindings = [];               // Saved bindings from localStorage
    let _widgetHost = null;            // The outer <div> element
    let _widgetRoot = null;            // Shadow DOM root
    let _isSetupMode = false;
    let _selectingFor = null;          // { type: 'input'|'output'|'trigger', inputName: string|null }
    let _currentBinding = {};          // Binding being configured
    let _highlightEl = null;           // The highlight overlay element (lives in document.body)
    let _overlayEl = null;             // Full-screen overlay element (lives in document.body)
    let _bannerEl = null;              // Overlay instruction banner
    let _activeListeners = [];         // Listeners attached during element picking
    let _activeTriggerListeners = [];  // Listeners attached during Run Mode
    let _toastTimer = null;
    let _selectedWorkflow = null;      // Currently selected workflow object

    // ─── Helpers ───────────────────────────────────────────────────

    /** Shorthand for querySelector inside shadow root */
    const $ = (sel) => _widgetRoot ? _widgetRoot.querySelector(sel) : null;
    const $$ = (sel) => _widgetRoot ? _widgetRoot.querySelectorAll(sel) : [];

    /** Unique storage key per API key */
    const _storageKey = () => `alfredo_bindings_${_config.apiKey || 'default'}`;

    // ─── Public API + Private methods ──────────────────────────────
    const api = {

        // ============================================================
        //  PUBLIC: init
        // ============================================================
        init({ serverUrl, apiKey, appName }) {
            if (!serverUrl) throw new Error('[AlfredoSDK] serverUrl is required');
            if (!apiKey) throw new Error('[AlfredoSDK] apiKey is required');
            if (!appName) throw new Error('[AlfredoSDK] appName is required');

            _config.serverUrl = serverUrl.replace(/\/+$/, '');
            _config.apiKey = apiKey;
            _config.appName = appName;

            // Create the widget
            api._createWidget();

            // Load saved bindings
            _bindings = api._loadBindings();

            if (_bindings.length > 0) {
                // Run Mode – activate bindings and minimise
                _isSetupMode = false;
                api._activateBindings();
                api.closeWidget();
            } else {
                // Setup Mode – show expanded
                _isSetupMode = true;
                api.openWidget();
            }

            // Fetch available workflows from server
            api._fetchWorkflows();
        },

        // ============================================================
        //  PUBLIC: getAvailableFlows
        // ============================================================
        getAvailableFlows() {
            return [..._workflows];
        },

        // ============================================================
        //  PUBLIC: trigger (programmatic)
        // ============================================================
        async trigger({ workflowId, inputData = {}, onStart, onResult, onError }) {
            try {
                if (onStart) onStart();
                const jobId = await api._triggerWorkflow(workflowId, inputData);
                await api._pollJobStatus(jobId, {
                    onProgress: null,
                    onComplete: (result) => { if (onResult) onResult(result); },
                    onError: (err) => { if (onError) onError(err); }
                });
            } catch (err) {
                if (onError) onError(err);
                else console.error('[AlfredoSDK] trigger error:', err);
            }
        },

        // ============================================================
        //  PUBLIC: openWidget / closeWidget / removeWidget
        // ============================================================
        openWidget() {
            const w = $( '.alfredo-widget');
            if (w) w.classList.remove('minimized');
        },

        closeWidget() {
            const w = $('.alfredo-widget');
            if (w) w.classList.add('minimized');
        },

        removeWidget() {
            // Clean up trigger listeners
            api._deactivateBindings();
            // Remove highlight / overlay if active
            api._stopPicking();
            // Remove host element
            if (_widgetHost && _widgetHost.parentNode) {
                _widgetHost.parentNode.removeChild(_widgetHost);
            }
            _widgetHost = null;
            _widgetRoot = null;
        },

        // ============================================================
        //  PRIVATE: Communication
        // ============================================================

        async _fetchWorkflows() {
            api._setStatus('loading', 'Fetching workflows…');
            try {
                const res = await fetch(`${_config.serverUrl}/api/apps/${_config.appName}/workflows`, {
                    headers: {
                        'X-API-Key': _config.apiKey,
                        'Accept': 'application/json'
                    }
                });
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const data = await res.json();
                _workflows = Array.isArray(data) ? data : (data.workflows || []);
                api._populateWorkflowDropdown();
                api._setStatus('connected', `${_workflows.length} workflow(s) loaded`);
            } catch (err) {
                console.error('[AlfredoSDK] Failed to fetch workflows:', err);
                api._setStatus('error', 'Connection failed');
                _workflows = [];
            }
        },

        async _triggerWorkflow(workflowId, inputData) {
            const res = await fetch(`${_config.serverUrl}/api/apps/${_config.appName}/trigger`, {
                method: 'POST',
                headers: {
                    'X-API-Key': _config.apiKey,
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    workflow_id: workflowId,
                    inputs: inputData
                })
            });
            if (!res.ok) {
                const text = await res.text().catch(() => '');
                throw new Error(`Trigger failed (HTTP ${res.status}): ${text}`);
            }
            const data = await res.json();
            return data.job_id || data.run_id || data.id;
        },

        async _pollJobStatus(jobId, callbacks) {
            const MAX_POLLS = 300;      // 300 × 2 s = 10 minutes
            const INTERVAL = 2000;

            for (let i = 0; i < MAX_POLLS; i++) {
                await new Promise((r) => setTimeout(r, INTERVAL));
                try {
                    const res = await fetch(`${_config.serverUrl}/api/jobs/${jobId}`, {
                        headers: {
                            'X-API-Key': _config.apiKey,
                            'Accept': 'application/json'
                        }
                    });
                    if (!res.ok) throw new Error(`HTTP ${res.status}`);
                    const data = await res.json();
                    const status = (data.status || '').toLowerCase();

                    if (status === 'running' || status === 'pending') {
                        if (callbacks.onProgress) callbacks.onProgress(data);
                        continue;
                    }
                    if (status === 'completed' || status === 'done') {
                        if (callbacks.onComplete) callbacks.onComplete(data.result || data);
                        return;
                    }
                    if (status === 'failed' || status === 'error') {
                        const errMsg = data.error || data.result || 'Workflow failed';
                        if (callbacks.onError) callbacks.onError(new Error(errMsg));
                        return;
                    }
                    // Unknown status – keep polling
                } catch (err) {
                    if (callbacks.onError) callbacks.onError(err);
                    return;
                }
            }
            // Timeout
            if (callbacks.onError) callbacks.onError(new Error('Polling timed out after 10 minutes'));
        },

        // ============================================================
        //  PRIVATE: Widget UI
        // ============================================================

        _createWidget() {
            // Prevent double-creation
            if (_widgetHost) return;

            _widgetHost = document.createElement('div');
            _widgetHost.id = 'alfredo-sdk-host';
            _widgetRoot = _widgetHost.attachShadow({ mode: 'open' });

            // Inject CSS
            const style = document.createElement('style');
            style.textContent = EMBEDDED_CSS;
            _widgetRoot.appendChild(style);

            // Build widget HTML
            const widget = document.createElement('div');
            widget.className = 'alfredo-widget';
            widget.innerHTML = `
                <!-- Mini logo (visible only when minimized) -->
                <div class="alfredo-mini-logo">
                    <img src="${_config.serverUrl}/sdk/logo.png" alt="Alfredo" style="width: 100%; height: 100%; border-radius: 50%; object-fit: cover; pointer-events: none;" />
                </div>

                <!-- Header -->
                <div class="alfredo-header">
                    <div class="alfredo-header-title">
                        <span class="alfredo-logo-char">A</span>
                        Alfredo
                    </div>
                    <div class="alfredo-header-controls">
                        <button class="alfredo-btn-minimize" title="Minimize">−</button>
                    </div>
                </div>

                <!-- Body -->
                <div class="alfredo-body">
                    <!-- Workflow selector -->
                    <div>
                        <div class="alfredo-section-label">Workflow</div>
                        <select class="alfredo-workflow-select">
                            <option value="">— Select a workflow —</option>
                        </select>
                    </div>

                    <!-- Input mappings (populated dynamically) -->
                    <div class="alfredo-input-section alfredo-hidden">
                        <div class="alfredo-section-label">Input Mappings</div>
                        <div class="alfredo-input-list"></div>
                    </div>

                    <!-- Output element -->
                    <div class="alfredo-output-section alfredo-hidden">
                        <div class="alfredo-section-title">Output Element</div>
                        <div class="alfredo-mapped-element alfredo-output-mapped">
                            <span>Not mapped</span>
                        </div>
                        <button class="alfredo-picker-btn alfredo-output-picker-btn">⊕ Pick Output</button>
                    </div>

                    <!-- Trigger element -->
                    <div class="alfredo-trigger-section alfredo-hidden">
                        <div class="alfredo-section-title">Trigger Element (optional)</div>
                        <div class="alfredo-mapped-element alfredo-trigger-mapped">
                            <span>Not mapped</span>
                        </div>
                        <button class="alfredo-picker-btn alfredo-trigger-picker-btn">⊕ Pick Trigger</button>
                    </div>

                    <!-- Validation summary -->
                    <div class="alfredo-validation alfredo-hidden"></div>

                    <!-- Test preview -->
                    <div class="alfredo-test-preview alfredo-hidden"></div>

                    <!-- Action buttons -->
                    <div class="alfredo-actions alfredo-hidden">
                        <button class="alfredo-btn-save" disabled>Save Binding</button>
                        <button class="alfredo-btn-test">Test</button>
                        <button class="alfredo-btn-clear" title="Clear all mappings">✕</button>
                    </div>

                    <!-- Empty state -->
                    <div class="alfredo-empty">
                        <div class="alfredo-empty-icon">📋</div>
                        Select a workflow to begin mapping elements.
                    </div>
                </div>

                <!-- Status bar -->
                <div class="alfredo-status">
                    <span class="alfredo-status-dot loading"></span>
                    <span class="alfredo-status-text">Connecting…</span>
                </div>
            `;

            _widgetRoot.appendChild(widget);
            document.body.appendChild(_widgetHost);

            // ── Wire up event listeners ─────────────────────────────
            // Minimize
            const miniBtn = _widgetRoot.querySelector('.alfredo-btn-minimize');
            miniBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                api.closeWidget();
            });

            // Close removed completely to prevent disappearing

            // Click on minimized bubble to expand
            widget.addEventListener('click', (e) => {
                if (widget.classList.contains('minimized')) {
                    e.stopPropagation();
                    api.openWidget();
                }
            });

            // Workflow select change
            const select = _widgetRoot.querySelector('.alfredo-workflow-select');
            select.addEventListener('change', (e) => {
                const id = e.target.value;
                const wf = _workflows.find((w) => String(w.id) === id);
                if (wf) {
                    _selectedWorkflow = wf;
                    _currentBinding = { workflowId: wf.id, workflowName: wf.name, inputs: {}, output: null, trigger: null };
                    api._renderWorkflowForm(wf);
                } else {
                    _selectedWorkflow = null;
                    _currentBinding = {};
                    api._hideFormSections();
                }
            });

            // Output picker button
            const outPickerBtn = _widgetRoot.querySelector('.alfredo-output-picker-btn');
            outPickerBtn.addEventListener('click', () => api._startPicking('output', null));

            // Trigger picker button
            const trigPickerBtn = _widgetRoot.querySelector('.alfredo-trigger-picker-btn');
            trigPickerBtn.addEventListener('click', () => api._startPicking('trigger', null));

            // Save button
            const saveBtn = _widgetRoot.querySelector('.alfredo-btn-save');
            saveBtn.addEventListener('click', () => api._saveBindings());

            // Test button
            const testBtn = _widgetRoot.querySelector('.alfredo-btn-test');
            testBtn.addEventListener('click', () => api._testBinding());

            // Clear button
            const clearBtn = _widgetRoot.querySelector('.alfredo-btn-clear');
            clearBtn.addEventListener('click', () => {
                _currentBinding = _selectedWorkflow
                    ? { workflowId: _selectedWorkflow.id, workflowName: _selectedWorkflow.name, inputs: {}, output: null, trigger: null }
                    : {};
                if (_selectedWorkflow) api._renderWorkflowForm(_selectedWorkflow);
            });

            // Make header draggable
            api._makeDraggable(_widgetRoot.querySelector('.alfredo-header'));
        },

        /** Populate the workflow dropdown from _workflows */
        _populateWorkflowDropdown() {
            const select = _widgetRoot ? _widgetRoot.querySelector('.alfredo-workflow-select') : null;
            if (!select) return;

            // Keep the first placeholder option, remove the rest
            while (select.options.length > 1) select.remove(1);

            _workflows.forEach((wf) => {
                const opt = document.createElement('option');
                opt.value = wf.id;
                opt.textContent = wf.name || `Workflow #${wf.id}`;
                select.appendChild(opt);
            });
        },

        /** Hide all form sections (used when no workflow is selected) */
        _hideFormSections() {
            ['.alfredo-input-section', '.alfredo-output-section', '.alfredo-trigger-section',
             '.alfredo-validation', '.alfredo-actions', '.alfredo-test-preview'].forEach((sel) => {
                const el = $(sel);
                if (el) el.classList.add('alfredo-hidden');
            });
            const empty = $('.alfredo-empty');
            if (empty) empty.classList.remove('alfredo-hidden');
        },

        // ============================================================
        //  PRIVATE: Render workflow form
        // ============================================================

        _renderWorkflowForm(workflow) {
            // Hide empty state
            const empty = $('.alfredo-empty');
            if (empty) empty.classList.add('alfredo-hidden');

            // --- Input mappings ---
            const inputSection = $('.alfredo-input-section');
            const inputList = $('.alfredo-input-list');
            if (inputSection && inputList) {
                inputList.innerHTML = '';

                // Gather required inputs – they live on the workflow's tasks
                const requiredInputs = api._getRequiredInputs(workflow);

                if (requiredInputs.length > 0) {
                    inputSection.classList.remove('alfredo-hidden');
                    requiredInputs.forEach((inp) => {
                        const name = inp.key || inp.name || inp;
                        const item = document.createElement('div');
                        const mapped = _currentBinding.inputs && _currentBinding.inputs[name];
                        item.className = `alfredo-input-item ${mapped ? 'mapped' : 'unmapped'}`;
                        item.dataset.inputName = name;

                        item.innerHTML = `
                            <span class="alfredo-input-item-label" title="${name}">${name}</span>
                            <span class="alfredo-input-item-selector">${mapped ? mapped.selector : ''}</span>
                            <button class="alfredo-picker-btn">⊕ Pick</button>
                        `;

                        // Wire picker button
                        item.querySelector('.alfredo-picker-btn').addEventListener('click', () => {
                            api._startPicking('input', name);
                        });

                        inputList.appendChild(item);
                    });
                } else {
                    inputSection.classList.add('alfredo-hidden');
                }
            }

            // --- Show output / trigger / actions sections ---
            ['.alfredo-output-section', '.alfredo-trigger-section', '.alfredo-actions'].forEach((sel) => {
                const el = $(sel);
                if (el) el.classList.remove('alfredo-hidden');
            });

            // Refresh output / trigger display
            api._refreshMappedDisplay();

            // Validate
            api._validateBinding();
        },

        /** Extract required inputs from a workflow definition */
        _getRequiredInputs(workflow) {
            // The server may return required_inputs at workflow level …
            if (Array.isArray(workflow.required_inputs) && workflow.required_inputs.length) {
                return workflow.required_inputs;
            }
            // … or they may be nested per-task
            if (Array.isArray(workflow.tasks)) {
                const inputs = [];
                const seen = new Set();
                workflow.tasks.forEach((t) => {
                    (t.required_inputs || []).forEach((ri) => {
                        const key = ri.key || ri.name || ri;
                        if (!seen.has(key)) {
                            seen.add(key);
                            inputs.push(ri);
                        }
                    });
                });
                return inputs;
            }
            return [];
        },

        /** Refresh the output / trigger mapped display elements */
        _refreshMappedDisplay() {
            const outMapped = $('.alfredo-output-mapped');
            if (outMapped) {
                if (_currentBinding.output) {
                    outMapped.innerHTML = `
                        <span class="alfredo-selector-tag" title="${_currentBinding.output.selector}">${_currentBinding.output.selector}</span>
                        <span style="color:#a0a0b0;font-size:11px;">(${_currentBinding.output.elementType})</span>
                    `;
                } else {
                    outMapped.innerHTML = '<span>Not mapped</span>';
                }
            }

            const trigMapped = $('.alfredo-trigger-mapped');
            if (trigMapped) {
                if (_currentBinding.trigger) {
                    trigMapped.innerHTML = `
                        <span class="alfredo-selector-tag" title="${_currentBinding.trigger.selector}">${_currentBinding.trigger.selector}</span>
                        <span style="color:#a0a0b0;font-size:11px;">(${_currentBinding.trigger.elementType})</span>
                    `;
                } else {
                    trigMapped.innerHTML = '<span>Not mapped</span>';
                }
            }
        },

        // ============================================================
        //  PRIVATE: Draggable
        // ============================================================

        _makeDraggable(handle) {
            if (!handle) return;
            let isDragging = false;
            let offsetX = 0;
            let offsetY = 0;

            const widget = handle.closest('.alfredo-widget');

            handle.addEventListener('mousedown', (e) => {
                if (e.target.tagName === 'BUTTON') return; // Don't drag when clicking buttons
                isDragging = true;
                const rect = widget.getBoundingClientRect();
                offsetX = e.clientX - rect.left;
                offsetY = e.clientY - rect.top;
                widget.style.transition = 'none';
                e.preventDefault();
            });

            document.addEventListener('mousemove', (e) => {
                if (!isDragging) return;
                const x = e.clientX - offsetX;
                const y = e.clientY - offsetY;
                widget.style.left = `${x}px`;
                widget.style.top = `${y}px`;
                widget.style.right = 'auto';
                widget.style.bottom = 'auto';
            });

            document.addEventListener('mouseup', () => {
                if (isDragging) {
                    isDragging = false;
                    widget.style.transition = '';
                }
            });
        },

        // ============================================================
        //  PRIVATE: Element Picker
        // ============================================================

        _startPicking(forType, inputName) {
            // Clean up any previous pick session
            api._stopPicking();

            _selectingFor = { type: forType, inputName };

            // Create overlay (in document, NOT in shadow DOM, so it covers everything)
            _overlayEl = document.createElement('div');
            _overlayEl.className = 'alfredo-overlay';
            // Apply inline styles because it lives outside the shadow root
            Object.assign(_overlayEl.style, {
                position: 'fixed', top: '0', left: '0', width: '100vw', height: '100vh',
                background: 'rgba(0,0,0,0.3)', zIndex: '2147483645', cursor: 'crosshair'
            });

            // Create banner
            _bannerEl = document.createElement('div');
            _bannerEl.className = 'alfredo-overlay-banner';
            const typeLabel = forType === 'input' ? `input "${inputName}"` : forType;
            _bannerEl.innerHTML = `Click an element to map as <b>${typeLabel}</b> &nbsp;|&nbsp; Press <kbd>Esc</kbd> to cancel`;
            Object.assign(_bannerEl.style, {
                position: 'fixed', top: '16px', left: '50%', transform: 'translateX(-50%)',
                background: '#1a1a2e', color: '#e0e0e0', padding: '10px 24px', borderRadius: '8px',
                fontSize: '13px', fontWeight: '600', boxShadow: '0 4px 24px rgba(0,0,0,0.5)',
                zIndex: '2147483647', pointerEvents: 'none', display: 'flex',
                alignItems: 'center', gap: '8px',
                fontFamily: "system-ui, -apple-system, sans-serif"
            });

            // Create highlight element
            _highlightEl = document.createElement('div');
            _highlightEl.className = `alfredo-highlight ${forType}-pick`;
            const borderColors = { input: '#6c63ff', output: '#2ecc71', trigger: '#f39c12' };
            const bc = borderColors[forType] || '#6c63ff';
            Object.assign(_highlightEl.style, {
                position: 'absolute', pointerEvents: 'none',
                border: `2px dashed ${bc}`, borderRadius: '4px',
                background: `${bc}20`, boxShadow: `0 0 12px ${bc}60`,
                zIndex: '2147483647', transition: 'all 0.1s ease',
                display: 'none'
            });

            document.body.appendChild(_overlayEl);
            document.body.appendChild(_bannerEl);
            document.body.appendChild(_highlightEl);

            // ── Mouse move handler ──────────────────────────────────
            const onMouseMove = (e) => {
                const target = document.elementFromPoint(e.clientX, e.clientY);
                if (!target || target === _overlayEl || target === _bannerEl || target === _highlightEl) {
                    _highlightEl.style.display = 'none';
                    return;
                }
                // Ignore the widget itself
                if (_widgetHost && (_widgetHost === target || _widgetHost.contains(target))) {
                    _highlightEl.style.display = 'none';
                    return;
                }
                api._highlightElement(target);
            };

            // ── Click handler ───────────────────────────────────────
            const onClick = (e) => {
                e.preventDefault();
                e.stopPropagation();

                // Temporarily hide overlay so elementFromPoint can find the real element
                _overlayEl.style.pointerEvents = 'none';
                const target = document.elementFromPoint(e.clientX, e.clientY);
                _overlayEl.style.pointerEvents = '';

                if (!target || _widgetHost === target || (_widgetHost && _widgetHost.contains(target))) {
                    return; // Ignore clicks on the widget
                }

                const selector = api._generateSelector(target);
                const tagName = target.tagName.toLowerCase();

                if (_selectingFor.type === 'input') {
                    _currentBinding.inputs = _currentBinding.inputs || {};
                    _currentBinding.inputs[_selectingFor.inputName] = {
                        selector,
                        readMethod: api._determineReadMethod(target),
                        elementType: tagName
                    };
                } else if (_selectingFor.type === 'output') {
                    _currentBinding.output = {
                        selector,
                        writeMethod: api._determineWriteMethod(target),
                        elementType: tagName
                    };
                } else if (_selectingFor.type === 'trigger') {
                    _currentBinding.trigger = {
                        selector,
                        event: 'click',
                        elementType: tagName
                    };
                }

                api._stopPicking();

                // Update widget UI
                if (_selectedWorkflow) {
                    api._renderWorkflowForm(_selectedWorkflow);
                }
                api._showToast(`Mapped ${_selectingFor?.type || 'element'}: ${selector}`, 'info');
            };

            // ── Key handler (Escape) ────────────────────────────────
            const onKeyDown = (e) => {
                if (e.key === 'Escape') {
                    api._stopPicking();
                    api._showToast('Picking cancelled', 'warning');
                }
            };

            // The overlay intercepts mouse events
            _overlayEl.addEventListener('mousemove', onMouseMove);
            _overlayEl.addEventListener('click', onClick);
            document.addEventListener('keydown', onKeyDown);

            _activeListeners = [
                { el: _overlayEl, type: 'mousemove', fn: onMouseMove },
                { el: _overlayEl, type: 'click', fn: onClick },
                { el: document, type: 'keydown', fn: onKeyDown }
            ];
        },

        _stopPicking() {
            // Remove listeners
            _activeListeners.forEach(({ el, type, fn }) => el.removeEventListener(type, fn));
            _activeListeners = [];

            // Remove DOM elements
            if (_overlayEl && _overlayEl.parentNode) _overlayEl.parentNode.removeChild(_overlayEl);
            if (_bannerEl && _bannerEl.parentNode) _bannerEl.parentNode.removeChild(_bannerEl);
            if (_highlightEl && _highlightEl.parentNode) _highlightEl.parentNode.removeChild(_highlightEl);
            _overlayEl = null;
            _bannerEl = null;
            _highlightEl = null;
            _selectingFor = null;
        },

        _generateSelector(element) {
            // Priority: #id > [data-alfredo] > .unique-class > tag:nth-child(n)
            if (!element || element === document.body || element === document.documentElement) {
                return 'body';
            }

            // 1. ID (must be unique)
            if (element.id) {
                const sel = `#${CSS.escape(element.id)}`;
                if (document.querySelectorAll(sel).length === 1) return sel;
            }

            // 2. data-alfredo attribute
            if (element.dataset && element.dataset.alfredo) {
                const sel = `[data-alfredo="${CSS.escape(element.dataset.alfredo)}"]`;
                if (document.querySelectorAll(sel).length === 1) return sel;
            }

            // 3. Unique class combination
            if (element.classList && element.classList.length > 0) {
                const classes = Array.from(element.classList)
                    .filter((c) => !c.startsWith('alfredo-'))
                    .map((c) => `.${CSS.escape(c)}`);
                if (classes.length > 0) {
                    const sel = element.tagName.toLowerCase() + classes.join('');
                    if (document.querySelectorAll(sel).length === 1) return sel;
                }
            }

            // 4. Build path with nth-child
            const path = [];
            let current = element;
            while (current && current !== document.body && current !== document.documentElement) {
                let segment = current.tagName.toLowerCase();

                if (current.id) {
                    segment = `#${CSS.escape(current.id)}`;
                    path.unshift(segment);
                    break;
                }

                // Compute nth-child position
                const parent = current.parentElement;
                if (parent) {
                    const siblings = Array.from(parent.children).filter(
                        (s) => s.tagName === current.tagName
                    );
                    if (siblings.length > 1) {
                        const idx = siblings.indexOf(current) + 1;
                        segment += `:nth-child(${idx})`;
                    }
                }

                path.unshift(segment);
                current = current.parentElement;

                // Stop if path is getting long
                if (path.length > 5) break;
            }

            return path.join(' > ');
        },

        _highlightElement(element) {
            if (!_highlightEl || !element) return;
            const rect = element.getBoundingClientRect();
            Object.assign(_highlightEl.style, {
                display: 'block',
                left: `${rect.left + window.scrollX - 2}px`,
                top: `${rect.top + window.scrollY - 2}px`,
                width: `${rect.width + 4}px`,
                height: `${rect.height + 4}px`
            });
        },

        _determineReadMethod(element) {
            const tag = element.tagName.toLowerCase();
            if (tag === 'input' || tag === 'textarea' || tag === 'select') return 'value';
            if (tag === 'img') return 'src';
            return 'textContent';
        },

        _determineWriteMethod(element) {
            const tag = element.tagName.toLowerCase();
            if (tag === 'input' || tag === 'textarea') return 'value';
            return 'innerHTML';
        },

        // ============================================================
        //  PRIVATE: Binding Management
        // ============================================================

        _validateBinding() {
            const validationEl = $('.alfredo-validation');
            const saveBtn = $('.alfredo-btn-save');
            if (!validationEl) return { valid: false, errors: [], warnings: [] };

            const errors = [];
            const warnings = [];

            if (!_selectedWorkflow) {
                errors.push('No workflow selected');
            } else {
                // Check all required inputs are mapped
                const requiredInputs = api._getRequiredInputs(_selectedWorkflow);
                requiredInputs.forEach((inp) => {
                    const name = inp.key || inp.name || inp;
                    if (!_currentBinding.inputs || !_currentBinding.inputs[name]) {
                        errors.push(`Input "${name}" is not mapped`);
                    }
                });

                // Check output
                if (_currentBinding.output) {
                    const outTag = _currentBinding.output.elementType;
                    const writableTags = ['div', 'p', 'span', 'textarea', 'pre', 'section', 'article', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'td', 'th'];
                    if (!writableTags.includes(outTag)) {
                        warnings.push(`Output element <${outTag}> may not be ideal for writing results`);
                    }
                }

                // Check trigger
                if (_currentBinding.trigger) {
                    const trigTag = _currentBinding.trigger.elementType;
                    const clickableTags = ['button', 'a', 'input'];
                    if (!clickableTags.includes(trigTag)) {
                        warnings.push(`Trigger element <${trigTag}> may not be naturally clickable`);
                    }
                }
            }

            const valid = errors.length === 0;

            // Update UI
            validationEl.classList.remove('alfredo-hidden', 'valid', 'invalid');
            validationEl.classList.add(valid ? 'valid' : 'invalid');

            let html = valid
                ? '✓ All inputs mapped — ready to save'
                : '<strong>Validation Issues:</strong>';

            if (errors.length > 0 || warnings.length > 0) {
                html += '<ul>';
                errors.forEach((e) => { html += `<li>${e}</li>`; });
                warnings.forEach((w) => { html += `<li class="alfredo-warning-item">⚠ ${w}</li>`; });
                html += '</ul>';
            }

            validationEl.innerHTML = html;

            // Enable/disable save
            if (saveBtn) saveBtn.disabled = !valid;

            return { valid, errors, warnings };
        },

        _saveBindings() {
            const { valid } = api._validateBinding();
            if (!valid) {
                api._showToast('Fix validation errors before saving', 'error');
                return;
            }

            // Add to bindings list (replace if same workflow)
            const idx = _bindings.findIndex((b) => b.workflowId === _currentBinding.workflowId);
            if (idx >= 0) {
                _bindings[idx] = { ..._currentBinding };
            } else {
                _bindings.push({ ..._currentBinding });
            }

            try {
                localStorage.setItem(_storageKey(), JSON.stringify(_bindings));
            } catch (err) {
                console.error('[AlfredoSDK] Failed to save bindings:', err);
                api._showToast('Failed to save bindings to localStorage', 'error');
                return;
            }

            api._showToast('Binding saved successfully!', 'success');
            api._activateBindings();
        },

        _loadBindings() {
            try {
                const raw = localStorage.getItem(_storageKey());
                if (raw) return JSON.parse(raw);
            } catch (err) {
                console.warn('[AlfredoSDK] Could not load bindings:', err);
            }
            return [];
        },

        _activateBindings() {
            // Deactivate previous listeners first
            api._deactivateBindings();

            _bindings.forEach((binding) => {
                if (!binding.trigger || !binding.trigger.selector) return;

                const triggerEl = document.querySelector(binding.trigger.selector);
                if (!triggerEl) {
                    console.warn(`[AlfredoSDK] Trigger element not found: ${binding.trigger.selector}`);
                    return;
                }

                const handler = async (e) => {
                    // Don't prevent default for links etc – just run the workflow alongside
                    e.stopPropagation();

                    // 1. Read all input values
                    const inputData = {};
                    let readError = false;
                    for (const [name, mapping] of Object.entries(binding.inputs || {})) {
                        const val = api._readValueFromElement(mapping.selector, mapping.readMethod);
                        if (val === null) {
                            api._showToast(`Could not read input "${name}" — element missing`, 'error');
                            readError = true;
                            break;
                        }
                        inputData[name] = val;
                    }
                    if (readError) return;

                    // 2. Show spinner on trigger element
                    const originalContent = triggerEl.innerHTML;
                    const spinner = document.createElement('span');
                    spinner.className = 'alfredo-spinner-inline';
                    spinner.style.cssText = 'display:inline-block;width:14px;height:14px;border:2px solid rgba(108,99,255,0.2);border-top-color:#6c63ff;border-radius:50%;animation:alfredo-sdk-spin 0.7s linear infinite;margin-left:6px;vertical-align:middle;';
                    triggerEl.appendChild(spinner);
                    triggerEl.disabled = true;

                    // Inject the spin animation into document if not present
                    if (!document.getElementById('alfredo-sdk-spin-style')) {
                        const s = document.createElement('style');
                        s.id = 'alfredo-sdk-spin-style';
                        s.textContent = '@keyframes alfredo-sdk-spin { to { transform: rotate(360deg); } }';
                        document.head.appendChild(s);
                    }

                    api._setStatus('loading', 'Running workflow…');

                    try {
                        // 3. Trigger workflow
                        const jobId = await api._triggerWorkflow(binding.workflowId, inputData);

                        // 4. Poll for results
                        await api._pollJobStatus(jobId, {
                            onProgress: () => {
                                api._setStatus('loading', 'Workflow running…');
                            },
                            onComplete: (result) => {
                                // 5. Write result to output element
                                if (binding.output && binding.output.selector) {
                                    const resultText = typeof result === 'string' ? result : JSON.stringify(result, null, 2);
                                    api._writeValueToElement(binding.output.selector, binding.output.writeMethod, resultText);
                                }
                                api._showToast('Workflow completed!', 'success');
                                api._setStatus('connected', 'Completed');
                            },
                            onError: (err) => {
                                api._showToast(`Workflow failed: ${err.message}`, 'error');
                                api._setStatus('error', 'Failed');
                            }
                        });
                    } catch (err) {
                        api._showToast(`Error: ${err.message}`, 'error');
                        api._setStatus('error', 'Error');
                    } finally {
                        // Restore trigger element
                        triggerEl.innerHTML = originalContent;
                        triggerEl.disabled = false;
                    }
                };

                triggerEl.addEventListener('click', handler);
                _activeTriggerListeners.push({ el: triggerEl, fn: handler });
            });
        },

        _deactivateBindings() {
            _activeTriggerListeners.forEach(({ el, fn }) => {
                el.removeEventListener('click', fn);
            });
            _activeTriggerListeners = [];
        },

        _testBinding() {
            const previewEl = $('.alfredo-test-preview');
            if (!previewEl) return;

            previewEl.classList.remove('alfredo-hidden');

            // Read values from all mapped inputs
            const requiredInputs = _selectedWorkflow ? api._getRequiredInputs(_selectedWorkflow) : [];
            let html = '';
            let allOk = true;

            requiredInputs.forEach((inp) => {
                const name = inp.key || inp.name || inp;
                const mapping = _currentBinding.inputs && _currentBinding.inputs[name];

                if (!mapping) {
                    html += `<div class="preview-row"><span class="preview-key">${name}</span><span class="preview-missing">Not mapped</span></div>`;
                    allOk = false;
                    return;
                }

                // Check selector still exists
                const el = document.querySelector(mapping.selector);
                if (!el) {
                    html += `<div class="preview-row"><span class="preview-key">${name}</span><span class="preview-missing">Element not found: ${mapping.selector}</span></div>`;
                    allOk = false;
                    return;
                }

                const val = api._readValueFromElement(mapping.selector, mapping.readMethod);
                html += `<div class="preview-row"><span class="preview-key">${name}</span><span class="preview-value preview-ok">${val !== null && val !== '' ? val : '<em>empty</em>'}</span></div>`;
            });

            // Check output element
            if (_currentBinding.output) {
                const outEl = document.querySelector(_currentBinding.output.selector);
                html += `<div class="preview-row"><span class="preview-key">output</span><span class="${outEl ? 'preview-ok' : 'preview-missing'}">${outEl ? _currentBinding.output.selector : 'Element not found'}</span></div>`;
                if (!outEl) allOk = false;
            }

            // Check trigger element
            if (_currentBinding.trigger) {
                const trigEl = document.querySelector(_currentBinding.trigger.selector);
                html += `<div class="preview-row"><span class="preview-key">trigger</span><span class="${trigEl ? 'preview-ok' : 'preview-missing'}">${trigEl ? _currentBinding.trigger.selector : 'Element not found'}</span></div>`;
                if (!trigEl) allOk = false;
            }

            previewEl.innerHTML = html || '<span class="preview-missing">No mappings to test</span>';

            api._showToast(
                allOk ? 'All selectors verified ✓' : 'Some elements could not be found',
                allOk ? 'success' : 'warning'
            );
        },

        // ============================================================
        //  PRIVATE: Utilities
        // ============================================================

        _readValueFromElement(selector, readMethod) {
            const el = document.querySelector(selector);
            if (!el) return null;
            switch (readMethod) {
                case 'value':       return el.value;
                case 'src':         return el.src;
                case 'textContent': return el.textContent;
                default:            return el.textContent;
            }
        },

        _writeValueToElement(selector, writeMethod, value) {
            const el = document.querySelector(selector);
            if (!el) return;
            switch (writeMethod) {
                case 'value':
                    el.value = value;
                    // Fire input event so frameworks pick up the change
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    break;
                case 'innerHTML':
                    el.innerHTML = value;
                    break;
                default:
                    el.textContent = value;
            }
        },

        _setStatus(state, text) {
            const dot = $('.alfredo-status-dot');
            const txt = $('.alfredo-status-text');
            if (dot) {
                dot.className = 'alfredo-status-dot';
                dot.classList.add(state); // 'connected' | 'loading' | 'error'
            }
            if (txt) txt.textContent = text || '';
        },

        _showToast(message, type = 'info') {
            // Remove existing toast in shadow DOM
            const existing = $$('.alfredo-toast');
            existing.forEach((t) => t.remove());

            if (_toastTimer) clearTimeout(_toastTimer);

            const toast = document.createElement('div');
            toast.className = `alfredo-toast ${type}`;
            toast.textContent = message;
            // We place the toast inside the shadow DOM so it uses our styles
            if (_widgetRoot) {
                _widgetRoot.appendChild(toast);
            }

            // Trigger show animation on next frame
            requestAnimationFrame(() => {
                toast.classList.add('show');
            });

            _toastTimer = setTimeout(() => {
                toast.classList.remove('show');
                setTimeout(() => toast.remove(), 350);
            }, 3500);
        }
    };

    return api;
})();

// ─── Expose globally ───────────────────────────────────────────
if (typeof window !== 'undefined') {
    window.AlfredoClient = AlfredoClient;
}
