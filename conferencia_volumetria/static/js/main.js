import { api, routes } from "./api-client.js";
import { bindUpload } from "./upload.js";
import { bindConference, loadActiveConference } from "./conference.js";
import { notify } from "./notifications.js";

bindUpload();
bindConference();

document.querySelector("#logout-button").addEventListener("click", async () => {
  try {
    const result = await api("/api/logout", { method: "POST", body: "{}" });
    sessionStorage.removeItem("conference_public_id");
    window.location.assign(result.redirect_url);
  } catch (error) {
    notify(error.message, "error");
  }
});

const syncButton = document.querySelector("#sync-pending-button");
syncButton.addEventListener("click", async () => {
  if (syncButton.disabled) return;
  syncButton.disabled = true;
  try {
    const result = await api(routes.syncPending, { method: "POST", body: "{}" });
    const found = Number(result.found || 0);
    notify(
      found
        ? `Sincronização concluída\nConferências encontradas: ${found}\nSincronizadas: ${result.synchronized || 0}\nJá existentes no Google: ${result.already_existing || 0}\nCom erro: ${result.failed || 0}${result.error_message ? `\n${result.error_message}` : ""}`
        : "Nenhuma conferência pendente de sincronização.",
      Number(result.failed || 0) ? "attention" : "success",
    );
  } catch (error) {
    notify(`${error.message}\nOs dados permanecem salvos localmente para nova tentativa.`, "error");
  } finally {
    syncButton.disabled = false;
  }
});

loadActiveConference().catch((error) => notify(error.message, "error"));
