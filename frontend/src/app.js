import { Editor, Extension, Mark } from '@tiptap/core';
import StarterKit from '@tiptap/starter-kit';
import { TableKit } from '@tiptap/extension-table';

// All application state is local. API keys are sent once to the backend and are
// never written to localStorage, sessionStorage, URLs, or the document model.
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const state = {
  settings: {}, documents: [], sources: [], current: null, editor: null,
  contentVersion: 0, savedVersion: 0, selection: null, scope: new Set(),
  savePromise: null, saveTimer: null, saveConflict: false, loading: false,
  assistantJob: null, assistantPending: false, indexJob: null, metadataId: null,
  suggestions: [], activeChatDocument: null,
};
const MODEL_LABELS = {
  'gpt-6-astra': 'GPT-6 Astra', 'gpt-6.1-sol': 'GPT-6.1 Sol',
  'gpt-6.1-terra': 'GPT-6.1 Terra', 'gpt-6-luna': 'GPT-6 Luna',
};
const emptyDocument = () => ({ type: 'doc', content: [{ type: 'paragraph' }] });
const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
const e = (tag, attrs = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = String(value ?? '');
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (key in node && !key.startsWith('aria-')) node[key] = value;
    else node.setAttribute(key, String(value));
  }
  for (const child of children.flat()) {
    if (child !== null && child !== undefined) node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
};
const friendlyDate = (value) => {
  if (!value) return 'Just now';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
};
const formatNumber = (value) => Number(value || 0).toLocaleString();
const wordCount = (text) => (text.trim().match(/\S+/gu) || []).length;

class APIError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}
async function api(path, { method = 'GET', data, body, signal } = {}) {
  const headers = { 'X-ThesisHelper-Request': '1' };
  if (data !== undefined) { headers['Content-Type'] = 'application/json'; body = JSON.stringify(data); }
  let response;
  try { response = await fetch(path, { method, headers, body, signal, credentials: 'same-origin' }); }
  catch (error) {
    if (error.name === 'AbortError') throw error;
    throw new APIError('Cannot reach ThesisHelper. Check that the local server window is still running.', 0);
  }
  const type = response.headers.get('content-type') || '';
  const payload = type.includes('application/json') ? await response.json() : await response.text();
  if (!response.ok) {
    let detail = payload?.detail || payload?.error || `Request failed (${response.status}).`;
    if (Array.isArray(detail)) detail = detail.map((item) => `${item.loc?.at(-1) || 'Input'}: ${item.msg || 'Invalid value'}`).join('; ');
    if (typeof detail !== 'string') detail = JSON.stringify(detail);
    throw new APIError(detail, response.status);
  }
  return payload;
}
function toast(message, type = '', duration = 6500) {
  const node = e('div', { class: `toast ${type}` }, e('span', { text: message }));
  node.append(e('button', { text: '×', 'aria-label': 'Dismiss notification', onclick: () => node.remove() }));
  $('#toast-region').append(node);
  setTimeout(() => node.remove(), duration);
}
function fail(error) { toast(error.message || String(error), 'error', 10000); }
function showDialog(selector) {
  const dialog = $(selector);
  if (!dialog.open) dialog.showModal();
}
function setSaveStatus(text, kind = '') {
  $('#save-status').className = kind;
  $('#save-status').replaceChildren(e('span', { class: 'status-dot' }), document.createTextNode(text));
}
function sourceLink(source) {
  return `/api/library/${encodeURIComponent(source.document_id || source.id)}/file${source.page ? `#page=${Number(source.page)}` : ''}`;
}

// Tiptap is bundled into static/app.js; the installed app loads no CDN assets.
const Superscript = Mark.create({
  name: 'superscript', parseHTML: () => [{ tag: 'sup' }], renderHTML: () => ['sup', 0],
});
const Subscript = Mark.create({
  name: 'subscript', parseHTML: () => [{ tag: 'sub' }], renderHTML: () => ['sub', 0],
});
const TextAlignment = Extension.create({
  name: 'preserveTextAlignment',
  addGlobalAttributes() {
    return [{ types: ['paragraph', 'heading'], attributes: { textAlign: {
      default: null,
      parseHTML: (element) => ['left', 'center', 'right', 'justify'].includes(element.style.textAlign) ? element.style.textAlign : null,
      renderHTML: (attributes) => ['left', 'center', 'right', 'justify'].includes(attributes.textAlign) ? { style: `text-align: ${attributes.textAlign}` } : {},
    } } }];
  },
});
function makeEditor() {
  state.editor = new Editor({
    element: $('#editor'),
    extensions: [
      StarterKit.configure({ heading: { levels: [1, 2, 3, 4, 5, 6] }, link: { openOnClick: false, autolink: false }, trailingNode: false }),
      TableKit.configure({ table: { resizable: false } }), Superscript, Subscript, TextAlignment,
    ],
    content: emptyDocument(),
    editorProps: { attributes: { role: 'textbox', 'aria-label': 'Thesis text editor', 'aria-multiline': 'true', spellcheck: 'true' } },
    onUpdate: () => { if (!state.loading) markDirty(); updateEditorState(); },
    onSelectionUpdate: ({ editor }) => {
      const { from, to, empty } = editor.state.selection;
      if (!empty && state.current) {
        const text = editor.state.doc.textBetween(from, to, '\n');
        state.selection = { from, to, text, documentId: state.current.id, contentVersion: state.contentVersion };
        renderSelection();
      }
      updateEditorState();
    },
    onTransaction: () => updateToolbar(),
  });
  state.editor.setEditable(false);
}
function updateEditorState() {
  if (!state.editor) return;
  $('#word-count').textContent = formatNumber(wordCount(state.editor.getText()));
  $('#empty-document-hint').hidden = !state.editor.isEmpty;
  $('#selection-count').textContent = state.selection ? `${wordCount(state.selection.text)} selected words` : 'No selection';
  updateToolbar();
  updateSuggestionStates();
}
function updateToolbar() {
  if (!state.editor) return;
  $$('[data-format]').forEach((button) => {
    const name = button.dataset.format;
    if (['bold', 'italic', 'underline', 'bulletList', 'orderedList', 'blockquote'].includes(name)) {
      button.classList.toggle('active', state.editor.isActive(name));
      button.setAttribute('aria-pressed', String(state.editor.isActive(name)));
    }
    button.disabled = !state.current;
  });
  const level = [1, 2, 3, 4, 5, 6].find((level) => state.editor.isActive('heading', { level }));
  $('#text-format').value = level ? `h${level}` : 'paragraph';
  $('#table-actions').hidden = !state.editor.isActive('table');
}
function renderSelection() {
  const selection = state.selection;
  $('#selection-box').hidden = !selection;
  $('#selection-preview').textContent = selection ? selection.text.slice(0, 450) : '';
  $('#selection-meta').textContent = selection ? `${wordCount(selection.text)} words selected · suggestions wait for your approval` : '';
  $('#selection-count').textContent = selection ? `${wordCount(selection.text)} selected words` : 'No selection';
}
function clearSelection() {
  state.selection = null;
  renderSelection();
}
function recoveryKey(id) { return `thesishelper.recovery.${id}`; }
function stashRecovery() {
  if (!state.current) return;
  try {
    localStorage.setItem(recoveryKey(state.current.id), JSON.stringify({
      id: state.current.id, title: $('#document-title').value,
      content: state.editor.getJSON(), revision: state.current.revision, updated_at: new Date().toISOString(),
    }));
  } catch { /* Server saves remain authoritative if storage is unavailable/full. */ }
}
function markDirty() {
  if (!state.current || state.loading) return;
  state.contentVersion += 1;
  setSaveStatus('Unsaved changes', 'saving');
  stashRecovery();
  clearTimeout(state.saveTimer);
  if (!state.saveConflict) state.saveTimer = setTimeout(() => saveDocument().catch(() => {}), 1100);
}
function clearSaveWarning() { $('#save-warning').hidden = true; $('#save-warning').replaceChildren(); }
function saveError(error) {
  setSaveStatus('Not saved', 'error');
  const box = $('#save-warning');
  box.hidden = false;
  box.replaceChildren(e('span', { text: error.status === 409 ? 'A newer version exists. Your unsaved text has been kept in this browser. Save a recovery copy, then reload the saved document.' : `Your changes are not saved: ${error.message}` }));
  box.append(e('button', { class: 'button', text: 'Download recovery copy', onclick: downloadRecovery }));
  if (error.status === 409) {
    state.saveConflict = true;
    box.append(e('button', { class: 'button', text: 'Reload saved document', onclick: () => loadDocument(state.current.id, { skipSave: true }).catch(fail) }));
  } else {
    box.append(e('button', { class: 'button', text: 'Retry save', onclick: () => saveDocument().catch(fail) }));
  }
}
function downloadRecovery() {
  if (!state.current) return;
  const data = { title: $('#document-title').value, content: state.editor.getJSON(), text: state.editor.getText(), saved_revision: state.current.revision };
  downloadBlob(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }), 'ThesisHelper-recovery.json');
}
function downloadBlob(blob, name) {
  const url = URL.createObjectURL(blob);
  const link = e('a', { href: url, download: name });
  document.body.append(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}
async function saveDocument() {
  clearTimeout(state.saveTimer);
  if (state.savePromise) { await state.savePromise; return saveDocument(); }
  if (!state.current || state.contentVersion === state.savedVersion) return state.current;
  if (state.saveConflict) throw new APIError('Resolve the saved-version conflict before continuing.', 409);
  const docId = state.current.id;
  const version = state.contentVersion;
  const payload = { title: $('#document-title').value.trim() || 'Untitled thesis', content: state.editor.getJSON(), revision: state.current.revision };
  setSaveStatus('Saving…', 'saving');
  state.savePromise = (async () => {
    try {
      const saved = await api(`/api/documents/${encodeURIComponent(docId)}`, { method: 'PUT', data: payload });
      if (state.current?.id === docId) {
        state.current = saved;
        state.savedVersion = version;
        const index = state.documents.findIndex((item) => item.id === docId);
        if (index >= 0) state.documents[index] = saved;
        renderDocumentList();
        $('#document-breadcrumb').textContent = saved.title;
        $('#paper-document-kind').textContent = `WORKING DOCUMENT · REVISION ${saved.revision}`;
        if (saved.warnings?.length) toast(saved.warnings.join(' '), 'warning', 12000);
        clearSaveWarning();
        if (state.contentVersion === version) {
          setSaveStatus('Saved locally');
          try { localStorage.removeItem(recoveryKey(docId)); } catch {}
        } else { stashRecovery(); setSaveStatus('Unsaved changes', 'saving'); }
      }
      return saved;
    } catch (error) { saveError(error); throw error; }
    finally { state.savePromise = null; }
  })();
  const saved = await state.savePromise;
  // Edits made while a save was in flight receive a later revision.
  if (state.current?.id === docId && state.contentVersion !== state.savedVersion) return saveDocument();
  return saved;
}
function offerRecovery(doc) {
  let recovery;
  try { recovery = JSON.parse(localStorage.getItem(recoveryKey(doc.id)) || 'null'); } catch { return; }
  if (!recovery?.content || (JSON.stringify(recovery.content) === JSON.stringify(doc.content) && recovery.title === doc.title)) return;
  const box = $('#save-warning'); box.hidden = false;
  box.replaceChildren(e('span', { text: `An unsaved browser draft from ${friendlyDate(recovery.updated_at)} is available. The current saved version is shown.` }));
  box.append(e('button', { class: 'button', text: 'Recover my draft', onclick: () => {
    state.loading = true;
    state.editor.commands.setContent(recovery.content, { emitUpdate: false });
    $('#document-title').value = recovery.title || doc.title;
    state.loading = false;
    clearSaveWarning(); markDirty(); updateEditorState();
    toast('The recovered text is back in the editor and will be saved as a new version.');
  } }));
  box.append(e('button', { class: 'button', text: 'Keep saved version', onclick: () => {
    try { localStorage.removeItem(recoveryKey(doc.id)); } catch {}
    clearSaveWarning();
  } }));
}
async function loadDocument(id, { skipSave = false } = {}) {
  if (!skipSave) await saveDocument();
  const doc = await api(`/api/documents/${encodeURIComponent(id)}`);
  state.loading = true;
  state.current = doc;
  state.contentVersion = 0; state.savedVersion = 0; state.saveConflict = false;
  state.editor.commands.setContent(doc.content || emptyDocument(), { emitUpdate: false });
  state.editor.setEditable(true);
  $('#document-title').disabled = false;
  $('#document-title').value = doc.title;
  $('#document-breadcrumb').textContent = doc.title;
  $('#paper-document-kind').textContent = `WORKING DOCUMENT · REVISION ${doc.revision || 1}`;
  state.loading = false;
  clearSelection(); clearSaveWarning(); updateEditorState(); renderDocumentList();
  setSaveStatus('Saved locally');
  offerRecovery(doc);
  await loadChat(doc.id);
}
function renderDocumentList() {
  const list = $('#document-list'); list.replaceChildren();
  if (!state.documents.length) list.append(e('p', { class: 'muted small', text: 'Your next idea starts with a new document.' }));
  for (const doc of state.documents) list.append(e('button', {
    class: state.current?.id === doc.id ? 'active' : '',
    title: `${doc.title} · ${friendlyDate(doc.updated_at)}`,
    onclick: () => loadDocument(doc.id).catch(fail),
    'aria-current': state.current?.id === doc.id ? 'page' : 'false',
  }, e('span', { text: doc.title })));
  const mobile = $('#mobile-document-list');
  mobile.replaceChildren(...state.documents.map((doc) => e('button', {
    class: state.current?.id === doc.id ? 'active' : '',
    onclick: async () => { try { await loadDocument(doc.id); $('#documents-dialog').close(); } catch (error) { fail(error); } },
  }, e('span', { text: doc.title }))));
}
async function refreshDocuments() {
  const result = await api('/api/documents');
  state.documents = result.documents || []; renderDocumentList();
}
async function createDocument(title) {
  await saveDocument();
  const doc = await api('/api/documents', { method: 'POST', data: { title } });
  await refreshDocuments(); await loadDocument(doc.id);
  toast('Your new document is ready.');
}
async function importDocument(file) {
  if (!file) return;
  if (!file.name.toLowerCase().endsWith('.docx')) throw new Error('Choose a .docx Word document.');
  await saveDocument();
  const form = new FormData(); form.append('file', file);
  $('#import-document').disabled = true;
  try {
    const result = await api('/api/documents/import', { method: 'POST', body: form });
    await refreshDocuments(); await loadDocument(result.id || result.document?.id);
    toast('Word document imported. Review its layout before continuing.');
    if (result.warnings?.length) toast(result.warnings.join(' '), 'warning', 18000);
  } finally { $('#import-document').disabled = false; $('#import-input').value = ''; }
}
async function exportDocument() {
  if (!state.current) return toast('Create or import a document first.', 'warning');
  await saveDocument();
  const response = await fetch(`/api/documents/${encodeURIComponent(state.current.id)}/export`, { headers: { 'X-ThesisHelper-Request': '1' } });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Word export failed. Your saved document is unchanged.');
  }
  const filename = `${state.current.title.replace(/[^\p{L}\p{N} _-]+/gu, '').slice(0, 100) || 'thesis'}.docx`;
  downloadBlob(await response.blob(), filename);
  toast('Your Word document is ready. Advanced Word layouts should be reviewed after import/export.');
}

function renderSettings(settings) {
  state.settings = settings || {};
  const model = settings.model || 'gpt-6-astra';
  $('#model-indicator').textContent = MODEL_LABELS[model] || model;
  $('#key-indicator').textContent = settings.api_key_set ? 'Key set' : 'Set up';
  $('#key-indicator').classList.toggle('ready', !!settings.api_key_set);
  $('#setting-model').value = Object.hasOwn(MODEL_LABELS, model) ? model : 'custom';
  $('#custom-model').value = Object.hasOwn(MODEL_LABELS, model) ? '' : model;
  $('#custom-model-label').hidden = $('#setting-model').value !== 'custom';
  $('#setting-reasoning').value = settings.reasoning_effort || 'high';
  $('#setting-mode').value = settings.reasoning_mode || 'standard';
  $('#setting-language').value = settings.language || 'en';
  $('#setting-citation').value = settings.citation_style || 'apa';
  $('#setting-max-tokens').value = settings.max_output_tokens || 12000;
  $('#remember-key').checked = !!(settings.key_storage && !['session', 'memory', 'none', 'environment'].includes(settings.key_storage));
  $('#remember-key').disabled = settings.can_remember_key === false;
  $('#key-help').textContent = settings.api_key_set ? `A key is configured${settings.key_storage ? ` (${settings.key_storage})` : ''}. Leave the key field blank to keep it. The key is never returned to this page.` : 'Use an OpenAI Platform API key with access to your chosen model. Your key is never stored in browser storage.';
}
async function saveSettings({ close = true } = {}) {
  const model = $('#setting-model').value === 'custom' ? $('#custom-model').value.trim() : $('#setting-model').value;
  if (!model) throw new Error('Enter a model ID or choose a listed model.');
  const data = {
    model, reasoning_effort: $('#setting-reasoning').value, reasoning_mode: $('#setting-mode').value,
    language: $('#setting-language').value, citation_style: $('#setting-citation').value,
    max_output_tokens: Number($('#setting-max-tokens').value), remember_key: $('#remember-key').checked,
  };
  const key = $('#api-key').value.trim();
  if (key) data.api_key = key;
  const result = await api('/api/settings', { method: 'PUT', data });
  $('#api-key').value = '';
  renderSettings(result.settings || result);
  if (close) { $('#settings-dialog').close(); toast('Settings saved.'); }
  return result;
}
async function testConnection() {
  const button = $('#test-key'); button.disabled = true;
  const output = $('#settings-test-result'); output.hidden = false; output.className = 'test-result'; output.textContent = 'Saving settings and checking model access…';
  try {
    await saveSettings({ close: false });
    const result = await api('/api/settings/test', { method: 'POST', data: {} });
    output.textContent = result.message || (result.ok ? 'Connection successful. Your API key is ready.' : 'Connection could not be verified.');
    output.classList.toggle('error', !result.ok);
  } catch (error) { output.textContent = error.message; output.classList.add('error'); }
  finally { button.disabled = false; }
}

function renderStats(stats = {}) {
  $('#source-count').textContent = formatNumber(stats.documents ?? state.sources.length);
  $('#indexed-count').textContent = formatNumber(stats.indexed_documents);
  $('#chunk-count').textContent = formatNumber(stats.chunks);
  $('#library-total').textContent = formatNumber(stats.documents ?? state.sources.length);
  $('#library-summary').textContent = `${formatNumber(stats.indexed_documents)} indexed sources · ${formatNumber(stats.chunks)} passages · ${formatNumber(stats.embedded_chunks)} semantic passages`;
}
async function refreshLibrary() {
  const result = await api('/api/library');
  state.sources = result.documents || [];
  const validIds = new Set(state.sources.map((item) => item.id));
  state.scope = new Set([...state.scope].filter((id) => validIds.has(id)));
  renderStats(result.stats); renderLibraryList(); renderScope();
  return result;
}
async function openLibrary() {
  showDialog('#library-dialog');
  $('#source-list').replaceChildren(e('p', { class: 'loading-text', text: 'Loading your source library…' }));
  await refreshLibrary();
}
function renderScope() {
  $('#scope-button').textContent = state.scope.size ? `${state.scope.size} selected source${state.scope.size === 1 ? '' : 's'}` : 'All sources';
  $('#scope-button').title = state.scope.size ? 'Only selected books are used for new assistant requests. Click to change.' : 'Search all indexed books. Click to choose sources.';
}
function renderLibraryList() {
  const container = $('#source-list'); container.replaceChildren();
  const filter = $('#library-search').value.trim().toLocaleLowerCase();
  const sources = state.sources.filter((doc) => !filter || `${doc.title} ${doc.filename} ${doc.authors} ${doc.year}`.toLocaleLowerCase().includes(filter));
  if (!sources.length) {
    container.append(e('div', { class: 'source-empty' }, e('h3', { text: state.sources.length ? 'No matching source titles.' : 'Your library starts here.' }), e('p', { text: state.sources.length ? 'Try another title, or search passages to look inside the books.' : 'Add PDF or Word sources, then index them. Each answer can point you back to a passage in your own reading material.' })));
    return;
  }
  for (const doc of sources) {
    const checkbox = e('input', { type: 'checkbox', checked: state.scope.has(doc.id), 'aria-label': `Use ${doc.title || doc.filename} as a source`, onchange: () => {
      checkbox.checked ? state.scope.add(doc.id) : state.scope.delete(doc.id); renderScope();
    } });
    const status = String(doc.status || 'pending');
    const details = e('div', { class: 'source-detail' },
      e('span', { class: `source-status ${status.replace(/[^a-z_-]/gi, '')}`, text: status.replaceAll('_', ' ') }),
      e('span', { text: `${formatNumber(doc.chunks)} passages` }),
      e('span', { text: `${formatNumber(doc.embedded_chunks)} embedded` }),
      e('span', { text: doc.verified ? 'Metadata verified' : 'Check metadata' }),
    );
    const info = e('div', {}, e('h4', { text: doc.title || doc.filename }), details);
    if (doc.filename && doc.filename !== doc.title) info.append(e('p', { class: 'muted small', text: doc.filename }));
    if (doc.error) info.append(e('p', { text: doc.error }));
    if (doc.warnings?.length) info.append(e('p', { text: doc.warnings.slice(0, 3).join(' · ') }));
    const buttons = e('div', { class: 'source-row-buttons' },
      e('button', { class: 'button', text: 'Details & citation', onclick: () => openMetadata(doc.id) }),
      e('a', { class: 'button', text: 'Open ↗', href: sourceLink(doc), target: '_blank', rel: 'noopener' }),
    );
    container.append(e('div', { class: 'source-row' }, checkbox, info, buttons));
  }
}
async function uploadSources(files) {
  const items = [...files];
  if (!items.length) return;
  const valid = items.filter((file) => /\.(pdf|docx)$/i.test(file.name));
  if (valid.length !== items.length) toast('Only PDF and DOCX files are supported. Other files were skipped.', 'warning');
  if (!valid.length) return;
  const body = new FormData(); valid.forEach((file) => body.append('files', file));
  $('#upload-button').disabled = true;
  try {
    const result = await api('/api/library/upload', { method: 'POST', body });
    const count = result.uploaded?.length || 0;
    toast(`${count} source${count === 1 ? '' : 's'} added. Choose an indexing option to make them searchable.`);
    if (result.errors?.length) toast(result.errors.map((item) => typeof item === 'string' ? item : `${item.filename || item.name || 'File'}: ${item.error || item.detail || 'Upload failed'}`).join(' · '), 'warning', 15000);
    await refreshLibrary();
  } finally { $('#upload-button').disabled = false; $('#source-upload-input').value = ''; }
}
function progressDescription(progress = {}) {
  if (typeof progress === 'string') return progress;
  const stages = { extracting: 'Reading source text…', embedding: 'Building the semantic index…', file_complete: 'Source indexed', complete: 'Indexing finished', cancelled: 'Indexing cancelled' };
  return progress.message || progress.detail || stages[progress.phase] || progress.stage || progress.status || 'Working…';
}
async function pollJob(id, onProgress) {
  let cycle = 0, failures = 0;
  while (true) {
    let job;
    try { job = await api(`/api/jobs/${encodeURIComponent(id)}`); failures = 0; }
    catch (error) {
      if ((error.status === 0 || error.status >= 500) && failures++ < 3) { await sleep(1600); continue; }
      if (error.status === 0) error.message += ' The task may still be running. Reload the page to reconnect.';
      throw error;
    }
    onProgress?.(job);
    if (['completed', 'done', 'succeeded'].includes(job.status)) return job.result;
    if (job.status === 'failed') throw new Error(job.error || 'The task failed. Please review your settings and source files.');
    if (job.status === 'cancelled') throw new APIError('Task cancelled. Completed work has been retained.', 499);
    await sleep(cycle++ < 6 ? 700 : 1400);
  }
}
async function startIndex(semantic) {
  if (state.indexJob) return;
  if (semantic && !state.settings.api_key_set) { showDialog('#settings-dialog'); toast('Add your OpenAI API key before building a semantic index.', 'warning'); return; }
  $('#index-local').disabled = true; $('#index-semantic').disabled = true;
  $('#index-progress').hidden = false; $('#index-progress-text').textContent = 'Preparing your sources…';
  $('#index-progress-bar').removeAttribute('value'); $('#index-progress-detail').textContent = '';
  try {
    const { job_id } = await api('/api/library/index', { method: 'POST', data: { semantic, ocr: $('#index-ocr').checked, force: false } });
    state.indexJob = job_id;
    const result = await pollJob(job_id, (job) => {
      const p = job.progress || {};
      $('#index-progress-text').textContent = progressDescription(p);
      const current = p.current ?? p.processed ?? p.completed_files;
      const total = p.total ?? p.total_files;
      if (Number(total) > 0 && Number.isFinite(Number(current))) { $('#index-progress-bar').max = Number(total); $('#index-progress-bar').value = Number(current); }
      $('#index-progress-detail').textContent = [p.filename || p.file, total ? `${current || 0} of ${total}` : '', p.chunks ? `${formatNumber(p.chunks)} passages` : ''].filter(Boolean).join(' · ');
    });
    $('#index-progress-text').textContent = 'Indexing finished';
    $('#index-progress-bar').max = 100; $('#index-progress-bar').value = 100;
    $('#index-progress-detail').textContent = result?.message || 'Review the source list for extraction warnings or files needing attention.';
    toast('Indexing finished. Your library is ready to search.');
  } catch (error) {
    $('#index-progress-text').textContent = error.status === 499 ? 'Indexing cancelled' : 'Indexing needs attention';
    $('#index-progress-detail').textContent = error.message;
    if (error.status !== 499) fail(error);
  } finally {
    state.indexJob = null; $('#index-local').disabled = false; $('#index-semantic').disabled = false;
    $('#cancel-index').disabled = false; await refreshLibrary().catch(fail);
  }
}
async function cancelJob(type) {
  const id = type === 'index' ? state.indexJob : state.assistantJob;
  if (!id) return;
  await api(`/api/jobs/${encodeURIComponent(id)}/cancel`, { method: 'POST', data: {} });
  if (type === 'index') { $('#cancel-index').disabled = true; $('#index-progress-text').textContent = 'Stopping after the current operation…'; }
  else $('#assistant-progress-text').textContent = 'Stopping…';
}
async function searchLibrary() {
  const query = $('#library-search').value.trim();
  if (!query) { $('#library-results').replaceChildren(); return; }
  const target = $('#library-results');
  target.replaceChildren(e('p', { class: 'loading-text', text: 'Searching indexed passages…' }));
  $('#library-search-button').disabled = true;
  try {
    const result = await api(`/api/library/search?q=${encodeURIComponent(query)}&semantic=false`);
    target.replaceChildren(e('div', { class: 'search-results-heading' }, e('span', { text: `${result.results?.length || 0} matching passages · keyword search` }), e('button', { class: 'subtle-button', text: 'Clear results', onclick: () => { target.replaceChildren(); $('#library-search').value = ''; renderLibraryList(); } })));
    for (const source of result.results || []) target.append(e('div', { class: 'library-results-card' }, makeEvidenceCard(source)));
    if (!result.results?.length) target.append(e('p', { class: 'small muted', text: 'No text matches. Check the indexing status or try different words. The assistant can use semantic retrieval when an index is available.' }));
  } catch (error) { target.replaceChildren(e('p', { class: 'error-text', text: error.message })); }
  finally { $('#library-search-button').disabled = false; }
}
function openMetadata(id) {
  const doc = state.sources.find((item) => item.id === id);
  if (!doc) return;
  state.metadataId = id;
  $('#metadata-filename').textContent = doc.filename || doc.title;
  for (const [element, key] of [['title','title'],['authors','authors'],['year','year'],['edition','edition'],['publisher','publisher'],['doi','doi'],['key','citation_key'],['offset','page_offset']]) $('#meta-' + element).value = doc[key] ?? '';
  $('#meta-verified').checked = !!doc.verified;
  $('#meta-offset').disabled = /\.docx$/i.test(doc.filename || '');
  $('#metadata-issues').hidden = !doc.metadata_issues?.length;
  $('#metadata-issues').textContent = doc.metadata_issues?.join(' · ') || '';
  showDialog('#metadata-dialog');
}
async function saveMetadata() {
  if (!state.metadataId) return;
  const patch = {};
  for (const [element, key] of [['title','title'],['authors','authors'],['year','year'],['edition','edition'],['publisher','publisher'],['doi','doi'],['key','citation_key']]) patch[key] = $('#meta-' + element).value.trim();
  patch.page_offset = $('#meta-offset').value === '' ? null : Number($('#meta-offset').value);
  patch.verified = $('#meta-verified').checked;
  await api(`/api/library/${encodeURIComponent(state.metadataId)}`, { method: 'PATCH', data: patch });
  $('#metadata-dialog').close(); await refreshLibrary(); toast('Source metadata saved. New answers will use the updated details.');
}

function citationText(source) { return source.formatted_citation || source.citation || ''; }
function makeEvidenceCard(source) {
  const label = source.label ? `${source.label} · ` : '';
  const card = e('article', { class: 'evidence-card' }, e('h4', { text: label + (source.title || source.filename || 'Source') }));
  card.append(e('div', { class: 'source-location', text: [source.authors, source.year, source.location || (source.page ? `PDF page ${source.page}` : 'Source passage')].filter(Boolean).join(' · ') }));
  if (source.quote || source.text) card.append(e('blockquote', { text: (source.quote || source.text).slice(0, 1800) }));
  if (source.quote) card.append(e('div', { class: 'quote-status', text: source.quote_verified ? 'Quoted text found in the extracted source.' : 'Quote not verified verbatim; check the source.' }));
  const actions = e('div', { class: 'source-actions' });
  if (source.chunk_id) actions.append(e('button', { text: 'Read context', onclick: () => openEvidence(source).catch(fail) }));
  if (source.document_id || source.id) actions.append(e('a', { text: 'Open original ↗', href: sourceLink(source), target: '_blank', rel: 'noopener' }));
  const ready = source.citation_ready === true && !!citationText(source);
  const issues = source.metadata_issues?.join(' · ') || 'Verify bibliographic details and page numbering in the source metadata.';
  actions.append(e('button', { text: 'Copy citation', disabled: !ready, title: ready ? citationText(source) : issues, onclick: () => copyText(citationText(source)) }));
  card.append(actions);
  if (!ready) card.append(e('div', { class: 'citation-issues', text: issues }));
  return card;
}
async function openEvidence(source) {
  $('#evidence-title').textContent = source.title || source.filename || 'Source passage';
  $('#evidence-open-file').href = sourceLink(source);
  const content = $('#evidence-content');
  content.replaceChildren(e('p', { class: 'loading-text', text: 'Opening the passage and its surrounding context…' }));
  showDialog('#evidence-dialog');
  const result = await api(`/api/library/chunks/${encodeURIComponent(source.chunk_id)}`);
  content.replaceChildren(e('div', { class: 'evidence-source-details', text: [source.authors, source.year, source.filename].filter(Boolean).join(' · ') }));
  for (const chunk of result.chunks || []) {
    content.append(e('section', { class: `evidence-context ${chunk.chunk_id === source.chunk_id ? 'focused' : ''}` },
      e('h3', {}, e('span', { text: chunk.location || 'Source passage' }), chunk.chunk_id === source.chunk_id ? e('span', { text: 'CITED PASSAGE' }) : null),
      e('p', { text: chunk.text }),
    ));
  }
  if (!result.chunks?.length) content.append(e('p', { class: 'error-text', text: 'This source version is no longer in the current index. Open the original file to verify it.' }));
}
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); toast('Copied to clipboard.'); }
  catch {
    const area = e('textarea', { value: text });
    area.style.position = 'fixed'; area.style.opacity = '0'; document.body.append(area); area.select();
    const copied = document.execCommand('copy'); area.remove();
    if (copied) toast('Copied to clipboard.'); else toast('Clipboard access was blocked. Select the text and copy it manually.', 'warning');
  }
}

// Assistant output is rendered as text, never trusted HTML. This small renderer
// handles only headings, bullet paragraphs and bold spans with safe DOM nodes.
function richText(text) {
  const root = e('div', { class: 'message-text' });
  for (const line of String(text || '').split('\n')) {
    const row = e('div');
    const heading = line.match(/^#{1,4}\s+(.+)/);
    const value = heading ? heading[1] : line;
    for (const [index, part] of value.split(/\*\*(.+?)\*\*/g).entries()) row.append(index % 2 ? e('strong', { text: part }) : document.createTextNode(part));
    if (heading) row.style.fontWeight = '650';
    if (!line) row.append(e('br'));
    root.append(row);
  }
  return root;
}
function selectionSnapshot() {
  if (!state.selection || !state.current || state.selection.documentId !== state.current.id) return null;
  const { from, to, text } = state.selection;
  try {
    if (state.editor.state.doc.textBetween(from, to, '\n') !== text) return null;
  } catch { return null; }
  return { ...state.selection, contentVersion: state.contentVersion };
}
function canReplace(snapshot) {
  if (!snapshot || !state.current || snapshot.documentId !== state.current.id || snapshot.contentVersion !== state.contentVersion) return false;
  try { return state.editor.state.doc.textBetween(snapshot.from, snapshot.to, '\n') === snapshot.text; }
  catch { return false; }
}
function textNodes(text) {
  return String(text).split(/\n\s*\n/).map((paragraph) => ({
    type: 'paragraph', content: paragraph.split('\n').flatMap((line, i) => [
      ...(i ? [{ type: 'hardBreak' }] : []), ...(line ? [{ type: 'text', text: line }] : []),
    ]),
  }));
}
function replacementNodes(text) {
  const paragraphs = textNodes(text);
  return paragraphs.length === 1 ? paragraphs[0].content : paragraphs;
}
function updateSuggestionStates() {
  for (const record of state.suggestions) {
    const valid = canReplace(record.snapshot);
    record.button.disabled = !valid;
    record.note.hidden = valid;
  }
}
function renderAssistantResult(result, snapshot = null, documentId = state.current?.id) {
  const wrapper = e('article', { class: 'chat-message assistant' }, e('div', { class: 'assistant-response-heading', text: '✧  THESISHELPER' }));
  wrapper.append(richText(result.answer || result.content || 'The assistant returned no text.'));
  if (result.suggestion) {
    const suggestion = typeof result.suggestion === 'string' ? result.suggestion : result.suggestion.text || '';
    if (suggestion) {
      const card = e('section', { class: 'suggestion-card' }, e('span', { class: 'eyebrow', text: 'PROPOSED TEXT · REVIEW BEFORE APPLYING' }), e('div', { class: 'suggestion-text', text: suggestion }));
      const replace = e('button', { class: 'button primary', text: 'Replace selection', disabled: !canReplace(snapshot), onclick: () => {
        if (!canReplace(snapshot)) return toast('Your document changed after this request. Select the passage again and ask for a new suggestion.', 'warning');
        state.editor.chain().focus().insertContentAt({ from: snapshot.from, to: snapshot.to }, replacementNodes(suggestion)).run();
        clearSelection(); updateSuggestionStates(); toast('Suggestion applied. Use Undo if you want to restore the original wording.');
      } });
      const append = e('button', { class: 'button', text: 'Append to document', onclick: () => {
        if (!state.current || state.current.id !== documentId) return toast('Open the document used for this request before applying its suggestion.', 'warning');
        state.editor.chain().focus().insertContentAt(state.editor.state.doc.content.size, textNodes(suggestion)).run();
        toast('Suggestion appended. Review its wording and citations.');
      } });
      const note = e('p', { class: 'stale-note', text: snapshot ? 'The document has changed since this request. Copy the suggestion or request a new one to replace a passage.' : 'Select text and request a new suggestion to replace a passage. Saved suggestions can still be copied or appended.', hidden: canReplace(snapshot) });
      card.append(e('div', { class: 'suggestion-actions' }, replace, append, e('button', { class: 'button', text: 'Copy', onclick: () => copyText(suggestion) })), note);
      state.suggestions.push({ button: replace, note, snapshot }); wrapper.append(card);
    }
  }
  for (const warning of result.warnings || []) wrapper.append(e('div', { class: 'message-warning', text: typeof warning === 'string' ? warning : JSON.stringify(warning) }));
  if (result.citations?.length) {
    const evidence = e('details', { class: 'evidence-list', open: true }, e('summary', { text: `${result.citations.length} source passage${result.citations.length === 1 ? '' : 's'} · inspect the evidence` }));
    result.citations.forEach((source) => evidence.append(makeEvidenceCard(source))); wrapper.append(evidence);
  }
  wrapper.append(e('div', { class: 'message-tools' }, e('button', { class: 'subtle-button', text: 'Copy answer', onclick: () => copyText(result.answer || '') })));
  const usage = result.usage || {};
  const tokens = Number(usage.input_tokens || 0) + Number(usage.output_tokens || 0);
  wrapper.append(e('div', { class: 'message-metadata', text: [MODEL_LABELS[result.model] || result.model, tokens ? `${formatNumber(tokens)} tokens` : '', 'Based on retrieved passages; coverage is not exhaustive'].filter(Boolean).join(' · ') }));
  return wrapper;
}
function appendUserMessage(content) {
  $('#assistant-empty')?.remove();
  $('#chat-feed').append(e('article', { class: 'chat-message user' }, e('div', { class: 'message-label', text: 'YOU' }), e('div', { class: 'message-text', text: content })));
}
async function loadChat(documentId) {
  const result = await api(`/api/chat?document_id=${encodeURIComponent(documentId)}`);
  if (state.current?.id !== documentId) return;
  state.activeChatDocument = documentId; state.suggestions = [];
  const feed = $('#chat-feed');
  const messages = result.messages || [];
  if (!messages.length) {
    feed.replaceChildren(makeChatEmpty());
  } else {
    feed.replaceChildren();
    for (const message of messages) {
      if (message.role === 'user') appendUserMessage(message.content || '');
      else if (message.role === 'assistant') {
        let response = message.result || { answer: message.content || '' };
        if (typeof response === 'string') { try { response = JSON.parse(response); } catch { response = { answer: response }; } }
        feed.append(renderAssistantResult(response, null, documentId));
      }
    }
  }
  feed.scrollTop = feed.scrollHeight;
}
function makeChatEmpty() {
  const empty = e('div', { class: 'assistant-empty', id: 'assistant-empty' },
    e('div', { class: 'empty-orbit', text: '✧' }),
    e('span', { class: 'eyebrow', text: 'A RESEARCH PARTNER' }),
    e('h3', {}, 'Build on what', e('br'), 'you have read.'),
    e('p', { text: 'Ask a question about your sources, or select a passage in your thesis to work on it together.' }),
  );
  for (const [label, prompt] of [
    ['Find the central themes', 'Help me identify the central themes in my source library.'],
    ['Develop a research question', 'Help me develop a clear research question using my source library.'],
  ]) empty.append(e('button', { class: 'example-prompt', onclick: () => { $('#chat-message').value = prompt; $('#chat-message').focus(); } }, label, e('span', { text: '↗' })));
  empty.append(e('p', { class: 'trust-note', text: 'Every source is yours. Check the cited passage before using a claim.' }));
  return empty;
}
function setAssistantBusy(busy) {
  $('#send-button').disabled = busy;
  $('#assistant-progress').hidden = !busy;
  $$('[data-action]').forEach((button) => { button.disabled = busy; });
}
async function runAssistant(actionOverride) {
  if (state.assistantJob || state.assistantPending) return;
  if (!state.settings.api_key_set) { showDialog('#settings-dialog'); toast('Add your OpenAI API key to start using the assistant.', 'warning'); return; }
  if (!state.current) return toast('Create or import a working document first.', 'warning');
  const action = actionOverride || $('#assistant-action').value;
  let message = $('#chat-message').value.trim();
  const snapshot = selectionSnapshot();
  if (state.selection && !snapshot) return toast('The selected passage has changed. Select it again before sending your request.', 'warning');
  if (action !== 'ask' && !snapshot && !message) return toast('Select a passage or type the text you want to work on.', 'warning');
  if (!message && action === 'ask') return $('#chat-message').focus();
  const defaults = { evidence: 'Find relevant evidence for this passage in my source library.', rewrite: 'Suggest a clear academic rewrite that preserves the original meaning and uses only supported factual claims.', citations: 'Check the claims, supporting sources, and citations in this passage. Identify missing or uncertain evidence.', critique: 'Critique this argument for clarity, logic, scientific precision, and support from my sources.', summarize: 'Summarize this passage accurately and identify its main claims.' };
  message ||= defaults[action] || 'Help me with this passage.';
  if (snapshot?.text.length > 16000) return toast('Please select a shorter passage (up to 16,000 characters) so the assistant can examine it carefully.', 'warning');
  const docId = state.current.id;
  state.assistantPending = true;
  setAssistantBusy(true);
  $('#assistant-progress-text').textContent = 'Saving your document…';
  let userMessageRendered = false;
  try {
    await saveDocument();
    if (state.current?.id !== docId || (snapshot && !canReplace(snapshot))) throw new Error('Your document changed while this request was being prepared. Select the passage and send it again.');
    // Saving does not change the editor's content version; edits do.
    $('#assistant-progress-text').textContent = 'Searching your source library…';
    const result = await api('/api/assistant', { method: 'POST', data: {
      document_id: docId, action, message,
      selection: snapshot?.text || '', context: state.editor.getText().slice(0, 24000),
      document_ids: [...state.scope], selection_revision: snapshot ? state.current.revision : null,
    } });
    state.assistantJob = result.job_id;
    appendUserMessage(message); userMessageRendered = true;
    $('#chat-message').value = '';
    $('#chat-feed').scrollTop = $('#chat-feed').scrollHeight;
    const response = await pollJob(result.job_id, (job) => { $('#assistant-progress-text').textContent = progressDescription(job.progress || { message: 'Reading and reasoning about the evidence…' }); });
    if (state.current?.id === docId) {
      $('#chat-feed').append(renderAssistantResult(response, snapshot, docId));
      $('#chat-feed').scrollTop = $('#chat-feed').scrollHeight;
    } else toast('Your assistant response is ready in its original document.');
  } catch (error) {
    if (error.status === 499) toast(error.message, 'warning'); else fail(error);
    if (userMessageRendered && state.current?.id === docId) $('#chat-feed').append(e('div', { class: 'message-warning', text: error.message }));
  } finally { state.assistantJob = null; state.assistantPending = false; setAssistantBusy(false); }
}

// Refreshing the browser never starts a second paid task. Reconnect to the jobs
// already running in the local server, then load their persisted result.
function reconnectJobs(jobs = []) {
  for (const job of jobs) {
    if (job.kind === 'index' && !state.indexJob) {
      state.indexJob = job.id;
      $('#index-local').disabled = true; $('#index-semantic').disabled = true;
      $('#index-progress').hidden = false;
      pollJob(job.id, (current) => {
        const p = current.progress || {};
        $('#index-progress-text').textContent = progressDescription(p);
        $('#index-progress-detail').textContent = p.filename || '';
        if (p.total) { $('#index-progress-bar').max = p.total; $('#index-progress-bar').value = p.current || 0; }
      }).then(() => {
        $('#index-progress-text').textContent = 'Indexing finished';
        $('#index-progress-bar').max = 100; $('#index-progress-bar').value = 100;
        toast('Your source index is ready.');
      }).catch((error) => {
        $('#index-progress-text').textContent = error.status === 499 ? 'Indexing cancelled' : 'Indexing needs attention';
        $('#index-progress-detail').textContent = error.message;
        if (error.status !== 499) fail(error);
      }).finally(() => {
        state.indexJob = null; $('#index-local').disabled = false; $('#index-semantic').disabled = false;
        $('#cancel-index').disabled = false; refreshLibrary().catch(fail);
      });
    } else if (job.kind === 'assistant' && !state.assistantJob) {
      state.assistantJob = job.id; setAssistantBusy(true);
      const documentId = String(job.key || '').replace(/^assistant:/, '');
      pollJob(job.id, (current) => { $('#assistant-progress-text').textContent = progressDescription(current.progress); })
        .then(async () => {
          if (state.current?.id === documentId) await loadChat(documentId);
          else toast('An assistant response is ready in its original document.');
        }).catch((error) => { if (error.status !== 499) fail(error); })
        .finally(() => { state.assistantJob = null; setAssistantBusy(false); });
    }
  }
}

async function openHistory() {
  if (!state.current) return toast('Create or import a document first.', 'warning');
  await saveDocument();
  showDialog('#history-dialog');
  const container = $('#history-list'); container.replaceChildren(e('p', { class: 'loading-text', text: 'Loading saved versions…' }));
  const result = await api(`/api/documents/${encodeURIComponent(state.current.id)}/history`);
  container.replaceChildren();
  for (const version of result.versions || []) {
    const current = version.revision === state.current.revision;
    container.append(e('div', { class: 'history-row' },
      e('div', {}, e('strong', { text: `Revision ${version.revision}${current ? ' · current' : ''}` }), e('p', { text: `${version.title || state.current.title} · ${friendlyDate(version.updated_at)}` })),
      e('button', { class: 'button', text: current ? 'Current' : 'Restore', disabled: current, onclick: async () => {
        try {
          await saveDocument();
          const restored = await api(`/api/documents/${encodeURIComponent(state.current.id)}/restore`, { method: 'POST', data: { revision: version.revision, current_revision: state.current.revision } });
          $('#history-dialog').close(); await refreshDocuments(); await loadDocument(restored.id, { skipSave: true });
          toast(`Revision ${version.revision} restored as a new version.`);
        } catch (error) { fail(error); }
      } }),
    ));
  }
  if (!result.versions?.length) container.append(e('p', { class: 'small muted', text: 'Saved versions will appear here after you make changes.' }));
}

function bindEvents() {
  $$('.close-dialog').forEach((button) => button.addEventListener('click', () => button.closest('dialog').close()));
  $$('dialog').forEach((dialog) => dialog.addEventListener('click', (event) => {
    if (event.target !== dialog) return;
    const box = dialog.getBoundingClientRect();
    if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) dialog.close();
  }));
  $('#open-library').addEventListener('click', () => openLibrary().catch(fail));
  $('#mobile-library').addEventListener('click', () => openLibrary().catch(fail));
  $('#mobile-documents').addEventListener('click', () => showDialog('#documents-dialog'));
  $('#mobile-new').addEventListener('click', () => { $('#documents-dialog').close(); $('#new-document-title').value = ''; showDialog('#new-document-dialog'); });
  $('#mobile-import').addEventListener('click', () => { $('#documents-dialog').close(); $('#import-input').click(); });
  $('#mobile-history').addEventListener('click', () => { $('#documents-dialog').close(); openHistory().catch(fail); });
  $('#scope-button').addEventListener('click', () => openLibrary().catch(fail));
  $('#nav-writing').addEventListener('click', () => state.editor.commands.focus());
  $('#open-settings').addEventListener('click', () => showDialog('#settings-dialog'));
  $('#model-indicator').addEventListener('click', () => showDialog('#settings-dialog'));
  $('#settings-dialog').addEventListener('close', () => { $('#api-key').value = ''; });
  $('#setting-model').addEventListener('change', () => { $('#custom-model-label').hidden = $('#setting-model').value !== 'custom'; });
  $('#settings-form').addEventListener('submit', (event) => { event.preventDefault(); saveSettings().catch(fail); });
  $('#test-key').addEventListener('click', testConnection);
  $('#document-title').addEventListener('input', () => { $('#document-breadcrumb').textContent = $('#document-title').value || 'Untitled thesis'; markDirty(); });
  $('#save-document').addEventListener('click', () => saveDocument().then(() => toast('Document saved.')).catch(fail));
  $('#export-document').addEventListener('click', () => exportDocument().catch(fail));
  $('#history-button').addEventListener('click', () => openHistory().catch(fail));
  $('#new-document').addEventListener('click', () => { $('#new-document-title').value = ''; showDialog('#new-document-dialog'); $('#new-document-title').focus(); });
  $('#new-document-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const title = $('#new-document-title').value.trim(); if (!title) return;
    const button = event.submitter; button.disabled = true;
    try { await createDocument(title); $('#new-document-dialog').close(); }
    catch (error) { fail(error); }
    finally { button.disabled = false; }
  });
  $('#import-document').addEventListener('click', () => $('#import-input').click());
  $('#import-input').addEventListener('change', (event) => importDocument(event.target.files[0]).catch(fail));
  $$('[data-format]').forEach((button) => {
    button.addEventListener('mousedown', (event) => event.preventDefault());
    button.addEventListener('click', () => {
      const chain = state.editor.chain().focus();
      const actions = { bold: () => chain.toggleBold(), italic: () => chain.toggleItalic(), underline: () => chain.toggleUnderline(), bulletList: () => chain.toggleBulletList(), orderedList: () => chain.toggleOrderedList(), blockquote: () => chain.toggleBlockquote(), table: () => chain.insertTable({ rows: 3, cols: 3, withHeaderRow: true }), undo: () => chain.undo(), redo: () => chain.redo() };
      actions[button.dataset.format]?.().run();
    });
  });
  $('#text-format').addEventListener('change', (event) => {
    const chain = state.editor.chain().focus();
    if (event.target.value === 'paragraph') chain.setParagraph().run();
    else chain.setHeading({ level: Number(event.target.value.slice(1)) }).run();
  });
  $$('[data-table]').forEach((button) => button.addEventListener('click', () => state.editor.chain().focus()[button.dataset.table]().run()));
  $('#clear-selection').addEventListener('click', clearSelection);
  $$('[data-action]').forEach((button) => button.addEventListener('click', () => {
    $('#assistant-action').value = button.dataset.action;
    runAssistant(button.dataset.action).catch(fail);
  }));
  $$('.example-prompt').forEach((button) => button.addEventListener('click', () => { $('#chat-message').value = button.dataset.prompt; $('#chat-message').focus(); }));
  $('#chat-form').addEventListener('submit', (event) => { event.preventDefault(); runAssistant().catch(fail); });
  $('#chat-message').addEventListener('keydown', (event) => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); runAssistant().catch(fail); } });
  $('#cancel-assistant').addEventListener('click', () => cancelJob('assistant').catch(fail));
  $('#upload-button').addEventListener('click', () => $('#source-upload-input').click());
  $('#source-upload-input').addEventListener('change', (event) => uploadSources(event.target.files).catch(fail));
  const dropzone = $('#source-dropzone');
  ['dragenter', 'dragover'].forEach((name) => dropzone.addEventListener(name, (event) => { event.preventDefault(); dropzone.classList.add('dragover'); }));
  ['dragleave', 'drop'].forEach((name) => dropzone.addEventListener(name, (event) => { event.preventDefault(); dropzone.classList.remove('dragover'); }));
  dropzone.addEventListener('drop', (event) => uploadSources(event.dataTransfer.files).catch(fail));
  $('#index-local').addEventListener('click', () => startIndex(false).catch(fail));
  $('#index-semantic').addEventListener('click', () => startIndex(true).catch(fail));
  $('#cancel-index').addEventListener('click', () => cancelJob('index').catch(fail));
  $('#library-search').addEventListener('input', renderLibraryList);
  $('#library-search').addEventListener('keydown', (event) => { if (event.key === 'Enter') { event.preventDefault(); searchLibrary().catch(fail); } });
  $('#library-search-button').addEventListener('click', () => searchLibrary().catch(fail));
  $('#clear-source-scope').addEventListener('click', () => { state.scope.clear(); renderScope(); renderLibraryList(); });
  $('#metadata-form').addEventListener('submit', (event) => { event.preventDefault(); saveMetadata().catch(fail); });
  $$('#metadata-form input:not([type="checkbox"])').forEach((input) => input.addEventListener('input', () => { $('#meta-verified').checked = false; }));
  $('#metadata-open-file').addEventListener('click', () => {
    if (state.metadataId) window.open(`/api/library/${encodeURIComponent(state.metadataId)}/file`, '_blank', 'noopener');
  });
  window.addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') { event.preventDefault(); saveDocument().catch(fail); }
  });
  window.addEventListener('beforeunload', (event) => {
    if (state.contentVersion !== state.savedVersion || state.savePromise) { stashRecovery(); event.preventDefault(); event.returnValue = ''; }
  });
  document.addEventListener('visibilitychange', () => { if (document.hidden && state.current) saveDocument().catch(() => {}); });
}

async function initialize() {
  makeEditor(); bindEvents();
  $('#document-title').disabled = true;
  try {
    const initial = await api('/api/state');
    renderSettings(initial.settings || {}); renderStats(initial.library || {});
    state.documents = initial.documents || []; renderDocumentList();
    const id = initial.active_document_id || state.documents[0]?.id;
    if (id) await loadDocument(id, { skipSave: true });
    else {
      // A blank, local working document is a useful first-run default.
      await createDocument('My thesis');
    }
    if (!state.settings.api_key_set) {
      $('#key-indicator').textContent = 'Add API key';
      toast('Your writing desk is ready. Add sources in the library and connect your OpenAI key in Settings.', '', 10000);
    }
    reconnectJobs(initial.jobs || []);
  } catch (error) {
    setSaveStatus('Connection unavailable', 'error');
    fail(error);
    const warning = $('#save-warning'); warning.hidden = false;
    warning.replaceChildren(e('span', { text: error.message }), e('button', { class: 'button', text: 'Retry connection', onclick: () => location.reload() }));
  }
}
initialize();
