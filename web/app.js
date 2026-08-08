"use strict";

// Pixel-art mascots share one 64x64 / 34-frame contract. The selected
// task kind chooses the visual identity without changing the backend payload:
// LOUPe is the general/researcher, MATh is mathematics, and CODy is code.
// Animation remains entirely in style.css (idle loop, hop on hover).
const MASCOT_PROFILES = {
  researcher: { className: "researcher", name: "LOUPe", label: "Assistant général" },
  pixelbit: { className: "pixelbit", name: "MATh", label: "Assistant mathématique" },
  cody: { className: "cody", name: "CODy", label: "Assistant code" },
};

function mascotProfile(kind = kindSelect?.value) {
  const normalized = String(kind || "auto").toLowerCase();
  if (normalized === "math") return MASCOT_PROFILES.pixelbit;
  if (normalized === "code") return MASCOT_PROFILES.cody;
  return MASCOT_PROFILES.researcher;
}

function mascotSprite(size, { watch = false, kind = kindSelect?.value } = {}) {
  const side = Math.round(size);
  const profile = mascotProfile(kind);
  const classes = `mascot ${profile.className}${watch ? " watch" : ""}`;
  return `<span class="${classes}" data-mascot="${profile.className}" aria-label="${profile.label}" role="img" style="width:${side}px;height:${side}px"></span>`;
}

// The floating desktop companion is a separate Win32 process: it cannot read
// this document, so the choice is published to the local server and the widget
// polls it. Fire-and-forget on purpose - the in-page theme must switch even if
// the widget is absent (non-Windows, or disabled).
function publishVisualTheme(className) {
  fetch("/api/v1/theme", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ theme: className }),
  }).catch(() => {
    /* No companion listening: the interface theme is unaffected. */
  });
}

function applyVisualTheme(kind = kindSelect?.value) {
  const profile = mascotProfile(kind);
  publishVisualTheme(profile.className);
  document.body.dataset.visualTheme = profile.className;
  document.body.dataset.mascot = profile.className;
  const brandName = document.querySelector(".brand-name");
  if (brandName) brandName.textContent = profile.name;
  const emptyTitle = document.querySelector(".empty-state h1");
  if (emptyTitle) emptyTitle.textContent = profile.name;
  const emptyBadge = document.getElementById("empty-badge");
  if (emptyBadge) emptyBadge.innerHTML = mascotSprite(64, { kind });
  const brandBadge = document.getElementById("brand-badge");
  if (brandBadge) brandBadge.innerHTML = mascotSprite(32, { kind });
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

// Feedback is deliberately available only for inference engines whose model
// remains on this machine. Cloud providers and delegated coding CLIs never
// get a feedback request, even if a user changes the selector while a turn is
// finishing.
const LOCAL_FEEDBACK_BACKENDS = new Set(["llama_cpp", "igpu", "ollama"]);

function isLocalFeedbackBackend(backend) {
  return LOCAL_FEEDBACK_BACKENDS.has(String(backend || "").trim());
}

const state = {
  config: null,
  sessionId: crypto.randomUUID ? crypto.randomUUID() : String(Date.now()),
  running: false,
  // One foreground run owns an AbortController and receives the server's
  // opaque job id as soon as the first SSE event arrives.
  activeRun: null,
  documents: [], // [{id, name, text, included, loading}]
  // Completed turns are sent with every request so a backend/model switch
  // keeps the same conversation context instead of starting from zero.
  conversation: [], // [{role: "user"|"assistant", text: string}]
  // Conversation currently loaded from the side panel, highlighted in the list.
  activeConversationId: null,
  // SQLite-backed notebook entries. localStorage is retained only as a
  // migration/fallback when the local API is unavailable.
  discussions: [],
  discussionsLoaded: false,
  discussionsRemote: false,
  // Pending CLI approval/questions keyed by the interaction id sent by SSE.
  cliInteractions: new Map(),
  // Guards against concurrent /api/compact calls (one summary at a time).
  compacting: false,
  // A background search is streaming: its transient status messages must not
  // be overwritten by the idle "which searcher is installed" line.
  researchRunning: false,
  scientificPapers: [],
  selectedPaperIds: new Set(),
  scientificResults: [],
  // Results remain addressable after a newer search so buttons in archived
  // research messages still save the reference they display.
  scientificRecordsByKey: new Map(),
  notebookNotes: [],
  datasets: [],
};

const el = (id) => document.getElementById(id);
const messagesEl = el("messages");
const composerEl = el("composer");
const promptInput = el("prompt-input");
const sendBtn = el("send-btn");
const backendSelect = el("backend-select");
const modelSection = el("model-section");
const modelSelect = el("model-select");
const apiKeySection = el("api-key-section");
const apiKeyInput = el("api-key");
const signupLink = el("signup-link");
const backendHint = el("backend-hint");
const customModelSection = el("custom-model-section");
const customModelInput = el("custom-model");
const codingWriteSection = el("coding-write-section");
const codingWriteToggle = el("coding-write-toggle");
const codingWorkspaceInput = el("coding-workspace");
const codingWriteHint = el("coding-write-hint");
const researchToggle = el("research-toggle");
const thinkingControl = el("thinking-control");
const reflectionSection = el("reflection-section");
const reflectionSelect = el("reflection-select");
const reflectionHint = el("reflection-hint");
const thinkingToggle = el("thinking-toggle");
const cyclesRange = el("cycles-range");
const cyclesValue = el("cycles-value");
const tokensRange = el("tokens-range");
const tokensValue = el("tokens-value");
const kindSelect = el("kind-select");
const newChatBtn = el("new-chat");
const researchQuestionInput = el("research-question");
const researchNowButton = el("research-now");
const researchStatus = el("research-status");
const compactStatusEl = el("compact-status");
const scientificSearchInput = el("scientific-search-input");
const scientificSearchButton = el("scientific-search-button");
const scientificSearchStatus = el("scientific-search-status");
const scientificResultsEl = el("scientific-results");
const librarySearchInput = el("library-search-input");
const libraryListEl = el("scientific-library-list");
const libraryExportFormat = el("library-export-format");
const libraryExportButton = el("library-export-button");
const compareButton = el("compare-selected-button");
const notebookNoteTitle = el("notebook-note-title");
const notebookNoteBody = el("notebook-note-body");
const notebookSaveButton = el("notebook-save-button");
const notebookListEl = el("notebook-list");
const helpNowButton = el("help-now");
const helpStatus = el("help-status");
const eyeTrackingStartButton = el("eye-tracking-start");
const eyeTrackingStopButton = el("eye-tracking-stop");

// ---------------------------------------------------------------- markdown

function escapeHtml(text) {
  return String(text || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
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

function resolveImageSource(source) {
  const value = String(source || "").trim();
  if (/^data:image\/(?:png|jpeg|gif|webp);base64,/i.test(value)) return value;
  if (/^https?:\/\//i.test(value)) return value;
  if (value.startsWith("/api/asset?")) return value;
  const clean = value.replace(/^file:\/\//i, "");
  if (!/\.(png|jpe?g|gif|webp|bmp|svg)(?:\?.*)?$/i.test(clean)) return "";
  return `/api/asset?path=${encodeURIComponent(clean)}`;
}

function imageMarkup(alt, source, title = "") {
  const safeSource = resolveImageSource(source);
  if (!safeSource) return escapeHtml(`Image non disponible: ${alt || source}`);
  return (
    `<figure class="image-figure">` +
    `<img class="message-image" src="${escapeHtml(safeSource)}" alt="${escapeHtml(alt || "Image générée")}" loading="lazy" />` +
    (title ? `<figcaption>${escapeHtml(title)}</figcaption>` : "") +
    `</figure>`
  );
}

function renderInline(text) {
  const paragraphs = text.split(/\n{2,}/).filter((p) => p.trim().length > 0);
  return paragraphs
    .map((p) => {
      const protected_ = [];
      let source = p.trim();
      source = source.replace(
        /!\[([^\]]*)\]\(([^)\s]+)(?:\s+["']([^"']*)["'])?\)/g,
        (_match, alt, url, title) => {
          const token = `\u0000${protected_.length}\u0000`;
          protected_.push(imageMarkup(alt, url, title));
          return token;
        }
      );

      let out = escapeHtml(source);
      out = out.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
      out = out.replace(/`([^`]+)`/g, "<code>$1</code>");
      out = out.replace(
        /\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
        '<a href="$2" target="_blank" rel="noopener">$1</a>'
      );
      // Keep generated HTML out of the bare-URL pass.
      out = out.replace(/<(code|a|figure)[^>]*>[\s\S]*?<\/\1>/g, (m) => {
        const token = `\u0000${protected_.length}\u0000`;
        protected_.push(m);
        return token;
      });
      out = out.replace(
        /(https?:\/\/[^\s<\u0000]+?)([.,;:!?)]*)(?=[\s\u0000]|$)/g,
        (_m, href, tail) =>
          `<a href="${href}" target="_blank" rel="noopener">${href}</a>${tail}`
      );
      out = out.replace(/\u0000(\d+)\u0000/g, (_m, i) => protected_[+i]);
      out = out.replace(/\n/g, "<br/>");
      return `<p>${out}</p>`;
    })
    .join("");
}

// ---------------------------------------------------------------- chat UI

function messageActionsMarkup({ feedback = false } = {}) {
  const feedbackMarkup = feedback
    ? `<span class="feedback-actions" role="group" aria-label="Évaluer cette réponse pour le modèle local">
        <span class="feedback-label">Signal local</span>
        <button class="feedback-button" type="button" data-feedback-rating="1" aria-label="Bonne réponse" aria-pressed="false" title="Bonne réponse">👍</button>
        <button class="feedback-button" type="button" data-feedback-rating="-1" aria-label="Mauvaise réponse" aria-pressed="false" title="Mauvaise réponse">👎</button>
        <span class="feedback-status" role="status" aria-live="polite"></span>
      </span>`
    : "";
  return (
    `<span class="message-actions">` +
    `<button class="message-copy" type="button" data-copy-message aria-label="Copier le texte du message" title="Copier le texte">Copier</button>` +
    feedbackMarkup +
    `</span>`
  );
}

async function writeClipboardText(text) {
  if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
    await navigator.clipboard.writeText(text);
    return;
  }
  const helper = document.createElement("textarea");
  helper.value = text;
  helper.setAttribute("readonly", "");
  helper.style.position = "fixed";
  helper.style.left = "-9999px";
  document.body.appendChild(helper);
  helper.select();
  const copied = document.execCommand("copy");
  helper.remove();
  if (!copied) throw new Error("Copie indisponible");
}

async function hashText(text) {
  if (!globalThis.crypto?.subtle || typeof TextEncoder === "undefined") return "";
  try {
    const bytes = new TextEncoder().encode(String(text || ""));
    const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
    return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  } catch {
    return "";
  }
}

async function copyMessageText(button) {
  const wrap = button.closest(".msg");
  if (!wrap) return;
  const text = wrap._messageText !== undefined
    ? String(wrap._messageText)
    : String(wrap.querySelector(".msg-content")?.innerText || "");
  if (!text) return;
  const original = button.textContent;
  button.disabled = true;
  try {
    await writeClipboardText(text);
    button.textContent = "Copié";
  } catch {
    button.textContent = "Échec";
  } finally {
    button.disabled = false;
    window.setTimeout(() => {
      if (button.isConnected) button.textContent = original || "Copier";
    }, 1500);
  }
}

async function submitMessageFeedback(button) {
  const wrap = button.closest(".msg");
  if (!wrap || !isLocalFeedbackBackend(wrap.dataset.backend)) return;
  if (wrap.dataset.feedbackPending === "1" || wrap.dataset.feedbackSubmitted === "1") return;
  const rating = Number(button.dataset.feedbackRating);
  if (rating !== 1 && rating !== -1) return;

  const buttons = [...wrap.querySelectorAll("[data-feedback-rating]")];
  const status = wrap.querySelector(".feedback-status");
  wrap.dataset.feedbackPending = "1";
  buttons.forEach((item) => { item.disabled = true; });
  if (status) status.textContent = "Enregistrement…";

  try {
    // Only bounded identifiers/hashes leave the browser. The prompt and
    // answer themselves never go to this endpoint and are not sent anywhere
    // outside the local process.
    const response = await fetch("/api/v1/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: String(state.sessionId || ""),
        backend: String(wrap.dataset.backend || ""),
        model: String(wrap.dataset.model || wrap._model || ""),
        rating,
        prompt_hash: await hashText(wrap._promptText || ""),
        response_hash: await hashText(wrap._messageText || ""),
      }),
    });
    let payload = {};
    try {
      payload = await response.json();
    } catch {
      /* The status code remains authoritative for an empty response. */
    }
    if (!response.ok || payload.error) throw new Error(payload.error || `HTTP ${response.status}`);

    wrap.dataset.feedbackSubmitted = "1";
    buttons.forEach((item) => {
      const selected = item === button;
      item.classList.toggle("is-selected", selected);
      item.setAttribute("aria-pressed", selected ? "true" : "false");
    });
    if (status) status.textContent = "Enregistré localement";
  } catch (error) {
    delete wrap.dataset.feedbackPending;
    buttons.forEach((item) => { item.disabled = false; });
    if (status) status.textContent = `Échec : ${error.message || error}`;
    return;
  }
  delete wrap.dataset.feedbackPending;
}

function clearEmptyState() {
  const empty = messagesEl.querySelector(".empty-state");
  if (empty) empty.remove();
}

function addUserMessage(text) {
  clearEmptyState();
  const wrap = document.createElement("div");
  wrap.className = "msg user";
  wrap._messageText = String(text || "");
  wrap.innerHTML = `
    <div class="msg-avatar">Toi</div>
    <div class="msg-body">
      <div class="msg-content">${renderMarkdown(text)}</div>
      <div class="msg-meta msg-user-meta">${messageActionsMarkup()}</div>
    </div>`;
  messagesEl.appendChild(wrap);
  renderMathIn(wrap.querySelector(".msg-content"));
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

const ORBIT_HTML = `<span class="orbit" aria-hidden="true"><span class="agent a1"></span><span class="agent a2"></span><span class="agent a3"></span></span>`;

const TRACE_LABELS = {
  started: "Départ",
  planned: "Plan",
  query: "Requête",
  source: "Source",
  reading: "Lecture",
  verification: "Vérification",
  decision: "Décision",
  warning: "Avertissement",
  completed: "Terminé",
};

function traceSummary(wrap) {
  const events = Array.isArray(wrap._researchTrace) ? wrap._researchTrace : [];
  const queries = events.filter((item) => item.type === "query").length;
  const sources = events.filter((item) => item.type === "source").length;
  const checks = events.filter((item) => item.type === "verification").length;
  const warnings = events.filter((item) => item.type === "warning").length;
  const parts = [`${events.length} étape(s)`];
  if (queries) parts.push(`${queries} requête(s)`);
  if (sources) parts.push(`${sources} source(s)`);
  if (checks) parts.push(`${checks} vérification(s)`);
  if (warnings) parts.push(`${warnings} alerte(s)`);
  const label = wrap.querySelector(".lab-trace-summary-text");
  if (label) label.textContent = parts.join(" · ");
}

function appendResearchTrace(wrap, type, text, details = {}) {
  if (!wrap || !text) return;
  const safeType = TRACE_LABELS[type] ? type : "decision";
  const event = {
    type: safeType,
    text: String(text),
    details: details && typeof details === "object" ? details : {},
    at: new Date().toISOString(),
  };
  if (!Array.isArray(wrap._researchTrace)) wrap._researchTrace = [];
  wrap._researchTrace.push(event);
  try {
    wrap.dataset.researchTrace = JSON.stringify(wrap._researchTrace);
  } catch {
    /* The live trace remains available even if a browser data attribute is full. */
  }
  const list = wrap.querySelector(".lab-trace-list");
  if (list) {
    const item = document.createElement("li");
    item.className = `lab-trace-event is-${safeType}`;
    const role = details.role ? `<span class="lab-trace-role">${escapeHtml(details.role)}</span>` : "";
    item.innerHTML =
      `<span class="lab-trace-pixel" aria-hidden="true"></span>` +
      `<span class="lab-trace-event-body"><span class="lab-trace-kind">${TRACE_LABELS[safeType]}</span>` +
      `<span class="lab-trace-text">${escapeHtml(text)}</span>${role}</span>`;
    list.appendChild(item);
  }
  traceSummary(wrap);
}

function notebookTraceMarkup(events = []) {
  const safeEvents = Array.isArray(events) ? events : [];
  return safeEvents
    .map((event) => {
      const type = TRACE_LABELS[event.type] ? event.type : "decision";
      const role = event.details && event.details.role
        ? `<span class="lab-trace-role">${escapeHtml(event.details.role)}</span>`
        : "";
      return (
        `<li class="lab-trace-event is-${type}"><span class="lab-trace-pixel" aria-hidden="true"></span>` +
        `<span class="lab-trace-event-body"><span class="lab-trace-kind">${TRACE_LABELS[type]}</span>` +
        `<span class="lab-trace-text">${escapeHtml(event.text || "")}</span>${role}</span></li>`
      );
    })
    .join("");
}

function addAssistantMessage({ title = "Carnet de laboratoire", watch = false } = {}) {
  clearEmptyState();
  const visualKind = kindSelect?.value || "auto";
  const profile = mascotProfile(visualKind);
  const wrap = document.createElement("div");
  wrap.className = "msg assistant lab-notebook-message is-running";
  wrap.dataset.visualKind = visualKind;
  wrap.dataset.mascot = profile.className;
  wrap._messageText = "";
  wrap._researchTrace = [];
  wrap._traceStartedAt = Date.now();
  wrap.innerHTML = `
    <div class="msg-avatar thinking-avatar">${mascotSprite(32, { watch, kind: visualKind })}${ORBIT_HTML}</div>
    <div class="msg-body">
      <article class="lab-notebook-page" aria-label="Réponse dans le carnet de laboratoire">
        <header class="lab-notebook-header">
          <span class="lab-notebook-tab">LAB · ${escapeHtml(profile.name)}</span>
          <span class="lab-notebook-title">${escapeHtml(title)}</span>
          <span class="lab-notebook-state" role="status" aria-live="polite">EN COURS</span>
        </header>
        <div class="msg-content lab-notebook-content">
          <div class="cli-interactions" aria-live="polite"></div>
          <div class="thinking"><span class="lab-researcher-cursor" aria-hidden="true"></span>${ORBIT_HTML}<span class="thinking-text">Les agents préparent le protocole…</span></div>
        </div>
        <details class="lab-research-trace" open>
          <summary><span>Journal de recherche</span><span class="lab-trace-summary-text">0 étape</span></summary>
          <button class="lab-trace-close" type="button" aria-label="Fermer le journal de recherche" title="Fermer le journal">Fermer</button>
          <ol class="lab-trace-list" aria-live="polite"></ol>
        </details>
        <div class="msg-meta lab-notebook-meta"></div>
      </article>
    </div>`;
  messagesEl.appendChild(wrap);
  appendResearchTrace(wrap, "started", "Réponse locale initialisée.");
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return wrap;
}

function updateThinkingStatus(wrap, text) {
  const label = wrap.querySelector(".thinking-text");
  if (label) label.textContent = text;
  const stateLabel = wrap.querySelector(".lab-notebook-state");
  if (stateLabel && wrap.classList.contains("is-running")) stateLabel.textContent = "EN COURS";
}

function renderCliInteraction(wrap, request) {
  const id = String(request.interaction_id || "").trim();
  if (!id || state.cliInteractions.has(id)) return;
  const container = wrap.querySelector(".cli-interactions");
  if (!container) return;
  const permission = request.kind === "permission";
  const card = document.createElement("section");
  card.className = "cli-interaction-card";
  card.dataset.cliInteraction = id;
  card.innerHTML =
    `<div class="cli-interaction-heading"><strong>${permission ? "Permission demandée" : "Question du CLI"}</strong>` +
    `<span>${escapeHtml(request.agent || "CLI")}</span></div>` +
    `<p class="cli-interaction-question">${escapeHtml(request.question || "Le CLI attend une décision.")}</p>` +
    (permission ? "" : `<input class="cli-interaction-answer" type="text" maxlength="4000" placeholder="Ta réponse…" />`) +
    `<div class="cli-interaction-actions">` +
    (permission
      ? `<button type="button" class="cli-interaction-approve" data-cli-decision="approve">Autoriser</button>`
      : `<button type="button" class="cli-interaction-answer-button" data-cli-decision="answer">Répondre</button>`) +
    `<button type="button" class="cli-interaction-deny" data-cli-decision="deny">Refuser</button>` +
    `<span class="cli-interaction-status" role="status" aria-live="polite">En attente</span></div>`;
  container.appendChild(card);
  state.cliInteractions.set(id, { wrap, card });
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

async function resolveCliInteraction(button) {
  const card = button.closest("[data-cli-interaction]");
  if (!card) return;
  const id = card.dataset.cliInteraction;
  const pending = state.cliInteractions.get(id);
  if (!pending || card.dataset.resolved === "1") return;
  const decision = button.dataset.cliDecision || "deny";
  const answerInput = card.querySelector(".cli-interaction-answer");
  const answer = answerInput ? answerInput.value.trim() : "";
  if (decision === "answer" && !answer) {
    if (answerInput) answerInput.focus();
    return;
  }
  const buttons = [...card.querySelectorAll("button")];
  buttons.forEach((item) => { item.disabled = true; });
  const status = card.querySelector(".cli-interaction-status");
  if (status) status.textContent = "Envoi…";
  try {
    const response = await fetch(`/api/v1/cli/interactions/${encodeURIComponent(id)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision, answer }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || payload.error) throw new Error(payload.error || `HTTP ${response.status}`);
    card.dataset.resolved = "1";
    card.classList.add("is-resolved");
    if (status) status.textContent = decision === "deny" ? "Refusé" : "Transmis";
    state.cliInteractions.delete(id);
  } catch (error) {
    buttons.forEach((item) => { item.disabled = false; });
    if (status) status.textContent = `Échec : ${error.message || error}`;
  }
}

function updateCliInteraction(id, message, resolved = false) {
  const pending = state.cliInteractions.get(String(id));
  if (!pending) return;
  const card = pending.card;
  const status = card.querySelector(".cli-interaction-status");
  if (status) status.textContent = message;
  if (resolved) {
    card.dataset.resolved = "1";
    card.classList.add("is-resolved");
    card.querySelectorAll("button, input").forEach((item) => { item.disabled = true; });
    state.cliInteractions.delete(String(id));
  }
}

function finalizeAssistantMessage(
  wrap,
  { finalSolution, consensusReached, completedCycles, backendLabel, backend = "", model = "" }
) {
  wrap.classList.remove("is-running");
  wrap.classList.add("is-complete");
  const avatar = wrap.querySelector(".msg-avatar");
  avatar.className = "msg-avatar";
  const visualKind = wrap.dataset.visualKind || kindSelect?.value || "auto";
  avatar.innerHTML = mascotSprite(32, { kind: visualKind });
  const stateLabel = wrap.querySelector(".lab-notebook-state");
  if (stateLabel) stateLabel.textContent = "ARCHIVÉ";
  const responseText = String(finalSolution || "(pas de réponse)");
  wrap._messageText = responseText;
  wrap.dataset.backend = String(backend || "");
  wrap.dataset.model = String(model || "");
  const content = wrap.querySelector(".msg-content");
  content.innerHTML = renderMarkdown(responseText);
  renderMathIn(content);
  const duration = Math.max(0, Math.round((Date.now() - (wrap._traceStartedAt || Date.now())) / 1000));
  appendResearchTrace(
    wrap,
    "completed",
    consensusReached ? "Réponse vérifiée par consensus." : "Réponse finalisée sans consensus complet.",
    { cycles: completedCycles }
  );
  const trace = wrap.querySelector(".lab-research-trace");
  if (trace) trace.open = false;
  const meta = wrap.querySelector(".msg-meta");
  const pillClass = consensusReached ? "ok" : "warn";
  const pillText = consensusReached ? "Consensus atteint" : "Pas de consensus";
  meta.innerHTML =
    `<span class="status-pill ${pillClass}">${pillText}</span>` +
    `<span>${completedCycles} cycle(s) · ${escapeHtml(backendLabel)} · ${duration}s</span>` +
    messageActionsMarkup({ feedback: isLocalFeedbackBackend(backend) });
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function errorInAssistantMessage(wrap, message) {
  wrap.classList.remove("is-running");
  wrap.classList.add("is-error");
  const avatar = wrap.querySelector(".msg-avatar");
  avatar.className = "msg-avatar";
  const visualKind = wrap.dataset.visualKind || kindSelect?.value || "auto";
  avatar.innerHTML = mascotSprite(32, { kind: visualKind });
  const stateLabel = wrap.querySelector(".lab-notebook-state");
  if (stateLabel) stateLabel.textContent = "ERREUR";
  appendResearchTrace(wrap, "warning", String(message || "Erreur inconnue"));
  const content = wrap.querySelector(".msg-content");
  content.innerHTML = `<p style="color:var(--danger)">${escapeHtml(message)}</p>`;
  const trace = wrap.querySelector(".lab-research-trace");
  if (trace) trace.open = true;
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
const sidePanelExpandEl = el("side-panel-expand");
const sidePanelCards = Array.from(document.querySelectorAll("#side-panel .panel-card"));

function setPanelCardZoom(card, zoomed) {
  const isZoomed = Boolean(zoomed);
  card.classList.toggle("is-zoomed", isZoomed);
  document.body.classList.toggle("panel-card-zoomed", isZoomed);
  const button = card.querySelector(".panel-card-zoom");
  if (button) {
    button.setAttribute("aria-expanded", String(isZoomed));
    button.setAttribute("aria-label", isZoomed ? "Réduire ce bloc" : "Agrandir ce bloc");
    button.title = isZoomed ? "Réduire ce bloc" : "Agrandir ce bloc";
    const label = button.querySelector("span");
    if (label) label.textContent = isZoomed ? "Réduire" : "Zoom";
  }
}

function closeZoomedPanelCard() {
  const zoomedCard = document.querySelector("#side-panel .panel-card.is-zoomed");
  if (zoomedCard) setPanelCardZoom(zoomedCard, false);
}

sidePanelCards.forEach((card, index) => {
  if (!card.id) card.id = `side-panel-card-${index + 1}`;
  const button = document.createElement("button");
  button.type = "button";
  button.className = "panel-card-zoom";
  button.setAttribute("aria-expanded", "false");
  button.setAttribute("aria-controls", card.id);
  button.setAttribute("aria-label", "Agrandir ce bloc");
  button.title = "Agrandir ce bloc";
  button.innerHTML = '<span aria-hidden="true">Zoom</span><svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" d="M8 3H3v5M16 3h5v5M8 21H3v-5M21 16v5h-5"/></svg>';
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    const currentlyZoomed = card.classList.contains("is-zoomed");
    closeZoomedPanelCard();
    setPanelCardZoom(card, !currentlyZoomed);
  });
  card.appendChild(button);
});

function setSidePanelExpanded(expanded) {
  const isExpanded = Boolean(expanded);
  document.body.classList.toggle("side-panel-expanded", isExpanded);
  if (sidePanelExpandEl) {
    sidePanelExpandEl.setAttribute("aria-expanded", String(isExpanded));
    sidePanelExpandEl.setAttribute("aria-label", isExpanded ? "Réduire le panneau" : "Ouvrir le panneau en grand");
    sidePanelExpandEl.title = isExpanded ? "Réduire la largeur" : "Ouvrir en grand";
    const label = sidePanelExpandEl.querySelector("span");
    if (label) label.textContent = isExpanded ? "Réduire" : "Ouvrir en grand";
  }
}

function setSidePanelCollapsed(collapsed) {
  document.body.classList.toggle("side-panel-collapsed", collapsed);
  if (collapsed) {
    closeZoomedPanelCard();
    setSidePanelExpanded(false);
  }
  sidePanelToggleEl.hidden = !collapsed;
  try {
    localStorage.setItem("3loop_side_panel_collapsed", collapsed ? "1" : "0");
  } catch {
    /* ignore */
  }
}

el("side-panel-close").addEventListener("click", () => setSidePanelCollapsed(true));
sidePanelToggleEl.addEventListener("click", () => setSidePanelCollapsed(false));
if (sidePanelExpandEl) {
  sidePanelExpandEl.addEventListener("click", () => {
    setSidePanelExpanded(!document.body.classList.contains("side-panel-expanded"));
  });
}
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (document.querySelector("#side-panel .panel-card.is-zoomed")) {
    closeZoomedPanelCard();
  } else if (document.body.classList.contains("side-panel-expanded")) {
    setSidePanelExpanded(false);
  }
});
document.addEventListener("click", (event) => {
  const zoomedCard = document.querySelector("#side-panel .panel-card.is-zoomed");
  if (zoomedCard && !zoomedCard.contains(event.target)) closeZoomedPanelCard();
});
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
        <span class="doc-meta">${doc.loading ? "chargement…" : (doc.pageCount ? `${doc.pageCount} p. · ` : "") + doc.text.length.toLocaleString("fr-FR") + " car."}</span>
        <button type="button" class="doc-remove" data-doc-remove="${doc.id}" aria-label="Retirer">×</button>
      </div>`
    )
    .join("");
}

documentListEl.addEventListener("change", async (event) => {
  const id = event.target.dataset.docId;
  if (!id) return;
  const doc = state.documents.find((d) => d.id === id);
  if (!doc) return;
  doc.included = event.target.checked;
  if (doc.included && !doc.text && doc.versionId) {
    doc.loading = true;
    renderDocumentList();
    try {
      const response = await fetch(
        `/api/v1/library/documents/${encodeURIComponent(doc.versionId)}/text`
      );
      const payload = await response.json();
      if (!response.ok || payload.error) throw new Error(payload.error || `HTTP ${response.status}`);
      doc.text = String(payload.text || "");
    } catch (error) {
      doc.included = false;
      errorInAssistantMessage(
        addAssistantMessage({ title: "Carnet de bibliothèque" }),
        `Document "${doc.name}": ${error.message || error}`
      );
    } finally {
      doc.loading = false;
      renderDocumentList();
    }
  }
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
    const response = await fetch("/api/v1/library/import", {
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
      entry.paperId = payload.paper_id || "";
      entry.versionId = payload.version_id || "";
      entry.pageCount = Number(payload.page_count) || 1;
      entry.sha256 = payload.sha256 || "";
    }
  } catch (err) {
    state.documents = state.documents.filter((d) => d.id !== placeholderId);
    errorInAssistantMessage(addAssistantMessage(), `Document "${file.name}": ${err}`);
  }
  renderDocumentList();
}

function attachedDocumentsContext() {
  // The server reads these identifiers from its local SQLite workspace,
  // scans every selected document, and returns a single bounded set of
  // relevant excerpts. Sending full text for every checkbox here could
  // overflow Qwen3's local context window before it can answer.
  const versionIds = [...new Set(
    state.documents
      .filter((doc) => doc.included && !doc.loading && doc.versionId)
      .map((doc) => String(doc.versionId).trim())
      .filter(Boolean)
  )];
  return versionIds.length ? `3LOOP_DOCUMENT_VERSION_IDS=${versionIds.join(",")}` : "";
}

// ---- conversations (save / reload / export / delete) ---------------------
//
// Storage keeps its "3loop_discussions" key and the #discussion-list /
// #save-discussion ids: only the rendering and the labels moved to
// "conversations", so already-saved entries keep working.

function legacyDiscussions() {
  try {
    const value = JSON.parse(localStorage.getItem("3loop_discussions") || "[]");
    return Array.isArray(value) ? value : [];
  } catch {
    return [];
  }
}

function normalizeDiscussion(raw) {
  const value = raw && typeof raw === "object" ? raw : {};
  const messages = Array.isArray(value.messages) ? value.messages.map((message) => ({
    role: message.role === "user" ? "user" : "assistant",
    text: String(message.text || ""),
    trace: Array.isArray(message.trace) ? message.trace : [],
    jobId: String(message.jobId || message.job_id || ""),
    backend: String(message.backend || ""),
    model: String(message.model || ""),
  })).filter((message) => message.text.trim()) : [];
  const conversation = Array.isArray(value.conversation)
    ? value.conversation
        .filter((message) => message && (message.role === "user" || message.role === "assistant") && message.text)
        .map((message) => ({ role: message.role, text: String(message.text) }))
    : [];
  return {
    ...value,
    id: String(value.id || ""),
    title: String(value.title || "Discussion"),
    savedAt: String(value.savedAt || value.saved_at || new Date().toISOString()),
    updatedAt: String(value.updatedAt || value.updated_at || value.savedAt || value.saved_at || new Date().toISOString()),
    compacted: Boolean(value.compacted),
    compactSummary: String(value.compactSummary || value.compact_summary || ""),
    compactMode: String(value.compactMode || value.compact_mode || ""),
    compactedAt: String(value.compactedAt || value.compacted_at || ""),
    messages,
    conversation,
  };
}

function loadDiscussions() {
  return Array.isArray(state.discussions) ? state.discussions : [];
}

function saveDiscussions(list) {
  state.discussions = Array.isArray(list) ? list.map(normalizeDiscussion) : [];
  try {
    // Kept as a bounded fallback and migration source, never as the primary
    // notebook when the SQLite API is available.
    localStorage.setItem("3loop_discussions", JSON.stringify(state.discussions));
  } catch {
    /* ignore storage quota errors */
  }
}

async function persistDiscussion(discussion) {
  const normalized = normalizeDiscussion(discussion);
  try {
    const response = await fetch("/api/v1/conversations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ...normalized,
        saved_at: normalized.savedAt,
        compact_summary: normalized.compactSummary,
        compact_mode: normalized.compactMode,
        compacted_at: normalized.compactedAt,
      }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || payload.error) throw new Error(payload.error || `HTTP ${response.status}`);
    const saved = normalizeDiscussion(payload);
    const existing = loadDiscussions().filter((item) => item.id !== saved.id);
    state.discussions = [saved, ...existing];
    state.discussionsRemote = true;
    state.discussionsLoaded = true;
    return saved;
  } catch (error) {
    state.discussionsRemote = false;
    saveDiscussions([normalized, ...loadDiscussions().filter((item) => item.id !== normalized.id)]);
    return normalized;
  }
}

async function loadDiscussionsFromServer() {
  const legacy = legacyDiscussions().map(normalizeDiscussion);
  try {
    const response = await fetch("/api/v1/conversations");
    const payload = await response.json();
    if (!response.ok || payload.error) throw new Error(payload.error || `HTTP ${response.status}`);
    let remote = Array.isArray(payload.items) ? payload.items.map(normalizeDiscussion) : [];
    state.discussionsRemote = true;
    // One-way migration: only legacy entries absent from SQLite are copied.
    if (remote.length === 0 && legacy.length > 0) {
      for (const discussion of legacy) {
        await persistDiscussion(discussion);
      }
      const refreshed = await fetch("/api/v1/conversations");
      const refreshedPayload = await refreshed.json();
      remote = Array.isArray(refreshedPayload.items)
        ? refreshedPayload.items.map(normalizeDiscussion)
        : remote;
    }
    state.discussions = remote;
    state.discussionsLoaded = true;
    renderDiscussionList();
  } catch {
    state.discussionsRemote = false;
    state.discussionsLoaded = true;
    saveDiscussions(legacy);
    renderDiscussionList();
  }
}

async function updateDiscussion(id, patch) {
  const entry = loadDiscussions().find((discussion) => discussion.id === id);
  if (!entry) return null;
  Object.assign(entry, patch);
  const saved = await persistDiscussion(entry);
  renderDiscussionList();
  return saved;
}

// Inline SVGs rather than "⇩" / "×": those glyphs render at whatever the
// user's fallback font decides, which is inconsistent and reads as a typo.
const ICON_EXPORT =
  `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" ` +
  `stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">` +
  `<path d="M12 4v10"/><path d="M8 11l4 4 4-4"/><path d="M5 19h14"/></svg>`;
const ICON_DELETE =
  `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" ` +
  `stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">` +
  `<path d="M5 7h14"/><path d="M10 4h4"/><path d="M6.6 7l.8 12.1a1 1 0 0 0 1 .9h7.2a1 1 0 0 0 1-.9L18.4 7"/>` +
  `<path d="M10.5 11v5"/><path d="M13.5 11v5"/></svg>`;

function conversationCount(discussion) {
  return Array.isArray(discussion.messages) ? discussion.messages.length : 0;
}

function renderDiscussionList() {
  const discussions = loadDiscussions();
  if (discussions.length === 0) {
    discussionListEl.className = "conversation-list muted";
    discussionListEl.innerHTML = "Aucune conversation sauvegardée.";
    return;
  }
  discussionListEl.className = "conversation-list";
  discussionListEl.innerHTML = discussions
    .slice()
    .reverse()
    .map((d) => {
      const id = escapeHtml(d.id);
      const active = d.id === state.activeConversationId ? " active" : "";
      const savedAt = new Date(d.savedAt).toLocaleDateString("fr-FR");
      const turns = conversationCount(d);
      const badge = d.compacted
        ? `<span class="conversation-compact-badge" title="Contexte compacté : le prochain message repart du résumé">compacté</span>`
        : "";
      return `
      <div class="conversation-item${active}" data-conversation-id="${id}">
        <button type="button" class="conversation-title" data-discussion-load="${id}" title="Recharger cette conversation et compacter son contexte">
          ${escapeHtml(d.title)}
        </button>
        <span class="conversation-meta">${savedAt}${turns ? ` · ${turns} message(s)` : ""}</span>
        ${badge}
        <span class="conversation-actions">
          <button type="button" class="doc-remove" data-discussion-export="${id}" aria-label="Exporter cette conversation en .md" title="Exporter en .md">${ICON_EXPORT}</button>
          <button type="button" class="doc-remove" data-discussion-remove="${id}" aria-label="Supprimer cette conversation" title="Supprimer">${ICON_DELETE}</button>
        </span>
      </div>`;
    })
    .join("");
}

const saveDiscussionButton = el("save-discussion");

saveDiscussionButton.addEventListener("click", async () => {
  const messages = [...messagesEl.querySelectorAll(".msg")]
    .map((node) => {
      let trace = Array.isArray(node._researchTrace) ? node._researchTrace : [];
      if (!trace.length && node.dataset.researchTrace) {
        try {
          trace = JSON.parse(node.dataset.researchTrace);
        } catch {
          trace = [];
        }
      }
      return {
        role: node.classList.contains("user") ? "user" : "assistant",
        text: node.querySelector(".msg-content")?.innerText.trim() || "",
        trace,
        jobId: node.dataset.jobId || "",
        backend: node.dataset.backend || "",
        model: node.dataset.model || "",
      };
    })
    .filter((message) => message.text);
  if (messages.length === 0) {
    // Used to `return` silently: the button appeared dead, with nothing
    // said about why. Saving an empty chat is a no-op, but the user has to
    // be told that rather than left guessing whether the feature is broken.
    setCompactStatus(
      "Rien à sauvegarder pour l’instant : pose d’abord une question.",
      "warn",
    );
    return;
  }
  const firstUserMessage = messages.find((message) => message.role === "user");
  const discussion = normalizeDiscussion({
    id: `disc_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
    title: (firstUserMessage ? firstUserMessage.text : "Discussion").slice(0, 60),
    savedAt: new Date().toISOString(),
    messages,
    conversation: state.conversation.slice(),
  });
  saveDiscussionButton.disabled = true;
  try {
    const saved = await persistDiscussion(discussion);
    state.activeConversationId = saved.id;
    renderDiscussionList();
    setCompactStatus(
      state.discussionsRemote ? "Conversation enregistrée dans le cahier SQLite local." : "Conversation enregistrée dans le cahier de secours du navigateur.",
      "done",
    );
  } finally {
    saveDiscussionButton.disabled = state.compacting;
  }
});

// ---- context compaction (POST /api/compact, plain JSON, no SSE) ----------
//
// Clicking a conversation reloads it *and* asks the currently selected
// backend to boil its context down to one turn. The transcript on screen and
// in localStorage is never touched: only state.conversation - what the engine
// is sent - is replaced, so a failed compaction costs nothing.

function setCompactStatus(text, kind = "") {
  if (!compactStatusEl) return;
  compactStatusEl.textContent = text || "";
  if (kind) compactStatusEl.dataset.state = kind;
  else delete compactStatusEl.dataset.state;
}

function setConversationBusy(busy) {
  state.compacting = busy;
  discussionListEl.setAttribute("aria-busy", busy ? "true" : "false");
  discussionListEl.classList.toggle("busy", busy);
  for (const button of discussionListEl.querySelectorAll("button")) {
    button.disabled = busy;
  }
  saveDiscussionButton.disabled = busy;
}

function currentModelLabel() {
  const option = modelSelect.options[modelSelect.selectedIndex];
  const label = option ? option.textContent.trim() : "";
  return label || currentBackendLabel();
}

function compactModeLabel(mode) {
  // Honest wording: "mechanical" means no model ran, just text reduction.
  return mode === "llm" ? `par ${currentModelLabel()}` : "mécaniquement (sans modèle)";
}

function formatChars(count) {
  return Number(count || 0).toLocaleString("fr-FR");
}

async function compactConversation(discussion) {
  if (state.compacting) return;
  const turns = state.conversation
    .filter((m) => m && (m.role === "user" || m.role === "assistant") && m.text)
    .map((m) => ({ role: m.role, text: String(m.text) }));
  if (turns.length < 2) {
    setCompactStatus("Contexte déjà minimal : rien à compacter.", "done");
    return;
  }
  const localChars = turns.reduce((total, m) => total + m.text.length, 0);

  setConversationBusy(true);
  setCompactStatus(`Compaction du contexte par ${currentModelLabel()}…`, "loading");
  try {
    const response = await fetch("/api/compact", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        conversation: turns,
        backend: backendSelect.value,
        model: selectedModel(),
        api_key: apiKeyInput.value,
        allow_writes: false,
        workspace_path: "",
        session_id: state.sessionId,
        max_tokens: 512,
      }),
    });
    const payload = await response.json();
    if (!payload || payload.error) {
      const reason = (payload && payload.error) || `HTTP ${response.status}`;
      setCompactStatus(
        `Compaction impossible : ${reason}. La conversation reste utilisable telle quelle.`,
        "error"
      );
      return;
    }
    const summary = String(payload.summary || "").trim();
    if (!summary) {
      setCompactStatus(
        "Compaction sans résumé exploitable : contexte laissé intact.",
        "error"
      );
      return;
    }
    state.conversation = [{ role: "assistant", text: summary }];
    await updateDiscussion(discussion.id, {
      compacted: true,
      compactSummary: summary,
      compactMode: payload.mode || "",
      compactedAt: new Date().toISOString(),
    });
    const before = Number(payload.original_chars) || localChars;
    const after = Number(payload.compact_chars) || summary.length;
    const turnCount = Number(payload.turns) || turns.length;
    setCompactStatus(
      `Contexte compacté ${compactModeLabel(payload.mode)} : ${turnCount} tour(s), ` +
        `${formatChars(before)} → ${formatChars(after)} caractères`,
      "done"
    );
  } catch (error) {
    setCompactStatus(
      `Compaction impossible : ${error.message || error}. La conversation reste utilisable telle quelle.`,
      "error"
    );
  } finally {
    renderDiscussionList();
    setConversationBusy(false);
  }
}

function restoreDiscussion(discussion) {
  state.conversation = Array.isArray(discussion.conversation)
    ? discussion.conversation
        .filter((m) => m && (m.role === "user" || m.role === "assistant") && m.text)
        .map((m) => ({ role: m.role, text: String(m.text) }))
    : discussion.messages.map((m) => ({ role: m.role, text: m.text }));
  state.sessionId = crypto.randomUUID ? crypto.randomUUID() : String(Date.now());
  state.activeConversationId = discussion.id;
  clearEmptyState();
  messagesEl.innerHTML = "";
  for (const message of discussion.messages) {
    if (message.role === "user") {
      addUserMessage(message.text);
      continue;
    }
    const wrap = addAssistantMessage({ title: "Carnet de laboratoire · archivé" });
    wrap.classList.remove("is-running");
    wrap.classList.add("is-complete");
    wrap._researchTrace = Array.isArray(message.trace) ? message.trace : [];
    wrap.dataset.researchTrace = JSON.stringify(wrap._researchTrace);
    if (message.jobId) wrap.dataset.jobId = String(message.jobId);
    const avatar = wrap.querySelector(".msg-avatar");
    avatar.className = "msg-avatar";
    const visualKind = wrap.dataset.visualKind || kindSelect?.value || "auto";
  avatar.innerHTML = mascotSprite(32, { kind: visualKind });
    const stateLabel = wrap.querySelector(".lab-notebook-state");
    if (stateLabel) stateLabel.textContent = "ARCHIVÉ";
    const content = wrap.querySelector(".msg-content");
    wrap._messageText = String(message.text || "");
    wrap._promptText = "";
    content.innerHTML = renderMarkdown(message.text);
    renderMathIn(content);
    const list = wrap.querySelector(".lab-trace-list");
    if (list) list.innerHTML = notebookTraceMarkup(wrap._researchTrace);
    const trace = wrap.querySelector(".lab-research-trace");
    if (trace) trace.open = false;
    const meta = wrap.querySelector(".msg-meta");
    meta.innerHTML = `<span class="status-pill ok">Réponse archivée</span><span>Trace de recherche restaurée</span>${messageActionsMarkup()}`;
    traceSummary(wrap);
  }
  messagesEl.scrollTop = messagesEl.scrollHeight;
  renderDiscussionList();
}

async function openDiscussion(id) {
  if (state.compacting) return; // one compaction at a time
  let discussion = loadDiscussions().find((item) => item.id === id);
  if (!discussion) return;
  if (state.discussionsRemote || !discussion.messages.length) {
    try {
      const response = await fetch(`/api/v1/conversations/${encodeURIComponent(id)}`);
      const payload = await response.json();
      if (response.ok && !payload.error) {
        discussion = normalizeDiscussion(payload);
        state.discussions = [discussion, ...loadDiscussions().filter((item) => item.id !== id)];
      }
    } catch {
      // Keep the local copy when SQLite is temporarily unavailable.
    }
  }
  if (!discussion.messages.length) {
    setCompactStatus("Cette conversation ne contient aucun message récupérable.", "error");
    return;
  }
  restoreDiscussion(discussion);
  const cached = String(discussion.compactSummary || "").trim();
  if (discussion.compacted && cached) {
    // Already summarised on a previous click: reuse it instead of paying for
    // the same call again.
    state.conversation = [{ role: "assistant", text: cached }];
    setCompactStatus(
      `Contexte déjà compacté : résumé de ${formatChars(cached.length)} caractères réutilisé.`,
      "done"
    );
    return;
  }
  await compactConversation(discussion);
}

discussionListEl.addEventListener("click", async (event) => {
  if (state.compacting) return;
  const removeTarget = event.target.closest("[data-discussion-remove]");
  if (removeTarget) {
    const removeId = removeTarget.dataset.discussionRemove;
    state.discussions = loadDiscussions().filter((discussion) => discussion.id !== removeId);
    try {
      const response = await fetch(`/api/v1/conversations/${encodeURIComponent(removeId)}`, { method: "DELETE" });
      if (!response.ok && response.status !== 404) throw new Error(`HTTP ${response.status}`);
    } catch {
      // The in-memory removal remains visible; the fallback is updated below.
    }
    saveDiscussions(loadDiscussions());
    if (state.activeConversationId === removeId) state.activeConversationId = null;
    renderDiscussionList();
    return;
  }
  const exportTarget = event.target.closest("[data-discussion-export]");
  if (exportTarget) {
    const discussion = loadDiscussions().find((item) => item.id === exportTarget.dataset.discussionExport);
    if (discussion) exportDiscussionAsMarkdown(discussion);
    return;
  }
  const loadTarget = event.target.closest("[data-discussion-load]");
  if (loadTarget) await openDiscussion(loadTarget.dataset.discussionLoad);
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
loadDiscussionsFromServer();

function scientificPaperId(paper) {
  return String(paper && (paper.id || paper.paper_id) || "").trim();
}

function paperAuthorsLabel(paper) {
  const authors = Array.isArray(paper && paper.authors) ? paper.authors : [];
  return authors.filter(Boolean).slice(0, 3).join(", ") || "Auteurs non renseignés";
}

function paperYearLabel(paper) {
  const year = Number(paper && paper.year);
  return Number.isFinite(year) && year > 0 ? String(year) : "année inconnue";
}

function renderScientificLibrary() {
  if (!libraryListEl) return;
  const query = String(librarySearchInput && librarySearchInput.value || "").trim().toLowerCase();
  const papers = state.scientificPapers.filter((paper) => {
    if (!query) return true;
    return [paper.title, paper.original_name, paper.abstract, paper.status]
      .filter(Boolean)
      .join(" ")
      .toLowerCase()
      .includes(query);
  });
  if (!papers.length) {
    libraryListEl.className = "scientific-library-list muted";
    libraryListEl.textContent = query ? "Aucune référence correspondante." : "Aucune référence importée.";
    if (compareButton) compareButton.disabled = true;
    return;
  }
  libraryListEl.className = "scientific-library-list";
  libraryListEl.innerHTML = papers.map((paper) => {
    const id = scientificPaperId(paper);
    const selected = state.selectedPaperIds.has(id);
    const title = paper.title || paper.original_name || "Référence sans titre";
    const location = paper.original_name && paper.original_name !== title
      ? ` · ${paper.original_name}`
      : "";
    return `<article class="scientific-library-item${selected ? " is-selected" : ""}">
      <label class="scientific-library-select">
        <input type="checkbox" data-paper-id="${escapeHtml(id)}" ${selected ? "checked" : ""} />
        <span class="scientific-library-copy">
          <span class="scientific-library-title" title="${escapeHtml(title)}">${escapeHtml(title)}</span>
          <span class="scientific-library-meta">${escapeHtml(paperYearLabel(paper))}${escapeHtml(location)} · ${escapeHtml(paper.status || "unread")}</span>
        </span>
      </label>
      <button type="button" class="text-button library-delete-button" data-delete-paper="${escapeHtml(id)}" aria-label="Supprimer cette référence">Supprimer</button>
    </article>`;
  }).join("");
  if (compareButton) compareButton.disabled = state.selectedPaperIds.size < 2;
}

async function deleteScientificPaper(paperId, button) {
  const id = String(paperId || "").trim();
  const paper = state.scientificPapers.find((item) => scientificPaperId(item) === id);
  if (!id || !paper) return;
  const title = paper.title || paper.original_name || "cette référence";
  if (!window.confirm(`Supprimer « ${title} » de la bibliothèque locale ?`)) return;
  if (button) button.disabled = true;
  try {
    const response = await fetch(`/api/v1/library/papers/${encodeURIComponent(id)}`, { method: "DELETE" });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || payload.error) throw new Error(payload.error || `HTTP ${response.status}`);
    state.scientificPapers = state.scientificPapers.filter((item) => scientificPaperId(item) !== id);
    state.selectedPaperIds.delete(id);
    state.documents = state.documents.filter((document) => String(document.paperId || "") !== id);
    renderDocumentList();
    renderScientificLibrary();
  } catch (error) {
    if (button) button.disabled = false;
    window.alert(`Suppression impossible : ${error.message || error}`);
  }
}

async function loadScientificLibrary() {
  try {
    const response = await fetch("/api/v1/library/papers?limit=100");
    if (!response.ok) return;
    const payload = await response.json();
    state.scientificPapers = Array.isArray(payload.items) ? payload.items : [];
    for (const paper of state.scientificPapers) {
      // Bibliography-only references have no document version and should not
      // appear in the prompt attachment list until a PDF/text is imported.
      if (!paper.version_id) continue;
      const id = `paper_${paper.id}`;
      if (state.documents.some((doc) => doc.id === id || doc.versionId === paper.version_id)) continue;
      state.documents.push({
        id,
        name: paper.original_name || paper.title || "Document scientifique",
        text: "",
        included: false,
        loading: false,
        paperId: paper.id,
        versionId: paper.version_id,
        pageCount: Number(paper.page_count) || 0,
        sha256: paper.blob_hash || "",
        persistent: true,
      });
    }
    renderDocumentList();
    renderScientificLibrary();
  } catch {
    /* The legacy in-memory attachment flow remains available. */
  }
}

function scientificRecordKey(record) {
  const doi = String(record && record.doi || "").trim().toLowerCase();
  if (doi) return `doi:${doi}`;
  const provider = String(record && record.provider || "").trim().toLowerCase();
  const externalId = String(record && record.external_id || "").trim().toLowerCase();
  if (provider && externalId) return `${provider}:${externalId}`;
  return `title:${String(record && record.title || "").trim().toLowerCase()}`;
}

function rememberScientificResults(records) {
  for (const record of Array.isArray(records) ? records : []) {
    const key = scientificRecordKey(record);
    if (key !== "title:" || String(record.title || "").trim()) {
      state.scientificRecordsByKey.set(key, record);
    }
  }
}

function markScientificRecordSaved(key) {
  const record = state.scientificRecordsByKey.get(String(key || ""));
  if (record) record._saved = true;
  [...document.querySelectorAll("[data-save-scientific-key]")].forEach((button) => {
    if (button.dataset.saveScientificKey === String(key || "")) {
      button.textContent = "Ajouté";
      button.disabled = true;
    }
  });
}

function renderScientificResults() {
  if (!scientificResultsEl) return;
  const results = Array.isArray(state.scientificResults) ? state.scientificResults : [];
  if (!results.length) {
    scientificResultsEl.className = "scientific-results muted";
    scientificResultsEl.textContent = "Aucun résultat.";
    return;
  }
  scientificResultsEl.className = "scientific-results";
  scientificResultsEl.innerHTML = results.map((record, index) => {
    const url = String(record.url || (record.doi ? `https://doi.org/${record.doi}` : "")).trim();
    const title = record.title || "Résultat sans titre";
    const authors = Array.isArray(record.authors) ? record.authors.slice(0, 2).join(", ") : "";
    const details = [record.provider, record.year, record.venue, authors].filter(Boolean).join(" · ");
    const artifact = [record.dataset && `dataset: ${record.dataset}`, record.code_url && "code", record.license && record.license]
      .filter(Boolean)
      .join(" · ");
    const snippet = String(record.abstract || "").trim();
    const link = url && /^https?:\/\//i.test(url)
      ? `<a class="scientific-result-link" href="${escapeHtml(url)}" target="_blank" rel="noopener">ouvrir</a>`
      : "";
    const recordKey = scientificRecordKey(record);
    const saveButton = record.title || record.external_id || record.doi
      ? `<button type="button" class="text-button" data-save-scientific-key="${escapeHtml(recordKey)}" ${record._saved ? "disabled" : ""}>${record._saved ? "Ajouté" : "Ajouter à la bibliothèque"}</button>`
      : "";
    return `<article class="scientific-result-card">
      <div class="scientific-result-heading">
        <span class="scientific-result-rank">${index + 1}</span>
        <div><h5>${escapeHtml(title)}</h5><p>${escapeHtml(details || "Source scientifique")}</p></div>
      </div>
      ${snippet ? `<p class="scientific-result-abstract">${escapeHtml(snippet.slice(0, 260))}${snippet.length > 260 ? "…" : ""}</p>` : ""}
      ${artifact ? `<p class="scientific-result-artifact">${escapeHtml(artifact)}</p>` : ""}
      <div class="scientific-result-actions">${link}${saveButton}</div>
    </article>`;
  }).join("");
}

function scientificResultAsBibliography(record) {
  const year = Number(record.year);
  return {
    id: record.doi || record.external_id || record.title || "reference",
    type: "article-journal",
    title: record.title || "",
    author: (Array.isArray(record.authors) ? record.authors : []).map((literal) => ({ literal })),
    ...(Number.isFinite(year) && year > 0 ? { issued: { "date-parts": [[year]] } } : {}),
    ...(record.venue ? { "container-title": record.venue } : {}),
    ...(record.abstract ? { abstract: record.abstract } : {}),
    ...(record.doi ? { DOI: record.doi } : {}),
    ...(record.url ? { URL: record.url } : {}),
    ...(record.external_id && record.provider ? { external_ids: { [String(record.provider).toLowerCase()]: record.external_id } } : {}),
    ...(String(record.provider || "").toLowerCase() === "arxiv" && record.external_id
      ? { arXiv: record.external_id }
      : {}),
  };
}

async function saveScientificResult(keyOrIndex) {
  const rawKey = String(keyOrIndex ?? "");
  const record = /^\d+$/.test(rawKey)
    ? state.scientificResults[Number(rawKey)]
    : state.scientificRecordsByKey.get(rawKey);
  if (!record) return;
  const recordKey = scientificRecordKey(record);
  if (record._saved) return;
  try {
    const response = await fetch("/api/v1/library/bibliography/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ format: "csl-json", content: JSON.stringify([scientificResultAsBibliography(record)]) }),
    });
    const payload = await response.json();
    if (!response.ok || payload.error) throw new Error(payload.error || `HTTP ${response.status}`);
    record._saved = true;
    markScientificRecordSaved(recordKey);
    const outcome = Array.isArray(payload.items) ? payload.items[0] : null;
    const wasExisting = outcome && outcome.status === "matched";
    if (scientificSearchStatus) scientificSearchStatus.textContent = wasExisting
      ? "Cette référence était déjà dans la bibliothèque locale."
      : "Référence ajoutée à la bibliothèque locale.";
    renderScientificResults();
    await loadScientificLibrary();
  } catch (error) {
    if (scientificSearchStatus) scientificSearchStatus.textContent = `Ajout impossible : ${error.message || error}`;
  }
}

function notebookNoteDate(note) {
  const raw = note && (note.updated_at || note.created_at);
  if (!raw) return "";
  const date = new Date(raw);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleDateString("fr-FR");
}

function renderNotebookNotes() {
  if (!notebookListEl) return;
  if (!state.notebookNotes.length) {
    notebookListEl.className = "notebook-list muted";
    notebookListEl.textContent = "Aucune note.";
    return;
  }
  notebookListEl.className = "notebook-list";
  notebookListEl.innerHTML = state.notebookNotes.map((note) => `
    <article class="notebook-note-item">
      <div class="notebook-note-heading"><strong>${escapeHtml(note.title || "Note sans titre")}</strong><span>${escapeHtml(notebookNoteDate(note))}</span></div>
      <p>${escapeHtml(note.body || "")}</p>
      <div class="notebook-note-actions">
        <button type="button" class="text-button notebook-delete-button" data-delete-note="${escapeHtml(note.id || "")}" aria-label="Supprimer cette note">Supprimer</button>
      </div>
    </article>`).join("");
}

async function deleteNotebookNote(noteId, button) {
  const id = String(noteId || "").trim();
  const note = state.notebookNotes.find((item) => String(item.id || "") === id);
  if (!id || !note) return;
  const title = note.title || "cette note";
  if (!window.confirm(`Supprimer « ${title} » du cahier de notes ?`)) return;
  if (button) button.disabled = true;
  try {
    const response = await fetch(`/api/v1/notebook/notes/${encodeURIComponent(id)}`, { method: "DELETE" });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || payload.error) throw new Error(payload.error || `HTTP ${response.status}`);
    state.notebookNotes = state.notebookNotes.filter((item) => String(item.id || "") !== id);
    renderNotebookNotes();
  } catch (error) {
    if (button) button.disabled = false;
    window.alert(`Suppression impossible : ${error.message || error}`);
  }
}

async function loadNotebookNotes() {
  if (!notebookListEl) return;
  try {
    const response = await fetch("/api/v1/notebook/notes");
    const payload = await response.json();
    if (!response.ok || payload.error) throw new Error(payload.error || `HTTP ${response.status}`);
    state.notebookNotes = Array.isArray(payload.items) ? payload.items : [];
    renderNotebookNotes();
  } catch {
    notebookListEl.className = "notebook-list muted";
    notebookListEl.textContent = "Carnet local indisponible.";
  }
}

async function saveNotebookNote() {
  const title = String(notebookNoteTitle && notebookNoteTitle.value || "").trim();
  const body = String(notebookNoteBody && notebookNoteBody.value || "").trim();
  if (!body) {
    if (notebookNoteBody) notebookNoteBody.focus();
    return;
  }
  if (notebookSaveButton) notebookSaveButton.disabled = true;
  try {
    const response = await fetch("/api/v1/notebook/notes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title, body }),
    });
    const payload = await response.json();
    if (!response.ok || payload.error) throw new Error(payload.error || `HTTP ${response.status}`);
    state.notebookNotes = [payload, ...state.notebookNotes];
    renderNotebookNotes();
    notebookNoteTitle.value = "";
    notebookNoteBody.value = "";
  } catch (error) {
    notebookListEl.className = "notebook-list muted";
    notebookListEl.textContent = `Impossible d’enregistrer : ${error.message || error}`;
  } finally {
    if (notebookSaveButton) notebookSaveButton.disabled = false;
  }
}

// ---------------------------------------------------------------- config / backend selection

// The installer creates four local Qwen3 profiles. Flash profiles use the
// bundled Modelfiles with thinking disabled; the plain profiles keep Qwen3's
// reasoning template. The selector exposes intent, not raw Ollama tags.
const REFLECTION_LEVELS_FALLBACK = [
  { id: "lite", label: "Flash lite", model: "qwen3:1.7b-flash", description: "Qwen3 1.7B Flash · rapide, sans raisonnement long" },
  { id: "flash", label: "Flash", model: "qwen3:1.7b", description: "Qwen3 1.7B · compromis vitesse/raisonnement" },
  { id: "high", label: "Élevé", model: "qwen3:4b-flash", description: "Qwen3 4B Flash · plus capable, réponse directe" },
  { id: "very_high", label: "Très élevé", model: "qwen3:4b", description: "Qwen3 4B · raisonnement approfondi" },
];
const QWEN3_FLASH_LITE_MODEL = "qwen3:1.7b-flash";
const QWEN3_FLASH_MODEL = "qwen3:1.7b";
const QWEN3_HIGH_MODEL = "qwen3:4b-flash";
const QWEN3_THINKING_MODEL = "qwen3:4b";
const THINKING_BACKENDS = new Set(["ollama", "igpu"]);

function reflectionLevels() {
  const configured = state.config && Array.isArray(state.config.reflection_levels)
    ? state.config.reflection_levels
    : [];
  return configured.length ? configured : REFLECTION_LEVELS_FALLBACK;
}

function reflectionLevelModel(level = reflectionSelect?.value) {
  const found = reflectionLevels().find((item) => item.id === String(level || ""));
  return found ? String(found.model || "") : QWEN3_FLASH_LITE_MODEL;
}

function isOllamaBackend(backend = backendSelect.value) {
  return backend === "ollama" || backend === "igpu";
}

function normalizedModelName(model) {
  return String(model || "").trim().toLowerCase();
}

function thinkingStorageKey(model) {
  return `3loop_ollama_thinking_${normalizedModelName(model)}`;
}

function supportsThinking(backend = backendSelect.value, model = selectedModel()) {
  return THINKING_BACKENDS.has(String(backend || ""))
    && normalizedModelName(model) === QWEN3_THINKING_MODEL;
}

function storedThinkingPreference(model) {
  try {
    const stored = localStorage.getItem(thinkingStorageKey(model));
    return stored === null ? true : stored === "1";
  } catch {
    return true;
  }
}

function selectedThinkingValue(backend = backendSelect.value, model = selectedModel()) {
  // The four-level selector is authoritative for local Ollama/iGPU profiles.
  // The old checkbox remains available only to a non-profile/manual caller.
  if (isOllamaBackend(backend)) return null;
  if (!supportsThinking(backend, model)) return null;
  return thinkingToggle ? Boolean(thinkingToggle.checked) : storedThinkingPreference(model);
}

function updateThinkingControl() {
  if (!thinkingControl || !thinkingToggle) return;
  if (isOllamaBackend()) {
    thinkingControl.hidden = true;
    thinkingToggle.disabled = true;
    thinkingToggle.checked = false;
    return;
  }
  const supported = supportsThinking();
  thinkingControl.hidden = !supported;
  thinkingToggle.disabled = !supported;
  if (!supported) {
    thinkingToggle.checked = false;
    return;
  }
  const enabled = storedThinkingPreference(selectedModel());
  thinkingToggle.checked = enabled;
  thinkingControl.title = enabled
    ? "Thinking actif : raisonnement approfondi."
    : "Thinking inactif : réponse directe.";
}

function ollamaModelLabel(model) {
  const normalized = normalizedModelName(model);
  if (normalized === QWEN3_FLASH_LITE_MODEL) return "Qwen3 1.7B Flash · Flash lite";
  if (normalized === QWEN3_FLASH_MODEL) return "Qwen3 1.7B · Flash";
  if (normalized === QWEN3_HIGH_MODEL) return "Qwen3 4B Flash · Élevé";
  if (normalized === QWEN3_THINKING_MODEL) return "Qwen3 4B · Très élevé";
  return String(model || "");
}

function populateReflectionLevels() {
  if (!reflectionSelect) return;
  const stored = (() => {
    try { return localStorage.getItem("3loop_reflection_level") || ""; } catch { return ""; }
  })();
  const current = reflectionSelect.dataset.initialized === "1"
    ? reflectionSelect.value
    : stored || reflectionSelect.value || "lite";
  const levels = reflectionLevels();
  const installed = new Set((state.config?.ollama_models || []).map(normalizedModelName));
  reflectionSelect.innerHTML = levels
    .map((item) => {
      const available = installed.has(normalizedModelName(item.model));
      const label = `${item.label} · ${item.description}${available ? " ✓" : " · non installé"}`;
      return `<option value="${escapeHtml(item.id)}">${escapeHtml(label)}</option>`;
    })
    .join("");
  reflectionSelect.value = levels.some((item) => item.id === current)
    ? current
    : (levels[0]?.id || "lite");
  reflectionSelect.dataset.initialized = "1";
  const selected = levels.find((item) => item.id === reflectionSelect.value) || levels[0];
  const available = selected && installed.has(normalizedModelName(selected.model));
  if (reflectionHint && selected) {
    reflectionHint.textContent = available
      ? `${selected.description} · modèle local disponible.`
      : `${selected.description} · modèle absent : relance l’installation des profils Qwen3.`;
    reflectionHint.className = available ? "hint" : "hint warn";
  }
}

function updateReflectionControl() {
  const local = isOllamaBackend();
  if (reflectionSection) reflectionSection.hidden = !local;
  if (modelSection) modelSection.hidden = local;
  if (local) populateReflectionLevels();
}

function populateOllamaModels(models) {
  modelSelect.innerHTML = models
    .map((model) => `<option value="${escapeHtml(model)}">${escapeHtml(ollamaModelLabel(model))}</option>`)
    .join("");
  if (models.includes(QWEN3_THINKING_MODEL)) modelSelect.value = QWEN3_THINKING_MODEL;
}

async function loadConfig() {
  const res = await fetch("/api/config");
  state.config = await res.json();
  populateBackendOptions();
  refreshResearchStatus();
  // Gmail analysis must wait for the selected backend/model to be populated;
  // otherwise an initial race could silently use the demo backend.
  void loadGmailStatus();
}

// The searcher is deliberately *not* the chat backend: it always runs on a
// small local model (or on raw queries when none is installed). Saying which
// one, in the panel, is the only way the user can tell.
const RESEARCH_STATUS_FALLBACK = "Le petit compagnon peut chercher pendant que tu travailles.";

function researchAgentInfo() {
  const agent = state.config && state.config.research_agent;
  return agent && typeof agent === "object" ? agent : null;
}

function researchAgentLabel() {
  const agent = researchAgentInfo();
  return agent && agent.label ? String(agent.label) : "";
}

function researchIdleStatusText() {
  const agent = researchAgentInfo();
  const label = researchAgentLabel();
  if (!label) return RESEARCH_STATUS_FALLBACK;
  const hint = agent && agent.hint ? String(agent.hint).trim() : "";
  return hint ? `Chercheur local : ${label} · ${hint}` : `Chercheur local : ${label}`;
}

function refreshResearchStatus() {
  // Never stomp on the live progress of a running search.
  if (!researchStatus || state.researchRunning) return;
  researchStatus.textContent = researchIdleStatusText();
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

  const ollamaModels = state.config.ollama_models || [];
  const hasStoredKey = Object.keys(state.config.cloud_providers).some((p) => loadStoredKey(p));
  // This workspace now explicitly prefers the local Qwen3 profile. Cloud
  // backends remain selectable, but startup no longer silently switches away
  // from the model whose Thinking/Flash control is visible in the composer.
  if (reflectionLevels().some((item) => ollamaModels.includes(item.model))) backendSelect.value = "ollama";
  else if (opencode.available) backendSelect.value = "opencode";
  else if (hasStoredKey) backendSelect.value = Object.keys(state.config.cloud_providers).find((p) => loadStoredKey(p));
  else if (localGguf.length > 0) backendSelect.value = "llama_cpp";
  else if (ollamaModels.length > 0) backendSelect.value = "ollama";
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

function isCodingCliBackend(backend) {
  return backend === "opencode" || backend === "claude_code" || backend === "codex";
}

function selectedReflectionLevel() {
  return isOllamaBackend() ? String(reflectionSelect?.value || "lite") : "";
}

function selectedModel() {
  if (isOllamaBackend()) return reflectionLevelModel(selectedReflectionLevel());
  const custom = String(customModelInput && customModelInput.value || "").trim();
  return custom || String(modelSelect.value || "");
}

function updateCodingWriteUI() {
  const enabled = isCodingCliBackend(backendSelect.value);
  if (customModelSection) customModelSection.hidden = !enabled;
  if (codingWriteSection) codingWriteSection.hidden = !enabled;
  if (!enabled && codingWriteToggle) codingWriteToggle.checked = false;
  if (codingWorkspaceInput) {
    codingWorkspaceInput.hidden = !enabled || !codingWriteToggle?.checked;
    codingWorkspaceInput.required = enabled && Boolean(codingWriteToggle?.checked);
  }
  if (codingWriteHint) {
    if (!enabled) codingWriteHint.textContent = "Lecture seule par défaut.";
    else if (codingWriteToggle?.checked) {
      codingWriteHint.className = "hint warn";
      codingWriteHint.textContent = "Une seule exécution compacte pourra modifier ce dossier. Vérifie le chemin avant d’envoyer.";
    } else {
      codingWriteHint.className = "hint warn";
      codingWriteHint.textContent = "Lecture seule par défaut. Active ce mode uniquement après avoir choisi le dossier cible.";
    }
  }
}

function updateBackendUI() {
  const backend = backendSelect.value;
  const cloudInfo = state.config.cloud_providers[backend];
  if (modelSection) modelSection.hidden = false;
  if (reflectionSection) reflectionSection.hidden = true;

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
    populateOllamaModels(models);
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
      populateOllamaModels(models);
      backendHint.textContent = "Inférence locale. Choisis Flash lite, Flash, Élevé ou Très élevé dans le sélecteur de réflexion.";
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
  updateCodingWriteUI();
  updateReflectionControl();
  updateThinkingControl();
}

// One delegated listener rather than one per block: code blocks and message
// actions are created continuously as answers stream in.
messagesEl.addEventListener("click", (event) => {
  const messageButton = event.target.closest("[data-copy-message]");
  if (messageButton) {
    copyMessageText(messageButton);
    return;
  }

  const feedbackButton = event.target.closest("[data-feedback-rating]");
  if (feedbackButton) {
    submitMessageFeedback(feedbackButton);
    return;
  }

  const cliDecision = event.target.closest("[data-cli-decision]");
  if (cliDecision) {
    resolveCliInteraction(cliDecision);
    return;
  }

  const codeButton = event.target.closest(".code-copy");
  if (!codeButton) return;
  const code = codeButton.closest(".code-block")?.querySelector("code");
  if (!code) return;
  writeClipboardText(code.textContent).then(
    () => {
      codeButton.textContent = "Copié";
      setTimeout(() => {
        if (codeButton.isConnected) codeButton.textContent = "Copier";
      }, 1500);
    },
    () => (codeButton.textContent = "Échec"),
  );
});

backendSelect.addEventListener("change", () => {
  if (customModelInput) customModelInput.value = "";
  updateBackendUI();
});
modelSelect.addEventListener("change", updateThinkingControl);
if (reflectionSelect) reflectionSelect.addEventListener("change", () => {
  try { localStorage.setItem("3loop_reflection_level", reflectionSelect.value); } catch { /* ignore */ }
  updateReflectionControl();
  updateThinkingControl();
});
if (thinkingToggle) thinkingToggle.addEventListener("change", () => {
  const backend = backendSelect.value;
  const model = selectedModel();
  if (!supportsThinking(backend, model)) {
    updateThinkingControl();
    return;
  }
  try {
    localStorage.setItem(thinkingStorageKey(model), thinkingToggle.checked ? "1" : "0");
  } catch {
    /* The current control remains usable if browser storage is unavailable. */
  }
  updateThinkingControl();
});
if (codingWriteToggle) codingWriteToggle.addEventListener("change", updateCodingWriteUI);
if (codingWorkspaceInput) codingWorkspaceInput.addEventListener("input", updateCodingWriteUI);
apiKeyInput.addEventListener("input", () => {
  storeKey(backendSelect.value, apiKeyInput.value);
  backendHint.textContent = apiKeyInput.value ? "" : "Colle ta clé API gratuite pour activer ce backend.";
  backendHint.className = apiKeyInput.value ? "hint" : "hint warn";
});

cyclesRange.addEventListener("input", () => (cyclesValue.textContent = cyclesRange.value));
tokensRange.addEventListener("input", () => (tokensValue.textContent = tokensRange.value));

newChatBtn.addEventListener("click", () => {
  state.sessionId = crypto.randomUUID ? crypto.randomUUID() : String(Date.now());
  state.conversation = [];
  state.activeConversationId = null;
  setCompactStatus("");
  renderDiscussionList();
  messagesEl.innerHTML = `
    <div class="empty-state">
      <div id="empty-badge" class="empty-badge"></div>
      <h1>LOUPe</h1>
      <p>Heuristique → Critique → Rédacteur → Vote de consensus.<br/>Pose une question de code, de maths, ou une recherche.</p>
    </div>`;
  applyVisualTheme();
});

// ---------------------------------------------------------------- run + SSE

function currentBackendLabel(backend = backendSelect.value) {
  const option = [...backendSelect.options].find((item) => item.value === backend);
  return option ? option.textContent : backend;
}

const SEND_BUTTON_ICON = '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path fill="currentColor" d="M3 11.5 21 3l-7 18-3-7-8-2.5Z"/></svg>';
const STOP_BUTTON_ICON = '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor"/></svg>';

function refreshSendButton() {
  const stopping = Boolean(state.running);
  const localRun = stopping && isLocalFeedbackBackend(backendSelect.value);
  const stopLabel = localRun ? "Arrêter le modèle local" : "Arrêter";
  sendBtn.disabled = stopping ? false : promptInput.value.trim().length === 0;
  sendBtn.classList.toggle("is-stop", stopping);
  sendBtn.classList.toggle("is-local-stop", localRun);
  sendBtn.setAttribute("aria-label", stopping ? stopLabel : "Envoyer");
  sendBtn.title = stopping ? stopLabel : "Envoyer";
  sendBtn.innerHTML = stopping
    ? `${STOP_BUTTON_ICON}<span class="send-button-label">${stopLabel}</span>`
    : SEND_BUTTON_ICON;
}

function cancelAssistantMessage(wrap) {
  if (!wrap || wrap.classList.contains("is-complete") || wrap.classList.contains("is-error")) return;
  wrap.classList.remove("is-running");
  wrap.classList.add("is-cancelled");
  const avatar = wrap.querySelector(".msg-avatar");
  if (avatar) {
    avatar.className = "msg-avatar";
    const visualKind = wrap.dataset.visualKind || kindSelect?.value || "auto";
  avatar.innerHTML = mascotSprite(32, { kind: visualKind });
  }
  const stateLabel = wrap.querySelector(".lab-notebook-state");
  if (stateLabel) stateLabel.textContent = "ARRÊTÉ";
  appendResearchTrace(wrap, "warning", "Génération arrêtée à ta demande.");
  const content = wrap.querySelector(".msg-content");
  if (content) content.innerHTML = "<p>Génération arrêtée à ta demande.</p>";
  const trace = wrap.querySelector(".lab-research-trace");
  if (trace) trace.open = true;
  const meta = wrap.querySelector(".msg-meta");
  if (meta) meta.innerHTML = '<span class="status-pill warn">Génération arrêtée</span>';
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function requestRunCancellation(jobId) {
  if (!jobId) return;
  fetch(`/api/v1/runs/${encodeURIComponent(jobId)}/cancel`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  }).catch(() => {
    // Closing the SSE request still releases the browser immediately. The
    // server will observe the disconnect if its cancellation request lost a race.
  });
}

function stopActiveRun() {
  const run = state.activeRun;
  if (!run || run.stopRequested) return;
  run.stopRequested = true;
  requestRunCancellation(run.jobId || run.runId);
  run.controller.abort();
  cancelAssistantMessage(run.wrap);
  refreshSendButton();
}

sendBtn.addEventListener("click", (event) => {
  if (!state.running) return;
  event.preventDefault();
  stopActiveRun();
});

async function runPrompt(prompt, enginePrompt = prompt, controller) {
  addUserMessage(prompt);
  const run = state.activeRun;
  if (!controller || !run || run.controller !== controller || controller.signal.aborted) return;
  const backend = backendSelect.value;
  const model = selectedModel();
  const reflectionLevel = selectedReflectionLevel();
  const thinking = selectedThinkingValue(backend, model);
  const allowWrites = isCodingCliBackend(backend) && Boolean(codingWriteToggle?.checked);
  const workspacePath = allowWrites ? String(codingWorkspaceInput?.value || "").trim() : "";
  if (allowWrites && !workspacePath) {
    const wrap = addAssistantMessage();
    errorInAssistantMessage(wrap, "Choisis explicitement un dossier de travail avant d’autoriser les écritures.");
    return;
  }
  const wrap = addAssistantMessage();
  run.wrap = wrap;
  wrap.dataset.backend = backend;
  wrap.dataset.model = model;
  wrap._promptText = String(prompt || "");
  const votes = {};
  const previousConversation = state.conversation.slice();

  const payload = {
    prompt: enginePrompt,
    run_id: run.runId,
    session_id: state.sessionId,
    conversation: previousConversation,
    conversation_append: { user: prompt },
    backend,
    model,
    reflection_level: reflectionLevel,
    api_key: apiKeyInput.value,
    allow_writes: allowWrites,
    workspace_path: workspacePath,
    research: researchToggle.checked && !allowWrites,
    max_cycles: Number(cyclesRange.value),
    max_tokens: Number(tokensRange.value),
    task_kind: kindSelect.value,
    ...(thinking === null ? {} : { thinking }),
  };

  let response;
  try {
    response = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
  } catch (err) {
    if (controller.signal.aborted) {
      cancelAssistantMessage(wrap);
      return;
    }
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
  } catch (err) {
    if (controller.signal.aborted) {
      cancelAssistantMessage(wrap);
    } else {
      errorInAssistantMessage(wrap, `Lecture de la réponse interrompue: ${err}`);
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

  if (data.job_id) {
    wrap.dataset.jobId = String(data.job_id);
    if (state.activeRun?.wrap === wrap) state.activeRun.jobId = String(data.job_id);
  }

  switch (eventName) {
    case "run_started":
      appendResearchTrace(wrap, "planned", "Protocole de réponse construit localement.");
      updateThinkingStatus(wrap, "Protocole prêt, lancement des agents…");
      break;
    case "cli_write_mode":
      appendResearchTrace(
        wrap,
        "warning",
        data.message || "Mode écriture actif : le CLI peut modifier le dossier choisi.",
      );
      updateThinkingStatus(wrap, "Mode écriture explicite actif, le CLI attend éventuellement une permission…");
      break;
    case "cli_interaction":
      renderCliInteraction(wrap, data);
      appendResearchTrace(
        wrap,
        "warning",
        data.kind === "permission" ? "Permission CLI en attente de ta décision." : "Question CLI en attente de ta réponse.",
        { role: data.agent || "CLI" },
      );
      updateThinkingStatus(wrap, "Le CLI attend ton interaction…");
      break;
    case "cli_interaction_resolved":
      updateCliInteraction(data.interaction_id, data.decision === "deny" ? "Refus transmis" : "Décision transmise", true);
      break;
    case "cli_interaction_timeout":
      updateCliInteraction(data.interaction_id, data.message || "Demande refusée après expiration.", true);
      appendResearchTrace(wrap, "warning", data.message || "Interaction CLI expirée.");
      break;
    case "cycle_started":
      appendResearchTrace(wrap, "decision", `Cycle ${data.cycle} lancé.`);
      updateThinkingStatus(wrap, `Cycle ${data.cycle} démarré…`);
      break;
    case "research_query":
      if (data.role) {
        const label = roleMeta(data.role).label;
        appendResearchTrace(wrap, "query", `${label} formule une requête.`, { role: label });
        updateThinkingStatus(wrap, `${label} prépare une recherche…`);
      }
      break;
    case "research_sources": {
      const sources = Array.isArray(data.sources) ? data.sources : [];
      if (sources.length) {
        for (const source of sources) {
          appendResearchTrace(
            wrap,
            "source",
            source.title || source.domain || "Source scientifique",
            { url: source.url || "" }
          );
        }
      } else {
        appendResearchTrace(wrap, "warning", "Aucune source triangulée pour cette étape.");
      }
      updateThinkingStatus(wrap, `${sources.length} source(s) retenue(s), lecture en cours…`);
      break;
    }
    case "vote":
      if (data.role) {
        votes[data.role] = { resolved: data.resolved, confidence: data.confidence };
        const label = roleMeta(data.role).label;
        appendResearchTrace(
          wrap,
          "verification",
          `${label} : ${data.resolved ? "validation" : "révision demandée"}.`,
          { role: label }
        );
        updateThinkingStatus(wrap, `${label} a vérifié le résultat (cycle ${data.cycle})…`);
      }
      break;
    case "agent_output":
      if (data.role) {
        const label = roleMeta(data.role).label;
        appendResearchTrace(wrap, "reading", `${label} a terminé son étape.`, { role: label });
        updateThinkingStatus(wrap, `${label} a répondu, étape suivante…`);
      }
      break;
    case "run_completed":
      finalizeAssistantMessage(wrap, {
        finalSolution: data.final_solution,
        consensusReached: data.consensus_reached,
        completedCycles: data.completed_cycles,
        backendLabel: currentBackendLabel(payload.backend),
        backend: data.backend || payload.backend,
        model: data.model || payload.model,
      });
      if (data.final_solution && payload.conversation_append?.user) {
        state.conversation = [
          ...(payload.conversation || []),
          { role: "user", text: payload.conversation_append.user },
          { role: "assistant", text: data.final_solution },
        ];
      }
      break;
    case "run_cancelled":
      cancelAssistantMessage(wrap);
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
  refreshSendButton();
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

async function scrapeUrl(url, signal) {
  const response = await fetch("/api/scrape", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url }),
    signal,
  });
  return response.json();
}

composerEl.addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = promptInput.value.trim();
  if (!text || state.running) return;
  promptInput.value = "";
  promptInput.style.height = "auto";
  const controller = new AbortController();
  const runId = crypto.randomUUID ? crypto.randomUUID() : `run-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  state.activeRun = { controller, runId, jobId: "", wrap: null, stopRequested: false };
  state.running = true;
  refreshSendButton();

  const urlMatch = text.match(URL_RE);
  let enginePrompt = text;
  const originalPlaceholder = promptInput.placeholder;

  try {
    if (urlMatch) {
      updateStatus(`Lecture de ${urlMatch[0]}…`);
      const question = stripUrls(text) || "Analyse ce qui est disponible localement.";
      try {
        const page = await scrapeUrl(urlMatch[0], controller.signal);
        if (page.error) throw new Error(page.error);
        enginePrompt =
          `Page web "${page.title || page.url}" (${page.url}):\n---\n${page.text}\n---\n\n` +
          `Question de l'utilisateur: ${question}`;
      } catch (error) {
        if (controller.signal.aborted) return;
        // A disconnected machine must still be able to answer from the
        // user's wording and any explicitly attached local documents.
        enginePrompt =
          `L'URL "${urlMatch[0]}" n'est pas accessible dans cette session ` +
          `(${error.message || error}). N'invente pas son contenu.\n\n` +
          `Question de l'utilisateur: ${question}`;
      }
      updateStatus("");
    }
    const documentsContext = attachedDocumentsContext();
    if (documentsContext) {
      enginePrompt = `${documentsContext}\n\nQuestion de l'utilisateur: ${enginePrompt}`;
    }
    await runPrompt(text, enginePrompt, controller);
  } finally {
    if (state.activeRun?.controller === controller) state.activeRun = null;
    state.running = false;
    refreshSendButton();
    promptInput.placeholder = originalPlaceholder;
  }
});

function updateStatus(text) {
  promptInput.placeholder = text || "Écris ton message…";
}

// ---------------------------------------------------------------- local eye tracking + instant help
//
// The Python service is optional and local. Its result is a confidence-aware
// estimate, so it only proposes help after a dwell event; the manual button
// and Ctrl+Maj+H remain available at all times.
let eyeTrackingPoll = null;
let lastEyeTrackingEvent = 0;

function requestInstantHelp(reason = "") {
  promptInput.focus();
  if (!promptInput.value.trim()) promptInput.placeholder = "Décris ce qui te bloque ici…";
  if (helpStatus) {
    helpStatus.textContent = reason || "Le chat est prêt. Décris le blocage ou colle le résultat concerné.";
    helpStatus.className = "hint ok";
  }
}

function renderEyeTrackingStatus(status) {
  if (!helpStatus || !status) return;
  const state = String(status.state || "unknown");
  const confidence = Math.round(Number(status.confidence || 0) * 100);
  const labels = {
    stopped: "Suivi Python arrêté. Aide manuelle disponible.",
    unavailable: String(status.message || "Suivi Python indisponible. Aide manuelle disponible."),
    calibrating: `Calibration locale en cours · confiance ${confidence} %.`,
    tracking: `Suivi Python actif · confiance ${confidence} % · maintien ${Number(status.dwell_seconds || 0).toFixed(1)} s.`,
    blocked: "Blocage probable détecté : l’aide est prête.",
  };
  helpStatus.textContent = labels[state] || String(status.message || "État du suivi inconnu.");
  helpStatus.className = state === "blocked" ? "hint warn" : state === "unavailable" ? "hint warn" : "hint";
  if (eyeTrackingStartButton) eyeTrackingStartButton.hidden = Boolean(status.available && state !== "stopped");
  if (eyeTrackingStopButton) eyeTrackingStopButton.hidden = !status.available || state === "stopped";
  if (state === "blocked" && Number(status.event_seq || 0) > lastEyeTrackingEvent) {
    lastEyeTrackingEvent = Number(status.event_seq || 0);
    requestInstantHelp("Blocage probable détecté par le modèle local. Décris maintenant ce qui te bloque.");
  }
}

async function pollEyeTrackingStatus() {
  try {
    const response = await fetch("/api/v1/eye-tracking/status");
    const status = await response.json();
    if (response.ok || status) renderEyeTrackingStatus(status);
  } catch {
    if (helpStatus) helpStatus.textContent = "Suivi Python indisponible. L’aide manuelle reste active.";
  }
}

function startEyeTrackingPoll() {
  clearInterval(eyeTrackingPoll);
  pollEyeTrackingStatus();
  eyeTrackingPoll = window.setInterval(pollEyeTrackingStatus, 900);
}

async function startEyeTracking() {
  if (eyeTrackingStartButton) eyeTrackingStartButton.disabled = true;
  try {
    const response = await fetch("/api/v1/eye-tracking/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ camera_index: 0 }),
    });
    const status = await response.json().catch(() => ({}));
    renderEyeTrackingStatus(status);
    if (!response.ok && status.state !== "unavailable") throw new Error(status.error || status.message || `HTTP ${response.status}`);
    startEyeTrackingPoll();
  } catch (error) {
    if (helpStatus) {
      helpStatus.textContent = `Suivi Python indisponible : ${error.message || error}. Utilise l’aide manuelle.`;
      helpStatus.className = "hint warn";
    }
  } finally {
    if (eyeTrackingStartButton) eyeTrackingStartButton.disabled = false;
  }
}

async function stopEyeTracking() {
  clearInterval(eyeTrackingPoll);
  eyeTrackingPoll = null;
  try {
    const response = await fetch("/api/v1/eye-tracking/stop", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    renderEyeTrackingStatus(await response.json());
  } catch {
    if (helpStatus) helpStatus.textContent = "Le suivi est arrêté côté interface. L’aide manuelle reste active.";
  }
}

if (helpNowButton) helpNowButton.addEventListener("click", () => requestInstantHelp());
if (eyeTrackingStartButton) eyeTrackingStartButton.addEventListener("click", startEyeTracking);
if (eyeTrackingStopButton) eyeTrackingStopButton.addEventListener("click", stopEyeTracking);
document.addEventListener("keydown", (event) => {
  if (event.ctrlKey && event.shiftKey && event.key.toLowerCase() === "h") {
    event.preventDefault();
    requestInstantHelp();
  }
});
pollEyeTrackingStatus();

// ---------------------------------------------------------------- boot

el("brand-badge").innerHTML = mascotSprite(32);
el("empty-badge").innerHTML = mascotSprite(64);
applyVisualTheme();
kindSelect.addEventListener("change", () => {
  applyVisualTheme(kindSelect.value);
  const profile = mascotProfile(kindSelect.value);
  if (backendHint && kindSelect.value !== "auto") {
    backendHint.dataset.visualHint = profile.className;
  }
});
loadConfig();
loadScientificLibrary();
loadNotebookNotes();
renderScientificResults();
renderNotebookNotes();
refreshSendButton();


// ---------------------------------------------------------------- background research

function researchResultsMarkup(sources, errors = {}) {
  const safeSources = Array.isArray(sources) ? sources : [];
  const cards = safeSources
    .map((source) => {
      const url = String(source.url || "").trim();
      if (!/^https?:\/\//i.test(url)) return "";
      return (
        `<a class="research-result" href="${escapeHtml(url)}" target="_blank" rel="noopener">` +
        `<span class="research-result-title">${escapeHtml(source.title || source.domain || url)}</span>` +
        `<span class="research-result-url">${escapeHtml(url)}</span>` +
        (source.snippet ? `<span class="research-result-snippet">${escapeHtml(source.snippet)}</span>` : "") +
        `</a>`
      );
    })
    .filter(Boolean)
    .join("");
  const errorCount = Object.keys(errors || {}).length;
  return (
    `<div class="research-results-head"><span class="status-pill ok">Recherche terminée</span>` +
    `<span>${safeSources.length} source(s)${errorCount ? ` · ${errorCount} erreur(s)` : ""}</span></div>` +
    `<div class="research-results-list">${cards || `<p class="muted">Aucune source concordante.</p>`}</div>`
  );
}

function finalizeResearchResultMessage(wrap, question, sources, errors) {
  wrap.classList.remove("is-running");
  wrap.classList.add("is-complete", "research-message");
  const avatar = wrap.querySelector(".msg-avatar");
  avatar.className = "msg-avatar";
  const visualKind = wrap.dataset.visualKind || kindSelect?.value || "auto";
  avatar.innerHTML = mascotSprite(32, { watch: true, kind: visualKind });
  const stateLabel = wrap.querySelector(".lab-notebook-state");
  if (stateLabel) stateLabel.textContent = "ARCHIVÉ";
  const content = wrap.querySelector(".msg-content");
  content.innerHTML =
    `<section class="lab-observation"><span class="lab-block-label">QUESTION ML</span>` +
    `<p><strong>${escapeHtml(question)}</strong></p></section>` +
    researchResultsMarkup(sources, errors);
  const sourceCount = Array.isArray(sources) ? sources.length : 0;
  appendResearchTrace(
    wrap,
    "completed",
    `Recherche terminée : ${sourceCount} source(s) conservée(s).`
  );
  const trace = wrap.querySelector(".lab-research-trace");
  if (trace) trace.open = false;
  const meta = wrap.querySelector(".msg-meta");
  wrap._messageText = content.innerText.trim();
  const jobId = wrap.dataset.jobId ? ` · job ${escapeHtml(wrap.dataset.jobId.slice(0, 8))}` : "";
  meta.innerHTML = `<span class="status-pill ok">Recherche ML terminée</span><span>${sourceCount} source(s)${jobId}</span>${messageActionsMarkup()}`;
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

async function runBackgroundResearch(question) {
  const query = String(question || "").trim();
  if (!query || !researchNowButton) return;
  researchNowButton.disabled = true;
  researchQuestionInput.disabled = true;
  state.researchRunning = true;
  const wrap = addAssistantMessage({ title: "Recherche machine learning", watch: true });
  appendResearchTrace(
    wrap,
    "planned",
    "Profil ML : articles, datasets, benchmarks, métriques, code, licences et reproductibilité."
  );
  updateThinkingStatus(wrap, "Le chercheur local prépare les requêtes ML…");
  const knownAgent = researchAgentLabel();
  researchStatus.textContent = knownAgent
    ? `Chercheur local : ${knownAgent} — interrogation des sources…`
    : "Les chercheurs interrogent plusieurs sources…";
  try {
    const response = await fetch("/api/research", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question: query,
        backend: backendSelect.value,
        model: modelSelect.value,
        api_key: apiKeyInput.value,
        max_tokens: Number(tokensRange.value),
        task_kind: kindSelect.value,
      }),
    });
    if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`);
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let result = null;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let separator;
      while ((separator = buffer.indexOf("\n\n")) !== -1) {
        const rawEvent = buffer.slice(0, separator);
        buffer = buffer.slice(separator + 2);
        let eventName = "message";
        let dataLine = "";
        for (const line of rawEvent.split("\n")) {
          if (line.startsWith("event:")) eventName = line.slice(6).trim();
          else if (line.startsWith("data:")) dataLine += line.slice(5).trim();
        }
        if (!dataLine) continue;
        const data = JSON.parse(dataLine);
        if (eventName === "research_started") {
          // The server names the searcher it actually managed to load, which
          // can differ from what /api/config advertised (a model may fail to
          // start), so its value wins.
          const label = String(data.agent || "").trim() || researchAgentLabel();
          const suffix = data.mode === "search_only" ? " (requêtes brutes, sans modèle)" : "";
          if (data.job_id) {
    wrap.dataset.jobId = String(data.job_id);
    if (state.activeRun?.wrap === wrap) state.activeRun.jobId = String(data.job_id);
  }
          appendResearchTrace(
            wrap,
            "planned",
            label ? `Planificateur local : ${label}${suffix}.` : "Planificateur de recherche démarré."
          );
          updateThinkingStatus(wrap, "Interrogation des sources scientifiques…");
          researchStatus.textContent = label
            ? `Chercheur local : ${label}${suffix} — interrogation des sources…`
            : String(data.message || "Recherche en cours…");
        } else if (eventName === "research_query") {
          const role = roleMeta(data.role).label;
          appendResearchTrace(wrap, "query", data.query || `${role} affine la recherche.`, { role });
          updateThinkingStatus(wrap, `${role} affine la recherche…`);
          researchStatus.textContent = `${role} affine la recherche…`;
        } else if (eventName === "research_completed") {
          result = data;
          for (const source of data.sources || []) {
            appendResearchTrace(
              wrap,
              "source",
              source.title || source.domain || source.url || "Source scientifique",
              { url: source.url || "" }
            );
          }
          for (const error of Object.values(data.errors || {})) {
            appendResearchTrace(wrap, "warning", String(error));
          }
        } else if (eventName === "error") {
          throw new Error(data.message || "Recherche impossible");
        }
      }
    }
    if (!result) throw new Error("La recherche n'a pas renvoyé de résultat");
    finalizeResearchResultMessage(wrap, query, result.sources || result.results || [], result.errors || {});
    const doneAgent = researchAgentLabel();
    researchStatus.textContent = doneAgent
      ? `Recherche terminée par ${doneAgent}. Tu peux continuer à travailler.`
      : "Recherche terminée. Tu peux continuer à travailler.";
    researchQuestionInput.value = "";
  } catch (error) {
    errorInAssistantMessage(wrap, `Recherche indisponible : ${error.message || error}`);
    researchStatus.textContent = `Recherche indisponible : ${error.message || error}`;
  } finally {
    state.researchRunning = false;
    researchNowButton.disabled = false;
    researchQuestionInput.disabled = false;
  }
}

if (researchNowButton) {
  researchNowButton.addEventListener("click", () => runBackgroundResearch(researchQuestionInput.value));
}
if (researchQuestionInput) {
  researchQuestionInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      runBackgroundResearch(researchQuestionInput.value);
    }
  });
}


// ---------------------------------------------------------------- scientific workspace UI ---------------------------------

function scientificRecordUrl(record) {
  const url = String(record && record.url || "").trim();
  if (/^https?:\/\//i.test(url)) return url;
  const doi = String(record && record.doi || "").trim();
  return doi ? `https://doi.org/${encodeURIComponent(doi)}` : "";
}

function scientificMessageMarkup(question, payload) {
  const results = Array.isArray(payload && payload.results) ? payload.results : [];
  const errors = payload && payload.errors && typeof payload.errors === "object" ? payload.errors : {};
  const cards = results.map((record, index) => {
    const url = scientificRecordUrl(record);
    const title = record.title || "Résultat sans titre";
    const meta = [record.provider, record.year, record.venue].filter(Boolean).join(" · ");
    const abstract = String(record.abstract || "").trim();
    const link = url
      ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener">source</a>`
      : "source non liée";
    const key = scientificRecordKey(record);
    const saveButton = record.title || record.external_id || record.doi
      ? `<button type="button" class="text-button" data-save-scientific-key="${escapeHtml(key)}" ${record._saved ? "disabled" : ""}>${record._saved ? "Ajouté" : "Ajouter à la bibliothèque"}</button>`
      : "";
    return `<article class="chat-scientific-result">
      <div class="chat-scientific-result-title"><span>${index + 1}</span><strong>${escapeHtml(title)}</strong></div>
      <div class="chat-scientific-result-meta">${escapeHtml(meta || "Source scientifique")} · ${link}</div>
      ${abstract ? `<p>${escapeHtml(abstract.slice(0, 360))}${abstract.length > 360 ? "…" : ""}</p>` : ""}
      <div class="scientific-result-actions">${saveButton}</div>
    </article>`;
  }).join("");
  const errorCount = Object.keys(errors).length;
  return `<section class="lab-observation"><span class="lab-block-label">QUESTION ML</span><p><strong>${escapeHtml(question)}</strong></p></section>
    <div class="scientific-message-summary"><span class="status-pill ok">Recherche fédérée</span><span>${results.length} résultat(s) · profil ${escapeHtml(payload && payload.profile || "machine-learning")}${errorCount ? ` · ${errorCount} fournisseur(s) en erreur` : ""}</span></div>
    <div class="chat-scientific-results">${cards || `<p class="muted">Aucun résultat concordant.</p>`}</div>`;
}

function finalizeScientificSearchMessage(wrap, question, payload) {
  wrap.classList.remove("is-running");
  wrap.classList.add("is-complete", "research-message");
  const avatar = wrap.querySelector(".msg-avatar");
  avatar.className = "msg-avatar";
  avatar.innerHTML = mascotSprite(32, { watch: true });
  const stateLabel = wrap.querySelector(".lab-notebook-state");
  if (stateLabel) stateLabel.textContent = "ARCHIVÉ";
  const content = wrap.querySelector(".msg-content");
  content.innerHTML = scientificMessageMarkup(question, payload);
  const count = Array.isArray(payload && payload.results) ? payload.results.length : 0;
  appendResearchTrace(wrap, "completed", `Recherche fédérée terminée : ${count} résultat(s).`);
  const trace = wrap.querySelector(".lab-research-trace");
  if (trace) trace.open = false;
  const meta = wrap.querySelector(".msg-meta");
  wrap._messageText = content.innerText.trim();
  meta.innerHTML = `<span class="status-pill ok">Recherche ML terminée</span><span>${count} résultat(s) · run local sauvegardé</span>${messageActionsMarkup()}`;
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

async function runScientificSearch() {
  const query = String(scientificSearchInput && scientificSearchInput.value || "").trim();
  if (!query || !scientificSearchButton) return;
  scientificSearchButton.disabled = true;
  if (scientificSearchInput) scientificSearchInput.disabled = true;
  if (scientificSearchStatus) scientificSearchStatus.textContent = "Le planificateur ML interroge les sources…";
  const wrap = addAssistantMessage({ title: "Recherche scientifique ML", watch: true });
  appendResearchTrace(wrap, "planned", "Profil ML borné : méthodes, datasets, benchmarks, métriques, code et licences.");
  updateThinkingStatus(wrap, "Construction du plan fédéré…");
  try {
    const response = await fetch("/api/v1/research/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: query, profile: "machine-learning", max_results: 12, timeout: 12 }),
    });
    const payload = await response.json();
    if (!response.ok || payload.error) throw new Error(payload.error || `HTTP ${response.status}`);
    for (const [provider, providerQuery] of Object.entries(payload.queries || {})) {
      appendResearchTrace(wrap, "query", `${provider} · ${providerQuery}`);
    }
    for (const record of payload.results || []) {
      appendResearchTrace(wrap, "source", record.title || record.provider || "Résultat scientifique", {
        url: scientificRecordUrl(record),
      });
    }
    for (const [provider, error] of Object.entries(payload.errors || {})) {
      appendResearchTrace(wrap, "warning", `${provider} : ${error}`);
    }
    state.scientificResults = Array.isArray(payload.results) ? payload.results : [];
    rememberScientificResults(state.scientificResults);
    renderScientificResults();
    finalizeScientificSearchMessage(wrap, query, payload);
    if (scientificSearchStatus) scientificSearchStatus.textContent = `${state.scientificResults.length} résultat(s) affiché(s) · run ${String(payload.run_id || "local").slice(0, 8)}`;
  } catch (error) {
    errorInAssistantMessage(wrap, `Recherche scientifique indisponible : ${error.message || error}`);
    if (scientificSearchStatus) scientificSearchStatus.textContent = `Recherche indisponible : ${error.message || error}`;
  } finally {
    if (scientificSearchButton) scientificSearchButton.disabled = false;
    if (scientificSearchInput) scientificSearchInput.disabled = false;
  }
}

function comparisonMarkup(result) {
  const papers = Array.isArray(result && result.papers) ? result.papers : [];
  const dimensions = Array.isArray(result && result.dimensions) ? result.dimensions : [];
  const rows = Array.isArray(result && result.matrix) ? result.matrix : [];
  const labels = new Map(papers.map((paper) => [String(paper.paper_id), paper.title || paper.paper_id]));
  const header = dimensions.map((dimension) => `<th scope="col">${escapeHtml(dimension)}</th>`).join("");
  const body = rows.map((row) => {
    const cells = dimensions.map((dimension) => {
      const value = row[dimension];
      const unknown = value === null || value === undefined || value === "";
      return `<td class="${unknown ? "comparison-unknown" : ""}">${unknown ? "inconnu" : escapeHtml(String(value))}</td>`;
    }).join("");
    return `<tr><th scope="row">${escapeHtml(labels.get(String(row.paper_id)) || row.paper_id || "article")}</th>${cells}</tr>`;
  }).join("");
  return `<section class="lab-observation"><span class="lab-block-label">COMPARAISON MULTI-ARTICLES</span><p><strong>${papers.length} article(s) · ${dimensions.length} dimension(s)</strong></p></section>
    <div class="comparison-table-wrap"><table class="comparison-table"><thead><tr><th scope="col">Article</th>${header}</tr></thead><tbody>${body || `<tr><td colspan="${dimensions.length + 1}">Aucun article comparable.</td></tr>`}</tbody></table></div>
    <p class="comparison-policy">${escapeHtml(result && result.unknown_policy || "Les dimensions non documentées restent inconnues.")}</p>`;
}

function finalizeComparisonMessage(wrap, result) {
  wrap.classList.remove("is-running");
  wrap.classList.add("is-complete", "research-message");
  const avatar = wrap.querySelector(".msg-avatar");
  avatar.className = "msg-avatar";
  avatar.innerHTML = mascotSprite(32, { watch: true });
  const stateLabel = wrap.querySelector(".lab-notebook-state");
  if (stateLabel) stateLabel.textContent = "ARCHIVÉ";
  const content = wrap.querySelector(".msg-content");
  content.innerHTML = comparisonMarkup(result);
  appendResearchTrace(wrap, "completed", "Comparaison terminée sans compléter les dimensions manquantes.");
  const trace = wrap.querySelector(".lab-research-trace");
  if (trace) trace.open = false;
  const meta = wrap.querySelector(".msg-meta");
  wrap._messageText = content.innerText.trim();
  meta.innerHTML = `<span class="status-pill ok">Comparaison prête</span><span>${(result.papers || []).length} article(s) · valeurs inconnues conservées</span>${messageActionsMarkup()}`;
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

async function compareSelectedPapers() {
  const paperIds = [...state.selectedPaperIds];
  if (paperIds.length < 2) return;
  if (compareButton) compareButton.disabled = true;
  const wrap = addAssistantMessage({ title: "Comparaison scientifique", watch: true });
  appendResearchTrace(wrap, "planned", `${paperIds.length} références sélectionnées ; aucune métrique absente ne sera inventée.`);
  updateThinkingStatus(wrap, "Lecture des métadonnées locales…");
  try {
    const response = await fetch("/api/v1/research/compare", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paper_ids: paperIds }),
    });
    const result = await response.json();
    if (!response.ok || result.error) throw new Error(result.error || `HTTP ${response.status}`);
    for (const paper of result.papers || []) {
      appendResearchTrace(wrap, "source", paper.title || paper.paper_id, {});
    }
    finalizeComparisonMessage(wrap, result);
  } catch (error) {
    errorInAssistantMessage(wrap, `Comparaison indisponible : ${error.message || error}`);
  } finally {
    if (compareButton) compareButton.disabled = state.selectedPaperIds.size < 2;
  }
}

async function exportScientificBibliography() {
  const format = String(libraryExportFormat && libraryExportFormat.value || "bibtex");
  try {
    const response = await fetch(`/api/v1/library/bibliography/export?format=${encodeURIComponent(format)}`);
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.error || `HTTP ${response.status}`);
    }
    const blob = await response.blob();
    const extension = format === "csl-json" ? "json" : format === "bibtex" ? "bib" : "ris";
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `3loop-bibliography.${extension}`;
    link.click();
    URL.revokeObjectURL(url);
  } catch (error) {
    if (scientificSearchStatus) scientificSearchStatus.textContent = `Export impossible : ${error.message || error}`;
  }
}

if (scientificSearchButton) scientificSearchButton.addEventListener("click", runScientificSearch);
if (scientificSearchInput) scientificSearchInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    runScientificSearch();
  }
});
if (librarySearchInput) librarySearchInput.addEventListener("input", renderScientificLibrary);
if (libraryListEl) libraryListEl.addEventListener("click", (event) => {
  const button = event.target.closest("[data-delete-paper]");
  if (!button) return;
  event.preventDefault();
  event.stopPropagation();
  deleteScientificPaper(button.dataset.deletePaper, button);
});
if (libraryListEl) libraryListEl.addEventListener("change", (event) => {
  const checkbox = event.target.closest("[data-paper-id]");
  if (!checkbox) return;
  const id = String(checkbox.dataset.paperId || "");
  if (checkbox.checked) state.selectedPaperIds.add(id);
  else state.selectedPaperIds.delete(id);
  renderScientificLibrary();
});
if (scientificResultsEl) scientificResultsEl.addEventListener("click", (event) => {
  const button = event.target.closest("[data-save-scientific-key]");
  if (button) saveScientificResult(button.dataset.saveScientificKey);
});
if (messagesEl) messagesEl.addEventListener("click", (event) => {
  const button = event.target.closest("[data-save-scientific-key]");
  if (button) saveScientificResult(button.dataset.saveScientificKey);
});
if (compareButton) compareButton.addEventListener("click", compareSelectedPapers);
if (libraryExportButton) libraryExportButton.addEventListener("click", exportScientificBibliography);
if (notebookListEl) notebookListEl.addEventListener("click", (event) => {
  const button = event.target.closest("[data-delete-note]");
  if (!button) return;
  event.preventDefault();
  deleteNotebookNote(button.dataset.deleteNote, button);
});
if (notebookSaveButton) notebookSaveButton.addEventListener("click", saveNotebookNote);
if (notebookNoteBody) notebookNoteBody.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter") saveNotebookNote();
});


// The trace stays open while a search is active, but it can be folded away
// immediately without interrupting the running request.
messagesEl.addEventListener("click", (event) => {
  const closeButton = event.target.closest(".lab-trace-close");
  if (!closeButton) return;
  const trace = closeButton.closest(".lab-research-trace");
  if (!trace) return;
  trace.open = false;
  closeButton.blur();
});

// ---------------------------------------------------------------- Gmail read-only
//
// OAuth is completed by the local Python server. This panel deliberately
// receives only message metadata and the model's analysis; Gmail tokens and
// raw message bodies never enter the browser.
const gmailConnectButton = el("gmail-connect");
const gmailRefreshButton = el("gmail-refresh");
const gmailConfigForm = el("gmail-config-form");
const gmailOAuthHelpButton = el("gmail-oauth-help");
const gmailClientIdInput = el("gmail-client-id");
const gmailClientSecretInput = el("gmail-client-secret");
const gmailSaveConfigButton = el("gmail-save-config");
const gmailStatusEl = el("gmail-status");
const gmailListEl = el("gmail-list");
let gmailAuthPoll = null;
let gmailLoadedFor = "";
let gmailAnalysisInFlight = null;

function gmailCategoryLabel(value) {
  return {
    "publicité": "publicité",
    travail: "travail",
    autre: "autre",
  }[String(value || "autre")] || "autre";
}

function gmailCategoryClass(value) {
  return String(value || "autre") === "publicité" ? "publicite" : String(value || "autre");
}

function renderGmailMessages(items) {
  const messages = Array.isArray(items) ? items : [];
  if (!messages.length) {
    gmailListEl.className = "gmail-list muted";
    gmailListEl.textContent = "Aucun email trouvé pour cette recherche.";
    return;
  }
  gmailListEl.className = "gmail-list";
  gmailListEl.innerHTML = messages.map((message) => {
    const category = String(message.category || "autre");
    const categoryClass = gmailCategoryClass(category);
    const sender = message.sender || message.sender_email || "Expéditeur inconnu";
    const date = message.date ? new Date(message.date).toLocaleString("fr-FR") : "date inconnue";
    return `<article class="gmail-message-card">
      <div class="gmail-message-heading">
        <strong>${escapeHtml(message.subject || "(sans objet)")}</strong>
        <span class="gmail-classification is-${escapeHtml(categoryClass)}">${escapeHtml(gmailCategoryLabel(category))}</span>
      </div>
      <div class="gmail-message-meta">${escapeHtml(sender)}${message.sender_email ? ` · ${escapeHtml(message.sender_email)}` : ""} · ${escapeHtml(date)}</div>
      <p class="gmail-message-summary">${escapeHtml(message.summary || message.snippet || "Résumé indisponible.")}</p>
    </article>`;
  }).join("");
}

function setGmailStatus(text, kind = "") {
  if (!gmailStatusEl) return;
  gmailStatusEl.textContent = text || "";
  gmailStatusEl.className = `panel-hint${kind ? ` ${kind}` : ""}`;
}

async function loadGmailStatus() {
  if (!gmailStatusEl) return;
  try {
    const response = await fetch("/api/v1/gmail/status");
    const status = await response.json();
    if (!response.ok || status.error) throw new Error(status.error || `HTTP ${response.status}`);
    if (!status.configured) {
      gmailConfigForm.hidden = false;
      gmailConnectButton.hidden = true;
      gmailConnectButton.disabled = false;
      gmailRefreshButton.hidden = true;
      setGmailStatus(
        "Colle tes identifiants OAuth Google ci-dessus pour commencer.",
        "warn"
      );
      return status;
    }
    gmailConfigForm.hidden = true;
    gmailConnectButton.hidden = status.connected;
    gmailConnectButton.disabled = false;
    gmailConnectButton.textContent = "Connecter Gmail";
    gmailRefreshButton.hidden = !status.connected;
    if (!status.connected) {
      gmailLoadedFor = "";
    }
    setGmailStatus(
      status.connected
        ? `Compte connecté : ${status.email || "Gmail"}. Scope : lecture seule.`
        : "Client Google prêt. Connecte un compte pour lire ses emails.",
      status.connected ? "ok" : ""
    );
    if (status.connected && state.config) {
      const accountKey = String(status.email || "connected");
      if (gmailLoadedFor !== accountKey) {
        gmailLoadedFor = accountKey;
        void loadGmailMessages();
      }
    }
    return status;
  } catch (error) {
    setGmailStatus(`Gmail indisponible : ${error.message || error}`, "warn");
    return null;
  }
}

async function openGmailOAuthHelp() {
  const url = "https://console.cloud.google.com/apis/credentials";
  const opened = window.open(url, "_blank", "noopener,noreferrer");
  if (!opened) {
    try {
      await fetch("/api/open-url", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url }),
      });
    } catch {
      /* The inline instructions remain available if the browser blocks it. */
    }
  }
  setGmailStatus("Google Cloud est ouvert : reviens ici après avoir créé ton client OAuth.");
}

async function saveGmailConfigAndConnect() {
  const clientId = String(gmailClientIdInput?.value || "").trim();
  const clientSecret = String(gmailClientSecretInput?.value || "").trim();
  if (!clientId) {
    setGmailStatus("Le Client ID Google est obligatoire.", "warn");
    gmailClientIdInput?.focus();
    return;
  }
  gmailSaveConfigButton.disabled = true;
  setGmailStatus("Enregistrement local des identifiants OAuth…");
  try {
    const response = await fetch("/api/v1/gmail/configure", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ client_id: clientId, client_secret: clientSecret }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || payload.error) throw new Error(payload.error || `HTTP ${response.status}`);
    // Do not retain the secret in the DOM longer than needed for this save.
    if (gmailClientSecretInput) gmailClientSecretInput.value = "";
    await loadGmailStatus();
    await openGmailAuthorization();
  } catch (error) {
    setGmailStatus(`Configuration Gmail impossible : ${error.message || error}`, "warn");
  } finally {
    gmailSaveConfigButton.disabled = false;
  }
}

async function openGmailAuthorization() {
  gmailConnectButton.disabled = true;
  setGmailStatus("Préparation de la connexion Google…");
  try {
    const response = await fetch("/api/v1/gmail/connect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || payload.error) throw new Error(payload.error || `HTTP ${response.status}`);
    const authUrl = String(payload.authorization_url || "");
    if (!authUrl) throw new Error("URL de connexion Google absente.");
    const opened = window.open(authUrl, "_blank", "noopener,noreferrer");
    if (!opened) {
      await fetch("/api/open-url", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: authUrl }),
      });
    }
    setGmailStatus("Autorise 3loop dans l’onglet Google ouvert…");
    clearInterval(gmailAuthPoll);
    gmailAuthPoll = window.setInterval(async () => {
      const status = await loadGmailStatus();
      if (status?.connected) {
        clearInterval(gmailAuthPoll);
        gmailAuthPoll = null;
      }
    }, 1500);
    window.setTimeout(() => {
      clearInterval(gmailAuthPoll);
      gmailAuthPoll = null;
    }, 120000);
  } catch (error) {
    setGmailStatus(`Connexion Gmail impossible : ${error.message || error}`, "warn");
    gmailConnectButton.disabled = false;
  }
}

function loadGmailMessages() {
  if (gmailAnalysisInFlight) return gmailAnalysisInFlight;

  const request = (async () => {
    gmailRefreshButton.disabled = true;
    setGmailStatus("Lecture et analyse des emails…");
    gmailListEl.className = "gmail-list muted";
    gmailListEl.textContent = "Analyse en cours…";
    try {
      const response = await fetch("/api/v1/gmail/analyze", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          limit: 25,
          query: "in:anywhere -label:SPAM -category:promotions newer_than:1d",
          // The server enforces the dedicated fast Qwen Flash profile. These
          // fields keep older servers on the same intended behavior too.
          backend: "ollama",
          model: "qwen3:1.7b-flash",
          thinking: false,
          api_key: apiKeyInput.value,
        }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || payload.error) throw new Error(payload.error || `HTTP ${response.status}`);
      renderGmailMessages(payload.items);
      const mode = payload.analysis_mode === "model" ? "résumés par Qwen Flash" : "résumés heuristiques";
      const engine = payload.analysis_model || "Qwen3 1.7B Flash";
      const warning = payload.analysis_warning ? ` · ${payload.analysis_warning}` : "";
      setGmailStatus(`${payload.count || 0} email(s) lu(s) sur les dernières 24 heures, ${mode} via ${engine}${warning}`, payload.analysis_warning ? "warn" : "ok");
    } catch (error) {
      gmailListEl.className = "gmail-list muted";
      gmailListEl.textContent = "Aucun email chargé.";
      setGmailStatus(`Lecture Gmail impossible : ${error.message || error}`, "warn");
    } finally {
      gmailRefreshButton.disabled = false;
    }
  })();

  gmailAnalysisInFlight = request;
  request.then(
    () => { if (gmailAnalysisInFlight === request) gmailAnalysisInFlight = null; },
    () => { if (gmailAnalysisInFlight === request) gmailAnalysisInFlight = null; },
  );
  return request;
}

if (gmailOAuthHelpButton) gmailOAuthHelpButton.addEventListener("click", openGmailOAuthHelp);
if (gmailSaveConfigButton) gmailSaveConfigButton.addEventListener("click", saveGmailConfigAndConnect);
if (gmailConnectButton) gmailConnectButton.addEventListener("click", openGmailAuthorization);
if (gmailRefreshButton) gmailRefreshButton.addEventListener("click", loadGmailMessages);
loadGmailStatus();
