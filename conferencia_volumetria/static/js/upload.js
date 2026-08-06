import { api } from "./api-client.js";
import { notify } from "./notifications.js";
import { load } from "./conference.js";

const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
const modal = document.querySelector("#import-modal");
const form = document.querySelector("#import-form");
const fileInput = document.querySelector("#pallet-file");
const confirmButton = document.querySelector("#import-confirm");
const closeButton = document.querySelector("#import-close");
const summary = document.querySelector("#import-summary");
const dropzone = document.querySelector("#import-dropzone");
const automaticFileNotice = document.querySelector("#automatic-file");
let origin = "";
let operation = "";
let importing = false;
let automaticFile = null;
let fileSelectionTouched = false;

async function base64(file) {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  for (let index = 0; index < bytes.length; index += 32768) binary += String.fromCharCode(...bytes.subarray(index, index + 32768));
  return btoa(binary);
}

function collaboratorSummary() {
  try { return JSON.parse(sessionStorage.getItem("conference_collaborator") || "{}"); } catch { return {}; }
}

function validFile(file) {
  return file instanceof File && /\.(xlsx|csv)$/i.test(file.name) && file.size <= MAX_UPLOAD_BYTES;
}

function updateModal() {
  const file = fileInput.files?.[0];
  const fileReady = validFile(file) || Boolean(automaticFile);
  if (!fileReady) { origin = ""; operation = ""; }
  const originReady = fileReady;
  const operationReady = fileReady && Boolean(origin);
  const selectedFilename = file?.name || automaticFile?.filename || "";
  document.querySelectorAll("[data-origin]").forEach((button) => { button.disabled = !originReady; button.classList.toggle("selected", button.dataset.origin === origin); });
  document.querySelectorAll("[data-operation]").forEach((button) => { button.disabled = !operationReady; button.classList.toggle("selected", button.dataset.operation === operation); });
  document.querySelector("#origin-choices").setAttribute("aria-disabled", String(!originReady));
  document.querySelector("#operation-choices").setAttribute("aria-disabled", String(!operationReady));
  const collaborator = collaboratorSummary();
  const ready = fileReady && origin && operation && collaborator.nome && ["T1", "T2", "T3", "ADM"].includes(collaborator.turno);
  confirmButton.disabled = !ready || importing;
  summary.textContent = ready
    ? `Origem: ${origin === "PORTAL" ? "Portal" : "TL"} | Operação: ${operation === "NIKESTORE" ? "Nike Store" : operation} | Arquivo: ${selectedFilename} | Colaborador: ${collaborator.nome} | Turno: ${collaborator.turno}`
    : "Selecione o arquivo, a origem e a operação para revisar a importação.";
}

function resetModal() {
  origin = ""; operation = ""; importing = false; automaticFile = null; fileSelectionTouched = false; form.reset();
  automaticFileNotice.textContent = "Buscando o relatório mais recente do WMS na pasta Downloads...";
  closeButton.disabled = false; confirmButton.textContent = "Importar e iniciar conferência"; updateModal();
}

function setFile(file) {
  if (!validFile(file)) {
    automaticFile = null; fileSelectionTouched = true;
    automaticFileNotice.textContent = "Selecione um arquivo .xlsx ou .csv de até 10 MB.";
    updateModal(); notify("Selecione um arquivo .xlsx ou .csv de até 10 MB.", "error"); return;
  }
  const transfer = new DataTransfer(); transfer.items.add(file); fileInput.files = transfer.files;
  automaticFile = null; fileSelectionTouched = true; automaticFileNotice.textContent = "Arquivo selecionado manualmente."; updateModal();
  notify("Arquivo selecionado com sucesso.", "success", { id: "file-selected" });
}

async function findLatestAutomaticFile() {
  try {
    const result = await api("/api/conferences/latest-wms-report");
    if (!result.found) {
      automaticFileNotice.textContent = "Nenhum relatório recente do WMS foi encontrado na pasta Downloads. Selecione o arquivo manualmente.";
      return;
    }
    if (fileSelectionTouched || fileInput.files?.[0]) return;
    automaticFile = result;
    automaticFileNotice.textContent = `Arquivo mais recente encontrado: Nome: ${result.filename} | Baixado em: ${result.downloaded_at}`;
    notify("Arquivo mais recente localizado.", "info", { id: "automatic-file-found" });
  } catch {
    automaticFileNotice.textContent = "Nenhum relatório recente do WMS foi encontrado na pasta Downloads. Selecione o arquivo manualmente.";
  } finally {
    updateModal();
  }
}

export function bindUpload() {
  document.querySelector("#new-import-button").addEventListener("click", () => { resetModal(); modal.showModal(); findLatestAutomaticFile(); });
  closeButton.addEventListener("click", () => { if (!importing) modal.close(); });
  modal.addEventListener("cancel", (event) => { if (importing) event.preventDefault(); });
  document.querySelectorAll("[data-origin]").forEach((button) => button.addEventListener("click", () => { if (button.disabled) return; origin = button.dataset.origin; operation = ""; updateModal(); }));
  document.querySelectorAll("[data-operation]").forEach((button) => button.addEventListener("click", () => { operation = button.dataset.operation; updateModal(); }));
  fileInput.addEventListener("change", () => { const file = fileInput.files?.[0]; if (file) setFile(file); else { automaticFile = null; fileSelectionTouched = true; automaticFileNotice.textContent = "Selecione um arquivo para liberar as próximas etapas."; updateModal(); notify("Selecione um arquivo para continuar.", "attention", { id: "file-required" }); } });
  ["dragenter", "dragover"].forEach((eventName) => dropzone.addEventListener(eventName, (event) => { event.preventDefault(); dropzone.classList.add("dragging"); }));
  ["dragleave", "drop"].forEach((eventName) => dropzone.addEventListener(eventName, (event) => { event.preventDefault(); dropzone.classList.remove("dragging"); }));
  dropzone.addEventListener("drop", (event) => setFile(event.dataTransfer.files[0]));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const file = fileInput.files?.[0];
    if (!origin) { notify("Origem é obrigatória.", "error"); return; }
    if (!operation) { notify("Operação é obrigatória.", "error"); return; }
    if (!validFile(file) && !automaticFile) { notify("Arquivo é obrigatório.", "error"); return; }
    if (importing) return;
    importing = true; confirmButton.disabled = true; closeButton.disabled = true; confirmButton.textContent = "Importando...";
    try {
      const created = automaticFile && !file
        ? await api("/api/conferences/import-automatic", { method: "POST", body: JSON.stringify({ automatic_file_id: automaticFile.file_id, origin, operation }) })
        : await api("/api/conferences", { method: "POST", body: JSON.stringify({ filename: file.name, content_base64: await base64(file), origin, operation }) });
      modal.close(); resetModal(); notify(created.message); await load(created.public_id, created);
    } catch (error) {
      notify(error.message, "error");
    } finally {
      importing = false; closeButton.disabled = false; confirmButton.textContent = "Importar e iniciar conferência"; updateModal();
    }
  });
}
