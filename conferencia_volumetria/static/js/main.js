import { api } from "./api-client.js";
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

const initialLoad = loadActiveConference();

initialLoad.catch((error) => notify(error.message, "error"));
