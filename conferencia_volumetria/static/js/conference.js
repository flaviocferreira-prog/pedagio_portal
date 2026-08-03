import { api, ApiError } from "./api-client.js";
import { notify } from "./notifications.js";
import { startTimer } from "./timer.js";

let conference = null;
let scanning = false;
let finishing = false;
let cancelling = false;
let authorizingReconference = false;
let scanSequence = 0;
let scanTimer = null;
const boxElements = new Map();
const $ = (selector) => document.querySelector(selector);
const AUTO_SCAN_DELAY_MS = 160;

function isActiveConference(data) {
  return data && data.status === "EM_ABERTO";
}

export function showImportCard() {
  conference = null;
  sessionStorage.removeItem("conference_public_id");
  $("#carton-code").value = "";
  $("#carton-list").replaceChildren();
  $("#extra-list").replaceChildren();
  boxElements.clear();
  [
    "#summary-id", "#summary-file", "#summary-agenda", "#summary-origin",
    "#summary-operation", "#summary-shift", "#summary-imported-at",
    "#summary-finalized-at", "#summary-collaborator", "#summary-attempt",
  ].forEach((selector) => { $(selector).textContent = ""; });
  [
    "#summary-expected", "#summary-confirmed", "#summary-pending", "#summary-surplus",
    "#summary-duplicate",
  ].forEach((selector) => { $(selector).textContent = "0"; });
  $("#summary-coverage").textContent = "0%";
  $("#progress").value = 0;
  $("#box-count").textContent = "0 caixas";
  $("#extra-count").textContent = "0 divergentes";
  $("#summary-finalized-wrap").hidden = true;
  $("#completion-notice").hidden = true;
  $("#historical-notice").hidden = true;
  $("#conference").hidden = true;
  $("#upload-card").hidden = false;
  startTimer({});
}

function normalizeCaixaEstoque(value) {
  return String(value ?? "").trim();
}

function renderSummary(data) {
  if (!data) {
    showImportCard();
    return false;
  }
  conference = data;
  sessionStorage.setItem("conference_public_id", data.public_id);
  $("#upload-card").hidden = isActiveConference(data);
  $("#conference").hidden = false;
  $("#summary-id").textContent = data.public_id;
  $("#summary-file").textContent = data.source_filename;
  const importation = data.importation || {};
  $("#summary-agenda").textContent = importation.agenda || "Não informada";
  $("#summary-origin").textContent = importation.origin || "Não informada";
  $("#summary-operation").textContent = importation.operation === "NIKESTORE" ? "Nike Store" : (importation.operation || "Não informada");
  $("#summary-shift").textContent = importation.shift || "Não informado";
  $("#summary-imported-at").textContent = importation.imported_at || data.created_at || "";
  const finalization = data.finalization || {};
  const cancellation = data.cancellation || {};
  const endedAt = finalization.finished_at || cancellation.cancelled_at || "";
  $("#summary-finalized-wrap").hidden = !endedAt;
  $("#summary-ended-label").textContent = data.status === "CANCELADA" ? "Cancelada em" : "Finalizada em";
  $("#summary-finalized-at").textContent = endedAt;
  const collaborator = data.collaborator || {};
  $("#summary-collaborator").textContent =
    `${collaborator.name || ""} — ${collaborator.registration || data.collaborator_id}`.trim();
  $("#summary-attempt").textContent = data.active_attempt.number;
  $("#summary-status").textContent = data.display_status || data.workflow_status || data.status;
  const summary = data.summary;
  $("#summary-expected").textContent = summary.total_expected;
  $("#summary-confirmed").textContent = summary.total_confirmed;
  $("#summary-pending").textContent = summary.total_missing;
  $("#summary-surplus").textContent = summary.total_extra;
  $("#summary-duplicate").textContent = summary.total_duplicate_reads;
  $("#summary-coverage").textContent = `${summary.coverage_percent}%`;
  $("#progress").value = summary.coverage_percent;
  $("#box-count").textContent = `${summary.total_expected} caixas`;
  $("#extra-count").textContent = `${summary.total_extra} divergentes`;
  const active = data.status === "EM_ABERTO" && Boolean(data.started_at);
  const awaitingFinalization = active && summary.can_finish;
  $("#carton-form").hidden = !active;
  $("#finish-button").hidden = !active;
  $("#finish-button").disabled = !summary.can_finish;
  $("#finish-button").classList.toggle("ready-to-finish", awaitingFinalization);
  $("#cancel-conference-button").hidden = !active;
  $("#completion-notice").hidden = !awaitingFinalization;
  $("#completion-notice").textContent = "100% conferido — aguardando finalização";
  $("#historical-notice").hidden = data.action !== "already_completed";
  $("#sync-button").hidden = data.status !== "FINALIZADA";
  $("#print-button").hidden = data.status !== "FINALIZADA";
  $("#authorize-reconference-button").hidden = !data.can_authorize_reconference;
  startTimer(data);
  return true;
}

function updateBoxElement(item, box, duplicateCodes = new Set()) {
  const confirmed = box.status === "CONFIRMED";
  item.className = confirmed ? "box-item confirmed" : "box-item pending";
  item.replaceChildren();
  const code = document.createElement("strong");
  code.textContent = normalizeCaixaEstoque(box.caixa_estoque);
  const state = document.createElement("span");
  state.textContent = duplicateCodes.has(normalizeCaixaEstoque(box.caixa_estoque))
    ? "DUPLICADO"
    : confirmed ? `Conferida${box.confirmed_at ? ` às ${box.confirmed_at}` : ""}` : "FALTA";
  item.append(code, state);
}

function renderExpectedBoxes(boxes, data) {
  boxElements.clear();
  const duplicateCodes = new Set(
    (data.divergences || [])
      .filter((item) => item.type === "DUPLICIDADE")
      .map((item) => normalizeCaixaEstoque(item.caixa_estoque)),
  );
  const fragment = document.createDocumentFragment();
  for (const box of boxes) {
    const item = document.createElement("li");
    item.dataset.caixaEstoque = normalizeCaixaEstoque(box.caixa_estoque);
    updateBoxElement(item, box, duplicateCodes);
    boxElements.set(normalizeCaixaEstoque(box.caixa_estoque), item);
    fragment.append(item);
  }
  $("#carton-list").replaceChildren(fragment);
}

function renderExtras(extras) {
  const items = extras.map((box) => {
    const item = document.createElement("li");
    item.className = "extra-item pending";
    const code = document.createElement("strong");
    code.textContent = normalizeCaixaEstoque(box.caixa_estoque);
    const status = document.createElement("span");
    status.textContent = "SOBRA";
    item.append(code, status);
    return item;
  });
  $("#extra-list").replaceChildren(...items);
}

export function render(data) {
  if (!renderSummary(data)) {
    $("#carton-list").replaceChildren();
    $("#extra-list").replaceChildren();
    return;
  }
  renderExpectedBoxes(data.cartons, data);
  renderExtras(data.unexpected_cartons || []);
  if (data.status === "EM_ABERTO") $("#carton-code").focus();
}

function applyScanResult(data) {
  renderSummary(data);
  const box = data.cartons.find(
    (candidate) => normalizeCaixaEstoque(candidate.caixa_estoque)
      === normalizeCaixaEstoque(data.caixa_estoque),
  );
  if (box) {
    const item = boxElements.get(normalizeCaixaEstoque(box.caixa_estoque));
    if (item) {
      const duplicateCodes = data.last_classification === "DUPLICATE"
        ? new Set([normalizeCaixaEstoque(data.expected_code)]) : new Set();
      updateBoxElement(item, box, duplicateCodes);
    }
  }
  renderExtras(data.unexpected_cartons || []);
}

async function renderAndStart(data) {
  render(data);
  if (data.status === "EM_ABERTO" && !data.started_at) {
    const started = await api(`/api/conferences/${encodeURIComponent(data.public_id)}/start`, {
      method: "POST",
      body: "{}",
    });
    render(started);
    return started;
  }
  return data;
}

export async function load(publicId, initialData = null) {
  const data = initialData || await api(`/api/conferences/${encodeURIComponent(publicId)}`);
  return renderAndStart(data);
}

export async function loadActiveConference() {
  const state = await api("/api/conferences/active");
  if (state.has_active_conference && state.conference) {
    return renderAndStart(state.conference);
  }
  showImportCard();
  return null;
}

function pendingMessage(error) {
  if (error.code !== "CONFERENCIA_COM_PENDENCIAS") return error.message;
  return `${error.message} Existem ${error.details.faltantes ?? 0} caixas faltantes.`;
}

export function bindConference() {
  const cartonForm = $("#carton-form");
  const input = $("#carton-code");
  const scanButton = cartonForm.querySelector("button");

  async function processScan(value) {
    const caixaEstoque = normalizeCaixaEstoque(value);
    if (scanning || !conference || conference.status !== "EM_ABERTO" || !caixaEstoque) return;
    scanning = true;
    input.disabled = true;
    scanButton.disabled = true;
    const sequence = ++scanSequence;
    try {
      const result = await api(`/api/conferences/${encodeURIComponent(conference.public_id)}/scan`, {
        method: "POST",
        body: JSON.stringify({ caixa_estoque: caixaEstoque }),
      });
      if (sequence !== scanSequence) return;
      applyScanResult(result);
      input.value = "";
      notify(result.message, result.result === "CONFERIDA" ? "success" : result.result === "DUPLICADA" ? "warning" : "error");
    } catch (error) {
      if (sequence === scanSequence) {
        notify(error.message, "error");
        input.select();
      }
    } finally {
      if (sequence === scanSequence) {
        scanning = false;
        input.disabled = false;
        scanButton.disabled = false;
        input.focus();
      }
    }
  }

  function scheduleAutomaticScan() {
    clearTimeout(scanTimer);
    const value = normalizeCaixaEstoque(input.value);
    if (!value || scanning) return;
    scanTimer = setTimeout(() => processScan(value), AUTO_SCAN_DELAY_MS);
  }

  cartonForm.addEventListener("submit", (event) => {
    event.preventDefault();
    clearTimeout(scanTimer);
    processScan(input.value);
  });
  input.addEventListener("input", scheduleAutomaticScan);
  input.addEventListener("paste", () => setTimeout(scheduleAutomaticScan, 0));

  const finishModal = $("#finish-modal");
  $("#finish-button").addEventListener("click", () => {
    if (conference?.status === "EM_ABERTO") finishModal.showModal();
  });
  $("#finish-cancel").addEventListener("click", () => finishModal.close());
  $("#finish-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (finishing || !conference) return;
    finishing = true;
    $("#finish-confirm").disabled = true;
    try {
      const result = await api(`/api/conferences/${encodeURIComponent(conference.public_id)}/finish`, { method: "POST", body: "{}" });
      finishModal.close();
      showImportCard();
      notify(result.message);
      $("#new-import-button").focus();
    } catch (error) {
      notify(error instanceof ApiError ? pendingMessage(error) : error.message, "error");
      input.focus();
    } finally {
      finishing = false;
      $("#finish-confirm").disabled = false;
    }
  });

  const cancelModal = $("#cancel-conference-modal");
  $("#cancel-conference-button").addEventListener("click", () => cancelModal.showModal());
  $("#cancel-conference-dismiss").addEventListener("click", () => cancelModal.close());
  $("#cancel-conference-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (cancelling || !conference) return;
    cancelling = true;
    $("#cancel-conference-confirm").disabled = true;
    try {
      const result = await api(`/api/conferences/${encodeURIComponent(conference.public_id)}/cancel`, { method: "POST", body: "{}" });
      cancelModal.close();
      render(result);
      notify(result.message, "success");
      $("#new-import-button").focus();
    } catch (error) {
      notify(error.message, "error");
    } finally {
      cancelling = false;
      $("#cancel-conference-confirm").disabled = false;
    }
  });

  $("#sync-button").addEventListener("click", async () => {
    try {
      notify((await api(`/api/conferences/${encodeURIComponent(conference.public_id)}/sync`, { method: "POST", body: "{}" })).message);
    } catch (error) {
      notify(error.message, "error");
    }
  });
  $("#print-button").addEventListener("click", () => window.print());

  const reconferenceModal = $("#reconference-modal");
  $("#authorize-reconference-button").addEventListener("click", () => {
    if (conference?.status === "FINALIZADA") reconferenceModal.showModal();
  });
  $("#reconference-dismiss").addEventListener("click", () => reconferenceModal.close());
  $("#reconference-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const reason = $("#reconference-reason").value.trim();
    if (authorizingReconference || reason.length < 10 || !conference) return;
    authorizingReconference = true;
    $("#reconference-confirm").disabled = true;
    try {
      const result = await api(`/api/conferences/${encodeURIComponent(conference.public_id)}/reconference`, {
        method: "POST", body: JSON.stringify({ justificativa: reason }),
      });
      reconferenceModal.close();
      $("#reconference-reason").value = "";
      render(result);
      notify(result.message, "success");
    } catch (error) {
      notify(error.message, "error");
    } finally {
      authorizingReconference = false;
      $("#reconference-confirm").disabled = false;
    }
  });
}
