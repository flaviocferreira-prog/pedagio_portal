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
let origin = "";
let operation = "";
let importing = false;

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
  const originReady = Boolean(origin);
  document.querySelectorAll("[data-origin]").forEach((button) => button.classList.toggle("selected", button.dataset.origin === origin));
  document.querySelectorAll("[data-operation]").forEach((button) => { button.disabled = !originReady; button.classList.toggle("selected", button.dataset.operation === operation); });
  fileInput.disabled = !(origin && operation);
  const collaborator = collaboratorSummary();
  const ready = origin && operation && validFile(file) && collaborator.nome && ["T1", "T2", "T3", "ADM"].includes(collaborator.turno);
  confirmButton.disabled = !ready || importing;
  summary.textContent = ready
    ? `Origem: ${origin === "PORTAL" ? "Portal" : "TL"} | Operação: ${operation === "NIKESTORE" ? "Nike Store" : operation} | Arquivo: ${file.name} | Colaborador: ${collaborator.nome} | Turno: ${collaborator.turno}`
    : "Preencha origem, operação e arquivo para revisar a importação.";
}

function resetModal() {
  origin = ""; operation = ""; importing = false; form.reset(); closeButton.disabled = false; confirmButton.textContent = "Importar e iniciar conferência"; updateModal();
}

function setFile(file) {
  if (!validFile(file)) { notify("Selecione um arquivo .xlsx ou .csv de até 10 MB.", "error"); return; }
  const transfer = new DataTransfer(); transfer.items.add(file); fileInput.files = transfer.files; updateModal();
}

export function bindUpload() {
  document.querySelector("#new-import-button").addEventListener("click", () => { resetModal(); modal.showModal(); });
  closeButton.addEventListener("click", () => { if (!importing) modal.close(); });
  modal.addEventListener("cancel", (event) => { if (importing) event.preventDefault(); });
  document.querySelectorAll("[data-origin]").forEach((button) => button.addEventListener("click", () => { origin = button.dataset.origin; operation = ""; fileInput.value = ""; updateModal(); }));
  document.querySelectorAll("[data-operation]").forEach((button) => button.addEventListener("click", () => { operation = button.dataset.operation; updateModal(); }));
  fileInput.addEventListener("change", updateModal);
  ["dragenter", "dragover"].forEach((eventName) => dropzone.addEventListener(eventName, (event) => { event.preventDefault(); dropzone.classList.add("dragging"); }));
  ["dragleave", "drop"].forEach((eventName) => dropzone.addEventListener(eventName, (event) => { event.preventDefault(); dropzone.classList.remove("dragging"); }));
  dropzone.addEventListener("drop", (event) => setFile(event.dataTransfer.files[0]));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const file = fileInput.files?.[0];
    if (importing || !origin || !operation || !validFile(file)) return;
    importing = true; confirmButton.disabled = true; closeButton.disabled = true; confirmButton.textContent = "Importando...";
    try {
      const created = await api("/api/conferences", { method: "POST", body: JSON.stringify({ filename: file.name, content_base64: await base64(file), origin, operation }) });
      modal.close(); resetModal(); notify(created.message); await load(created.public_id, created);
    } catch (error) {
      notify(error.message, "error");
    } finally {
      importing = false; closeButton.disabled = false; confirmButton.textContent = "Importar e iniciar conferência"; updateModal();
    }
  });
}
