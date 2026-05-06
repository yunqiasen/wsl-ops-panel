(() => {
  const systemTerminal = document.getElementById('system-terminal');
  const debugTerminals = document.getElementById('debug-terminals');
  const createButton = document.getElementById('create-debug-terminal');

  function appendChunk(node, chunk) {
    node.textContent += chunk;
    node.scrollTop = node.scrollHeight;
  }

  function connectSystemTerminal() {
    if (!systemTerminal) {
      return;
    }

    const events = new EventSource('/api/terminals/system/stream');
    events.onmessage = (event) => {
      const payload = JSON.parse(event.data);
      appendChunk(systemTerminal, payload.chunk);
    };
  }

  function redirectIfUnauthorized(response) {
    const redirectTo = response.headers.get('HX-Redirect') || response.headers.get('X-Login-Redirect');
    if (response.status === 401 && redirectTo) {
      window.location.href = redirectTo;
      return true;
    }
    return false;
  }

  function createSessionCard(session) {
    const wrapper = document.createElement('section');
    wrapper.dataset.sessionId = session.id;
    wrapper.className = 'panel';

    const title = document.createElement('h3');
    title.textContent = `${session.shell} · ${session.cwd}`;

    const output = document.createElement('pre');
    output.className = 'log-box';
    output.setAttribute('aria-label', `debug terminal ${session.id}`);

    const form = document.createElement('form');
    form.className = 'inline-form';
    const input = document.createElement('textarea');
    input.rows = 3;
    input.placeholder = '输入命令，点击发送';

    const sendButton = document.createElement('button');
    sendButton.type = 'submit';
    sendButton.textContent = '发送';

    const closeButton = document.createElement('button');
    closeButton.type = 'button';
    closeButton.textContent = '关闭';

    form.append(input, sendButton);
    wrapper.append(title, output, form, closeButton);

    const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const socket = new WebSocket(`${protocol}://${window.location.host}/api/terminals/debug/${session.id}/ws`);
    socket.onmessage = (event) => appendChunk(output, event.data);
    socket.onclose = () => {
      input.disabled = true;
      sendButton.disabled = true;
    };

    form.addEventListener('submit', (event) => {
      event.preventDefault();
      if (!input.value.trim()) {
        return;
      }
      if (socket.readyState !== WebSocket.OPEN) {
        return;
      }
      const payload = input.value.endsWith('\n') ? input.value : `${input.value}\n`;
      socket.send(payload);
      input.value = '';
    });

    closeButton.addEventListener('click', async () => {
      await fetch(`/api/terminals/debug/${session.id}`, { method: 'DELETE' });
      socket.close();
      wrapper.remove();
    });

    return wrapper;
  }

  async function loadDebugSessions() {
    if (!debugTerminals) {
      return;
    }

    const response = await fetch('/api/terminals/debug');
    if (redirectIfUnauthorized(response) || !response.ok) {
      return;
    }
    const sessions = await response.json();
    debugTerminals.replaceChildren(...sessions.map(createSessionCard));
  }

  async function createDebugSession() {
    const response = await fetch('/api/terminals/debug', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    });
    if (redirectIfUnauthorized(response) || !response.ok) {
      return;
    }
    const session = await response.json();
    debugTerminals.appendChild(createSessionCard(session));
  }

  async function emulateHtmxRequest(element, event) {
    const endpoint = element.getAttribute('hx-post');
    if (!endpoint) {
      return;
    }
    event.preventDefault();

    const target = document.querySelector(element.getAttribute('hx-target'));
    const headers = { 'HX-Request': 'true' };
    const options = { method: 'POST', headers };

    if (element.tagName === 'FORM') {
      const formData = new FormData(element);
      options.body = formData;
    }

    const response = await fetch(endpoint, options);
    if (redirectIfUnauthorized(response)) {
      return;
    }
    const html = await response.text();
    if (target) {
      target.innerHTML = html;
    }
  }

  function installHtmxFallback() {
    if (window.htmx) {
      return;
    }

    document.addEventListener('click', (event) => {
      const target = event.target.closest('[hx-post]');
      if (!target || target.tagName === 'FORM') {
        return;
      }
      emulateHtmxRequest(target, event);
    });

    document.addEventListener('submit', (event) => {
      const form = event.target.closest('form[hx-post]');
      if (!form) {
        return;
      }
      emulateHtmxRequest(form, event);
    });
  }

  if (createButton) {
    createButton.addEventListener('click', createDebugSession);
  }

  installHtmxFallback();
  connectSystemTerminal();
  loadDebugSessions();
})();
