import { api, routes } from "./api-client.js";
import { notify } from "./notifications.js";

const button = document.querySelector("#sync-pending-button");
const count = document.querySelector("#sync-pending-count");
const status = document.querySelector("#sync-status");
const statusText = document.querySelector("#sync-status-text");
let requesting = false;

function renderSyncStatus(data = {}) {
  const pending = Math.max(0, Number(data.pending) || 0);
  const configured = data.configured !== false;
  const running = Boolean(data.running);
  const health = String(data.health || "unknown");

  count.textContent = pending > 99 ? "99+" : String(pending);
  count.hidden = pending === 0;
  button.disabled = requesting || running || !configured;

  if (requesting || running) {
    status.dataset.state = "running";
    statusText.textContent = "Sincronização em andamento...";
  } else if (!configured || health === "not_configured") {
    status.dataset.state = "not_configured";
    statusText.textContent = "Google Sheets não configurado";
  } else if (health === "unavailable") {
    status.dataset.state = "unavailable";
    statusText.textContent = pending ? `Google indisponível · ${pending} pendente(s)` : "Google Sheets indisponível";
  } else if (health === "unknown") {
    status.dataset.state = "unknown";
    statusText.textContent = "Verificando sincronização...";
  } else if (pending) {
    status.dataset.state = "pending";
    statusText.textContent = `${pending} conferência(s) pendente(s)`;
  } else {
    status.dataset.state = "healthy";
    statusText.textContent = "Tudo sincronizado";
  }
}

export async function loadSyncStatus() {
  try {
    const data = await api(routes.syncStatus);
    renderSyncStatus(data);
    return data;
  } catch (error) {
    status.dataset.state = "unavailable";
    statusText.textContent = "Status de sincronização indisponível";
    throw error;
  }
}

export function bindSync() {
  button.addEventListener("click", async () => {
    if (requesting) return;
    requesting = true;
    renderSyncStatus({ running: true });
    try {
      const result = await api(routes.syncPending, { method: "POST", body: "{}" });
      notify(result.message, result.failed || result.busy ? "attention" : "success");
    } catch (error) {
      notify(error.message, "error");
    } finally {
      requesting = false;
      loadSyncStatus().catch(() => {});
    }
  });

  window.addEventListener("sync-state-changed", () => {
    loadSyncStatus().catch(() => {});
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) loadSyncStatus().catch(() => {});
  });
  loadSyncStatus().catch(() => {});
}
