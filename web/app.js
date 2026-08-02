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
  tempHistory: [], // [{cycle, heuristic, critic, writer}]
  debateEntries: [],
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
const panelStatus = el("panel-status");
const panelSources = el("panel-sources");
const panelDebate = el("panel-debate");
const tempChart = el("temp-chart");

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

function resetSidePanel() {
  panelStatus.textContent = "Aucune execution pour l'instant.";
  panelStatus.className = "muted";
  panelSources.innerHTML = "Aucune source pour l'instant.";
  panelSources.className = "muted";
  panelDebate.innerHTML = "Rien a afficher pour l'instant.";
  panelDebate.className = "muted";
  state.debateEntries = [];
}

function setVotePanel(votes) {
  const rows = Object.entries(ROLE_META)
    .map(([role, meta]) => {
      const vote = votes[role];
      let text = "en attente";
      let dotColor = "var(--text-faint)";
      if (vote) {
        text = vote.resolved ? `résolu (${Math.round(vote.confidence * 100)}%)` : `à revoir (${Math.round(vote.confidence * 100)}%)`;
        dotColor = vote.resolved ? "var(--ok)" : "var(--warn)";
      }
      const resolvedClass = vote && vote.resolved ? " resolved" : "";
      return `<div class="vote-row${resolvedClass}"><span class="vote-role"><span class="vote-dot" style="background:${dotColor}"></span>${meta.label}</span><span>${text}</span></div>`;
    })
    .join("");
  panelStatus.innerHTML = rows;
  panelStatus.className = "";
}

function appendDebateEntry(role, cycle, content) {
  const meta = roleMeta(role);
  state.debateEntries.push({ role, meta, cycle, content });
  panelDebate.className = "";
  panelDebate.innerHTML = state.debateEntries
    .map(
      (e) =>
        `<div class="debate-entry"><span class="role"><span class="vote-dot" style="background:${e.meta.color}"></span>${e.meta.label}</span> - cycle ${e.cycle}<br/>${escapeHtml((e.content || "").slice(0, 220))}${(e.content || "").length > 220 ? "..." : ""}</div>`
    )
    .join("");
}

function setSourcesPanel(sources) {
  if (!sources || sources.length === 0) return;
  panelSources.className = "";
  panelSources.innerHTML = sources
    .map((s) => `<div><a href="${s.url}" target="_blank" rel="noopener">${escapeHtml(s.title)}</a></div>`)
    .join("");
}

// ---------------------------------------------------------------- temp chart

function pushTempPoint(cycle, posterior) {
  state.tempHistory.push({ cycle, ...posterior });
  drawTempChart();
}

function drawTempChart() {
  const w = 260, h = 110, pad = 8;
  const tmin = 0.2, tmax = 0.7;
  const points = state.tempHistory;
  if (points.length === 0) {
    tempChart.innerHTML = "";
    return;
  }
  const n = Math.max(points.length - 1, 1);
  const scaleX = (i) => pad + (i / n) * (w - pad * 2);
  const scaleY = (v) => h - pad - ((v - tmin) / (tmax - tmin)) * (h - pad * 2);

  const seriesFor = (key) =>
    points.map((p, i) => `${scaleX(i)},${scaleY(p[key] ?? tmin)}`).join(" ");

  const colors = { heuristic: "var(--heuristic)", critic: "var(--critic)", writer: "var(--writer)" };
  let svg = "";
  for (const key of ["heuristic", "critic", "writer"]) {
    svg += `<polyline points="${seriesFor(key)}" fill="none" stroke="${colors[key]}" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"/>`;
  }
  tempChart.innerHTML = svg;
}

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
  state.tempHistory = [];
  drawTempChart();
  resetSidePanel();
});

// ---------------------------------------------------------------- run + SSE

function currentBackendLabel() {
  const opt = backendSelect.options[backendSelect.selectedIndex];
  return opt ? opt.textContent : backendSelect.value;
}

async function runPrompt(prompt) {
  addUserMessage(prompt);
  const wrap = addAssistantMessage();
  resetSidePanel();
  const votes = {};

  const payload = {
    prompt,
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
        setVotePanel(votes);
        updateThinkingStatus(wrap, `${roleMeta(data.role).label} a vote (cycle ${data.cycle})…`);
      }
      break;
    case "agent_output":
      if (data.role) {
        appendDebateEntry(data.role, data.cycle, data.content);
        updateThinkingStatus(wrap, `${roleMeta(data.role).label} a repondu, etape suivante…`);
      }
      break;
    case "research_sources":
      setSourcesPanel(data.sources);
      break;
    case "prior_updated":
      if (data.posterior) pushTempPoint(data.cycle, data.posterior);
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

composerEl.addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = promptInput.value.trim();
  if (!text || state.running) return;
  promptInput.value = "";
  promptInput.style.height = "auto";
  state.running = true;
  sendBtn.disabled = true;
  try {
    await runPrompt(text);
  } finally {
    state.running = false;
    sendBtn.disabled = promptInput.value.trim().length === 0;
  }
});

// ---------------------------------------------------------------- boot

el("brand-badge").innerHTML = mascotSvg(24);
el("empty-badge").innerHTML = mascotSvg(56);
loadConfig();
