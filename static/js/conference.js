import { api, ApiError } from "./api-client.js";
import { notify } from "./notifications.js";
import { startTimer } from "./timer.js";

let conference = null;
let scanning = false;
let finishing = false;
let restarting = false;
let cancelling = false;
let scanSequence = 0;
let scanTimer = null;
const boxElements = new Map();
const $ = (selector) => document.querySelector(selector);
const AUTO_SCAN_DELAY_MS = 160;

function isActiveConference(data) {
  return data && ["READY", "IN_PROGRESS"].includes(data.status);
}

export function showImportCard() {
  conference = null;
  sessionStorage.removeItem("conference_public_id");
  $("#conference").hidden = true;
  $("#upload-card").hidden = false;
  startTimer({});
}

function normalizeCaixaEstoque(value) {
  return String(value ?? "").trim();
}

function renderSummary(data) {
  if (!data || data.workflow_status === "CANCELADA") {
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
  $("#summary-origin").textContent = importation.origin || "Não informada";
  $("#summary-operation").textContent = importation.operation === "NIKESTORE" ? "Nike Store" : (importation.operation || "Não informada");
  $("#summary-shift").textContent = importation.shift || "Não informado";
  $("#summary-imported-at").textContent = importation.imported_at || data.created_at || "";
  const finalization = data.finalization || {};
  $("#summary-finalized-wrap").hidden = !finalization.finished_at;
  $("#summary-finalized-at").textContent = finalization.finished_at || "";
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
  const active = data.status === "IN_PROGRESS";
  const awaitingFinalization = data.workflow_status === "AGUARDANDO_FINALIZACAO";
  $("#carton-form").hidden = !active;
  $("#finish-button").hidden = !active;
  $("#finish-button").classList.toggle("ready-to-finish", awaitingFinalization);
  $("#restart-button").hidden = !active;
  $("#cancel-conference-button").hidden = !active;
  $("#completion-notice").hidden = !awaitingFinalization;
  $("#sync-button").hidden = data.workflow_status !== "FINALIZADA";
  startTimer(data);
  return true;
}

function updateBoxElement(item, box) {
  const confirmed = box.status === "CONFIRMED";
  item.className = confirmed ? "box-item confirmed" : "box-item pending";
  item.replaceChildren();
  const code = document.createElement("strong");
  code.textContent = normalizeCaixaEstoque(box.caixa_estoque);
  const state = document.createElement("span");
  state.textContent = confirmed
    ? `Conferida${box.confirmed_at ? ` às ${box.confirmed_at}` : ""}`
    : "Pendente";
  item.append(code, state);
}

function renderExpectedBoxes(boxes) {
  boxElements.clear();
  const fragment = document.createDocumentFragment();
  for (const box of boxes) {
    const item = document.createElement("li");
    item.dataset.caixaEstoque = normalizeCaixaEstoque(box.caixa_estoque);
    updateBoxElement(item, box);
    boxElements.set(normalizeCaixaEstoque(box.caixa_estoque), item);
    fragment.append(item);
  }
  $("#carton-list").replaceChildren(fragment);
}

function renderExtras(extras) {
  const items = extras.map((box) => {
    const item = document.createElement("li");
    item.className = "extra-item";
    const code = document.createElement("strong");
    code.textContent = normalizeCaixaEstoque(box.caixa_estoque);
    const attempts = document.createElement("span");
    attempts.textContent = box.attempts > 1 ? `${box.attempts} leituras` : "Código não esperado";
    item.append(code, attempts);
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
  renderExpectedBoxes(data.cartons);
  renderExtras(data.unexpected_cartons);
  if (data.status === "IN_PROGRESS") $("#carton-code").focus();
}

function applyScanResult(data) {
  renderSummary(data);
  const box = data.cartons.find(
    (candidate) => normalizeCaixaEstoque(candidate.caixa_estoque)
      === normalizeCaixaEstoque(data.caixa_estoque),
  );
  if (box) {
    const item = boxElements.get(normalizeCaixaEstoque(box.caixa_estoque));
    if (item) updateBoxElement(item, box);
  }
  renderExtras(data.unexpected_cartons);
}

async function renderAndStart(data) {
  render(data);
  if (data.status === "READY") {
    await new Promise((resolve) => requestAnimationFrame(resolve));
    const started = await api(`/api/conferences/${encodeURIComponent(data.public_id)}/start`, { method: "POST", body: "{}" });
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
  if (state.latest_conference) {
    render(state.latest_conference);
    return state.latest_conference;
  }
  if (!state.has_active_conference || !state.conference) {
    showImportCard();
    return null;
  }
  return null;
}

function pendingMessage(error) {
  if (error.code !== "CONFERENCIA_COM_PENDENCIAS") return error.message;
  return `${error.message} Existem ${error.details.faltantes ?? 0} caixas faltantes e ${error.details.divergentes ?? 0} caixas divergentes.`;
}

export function bindConference() {
  const cartonForm = $("#carton-form");
  const input = $("#carton-code");
  const scanButton = cartonForm.querySelector("button");

  async function processScan(value) {
    const caixaEstoque = normalizeCaixaEstoque(value);
    if (scanning || !conference || conference.status !== "IN_PROGRESS" || !caixaEstoque) return;
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
    if (conference?.status === "IN_PROGRESS") finishModal.showModal();
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
      render(result);
      notify(result.message);
    } catch (error) {
      notify(error instanceof ApiError ? pendingMessage(error) : error.message, "error");
      input.focus();
    } finally {
      finishing = false;
      $("#finish-confirm").disabled = false;
    }
  });

  const restartModal = $("#restart-modal");
  $("#restart-button").addEventListener("click", () => restartModal.showModal());
  $("#restart-cancel").addEventListener("click", () => restartModal.close());
  $("#restart-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (restarting || !conference) return;
    restarting = true;
    $("#restart-confirm").disabled = true;
    try {
      restartModal.close();
      render(await api(`/api/conferences/${encodeURIComponent(conference.public_id)}/restart`, { method: "POST", body: "{}" }));
      notify("Conferência reiniciada. Uma nova tentativa foi criada.");
    } catch (error) {
      notify(error.message, "error");
    } finally {
      restarting = false;
      $("#restart-confirm").disabled = false;
      input.focus();
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
      sessionStorage.removeItem("conference_public_id");
      showImportCard();
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
}
