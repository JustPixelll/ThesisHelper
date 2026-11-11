// DOM integration tests use a controlled API contract double. They exercise the
// shipped editor and UI logic, but do not claim browser layout or live GPT tests.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { Window } from 'happy-dom';
import { build } from 'esbuild';

const html = await readFile(new URL('../src/index.html', import.meta.url), 'utf8');
const source = await readFile(new URL('../src/app.js', import.meta.url), 'utf8');
const compiled = await build({
  stdin: { contents: source + '\nglobalThis.__UI_TEST = {state, saveDocument, loadDocument, runAssistant, refreshLibrary, makeEvidenceCard, startIndex, openHistory};', resolveDir: fileURLToPath(new URL('../src', import.meta.url)), sourcefile: 'app.js' },
  bundle: true, format: 'iife', write: false, platform: 'browser', logLevel: 'silent',
});
const script = compiled.outputFiles[0].text;
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const clone = (value) => JSON.parse(JSON.stringify(value));

async function setup({ apiKey = true, saveDelay = 0 } = {}) {
  const window = new Window({ url: 'http://127.0.0.1:8765/', settings: { disableCSSFileLoading: true, disableJavaScriptFileLoading: true } });
  const doc = { id: 'thesis-1', title: 'My thesis', content: { type: 'doc', content: [{ type: 'paragraph', content: [{ type: 'text', text: 'Original claim. Another sentence.' }] }] }, revision: 1, updated_at: new Date().toISOString() };
  const settings = { api_key_set: apiKey, key_storage: 'session', can_remember_key: false, model: 'gpt-6-astra', reasoning_effort: 'high', reasoning_mode: 'standard', language: 'en', citation_style: 'apa', max_output_tokens: 12000 };
  const calls = []; const messages = []; const saves = [];
  const book = { id: 'book-1', filename: 'source.pdf', title: 'Source book', authors: 'Smith, Jane', year: '2024', publisher: 'Example Press', doi: '', edition: '', citation_key: 'Smith2024', page_offset: -2, verified: true, status: 'indexed', chunks: 12, embedded_chunks: 0, warnings: [] };
  const evidence = { chunk_id: 'chunk-1', document_id: 'book-1', title: book.title, authors: book.authors, year: book.year, text: 'A supported claim from the source.', location: 'PDF page 3; printed page 1', page: 3, printed_page: 1, source_hash: 'test-hash', filename: 'source.pdf', metadata_verified: true, citation_ready: true, formatted_citation: '(Smith, 2024, p. 1)', metadata_issues: [], label: '[S1]', quote: 'A supported claim', quote_verified: true };
  const response = { answer: 'The selected claim can be expressed more precisely. [S1]', suggestion: 'A supported revision.', citations: [evidence], warnings: [], usage: { input_tokens: 120, output_tokens: 40 }, model: 'gpt-6-astra' };
  const json = (payload, status = 200) => ({ ok: status >= 200 && status < 300, status, headers: { get: () => 'application/json' }, json: async () => clone(payload), text: async () => JSON.stringify(payload) });
  window.fetch = async (path, options = {}) => {
    const method = options.method || 'GET';
    const data = typeof options.body === 'string' ? JSON.parse(options.body) : options.body;
    calls.push({ path, method, data, headers: options.headers });
    assert.equal(options.headers?.['X-ThesisHelper-Request'], '1');
    if (path === '/api/state') return json({ settings, library: { documents: 1, indexed_documents: 1, chunks: 12, embedded_chunks: 0 }, documents: [doc], active_document_id: doc.id, jobs: [] });
    if (path === '/api/documents') return json({ documents: [doc] });
    if (path === '/api/documents/thesis-1' && method === 'GET') return json(doc);
    if (path === '/api/documents/thesis-1' && method === 'PUT') {
      if (saveDelay) await pause(saveDelay);
      if (data.revision !== doc.revision) return json({ detail: 'Revision conflict' }, 409);
      Object.assign(doc, clone(data), { revision: doc.revision + 1 }); saves.push(clone(doc));
      return json(doc);
    }
    if (path === '/api/documents/thesis-1/history') return json({ versions: [{ revision: 1, title: doc.title, updated_at: doc.updated_at }, ...saves] });
    if (path.startsWith('/api/chat?')) return json({ messages });
    if (path === '/api/library') return json({ documents: [book], stats: { documents: 1, indexed_documents: 1, chunks: 12, embedded_chunks: 0 } });
    if (path.startsWith('/api/library/search?')) return json({ results: [evidence] });
    if (path === '/api/library/chunks/chunk-1') return json({ chunks: [evidence] });
    if (path === '/api/library/book-1' && method === 'PATCH') { Object.assign(book, data); return json(book); }
    if (path === '/api/library/index') return json({ job_id: 'index-job' });
    if (path === '/api/jobs/index-job') return json({ id: 'index-job', status: 'completed', progress: {}, result: { indexed: 1 } });
    if (path === '/api/assistant') { messages.push({ role: 'user', content: data.message }); return json({ job_id: 'assistant-job' }); }
    if (path === '/api/jobs/assistant-job') { messages.push({ role: 'assistant', content: response.answer, result: response }); return json({ id: 'assistant-job', status: 'completed', progress: {}, result: response }); }
    if (path === '/api/settings' && method === 'PUT') {
      Object.assign(settings, Object.fromEntries(Object.entries(data).filter(([key]) => !['api_key', 'remember_key'].includes(key))));
      if (data.api_key) settings.api_key_set = true;
      return json(settings);
    }
    throw new Error('Unexpected test request: ' + method + ' ' + path);
  };
  window.document.write(html.replace(/<script[^>]*>.*?<\/script>/gs, '').replace(/<link[^>]*>/g, ''));
  window.eval(script);
  for (let i = 0; i < 50 && !window.__UI_TEST?.state.current; i++) await pause(10);
  assert.equal(window.__UI_TEST.state.current.id, doc.id, 'UI boots against the API contract');
  await pause(30);
  return { window, ui: window.__UI_TEST, doc, settings, calls, saves, evidence, response, close: () => window.happyDOM.abort() };
}

test('editor saves preserve changes made during an in-flight save', async () => {
  const app = await setup({ saveDelay: 45 });
  try {
    app.ui.state.editor.commands.setContent({ type: 'doc', content: [{ type: 'paragraph', content: [{ type: 'text', text: 'First edit.' }] }] });
    const first = app.ui.saveDocument();
    await pause(5);
    app.ui.state.editor.commands.insertContent(' Second edit.');
    const second = app.ui.saveDocument();
    await Promise.all([first, second]);
    assert.equal(app.doc.revision, 3);
    assert.match(JSON.stringify(app.doc.content), /Second edit/);
    assert.equal(app.ui.state.contentVersion, app.ui.state.savedVersion);
    assert.equal(app.window.document.querySelector('#save-warning').hidden, true);
    assert.equal(app.window.localStorage.getItem('thesishelper.recovery.thesis-1'), null);
    assert.ok(app.saves.every((saved, index) => saved.revision === index + 2));
  } finally { await app.close(); }
});

test('highlighted text is sent and replacement updates only the original selection', async () => {
  const app = await setup();
  try {
    app.ui.state.editor.commands.setTextSelection({ from: 1, to: 16 });
    assert.equal(app.window.document.querySelector('#selection-box').hidden, false);
    await app.ui.runAssistant('rewrite');
    const call = app.calls.find((item) => item.path === '/api/assistant');
    assert.equal(call.data.selection, 'Original claim.');
    assert.equal(call.data.selection_revision, 1);
    const replace = [...app.window.document.querySelectorAll('.suggestion-actions button')].find((item) => item.textContent === 'Replace selection');
    assert.equal(replace.disabled, false);
    replace.click();
    assert.equal(app.ui.state.editor.getText(), 'A supported revision. Another sentence.');
    await app.ui.saveDocument();
    assert.equal(replace.disabled, true, 'A suggestion cannot be applied twice');
  } finally { await app.close(); }
});

test('a suggestion cannot overwrite text after the document changes', async () => {
  const app = await setup();
  try {
    app.ui.state.editor.commands.setTextSelection({ from: 1, to: 16 });
    await app.ui.runAssistant('rewrite');
    app.ui.state.editor.commands.insertContentAt(app.ui.state.editor.state.doc.content.size - 1, ' A new thought.');
    const replace = app.window.document.querySelector('.suggestion-actions button');
    assert.equal(replace.disabled, true);
    replace.click();
    assert.match(app.ui.state.editor.getText(), /Original claim/);
    assert.match(app.ui.state.editor.getText(), /A new thought/);
    await app.ui.saveDocument();
  } finally { await app.close(); }
});

test('a configured key never appears in browser recovery storage or a URL', async () => {
  const app = await setup({ apiKey: false });
  try {
    app.window.document.querySelector('#api-key').value = 'sk-synthetic-test-key';
    app.window.document.querySelector('#settings-form').dispatchEvent(new app.window.Event('submit', { bubbles: true, cancelable: true }));
    await pause(30);
    assert.equal(app.window.document.querySelector('#api-key').value, '');
    for (let i = 0; i < app.window.localStorage.length; i++) assert.ok(!app.window.localStorage.getItem(app.window.localStorage.key(i)).includes('sk-synthetic-test-key'));
    assert.ok(app.calls.every((call) => !call.path.includes('sk-synthetic-test-key')));
    assert.equal(app.window.document.querySelector('#key-indicator').textContent, 'Key set');
  } finally { await app.close(); }
});

test('evidence is inert text, and unverified citations cannot be copied', async () => {
  const app = await setup();
  try {
    const card = app.ui.makeEvidenceCard({ ...app.evidence, title: '<script>unsafe()</script>', quote: '<img src=x onerror=unsafe()>', citation_ready: false, metadata_issues: ['Verify the printed page.'] });
    assert.equal(card.querySelector('script'), null); assert.equal(card.querySelector('img'), null);
    assert.match(card.textContent, /<script>/);
    const copy = [...card.querySelectorAll('button')].find((item) => item.textContent === 'Copy citation');
    assert.equal(copy.disabled, true);
    assert.match(card.textContent, /Verify the printed page/);
    const original = card.querySelector('a'); assert.equal(original.getAttribute('href'), '/api/library/book-1/file#page=3');
  } finally { await app.close(); }
});

test('local indexing is available without a key and scopes reach assistant requests', async () => {
  const app = await setup({ apiKey: false });
  try {
    await app.ui.refreshLibrary();
    await app.ui.startIndex(false);
    assert.deepEqual(app.calls.find((call) => call.path === '/api/library/index').data, { semantic: false, ocr: false, force: false });
    const scope = app.window.document.querySelector('.source-row input'); scope.click();
    assert.match(app.window.document.querySelector('#scope-button').textContent, /1 selected/);
    app.ui.state.settings.api_key_set = true;
    app.window.document.querySelector('#chat-message').value = 'What does inclusion mean?';
    await app.ui.runAssistant('ask');
    const call = app.calls.find((item) => item.path === '/api/assistant');
    assert.deepEqual(call.data.document_ids, ['book-1']);
  } finally { await app.close(); }
});

test('formatting and imported alignment survive the editor document model', async () => {
  const app = await setup();
  try {
    app.ui.state.editor.commands.setContent({ type: 'doc', content: [
      { type: 'heading', attrs: { level: 5, textAlign: 'center' }, content: [{ type: 'text', text: 'Detailed subsection' }] },
      { type: 'paragraph', content: [{ type: 'text', text: 'x' }, { type: 'text', text: '2', marks: [{ type: 'superscript' }] }] },
    ] });
    await app.ui.saveDocument();
    assert.equal(app.doc.content.content[0].attrs.level, 5);
    assert.equal(app.doc.content.content[0].attrs.textAlign, 'center');
    assert.equal(app.doc.content.content[1].content[1].marks[0].type, 'superscript');
    assert.equal(app.window.document.querySelector('.ProseMirror h5').textContent, 'Detailed subsection');
  } finally { await app.close(); }
});

test('editing bibliographic fields requires renewed metadata verification', async () => {
  const app = await setup();
  try {
    await app.ui.refreshLibrary();
    [...app.window.document.querySelectorAll('.source-row button')].find((button) => button.textContent === 'Details & citation').click();
    const verified = app.window.document.querySelector('#meta-verified');
    assert.equal(verified.checked, true);
    const authors = app.window.document.querySelector('#meta-authors');
    authors.value = 'Brown, A.';
    authors.dispatchEvent(new app.window.Event('input', { bubbles: true }));
    assert.equal(verified.checked, false);
    app.window.document.querySelector('#metadata-form').dispatchEvent(new app.window.Event('submit', { bubbles: true, cancelable: true }));
    await pause(30);
    const save = app.calls.find((call) => call.path === '/api/library/book-1' && call.method === 'PATCH');
    assert.equal(save.data.verified, false);
  } finally { await app.close(); }
});
