const conversation = document.getElementById("conversation");
const msgInput = document.getElementById("msg-input");
const sendBtn = document.getElementById("send-btn");
const newChatBtn = document.getElementById("new-chat");
const suggestions = document.querySelectorAll(".suggestion");
const modelPicker = document.getElementById("model-picker");

const statusDot = document.getElementById("status-dot");
const statusText = document.getElementById("status-text");
const statusModels = document.getElementById("status-models");
const statusDevice = document.getElementById("status-device");

// ---------------------------------------------------------------------------
// Modèles
// ---------------------------------------------------------------------------
const MODEL_STORAGE_KEY = "opsiom.model";

// Liste de secours, utilisée seulement si GET /models échoue. Doit rester
// alignée avec MODEL_DISPLAY dans app.py.
const FALLBACK_MODELS = [
  { id: "nano", label: "Opsiom Micro", params: "25M" },
  { id: "small", label: "Opsiom Nano", params: "45M" },
  { id: "large", label: "Opsiom Large", params: "200M" },
];
const FALLBACK_DEFAULT = "small";

let models = FALLBACK_MODELS;
let currentModelId = FALLBACK_DEFAULT;
let isSending = false;

function getModelInfo(id = currentModelId) {
  return models.find((m) => m.id === id) || models[0];
}

function readSavedModel() {
  try { return localStorage.getItem(MODEL_STORAGE_KEY); } catch (e) { return null; }
}

function selectModel(id) {
  currentModelId = id;
  try { localStorage.setItem(MODEL_STORAGE_KEY, id); } catch (e) { /* stockage indisponible */ }
  updatePickerState();
}

function updatePickerState() {
  modelPicker.querySelectorAll(".model-btn").forEach((btn) => {
    const active = btn.dataset.id === currentModelId;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-checked", String(active));
  });
}

function renderModelPicker() {
  modelPicker.textContent = "";
  models.forEach((m) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "model-btn";
    btn.dataset.id = m.id;
    btn.setAttribute("role", "radio");
    btn.title = `${m.label} — ${m.params} de paramètres`;

    const name = document.createElement("span");
    name.textContent = String(m.label).replace(/^Opsiom\s+/i, "");
    const size = document.createElement("small");
    size.textContent = m.params;

    btn.append(name, size);
    btn.addEventListener("click", () => selectModel(m.id));
    modelPicker.appendChild(btn);
  });
  updatePickerState();
}

async function initModelPicker() {
  // Affichage immédiat avec la liste de secours et le dernier choix mémorisé
  const saved = readSavedModel();
  if (saved && models.some((m) => m.id === saved)) currentModelId = saved;
  renderModelPicker();

  // Puis on remplace par la liste réelle fournie par le serveur
  try {
    const resp = await fetch("/models", { headers: { Accept: "application/json" } });
    if (!resp.ok) return;
    const data = await resp.json();
    if (!Array.isArray(data.models) || !data.models.length) return;

    models = data.models;
    const serverDefault = data.default && models.some((m) => m.id === data.default)
      ? data.default
      : models[0].id;
    currentModelId = saved && models.some((m) => m.id === saved) ? saved : serverDefault;
    renderModelPicker();
  } catch (err) {
    console.warn("Liste des modèles indisponible, liste de secours conservée :", err);
  }
}

// ---------------------------------------------------------------------------
// Bulles de conversation
// ---------------------------------------------------------------------------
function removePlaceholder() {
  const ph = document.getElementById("placeholder");
  if (ph) ph.remove();
}

function addBubble(role, text, { error = false, meta = "" } = {}) {
  removePlaceholder();
  const el = document.createElement("div");
  el.className = `bubble ${role}${error ? " error" : ""}`;
  el.textContent = text;

  if (meta) {
    const metaEl = document.createElement("div");
    metaEl.className = "bubble-meta";
    metaEl.textContent = meta;
    el.appendChild(metaEl);
  }

  conversation.appendChild(el);
  conversation.scrollTop = conversation.scrollHeight;
  return el;
}

// ---------------------------------------------------------------------------
// Animation « Opsiom réfléchit »
// ---------------------------------------------------------------------------
const THINKING_PHRASES = [
  "Opsiom réfléchit…",
  "Je cherche mes mots…",
  "J'assemble la réponse…",
  "Encore un instant…",
];
const PHRASE_EVERY_MS = 2600;
const SLOW_AFTER_S = 15;

function addThinking() {
  removePlaceholder();
  const info = getModelInfo();

  const el = document.createElement("div");
  el.className = "bubble assistant thinking";
  el.setAttribute("role", "status");

  // Lu une seule fois par les lecteurs d'écran (le reste est décoratif)
  const sr = document.createElement("span");
  sr.className = "sr-only";
  sr.textContent = "Opsiom réfléchit…";

  const wave = document.createElement("div");
  wave.className = "wave";
  wave.setAttribute("aria-hidden", "true");
  for (let i = 0; i < 5; i++) wave.appendChild(document.createElement("i"));

  const body = document.createElement("div");
  body.className = "think-body";
  body.setAttribute("aria-hidden", "true");

  let text = document.createElement("span");
  text.className = "think-text";
  text.textContent = THINKING_PHRASES[0];

  const meta = document.createElement("span");
  meta.className = "think-meta";
  meta.textContent = `${info.label} · 0 s`;

  body.append(text, meta);
  el.append(sr, wave, body);
  conversation.appendChild(el);
  conversation.scrollTop = conversation.scrollHeight;

  const started = Date.now();
  let phraseIdx = 0;

  // On recrée le <span> à chaque changement de phrase pour rejouer le fondu
  const phraseTimer = setInterval(() => {
    phraseIdx = (phraseIdx + 1) % THINKING_PHRASES.length;
    const next = document.createElement("span");
    next.className = "think-text";
    next.textContent = THINKING_PHRASES[phraseIdx];
    body.replaceChild(next, text);
    text = next;
  }, PHRASE_EVERY_MS);

  const clockTimer = setInterval(() => {
    const s = Math.floor((Date.now() - started) / 1000);
    let line = `${info.label} · ${s} s`;
    if (s >= SLOW_AFTER_S) line += " · génération sur CPU, ça peut prendre un moment";
    meta.textContent = line;
  }, 1000);

  return {
    elapsedSeconds: () => Math.round((Date.now() - started) / 1000),
    remove() {
      clearInterval(phraseTimer);
      clearInterval(clockTimer);
      el.remove();
    },
  };
}

// ---------------------------------------------------------------------------
// Statut du serveur (sidebar)
// ---------------------------------------------------------------------------
async function refreshStatus() {
  statusDot.className = "dot dot-pending";
  statusText.textContent = "Connexion...";
  statusModels.textContent = "";
  statusDevice.textContent = "";

  try {
    const resp = await fetch("/status");
    const data = await resp.json();

    if (data.online) {
      statusDot.className = "dot dot-online";
      statusText.textContent = "En ligne";
      if (Array.isArray(data.models_loaded)) {
        const n = data.models_loaded.length;
        statusModels.textContent = `${n} modèle${n > 1 ? "s" : ""} chargé${n > 1 ? "s" : ""}`;
      }
      if (data.device) statusDevice.textContent = `Inférence : ${String(data.device).toUpperCase()}`;
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

// ---------------------------------------------------------------------------
// Envoi d'un message
// ---------------------------------------------------------------------------
async function sendMessage(text) {
  const message = text.trim();
  if (!message || isSending) return;

  isSending = true;
  sendBtn.disabled = true;

  // Le modèle est figé au moment de l'envoi, même si l'utilisateur change
  // de sélection pendant l'attente.
  const modelId = currentModelId;

  addBubble("user", message);
  msgInput.value = "";
  autoResize();

  const thinking = addThinking();

  try {
    const resp = await fetch("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, model: modelId }),
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

    const seconds = thinking.elapsedSeconds();
    thinking.remove();

    const used = getModelInfo(data.model || modelId);
    addBubble("assistant", data.response || "(réponse vide)", {
      meta: `${used.label} · ${used.params} · ${seconds} s`,
    });

  } catch (err) {
    console.error("Erreur lors de l'envoi du message :", err);
    thinking.remove();
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

initModelPicker();
refreshStatus();
