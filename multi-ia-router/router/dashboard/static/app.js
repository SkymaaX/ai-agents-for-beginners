// Tableau de bord du routeur multi-IA. JavaScript simple, sans dépendance.
// Tout texte venant des modèles passe par esc() ; le Markdown est rendu (et neutralisé) côté serveur.
"use strict";

const S = {
  meta: null, projects: [], id: null, detail: null, tab: "activity",
  part: null, version: {}, deliverable: null, events: [], source: null,
  current: null, cache: {}, signatures: {}, refreshTimer: null,
};

const STATUS = { new: "nouveau", running: "en cours", paused: "en pause", failed: "échec", done: "terminé", stale: "interrompu" };
const PART_STATUS = { pending: "à produire", produced: "en relecture", to_revise: "à réviser", accepted: "acceptée", arbitrated: "tranchée par le chef", removed: "retirée" };
const TYPE = { text: "Texte", code: "Code", files: "Fichiers", blender: "Blender", image: "Image", video: "Vidéo", audio: "Audio" };
const ROLE = {
  "chef.reformulation": "reformule la demande", "repartiteur.plan": "découpe la demande en plan",
  "repartiteur.modification": "planifie la modification", "chercheur": "fait la recherche",
  "producteur": "produit", "auteur.revision": "révise", "critique": "critique",
  "chef.arbitrage": "arbitre le désaccord sur", "assembleur": "assemble le livrable",
  "chef.validation": "compare le livrable à la demande",
};

const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (n) => Number(n || 0).toLocaleString("fr-FR");
const clock = (ts) => (ts || "").slice(11, 19);
const list = (items) => (items && items.length ? `<ul>${items.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>` : `<p class="muted small">—</p>`);

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let message = `Erreur ${response.status}`;
    try { message = (await response.json()).detail || message; } catch (_) { /* réponse non JSON */ }
    throw new Error(message);
  }
  return response.json();
}
const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });

function family(agent) {
  const member = S.meta && S.meta.team.find((a) => a.name === agent);
  return member ? `fam-${member.family}` : "";
}
function partTitle(id) {
  const part = S.detail && S.detail.parts.find((p) => p.id === id);
  return part ? part.title : id;
}

// ───────────────────────────────────────────────────────────── chargement
async function init() {
  S.meta = await api("/api/meta");
  const mode = $("mode");
  mode.textContent = S.meta.mock ? "Mode simulation" : "Mode réel";
  mode.classList.toggle("mock", S.meta.mock);
  if (S.meta.mock) { $("mock").checked = true; $("mock").disabled = true; }
  $("launch").addEventListener("click", launch);
  $("call-close").addEventListener("click", () => $("call-dialog").close());
  await loadProjects();
  loadTools();
  setInterval(loadTools, 15000);
  const wanted = location.hash.slice(1);
  if (wanted && S.projects.some((p) => p.id === wanted)) openProject(wanted);
  setInterval(loadProjects, 5000);
}

async function loadProjects() {
  try { S.projects = await api("/api/projects"); } catch (_) { return; }
  const box = $("projects");
  if (!S.projects.length) { box.innerHTML = `<li class="empty">Aucun projet pour l'instant.</li>`; return; }
  box.innerHTML = S.projects.map((p) => {
    const status = p.stale ? "stale" : p.status;
    const sub = [STATUS[status] || status, p.mock ? "simulation" : "", p.modifications ? `${p.modifications} modif.` : "", (p.created || "").slice(0, 16).replace("T", " ")].filter(Boolean).join(" · ");
    return `<li><button data-id="${esc(p.id)}" class="${p.id === S.id ? "active" : ""}">
      <span class="dot ${esc(status)}"></span>
      <span><span class="title">${esc(p.title)}</span><span class="sub">${esc(sub)}</span></span></button></li>`;
  }).join("");
  box.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => openProject(b.dataset.id)));
}

async function loadTools() {
  let tools;
  try { tools = await api("/api/tools"); } catch (_) { return; }
  const box = $("tools");
  if (!box) return;
  box.innerHTML = tools.map((t) => {
    const dot = t.state === "en marche" ? "on" : t.state === "installé" ? "half" : "off";
    const hint = t.state === "en marche" ? `remplace ${t.replaces}` : `remplace ${t.replaces} · ${t.state}`;
    const open = t.up && t.url ? `<a class="open" href="${esc(t.url)}" target="_blank" rel="noopener">Ouvrir</a>` : `<span class="state"></span>`;
    return `<li title="${esc(t.summary)} — ${esc(t.command)}"><span class="dot ${dot}"></span><span class="name">${esc(t.name)}<span class="sub">${esc(hint)}</span></span>${open}</li>`;
  }).join("");
}

async function launch() {
  const request = $("request").value.trim();
  const error = $("launch-error");
  error.hidden = true;
  if (!request) { error.textContent = "Écris d'abord ta demande."; error.hidden = false; return; }
  $("launch").disabled = true;
  try {
    const created = await post("/api/projects", { request, mock: $("mock").checked });
    $("request").value = "";
    await loadProjects();
    openProject(created.id);
  } catch (exc) {
    error.textContent = exc.message; error.hidden = false;
  } finally { $("launch").disabled = false; }
}

async function openProject(id) {
  if (S.source) { S.source.close(); S.source = null; }
  Object.assign(S, { id, detail: null, part: null, version: {}, deliverable: null, events: [], current: null, cache: {}, signatures: {}, tab: "activity" });
  location.hash = id;
  $("welcome").hidden = true;
  const box = $("project");
  box.hidden = false;
  box.innerHTML = `
    <section class="card" id="head"></section>
    <section class="card"><h2>Pipeline</h2><div id="stepper" class="stepper"></div><div id="now" class="now"></div></section>
    <section class="card"><h2>Équipe</h2><div id="team" class="team"></div></section>
    <section class="card"><nav id="tabs" class="tabs"></nav><div id="tab"></div></section>`;
  await refresh();
  loadProjects();
  S.source = new EventSource(`/api/projects/${encodeURIComponent(id)}/events?since=0`);
  S.source.onmessage = (message) => {
    if (S.id !== id) return;
    const event = JSON.parse(message.data);
    S.events.push(event);
    if (event.type === "agent_start") S.current = event;
    if (["agent_end", "agent_error", "stage_end", "pipeline_done", "pipeline_failed", "pipeline_paused"].includes(event.type)) S.current = null;
    if (["pipeline_start", "pipeline_done", "pipeline_failed", "pipeline_paused", "brief"].includes(event.type)) loadProjects();
    renderNow(); renderTeam();
    if (S.tab === "activity") renderFeed();
    scheduleRefresh();
  };
}

function scheduleRefresh() {
  if (S.refreshTimer) return;
  S.refreshTimer = setTimeout(async () => { S.refreshTimer = null; await refresh(); }, 600);
}

async function refresh() {
  if (!S.id) return;
  const id = S.id;
  let detail;
  try { detail = await api(`/api/projects/${encodeURIComponent(id)}`); } catch (_) { return; }
  if (S.id !== id) return;
  S.detail = detail;
  renderHead(); renderStepper(); renderNow(); renderTeam(); renderTabs(); renderTab();
}

// ───────────────────────────────────────────────────────────── en-tête, pipeline, équipe
function renderHead() {
  const d = S.detail, st = d.state;
  const status = d.stale ? "stale" : st.status;
  const canResume = ["paused", "failed"].includes(st.status) || d.stale;
  let banners = "";
  if (st.error) banners += `<div class="banner ${st.status === "paused" ? "warn" : "bad"}">${esc(st.error)}</div>`;
  if (d.stale) banners += `<div class="banner warn">Ce projet a été interrompu en cours de route. L'état est sauvegardé : tu peux le reprendre là où il s'est arrêté.</div>`;
  if (st.reserves && st.reserves.length) banners += `<div class="banner warn"><b>Livré avec réserves :</b> ${st.reserves.map(esc).join(" ; ")}</div>`;
  $("head").innerHTML = `
    <div class="project-head">
      <div>
        <h2>${esc(st.title || "Projet en cours de reformulation…")}</h2>
        <p class="request">${esc(st.request)}</p>
      </div>
      <div class="head-actions">
        ${st.mock ? `<span class="chip">simulation</span>` : ""}
        <span class="chip ${esc(status)}">${esc(STATUS[status] || status)}</span>
        ${canResume ? `<button class="ghost" id="resume">Reprendre</button>` : ""}
      </div>
    </div>${banners}`;
  const resume = $("resume");
  if (resume) resume.addEventListener("click", async () => {
    resume.disabled = true;
    try { await post(`/api/projects/${encodeURIComponent(S.id)}/resume`); await refresh(); } catch (exc) { alert(exc.message); }
  });
}

function renderStepper() {
  $("stepper").innerHTML = S.detail.stages.map((stage, index) => {
    const mark = stage.status === "done" ? "✓" : stage.status === "error" ? "!" : index + 1;
    return `<div class="step ${esc(stage.status)}" title="${esc(stage.detail || "")}"><span class="bullet">${mark}</span><span class="label">${esc(stage.label)}</span></div>`;
  }).join("");
}

function renderNow() {
  const box = $("now");
  if (!box || !S.detail) return;
  const st = S.detail.state;
  const current = S.current || (S.detail.running && st.current.agent ? st.current : null);
  if (S.detail.running && current && current.agent) {
    const what = ROLE[current.role] || current.role || "travaille";
    const part = current.part_id ? ` la partie <b>${esc(current.part_id)}</b> — ${esc(partTitle(current.part_id))}` : "";
    const retry = current.attempt > 1 ? ` <span class="muted">(essai ${current.attempt})</span>` : "";
    box.innerHTML = `<span class="dot running"></span><span><span class="who ${family(current.agent)}">${esc(current.agent)}</span> ${esc(what)}${part}${retry}</span>`;
  } else if (S.detail.running) {
    const stage = S.detail.stages.find((s) => s.status === "running");
    box.innerHTML = `<span class="dot running"></span><span>${stage ? `Étape en cours : ${esc(stage.label)}` : "Démarrage…"}</span>`;
  } else {
    const text = { done: "Pipeline terminé. Le livrable est dans l'onglet « Livrable ».", paused: "Pipeline en pause.", failed: "Pipeline arrêté sur une erreur." }[st.status] || "Pipeline à l'arrêt.";
    box.innerHTML = `<span class="dot ${S.detail.stale ? "stale" : esc(st.status)}"></span><span>${text}</span>`;
  }
}

function renderTeam() {
  const box = $("team");
  if (!box || !S.meta || !S.detail) return;
  const active = S.detail.running && (S.current ? S.current.agent : S.detail.state.current.agent);
  const role = S.current ? S.current.role : S.detail.state.current.role;
  const stats = S.detail.totals.by_agent;
  box.innerHTML = S.meta.team.map((a) => {
    const s = stats[a.name];
    const doing = active === a.name ? (ROLE[role] || "travaille") + "…" : "";
    return `<div class="agent fam-${esc(a.family)} ${a.enabled ? "" : "off"} ${active === a.name ? "active" : ""}" title="${esc(a.description)}">
      <div class="name"><span>${esc(a.name)}</span>${a.enabled ? "" : `<span class="chip">désactivé</span>`}</div>
      <div class="roles">${esc(a.roles.join(" · ") || "—")}</div>
      <div class="doing">${esc(doing)}</div>
      <div class="stat">${s ? `${s.calls} appel${s.calls > 1 ? "s" : ""} · ${num(s.tokens_in + s.tokens_out)} tokens · ${Math.round(s.duration_s)} s` : "pas encore sollicité"}</div>
    </div>`;
  }).join("");
}

// ───────────────────────────────────────────────────────────── onglets
function renderTabs() {
  const d = S.detail;
  const tabs = [
    ["activity", "Activité", ""], ["parts", "Plan et parties", d.parts.length || ""], ["research", "Recherche", ""],
    ["deliverable", "Livrable", d.deliverables.length ? `v${d.deliverables.length}` : ""], ["log", "Journal", d.totals.calls || ""],
  ];
  $("tabs").innerHTML = tabs.map(([id, label, count]) => `<button data-tab="${id}" class="${S.tab === id ? "active" : ""}">${label}${count ? `<span class="count">${esc(count)}</span>` : ""}</button>`).join("");
  $("tabs").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => { S.tab = b.dataset.tab; S.signatures = {}; renderTabs(); renderTab(); }));
}

// Ne redessine un onglet que si ses données ont changé : la lecture n'est pas interrompue par le direct.
function changed(key, data) {
  const signature = JSON.stringify(data);
  if (S.signatures[key] === signature) return false;
  S.signatures[key] = signature;
  return true;
}

function renderTab() {
  ({ activity: tabActivity, parts: tabParts, research: tabResearch, deliverable: tabDeliverable, log: tabLog }[S.tab])();
}

function tabActivity() {
  const brief = S.detail.brief;
  if (changed("activity", brief)) {
    $("tab").innerHTML = `<div class="two">
      <div class="kv">${brief ? `
        <div><h4>Objectif reformulé par le chef</h4><p>${esc(brief.objective)}</p></div>
        <div><h4>Livrables attendus</h4>${list(brief.deliverables)}</div>
        <div><h4>Contraintes</h4>${list(brief.constraints)}</div>
        <div><h4>Critères d'acceptation</h4>${list(brief.acceptance_criteria)}</div>
        ${brief.open_questions && brief.open_questions.length ? `<div><h4>Questions ouvertes et hypothèses</h4>${list(brief.open_questions)}</div>` : ""}`
      : `<p class="muted">Le chef reformule la demande…</p>`}</div>
      <div><div class="kv"><h4>En direct</h4></div><ul id="feed" class="feed"></ul></div></div>`;
  }
  renderFeed();
}

function describe(e) {
  const who = e.agent ? `<span class="who ${family(e.agent)}">${esc(e.agent)}</span>` : "";
  const part = e.part_id ? ` — partie ${esc(e.part_id)}` : "";
  switch (e.type) {
    case "pipeline_start": return ["Pipeline démarré" + (e.mock ? " (simulation)" : ""), "soft"];
    case "stage_start": return [esc((S.meta.stage_labels[e.stage] || e.stage)), "stage"];
    case "agent_start": return [`${who} ${esc(ROLE[e.role] || e.role)}${part}${e.attempt > 1 ? ` (essai ${e.attempt})` : ""}${e.context_cut ? " · contexte réduit" : ""}`, ""];
    case "agent_end": return [`${who} a terminé en ${Number(e.duration || 0).toFixed(1)} s · ${num((e.tokens_in || 0) + (e.tokens_out || 0))} tokens`, "soft"];
    case "agent_error": return [`${who} : ${esc(e.error)}${e.final ? "" : " — nouvel essai"}`, "bad"];
    case "json_invalid": return [`${who} : JSON invalide, renvoyé pour correction`, "bad"];
    case "brief": return [`Brief établi : ${esc(e.title)}`, "good"];
    case "plan": return [`Plan reçu : ${esc(e.parts)} partie(s) pour ${esc((e.agents || []).join(", "))}`, "good"];
    case "research_query": return [`Recherche ${e.source === "vault" ? "dans la vault" : "web"} : ${esc(e.query)}`, "soft"];
    case "research_error": return [`Recherche échouée (${esc(e.source)}) : ${esc(e.error)}`, "bad"];
    case "research": return [`Dossier de recherche prêt : ${esc(e.sources)} source(s)`, "good"];
    case "part_version": return [`Partie ${esc(e.part_id)} : version ${esc(e.version)} ${e.revised ? "révisée" : "produite"} par ${esc(e.agent)}`, ""];
    case "part_update":
      if (e.status === "accepted") return [`Partie ${esc(e.part_id)} acceptée (version ${esc(e.version)}, tour ${esc(e.round)})`, "good"];
      if (e.status === "revise") return [`Partie ${esc(e.part_id)} à revoir : ${esc(e.problems)} problème(s) signalé(s) au tour ${esc(e.round)}`, ""];
      if (e.status === "arbitrated") return [`Partie ${esc(e.part_id)} : le chef tranche pour la version ${esc(e.version)}`, "good"];
      return null;
    case "blender_run": return [`Blender exécute le script de la partie ${esc(e.part_id)} (tentative ${esc(e.attempt)})`, "soft"];
    case "blender_result": return [`Blender : ${e.ok ? "exécution réussie" : "échec, erreur renvoyée à l'auteur"}`, e.ok ? "good" : "bad"];
    case "gpu_switch": return [`Carte graphique : ${esc(e.unload)} déchargé pour laisser la place à ${esc(e.load)}`, "soft"];
    case "permission_denied": return [`${who} : outil(s) refusé(s) — ${esc((e.tools || []).join(", "))}`, "bad"];
    case "assembly": return [`Livrable assemblé (version ${esc(e.version)})`, "good"];
    case "verdict":
      if (e.ok) return [`Le chef valide le livrable (version ${esc(e.version)})`, "good"];
      return [e.final ? `Le chef relève encore ${esc(e.gaps)} manque(s) : livraison avec réserves` : `Le chef renvoie le livrable : ${esc(e.gaps)} manque(s) à corriger`, "bad"];
    case "replan": return [`Modification : parties retouchées [${esc((e.changed || []).join(", "))}], ajoutées [${esc((e.added || []).join(", "))}], retirées [${esc((e.removed || []).join(", "))}]`, "good"];
    case "delivered": return [`Livrable enregistré${e.vault ? " et copié dans la vault" : ""}`, "good"];
    case "pipeline_done": return ["Projet terminé", "good"];
    case "pipeline_paused": return [`En pause : ${esc(e.error)}`, "bad"];
    case "pipeline_failed": return [`Arrêt : ${esc(e.error)}`, "bad"];
    default: return null;
  }
}

function renderFeed() {
  const feed = $("feed");
  if (!feed) return;
  const pinned = feed.scrollTop + feed.clientHeight >= feed.scrollHeight - 30;
  feed.innerHTML = S.events.slice(-400).map((e) => {
    const line = describe(e);
    return line ? `<li><time>${esc(clock(e.ts))}</time><span class="${line[1]}">${line[0]}</span></li>` : "";
  }).join("") || `<li><time></time><span class="muted">En attente d'activité…</span></li>`;
  if (pinned) feed.scrollTop = feed.scrollHeight;
}

// ───────────────────────────────────────────────────────────── plan et parties
function tabParts() {
  const d = S.detail;
  if (!d.parts.length) { if (changed("parts", "vide")) $("tab").innerHTML = `<p class="muted">Le répartiteur n'a pas encore rendu son plan.</p>`; return; }
  if (!S.part || !d.parts.some((p) => p.id === S.part)) S.part = d.parts[0].id;
  if (!changed("parts", [d.plan, d.parts, S.part, S.version[S.part]])) return;
  const cards = d.parts.map((p) => `
    <button class="part ${p.id === S.part ? "active" : ""}" data-part="${esc(p.id)}">
      <span class="top"><span class="pid">${esc(p.id)} · ${esc(TYPE[p.type] || p.type)}</span><span class="chip ${esc(p.status)}">${esc(PART_STATUS[p.status] || p.status)}</span></span>
      <span class="ptitle">${esc(p.title)}</span>
      <span class="meta"><span class="who ${family(p.agent)}">${esc(p.agent)}</span> produit · <span class="who ${family(p.critic)}">${esc(p.critic)}</span> critique${p.version ? ` · v${p.accepted_version || p.version}` : ""}</span>
    </button>`).join("");
  $("tab").innerHTML = `
    ${d.plan && d.plan.summary ? `<p class="small muted" style="margin-bottom:12px">${esc(d.plan.summary)}</p>` : ""}
    <div class="parts">${cards}</div><div id="part-detail" class="detail"></div>`;
  $("tab").querySelectorAll(".part").forEach((b) => b.addEventListener("click", () => { S.part = b.dataset.part; S.signatures.parts = null; tabParts(); }));
  renderPart();
}

async function renderPart() {
  const part = S.detail.parts.find((p) => p.id === S.part);
  const box = $("part-detail");
  if (!part || !box) return;
  const brief = `
    <div class="two">
      <div class="box"><h4>Prompt rédigé par le répartiteur</h4><p class="small" style="white-space:pre-wrap">${esc(part.prompt)}</p></div>
      <div class="box"><h4>Critères de réussite</h4>${list(part.success_criteria)}
        ${part.depends_on && part.depends_on.length ? `<p class="small muted">Dépend de : ${esc(part.depends_on.join(", "))}</p>` : ""}</div>
    </div>`;
  if (!part.versions.length) { box.innerHTML = brief + `<p class="muted">Pas encore de version produite.</p>`; return; }
  const chosen = S.version[part.id] && part.versions.some((v) => v.n === S.version[part.id]) ? S.version[part.id] : (part.accepted_version || part.versions[part.versions.length - 1].n);
  const version = part.versions.find((v) => v.n === chosen);
  const pills = part.versions.map((v) => `<button data-v="${v.n}" class="${v.n === chosen ? "active" : ""} ${v.n === part.accepted_version ? "kept" : ""}">Version ${v.n}</button>`).join("");
  const key = `${S.id}:${part.id}:${chosen}`;
  if (!S.cache[key]) {
    try { S.cache[key] = await api(`/api/projects/${encodeURIComponent(S.id)}/parts/${encodeURIComponent(part.id)}/versions/${chosen}`); } catch (exc) { box.innerHTML = brief + `<p class="error">${esc(exc.message)}</p>`; return; }
    if (S.part !== part.id) return;
  }
  const content = S.cache[key];
  const files = version.files.length ? `<div class="box"><h4>Fichiers de cette version</h4><div class="files">${version.files.map((f) => f.audio
    ? `<span class="audiofile"><audio controls preload="none" src="${esc(f.url)}"></audio><span class="muted small">${esc(f.name)}</span></span>`
    : f.image
    ? `<a class="thumb" href="${esc(f.url)}" target="_blank" rel="noopener"><img src="${esc(f.url)}" alt="${esc(f.name)}"></a>`
    : `<a class="file" href="${esc(f.url)}" target="_blank" rel="noopener">${esc(f.name)} <span class="muted">(${num(f.size)} o)</span></a>`).join("")}</div></div>` : "";
  box.innerHTML = `${brief}
    <div class="versions">${pills}</div>
    <div class="two">
      <div class="box"><h4><span>Contenu — version ${chosen}, par <span class="who ${family(part.agent)}">${esc(part.agent)}</span></span></h4><div class="prose">${content.html}</div></div>
      <div style="display:grid;gap:14px;align-content:start">
        ${critiqueBox(version.critique)}
        ${content.response_html ? `<div class="box"><h4>Réponse de l'auteur à la critique précédente</h4><div class="prose">${content.response_html}</div></div>` : ""}
        ${part.arbitration ? `<div class="box"><h4>Arbitrage du chef <span class="chip arbitrated">version ${esc(part.arbitration.chosen_version)} retenue</span></h4>
          <p class="small">${esc(part.arbitration.rationale)}</p>${part.arbitration.reserves && part.arbitration.reserves.length ? `<p class="small muted" style="margin-top:6px">Réserves :</p>${list(part.arbitration.reserves)}` : ""}</div>` : ""}
        ${files}
      </div>
    </div>`;
  box.querySelectorAll(".versions button").forEach((b) => b.addEventListener("click", () => { S.version[part.id] = Number(b.dataset.v); S.signatures.parts = null; tabParts(); }));
}

function critiqueBox(c) {
  if (!c) return `<div class="box"><h4>Critique</h4><p class="muted small">Pas encore relue.</p></div>`;
  const rows = Object.entries(S.meta.grid).map(([key, label]) => {
    const value = c.scores[key];
    const level = value < 3 ? "low" : value < S.meta.review.min_score ? "mid" : "";
    return `<div class="score"><span>${esc(label)}</span><span class="bar"><i class="${level}" style="width:${value * 20}%"></i></span><b>${esc(value)}/5</b></div>`;
  }).join("");
  const problems = c.problems.length ? `<ul class="problems">${c.problems.map((p) => `<li>
    <span class="chip ${esc(p.severity)}">${esc(p.severity)}</span> <span class="muted small">${esc(S.meta.grid[p.critere] || p.critere)}</span>
    <div>${esc(p.detail)}</div><div class="fix">Correction proposée : ${esc(p.correction)}</div></li>`).join("")}</ul>` : `<p class="small muted">Aucun problème signalé.</p>`;
  return `<div class="box"><h4><span>Critique par <span class="who ${family(c.critic)}">${esc(c.critic)}</span> — tour ${esc(c.round)}</span>
    <span class="chip ${esc(c.verdict)}">${c.verdict === "accept" ? "acceptée" : "à revoir"}</span></h4>
    <div class="scores">${rows}</div>${c.summary ? `<p class="small" style="margin:10px 0">${esc(c.summary)}</p>` : ""}${problems}</div>`;
}

// ───────────────────────────────────────────────────────────── recherche, livrable, journal
async function tabResearch() {
  const d = S.detail;
  if (!d.has_research) { if (changed("research", "vide")) $("tab").innerHTML = `<p class="muted">La recherche commune n'a pas encore eu lieu.</p>`; return; }
  const stage = d.stages.find((s) => s.id === "research");
  if (!changed("research", [stage && stage.ended, d.modifications.length])) return;
  const id = S.id;
  const data = await api(`/api/projects/${encodeURIComponent(id)}/research`);
  if (S.id !== id || S.tab !== "research") return;
  const sources = data.sources.length ? `<div class="box"><h4>Sources (${data.sources.length})</h4><ul class="small">${data.sources.map((s) => `<li><b>${esc(s.id)}</b> ${s.url
    ? `<a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.title)}</a>` : `${esc(s.title)} <span class="muted">(vault)</span>`}</li>`).join("")}</ul></div>` : "";
  $("tab").innerHTML = `<p class="small muted" style="margin-bottom:12px">Faite une seule fois, partagée à toutes les parties.</p><div class="two"><div class="prose">${data.html}</div><div>${sources}</div></div>`;
}

async function tabDeliverable() {
  const d = S.detail, st = d.state;
  if (!d.deliverables.length) { if (changed("deliverable", "vide")) $("tab").innerHTML = `<p class="muted">Le livrable n'est pas encore assemblé.</p>`; return; }
  const latest = d.deliverables[d.deliverables.length - 1].version;
  const chosen = S.deliverable !== null && (S.deliverable === 0 ? d.final : d.deliverables.some((v) => v.version === S.deliverable)) ? S.deliverable : (d.final && st.status === "done" ? 0 : latest);
  if (!changed("deliverable", [d.deliverables, d.final, st.status, st.deliverable_version, chosen, d.modifications])) return;
  const id = S.id;
  let data;
  try { data = await api(`/api/projects/${encodeURIComponent(id)}/deliverable?version=${chosen}`); } catch (exc) { $("tab").innerHTML = `<p class="error">${esc(exc.message)}</p>`; return; }
  if (S.id !== id || S.tab !== "deliverable") return;
  const pills = d.deliverables.map((v) => `<button data-v="${v.version}" class="${v.version === chosen ? "active" : ""}">Assemblage v${v.version}</button>`).join("")
    + (d.final ? `<button data-v="0" class="${chosen === 0 ? "active" : ""}">Livrable final</button>` : "");
  const entry = d.deliverables.find((v) => v.version === (chosen === 0 ? st.deliverable_version : chosen));
  const verdict = entry && entry.verdict;
  const verdictBox = verdict ? `<div class="box"><h4><span>Contrôle du chef (<span class="who ${family(verdict.validator)}">${esc(verdict.validator)}</span>) — version ${esc(verdict.assembly_version)}</span>
      <span class="chip ${verdict.ok ? "accept" : "revise"}">${verdict.ok ? "conforme à la demande" : "renvoyé en correction"}</span></h4>
      <p class="small">${esc(verdict.summary)}</p>${verdict.gaps.length ? `<ul class="problems" style="margin-top:8px">${verdict.gaps.map((g) => `<li>${g.part_id ? `<span class="chip">${esc(g.part_id)}</span> ` : `<span class="chip">assemblage</span> `}${esc(g.description)}<div class="fix">Correction demandée : ${esc(g.instruction)}</div></li>`).join("")}</ul>` : ""}</div>`
    : `<div class="box"><h4>Contrôle du chef</h4><p class="muted small">Pas encore contrôlé.</p></div>`;
  const history = d.modifications.length ? `<div class="box"><h4>Modifications demandées</h4><ul class="small">${d.modifications.map((m) => `<li><b>${esc(m.n)}.</b> ${esc(m.instruction)} <span class="chip ${m.status === "done" ? "done" : "running"}">${m.status === "done" ? "appliquée" : "en cours"}</span>
      ${m.plan ? `<div class="muted">Parties retouchées : ${esc((m.plan.changes || []).map((c) => c.part_id).join(", ") || "aucune")}${(m.plan.new_parts || []).length ? ` · ajoutées : ${esc(m.plan.new_parts.map((p) => p.id).join(", "))}` : ""}${(m.plan.remove_parts || []).length ? ` · retirées : ${esc(m.plan.remove_parts.join(", "))}` : ""}</div>` : ""}</li>`).join("")}</ul></div>` : "";
  const revise = st.status === "done" ? `<div class="box revise"><h4>Demander une modification</h4>
      <p class="small muted">Seules les parties concernées repassent en production et en critique ; le reste est réutilisé tel quel.</p>
      <textarea id="revise-text" rows="3" placeholder="Ex. : ajoute une section sur le budget, raccourcis l'introduction…"></textarea>
      <button class="primary" id="revise-go">Modifier le projet</button><p id="revise-error" class="error" hidden></p></div>` : "";
  $("tab").innerHTML = `
    <div class="toolbar"><div class="versions">${pills}</div><a class="ghost" href="${esc(data.download)}" download style="text-decoration:none;padding:6px 12px;border:1px solid var(--border);border-radius:8px;color:var(--text)">Télécharger le Markdown</a></div>
    <p class="path">${esc(data.path)}</p>
    <div class="two" style="margin-top:12px"><div class="prose">${data.html}</div><div style="display:grid;gap:14px;align-content:start">${verdictBox}${history}${revise}</div></div>`;
  $("tab").querySelectorAll(".versions button").forEach((b) => b.addEventListener("click", () => { S.deliverable = Number(b.dataset.v); S.signatures.deliverable = null; tabDeliverable(); }));
  const go = $("revise-go");
  if (go) go.addEventListener("click", async () => {
    const error = $("revise-error");
    error.hidden = true; go.disabled = true;
    try {
      await post(`/api/projects/${encodeURIComponent(S.id)}/revise`, { instruction: $("revise-text").value });
      S.deliverable = null; S.tab = "activity"; S.signatures = {};
      await refresh();
    } catch (exc) { error.textContent = exc.message; error.hidden = false; go.disabled = false; }
  });
}

async function tabLog() {
  const d = S.detail;
  if (!changed("log", d.totals)) return;
  const id = S.id;
  const calls = await api(`/api/projects/${encodeURIComponent(id)}/calls`);
  if (S.id !== id || S.tab !== "log") return;
  const t = d.totals;
  const cost = (value) => (value ? `${value.toFixed(3)} $` : "—");
  const agents = Object.entries(t.by_agent).map(([name, a]) => `<tr><td><span class="who ${family(name)}">${esc(name)}</span></td><td class="num">${a.calls}</td><td class="num">${a.errors || ""}</td>
    <td class="num">${num(a.tokens_in)}</td><td class="num">${num(a.tokens_out)}</td><td class="num">${Math.round(a.duration_s)} s</td><td class="num">${cost(a.cost_usd)}</td></tr>`).join("");
  const rows = calls.map((c) => `<tr data-i="${c.index}" class="${c.ok ? "" : "err"}"><td>${esc(clock(c.ts))}</td><td>${esc(S.meta.stage_labels[c.stage] || c.stage || "")}</td>
    <td><span class="who ${family(c.agent)}">${esc(c.agent)}</span></td><td>${esc(c.role)}</td><td>${esc(c.part_id || "")}</td><td class="num">${c.attempt}</td>
    <td class="num">${num(c.tokens_in)}</td><td class="num">${num(c.tokens_out)}${c.tokens_estimated ? " ≈" : ""}</td><td class="num">${Number(c.duration_s || 0).toFixed(1)} s</td><td>${c.ok ? "ok" : esc((c.error || "").slice(0, 60))}</td></tr>`).join("");
  $("tab").innerHTML = `
    <div class="scroll" style="max-height:none;margin-bottom:16px"><table class="log"><thead><tr><th>Agent</th><th class="num">Appels</th><th class="num">Erreurs</th><th class="num">Tokens entrée</th><th class="num">Tokens sortie</th><th class="num">Durée</th><th class="num">Coût</th></tr></thead>
      <tbody style="cursor:default">${agents}<tr><td><b>Total</b></td><td class="num"><b>${t.calls}</b></td><td></td><td class="num"><b>${num(t.tokens_in)}</b></td><td class="num"><b>${num(t.tokens_out)}</b></td><td class="num"><b>${Math.round(t.duration_s)} s</b></td><td class="num"><b>${cost(t.cost_usd)}</b></td></tr></tbody></table></div>
    <p class="small muted" style="margin-bottom:8px">Clique sur un appel pour lire le prompt envoyé et la réponse reçue. « ≈ » = tokens estimés (l'agent ne les fournit pas).</p>
    <div class="scroll"><table class="log"><thead><tr><th>Heure</th><th>Étape</th><th>Agent</th><th>Rôle</th><th>Partie</th><th class="num">Essai</th><th class="num">Entrée</th><th class="num">Sortie</th><th class="num">Durée</th><th>Résultat</th></tr></thead><tbody id="calls">${rows}</tbody></table></div>`;
  $("calls").querySelectorAll("tr").forEach((row) => row.addEventListener("click", () => showCall(Number(row.dataset.i))));
}

async function showCall(index) {
  const call = await api(`/api/projects/${encodeURIComponent(S.id)}/calls/${index}`);
  $("call-title").textContent = `${call.agent} — ${call.role}${call.part_id ? ` — ${call.part_id}` : ""} (essai ${call.attempt})`;
  $("call-body").innerHTML = `
    <p class="small muted">Modèle : ${esc(call.model || "—")} · ${num(call.tokens_in)} → ${num(call.tokens_out)} tokens · ${esc(call.duration_s)} s${call.cost_usd ? ` · ${call.cost_usd.toFixed(4)} $` : ""}</p>
    ${call.error ? `<div class="banner bad">${esc(call.error)}</div>` : ""}
    ${call.system ? `<div><h4 class="small muted">Prompt système</h4><pre>${esc(call.system)}</pre></div>` : ""}
    <div><h4 class="small muted">Prompt envoyé</h4><pre>${esc(call.prompt)}</pre></div>
    <div><h4 class="small muted">Réponse reçue</h4><pre>${esc(call.response || "(aucune)")}</pre></div>`;
  $("call-dialog").showModal();
}

init();
