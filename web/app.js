"use strict";

// Hand-drawn detective mascot (real artwork, not procedural): a static
// idle frame plus a 5-frame strip (assets/mascot_strip.png) that raises a
// magnifying glass, played as a CSS sprite animation on hover. The same
// source frames back the floating desktop companion (native_widget.py).
const MASCOT_ASPECT = 275 / 261; // sprite frame height / width

function mascotSvg(size) {
  const height = Math.round(size * MASCOT_ASPECT);
  return `<span class="mascot" style="width:${size}px;height:${height}px"></span>`;
}

// The three roles that debate and vote. The vote panel iterates exactly
// these, so support roles must not be added here or they would sit in it
// forever showing "en attente".
const ROLE_META = {
  heuristic: { label: "Heuristique", color: "var(--heuristic)" },
  critic: { label: "Critique", color: "var(--critic)" },
  writer: { label: "Redacteur", color: "var(--writer)" },
};

// Support roles: they shape the context that the debating roles read, but
// they cast no vote.
const SUPPORT_ROLE_META = {
  context: { label: "Contexte", color: "var(--context)" },
  researcher: { label: "Chercheur", color: "var(--researcher)" },
};

function roleMeta(role) {
  return (
    ROLE_META[role] ||
    SUPPORT_ROLE_META[role] || { label: role, color: "var(--text-faint)" }
  );
}

const state = {
  config: null,
  sessionId: crypto.randomUUID ? crypto.randomUUID() : String(Date.now()),
  running: false,
  documents: [], // [{id, name, text, included, loading}]
};

const el = (id) => document.getElementById(id);
const messagesEl = el("messages");
const composerEl = el("composer");
const promptInput = el("prompt-input");
const sendBtn = el("send-btn");
const backendSelect = el("backend-select");
const modelSelect = el("model-select");
const apiKeySection = el("api-key-section");
const apiKeyInput = el("api-key");
const signupLink = el("signup-link");
const backendHint = el("backend-hint");
const researchToggle = el("research-toggle");
const cyclesRange = el("cycles-range");
const cyclesValue = el("cycles-value");
const tokensRange = el("tokens-range");
const tokensValue = el("tokens-value");
const kindSelect = el("kind-select");
const newChatBtn = el("new-chat");

// ---------------------------------------------------------------- markdown

function escapeHtml(text) {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function codeBlock(lang, code) {
  const label = (lang || "code").toLowerCase();
  // The raw text is stashed on the element so the copy button hands back
  // exactly what the model wrote, not the HTML-escaped rendering.
  return (
    `<div class="code-block" data-lang="${escapeHtml(label)}">` +
    `<div class="code-head"><span class="code-lang">${escapeHtml(label)}</span>` +
    `<button class="code-copy" type="button">Copier</button></div>` +
    `<pre><code class="lang-${escapeHtml(label)}">${escapeHtml(code)}</code></pre>` +
    `</div>`
  );
}

function renderMarkdown(raw) {
  const text = raw || "";
  let html = "";
  let index = 0;

  // Walked manually rather than split on a paired-fence regex: generation is
  // routinely cut off by max_tokens mid-block, leaving an opening ``` with no
  // closing one. A paired regex silently fails to match there and dumps the
  // whole remainder as prose with a stray ``` in it.
  const opener = /```([\w+#-]*)[ \t]*\r?\n?/g;
  while (index < text.length) {
    opener.lastIndex = index;
    const open = opener.exec(text);
    if (!open) {
      html += renderInline(text.slice(index));
      break;
    }
    html += renderInline(text.slice(index, open.index));

    const bodyStart = open.index + open[0].length;
    const close = text.indexOf("```", bodyStart);
    if (close === -1) {
      // Unterminated: everything left is code. Better a complete block than
      // a broken one, since the answer really was truncated there.
      html += codeBlock(open[1], text.slice(bodyStart).replace(/\s+$/, ""));
      break;
    }
    html += codeBlock(open[1], text.slice(bodyStart, close).replace(/\s+$/, ""));
    index = close + 3;
  }
  return html;
}

function renderMathIn(element) {
  if (typeof window.renderMathInElement !== "function") return;
  try {
    window.renderMathInElement(element, {
      delimiters: [
        { left: "$$", right: "$$", display: true },
        { left: "\\[", right: "\\]", display: true },
        { left: "\\(", right: "\\)", display: false },
        { left: "$", right: "$", display: false },
      ],
      throwOnError: false,
    });
  } catch (err) {
    // A malformed formula must never break the rest of the message.
  }
}

function renderInline(text) {
  const paragraphs = text.split(/\n{2,}/).filter((p) => p.trim().length > 0);
  return paragraphs
    .map((p) => {
      let out = escapeHtml(p.trim());
      out = out.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
      out = out.replace(/`([^`]+)`/g, "<code>$1</code>");
      out = out.replace(/\n/g, "<br/>");
      return `<p>${out}</p>`;
    })
    .join("");
}

// ---------------------------------------------------------------- chat UI

function clearEmptyState() {
  const empty = messagesEl.querySelector(".empty-state");
  if (empty) empty.remove();
}

function addUserMessage(text) {
  clearEmptyState();
  const wrap = document.createElement("div");
  wrap.className = "msg user";
  wrap.innerHTML = `
    <div class="msg-avatar">Toi</div>
    <div class="msg-body">
      <div class="msg-content">${renderMarkdown(text)}</div>
    </div>`;
  messagesEl.appendChild(wrap);
  renderMathIn(wrap.querySelector(".msg-content"));
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

const ORBIT_HTML = `<span class="orbit"><span class="agent a1"></span><span class="agent a2"></span><span class="agent a3"></span></span>`;

function addAssistantMessage() {
  clearEmptyState();
  const wrap = document.createElement("div");
  wrap.className = "msg assistant";
  wrap.innerHTML = `
    <div class="msg-avatar thinking-avatar">${mascotSvg(24)}${ORBIT_HTML}</div>
    <div class="msg-body">
      <div class="msg-content">
        <div class="thinking">${ORBIT_HTML}<span class="thinking-text">Les agents se concertent…</span></div>
      </div>
      <div class="msg-meta"></div>
    </div>`;
  messagesEl.appendChild(wrap);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return wrap;
}

function updateThinkingStatus(wrap, text) {
  const label = wrap.querySelector(".thinking-text");
  if (label) label.textContent = text;
}

function finalizeAssistantMessage(wrap, { finalSolution, consensusReached, completedCycles, backendLabel }) {
  const avatar = wrap.querySelector(".msg-avatar");
  avatar.className = "msg-avatar";
  avatar.innerHTML = mascotSvg(24);
  const content = wrap.querySelector(".msg-content");
  content.innerHTML = renderMarkdown(finalSolution || "(pas de reponse)");
  renderMathIn(content);
  const meta = wrap.querySelector(".msg-meta");
  const pillClass = consensusReached ? "ok" : "warn";
  const pillText = consensusReached ? "Consensus atteint" : "Pas de consensus";
  meta.innerHTML = `<span class="status-pill ${pillClass}">${pillText}</span><span>${completedCycles} cycle(s) · ${backendLabel}</span>`;
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function errorInAssistantMessage(wrap, message) {
  const avatar = wrap.querySelector(".msg-avatar");
  avatar.className = "msg-avatar";
  avatar.innerHTML = mascotSvg(24);
  const content = wrap.querySelector(".msg-content");
  content.innerHTML = `<p style="color:var(--danger)">${escapeHtml(message)}</p>`;
}

// ---------------------------------------------------------------- side panel
//
// Replaced the old debug view (live vote states, per-cycle debate excerpts,
// a temperature-prior chart) with two things people actually asked to use:
// attaching documents as context, and saving/reloading past discussions.
// The panel itself is collapsible - closed state persisted so it stays
// out of the way once dismissed.

const documentListEl = el("document-list");
const discussionListEl = el("discussion-list");
const sidePanelEl = el("side-panel");
const sidePanelToggleEl = el("side-panel-toggle");

function setSidePanelCollapsed(collapsed) {
  document.body.classList.toggle("side-panel-collapsed", collapsed);
  sidePanelToggleEl.hidden = !collapsed;
  try {
    localStorage.setItem("3loop_side_panel_collapsed", collapsed ? "1" : "0");
  } catch {
    /* ignore */
  }
}

el("side-panel-close").addEventListener("click", () => setSidePanelCollapsed(true));
sidePanelToggleEl.addEventListener("click", () => setSidePanelCollapsed(false));
try {
  setSidePanelCollapsed(localStorage.getItem("3loop_side_panel_collapsed") === "1");
} catch {
  /* ignore */
}

// ---- documents (RAG-lite: attach files, fold their text into the prompt) --

function renderDocumentList() {
  if (state.documents.length === 0) {
    documentListEl.className = "doc-list muted";
    documentListEl.innerHTML = "Aucun document.";
    return;
  }
  documentListEl.className = "doc-list";
  documentListEl.innerHTML = state.documents
    .map(
      (doc) => `
      <div class="doc-item">
        <label class="doc-check">
          <input type="checkbox" data-doc-id="${doc.id}" ${doc.included ? "checked" : ""} />
          <span class="doc-name" title="${escapeHtml(doc.name)}">${escapeHtml(doc.name)}</span>
        </label>
        <span class="doc-meta">${doc.text.length.toLocaleString("fr-FR")} car.</span>
        <button type="button" class="doc-remove" data-doc-remove="${doc.id}" aria-label="Retirer">×</button>
      </div>`
    )
    .join("");
}

documentListEl.addEventListener("change", (event) => {
  const id = event.target.dataset.docId;
  if (!id) return;
  const doc = state.documents.find((d) => d.id === id);
  if (doc) doc.included = event.target.checked;
});

documentListEl.addEventListener("click", (event) => {
  const id = event.target.dataset.docRemove;
  if (!id) return;
  state.documents = state.documents.filter((d) => d.id !== id);
  renderDocumentList();
});

el("document-input").addEventListener("change", async (event) => {
  const files = [...event.target.files];
  event.target.value = ""; // allow re-picking the same file later
  for (const file of files) {
    await attachDocument(file);
  }
});

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result.split(",", 2)[1]);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

async function attachDocument(file) {
  const placeholderId = `doc_${Date.now()}_${Math.random().toString(36).slice(2)}`;
  state.documents.push({ id: placeholderId, name: file.name, text: "", included: true, loading: true });
  renderDocumentList();
  try {
    const content_base64 = await fileToBase64(file);
    const response = await fetch("/api/documents", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: file.name, content_base64 }),
    });
    const payload = await response.json();
    const entry = state.documents.find((d) => d.id === placeholderId);
    if (!entry) return; // removed while uploading
    if (payload.error) {
      state.documents = state.documents.filter((d) => d.id !== placeholderId);
      errorInAssistantMessage(addAssistantMessage(), `Document "${file.name}": ${payload.error}`);
    } else {
      entry.text = payload.text;
      entry.loading = false;
    }
  } catch (err) {
    state.documents = state.documents.filter((d) => d.id !== placeholderId);
    errorInAssistantMessage(addAssistantMessage(), `Document "${file.name}": ${err}`);
  }
  renderDocumentList();
}

function attachedDocumentsContext() {
  const included = state.documents.filter((d) => d.included && d.text && !d.loading);
  if (included.length === 0) return "";
  return included
    .map((d) => `Document "${d.name}":\n---\n${d.text}\n---`)
    .join("\n\n");
}

// ---- discussions (save / reload / delete past conversations) -------------

function loadDiscussions() {
  try {
    return JSON.parse(localStorage.getItem("3loop_discussions") || "[]");
  } catch {
    return [];
  }
}

function saveDiscussions(list) {
  try {
    localStorage.setItem("3loop_discussions", JSON.stringify(list));
  } catch {
    /* ignore: e.g. storage quota - the in-memory list still works this session */
  }
}

function renderDiscussionList() {
  const discussions = loadDiscussions();
  if (discussions.length === 0) {
    discussionListEl.className = "doc-list muted";
    discussionListEl.innerHTML = "Aucune discussion sauvegardée.";
    return;
  }
  discussionListEl.className = "doc-list";
  discussionListEl.innerHTML = discussions
    .slice()
    .reverse()
    .map(
      (d) => `
      <div class="doc-item">
        <button type="button" class="doc-name doc-name-btn" data-discussion-load="${d.id}" title="Recharger">
          ${escapeHtml(d.title)}
        </button>
        <span class="doc-meta">${new Date(d.savedAt).toLocaleDateString("fr-FR")}</span>
        <button type="button" class="doc-remove" data-discussion-export="${d.id}" aria-label="Exporter en .md" title="Exporter en .md">⇩</button>
        <button type="button" class="doc-remove" data-discussion-remove="${d.id}" aria-label="Supprimer">×</button>
      </div>`
    )
    .join("");
}

el("save-discussion").addEventListener("click", () => {
  const messages = [...messagesEl.querySelectorAll(".msg")]
    .map((node) => ({
      role: node.classList.contains("user") ? "user" : "assistant",
      text: node.querySelector(".msg-content")?.innerText.trim() || "",
    }))
    .filter((m) => m.text);
  if (messages.length === 0) return;
  const firstUserMessage = messages.find((m) => m.role === "user");
  const discussions = loadDiscussions();
  discussions.push({
    id: `disc_${Date.now()}`,
    title: (firstUserMessage ? firstUserMessage.text : "Discussion").slice(0, 60),
    savedAt: new Date().toISOString(),
    messages,
  });
  saveDiscussions(discussions);
  renderDiscussionList();
});

discussionListEl.addEventListener("click", (event) => {
  const loadId = event.target.dataset.discussionLoad;
  const removeId = event.target.dataset.discussionRemove;
  const exportId = event.target.dataset.discussionExport;
  const discussions = loadDiscussions();

  if (loadId) {
    const discussion = discussions.find((d) => d.id === loadId);
    if (!discussion) return;
    clearEmptyState();
    messagesEl.innerHTML = "";
    for (const message of discussion.messages) {
      const wrap = document.createElement("div");
      wrap.className = `msg ${message.role}`;
      wrap.innerHTML = `
        <div class="msg-avatar">${message.role === "user" ? "Toi" : mascotSvg(24)}</div>
        <div class="msg-body"><div class="msg-content">${renderMarkdown(message.text)}</div></div>`;
      messagesEl.appendChild(wrap);
      renderMathIn(wrap.querySelector(".msg-content"));
    }
    messagesEl.scrollTop = messagesEl.scrollHeight;
  } else if (removeId) {
    saveDiscussions(discussions.filter((d) => d.id !== removeId));
    renderDiscussionList();
  } else if (exportId) {
    const discussion = discussions.find((d) => d.id === exportId);
    if (!discussion) return;
    exportDiscussionAsMarkdown(discussion);
  }
});

function exportDiscussionAsMarkdown(discussion) {
  // Plain markdown, not JSON: this is meant to be handed to another tool
  // (Claude Code, Codex, OpenCode GO...) as context to resume with - a
  // transcript is exactly the shape a coding-agent CLI reads well.
  const lines = [
    `# ${discussion.title}`,
    "",
    `_Discussion 3loop sauvegardée le ${new Date(discussion.savedAt).toLocaleString("fr-FR")}._`,
    "",
  ];
  for (const message of discussion.messages) {
    lines.push(message.role === "user" ? "## Question" : "## Réponse", "", message.text, "");
  }
  const blob = new Blob([lines.join("\n")], { type: "text/markdown" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `3loop-${discussion.id}.md`;
  link.click();
  URL.revokeObjectURL(url);
}

renderDocumentList();
renderDiscussionList();

// ---------------------------------------------------------------- config / backend selection

async function loadConfig() {
  const res = await fetch("/api/config");
  state.config = await res.json();
  populateBackendOptions();
}

function populateBackendOptions() {
  const options = [];
  const opencode = state.config.opencode || { available: false, models: [] };
  // Listed first and preferred by default: local inference is capped by what
  // a ~3B model can do, while OpenCode reaches whatever frontier model the
  // user has configured.
  if (opencode.available) {
    options.push({ value: "opencode", label: "OpenCode (le plus capable)" });
  }
  const claudeCode = state.config.claude_code || { available: false };
  if (claudeCode.available) {
    options.push({ value: "claude_code", label: "Claude Code" });
  }
  const codex = state.config.codex || { available: false };
  if (codex.available) {
    options.push({ value: "codex", label: "Codex" });
  }
  for (const [key, info] of Object.entries(state.config.cloud_providers)) {
    options.push({ value: key, label: key === "groq" ? "Groq (cloud gratuit)" : "NVIDIA Nemotron (cloud gratuit)" });
  }
  const localGguf = state.config.local_gguf || [];
  if (localGguf.length > 0) {
    options.push({ value: "llama_cpp", label: "GGUF local (le plus rapide)" });
  }
  if ((state.config.ollama_models || []).length > 0) {
    options.push({ value: "igpu", label: "iGPU local (Vulkan)" });
  }
  options.push({ value: "ollama", label: "Ollama (local)" });
  options.push({ value: "demo", label: "Démo hors-ligne" });

  backendSelect.innerHTML = options.map((o) => `<option value="${o.value}">${o.label}</option>`).join("");

  const hasStoredKey = Object.keys(state.config.cloud_providers).some((p) => loadStoredKey(p));
  if (opencode.available) backendSelect.value = "opencode";
  else if (hasStoredKey) backendSelect.value = Object.keys(state.config.cloud_providers).find((p) => loadStoredKey(p));
  else if (localGguf.length > 0) backendSelect.value = "llama_cpp";
  else if (state.config.ollama_models.length > 0) backendSelect.value = "ollama";
  else backendSelect.value = "demo";

  updateBackendUI();
}

function loadStoredKey(provider) {
  try {
    return localStorage.getItem(`3loop_key_${provider}`) || "";
  } catch {
    return "";
  }
}

function storeKey(provider, key) {
  try {
    localStorage.setItem(`3loop_key_${provider}`, key);
  } catch {
    /* ignore */
  }
}

function updateBackendUI() {
  const backend = backendSelect.value;
  const cloudInfo = state.config.cloud_providers[backend];

  if (cloudInfo) {
    apiKeySection.hidden = false;
    apiKeyInput.value = loadStoredKey(backend);
    signupLink.href = cloudInfo.signup_url;
    signupLink.textContent = `Créer une clé gratuite (${cloudInfo.signup_url.replace("https://", "")})`;
    modelSelect.innerHTML = cloudInfo.models.map((m) => `<option value="${m}">${m}</option>`).join("");
    backendHint.textContent = apiKeyInput.value ? "" : "Colle ta clé API gratuite pour activer ce backend.";
    backendHint.className = apiKeyInput.value ? "hint" : "hint warn";
  } else if (backend === "opencode") {
    apiKeySection.hidden = true;
    const opencode = state.config.opencode || { models: [], default: "" };
    modelSelect.innerHTML = opencode.models
      .map(
        (m) =>
          `<option value="${escapeHtml(m)}"${m === opencode.default ? " selected" : ""}>${escapeHtml(m)}</option>`
      )
      .join("");
    // Say plainly that an external process runs, and where its log is: a
    // background subprocess nobody can see is impossible to debug.
    backendHint.textContent =
      "Delegue a OpenCode installe sur ta machine (processus externe, sans fenetre). Journal : " +
      (opencode.log_path || "~/.3loop/opencode.log");
    backendHint.className = "hint";
  } else if (backend === "claude_code" || backend === "codex") {
    apiKeySection.hidden = true;
    const info = state.config[backend] || { models: [], default: "" };
    modelSelect.innerHTML = info.models
      .map((m) => {
        const label = m || "(par defaut de la CLI)";
        return `<option value="${escapeHtml(m)}"${m === info.default ? " selected" : ""}>${escapeHtml(label)}</option>`;
      })
      .join("");
    const label = backend === "claude_code" ? "Claude Code" : "Codex";
    backendHint.textContent =
      `Delegue a ${label} installe sur ta machine (processus externe, sans fenetre, ` +
      `execution en lecture seule - aucun fichier modifie). Journal : ~/.3loop/${backend === "claude_code" ? "claude-code" : "codex"}.log`;
    backendHint.className = "hint";
  } else if (backend === "llama_cpp") {
    apiKeySection.hidden = true;
    const models = state.config.local_gguf || [];
    modelSelect.innerHTML = models
      .map((m) => `<option value="${escapeHtml(m.path)}">${escapeHtml(m.label)}</option>`)
      .join("");
    backendHint.textContent = "Poids GGUF chargés directement : environ 2x plus rapide qu'Ollama.";
    backendHint.className = "hint";
  } else if (backend === "igpu") {
    apiKeySection.hidden = true;
    const models = state.config.ollama_models || [];
    modelSelect.innerHTML = models.map((m) => `<option value="${m}">${m}</option>`).join("");
    // Measured on a Ryzen 5000U: prefill is compute-bound and gains from the
    // iGPU, decode is memory-bound and barely does - the two share the same
    // DRAM. Worth it on long prompts, not on long generations.
    backendHint.textContent =
      "Decharge sur le GPU integre via Vulkan. Prefill +32%, decodage +9% : "
      + "avantageux sur les prompts longs, pas sur les reponses longues.";
    backendHint.className = "hint";
  } else if (backend === "ollama") {
    apiKeySection.hidden = true;
    const models = state.config.ollama_models;
    if (models.length > 0) {
      modelSelect.innerHTML = models.map((m) => `<option value="${m}">${m}</option>`).join("");
      backendHint.textContent = "Inférence CPU locale via serveur Ollama.";
      backendHint.className = "hint";
    } else {
      modelSelect.innerHTML = "";
      backendHint.textContent = "Aucun serveur Ollama détecté sur localhost:11434.";
      backendHint.className = "hint warn";
    }
  } else {
    apiKeySection.hidden = true;
    modelSelect.innerHTML = "";
    backendHint.textContent = "Réponses simulées, aucune installation requise.";
    backendHint.className = "hint";
  }
}

// One delegated listener rather than one per block: code blocks are created
// continuously as answers stream in.
messagesEl.addEventListener("click", (event) => {
  const button = event.target.closest(".code-copy");
  if (!button) return;
  const code = button.closest(".code-block")?.querySelector("code");
  if (!code) return;
  navigator.clipboard.writeText(code.textContent).then(
    () => {
      button.textContent = "Copié";
      setTimeout(() => (button.textContent = "Copier"), 1500);
    },
    () => (button.textContent = "Échec"),
  );
});

backendSelect.addEventListener("change", updateBackendUI);
apiKeyInput.addEventListener("input", () => {
  storeKey(backendSelect.value, apiKeyInput.value);
  backendHint.textContent = apiKeyInput.value ? "" : "Colle ta clé API gratuite pour activer ce backend.";
  backendHint.className = apiKeyInput.value ? "hint" : "hint warn";
});

cyclesRange.addEventListener("input", () => (cyclesValue.textContent = cyclesRange.value));
tokensRange.addEventListener("input", () => (tokensValue.textContent = tokensRange.value));

newChatBtn.addEventListener("click", () => {
  messagesEl.innerHTML = `
    <div class="empty-state">
      <div id="empty-badge" class="empty-badge"></div>
      <h1>3loop</h1>
      <p>Heuristique → Critique → Rédacteur → Vote de consensus.<br/>Pose une question de code, de maths, ou une recherche.</p>
    </div>`;
  document.getElementById("empty-badge").innerHTML = mascotSvg(56);
});

// ---------------------------------------------------------------- run + SSE

function currentBackendLabel() {
  const opt = backendSelect.options[backendSelect.selectedIndex];
  return opt ? opt.textContent : backendSelect.value;
}

async function runPrompt(prompt, enginePrompt = prompt) {
  addUserMessage(prompt);
  const wrap = addAssistantMessage();
  const votes = {};

  const payload = {
    prompt: enginePrompt,
    session_id: state.sessionId,
    backend: backendSelect.value,
    model: modelSelect.value,
    api_key: apiKeyInput.value,
    research: researchToggle.checked,
    max_cycles: Number(cyclesRange.value),
    max_tokens: Number(tokensRange.value),
    task_kind: kindSelect.value,
  };

  let response;
  try {
    response = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch (err) {
    errorInAssistantMessage(wrap, `Impossible de contacter le moteur local: ${err}`);
    return;
  }

  // Reassure the user during a slow cloud call: no event for a while just
  // means the current LLM request hasn't returned yet, not that it's stuck.
  let idleTimer = null;
  const armIdleNotice = () => {
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => {
      updateThinkingStatus(wrap, "Toujours en cours (l'API peut etre lente)…");
    }, 15000);
  };
  armIdleNotice();

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let sepIndex;
      while ((sepIndex = buffer.indexOf("\n\n")) !== -1) {
        const rawEvent = buffer.slice(0, sepIndex);
        buffer = buffer.slice(sepIndex + 2);
        armIdleNotice();
        handleSseEvent(rawEvent, wrap, votes, payload);
      }
    }
  } finally {
    clearTimeout(idleTimer);
  }
}

function handleSseEvent(rawEvent, wrap, votes, payload) {
  let eventName = "message";
  let dataLine = "";
  for (const line of rawEvent.split("\n")) {
    if (line.startsWith("event:")) eventName = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLine += line.slice(5).trim();
  }
  if (!dataLine) return;
  let data;
  try {
    data = JSON.parse(dataLine);
  } catch {
    return;
  }

  switch (eventName) {
    case "cycle_started":
      updateThinkingStatus(wrap, `Cycle ${data.cycle} demarre…`);
      break;
    case "research_query":
      if (data.role) updateThinkingStatus(wrap, `${roleMeta(data.role).label} prepare une recherche…`);
      break;
    case "vote":
      if (data.role) {
        votes[data.role] = { resolved: data.resolved, confidence: data.confidence };
        updateThinkingStatus(wrap, `${roleMeta(data.role).label} a vote (cycle ${data.cycle})…`);
      }
      break;
    case "agent_output":
      if (data.role) {
        updateThinkingStatus(wrap, `${roleMeta(data.role).label} a repondu, etape suivante…`);
      }
      break;
    case "run_completed":
      finalizeAssistantMessage(wrap, {
        finalSolution: data.final_solution,
        consensusReached: data.consensus_reached,
        completedCycles: data.completed_cycles,
        backendLabel: currentBackendLabel(),
      });
      break;
    case "error":
      errorInAssistantMessage(wrap, data.message || "erreur inconnue");
      break;
    default:
      break;
  }
}

// ---------------------------------------------------------------- composer

promptInput.addEventListener("input", () => {
  promptInput.style.height = "auto";
  promptInput.style.height = Math.min(promptInput.scrollHeight, 160) + "px";
  sendBtn.disabled = promptInput.value.trim().length === 0 || state.running;
});

promptInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    composerEl.requestSubmit();
  }
});

// ---------------------------------------------------------------- scraping
//
// A URL typed or pasted into the composer is fetched, stripped to its
// readable text (server-side: HTML chrome and scripts never reach the
// browser), and folded into what the engine sees - the user's own message
// still shows exactly what they typed, only the model gets the page.

const URL_RE = /https?:\/\/[^\s]+/;

function stripUrls(text) {
  return text.replace(/https?:\/\/[^\s]+/g, "").replace(/\s{2,}/g, " ").trim();
}

async function scrapeUrl(url) {
  const response = await fetch("/api/scrape", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url }),
  });
  return response.json();
}

composerEl.addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = promptInput.value.trim();
  if (!text || state.running) return;
  promptInput.value = "";
  promptInput.style.height = "auto";
  state.running = true;
  sendBtn.disabled = true;

  const urlMatch = text.match(URL_RE);
  let enginePrompt = text;
  const originalPlaceholder = promptInput.placeholder;

  try {
    if (urlMatch) {
      updateStatus(`Lecture de ${urlMatch[0]}…`);
      const page = await scrapeUrl(urlMatch[0]);
      if (page.error) {
        updateStatus("");
        errorInAssistantMessage(addAssistantMessage(), `Page illisible: ${page.error}`);
        return;
      }
      const question = stripUrls(text) || "Resume cette page.";
      enginePrompt =
        `Page web "${page.title || page.url}" (${page.url}):\n---\n${page.text}\n---\n\n` +
        `Question de l'utilisateur: ${question}`;
      updateStatus("");
    }
    const documentsContext = attachedDocumentsContext();
    if (documentsContext) {
      enginePrompt = `${documentsContext}\n\nQuestion de l'utilisateur: ${enginePrompt}`;
    }
    await runPrompt(text, enginePrompt);
  } finally {
    state.running = false;
    sendBtn.disabled = promptInput.value.trim().length === 0;
    promptInput.placeholder = originalPlaceholder;
  }
});

function updateStatus(text) {
  promptInput.placeholder = text || "Écris ton message…";
}

// ---------------------------------------------------------------- boot

el("brand-badge").innerHTML = mascotSvg(24);
el("empty-badge").innerHTML = mascotSvg(56);
loadConfig();
