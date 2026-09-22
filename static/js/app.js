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

const quotaCard = document.getElementById("quota-card");
const quotaUsedEl = document.getElementById("quota-used");
const quotaLimitEl = document.getElementById("quota-limit");
const quotaBarFill = document.getElementById("quota-bar-fill");
const quotaBanner = document.getElementById("quota-banner");

const settingsWrap = document.getElementById("settings-wrap");
const settingsToggle = document.getElementById("settings-toggle");
const settingsPanel = document.getElementById("settings-panel");
const settingsReset = document.getElementById("settings-reset");

// ---------------------------------------------------------------------------
// Icônes réutilisées par les boutons d'action sur les messages
// ---------------------------------------------------------------------------
const COPY_ICON = '<svg width="13" height="13" viewBox="0 0 13 13" fill="none"><rect x="4.5" y="4.5" width="7" height="7" rx="1.3" stroke="currentColor" stroke-width="1.1"/><path d="M2.5 8.5V2.8A1.3 1.3 0 0 1 3.8 1.5H8.5" stroke="currentColor" stroke-width="1.1"/></svg>';
const CHECK_ICON = '<svg width="13" height="13" viewBox="0 0 13 13" fill="none"><path d="M2.5 7L5 9.5L10.5 3.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const EDIT_ICON = '<svg width="12" height="12" viewBox="0 0 12 12" fill="none"><path d="M8.5 1.5L10.5 3.5L4 10L1.3 10.7L2 8L8.5 1.5Z" stroke="currentColor" stroke-width="1.1" stroke-linejoin="round"/></svg>';

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
let currentAbortController = null;

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
// Quota gratuit (messages/jour)
// ---------------------------------------------------------------------------
function updateQuota(quota) {
  if (!quota || typeof quota.used !== "number" || typeof quota.limit !== "number") return;
  const { used, limit } = quota;
  const remaining = typeof quota.remaining === "number" ? quota.remaining : Math.max(0, limit - used);

  quotaUsedEl.textContent = used;
  quotaLimitEl.textContent = limit;

  const pct = limit > 0 ? Math.min(100, (used / limit) * 100) : 0;
  quotaBarFill.style.width = `${pct}%`;
  quotaBarFill.classList.toggle("warn", remaining > 0 && remaining <= Math.max(1, Math.round(limit * 0.2)));
  quotaBarFill.classList.toggle("empty", remaining <= 0);

  const exhausted = remaining <= 0;
  quotaBanner.hidden = !exhausted;
  msgInput.disabled = exhausted;
  msgInput.placeholder = exhausted ? "Quota atteint pour aujourd'hui" : "Écrivez à Opsiom…";
  if (!isSending) sendBtn.disabled = exhausted;
}

// Affichage immédiat à partir des valeurs déjà rendues par Flask, avant même
// le premier appel à /status — évite un flash "0/0" au chargement.
if (quotaCard) {
  const used = Number(quotaCard.dataset.used || 0);
  const limit = Number(quotaCard.dataset.limit || 0);
  updateQuota({ used, limit, remaining: Math.max(0, limit - used) });
}

// ---------------------------------------------------------------------------
// Réglages de génération (temperature, top_p, top_k, repetition_penalty, longueur)
// ---------------------------------------------------------------------------
const SETTINGS_STORAGE_KEY = "opsiom.gen_settings";

// Bornes alignées avec les _clamp(...) de app.py — à garder synchronisées si
// les limites changent côté serveur.
const SETTINGS_BOUNDS = {
  temperature: [0, 2],
  top_p: [0, 1],
  top_k: [0, 200],
  repetition_penalty: [1, 2],
  max_new_tokens: [1, 300],
};
const SETTINGS_DEFAULTS = {
  temperature: 0.7,
  top_p: 0.9,
  top_k: 40,
  repetition_penalty: 1.3,
  max_new_tokens: 200,
};
const SETTINGS_DECIMALS = {
  temperature: 2,
  top_p: 2,
  top_k: 0,
  repetition_penalty: 2,
  max_new_tokens: 0,
};

let genSettings = { ...SETTINGS_DEFAULTS };

const settingsInputs = {
  temperature: document.getElementById("set-temperature"),
  top_p: document.getElementById("set-top-p"),
  top_k: document.getElementById("set-top-k"),
  repetition_penalty: document.getElementById("set-rep"),
  max_new_tokens: document.getElementById("set-tokens"),
};
const settingsValues = {
  temperature: document.getElementById("val-temperature"),
  top_p: document.getElementById("val-top-p"),
  top_k: document.getElementById("val-top-k"),
  repetition_penalty: document.getElementById("val-rep"),
  max_new_tokens: document.getElementById("val-tokens"),
};

function clampSetting(key, value) {
  const [lo, hi] = SETTINGS_BOUNDS[key];
  if (Number.isNaN(value)) return SETTINGS_DEFAULTS[key];
  return Math.min(hi, Math.max(lo, value));
}

function formatSetting(key, value) {
  return SETTINGS_DECIMALS[key] > 0 ? value.toFixed(SETTINGS_DECIMALS[key]) : String(value);
}

function applySettingsToInputs() {
  for (const key in settingsInputs) {
    const input = settingsInputs[key];
    if (!input) continue;
    input.value = genSettings[key];
    settingsValues[key].textContent = formatSetting(key, genSettings[key]);
  }
}

function saveSettings() {
  try { localStorage.setItem(SETTINGS_STORAGE_KEY, JSON.stringify(genSettings)); } catch (e) { /* stockage indisponible */ }
}

function loadSettings() {
  try {
    const saved = JSON.parse(localStorage.getItem(SETTINGS_STORAGE_KEY) || "{}");
    for (const key in SETTINGS_DEFAULTS) {
      if (typeof saved[key] === "number") genSettings[key] = clampSetting(key, saved[key]);
    }
  } catch (e) { /* stockage indisponible ou JSON invalide : on garde les défauts */ }
  applySettingsToInputs();
}

Object.keys(settingsInputs).forEach((key) => {
  const input = settingsInputs[key];
  if (!input) return;
  input.addEventListener("input", () => {
    const raw = SETTINGS_DECIMALS[key] > 0 ? parseFloat(input.value) : parseInt(input.value, 10);
    genSettings[key] = clampSetting(key, raw);
    settingsValues[key].textContent = formatSetting(key, genSettings[key]);
    saveSettings();
  });
});

if (settingsReset) {
  settingsReset.addEventListener("click", () => {
    genSettings = { ...SETTINGS_DEFAULTS };
    applySettingsToInputs();
    saveSettings();
  });
}

function openSettings() {
  settingsPanel.hidden = false;
  settingsToggle.setAttribute("aria-expanded", "true");
}
function closeSettings() {
  settingsPanel.hidden = true;
  settingsToggle.setAttribute("aria-expanded", "false");
}
function settingsAreOpen() {
  return settingsPanel && !settingsPanel.hidden;
}

if (settingsToggle) {
  settingsToggle.addEventListener("click", () => {
    if (settingsAreOpen()) closeSettings(); else openSettings();
  });
  document.addEventListener("click", (e) => {
    if (settingsAreOpen() && !settingsWrap.contains(e.target)) closeSettings();
  });
}

// ---------------------------------------------------------------------------
// Rendu Markdown minimal (gras, italique, code, listes) — pas de dépendance
// externe : on échappe d'abord le HTML, puis on n'introduit que les balises
// contrôlées ci-dessous.
// ---------------------------------------------------------------------------
function escapeHtml(str) {
  return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function renderMarkdown(raw) {
  // 1) Isole les blocs ```code``` avant tout le reste pour ne pas leur
  //    appliquer les transformations gras/italique/listes.
  const blocks = [];
  let text = raw.replace(/```([a-zA-Z0-9_-]*)\n?([\s\S]*?)```/g, (_, lang, code) => {
    blocks.push(`<pre><code>${escapeHtml(code.trim())}</code></pre>`);
    return `\u0000${blocks.length - 1}\u0000`;
  });

  text = escapeHtml(text);

  text = text.replace(/`([^`\n]+)`/g, (_, code) => `<code>${code}</code>`);
  text = text.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  text = text.replace(/(?<!\*)\*([^*\n]+)\*(?!\*)/g, "<em>$1</em>");

  // 2) Découpage en blocs : paragraphes (lignes consécutives réunies par
  //    <br>), listes, et blocs de code déjà isolés — une ligne vide clôt
  //    le bloc courant sans laisser de <br> orphelin entre deux éléments
  //    de bloc (qui ont déjà leur propre marge en CSS).
  const lines = text.split("\n");
  const out = [];
  let listType = null;
  let paraOpen = false;

  const closeList = () => { if (listType) { out.push(`</${listType}>`); listType = null; } };
  const closePara = () => { if (paraOpen) { out.push("</p>"); paraOpen = false; } };

  for (const line of lines) {
    const bullet = line.match(/^\s*[-*]\s+(.*)$/);
    const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    const isCodeToken = /^\u0000\d+\u0000$/.test(line);

    if (bullet) {
      closePara();
      if (listType !== "ul") { closeList(); out.push("<ul>"); listType = "ul"; }
      out.push(`<li>${bullet[1]}</li>`);
    } else if (numbered) {
      closePara();
      if (listType !== "ol") { closeList(); out.push("<ol>"); listType = "ol"; }
      out.push(`<li>${numbered[1]}</li>`);
    } else if (isCodeToken) {
      closeList();
      closePara();
      out.push(line);
    } else if (line === "") {
      closeList();
      closePara();
    } else {
      closeList();
      if (!paraOpen) { out.push("<p>"); paraOpen = true; } else { out.push("<br>"); }
      out.push(line);
    }
  }
  closeList();
  closePara();
  text = out.join("");

  // 3) Restaure les blocs de code isolés à l'étape 1
  text = text.replace(/\u0000(\d+)\u0000/g, (_, idx) => blocks[Number(idx)]);

  return text;
}

// ---------------------------------------------------------------------------
// Bulles de conversation
// ---------------------------------------------------------------------------
function removePlaceholder() {
  const ph = document.getElementById("placeholder");
  if (ph) ph.remove();
}

const PLACEHOLDER_HTML = "<h2>Une IA qui pense en français.</h2><p>Posez une question, développez une idée ou commencez par un simple bonjour.</p>";

function restorePlaceholderIfEmpty() {
  if (conversation.children.length) return;
  const ph = document.createElement("div");
  ph.id = "placeholder";
  ph.innerHTML = PLACEHOLDER_HTML;
  conversation.appendChild(ph);
}

function isNearBottom() {
  return conversation.scrollHeight - conversation.scrollTop - conversation.clientHeight < 72;
}

function makeActionButton(kind) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = kind === "copy" ? "msg-action-btn copy-btn" : "msg-action-btn msg-edit";
  btn.title = kind === "copy" ? "Copier" : "Modifier";
  btn.setAttribute("aria-label", kind === "copy" ? "Copier la réponse" : "Modifier ce message");
  btn.innerHTML = kind === "copy" ? COPY_ICON : EDIT_ICON;
  return btn;
}

function addBubble(role, text, { error = false, meta = "", muted = false } = {}) {
  removePlaceholder();
  const wasNearBottom = isNearBottom();

  const el = document.createElement("div");
  el.className = `bubble ${role}${error ? " error" : ""}${muted ? " notice" : ""}`;
  el.dataset.raw = text;

  const content = document.createElement("div");
  content.className = "msg-content";
  if (role === "assistant" && !error && !muted) {
    content.innerHTML = renderMarkdown(text);
  } else {
    content.textContent = text;
  }
  el.appendChild(content);

  if (meta) {
    const metaEl = document.createElement("div");
    metaEl.className = "bubble-meta";
    metaEl.textContent = meta;
    el.appendChild(metaEl);
  }

  if (role === "assistant" && !error && !muted) {
    el.appendChild(makeActionButton("copy"));
  } else if (role === "user") {
    el.appendChild(makeActionButton("edit"));
  }

  conversation.appendChild(el);
  if (role === "user" || wasNearBottom) conversation.scrollTop = conversation.scrollHeight;
  return el;
}

// Modifie un message déjà envoyé : le retire (ainsi que tout ce qui suit,
// puisque la réponse associée n'est plus valable) et le replace dans le
// champ de saisie.
function editMessage(bubbleEl) {
  if (isSending || !bubbleEl) return;
  const raw = bubbleEl.dataset.raw || "";
  let node = bubbleEl;
  while (node) {
    const next = node.nextElementSibling;
    node.remove();
    node = next;
  }
  msgInput.value = raw;
  autoResize();
  msgInput.focus();
  msgInput.setSelectionRange(raw.length, raw.length);
  restorePlaceholderIfEmpty();
}

conversation.addEventListener("click", (e) => {
  const editBtn = e.target.closest(".msg-edit");
  if (editBtn) {
    editMessage(editBtn.closest(".bubble.user"));
    return;
  }
  const copyBtn = e.target.closest(".copy-btn");
  if (copyBtn) {
    const raw = copyBtn.closest(".bubble")?.dataset.raw || "";
    if (!raw || !navigator.clipboard) return;
    navigator.clipboard.writeText(raw).then(() => {
      copyBtn.classList.add("copied");
      copyBtn.innerHTML = CHECK_ICON;
      setTimeout(() => {
        copyBtn.classList.remove("copied");
        copyBtn.innerHTML = COPY_ICON;
      }, 1400);
    }).catch(() => { /* presse-papiers indisponible : on ignore silencieusement */ });
  }
});

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
  const wasNearBottom = isNearBottom();
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
  if (wasNearBottom) conversation.scrollTop = conversation.scrollHeight;

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
      if (data.quota) updateQuota(data.quota);
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
  if (!message || isSending || msgInput.disabled) return;

  isSending = true;
  sendBtn.disabled = true;
  conversation.classList.add("is-sending");

  // Le modèle et les réglages sont figés au moment de l'envoi, même si
  // l'utilisateur les change pendant l'attente.
  const modelId = currentModelId;
  const settingsSnapshot = { ...genSettings };

  addBubble("user", message);
  msgInput.value = "";
  autoResize();

  const thinking = addThinking();
  currentAbortController = new AbortController();

  try {
    const resp = await fetch("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, model: modelId, ...settingsSnapshot }),
      signal: currentAbortController.signal,
    });

    let data;
    try {
      data = await resp.json();
    } catch (parseErr) {
      throw new Error("Réponse du serveur illisible.");
    }

    if (data.quota) updateQuota(data.quota);

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
    thinking.remove();
    if (err.name === "AbortError") {
      addBubble("assistant", "Génération annulée.", { muted: true });
    } else {
      console.error("Erreur lors de l'envoi du message :", err);
      addBubble("assistant", `Erreur : ${err.message}`, { error: true });
    }
  } finally {
    isSending = false;
    currentAbortController = null;
    conversation.classList.remove("is-sending");
    sendBtn.disabled = msgInput.disabled;
    if (!msgInput.disabled) msgInput.focus();
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
    return;
  }
  // Flèche haut dans un champ vide : reprend le dernier message envoyé pour
  // le corriger, comme un historique de terminal.
  if (e.key === "ArrowUp" && !msgInput.value && !isSending) {
    const userBubbles = conversation.querySelectorAll(".bubble.user");
    const last = userBubbles[userBubbles.length - 1];
    if (last) {
      e.preventDefault();
      editMessage(last);
    }
  }
});

msgInput.addEventListener("input", autoResize);

// Échap : ferme le panneau de réglages s'il est ouvert, sinon annule une
// génération en cours.
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (settingsAreOpen()) {
    closeSettings();
    return;
  }
  if (isSending && currentAbortController) currentAbortController.abort();
});

newChatBtn.addEventListener("click", () => {
  conversation.innerHTML = "";
  restorePlaceholderIfEmpty();
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
loadSettings();
