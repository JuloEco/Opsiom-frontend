const conversation = document.getElementById("conversation");
const placeholder = document.getElementById("placeholder");
const msgInput = document.getElementById("msg-input");
const sendBtn = document.getElementById("send-btn");
const newChatBtn = document.getElementById("new-chat");
const suggestions = document.querySelectorAll(".suggestion");

const statusDot = document.getElementById("status-dot");
const statusText = document.getElementById("status-text");
const statusCheckpoint = document.getElementById("status-checkpoint");
const statusRag = document.getElementById("status-rag");

let isSending = false;

function addBubble(role, text, { pending = false, error = false } = {}) {
  if (placeholder) placeholder.remove();
  const el = document.createElement("div");
  el.className = `bubble ${role}${pending ? " pending" : ""}${error ? " error" : ""}`;
  el.textContent = text;
  conversation.appendChild(el);
  conversation.scrollTop = conversation.scrollHeight;
  return el;
}

async function refreshStatus() {
  statusDot.className = "dot dot-pending";
  statusText.textContent = "Connexion...";
  statusCheckpoint.textContent = "";
  statusRag.textContent = "";

  try {
    const resp = await fetch("/status");
    const data = await resp.json();

    if (data.online) {
      statusDot.className = "dot dot-online";
      statusText.textContent = "En ligne";
      if (data.checkpoint) statusCheckpoint.textContent = data.checkpoint;
      statusRag.textContent = data.rag_enabled ? "RAG activé" : "RAG désactivé";
    } else {
      statusDot.className = "dot dot-offline";
      statusText.textContent = data.message || "Hors ligne";
    }
  } catch (err) {
    // Le /status lui-même a échoué (réseau coupé, serveur Render down, etc.)
    console.error("Impossible de contacter le serveur Flask lui-même :", err);
    statusDot.className = "dot dot-offline";
    statusText.textContent = "Serveur injoignable";
  }
}

async function sendMessage(text) {
  const message = text.trim();
  if (!message || isSending) return;

  isSending = true;
  sendBtn.disabled = true;

  addBubble("user", message);
  msgInput.value = "";
  autoResize();

  const pendingBubble = addBubble("assistant", "Opsiom réfléchit...", { pending: true });

  try {
    const resp = await fetch("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    });

    let data;
    try {
      data = await resp.json();
    } catch (parseErr) {
      throw new Error("Réponse du serveur illisible.");
    }

    if (!resp.ok) {
      throw new Error(data.error || `Erreur serveur (${resp.status})`);
    }

    pendingBubble.remove();
    addBubble("assistant", data.response || "(réponse vide)");

  } catch (err) {
    console.error("Erreur lors de l'envoi du message :", err);
    pendingBubble.remove();
    addBubble("assistant", `Erreur : ${err.message}`, { error: true });
  } finally {
    isSending = false;
    sendBtn.disabled = false;
    msgInput.focus();
  }
}

function autoResize() {
  msgInput.style.height = "auto";
  msgInput.style.height = Math.min(msgInput.scrollHeight, 140) + "px";
}

sendBtn.addEventListener("click", () => sendMessage(msgInput.value));

msgInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendMessage(msgInput.value);
  }
});

msgInput.addEventListener("input", autoResize);

newChatBtn.addEventListener("click", () => {
  conversation.innerHTML = "";
  const ph = document.createElement("div");
  ph.id = "placeholder";
  ph.innerHTML = "<h2>Une IA qui pense en français.</h2><p>Posez une question, développez une idée ou commencez par un simple bonjour.</p>";
  conversation.appendChild(ph);
});

suggestions.forEach((btn) => {
  btn.addEventListener("click", () => {
    msgInput.value = btn.dataset.prompt;
    autoResize();
    msgInput.focus();
  });
});

refreshStatus();
