let timerHandle = null;

function serverDate(value) {
  if (!value) return null;
  const isoValue = value.includes("T") ? value : value.replace(" ", "T");
  return new Date(isoValue.endsWith("Z") ? isoValue : `${isoValue}Z`);
}

function formatDuration(totalSeconds) {
  const safeSeconds = Math.max(0, Math.floor(totalSeconds || 0));
  const hours = String(Math.floor(safeSeconds / 3600)).padStart(2, "0");
  const minutes = String(Math.floor((safeSeconds % 3600) / 60)).padStart(2, "0");
  const seconds = String(safeSeconds % 60).padStart(2, "0");
  return `${hours}:${minutes}:${seconds}`;
}

export function startTimer(conference) {
  clearInterval(timerHandle);
  timerHandle = null;
  const output = document.querySelector("#summary-timer");
  const startedAt = serverDate(conference.started_at);
  if (!startedAt || Number.isNaN(startedAt.getTime())) {
    output.textContent = "00:00:00";
    return;
  }
  const tick = () => {
    const elapsed = conference.finished_at
      ? conference.duration_seconds
      : (Date.now() - startedAt.getTime()) / 1000;
    output.textContent = formatDuration(elapsed);
  };
  tick();
  if (conference.status === "IN_PROGRESS") {
    timerHandle = window.setInterval(tick, 1000);
  }
}
