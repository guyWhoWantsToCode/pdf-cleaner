'use strict';
/* PDF Cleaner — browser side. Talks only to the local server on 127.0.0.1.
 * "Delete" never erases anything: after the user confirms, the server moves the
 * file to the Windows Recycle Bin, where it can be restored. */
(() => {
  const $ = (sel) => document.querySelector(sel);
  const TOKEN = document.querySelector('meta[name="app-token"]').content;

  // Per-browser conveniences only (sort, filter, theme). Review decisions live on the server.
  const LS = {
    get(key, fallback) {
      try { const v = localStorage.getItem('pdfc.' + key); return v === null ? fallback : JSON.parse(v); }
      catch { return fallback; }
    },
    set(key, value) { try { localStorage.setItem('pdfc.' + key, JSON.stringify(value)); } catch { /* ignore */ } },
  };

  const STATUS = {
    keep: { label: 'Kept', cls: 'keep' },
    skip: { label: 'Skipped', cls: 'skip' },
    deleted: { label: 'In Recycle Bin', cls: 'deleted' },
    none: { label: 'Not reviewed', cls: 'none' },
  };

  const state = {
    root: null,
    files: [],
    byId: new Map(),
    view: [],
    currentId: null,
    filter: LS.get('filter', 'all'),
    sort: LS.get('sort', 'name'),
    dir: LS.get('dir', 1),
    query: '',
    actionBusy: false,
  };

  const el = {
    folderForm: $('#folderForm'), folderInput: $('#folderInput'), chkRecursive: $('#chkRecursive'),
    btnPick: $('#btnPick'), btnPickBig: $('#btnPickBig'), btnTheme: $('#btnTheme'), btnFiles: $('#btnFiles'),
    progress: $('#progress'), progressBar: $('#progressBar'), progressText: $('#progressText'),
    search: $('#search'), filter: $('#filter'), sort: $('#sort'), btnDir: $('#btnDir'),
    listMeta: $('#listMeta'), fileList: $('#fileList'), scrim: $('#scrim'),
    welcome: $('#welcome'), viewerWrap: $('#viewerWrap'), viewer: $('#viewer'), viewerMsg: $('#viewerMsg'),
    viewerTitle: $('#viewerTitle'), position: $('#position'), btnPrev: $('#btnPrev'), btnNext: $('#btnNext'),
    btnOpenTab: $('#btnOpenTab'),
    details: $('#details'), dStatus: $('#dStatus'), dName: $('#dName'), dSize: $('#dSize'), dPages: $('#dPages'),
    dModified: $('#dModified'), dLocation: $('#dLocation'), actions: $('#actions'), ghostNote: $('#ghostNote'),
    btnKeep: $('#btnKeep'), btnSkip: $('#btnSkip'), btnDelete: $('#btnDelete'), btnReset: $('#btnReset'),
    dialog: $('#confirmDialog'), confirmName: $('#confirmName'), confirmPath: $('#confirmPath'),
    btnConfirmDelete: $('#btnConfirmDelete'), btnCancelDelete: $('#btnCancelDelete'),
    busy: $('#busy'), busyText: $('#busyText'), toasts: $('#toasts'),
  };

  // ------------------------------------------------------------------ helpers

  async function api(path, body) {
    let res;
    try {
      res = await fetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Token': TOKEN },
        body: JSON.stringify(body || {}),
      });
    } catch {
      throw new Error("Can't reach PDF Cleaner. Is its window still open?");
    }
    let data = null;
    try { data = await res.json(); } catch { /* not JSON */ }
    if (!res.ok || !data || data.ok === false) {
      throw new Error((data && data.error) || `Something went wrong (${res.status}).`);
    }
    return data;
  }

  const escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function fmtSize(bytes) {
    if (bytes == null) return '—';
    const units = ['B', 'KB', 'MB', 'GB'];
    let n = bytes, i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n : n.toFixed(n < 10 ? 1 : 0)) + ' ' + units[i];
  }
  const dateFull = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' });
  const dateShort = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' });
  const fmtDate = (s, short) => (s ? (short ? dateShort : dateFull).format(new Date(s * 1000)) : '—');
  const fmtPages = (f) => (f.pages === undefined ? (f.cloud ? 'Online-only' : '…') : f.pages === null ? '—' : String(f.pages));
  const statusKey = (f) => f.status || 'none';
  const current = () => (state.currentId ? state.byId.get(state.currentId) : null);
  const joinPath = (root, rel) => root.replace(/[\\/]+$/, '') + '\\' + rel;

  function toast(message, kind = 'info', opts = {}) {
    const t = document.createElement('div');
    t.className = 'toast' + (kind === 'error' ? ' error' : '');
    t.setAttribute('role', kind === 'error' ? 'alert' : 'status');
    const msg = document.createElement('span');
    msg.className = 'msg';
    msg.textContent = message;
    t.appendChild(msg);
    let timer;
    const close = () => { clearTimeout(timer); t.remove(); };
    if (opts.action) {
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = opts.action;
      b.addEventListener('click', () => { close(); opts.onAction(); });
      t.appendChild(b);
    }
    const x = document.createElement('button');
    x.type = 'button';
    x.setAttribute('aria-label', 'Dismiss');
    x.textContent = '✕';
    x.style.textDecoration = 'none';
    x.addEventListener('click', close);
    t.appendChild(x);
    el.toasts.appendChild(t);
    while (el.toasts.children.length > 3) el.toasts.firstChild.remove();
    timer = setTimeout(close, opts.duration || (kind === 'error' ? 8000 : 5000));
  }

  function setBusy(on, text) {
    el.busy.hidden = !on;
    if (text) el.busyText.textContent = text;
  }

  // ------------------------------------------------------------------ view (filter / search / sort)

  function computeView() {
    const q = state.query.trim().toLowerCase();
    const words = q ? q.split(/\s+/) : [];
    let list = state.files.filter((f) => {
      if (state.filter !== 'all' && statusKey(f) !== state.filter) return false;
      if (!words.length) return true;
      const hay = (f.name + ' ' + f.folder).toLowerCase();
      return words.every((w) => hay.includes(w));
    });
    const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });
    const by = {
      name: (a, b) => collator.compare(a.name, b.name),
      folder: (a, b) => collator.compare(a.folder, b.folder) || collator.compare(a.name, b.name),
      size: (a, b) => (a.size ?? -1) - (b.size ?? -1),
      mtime: (a, b) => (a.mtime ?? 0) - (b.mtime ?? 0),
      pages: (a, b) => (a.pages ?? -1) - (b.pages ?? -1),
    }[state.sort] || ((a, b) => collator.compare(a.name, b.name));
    list.sort((a, b) => by(a, b) * state.dir || collator.compare(a.name, b.name));
    state.view = list.map((f) => f.id);
  }

  function subLine(f) {
    const parts = [];
    if (f.folder && f.folder !== '.') parts.push(f.folder);
    parts.push(fmtSize(f.size));
    if (f.pages != null) parts.push(f.pages + (f.pages === 1 ? ' page' : ' pages'));
    parts.push(fmtDate(f.mtime, true));
    return parts.join(' · ');
  }

  function renderList() {
    if (!state.files.length) {
      el.fileList.innerHTML = state.root ? '<li class="list-empty">No PDFs in this folder.</li>' : '';
      el.listMeta.textContent = '';
      return;
    }
    if (!state.view.length) {
      el.fileList.innerHTML = '<li class="list-empty">No files match. Try a different search or filter.</li>';
    } else {
      const html = state.view.map((id) => {
        const f = state.byId.get(id);
        const s = statusKey(f);
        return `<li class="file-item${f.ghost ? ' ghost' : ''}${id === state.currentId ? ' current' : ''}" role="option" data-id="${id}" aria-selected="${id === state.currentId}" title="${escapeHtml(f.rel)}">`
          + `<span class="dot ${s}" title="${STATUS[s].label}"></span>`
          + `<span class="name">${escapeHtml(f.name)}</span>`
          + `<span class="sub">${escapeHtml(subLine(f))}</span></li>`;
      }).join('');
      el.fileList.innerHTML = html;
    }
    const n = state.view.length, total = state.files.length;
    el.listMeta.textContent = n === total ? `${total} file${total === 1 ? '' : 's'}` : `Showing ${n} of ${total}`;
  }

  function renderProgress() {
    const c = { keep: 0, skip: 0, deleted: 0, none: 0 };
    for (const f of state.files) c[statusKey(f)]++;
    const total = state.files.length;
    el.progress.hidden = !total;
    if (!total) return;
    const pct = (n) => (100 * n / total) + '%';
    const [sKeep, sDel, sSkip] = el.progressBar.children;
    sKeep.style.width = pct(c.keep); sDel.style.width = pct(c.deleted); sSkip.style.width = pct(c.skip);
    const decided = c.keep + c.deleted;
    el.progressText.innerHTML =
      `<span><strong>${decided}</strong> of ${total} decided (${Math.round(100 * decided / total)}%)</span>`
      + `<span class="legend keep">${c.keep} kept</span>`
      + `<span class="legend deleted">${c.deleted} in Recycle Bin</span>`
      + `<span class="legend skip">${c.skip} skipped</span>`
      + `<span class="legend none">${c.none} not reviewed</span>`;
    // Counts in the filter menu.
    const labels = { all: 'All files', none: 'Not reviewed', keep: 'Kept', skip: 'Skipped', deleted: 'In Recycle Bin' };
    for (const opt of el.filter.options) {
      const n = opt.value === 'all' ? total : c[opt.value];
      opt.textContent = `${labels[opt.value]} (${n})`;
    }
  }

  function rebuild() {
    computeView();
    renderList();
    renderProgress();
    updatePosition();
  }

  // ------------------------------------------------------------------ selection + viewer

  function updatePosition() {
    const i = state.view.indexOf(state.currentId);
    el.position.textContent = i >= 0 ? `${i + 1} / ${state.view.length}` : `– / ${state.view.length}`;
    el.btnPrev.disabled = i <= 0 && i !== -1 ? true : state.view.length === 0;
    el.btnNext.disabled = i === state.view.length - 1 || state.view.length === 0;
  }

  function showViewerMessage(title, text) {
    el.viewer.hidden = true;
    el.viewer.removeAttribute('src');
    el.viewer.dataset.id = '';
    el.viewerMsg.hidden = false;
    el.viewerMsg.innerHTML = `<div class="box"><h3>${escapeHtml(title)}</h3><p>${escapeHtml(text)}</p></div>`;
  }

  function select(id) {
    state.currentId = id || null;
    const f = current();
    for (const li of el.fileList.querySelectorAll('.file-item.current')) {
      li.classList.remove('current'); li.setAttribute('aria-selected', 'false');
    }
    if (f) {
      const li = el.fileList.querySelector(`[data-id="${id}"]`);
      if (li) { li.classList.add('current'); li.setAttribute('aria-selected', 'true'); li.scrollIntoView({ block: 'nearest' }); }
    }
    updatePosition();
    document.body.classList.remove('drawer-open');

    if (!f) {
      el.details.hidden = true;
      el.viewerTitle.textContent = '';
      el.btnOpenTab.hidden = true;
      if (state.root) {
        showViewerMessage(state.files.length ? 'Nothing selected' : 'No PDFs found',
          state.files.length ? 'Pick a file from the list, or change the search or filter.'
            : 'This folder has no PDF files. Try including subfolders, or choose another folder.');
      }
      document.title = 'PDF Cleaner';
      return;
    }

    document.title = `${f.name} · PDF Cleaner`;
    el.viewerTitle.textContent = f.name;
    if (f.ghost) {
      el.btnOpenTab.hidden = true;
      showViewerMessage('This file is in the Recycle Bin',
        'To get it back, open the Recycle Bin on your desktop, right-click the file, choose Restore, then scan again.');
    } else {
      const url = `/api/file/${encodeURIComponent(f.id)}/${encodeURIComponent(f.name)}?t=${encodeURIComponent(TOKEN)}`;
      el.btnOpenTab.href = url;
      el.btnOpenTab.hidden = false;
      el.viewerMsg.hidden = true;
      el.viewer.hidden = false;
      if (el.viewer.dataset.id !== f.id) {
        el.viewer.dataset.id = f.id;
        el.viewer.src = url + '#view=FitH';
      }
      if (f.pages === undefined) loadPagesFor([f.id], true);
    }
    renderDetails();
    if (state.root) LS.set('last:' + state.root.toLowerCase(), f.id);
  }

  function renderDetails() {
    const f = current();
    if (!f) { el.details.hidden = true; return; }
    el.details.hidden = false;
    const s = statusKey(f);
    el.dStatus.className = 'chip ' + STATUS[s].cls;
    el.dStatus.textContent = STATUS[s].label;
    el.dName.textContent = f.name;
    el.dName.title = f.name;
    el.dSize.textContent = fmtSize(f.size);
    el.dPages.textContent = fmtPages(f);
    el.dModified.textContent = fmtDate(f.mtime);
    el.dLocation.textContent = joinPath(state.root, f.folder === '.' ? '' : f.folder).replace(/\\$/, '');
    el.actions.hidden = !!f.ghost;
    el.ghostNote.hidden = !f.ghost;
    el.btnKeep.classList.toggle('selected', s === 'keep');
    el.btnSkip.classList.toggle('selected', s === 'skip');
    el.btnReset.hidden = !(s === 'keep' || s === 'skip');
  }

  function move(step) {
    if (!state.view.length) return;
    let i = state.view.indexOf(state.currentId);
    if (i === -1) i = step > 0 ? -1 : state.view.length;
    const j = Math.min(Math.max(i + step, 0), state.view.length - 1);
    if (j !== i) select(state.view[j]);
  }

  // After a decision, go to the next file in the order the list had before the change.
  function advanceFrom(id, oldView) {
    const visible = new Set(state.view);
    const i = oldView.indexOf(id);
    for (let j = i + 1; j < oldView.length; j++) {
      const f = state.byId.get(oldView[j]);
      if (visible.has(oldView[j]) && !f.ghost) return select(oldView[j]);
    }
    const nextOpen = state.view.find((x) => x !== id && !state.byId.get(x).status);
    if (nextOpen) {
      toast('Reached the end of the list. Jumped to the first file not yet reviewed.');
      return select(nextOpen);
    }
    toast('That was the last file in this list.');
    select(visible.has(id) ? id : (state.view[state.view.length - 1] || null));
  }

  // ------------------------------------------------------------------ actions

  async function setStatus(status, { advance = true } = {}) {
    const f = current();
    if (!f || f.ghost || state.actionBusy) return;
    const prev = f.status || null;
    const oldView = state.view.slice();
    state.actionBusy = true;
    try {
      await api('/api/status', { id: f.id, status });
      f.status = status;
      rebuild();
      if (advance) advanceFrom(f.id, oldView); else select(f.id);
      if (status && status !== prev) {
        toast(`${status === 'keep' ? 'Kept' : 'Skipped'}: ${f.name}`, 'info', {
          action: 'Undo',
          onAction: () => undoStatus(f, prev),
        });
      } else if (!status) {
        toast(`Decision cleared: ${f.name}`);
      }
    } catch (e) {
      toast(e.message, 'error');
    } finally {
      state.actionBusy = false;
    }
  }

  async function undoStatus(f, prev) {
    try {
      await api('/api/status', { id: f.id, status: prev });
      f.status = prev;
      rebuild();
      select(f.id);
    } catch (e) { toast(e.message, 'error'); }
  }

  function askDelete() {
    const f = current();
    if (!f || f.ghost || state.actionBusy) return;
    el.confirmName.textContent = f.name;
    el.confirmPath.textContent = joinPath(state.root, f.rel);
    el.btnConfirmDelete.disabled = false;
    el.btnCancelDelete.disabled = false;
    el.dialog.showModal();
    el.btnCancelDelete.focus();  // Enter = Cancel, so a stray key press never deletes
  }

  async function confirmDelete() {
    const f = current();
    if (!f || f.ghost) { el.dialog.close(); return; }
    const oldView = state.view.slice();
    el.btnConfirmDelete.disabled = true;
    el.btnCancelDelete.disabled = true;
    state.actionBusy = true;
    // Unload the PDF first so the browser isn't holding it.
    el.viewer.removeAttribute('src');
    el.viewer.dataset.id = '';
    try {
      await api('/api/delete', { id: f.id, confirm: 'move-to-recycle-bin' });
      el.dialog.close();
      f.status = 'deleted';
      f.ghost = true;
      rebuild();
      advanceFrom(f.id, oldView);
      toast(`Moved to the Recycle Bin: ${f.name}`, 'info', { duration: 7000 });
    } catch (e) {
      el.dialog.close();
      toast(e.message, 'error');
      select(f.id);
    } finally {
      state.actionBusy = false;
    }
  }

  // ------------------------------------------------------------------ page counts (in the background)

  let pageLoopRunning = false;

  async function loadPagesFor(ids, force) {
    try {
      const { pages } = await api('/api/pages', { ids, force: !!force });
      for (const [id, n] of Object.entries(pages)) {
        const f = state.byId.get(id);
        if (!f) continue;
        f.pages = n;
        const li = el.fileList.querySelector(`[data-id="${id}"] .sub`);
        if (li) li.textContent = subLine(f);
        if (id === state.currentId) el.dPages.textContent = fmtPages(f);
      }
    } catch { /* page counts are optional */ }
  }

  async function loadAllPages() {
    if (pageLoopRunning) return;
    pageLoopRunning = true;
    const root = state.root;
    try {
      for (;;) {
        if (state.root !== root) break;
        const batch = state.files.filter((f) => f.pages === undefined && !f.ghost && !f.cloud).slice(0, 12).map((f) => f.id);
        if (!batch.length) break;
        await loadPagesFor(batch, false);
        for (const id of batch) { const f = state.byId.get(id); if (f && f.pages === undefined) f.pages = null; }
      }
      if (state.root === root && state.sort === 'pages') { rebuild(); select(state.currentId); }
    } finally {
      pageLoopRunning = false;
    }
  }

  // ------------------------------------------------------------------ scanning

  async function scan(folder, recursive) {
    setBusy(true, 'Looking for PDFs…');
    try {
      const data = await api('/api/scan', { folder, recursive });
      state.root = data.root;
      state.files = data.files;
      state.byId = new Map(data.files.map((f) => [f.id, f]));
      state.currentId = null;
      el.folderInput.value = data.root;
      el.chkRecursive.checked = data.recursive;
      el.welcome.hidden = true;
      el.viewerWrap.hidden = false;
      rebuild();
      const last = LS.get('last:' + data.root.toLowerCase(), null);
      const firstOpen = state.view.find((id) => !state.byId.get(id).status);
      select((last && state.view.includes(last)) ? last : (firstOpen || state.view[0] || null));
      const live = data.files.filter((f) => !f.ghost).length;
      if (data.truncated) {
        toast(`Found more than ${data.limit.toLocaleString()} PDFs. Only the first ${data.limit.toLocaleString()} are shown. Choose a smaller folder to see the rest.`, 'error');
      } else {
        toast(`Found ${live} PDF${live === 1 ? '' : 's'}.`);
      }
      loadAllPages();
    } catch (e) {
      toast(e.message, 'error');
    } finally {
      setBusy(false);
    }
  }

  async function pickFolder() {
    setBusy(true, 'Choose a folder in the window that just opened…');
    try {
      const { folder } = await api('/api/pick-folder', { initial: state.root || el.folderInput.value || '' });
      setBusy(false);
      if (folder) {
        el.folderInput.value = folder;
        await scan(folder, el.chkRecursive.checked);
      }
    } catch (e) {
      toast(e.message, 'error');
    } finally {
      setBusy(false);
    }
  }

  // ------------------------------------------------------------------ theme

  function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
  }

  // ------------------------------------------------------------------ events

  function bind() {
    el.btnPick.addEventListener('click', pickFolder);
    el.btnPickBig.addEventListener('click', pickFolder);
    el.folderForm.addEventListener('submit', (e) => {
      e.preventDefault();
      const v = el.folderInput.value.trim();
      if (!v) { pickFolder(); return; }
      scan(v, el.chkRecursive.checked);
    });
    el.chkRecursive.addEventListener('change', () => {
      if (state.root) scan(state.root, el.chkRecursive.checked);
    });

    el.btnTheme.addEventListener('click', () => {
      const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      applyTheme(next);
      LS.set('theme', next);
    });
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', (e) => {
      if (!LS.get('theme', null)) applyTheme(e.matches ? 'dark' : 'light');
    });

    let searchTimer;
    el.search.addEventListener('input', () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => { state.query = el.search.value; rebuild(); select(state.currentId); }, 120);
    });
    el.filter.value = state.filter;
    el.sort.value = state.sort;
    el.btnDir.style.transform = state.dir < 0 ? 'scaleY(-1)' : '';
    el.filter.addEventListener('change', () => {
      state.filter = el.filter.value; LS.set('filter', state.filter);
      rebuild();
      if (!state.view.includes(state.currentId)) select(state.view[0] || null); else select(state.currentId);
    });
    el.sort.addEventListener('change', () => {
      state.sort = el.sort.value; LS.set('sort', state.sort); rebuild(); select(state.currentId);
    });
    el.btnDir.addEventListener('click', () => {
      state.dir = -state.dir; LS.set('dir', state.dir);
      el.btnDir.style.transform = state.dir < 0 ? 'scaleY(-1)' : '';
      rebuild(); select(state.currentId);
    });

    el.fileList.addEventListener('click', (e) => {
      const li = e.target.closest('.file-item');
      if (li) select(li.dataset.id);
    });

    el.btnPrev.addEventListener('click', () => move(-1));
    el.btnNext.addEventListener('click', () => move(1));
    el.btnKeep.addEventListener('click', () => setStatus('keep'));
    el.btnSkip.addEventListener('click', () => setStatus('skip'));
    el.btnReset.addEventListener('click', () => setStatus(null, { advance: false }));
    el.btnDelete.addEventListener('click', askDelete);
    el.btnConfirmDelete.addEventListener('click', confirmDelete);

    el.btnFiles.addEventListener('click', () => document.body.classList.toggle('drawer-open'));
    el.scrim.addEventListener('click', () => document.body.classList.remove('drawer-open'));

    document.addEventListener('keydown', (e) => {
      if (el.dialog.open) return;  // the dialog handles its own keys (Esc = cancel)
      const t = e.target;
      if (t.closest && t.closest('input, select, textarea')) {
        if (e.key === 'Escape') t.blur();
        return;
      }
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      const key = e.key.length === 1 ? e.key.toLowerCase() : e.key;
      const handlers = {
        k: () => setStatus('keep'),
        s: () => setStatus('skip'),
        d: askDelete,
        Delete: askDelete,
        u: () => { const f = current(); if (f && f.status && !f.ghost) setStatus(null, { advance: false }); },
        ArrowRight: () => move(1), ArrowDown: () => move(1), j: () => move(1),
        ArrowLeft: () => move(-1), ArrowUp: () => move(-1),
        '/': () => el.search.focus(),
        Escape: () => document.body.classList.remove('drawer-open'),
      };
      const fn = handlers[key];
      if (fn) { e.preventDefault(); fn(); }
    });
  }

  // ------------------------------------------------------------------ start

  async function init() {
    bind();
    try {
      const { settings } = await api('/api/state');
      if (typeof settings.recursive === 'boolean') el.chkRecursive.checked = settings.recursive;
      if (settings.lastFolder) {
        el.folderInput.value = settings.lastFolder;
        await scan(settings.lastFolder, el.chkRecursive.checked);
      }
    } catch (e) {
      toast(e.message, 'error');
    }
  }

  init();
})();
