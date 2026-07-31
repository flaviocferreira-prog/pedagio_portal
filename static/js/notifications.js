export function notify(message, type = "success") {
  notifyElement("#notice", message, type);
}

export function notifyElement(selector, message, type = "success") {
  const box = document.querySelector(selector);
  if (!box) return;
  box.textContent = message;
  box.className = `notice${message ? ` ${type}` : ""}`;
}
