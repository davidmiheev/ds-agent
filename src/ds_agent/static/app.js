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
            this.messages.push({ role: 'assistant', html: this._renderMd(m.content) });
          } else if (m.role === 'thinking') {
            this.messages.push({ role: 'assistant', html: this._renderMd('> ' + m.content + '\n\n'), thinking: true });
          } else if (m.role === 'tool') {
            this.messages.push({ role: 'tool', html: this._renderToolUse(m.content) });
          } else if (m.role === 'tool-result') {
            this.messages.push({ role: 'tool-result', html: this._renderToolResult({ content: m.content }) });
          }
        }
        if (d.last_usage) this.lastUsage = d.last_usage;
        this._scrollDown();
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
        return;
      }

      // Assistant text: stream into a single growing message.
      // Frames carry content at the top level (f.content); older/newer shapes
      // may wrap it in f.message, so accept both.
      if (f.type === 'assistant') {
        const blocks = f.content || (f.message && f.message.content) || [];
        for (const b of blocks) {
          if (b.type === 'text' && b.text) {
            this._appendAssistantText(b.text);
          } else if (b.type === 'thinking' && b.thinking) {
            this._appendAssistantText('> ' + b.thinking + '\n\n', { thinking: true });
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

    _appendAssistantText(text, opts = {}) {
      if (this._activeAssistantIdx < 0) {
        this.messages.push({ role: 'assistant', html: '' });
        this._activeAssistantIdx = this.messages.length - 1;
      }
      const m = this.messages[this._activeAssistantIdx];
      m._raw = (m._raw || '') + (opts.thinking ? '> ' + text : text);
      m.html = this._renderMd(m._raw);
      this._scrollDown();
    },

    _newToolBlock(b) {
      this.messages.push({ role: 'tool', html: this._renderToolUse(b) });
      this._activeToolIdx = this.messages.length - 1;
      this._scrollDown();
    },

    _appendToolResult(c) {
      const html = this._renderToolResult(c);
      this.messages.push({ role: 'tool-result', html });
      this._scrollDown();
    },

    _appendSystem(text) {
      this.messages.push({ role: 'system', html: `<span class="muted small">${escapeHtml(text)}</span>` });
      this._scrollDown();
    },

    _renderMd(text) {
      if (!window.marked) return escapeHtml(text);
      const dirty = marked.parse(text, { breaks: true, gfm: true });
      return DOMPurify.sanitize(dirty, { ADD_TAGS: ['img'], ADD_ATTR: ['src', 'alt', 'class'] });
    },

    _renderToolUse(b) {
      const args = JSON.stringify(b.input || {}, null, 2);
      return `<div class="tool">
        <div class="tool-head"><span class="badge">${escapeHtml(b.name || 'tool')}</span> <span class="muted small">${escapeHtml(b.id || '')}</span></div>
        <pre><code class="language-json">${escapeHtml(args)}</code></pre>
      </div>`;
    },

    _renderToolResult(c) {
      const inner = Array.isArray(c.content) ? c.content : [{ type: 'text', text: String(c.content || '') }];
      const parts = inner.map(b => {
        if (b.type === 'text') {
          // The server has already pre-processed __ARTIFACT__ markers into <div class="artifact"> blocks
          return b.text;
        }
        if (b.type === 'image') {
          return `<img class="tool-img" alt="output" src="data:${b.mime_type || 'image/png'};base64,${b.data}">`;
        }
        return '';
      }).join('\n');
      const err = c.is_error ? ' error' : '';
      // Parse server-emitted artifact divs and turn them into real elements.
      const cleaned = expandArtifacts(parts);
      return `<div class="tool-result${err}">${cleaned}</div>`;
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

// Server emits <div class="artifact" data-kind="..." data-mime="..." data-name="..." data-b64="..."></div>
// Expand them into actual <img>/<a download> elements.
function expandArtifacts(html) {
  const tmpl = document.createElement('template');
  tmpl.innerHTML = html;
  tmpl.content.querySelectorAll('.artifact').forEach(div => {
    const kind = div.dataset.kind;
    const mime = div.dataset.mime;
    const name = div.dataset.name;
    const b64  = div.dataset.b64;
    const dataUri = `data:${mime};base64,${b64}`;
    let node;
    if (mime.startsWith('image/')) {
      node = document.createElement('div');
      node.className = 'artifact-image';
      const img = document.createElement('img');
      img.src = dataUri; img.alt = name;
      const cap = document.createElement('div');
      cap.className = 'artifact-cap muted small';
      cap.textContent = `${name} (${kind})`;
      node.append(img, cap);
    } else {
      node = document.createElement('a');
      node.className = 'artifact-file';
      node.href = dataUri;
      node.download = name;
      node.textContent = `⬇ ${name} (${mime})`;
    }
    div.replaceWith(node);
  });
  // Run syntax highlighting on <pre><code> blocks that aren't <pre> for tool inputs (those are pre-marked)
  tmpl.content.querySelectorAll('pre code').forEach(el => { try { hljs.highlightElement(el); } catch {} });
  return tmpl.innerHTML;
}
