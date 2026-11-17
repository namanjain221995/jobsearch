"use strict";
// All job data is untrusted: it is rendered with textContent / DOM nodes only, never innerHTML.
const $ = (id) => document.getElementById(id);
const MAX_BYTES = 10 * 1024 * 1024;
const COUNTERS = [
  ["requested", "Requested"], ["total_discovered", "Postings examined"], ["boards_checked", "Source reads OK"],
  ["sources_pending", "Source reads loading"], ["ai_leads", "AI web leads"], ["browser_pages", "Browser pages rendered"],
  ["crawled_links", "Links crawled"], ["ai_extracted", "AI-extracted jobs"], ["unique_candidates", "Unique candidates"],
  ["verified_active", "Verified active"], ["rejected_duplicates", "Duplicates removed"],
  ["rejected_eligibility", "Eligibility rejects"], ["rejected_non_us", "Non-US rejects"],
  ["rejected_unverified", "Unverified rejects"], ["ai_ranked", "AI-ranked"], ["final_matches", "Final matches"],
];
let resumeId = null, searchId = null, eventSource = null, jobs = [], shownVersion = 0;

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v; else if (k === "text") node.textContent = v; else node.setAttribute(k, v);
  }
  for (const c of children) if (c) node.append(c);
  return node;
}
function safeUrl(url) {
  try { const u = new URL(url); return u.protocol === "https:" || u.protocol === "http:" ? u.href : null; } catch { return null; }
}
function showError(msg) { const e = $("form-error"); e.textContent = msg; e.hidden = !msg; }
async function api(path, options) {
  const res = await fetch(path, options);
  let body = null;
  try { body = await res.json(); } catch { /* non-JSON */ }
  if (!res.ok) throw new Error(body && body.error ? [body.error, body.detail].filter(Boolean).join(" — ") : `Request failed (${res.status})`);
  return body;
}

// ---------------------------------------------------------------- resume upload
async function uploadResume(file) {
  showError("");
  resumeId = null;
  const zone = $("dropzone"), status = $("resume-status");
  const ext = file.name.split(".").pop().toLowerCase();
  if (!["pdf", "docx", "txt"].includes(ext)) { status.textContent = ""; return showError("Unsupported file type. Upload a PDF, DOCX or TXT resume."); }
  if (file.size > MAX_BYTES) { status.textContent = ""; return showError("File is too large. Maximum size is 10 MB."); }
  $("dropzone-text").textContent = file.name;
  zone.classList.add("has-file");
  status.textContent = "Reading resume…";
  const form = new FormData();
  form.append("file", file);
  try {
    const data = await api("/api/resume/parse", { method: "POST", body: form });
    resumeId = data.resume_id;
    const skills = data.detected_skills.length ? ` Detected skills: ${data.detected_skills.slice(0, 12).join(", ")}${data.detected_skills.length > 12 ? "…" : ""}.` : "";
    status.textContent = `Uploaded ${data.filename} (${data.characters.toLocaleString()} characters).${skills}${data.truncated ? " Note: very long resume was truncated." : ""}`;
  } catch (err) {
    zone.classList.remove("has-file");
    $("dropzone-text").textContent = "Drag and drop a PDF, DOCX or TXT file here, or click to choose";
    status.textContent = "";
    showError(err.message);
  }
}
function initUpload() {
  const zone = $("dropzone"), input = $("resume-input");
  zone.addEventListener("click", () => input.click());
  zone.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } });
  input.addEventListener("change", () => { if (input.files[0]) uploadResume(input.files[0]); });
  ["dragenter", "dragover"].forEach((t) => zone.addEventListener(t, (e) => { e.preventDefault(); zone.classList.add("over"); }));
  ["dragleave", "drop"].forEach((t) => zone.addEventListener(t, (e) => { e.preventDefault(); zone.classList.remove("over"); }));
  zone.addEventListener("drop", (e) => { const f = e.dataTransfer.files[0]; if (f) uploadResume(f); });
}

// ---------------------------------------------------------------- search + progress
function renderStages(stages, activeId, finished) {
  const list = $("stage-list");
  list.replaceChildren();
  const activeIndex = stages.findIndex((s) => s.id === activeId);
  stages.forEach((s, i) => {
    const cls = finished || i < activeIndex ? "done" : i === activeIndex ? "active" : "";
    list.append(el("li", { class: cls, text: s.label }));
  });
}
function renderCounters(stats) {
  const dl = $("counters");
  dl.replaceChildren();
  for (const [key, label] of COUNTERS) {
    if (stats[key] === undefined) continue;
    dl.append(el("div", {}, el("dt", { text: label }), el("dd", { text: Number(stats[key]).toLocaleString() })));
  }
}
function renderWarnings(warnings) {
  const ul = $("warnings");
  ul.replaceChildren(...(warnings || []).map((w) => el("li", { text: w })));
}
function renderSources(sources) {
  const table = $("sources"), body = table.querySelector("tbody");
  const names = Object.keys(sources || {});
  table.hidden = names.length === 0;
  body.replaceChildren(...names.map((name) => {
    const s = sources[name];
    return el("tr", {}, el("td", { text: name }), el("td", { text: String(s.boards_ok ?? 0) }), el("td", { text: String(s.pending ?? 0) }),
      el("td", { text: String(s.boards_failed ?? 0) }), el("td", { text: Number(s.postings ?? 0).toLocaleString() }));
  }));
}
function setRunning(running) {
  $("submit-btn").disabled = running;
  $("cancel-btn").hidden = !running;
}
async function startSearch(e) {
  e.preventDefault();
  showError("");
  if (!resumeId) return showError("Upload your resume first.");
  const titles = $("titles").value.trim();
  const years = Number($("years").value), count = Number($("count").value);
  if (titles.length < 2) return showError("Enter at least one desired job title.");
  if (!Number.isFinite(years) || years < 0 || years > 40 || $("years").value === "") return showError("Years of experience must be between 0 and 40.");
  if (!Number.isInteger(count) || count < 0 || count > 500 || $("count").value === "") return showError("Number of job results must be between 0 and 500.");
  const body = {
    resume_id: resumeId, job_titles: titles, years_experience: years, result_count: count,
    authorization: $("authorization").value, sponsorship_needed: $("sponsorship").value,
    exclude_citizenship_required: $("exclude-citizenship").checked, work_mode: $("work-mode").value,
    deep_search: $("deep-search").checked,
  };
  shownVersion = 0;
  renderSources({});
  $("results").hidden = true;
  $("progress").hidden = false;
  renderWarnings([]);
  setRunning(true);
  try {
    const data = await api("/api/jobs/search", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    searchId = data.search_id;
    listen(searchId);
  } catch (err) {
    setRunning(false);
    $("progress").hidden = true;
    showError(err.message);
  }
}
function listen(id) {
  let stages = [];
  if (eventSource) eventSource.close();
  eventSource = new EventSource(`/api/jobs/search/${encodeURIComponent(id)}/events`);
  eventSource.addEventListener("stages", (e) => { stages = JSON.parse(e.data); renderStages(stages, null, false); });
  eventSource.addEventListener("progress", (e) => {
    const d = JSON.parse(e.data);
    renderStages(stages, d.stage, false);
    renderCounters(d.stats);
    renderWarnings(d.warnings);
    renderSources(d.sources);
    if (d.partial && d.results_version > shownVersion) { shownVersion = d.results_version; loadResults(id); }   // show matches early
  });
  eventSource.addEventListener("done", async (e) => {
    const d = JSON.parse(e.data);
    eventSource.close();
    eventSource = null;
    setRunning(false);
    renderStages(stages, null, d.status === "completed");
    renderCounters(d.stats);
    renderWarnings(d.warnings);
    renderSources(d.sources);
    await loadResults(id);
  });
  eventSource.onerror = () => {
    if (!eventSource) return;
    eventSource.close();
    eventSource = null;
    pollUntilDone(id);     // fall back to polling if the stream drops
  };
}
async function pollUntilDone(id) {
  try {
    for (;;) {
      const s = await api(`/api/jobs/search/${encodeURIComponent(id)}/status`);
      renderCounters(s.stats);
      renderWarnings(s.warnings);
      if (["completed", "cancelled", "failed"].includes(s.status)) break;
      await new Promise((r) => setTimeout(r, 1500));
    }
    setRunning(false);
    await loadResults(id);
  } catch (err) { setRunning(false); showError(err.message); }
}
async function cancelSearch() {
  if (!searchId) return;
  try { await api(`/api/jobs/search/${encodeURIComponent(searchId)}/cancel`, { method: "POST" }); } catch (err) { showError(err.message); }
}

// ---------------------------------------------------------------- results
async function loadResults(id) {
  try {
    const data = await api(`/api/jobs/search/${encodeURIComponent(id)}/results`);
    if (id !== searchId) return;
    jobs = data.jobs || [];
    const running = data.status === "running" || data.status === "queued";
    $("results").hidden = false;
    const banner = $("partial-banner");
    banner.hidden = !data.partial;
    banner.textContent = running
      ? "Preliminary matches from sources already read, scored by keywords. The search is still running; this list will be replaced by the final ranking."
      : "The search stopped before it finished. These are preliminary matches from sources already read, scored by keywords only.";
    $("results-heading").textContent = data.partial ? `Preliminary results (${jobs.length})` : `Results (${jobs.length})`;
    $("results-message").textContent = data.message || "";
    renderSources(data.sources);
    $("csv-link").href = `/api/jobs/search/${encodeURIComponent(id)}/export.csv`;
    $("json-link").href = `/api/jobs/search/${encodeURIComponent(id)}/export.json`;
    renderWarnings(data.warnings);
    renderCards();
  } catch (err) { showError(err.message); }
}
function fact(label, value, cls) {
  return el("div", {}, el("dt", { text: label }), el("dd", { text: value || "Not listed", class: cls || "" }));
}
function chips(label, items, cls) {
  if (!items || !items.length) return null;
  const row = el("div", { class: "skills" }, el("span", { class: "label", text: label }));
  items.forEach((s) => row.append(el("span", { class: `chip ${cls || ""}`, text: s })));
  return row;
}
function card(job) {
  const authCls = job.authorization_status === "Eligible" ? "tag-ok" : job.authorization_status === "Potential mismatch" ? "tag-bad" : "";
  const citCls = job.citizenship_requirement === "Required" ? "tag-bad" : "";
  const head = el("div", { class: "card-head" },
    el("div", {}, el("h3", { text: job.title }), el("p", { class: "company", text: job.company })),
    el("div", { class: "score", title: job.score_source === "ai" ? "AI-assessed score" : "Keyword-based score" },
      el("b", { text: String(job.score) }), el("span", { text: "match / 100" })));
  const facts = el("dl", { class: "facts" },
    fact("Location", job.location), fact("Work mode", job.work_mode), fact("Salary", job.salary),
    fact("Experience required", job.experience_required),
    fact("Work authorization", job.authorization_status, authCls),
    fact("Citizenship requirement", job.citizenship_requirement, citCls),
    fact("Visa sponsorship", job.sponsorship), fact("Eligibility details", job.eligibility_note),
    fact("Posted", job.posted_at || "Not listed"));
  const actions = el("div", { class: "card-actions" });
  const applyUrl = safeUrl(job.apply_url), viewUrl = safeUrl(job.url);
  if (applyUrl) actions.append(el("a", { class: "btn apply", href: applyUrl, target: "_blank", rel: "noopener noreferrer", text: "Apply Now" }));
  if (viewUrl) actions.append(el("a", { class: "btn", href: viewUrl, target: "_blank", rel: "noopener noreferrer", text: "View Job" }));
  const link = applyUrl || viewUrl;
  if (link) {
    const copy = el("button", { type: "button", text: "Copy link" });
    copy.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(link); copy.textContent = "Copied"; } catch { copy.textContent = "Copy failed"; }
      setTimeout(() => { copy.textContent = "Copy link"; }, 1500);
    });
    actions.append(copy);
  }
  const b = job.score_breakdown || {};
  const breakdown = `Skills ${b.skills ?? "-"}/35 · Title ${b.title ?? "-"}/25 · Experience ${b.experience ?? "-"}/20 · Arrangement ${b.arrangement ?? "-"}/10 · Additional ${b.additional ?? "-"}/10 (${job.score_source === "ai" ? "AI-assessed" : "keyword-based"})`;
  return el("article", { class: "card" }, head, facts,
    el("p", { class: "reason", text: job.reason }),
    job.experience_comparison ? el("p", { class: "reason", text: job.experience_comparison }) : null,
    chips("Matching skills:", job.matching_skills), chips("Missing:", job.missing_skills, "miss"),
    el("p", { class: "meta", text: breakdown }),
    el("p", { class: "meta", text: `Source: ${(job.sources && job.sources.length ? job.sources : [job.source]).join(", ")} · Verified ${job.verified_at || "unknown"} · ${job.verification}` }),
    actions);
}
function renderCards() {
  const minScore = Number($("min-score").value) || 0, auth = $("auth-filter").value, remote = $("remote-only").checked;
  let shown = jobs.filter((j) => j.score >= minScore && (!auth || j.authorization_status === auth) && (!remote || j.work_mode === "Remote"));
  if ($("sort").value === "date") shown = shown.slice().sort((a, b) => (b.posted_at || "").localeCompare(a.posted_at || "") || b.score - a.score);
  else shown = shown.slice().sort((a, b) => (a.score_source === "ai" ? 0 : 1) - (b.score_source === "ai" ? 0 : 1) || b.score - a.score);
  $("shown-count").textContent = `Showing ${shown.length} of ${jobs.length} jobs.`;
  $("cards").replaceChildren(...shown.map(card));
}

async function loadHealth() {
  try {
    const h = await api("/api/health");
    const part = (name, p) => `${name}: ${p.configured ? `configured (${p.model})` : "not configured"}`;
    const browser = h.browser.enabled ? (h.browser.available ? `on (${h.browser.concurrency} pages at once)` : `off: ${h.browser.note}`) : "off";
    $("provider-status").textContent = `${part("OpenAI", h.providers.openai)} · ${part("Claude", h.providers.claude)} · ${h.sources.length} sources: ${h.sources.map((x) => x.name).join(", ")} (${h.seed_boards} employers) · Local index: ${Number(h.index.fresh_postings).toLocaleString()} fresh postings · Browser crawler: ${browser}`;
  } catch { $("provider-status").textContent = "Server status unavailable."; }
}

initUpload();
loadHealth();
$("search-form").addEventListener("submit", startSearch);
$("cancel-btn").addEventListener("click", cancelSearch);
["sort", "min-score", "auth-filter", "remote-only"].forEach((id) => $(id).addEventListener("input", renderCards));
