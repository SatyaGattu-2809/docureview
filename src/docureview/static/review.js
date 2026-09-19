'use strict';
let credential = '', principal = null, selected = null, originalUrl = null;
let generation = 0, offset = 0, uploadKey = null, activeJob = null;
const $ = id => document.getElementById(id);
const headerFields = ['invoice_number','vendor','invoice_date','currency','subtotal','tax','shipping','discount','total'];
function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function message(text, error = false) { $('message').textContent = text; $('message').className = error ? 'error' : ''; }
async function api(path, options = {}) {
  if (!credential) throw new Error('Connect with your API key first.');
  const response = await fetch(path, {...options, headers: {...options.headers, 'X-API-Key': credential}});
  if (!response.ok) {
    let detail; try { detail = (await response.json()).detail; } catch { /* No JSON error body. */ }
    throw new Error(typeof detail === 'string' ? detail : `Request failed (${response.status}). Check required fields and invoice arithmetic.`);
  }
  return response;
}
function clearDocument() {
  selected = null;
  if (originalUrl) URL.revokeObjectURL(originalUrl);
  originalUrl = null;
  $('detail').replaceChildren(element('p', 'Select an invoice to compare its source and extracted fields.'));
}
function logout() {
  generation++; credential = ''; principal = null; activeJob = null; uploadKey = null;
  $('key').value = ''; $('file').value = ''; $('queue').replaceChildren(); clearDocument();
  $('identity').textContent = 'Disconnected. Credentials and displayed documents cleared.';
  message('');
}
$('disconnect').onclick = logout;
$('connect').onsubmit = async event => {
  event.preventDefault(); const next = $('key').value; logout(); credential = next;
  try {
    principal = await (await api('/v1/me')).json();
    $('identity').textContent = `${principal.name} · ${principal.tenant} · ${principal.role}`;
    message('Connected.'); await refresh();
  } catch (error) { logout(); message(error.message, true); }
};
async function refresh(append = false) {
  if (!principal) throw new Error('Connect first.');
  if (principal.role !== 'reviewer') { message('Uploader access: upload and view your processing result. A reviewer must approve it.'); return; }
  if (!append) { offset = 0; $('queue').replaceChildren(); }
  const currentGeneration = generation;
  const docs = await (await api(`/v1/reviews?limit=20&offset=${offset}`)).json();
  if (generation !== currentGeneration) return;
  for (const doc of docs) {
    const title = doc.assessment.normalized_values.invoice_number || doc.id.slice(0, 8);
    const button = element('button', `${title} · ${doc.assessment.normalized_values.vendor || 'Unknown vendor'}`);
    button.onclick = () => openDocument(doc.id).catch(error => message(error.message, true));
    $('queue').append(button);
  }
  offset += docs.length; $('more').hidden = docs.length < 20;
  if (!offset) $('queue').append(element('p', 'No invoices waiting for review.'));
}
$('refresh').onclick = () => refresh().catch(error => message(error.message, true));
$('more').onclick = () => refresh(true).catch(error => message(error.message, true));
$('file').onchange = () => { uploadKey = null; };
$('upload').onsubmit = async event => {
  event.preventDefault(); const file = $('file').files[0]; if (!file) return;
  const button = event.submitter; button.disabled = true;
  const currentGeneration = generation;
  uploadKey ||= crypto.randomUUID();
  try {
    let media = file.type;
    if (file.name.toLowerCase().endsWith('.txt')) media = 'text/plain';
    const job = await (await api('/v1/jobs', {method:'POST', headers:{'Content-Type':media, 'Idempotency-Key':uploadKey}, body:file})).json();
    activeJob = job.id;
    message(`Queued ${job.id}. The background worker must be running.`);
    for (let attempt = 0; attempt < 150 && generation === currentGeneration; attempt++) {
      const state = await (await api(`/v1/jobs/${job.id}`)).json();
      if (state.state === 'succeeded') {
        activeJob = null; uploadKey = null; $('file').value = '';
        await refresh(); await openDocument(state.document_id); message('Extraction ready for human review.'); return;
      }
      if (state.state === 'failed') throw new Error(`${state.error}. Job: ${job.id}`);
      message(`${state.state} · attempt ${state.attempts} · job ${job.id}`);
      await new Promise(resolve => setTimeout(resolve, 2000));
    }
    if (generation === currentGeneration) message(`Job ${activeJob} is still pending. Refresh the review queue later.`);
  } catch (error) { if (generation === currentGeneration) message(error.message, true); }
  finally { button.disabled = false; }
};
function showSource(doc, span, host) {
  host.replaceChildren();
  for (const page of doc.source_pages) {
    host.append(element('h3', `Page ${page.page} · ${page.method}`));
    const pre = element('pre', undefined, 'source');
    if (span && span.page === page.page) {
      pre.append(document.createTextNode(page.text.slice(0, span.start)),
                 element('mark', page.text.slice(span.start, span.end)),
                 document.createTextNode(page.text.slice(span.end)));
    } else pre.textContent = page.text;
    host.append(pre);
  }
}
async function openDocument(id) {
  const currentGeneration = generation;
  const doc = await (await api(`/v1/documents/${id}`)).json();
  if (generation !== currentGeneration) return;
  clearDocument(); selected = doc;
  const detail = $('detail'); detail.replaceChildren();
  detail.append(element('h2', doc.assessment.normalized_values.invoice_number || 'Invoice review'));
  detail.append(element('p', `${doc.status} · version ${doc.version} · expires ${new Date(doc.expires_at).toLocaleString()}`, 'metadata'));
  const grid = element('div', undefined, 'review-grid'); detail.append(grid);
  const sourceColumn = element('section'), fieldColumn = element('section'); grid.append(sourceColumn, fieldColumn);
  sourceColumn.append(element('h3', 'Source document'));
  const originalButton = element('button', 'Open original'); sourceColumn.append(originalButton);
  const preview = element('div', undefined, 'preview'); sourceColumn.append(preview);
  originalButton.onclick = async () => {
    try {
      const blob = await (await api(`/v1/documents/${doc.id}/original`)).blob();
      if (generation !== currentGeneration || selected?.id !== doc.id) return;
      if (originalUrl) URL.revokeObjectURL(originalUrl);
      originalUrl = URL.createObjectURL(blob); preview.replaceChildren();
      const link = element('a', 'Download original'); link.href = originalUrl; link.download = `invoice-${doc.id}`; preview.append(link);
      if (doc.media_type.startsWith('image/')) { const img = element('img'); img.src = originalUrl; img.alt = 'Original uploaded invoice'; preview.append(img); }
      else if (doc.media_type === 'application/pdf') { const frame = element('iframe'); frame.src = originalUrl; frame.title = 'Original invoice PDF'; frame.setAttribute('sandbox', ''); preview.append(frame); }
      else preview.append(element('pre', await blob.text(), 'source'));
    } catch (error) { message(error.message, true); }
  };
  const sourceText = element('div'); sourceColumn.append(sourceText); showSource(doc, null, sourceText);
  fieldColumn.append(element('h3', 'Verify and correct fields'));
  const issues = element('ul', undefined, 'issues');
  for (const issue of doc.assessment.issues) issues.append(element('li', issue));
  fieldColumn.append(issues);
  const inputs = {};
  const initial = doc.final_invoice || doc.assessment.invoice || doc.assessment.normalized_values;
  for (const name of headerFields) {
    const row = element('div', undefined, 'field');
    const label = element('label', name.replaceAll('_', ' ')); label.htmlFor = `field-${name}`;
    const input = element('input'); input.id = `field-${name}`;
    input.value = initial[name] ?? (['shipping','discount'].includes(name) ? '0.00' : '');
    input.disabled = doc.status !== 'needs_review' || principal.role !== 'reviewer';
    inputs[name] = input; row.append(label, input);
    const check = doc.assessment.field_checks.find(item => item.field === name);
    if (check?.source) {
      const evidence = element('button', `Page ${check.source.page}: ${check.source.text}`, 'evidence');
      evidence.onclick = () => showSource(doc, check.source, sourceText); row.append(evidence);
    }
    fieldColumn.append(row);
  }
  const itemSection = element('div', undefined, 'items'); itemSection.append(element('h3', 'Line items (optional)'));
  const table = element('table'); const heading = element('tr');
  const itemFields = ['description','quantity','unit_price','amount'];
  for (const name of itemFields) heading.append(element('th', name.replaceAll('_',' ')));
  heading.append(element('th', '')); table.append(heading); itemSection.append(table);
  const itemRows = [];
  function addItem(item = {}, index = null) {
    const tr = element('tr'), fields = {};
    for (const name of itemFields) {
      const td = element('td'), input = element('input'); input.value = item[name] ?? '';
      input.setAttribute('aria-label', `Line item ${name}`); input.disabled = doc.status !== 'needs_review' || principal.role !== 'reviewer';
      fields[name] = input; td.append(input);
      const check = doc.assessment.field_checks.find(c => c.field === `line_items.${index}.${name}`);
      if (check?.source) { const evidence = element('button', 'Source'); evidence.onclick = () => showSource(doc, check.source, sourceText); td.append(evidence); }
      tr.append(td);
    }
    const remove = element('button','×'); remove.setAttribute('aria-label','Remove line item');
    remove.disabled = doc.status !== 'needs_review' || principal.role !== 'reviewer';
    remove.onclick = () => { tr.remove(); itemRows.splice(itemRows.indexOf(fields),1); };
    const td = element('td'); td.append(remove); tr.append(td); table.append(tr); itemRows.push(fields);
  }
  const lines = initial.line_items || doc.extraction.line_items.map((item,index) => Object.fromEntries(itemFields.map(name => [name,doc.assessment.normalized_values[`line_items.${index}.${name}`] || ''])));
  lines.forEach((item,index) => addItem(item,index));
  const add = element('button', 'Add line'); add.onclick = () => addItem();
  add.disabled = doc.status !== 'needs_review' || principal.role !== 'reviewer'; itemSection.append(add); fieldColumn.append(itemSection);
  const note = element('textarea'); note.setAttribute('aria-label','Review note'); note.placeholder = 'What did you verify or correct?'; fieldColumn.append(note);
  const actions = element('div', undefined, 'actions'); fieldColumn.append(actions);
  for (const decision of ['approved','rejected']) {
    const button = element('button', decision === 'approved' ? 'Approve invoice' : 'Reject', decision === 'rejected' ? 'danger' : '');
    button.disabled = doc.status !== 'needs_review' || principal.role !== 'reviewer';
    button.onclick = async () => {
      for (const child of actions.children) child.disabled = true;
      try {
        const body = {expected_version:doc.version, decision, note:note.value};
        if (decision === 'approved') {
          body.corrected_invoice = Object.fromEntries(headerFields.map(name => [name,inputs[name].value]));
          body.corrected_invoice.line_items = itemRows.map(row => Object.fromEntries(itemFields.map(name => [name,row[name].value])));
        }
        await api(`/v1/documents/${doc.id}/review`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
        await refresh(); await openDocument(doc.id); message(`Invoice ${decision}.`);
      } catch (error) {
        message(`${error.message} Refresh this document before retrying a stale decision.`,true);
        for (const child of actions.children) child.disabled = false;
      }
    };
    actions.append(button);
  }
  const remove = element('button', 'Delete document and original', 'danger');
  remove.disabled = principal.role !== 'reviewer';
  remove.onclick = async () => {
    if (!confirm('Delete this document, original, extraction, and review history?')) return;
    try { await api(`/v1/documents/${doc.id}`, {method:'DELETE'}); clearDocument(); await refresh(); message('Document deleted.'); }
    catch (error) { message(error.message,true); }
  }; actions.append(remove);
}
