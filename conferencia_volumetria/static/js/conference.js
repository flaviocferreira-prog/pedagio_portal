import { api, ApiError, routes } from "./api-client.js";
import { notify } from "./notifications.js";
import { startTimer } from "./timer.js";

let conference = null;
let scanning = false;
const scanQueue = [];
let finishing = false;
let cancelling = false;
let authorizingReconference = false;
let scanTimer = null;
const boxElements = new Map();
let surplusAlertSuppressedForFinish = false;
let surplusCorrection = null;
const $ = (selector) => document.querySelector(selector);
const AUTO_SCAN_DELAY_MS = 160;

function normalizeCaixaEstoque(value) { return String(value ?? "").trim(); }
function isActiveConference(data) { return data && data.status === "EM_ABERTO"; }
function resetSurplusAlert() {
  const alert = $("#surplus-alert");
  surplusAlertSuppressedForFinish = false;
  alert.hidden = true;
  alert.setAttribute("aria-hidden", "true");
  alert.classList.remove("surplus-alert--visible");
  ["#surplus-alert-title", "#surplus-alert-summary", "#surplus-alert-code", "#surplus-alert-guidance"].forEach((selector) => { $(selector).textContent = ""; });
}

function renderSurplusAlert(data = conference) {
  if (!isActiveConference(data)) {
    resetSurplusAlert();
    return;
  }
  const total = Number(data?.summary?.total_extra || 0);
  const extras = data?.unexpected_cartons || [];
  if (total <= 0 || !extras.length) {
    resetSurplusAlert();
    return;
  }
  const latest = extras.at(-1);
  const alert = $("#surplus-alert");
  $("#surplus-alert-title").textContent = total === 1
    ? "ATENÇÃO — SOBRA IDENTIFICADA"
    : "ATENÇÃO — SOBRAS IDENTIFICADAS";
  $("#surplus-alert-summary").textContent = `Esta conferência possui ${total} ${total === 1 ? "sobra" : "sobras"}.`;
  $("#surplus-alert-code").textContent = `Última caixa identificada: ${normalizeCaixaEstoque(latest?.caixa_estoque) || "Não informada"}`;
  $("#surplus-alert-guidance").textContent = "Continue a conferência e trate as sobras antes de finalizar.";
  const shouldDisplay = !surplusAlertSuppressedForFinish;
  alert.hidden = !shouldDisplay;
  alert.setAttribute("aria-hidden", String(!shouldDisplay));
  alert.classList.toggle("surplus-alert--visible", shouldDisplay);
}

function suppressSurplusAlertForFinish() {
  surplusAlertSuppressedForFinish = true;
  renderSurplusAlert();
}

function restoreSurplusAlertAfterFinishModal() {
  surplusAlertSuppressedForFinish = false;
  renderSurplusAlert();
}

function resetSurplusCorrection() {
  surplusCorrection = null;
  const notice = $("#surplus-correction-notice");
  notice.hidden = true;
  notice.setAttribute("aria-hidden", "true");
  $("#surplus-correction-code").textContent = "";
}

function renderSurplusCorrection(data = conference) {
  const pending = data?.unexpected_cartons || [];
  const selected = surplusCorrection && pending.find(
    (item) => Number(item.event_id) === Number(surplusCorrection.eventId),
  );
  if (!isActiveConference(data) || !selected) {
    resetSurplusCorrection();
    return;
  }
  surplusCorrection = { eventId: selected.event_id, code: selected.caixa_estoque };
  const notice = $("#surplus-correction-notice");
  $("#surplus-correction-code").textContent = normalizeCaixaEstoque(selected.caixa_estoque);
  notice.hidden = false;
  notice.setAttribute("aria-hidden", "false");
}

export function showImportCard() {
  conference = null;
  scanning = false;
  scanQueue.length = 0;
  clearTimeout(scanTimer);
  resetSurplusAlert();
  resetSurplusCorrection();
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
  $("#extra-count").textContent = "0 sobras";
  $("#summary-finalized-wrap").hidden = true;
  $("#conference").hidden = true;
  $("#new-import-button").hidden = false;
  startTimer({});
}

function renderSummary(data) {
  if (!data) { showImportCard(); return false; }
  const previousConference = conference;
  conference = data;
  sessionStorage.setItem("conference_public_id", data.public_id);
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
  $("#extra-count").textContent = `${summary.total_extra} sobras`;
  const active = data.status === "EM_ABERTO" && Boolean(data.started_at);
  $("#carton-form").hidden = !active;
  $("#finish-button").hidden = !active;
  const pendingSurpluses = Number(summary.total_extra || 0);
  const canFinish = summary.can_finish && pendingSurpluses === 0;
  $("#finish-button").disabled = !canFinish;
  $("#finish-button").classList.toggle("ready-to-finish", active && canFinish);
  $("#cancel-conference-button").hidden = !active;
  $("#authorize-reconference-button").hidden = !data.can_authorize_reconference;
  if (active && canFinish && !previousConference?.summary?.can_finish) {
    notify("100% conferido — aguardando finalização.", "attention", { id: `finish-ready-${data.public_id}` });
  }
  if (data.action === "already_completed" && previousConference?.public_id !== data.public_id) {
    notify("Resultado de uma conferência anterior — somente consulta.", "info", { id: `historical-${data.public_id}` });
  }
  startTimer(data);
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
  $("#extra-list").replaceChildren(...extras.map((box) => {
    const item = document.createElement("li"); item.className = "extra-item pending";
    const code = document.createElement("strong"); code.textContent = normalizeCaixaEstoque(box.caixa_estoque);
    const correct = document.createElement("button"); correct.type = "button"; correct.className = "correct-surplus-button";
    correct.dataset.surplusEventId = String(box.event_id); correct.textContent = "Corrigir sobra";
    item.replaceChildren(code, correct); return item;
  }));
}

export function render(data) {
  if (!renderSummary(data)) return;
  renderExpectedBoxes(data.cartons, data); renderExtras(data.unexpected_cartons || []);
  renderSurplusAlert(data);
  renderSurplusCorrection(data);
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

export function bindConference() {
  const cartonForm = $("#carton-form"); const input = $("#carton-code");
  async function processNextScan() {
    if (scanning || !scanQueue.length) return;
    if (!conference || conference.status !== "EM_ABERTO") { scanQueue.length = 0; return; }
    const caixaEstoque = scanQueue.shift();
    const publicId = conference.public_id;
    const correction = surplusCorrection;
    scanning = true;
    try {
      if (correction) {
        const result = await api(`/api/conferences/${encodeURIComponent(publicId)}/correct-surplus`, {
          method: "POST",
          body: JSON.stringify({ surplus_event_id: correction.eventId, caixa_estoque: caixaEstoque }),
        });
        render(result);
        notify(`Sobra corrigida com sucesso\nCaixa: ${normalizeCaixaEstoque(result.caixa_estoque)}`, "success");
      } else {
        const result = await api(`/api/conferences/${encodeURIComponent(publicId)}/scan`, { method: "POST", body: JSON.stringify({ caixa_estoque: caixaEstoque }) });
        render(result);
        if (result.result !== "CONFERIDA" && result.result !== "SOBRA") notify(result.message, "attention");
      }
    } catch (error) {
      if (correction && error instanceof ApiError && error.code === "CODIGO_CORRECAO_INCORRETO") {
        notify(`Código incorreto\nEsperado: ${normalizeCaixaEstoque(error.details.expected_code)}\nLido: ${normalizeCaixaEstoque(error.details.scanned_code)}\nBipe novamente a caixa correta.`, "error");
      } else if (correction && error instanceof ApiError && ["SOBRA_JA_CORRIGIDA", "SOBRA_NAO_ENCONTRADA"].includes(error.code)) {
        resetSurplusCorrection();
        notify(error.message, "error");
      } else {
        notify(error instanceof ApiError ? error.message : error.message, "error");
      }
    } finally {
      scanning = false;
      if (conference?.status === "EM_ABERTO") input.focus();
      processNextScan();
    }
  }
  function queueScan(value) {
    const caixaEstoque = normalizeCaixaEstoque(value);
    if (!conference || conference.status !== "EM_ABERTO" || !caixaEstoque) return;
    scanQueue.push(caixaEstoque);
    input.value = "";
    processNextScan();
  }
  function scheduleScan() { clearTimeout(scanTimer); const value = normalizeCaixaEstoque(input.value); if (value) scanTimer = setTimeout(() => queueScan(input.value), AUTO_SCAN_DELAY_MS); }
  cartonForm.addEventListener("submit", (event) => { event.preventDefault(); clearTimeout(scanTimer); queueScan(input.value); }); input.addEventListener("input", scheduleScan); input.addEventListener("paste", () => setTimeout(scheduleScan, 0));
  $("#extra-list").addEventListener("click", (event) => {
    const button = event.target.closest(".correct-surplus-button");
    if (!button || !conference || conference.status !== "EM_ABERTO") return;
    const eventId = Number(button.dataset.surplusEventId);
    const selected = (conference.unexpected_cartons || []).find((item) => Number(item.event_id) === eventId);
    if (!selected) return;
    clearTimeout(scanTimer);
    input.value = "";
    surplusCorrection = { eventId: selected.event_id, code: selected.caixa_estoque };
    renderSurplusCorrection(conference);
    input.focus();
  });
  $("#cancel-surplus-correction").addEventListener("click", () => {
    resetSurplusCorrection();
    input.value = "";
    input.focus();
  });
  const finishModal = $("#finish-modal");
  function configureFinishModal() {
    const surplus = Number(conference?.summary?.total_extra || 0);
    const hasSurplus = surplus > 0;
    const warning = $("#finish-surplus-warning");
    const details = $("#finish-surplus-details");
    finishModal.classList.toggle("finish-modal--surplus", hasSurplus);
    $("#finish-surplus-icon").hidden = !hasSurplus;
    $("#finish-title-text").textContent = hasSurplus ? "ATENÇÃO — EXISTEM SOBRAS" : "Finalizar conferência";
    $("#finish-intro").textContent = hasSurplus ? "" : "Tem certeza de que deseja finalizar esta conferência? Após finalizar, não será possível bipar, cancelar ou alterar os dados.";
    warning.hidden = !hasSurplus;
    details.hidden = !hasSurplus;
    warning.textContent = hasSurplus ? `Foram identificadas ${surplus} sobras nesta conferência. Você já tratou todas as sobras no palete?` : "";
    $("#finish-surplus-list").replaceChildren(...(conference?.unexpected_cartons || []).map((box) => {
      const item = document.createElement("li");
      const code = document.createElement("strong"); code.textContent = normalizeCaixaEstoque(box.caixa_estoque);
      const order = document.createElement("span"); order.textContent = box.scan_order ? `Leitura #${box.scan_order}` : "SOBRA";
      const timestamp = document.createElement("time"); timestamp.textContent = box.first_seen_at_local || box.first_seen_at || "Horário não informado";
      item.replaceChildren(code, order, timestamp); return item;
    }));
    $("#finish-cancel").textContent = hasSurplus ? "Voltar para a conferência" : "Voltar";
    $("#finish-confirm").textContent = hasSurplus ? "Sim, tratei todas as sobras" : "Confirmar finalização";
  }
  $("#finish-button").addEventListener("click", () => {
    if (conference?.status !== "EM_ABERTO") return;
    configureFinishModal();
    if (Number(conference.summary?.total_extra || 0) > 0) suppressSurplusAlertForFinish();
    finishModal.showModal();
  });
  $("#finish-cancel").addEventListener("click", () => finishModal.close());
  finishModal.addEventListener("close", () => {
    if (!finishing && conference?.status === "EM_ABERTO") {
      restoreSurplusAlertAfterFinishModal();
      input.focus();
    }
  });
  $("#finish-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (finishing || !conference) return;
    const confirmSurpluses = Number(conference.summary?.total_extra || 0) > 0;
    finishing = true;
    $("#finish-confirm").disabled = true;
    try {
      const result = await api(`/api/conferences/${encodeURIComponent(conference.public_id)}/finish`, { method: "POST", body: JSON.stringify({ confirmar_sobras: confirmSurpluses }) });
      finishModal.close(); showImportCard();
      window.dispatchEvent(new Event("sync-state-changed"));
      notify(result.message);
      $("#new-import-button").focus();
    } catch (error) {
      notify(error instanceof ApiError ? error.message : error.message, "error");
      if (finishModal.open) finishModal.close();
      restoreSurplusAlertAfterFinishModal();
      input.focus();
    } finally {
      finishing = false;
      $("#finish-confirm").disabled = false;
    }
  });
  const cancelModal = $("#cancel-conference-modal"); $("#cancel-conference-button").addEventListener("click", () => cancelModal.showModal()); $("#cancel-conference-dismiss").addEventListener("click", () => cancelModal.close()); $("#cancel-conference-form").addEventListener("submit", async (event) => { event.preventDefault(); if (cancelling || !conference) return; cancelling = true; try { const result = await api(`/api/conferences/${encodeURIComponent(conference.public_id)}/cancel`, { method: "POST", body: "{}" }); cancelModal.close(); showImportCard(); notify(result.message, "success"); } catch (error) { notify(error.message, "error"); } finally { cancelling = false; } });
  const reconferenceModal = $("#reconference-modal"); $("#authorize-reconference-button").addEventListener("click", () => { if (conference?.status === "FINALIZADA") reconferenceModal.showModal(); }); $("#reconference-dismiss").addEventListener("click", () => reconferenceModal.close()); $("#reconference-form").addEventListener("submit", async (event) => { event.preventDefault(); const reason = $("#reconference-reason").value.trim(); if (authorizingReconference || reason.length < 10 || !conference) return; authorizingReconference = true; try { const result = await api(`/api/conferences/${encodeURIComponent(conference.public_id)}/reconference`, { method: "POST", body: JSON.stringify({ justificativa: reason }) }); reconferenceModal.close(); $("#reconference-reason").value = ""; render(result); notify(result.message, "success"); } catch (error) { notify(error.message, "error"); } finally { authorizingReconference = false; } });
}
