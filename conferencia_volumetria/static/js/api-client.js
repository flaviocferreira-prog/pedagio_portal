export class ApiError extends Error {
  constructor(message, code = "REQUEST_ERROR", details = {}, status = 0) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.details = details;
    this.status = status;
  }
}

export async function api(url, options = {}) {
  let response;
  const headers = new Headers(options.headers || {});
  if (options.body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  try {
    response = await fetch(url, {
      credentials: "same-origin",
      ...options,
      headers,
    });
  } catch {
    throw new ApiError("Não foi possível conectar ao servidor.", "NETWORK_ERROR");
  }

  let body;
  try {
    body = await response.json();
  } catch {
    throw new ApiError(
      "Resposta inválida do servidor.",
      "INVALID_SERVER_RESPONSE",
      {},
      response.status,
    );
  }

  if (!response.ok || !body.success) {
    const error = body.error || {};
    const apiError = new ApiError(
      error.message || "Operação não concluída.",
      error.code || "REQUEST_ERROR",
      error.details || {},
      response.status,
    );
    if (
      apiError.code === "COLABORADOR_NAO_IDENTIFICADO"
      && apiError.details.redirect_url
    ) {
      window.location.assign(apiError.details.redirect_url);
    }
    throw apiError;
  }
  return body.data;
}
