const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const source = readFileSync(require('node:path').join(__dirname, '../static/app.js'), 'utf8');
const settle = () => new Promise((resolve) => setImmediate(resolve));

function page(fetch, clipboard = async () => {}) {
  const elements = new Map();
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, {
      value: id === 'payload' ? 'Hello, C10K!' : '', textContent: '', dataset: {},
      disabled: false, hidden: false, handlers: {}, validity: '',
      setAttribute(name, value) { this[name] = value; },
      addEventListener(name, handler) { this.handlers[name] = handler; },
      setCustomValidity(value) { this.validity = value; },
      reportValidity() { return !this.validity; },
    });
    return elements.get(id);
  };
  const tabs = ['echo', 'health', 'status'].map((name) => {
    const tab = element('tab-' + name); tab.dataset.endpoint = name; return tab;
  });
  const document = { hidden: false, getElementById: element, querySelectorAll: () => tabs,
    addEventListener() {} };
  const timers = [];
  vm.runInNewContext(source, { document, fetch, TextEncoder, AbortController, Intl, Date,
    performance: { now: () => 25 }, setTimeout: (...args) => { timers.push(args); return timers.length; },
    clearTimeout() {}, setInterval() {}, location: { origin: 'https://demo.example' },
    navigator: { clipboard: { writeText: clipboard } }, URL });
  return { element, tabs, document, submit: () => element('request-form').handlers.submit({ preventDefault() {} }) };
}

const response = (body, status = 200) => ({ ok: status === 200, status, text: async () => body });
const healthy = () => response(JSON.stringify({ status: 'ok', active_connections: 3, peak_connections: 11, uptime_seconds: 120 }));

test('live status uses server data and clears stale values after network failure', async () => {
  let offline = false;
  const p = page(async () => { if (offline) throw new Error('offline'); return healthy(); });
  await settle();
  assert.equal(p.element('health-label').textContent, 'Online');
  assert.equal(p.element('live-connections').textContent, '3');
  assert.equal(p.element('live-peak').textContent, '11');
  assert.equal(p.element('live-uptime').textContent, '2m 0s');
  offline = true;
  await p.element('refresh-status').handlers.click();
  assert.equal(p.element('health-label').textContent, 'Unreachable');
  assert.equal(p.element('live-connections').textContent, '—');
});

test('echo sends exact Unicode content and renders markup as inert text', async () => {
  let received;
  const p = page(async (path, options) => {
    if (path === '/status') return healthy();
    received = { path, options };
    return response(options.body);
  });
  await settle();
  const body = '<img src=x onerror=alert(1)> नमस्ते 🌱';
  p.element('payload').value = body;
  await p.submit();
  assert.equal(received.path, '/echo');
  assert.equal(received.options.method, 'POST');
  assert.equal(received.options.body, body);
  assert.equal(p.element('response-body').textContent, body);
  assert.equal(p.element('response-meta').dataset.state, 'success');
  assert.equal(p.element('send-request').disabled, false);
});

test('health GET, HTTP errors, and aborted requests are handled without a stuck button', async () => {
  let mode = 'error', received;
  const p = page(async (path, options) => {
    if (path === '/status') return healthy();
    received = options;
    if (mode === 'timeout') { const e = new Error('timeout'); e.name = 'AbortError'; throw e; }
    return response('503 unavailable', 503);
  });
  await settle();
  p.tabs[1].handlers.click();
  assert.equal(p.element('payload').hidden, true);
  await p.submit();
  assert.equal(received.method, 'GET');
  assert.equal(p.element('response-meta').dataset.state, 'error');
  assert.equal(p.element('response-body').textContent, '503 unavailable');
  mode = 'timeout';
  await p.submit();
  assert.match(p.element('response-body').textContent, /timed out/);
  assert.equal(p.element('send-request').disabled, false);
});

test('UTF-8 byte limit prevents oversized submissions and curl safely quotes user text', async () => {
  let echoCalls = 0, copied;
  const p = page(async (path) => { if (path === '/echo') echoCalls++; return healthy(); }, async (text) => { copied = text; });
  await settle();
  p.element('payload').value = '🌱'.repeat(1100);
  await p.submit();
  assert.equal(echoCalls, 0);
  assert.match(p.element('payload').validity, /4,096/);
  p.element('payload').value = "it's $(whoami)";
  await p.element('copy-curl').handlers.click();
  assert.equal(copied, `curl -i 'https://demo.example/echo' -H 'Content-Type: text/plain; charset=utf-8' --data-binary 'it'"'"'s $(whoami)'`);
});

test('clipboard failure shows a copyable command and hidden tabs do not poll', async () => {
  let calls = 0;
  const p = page(async () => { calls++; return healthy(); }, async () => { throw new Error('denied'); });
  await settle();
  p.document.hidden = true;
  await p.element('refresh-status').handlers.click();
  assert.equal(calls, 1);
  await p.element('copy-curl').handlers.click();
  assert.match(p.element('response-body').textContent, /^curl -i /);
  assert.match(p.element('copy-feedback').textContent, /Clipboard unavailable/);
});
