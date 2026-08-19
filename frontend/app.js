const form = document.querySelector('#ask-form');
const input = document.querySelector('#question');
const conversation = document.querySelector('#conversation');
const welcome = document.querySelector('#welcome');
const count = document.querySelector('#char-count');
const askButton = document.querySelector('.ask-button');
const history = [];

function resizeInput() {
  input.style.height = 'auto';
  input.style.height = `${Math.min(input.scrollHeight, 132)}px`;
  count.textContent = `${input.value.length} / 1000`;
}

function addMessage(role, text, extra = {}) {
  if (welcome) welcome.remove();
  const message = document.createElement('article');
  message.className = `message ${role === 'user' ? 'user' : extra.error ? 'error-message' : ''}`;
  const mark = document.createElement('div');
  mark.className = 'message-mark';
  mark.textContent = role === 'user' ? 'YOU' : extra.error ? '!' : 'HW';
  const body = document.createElement('div');
  body.className = 'message-body';
  const label = document.createElement('div');
  label.className = 'message-label';
  label.textContent = role === 'user' ? 'Question' : extra.error ? 'Service notice' : 'Haulwise';
  const content = document.createElement('div');
  content.className = 'message-text';
  content.textContent = text;
  body.append(label, content);
  if (extra.facts) {
    const note = document.createElement('div');
    note.className = 'data-note';
    note.innerHTML = '<span class="status-dot"></span> Calculated from read-only PostgreSQL data';
    body.append(note);
  }
  message.append(mark, body);
  conversation.append(message);
  message.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

async function ask(question) {
  addMessage('user', question);
  askButton.disabled = true;
  askButton.textContent = '...';
  try {
    const response = await fetch('/api/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question, history: history.slice(-8) }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || 'The service could not answer.');
    history.push({ role: 'user', content: question });
    history.push({ role: 'assistant', content: payload.answer, intent: payload.intent });
    addMessage('assistant', payload.answer, { facts: payload.facts });
  } catch (error) {
    addMessage('assistant', error.message, { error: true });
  } finally {
    askButton.disabled = false;
    askButton.innerHTML = 'Ask <span>></span>';
  }
}

form.addEventListener('submit', (event) => {
  event.preventDefault();
  const question = input.value.trim();
  if (!question || askButton.disabled) return;
  input.value = '';
  resizeInput();
  ask(question);
});

input.addEventListener('input', resizeInput);
input.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    form.requestSubmit();
  }
});

document.querySelectorAll('.starter').forEach((button) => {
  button.addEventListener('click', () => {
    input.value = button.dataset.question;
    resizeInput();
    input.focus();
  });
});
