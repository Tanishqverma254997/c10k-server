'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  const tabs = [...document.querySelectorAll('[data-endpoint]')];
  const number = new Intl.NumberFormat();
  const payload = $('payload');
  const byteLength = (text) => new TextEncoder().encode(text).length;
  let endpoint = 'echo';
  let checking = false;
  let sending = false;
  let selectedDuringRequest = false;
  let copyTimer;

  async function request(path, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 8000);
    const started = performance.now();
    try {
      const response = await fetch(path, { ...options, cache: 'no-store', signal: controller.signal });
      const text = await response.text();
      return { response, text, elapsed: Math.round(performance.now() - started) };
    } finally {
      clearTimeout(timer);
    }
  }

  function duration(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return 'Unavailable';
    const s = Math.floor(seconds);
    if (s >= 86400) return `${Math.floor(s / 86400)}d ${Math.floor(s % 86400 / 3600)}h`;
    if (s >= 3600) return `${Math.floor(s / 3600)}h ${Math.floor(s % 3600 / 60)}m`;
    if (s >= 60) return `${Math.floor(s / 60)}m ${s % 60}s`;
    return `${s}s`;
  }

  async function checkStatus() {
    if (checking || document.hidden) return;
    checking = true;
    $('refresh-status').disabled = true;
    try {
      const { response, text, elapsed } = await request('/status');
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = JSON.parse(text);
      if (data.status !== 'ok' || !Number.isInteger(data.active_connections) || data.active_connections < 0) {
        throw new Error('Unexpected status response');
      }
      $('health-badge').dataset.state = 'online';
      $('health-label').textContent = 'Online';
      $('live-connections').textContent = number.format(data.active_connections);
      $('live-peak').textContent = Number.isInteger(data.peak_connections) ? number.format(data.peak_connections) : 'Unavailable';
      $('live-uptime').textContent = duration(data.uptime_seconds);
      $('live-roundtrip').textContent = `${number.format(elapsed)} ms`;
      $('last-checked').textContent = `Checked ${new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}`;
    } catch (error) {
      $('health-badge').dataset.state = 'offline';
      $('health-label').textContent = 'Unreachable';
      for (const id of ['live-connections', 'live-peak', 'live-uptime', 'live-roundtrip']) $(id).textContent = '—';
      $('last-checked').textContent = error.name === 'AbortError' ? 'Check timed out. Try refreshing.' : 'Could not reach the server. Try refreshing.';
    } finally {
      checking = false;
      $('refresh-status').disabled = false;
    }
  }

  function updateSize() {
    const bytes = byteLength(payload.value);
    $('payload-size').textContent = `${number.format(bytes)} / 4,096 bytes`;
    payload.setCustomValidity(bytes > 4096 ? 'Please keep the request body within 4,096 UTF-8 bytes.' : '');
  }

  function selectEndpoint(next) {
    endpoint = next;
    const echo = next === 'echo';
    tabs.forEach((tab) => tab.setAttribute('aria-pressed', String(tab.dataset.endpoint === next)));
    payload.hidden = !echo;
    payload.disabled = !echo;
    $('payload-label').hidden = !echo;
    $('payload-size').hidden = !echo;
    $('endpoint-help').textContent = echo ? 'The server returns the exact UTF-8 bytes you send.' : next === 'health' ? 'A lightweight liveness check. A healthy server returns OK.' : 'Inspect the real backend connection count, process peak, and uptime.';
    $('response-body').textContent = 'Send a request to see the real response here.';
    $('response-meta').textContent = 'No request sent yet';
    delete $('response-meta').dataset.state;
    $('copy-feedback').textContent = '';
    if (sending) selectedDuringRequest = true;
    updateSize();
  }

  $('request-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (sending) return;
    updateSize();
    if (endpoint === 'echo' && !payload.reportValidity()) return;
    sending = true;
    selectedDuringRequest = false;
    const target = endpoint;
    $('send-request').disabled = true;
    $('send-request').textContent = 'Sending…';
    $('response-meta').textContent = 'Request in progress…';
    delete $('response-meta').dataset.state;
    tabs.forEach((tab) => { tab.disabled = true; });
    try {
      const options = target === 'echo' ? { method: 'POST', headers: { 'Content-Type': 'text/plain; charset=utf-8' }, body: payload.value } : { method: 'GET' };
      const { response, text, elapsed } = await request(`/${target}`, options);
      if (selectedDuringRequest) return;
      let formatted = text;
      if (target === 'status' && response.ok) {
        try { formatted = JSON.stringify(JSON.parse(text), null, 2); } catch { /* Show unexpected responses as text. */ }
      }
      $('response-body').textContent = formatted || '(empty response)';
      $('response-meta').textContent = `${response.status} ${response.ok ? 'OK' : 'Error'} · ${elapsed} ms · ${byteLength(text)} bytes`;
      $('response-meta').dataset.state = response.ok ? 'success' : 'error';
    } catch (error) {
      $('response-body').textContent = error.name === 'AbortError' ? 'The request timed out after 8 seconds. Try again when the server is available.' : 'The request could not reach the server. Check your connection and try again.';
      $('response-meta').textContent = 'Request failed';
      $('response-meta').dataset.state = 'error';
    } finally {
      sending = false;
      $('send-request').disabled = false;
      $('send-request').textContent = 'Send request';
      tabs.forEach((tab) => { tab.disabled = false; });
    }
  });

  // POSIX shell quoting also keeps pasted request bodies from becoming commands.
  const shellQuote = (text) => "'" + text.replace(/'/g, "'\"'\"'") + "'";
  $('copy-curl').addEventListener('click', async () => {
    const url = new URL(`/${endpoint}`, location.origin).href;
    const command = endpoint === 'echo'
      ? `curl -i ${shellQuote(url)} -H 'Content-Type: text/plain; charset=utf-8' --data-binary ${shellQuote(payload.value)}`
      : `curl -i ${shellQuote(url)}`;
    clearTimeout(copyTimer);
    try {
      await navigator.clipboard.writeText(command);
      $('copy-feedback').textContent = 'Copied · Bash / WSL';
    } catch {
      $('copy-feedback').textContent = 'Clipboard unavailable. Command shown below.';
      $('response-body').textContent = command;
      $('response-meta').textContent = 'curl command · Bash / WSL';
      delete $('response-meta').dataset.state;
    }
    copyTimer = setTimeout(() => { $('copy-feedback').textContent = ''; }, 5000);
  });
  tabs.forEach((tab) => tab.addEventListener('click', () => selectEndpoint(tab.dataset.endpoint)));
  payload.addEventListener('input', updateSize);
  $('refresh-status').addEventListener('click', checkStatus);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) checkStatus(); });
  updateSize();
  checkStatus();
  setInterval(checkStatus, 15000);
})();
