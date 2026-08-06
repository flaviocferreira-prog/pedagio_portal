import { api, ApiError, routes } from "./api-client.js";
import { notify } from "./notifications.js";
import { startTimer } from "./timer.js";

let conference = null;
let scanning = false;
let finishing = false;
let cancelling = false;
let authorizingReconference = false;
let scanSequence = 0;
let scanTimer = null;
let syncing = false;
let syncPopup = null;
let syncContext = null;
let syncTimeout = null;
let synchronizedCount = 0;
const boxElements = new Map();
const $ = (selector) => document.querySelector(selector);
const AUTO_SCAN_DELAY_MS = 160;
const GOOGLE_BRIDGE_TIMEOUT_MS = 300000;

function normalizeCaixaEstoque(value) { return String(value ?? "").trim(); }
function isActiveConference(data) { return data && data.status === "EM_ABERTO"; }

export async function refreshPendingSyncs() {
  const result = await api(routes.pendingSynchronizations);
  const count = result.pending || 0;
  $("#actions-card").hidden = false;
  $("#pending-sync-count").textContent = `${count} ${count === 1 ? "importação pendente" : "importações pendentes"}`;
  $("#sync-pending-button").disabled = syncing || count === 0;
  return count;
}

export function showImportCard() {
  conference = null;
  sessionStorage.removeItem("conference_public_id");
  $("#carton-code").value = "";
  $("#carton-list").replaceChildren();
  $("#extra-list").replaceChildren();
  boxElements.clear();
  ["#summary-id", "#summary-file", "#summary-origin", "#summary-operation", "#summary-shift", "#summary-imported-at", "#summary-finalized-at", "#summary-collaborator", "#summary-attempt"].forEach((selector) => { $(selector).textContent = ""; });
  ["#summary-expected", "#summary-confirmed", "#summary-pending", "#summary-surplus", "#summary-duplicate"].forEach((selector) => { $(selector).textContent = "0"; });
  $("#summary-coverage").textContent = "0%";
  $("#summary-pallet-class").textContent = "NÃO INFORMADO";
  $("#progress").value = 0;
  $("#box-count").textContent = "0 caixas";
  $("#extra-count").textContent = "0 divergentes";
  $("#summary-finalized-wrap").hidden = true;
  $("#conference").hidden = true;
  $("#actions-card").hidden = false;
  $("#new-import-button").hidden = false;
  startTimer({});
  refreshPendingSyncs().catch((error) => notify(error.message, "error"));
}

function renderSummary(data) {
  if (!data) { showImportCard(); return false; }
  const previousConference = conference;
  conference = data;
  sessionStorage.setItem("conference_public_id", data.public_id);
  $("#actions-card").hidden = false;
  $("#new-import-button").hidden = isActiveConference(data);
  $("#conference").hidden = false;
  $("#summary-id").textContent = data.public_id;
  $("#summary-file").textContent = data.source_filename;
  const imported = data.importation || {};
  $("#summary-origin").textContent = imported.origin || "Não informada";
  $("#summary-operation").textContent = imported.operation === "NIKESTORE" ? "Nike Store" : (imported.operation || "Não informada");
  $("#summary-shift").textContent = imported.shift || "Não informado";
  $("#summary-imported-at").textContent = imported.imported_at || data.created_at || "";
  const endedAt = data.finalization?.finished_at || data.cancellation?.cancelled_at || "";
  $("#summary-finalized-wrap").hidden = !endedAt;
  $("#summary-ended-label").textContent = data.status === "CANCELADA" ? "Cancelada em" : "Finalizada em";
  $("#summary-finalized-at").textContent = endedAt;
  const collaborator = data.collaborator || {};
  $("#summary-collaborator").textContent = `${collaborator.name || ""} — ${collaborator.registration || data.collaborator_id}`.trim();
  $("#summary-attempt").textContent = data.active_attempt?.number || 0;
  $("#summary-status").textContent = data.display_status || data.workflow_status || data.status;
  const summary = data.summary;
  $("#summary-expected").textContent = summary.total_expected;
  $("#summary-confirmed").textContent = summary.total_confirmed;
  $("#summary-pending").textContent = summary.total_missing;
  $("#summary-surplus").textContent = summary.total_extra;
  $("#summary-duplicate").textContent = summary.total_duplicate_reads;
  $("#summary-coverage").textContent = `${summary.coverage_percent}%`;
  $("#summary-pallet-class").textContent = data.pallet_class || "NÃO INFORMADO";
  $("#progress").value = summary.coverage_percent;
  $("#box-count").textContent = `${summary.total_expected} caixas`;
  $("#extra-count").textContent = `${summary.total_extra} divergentes`;
  const active = data.status === "EM_ABERTO" && Boolean(data.started_at);
  $("#carton-form").hidden = !active;
  $("#finish-button").hidden = !active;
  $("#finish-button").disabled = !summary.can_finish;
  $("#finish-button").classList.toggle("ready-to-finish", active && summary.can_finish);
  $("#cancel-conference-button").hidden = !active;
  $("#authorize-reconference-button").hidden = !data.can_authorize_reconference;
  if (active && summary.can_finish && !previousConference?.summary?.can_finish) {
    notify("100% conferido — aguardando finalização.", "attention", { id: `finish-ready-${data.public_id}` });
  }
  if (data.action === "already_completed" && previousConference?.public_id !== data.public_id) {
    notify("Resultado de uma conferência anterior — somente consulta.", "info", { id: `historical-${data.public_id}` });
  }
  startTimer(data);
  refreshPendingSyncs().catch(() => {});
  return true;
}

function updateBoxElement(item, box, duplicates = new Set()) {
  item.className = box.status === "CONFIRMED" ? "box-item confirmed" : "box-item pending";
  const code = document.createElement("strong"); code.textContent = normalizeCaixaEstoque(box.caixa_estoque);
  const classe = document.createElement("span"); classe.textContent = box.ds_classe || "NÃO INFORMADO";
  const state = document.createElement("span"); state.textContent = duplicates.has(normalizeCaixaEstoque(box.caixa_estoque)) ? "DUPLICADO" : box.status === "CONFIRMED" ? "OK" : "FALTA";
  item.replaceChildren(code, classe, state);
}

function renderExpectedBoxes(boxes, data) {
  boxElements.clear();
  const duplicates = new Set((data.divergences || []).filter((item) => item.type === "DUPLICIDADE").map((item) => normalizeCaixaEstoque(item.caixa_estoque)));
  const fragment = document.createDocumentFragment();
  [...boxes].sort((a, b) => Number(a.status === "CONFIRMED") - Number(b.status === "CONFIRMED")).forEach((box) => {
    const item = document.createElement("li"); item.dataset.caixaEstoque = normalizeCaixaEstoque(box.caixa_estoque);
    updateBoxElement(item, box, duplicates); boxElements.set(normalizeCaixaEstoque(box.caixa_estoque), item); fragment.append(item);
  });
  $("#carton-list").replaceChildren(fragment);
}

function renderExtras(extras) {
  $("#extra-list").replaceChildren(...extras.map((box) => { const item = document.createElement("li"); item.className = "extra-item pending"; const code = document.createElement("strong"); code.textContent = normalizeCaixaEstoque(box.caixa_estoque); item.append(code); return item; }));
}

export function render(data) {
  if (!renderSummary(data)) return;
  renderExpectedBoxes(data.cartons, data); renderExtras(data.unexpected_cartons || []);
  if (data.status === "EM_ABERTO") $("#carton-code").focus();
}

async function renderAndStart(data) {
  render(data);
  if (data.status === "EM_ABERTO" && !data.started_at) { const started = await api(`/api/conferences/${encodeURIComponent(data.public_id)}/start`, { method: "POST", body: "{}" }); render(started); return started; }
  return data;
}

export async function load(publicId, initialData = null) { return renderAndStart(initialData || await api(`/api/conferences/${encodeURIComponent(publicId)}`)); }
export async function loadActiveConference() {
  const state = await api(routes.activeConference);
  if (state.has_active_conference && state.conference) return renderAndStart(state.conference);
  showImportCard(); return null;
}

function submitSyncPopup(prepared) {
  const form = syncPopup.document.createElement("form"); form.method = "POST"; form.action = prepared.apps_script_url;
  Object.entries({ modo: "popup", payload: prepared.payload, assinatura: prepared.signature, id_conferencia: prepared.public_id, attempt_id: prepared.attempt_id, parent_origin: prepared.application_origin, return_url: prepared.return_url, nonce: prepared.nonce, popup_token: prepared.popup_token }).forEach(([name, value]) => { const input = syncPopup.document.createElement("input"); input.type = "hidden"; input.name = name; input.value = value; form.append(input); });
  syncPopup.document.body.replaceChildren(form); form.submit();
}

async function failSync(code) {
  if (!syncContext) return;
  clearTimeout(syncTimeout);
  try { await api(`/sincronizacao/${encodeURIComponent(syncContext.public_id)}/erro/`, { method: "POST", body: JSON.stringify({ code, attempt_id: syncContext.attempt_id, nonce: syncContext.nonce }) }); } catch {}
  syncing = false; syncContext = null;
  notify(`Falha ao sincronizar. ${synchronizedCount} sincronizada(s); as demais permanecem pendentes.`, "error", { id: "pending-sync-result" });
  await refreshPendingSyncs();
}

async function prepareNextSync() {
  const result = await api(routes.preparePendingSynchronizations, { method: "POST", body: "{}" });
  const prepared = result.prepared;
  if (!prepared) {
    syncing = false; syncContext = null; clearTimeout(syncTimeout);
    if (syncPopup && !syncPopup.closed) syncPopup.close();
    notify(`${synchronizedCount} conferência(s) sincronizada(s) com sucesso. ${result.pending || 0} pendente(s).`, "success", { id: "pending-sync-result" });
    await refreshPendingSyncs(); return;
  }
  syncContext = prepared;
  submitSyncPopup(prepared);
  clearTimeout(syncTimeout);
  syncTimeout = setTimeout(() => failSync("GOOGLE_BRIDGE_TIMEOUT"), GOOGLE_BRIDGE_TIMEOUT_MS);
}

async function startPendingSynchronization() {
  if (syncing) return;
  syncPopup = window.open("", "google_sync", "popup=yes,width=520,height=650,resizable=yes,scrollbars=yes");
  if (!syncPopup) { notify("Popup bloqueado. Permita janelas para sincronizar.", "error"); return; }
  syncing = true; synchronizedCount = 0;
  try { await prepareNextSync(); } catch (error) { await failSync(error.code === "NETWORK_ERROR" ? "NETWORK_ERROR" : "LOCAL_CONFIRMATION_FAILED"); notify(error.message, "error"); }
}

async function handleSyncMessage(event) {
  if (event.origin !== window.location.origin || event.source !== syncPopup || !syncing || !syncContext) return;
  const data = event.data;
  if (!data || data.source !== "google-sheets-sync" || data.type !== "GOOGLE_SYNC_RESULT" || data.conference_id !== syncContext.public_id || data.attempt_id !== syncContext.attempt_id || data.nonce !== syncContext.nonce) return;
  if (data.status === "ERROR") { await failSync(data.error_code || "GOOGLE_SYNC_FAILED"); return; }
  if (data.status !== "SUCCESS" && data.status !== "ALREADY_SYNCED") { await failSync("GOOGLE_RECEIPT_INVALID"); return; }
  clearTimeout(syncTimeout); synchronizedCount += 1;
  try { await prepareNextSync(); } catch (error) { await failSync("LOCAL_CONFIRMATION_FAILED"); notify(error.message, "error"); }
}

export function bindConference() {
  const cartonForm = $("#carton-form"); const input = $("#carton-code");
  async function processScan(value) {
    const caixaEstoque = normalizeCaixaEstoque(value); if (scanning || !conference || conference.status !== "EM_ABERTO" || !caixaEstoque) return;
    scanning = true; input.disabled = true; const sequence = ++scanSequence;
    try { const result = await api(`/api/conferences/${encodeURIComponent(conference.public_id)}/scan`, { method: "POST", body: JSON.stringify({ caixa_estoque: caixaEstoque }) }); if (sequence === scanSequence) { render(result); input.value = ""; if (result.result !== "CONFERIDA") notify(result.message, result.result === "DUPLICADA" ? "attention" : "error"); } }
    catch (error) { if (sequence === scanSequence) { notify(error.message, "error"); input.select(); } }
    finally { if (sequence === scanSequence) { scanning = false; input.disabled = false; input.focus(); } }
  }
  function scheduleScan() { clearTimeout(scanTimer); const value = normalizeCaixaEstoque(input.value); if (value && !scanning) scanTimer = setTimeout(() => processScan(value), AUTO_SCAN_DELAY_MS); }
  cartonForm.addEventListener("submit", (event) => { event.preventDefault(); clearTimeout(scanTimer); processScan(input.value); }); input.addEventListener("input", scheduleScan); input.addEventListener("paste", () => setTimeout(scheduleScan, 0));
  const finishModal = $("#finish-modal"); $("#finish-button").addEventListener("click", () => { if (conference?.status === "EM_ABERTO") finishModal.showModal(); }); $("#finish-cancel").addEventListener("click", () => finishModal.close());
  $("#finish-form").addEventListener("submit", async (event) => { event.preventDefault(); if (finishing || !conference) return; finishing = true; $("#finish-confirm").disabled = true; try { const result = await api(`/api/conferences/${encodeURIComponent(conference.public_id)}/finish`, { method: "POST", body: "{}" }); finishModal.close(); showImportCard(); notify(result.message); $("#new-import-button").focus(); } catch (error) { notify(error instanceof ApiError ? error.message : error.message, "error"); input.focus(); } finally { finishing = false; $("#finish-confirm").disabled = false; } });
  const cancelModal = $("#cancel-conference-modal"); $("#cancel-conference-button").addEventListener("click", () => cancelModal.showModal()); $("#cancel-conference-dismiss").addEventListener("click", () => cancelModal.close()); $("#cancel-conference-form").addEventListener("submit", async (event) => { event.preventDefault(); if (cancelling || !conference) return; cancelling = true; try { const result = await api(`/api/conferences/${encodeURIComponent(conference.public_id)}/cancel`, { method: "POST", body: "{}" }); cancelModal.close(); showImportCard(); notify(result.message, "success"); } catch (error) { notify(error.message, "error"); } finally { cancelling = false; } });
  $("#sync-pending-button").addEventListener("click", startPendingSynchronization); window.addEventListener("message", handleSyncMessage);
  const reconferenceModal = $("#reconference-modal"); $("#authorize-reconference-button").addEventListener("click", () => { if (conference?.status === "FINALIZADA") reconferenceModal.showModal(); }); $("#reconference-dismiss").addEventListener("click", () => reconferenceModal.close()); $("#reconference-form").addEventListener("submit", async (event) => { event.preventDefault(); const reason = $("#reconference-reason").value.trim(); if (authorizingReconference || reason.length < 10 || !conference) return; authorizingReconference = true; try { const result = await api(`/api/conferences/${encodeURIComponent(conference.public_id)}/reconference`, { method: "POST", body: JSON.stringify({ justificativa: reason }) }); reconferenceModal.close(); $("#reconference-reason").value = ""; render(result); notify(result.message, "success"); } catch (error) { notify(error.message, "error"); } finally { authorizingReconference = false; } });
}
