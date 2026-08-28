// Main chat app: WebSocket bridge to the agent subprocess.
// Server frames: { type: "ready" | "assistant" | "user" | "system" | "result" | "tool_result" | "tool_use" | ... }
// Client frames: { type: "user", text } | { type: "interrupt" } | { type: "ping" }

function chatApp() {
  return {
    currentId: null,
    title: '',
    model: '',
    workspace: '',
    messages: [],
    input: '',
    busy: false,
    connected: false,
    files: [],
    ws: null,
    // Picker state
    showPicker: false,
    pickerProviders: {},
    pickerProvider: 'openrouter',
    pickerModel: '',
    // Last-turn usage
    lastUsage: null,
    // Context-window fill (from /v1/sessions/{id}/context)
    contextPct: 0,
    // Dataset upload state
    uploading: false,
    uploadedPath: '',
    // track per-message streaming text so the assistant block keeps updating
    _activeAssistantIdx: -1,
    _activeToolIdx: -1,

    get canSend() { return this.currentId && this.connected && !this.busy && this.input.trim().length > 0; },

    init() {
      // Restore the last-open session across tab refreshes:
      // 1. URL hash (#<sid>) — survives reloads and is shareable
      // 2. sessionStorage fallback
      // 3. first session in the sidebar
      const hashId = (location.hash || '').replace('#', '');
      const savedId = sessionStorage.getItem('ca.session');
      const first = document.querySelector('.session-item');
      const target = [hashId, savedId, first && first.dataset.id].find(Boolean);
      if (target) this.open(target);
    },

    _remember(id) {
      try { sessionStorage.setItem('ca.session', id); } catch {}
      if (location.hash !== '#' + id) history.replaceState(null, '', '#' + id);
    },

    async newSession() {
      // Open a proper picker (fetched from /v1/models)
      this.pickerProvider = 'openrouter';
      this.pickerModel = '';
      this.showPicker = true;
      try {
        const r = await fetch('/v1/models');
        const data = await r.json();
        this.pickerProviders = data.providers || {};
        // Default the model picker to the first entry of the selected provider
        const first = (this.pickerProviders.openrouter || [])[0];
        if (first) this.pickerModel = first.id;
      } catch (e) {
        this.pickerProviders = { openrouter: [{ id: 'anthropic/claude-sonnet-4-5', label: 'Claude Sonnet 4.5', tag: 'default', ctx: 1000000 }] };
        this.pickerModel = 'anthropic/claude-sonnet-4-5';
      }
    },

    async pickerConfirm() {
      const provider = this.pickerProvider;
      const model = (this.pickerModel || '').trim();
      if (!model) { alert('pick a model'); return; }
      const r = await fetch('/v1/sessions', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ provider, model }),
      });
      if (!r.ok) { alert(await r.text()); return; }
      this.showPicker = false;
      const sid = (await r.json()).id;
      this._remember(sid);   // hash + sessionStorage so the reload lands on it
      location.reload();
    },

    pickerCancel() { this.showPicker = false; },

    open(id) {
      if (this.currentId === id) return;
      this.currentId = id;
      this.messages = [];
      this._activeAssistantIdx = -1;
      this._activeToolIdx = -1;
      this._remember(id);
      this._connect(id);
      this._loadHistory(id);
      this._loadFiles(id);
      this._refreshContext();
    },

    // Rebuild the chat from the on-disk transcript so a tab refresh keeps
    // the full history (including artifacts) instead of starting blank.
    async _loadHistory(id) {
      try {
        const r = await fetch(`/v1/sessions/${id}/history`);
        if (!r.ok || this.currentId !== id) return;  // user switched sessions meanwhile
        const d = await r.json();
        for (const m of (d.messages || [])) {
          if (m.role === 'user') {
            this.messages.push({ role: 'user', html: this._renderMd(m.content) });
          } else if (m.role === 'assistant') {
            this.messages.push({ role: 'assistant', html: expandArtifacts(this._renderMd(m.content)) });
          } else if (m.role === 'thinking') {
            this.messages.push({ role: 'assistant', html: this._renderThinking(m.content), thinking: true });
          } else if (m.role === 'tool') {
            this.messages.push({ role: 'tool', html: this._renderToolUse(m.content) });
          } else if (m.role === 'tool-result') {
            this.messages.push({ role: 'tool-result', html: this._renderToolResult({ content: m.content }) });
          }
        }
        if (d.last_usage) this.lastUsage = d.last_usage;
        this._scrollDown();
        this._enhanceCodeBlocks();
      } catch (e) { /* history is best-effort */ }
    },

    async _refreshContext() {
      if (!this.currentId) return;
      try {
        const r = await fetch(`/v1/sessions/${this.currentId}/context`);
        const d = await r.json();
        this.contextPct = Math.round(d.percentage || 0);
      } catch (e) { this.contextPct = 0; }
    },

    async compact() {
      if (!this.currentId) return;
      await fetch(`/v1/sessions/${this.currentId}/compact`, { method: 'POST' });
      this._refreshContext();
    },

    deleteCurrent() {
      if (!this.currentId) return;
      if (!confirm('delete this session and its workspace?')) return;
      try { sessionStorage.removeItem('ca.session'); } catch {}
      history.replaceState(null, '', location.pathname);
      fetch(`/v1/sessions/${this.currentId}`, { method: 'DELETE' }).then(() => location.reload());
    },

    _connect(id) {
      if (this.ws) try { this.ws.close(); } catch {}
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      this.ws = new WebSocket(`${proto}://${location.host}/ws/sessions/${id}`);
      this.ws.onopen = () => { this.connected = true; this.busy = false; };
      this.ws.onclose = () => { this.connected = false; };
      this.ws.onerror = () => { this.connected = false; };
      this.ws.onmessage = (ev) => this._onFrame(JSON.parse(ev.data));
    },

    async _loadFiles(id) {
      this.files = await fetch(`/v1/sessions/${id}/files`).then(r => r.json());
    },

    _onFrame(f) {
      if (f.type === 'ready') {
        this.title = f.title; this.model = f.model; this.workspace = f.workspace;
        return;
      }
      if (f.type === 'pong') return;

      // System messages: include init (session id) but ignore most
      if (f.type === 'system') {
        const sub = f.subtype || '';
        if (sub === 'init') { this.title = f.title || this.title; }
        if (sub === 'watchdog' && f.message) {
          this._appendSystem(`⚠ ${f.message}`);
        }
        return;
      }

      // Error frames: clear busy state so the UI doesn't stay stuck on "working…"
      if (f.type === 'error' || f.type === 'reader_error') {
        this.busy = false;
        this._activeAssistantIdx = -1;
        this._activeToolIdx = -1;
        this._appendSystem(`⚠ ${f.message || 'unknown error'}`);
        return;
      }

      // Assistant text / tool calls
      if (f.type === 'assistant') {
        const blocks = f.content || (f.message && f.message.content) || [];
        for (const b of blocks) {
          if (b.type === 'text' && b.text) {
            this._appendAssistantText(b.text);
          } else if (b.type === 'thinking' && b.thinking) {
            this._appendThinkingBlock(b.thinking);
          } else if (b.type === 'tool_use') {
            this._newToolBlock(b);
          }
        }
        return;
      }

      // User message from server = a tool result coming back
      if (f.type === 'user') {
        const content = f.content || (f.message && f.message.content) || [];
        for (const c of content) {
          if (c.type === 'tool_result') {
            this._appendToolResult(c);
          }
        }
        return;
      }

      // Result = end of turn
      if (f.type === 'result') {
        this.busy = false;
        this._activeAssistantIdx = -1;
        this._activeToolIdx = -1;
        const u = f.usage || {};
        const inT = u.input_tokens || 0;
        const outT = u.output_tokens || 0;
        const cr = u.cache_read_tokens || 0;
        const cc = u.cache_creation_tokens || 0;
        const cacheHit = u.cache_hit_pct != null ? ` · cache ${u.cache_hit_pct}%` : '';
        const cost = f.total_cost_usd != null ? `$${f.total_cost_usd.toFixed(4)}` : '';
        this.lastUsage = u;
        this._appendSystem(
          `done — ${cost} · ${inT.toLocaleString()} in / ${outT.toLocaleString()} out` +
          ` · ${cr.toLocaleString()} cached read${cacheHit}` +
          (cc ? ` · ${cc.toLocaleString()} cache write` : '')
        );
        this._loadFiles(this.currentId);  // refresh file list
        this._refreshContext();           // refresh context-window fill
        return;
      }

      // Anything else: drop into a debug fold
      this._appendSystem(JSON.stringify(f).slice(0, 400));
    },

    _appendAssistantText(text) {
      if (this._activeAssistantIdx < 0) {
        this.messages.push({ role: 'assistant', html: '', _raw: '' });
        this._activeAssistantIdx = this.messages.length - 1;
      }
      const m = this.messages[this._activeAssistantIdx];
      m._raw = (m._raw || '') + text;
      m.html = expandArtifacts(this._renderMd(m._raw));
      this._scrollDown();
      this._enhanceCodeBlocks();
    },

    _appendThinkingBlock(thinkingText) {
      this._activeAssistantIdx = -1;
      this.messages.push({ role: 'assistant', html: this._renderThinking(thinkingText), thinking: true });
      this._scrollDown();
      this._enhanceCodeBlocks();
    },

    _renderThinking(text) {
      const rendered = this._renderMd(text);
      return `<details class="thinking-block">
        <summary class="tool-summary">
          <div class="tool-head">
            <span class="badge badge-thinking">thinking</span>
            <span class="muted small">internal reasoning</span>
          </div>
          <span class="muted small">click to toggle</span>
        </summary>
        <div class="tool-content">
          ${rendered}
        </div>
      </details>`;
    },

    _newToolBlock(b) {
      // Crucial: reset assistant index so subsequent text starts a new message block below this tool
      this._activeAssistantIdx = -1;
      this.messages.push({ role: 'tool', html: this._renderToolUse(b) });
      this._activeToolIdx = this.messages.length - 1;
      this._scrollDown();
      this._enhanceCodeBlocks();
    },

    _appendToolResult(c) {
      // Crucial: reset assistant index so subsequent text starts a new message block below this tool result
      this._activeAssistantIdx = -1;
      const html = this._renderToolResult(c);
      this.messages.push({ role: 'tool-result', html });
      this._scrollDown();
      this._enhanceCodeBlocks();
    },

    _appendSystem(text) {
      this.messages.push({ role: 'system', html: `<span class="muted small">${escapeHtml(text)}</span>` });
      this._scrollDown();
    },

    _renderMd(text) {
      if (!window.marked) return escapeHtml(text);
      try {
        const dirty = marked.parse(text, { breaks: true, gfm: true });
        return DOMPurify.sanitize(dirty, {
          ADD_TAGS: ['img', 'svg', 'path', 'button', 'details', 'summary', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'span', 'div', 'code', 'pre'],
          ADD_ATTR: ['src', 'alt', 'class', 'target', 'href', 'download', 'title', 'rel',
                     'data-kind', 'data-mime', 'data-name', 'data-path', 'data-b64', 'open'],
        });
      } catch (e) {
        return escapeHtml(text);
      }
    },

    _renderToolUse(b) {
      const toolName = b.name || 'tool';
      const inputObj = b.input || {};
      let summaryText = '';
      if (typeof inputObj === 'object') {
        if (inputObj.query) summaryText = `query: "${String(inputObj.query).slice(0, 60)}"`;
        else if (inputObj.command) summaryText = `cmd: "${String(inputObj.command).slice(0, 60)}"`;
        else if (inputObj.code) summaryText = `code (${String(inputObj.code).split('\n').length} lines)`;
        else if (inputObj.file_path || inputObj.path) summaryText = `path: ${inputObj.file_path || inputObj.path}`;
      }
      const args = JSON.stringify(inputObj, null, 2);
      return `<details class="tool" open>
        <summary class="tool-summary">
          <div class="tool-head">
            <span class="badge">${escapeHtml(toolName)}</span>
            <span class="tool-title muted small">${escapeHtml(summaryText || b.id || '')}</span>
          </div>
          <span class="muted small">toggle</span>
        </summary>
        <div class="tool-content">
          <pre><code class="language-json">${escapeHtml(args)}</code></pre>
        </div>
      </details>`;
    },

    _renderToolResult(c) {
      const inner = Array.isArray(c.content) ? c.content : [{ type: 'text', text: String(c.content || '') }];
      const hasError = !!c.is_error;
      const parts = inner.map(b => {
        if (b.type === 'text') {
          const rawText = b.text || '';
          // Check if it's already an artifact block emitted by artifact_parser
          if (rawText.includes('class="artifact"')) {
            return expandArtifacts(rawText);
          }
          // Pretty-format JSON responses if valid JSON
          const trimmed = rawText.trim();
          if ((trimmed.startsWith('{') && trimmed.endsWith('}')) || (trimmed.startsWith('[') && trimmed.endsWith(']'))) {
            try {
              const parsed = JSON.parse(trimmed);
              const formatted = JSON.stringify(parsed, null, 2);
              return `<pre><code class="language-json">${escapeHtml(formatted)}</code></pre>`;
            } catch (_) {}
          }
          // Markdown or preformatted code
          if (trimmed.includes('\n') || trimmed.length > 80) {
            return `<pre><code>${escapeHtml(rawText)}</code></pre>`;
          }
          return `<div style="font-family: var(--mono); font-size: 12.5px;">${escapeHtml(rawText)}</div>`;
        }
        if (b.type === 'image') {
          return `<img class="tool-img" alt="output" src="data:${b.mime_type || 'image/png'};base64,${b.data}">`;
        }
        return '';
      }).join('\n');

      return `<details class="tool" open>
        <summary class="tool-summary">
          <div class="tool-head">
            <span class="badge badge-result ${hasError ? 'error' : ''}">${hasError ? 'tool error' : 'tool result'}</span>
          </div>
          <span class="muted small">toggle</span>
        </summary>
        <div class="tool-content tool-result ${hasError ? 'error' : ''}">
          ${parts}
        </div>
      </details>`;
    },

    _enhanceCodeBlocks() {
      this.$nextTick(() => {
        const container = document.getElementById('messages');
        if (container) enhanceCodeBlocks(container);
      });
    },

    send() {
      if (!this.canSend) return;
      const text = this.input.trim();
      this.input = '';
      this.messages.push({ role: 'user', html: this._renderMd(text) });
      this.busy = true;
      this._activeAssistantIdx = -1;
      this.ws.send(JSON.stringify({ type: 'user', text }));
      this._scrollDown();
      this._enhanceCodeBlocks();
    },

    interrupt() {
      this.ws.send(JSON.stringify({ type: 'interrupt' }));
    },

    async uploadDataset(ev) {
      const input = ev.target;
      const file = input.files && input.files[0];
      input.value = '';  // allow re-uploading the same file
      if (!file || !this.currentId) return;
      this.uploading = true;
      try {
        const fd = new FormData();
        fd.append('file', file);
        const r = await fetch(`/v1/sessions/${this.currentId}/upload`, { method: 'POST', body: fd });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { alert(`upload failed: ${d.detail || r.status}`); return; }
        this.uploadedPath = d.path;
        this._loadFiles(this.currentId);
        // Nudge the agent so it knows the dataset is ready.
        if (this.connected && !this.busy) {
          this.sendText(`I uploaded a dataset to ${d.path}. Take a look with ds_preview and tell me what's in it.`);
        }
      } finally {
        this.uploading = false;
      }
    },

    sendText(text) {
      if (!this.currentId || !this.connected || this.busy) return;
      this.messages.push({ role: 'user', html: this._renderMd(text) });
      this.busy = true;
      this._activeAssistantIdx = -1;
      this.ws.send(JSON.stringify({ type: 'user', text }));
      this._scrollDown();
      this._enhanceCodeBlocks();
    },

    fmtSize(n) {
      if (n < 1024) return `${n} B`;
      if (n < 1024*1024) return `${(n/1024).toFixed(1)} KB`;
      return `${(n/1024/1024).toFixed(1)} MB`;
    },

    _scrollDown() {
      this.$nextTick(() => {
        const el = document.getElementById('messages');
        if (el) el.scrollTop = el.scrollHeight;
      });
    },
  };
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[ch]));
}

// Convert base64 data to a Blob for reliable in-browser viewing and downloading
function b64toBlob(b64Data, contentType) {
  try {
    const byteCharacters = atob(b64Data);
    const byteArrays = [];
    for (let offset = 0; offset < byteCharacters.length; offset += 512) {
      const slice = byteCharacters.slice(offset, offset + 512);
      const byteNumbers = new Array(slice.length);
      for (let i = 0; i < slice.length; i++) {
        byteNumbers[i] = slice.charCodeAt(i);
      }
      byteArrays.push(new Uint8Array(byteNumbers));
    }
    return new Blob(byteArrays, { type: contentType });
  } catch (e) {
    return new Blob([b64Data], { type: contentType });
  }
}

// Server emits <div class="artifact" data-kind="..." data-mime="..." data-name="..." data-path="..." data-b64="..."></div>
// Expand them into actual <img> or interactive file card elements with Blob URLs that open in the browser.
// Blob URLs are cached per path so re-renders (streaming text accumulation) don't leak object URLs.
const _blobUrlCache = new Map();
function expandArtifacts(html) {
  const tmpl = document.createElement('template');
  tmpl.innerHTML = html;
  tmpl.content.querySelectorAll('.artifact').forEach(div => {
    const kind = div.dataset.kind || 'file';
    const mime = div.dataset.mime || 'text/plain';
    const name = div.dataset.name || 'file';
    const b64  = div.dataset.b64 || '';
    const path = div.dataset.path || name;
    
    let node;
    if (mime.startsWith('image/')) {
      const dataUri = `data:${mime};base64,${b64}`;
      node = document.createElement('div');
      node.className = 'artifact-image';
      const img = document.createElement('img');
      img.src = dataUri; img.alt = name;
      const cap = document.createElement('div');
      cap.className = 'artifact-cap muted small';
      cap.textContent = `${name} (${kind})`;
      node.append(img, cap);
    } else {
      let blobUrl = _blobUrlCache.get(path);
      if (!blobUrl) {
        const blob = b64toBlob(b64, mime);
        blobUrl = URL.createObjectURL(blob);
        _blobUrlCache.set(path, blobUrl);
      }
      node = document.createElement('div');
      node.className = 'artifact-file-card';
      
      const icon = (kind === 'csv' || kind === 'tsv') ? '📊' :
                   kind === 'json' ? '🏷️' :
                   (kind === 'md' || kind === 'text') ? '📝' :
                   kind === 'pdf' ? '📑' : '📁';
      
      node.innerHTML = `
        <div class="artifact-file-left">
          <span class="artifact-file-icon">${icon}</span>
          <div class="artifact-file-meta">
            <span class="artifact-file-name">${escapeHtml(name)}</span>
            <span class="muted small">${escapeHtml(kind.toUpperCase())} · ${escapeHtml(mime)}</span>
          </div>
        </div>
        <div class="artifact-file-actions">
          <a class="artifact-action-btn" href="${blobUrl}" target="_blank" rel="noopener noreferrer" title="Open and view in browser tab">Open</a>
          <a class="artifact-action-btn download" href="${blobUrl}" download="${escapeHtml(name)}" title="Download file">Download</a>
        </div>
      `;
    }
    div.replaceWith(node);
  });
  return tmpl.innerHTML;
}

// Enhance code blocks with syntax highlighting and a sleek copy button, and render KaTeX math
function enhanceCodeBlocks(root) {
  if (!root) return;

  // 1. Render LaTeX / Math formulae via KaTeX if available
  if (window.renderMathInElement) {
    try {
      renderMathInElement(root, {
        delimiters: [
          { left: '$$', right: '$$', display: true },
          { left: '$', right: '$', display: false },
          { left: '\\(', right: '\\)', display: false },
          { left: '\\[', right: '\\]', display: true },
        ],
        throwOnError: false,
        ignoredTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code', 'option'],
      });
    } catch (e) {
      console.error('KaTeX rendering error:', e);
    }
  }
  
  root.querySelectorAll('pre code').forEach((codeBlock) => {
    // 2. Syntax highlighting
    if (!codeBlock.dataset.highlighted && window.hljs) {
      try {
        hljs.highlightElement(codeBlock);
      } catch (e) {}
    }

    const pre = codeBlock.parentElement;
    if (!pre || pre.querySelector('.copy-code-btn')) return;

    // 3. Add copy button
    const copyBtn = document.createElement('button');
    copyBtn.className = 'copy-code-btn';
    copyBtn.type = 'button';
    copyBtn.innerHTML = `
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect>
        <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
      </svg>
      <span>Copy</span>
    `;

    copyBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      const textToCopy = codeBlock.innerText.replace(/\n$/, '');
      navigator.clipboard.writeText(textToCopy).then(() => {
        copyBtn.classList.add('copied');
        copyBtn.innerHTML = `
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="20 6 9 17 4 12"></polyline>
          </svg>
          <span>Copied!</span>
        `;
        setTimeout(() => {
          copyBtn.classList.remove('copied');
          copyBtn.innerHTML = `
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect>
              <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
            </svg>
            <span>Copy</span>
          `;
        }, 2000);
      }).catch(err => {
        console.error('Copy failed', err);
      });
    });

    pre.appendChild(copyBtn);
  });
}
