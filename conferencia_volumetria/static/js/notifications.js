const DURATIONS = { success: 3000, info: 4000, attention: 5000, error: 7000 };
const TYPE_ALIASES = { warning: "attention" };
const TYPE_ICONS = { success: "✓", info: "i", attention: "!", error: "×" };
const activeToasts = new Map();

function container() {
  let element = document.querySelector("#toast-container");
  if (!element) {
    element = document.createElement("div");
    element.id = "toast-container";
    element.setAttribute("aria-live", "polite");
    element.setAttribute("aria-atomic", "false");
    document.body.append(element);
  }
  return element;
}

function removeToast(key) {
  const entry = activeToasts.get(key);
  if (!entry) return;
  if (entry.timer) clearTimeout(entry.timer);
  activeToasts.delete(key);
  entry.element.remove();
}

function scheduleRemoval(key, duration) {
  const entry = activeToasts.get(key);
  if (!entry || duration === false) return;
  if (entry.timer) clearTimeout(entry.timer);
  entry.timer = window.setTimeout(() => removeToast(key), duration);
}

export function showNotification(type, message, options = {}) {
  if (!message) return null;
  const normalizedType = TYPE_ALIASES[type] || (DURATIONS[type] ? type : "info");
  const key = options.id || `${normalizedType}:${message}`;
  const duration = options.autoClose === false ? false : (options.duration ?? DURATIONS[normalizedType]);
  const existing = activeToasts.get(key);
  if (existing) { scheduleRemoval(key, duration); return existing.element; }

  const element = document.createElement("article");
  element.className = `toast toast--${normalizedType}`;
  element.setAttribute("role", normalizedType === "error" ? "alert" : "status");
  const icon = document.createElement("span");
  icon.className = "toast__icon";
  icon.setAttribute("aria-hidden", "true");
  icon.textContent = TYPE_ICONS[normalizedType];
  const text = document.createElement("span");
  text.className = "toast__message";
  text.textContent = message;
  const close = document.createElement("button");
  close.className = "toast__close";
  close.type = "button";
  close.setAttribute("aria-label", "Fechar notificação");
  close.textContent = "×";
  close.addEventListener("click", () => removeToast(key));
  element.append(icon, text, close);
  container().prepend(element);
  activeToasts.set(key, { element, timer: null });
  scheduleRemoval(key, duration);
  return element;
}

export function notify(message, type = "success", options = {}) {
  return showNotification(type, message, options);
}
