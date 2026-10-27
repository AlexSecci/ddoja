const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync(process.argv[2], 'utf8');
const bindStart = source.indexOf('const bind =');
const bindEnd = source.indexOf('const button =', bindStart);
const toggleScope = {};
vm.runInNewContext(source.slice(bindStart, bindEnd) + '\nglobalThis.bindFixture = bind;', toggleScope);
const detached = { isConnected: false }, connected = { isConnected: true };
let toggles = 0;
toggleScope.bindFixture(detached, 'toggle', () => { toggles += 1; });
detached.ontoggle({ currentTarget: detached });
assert.equal(toggles, 0, 'detached render templates never trigger data fetches or another render');
detached.ontoggle({ currentTarget: connected });
assert.equal(toggles, 1, 'the handler copied to the connected details still handles user toggles');
const start = source.indexOf('function nodeKey(');
const end = source.indexOf("\ndocument.addEventListener('compositionstart'", start);
assert(start >= 0 && end > start, 'UI reconciliation helpers should remain available as one source block');

const document = { activeElement: null };
const window = { scrollX: 0, scrollY: 240, innerHeight: 900, scrollTo(x, y) { this.scrollX = x; this.scrollY = y; } };
const sandbox = { document, window, Set, Map, Array, Boolean, Math };
vm.runInNewContext(source.slice(start, end) + '\nglobalThis.helpers = { nodeKey, reconcileChildren, preserveViewport };', sandbox);
const { reconcileChildren, preserveViewport } = sandbox.helpers;

class FixtureNode {
  constructor(name, type = 1, value = '') {
    this.nodeType = type;
    this.nodeName = name.toUpperCase();
    this.nodeValue = type === 3 ? value : null;
    this.childNodes = [];
    this.parentNode = null;
    this.dataset = {};
    this._attributes = new Map();
    this._value = '';
    this._fileValue = false;
    this.type = 'text';
    this.defaultValue = '';
    this.checked = false;
    this.defaultChecked = false;
    this.selectionStart = 0;
    this.selectionEnd = 0;
    this.selectionDirection = 'none';
    this.open = false;
    this.onclick = this.onsubmit = this.oninput = this.onchange = this.onkeydown = this.ontoggle = null;
    if (name.toUpperCase() === 'INPUT' || name.toUpperCase() === 'TEXTAREA') this.value = value;
  }
  get value() { return this._value; }
  set value(value) {
    if (this.type === 'file' && this._fileValue && value !== this._value) {
      throw new Error('A live file input value must not be assigned during reconciliation.');
    }
    this._value = String(value ?? '');
  }
  get attributes() { return [...this._attributes].map(([name, value]) => ({ name, value })); }
  get firstChild() { return this.childNodes[0] || null; }
  get children() { return this.childNodes.filter(node => node.nodeType === 1); }
  get nextSibling() {
    if (!this.parentNode) return null;
    const siblings = this.parentNode.childNodes, index = siblings.indexOf(this);
    return siblings[index + 1] || null;
  }
  get isConnected() {
    let node = this;
    while (node.parentNode) node = node.parentNode;
    return node === root;
  }
  setAttribute(name, value) { this._attributes.set(name, String(value)); }
  getAttribute(name) { return this._attributes.has(name) ? this._attributes.get(name) : null; }
  hasAttribute(name) { return this._attributes.has(name); }
  removeAttribute(name) { this._attributes.delete(name); }
  append(...nodes) { nodes.forEach(node => this.insertBefore(node, null)); }
  insertBefore(node, reference) {
    if (node === reference) return node;
    if (node.parentNode) node.parentNode.removeChild(node);
    const at = reference ? this.childNodes.indexOf(reference) : -1;
    if (at < 0) this.childNodes.push(node); else this.childNodes.splice(at, 0, node);
    node.parentNode = this;
    return node;
  }
  removeChild(node) {
    const index = this.childNodes.indexOf(node);
    if (index >= 0) this.childNodes.splice(index, 1);
    node.parentNode = null;
    return node;
  }
  contains(target) { return target === this || this.childNodes.some(node => node.contains(target)); }
  setSelectionRange(start, end, direction = 'none') {
    this.selectionStart = start; this.selectionEnd = end; this.selectionDirection = direction;
  }
  getBoundingClientRect() { return { top: 120, bottom: 160 }; }
}

function keyed(node, key) { node.dataset.renderKey = key; return node; }
function text(value) { return new FixtureNode('#text', 3, value); }
function card(key, title, inputValue, { open, fileValue, handler }) {
  const result = keyed(new FixtureNode('article'), key);
  const heading = keyed(new FixtureNode('h3'), 'heading');
  heading.append(text(title));
  result.append(heading);
  const input = keyed(new FixtureNode('textarea', 1, inputValue), 'answer');
  input.defaultValue = '';
  input.oninput = handler;
  result.append(input);
  const details = keyed(new FixtureNode('details'), 'context');
  details.open = open;
  details.append(keyed(new FixtureNode('summary'), 'summary'));
  result.append(details);
  const file = keyed(new FixtureNode('input'), 'file-input');
  file.type = 'file'; file._value = fileValue; file._fileValue = true; file.files = [{ name: 'reviewed.pdf' }];
  result.append(file);
  result.__input = input; result.__details = details; result.__file = file;
  return result;
}

const root = new FixtureNode('div');
const oldCard = card('inbox:fixture-question', 'Question awaiting answer', 'My answer', {
  open: true, fileValue: 'C:\\fakepath\\reviewed.pdf', handler: () => 'old',
});
const secondCard = keyed(new FixtureNode('article'), 'inbox:second');
root.append(oldCard, secondCard);
document.activeElement = oldCard.__input;
oldCard.__input.value = 'I typed into the middle';
oldCard.__input.defaultValue = '';
oldCard.__input.setSelectionRange(12, 12, 'none');
oldCard.__input.__pipelineComposing = true;
const refreshedHandler = event => event.currentTarget.value;
const nextA = card('inbox:fixture-question', 'Question context updated', 'Server default text', {
  open: false, fileValue: 'C:\\fakepath\\reviewed.pdf', handler: refreshedHandler,
});
const nextB = keyed(new FixtureNode('article'), 'inbox:second');
const nextC = keyed(new FixtureNode('article'), 'inbox:new');

preserveViewport(root, () => reconcileChildren(root, [nextB, nextA, nextC]));
assert.equal(root.childNodes[0], oldCard, 'an active keyed card keeps its place while the user edits');
assert.equal(root.childNodes[1], secondCard);
assert.equal(root.childNodes[2].dataset.renderKey, 'inbox:new');
assert.equal(oldCard.__input, document.activeElement, 'the active textarea node remains mounted');
assert.equal(oldCard.__input.value, 'I typed into the middle', 'a mid-string draft is not overwritten by server state');
assert.equal(oldCard.__input.selectionStart, 12, 'the caret remains in place during composition');
assert.equal(oldCard.__input.__pipelineComposing, true);
assert.equal(oldCard.__details.open, true, 'expanded nested context stays expanded');
assert.equal(oldCard.__file.value, 'C:\\fakepath\\reviewed.pdf', 'file inputs are never reassigned');
assert.equal(oldCard.__file.files[0].name, 'reviewed.pdf');
oldCard.__input.oninput({ currentTarget: oldCard.__input });
assert.equal(oldCard.__input.oninput, refreshedHandler, 'keyed reconciliation installs current handlers');
assert.match(oldCard.childNodes[0].childNodes[0].nodeValue, /context updated/, 'non-draft content still refreshes');

document.activeElement = new FixtureNode('input');
preserveViewport(root, () => reconcileChildren(root, [nextB, nextA, nextC]));
assert.deepEqual(root.childNodes.map(node => node.dataset.renderKey), ['inbox:second', 'inbox:fixture-question', 'inbox:new'],
  'the server order is applied after the active card loses focus');
assert.equal(window.scrollY, 240, 'reconciliation preserves the current scroll position');
console.log('UI keyed reconciliation fixture passed: draft, caret/IME, open details, file input, handler, order, and scroll.');

const selectionScope = { batchSelections: new Map(), batchSelectionNotice: '' };
const selectionStart = source.indexOf('function reconcileBatchSelections(');
const selectionEnd = source.indexOf('function renderApprovalBatches(', selectionStart);
vm.runInNewContext(source.slice(selectionStart, selectionEnd) + '\nglobalThis.reconcile = reconcileBatchSelections;', selectionScope);
const selected = { id: 'one', pipeline_id: 'company', scope_token: 'original', batch_eligible: true };
selectionScope.batchSelections.set('one', { ...selected });
selectionScope.reconcile([selected, { ...selected, id: 'future' }]);
assert.equal(selectionScope.batchSelections.size, 1, 'polling never adds future requests to a selection');
selectionScope.reconcile([{ ...selected, scope_token: 'new-terms' }]);
assert.equal(selectionScope.batchSelections.size, 0, 'changed terms clear the old selection rather than adopting a new fingerprint');
assert.match(selectionScope.batchSelectionNotice, /removed/);
selectionScope.batchSelections.set('one', { ...selected });
selectionScope.reconcile([{ ...selected, batch_eligible: false }]);
assert.equal(selectionScope.batchSelections.size, 0, 'stopped or stale requests are deselected');
console.log('Batch selection fixture passed: frozen scopes, future exclusion, stale and stopped removal.');
