let abortController = null;
let sessionId = localStorage.getItem('agent_session_id') || null;

// Sanitize HTML after markdown parsing to prevent XSS
function sanitize(html) {
  return DOMPurify ? DOMPurify.sanitize(html) : html;
}

const logContainer = document.getElementById('log-container');
const statusElem = document.getElementById('status');
const userInput = document.getElementById('user-input');
const sendBtn = document.getElementById('send-btn');
const stopBtn = document.getElementById('stop-btn');
const clearBtn = document.getElementById('clear-btn');

function setProcessing(processing) {
  statusElem.style.display = processing ? 'flex' : 'none';
  sendBtn.disabled = processing;
  stopBtn.disabled = !processing;
}

function appendLog(role, content, isFinal = false, command = null, toolName = null) {
  const wrapper = document.createElement('div');
  wrapper.className = `log-entry ${role}-msg`;

  const timestamp = new Date().toLocaleTimeString([], { 
    hour12: false, 
    hour: '2-digit', 
    minute: '2-digit', 
    second: '2-digit' 
  });

  if (role === 'tool') {
    const details = document.createElement('details');
    const summary = document.createElement('summary');
    summary.style.cursor = 'pointer';
    if (command) {
      summary.innerHTML = `Command: <code>${sanitize(command)}</code>`;
    } else if (toolName) {
      summary.innerHTML = `Tool: <code>${sanitize(toolName)}</code>`;
    } else {
      summary.textContent = 'Tool Output';
    }
    details.appendChild(summary);

    const outputDiv = document.createElement('div');
    outputDiv.className = 'content-body';
    outputDiv.style.whiteSpace = 'pre-wrap';
    outputDiv.textContent = content || 'No output.';
    details.appendChild(outputDiv);

    wrapper.appendChild(details);
  } else if (role === 'assistant' || role === 'user') {
    const contentDiv = document.createElement('div');
    contentDiv.className = 'content-body';
    contentDiv.innerHTML = sanitize(marked.parse(content));
    wrapper.appendChild(contentDiv);
  } else {
    const contentDiv = document.createElement('div');
    contentDiv.className = 'content-body';
    contentDiv.style.whiteSpace = 'pre-wrap';
    contentDiv.textContent = content;
    wrapper.appendChild(contentDiv);
  }

  const meta = document.createElement('div');
  meta.className = 'log-meta';
  meta.innerText = `[${role.toUpperCase()}] ${timestamp}`;
  wrapper.appendChild(meta);

  if (isFinal) {
    wrapper.classList.add('is-final');
  }

  logContainer.appendChild(wrapper);
  logContainer.scrollTop = logContainer.scrollHeight;
}

window.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') stopRequest();
});

async function sendMessage() {
  const text = userInput.value.trim();
  if (!text) return;

  if (abortController) abortController.abort();
  abortController = new AbortController();
  setProcessing(true);

  appendLog('user', text);
  userInput.value = '';

  try {
    const response = await fetch('/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        messages: [{ role: 'user', content: text }],
        session_id: sessionId,
      }),
      signal: abortController.signal,
    });

    if (!response.ok) throw new Error(`Server ${response.status}`);

    // Parse NDJSON stream and render each message immediately
    let buffer = '';
    const reader = response.body.pipeThrough(new TextDecoderStream())
      .pipeThrough(new TransformStream({
        transform(chunk, controller) {
          buffer += chunk;
          const lines = buffer.split('\n');
          buffer = lines.pop() || ''; // Keep incomplete line in buffer
          for (const line of lines) {
            if (line.trim()) controller.enqueue(line);
          }
        },
        flush() {
          if (buffer.trim()) controller.enqueue(buffer);
        },
      }))
      .getReader();

    let lastAssistantIndex = -1;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      let line = value;
      if (line.startsWith('{')) {
        try {
          const msg = JSON.parse(line);

          // Handle session_id (special last message from server)
          if (msg._session_id) {
            sessionId = msg._session_id;
            localStorage.setItem('agent_session_id', sessionId);
            continue;
          }

          // Skip the user message — already displayed by the immediate echo
          if (msg.role === 'user') continue;

          // Track assistant message index for final marking
          if (msg.role === 'assistant') {
            lastAssistantIndex = logContainer.querySelectorAll('.log-entry').length;
          }

          if (msg.role === 'tool') {
            appendLog('tool', msg.content, false, msg.command, msg.toolName);
          } else {
            appendLog(msg.role, msg.content || '', false);
          }
        } catch {
          // Skip malformed lines
        }
      }
    }

    // Mark the last assistant message as final
    if (lastAssistantIndex >= 0) {
      const entries = logContainer.querySelectorAll('.log-entry');
      if (entries[lastAssistantIndex]) {
        entries[lastAssistantIndex].classList.add('is-final');
      }
    }

  } catch (err) {
    if (err.name === 'AbortError') {
      appendLog('system', 'Request stopped by user.');
    } else {
      appendLog('system', `Error: ${err.message}`);
    }
  } finally {
    abortController = null;
    setProcessing(false);
  }
}

function stopRequest() {
  if (abortController) abortController.abort();
  else console.log('No active request to stop.');
}

async function clearHistory() {
  if (!sessionId) {
    logContainer.innerHTML = '';
    appendLog('system', 'History cleared (local reset).');
    return;
  }

  try {
    await fetch('/clear', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId}),
    });
    logContainer.innerHTML = '';
    appendLog('system', 'History cleared.');
  } catch {
    appendLog('system', 'Failed to clear history.');
  }
}

sendBtn.addEventListener('click', sendMessage);
userInput.addEventListener('keypress', e => { if (e.key === 'Enter') sendMessage(); });
stopBtn.addEventListener('click', stopRequest);
clearBtn.addEventListener('click', clearHistory);

setProcessing(false);