/**
 * PORTAL PEDAGIO - Web App de sincronizacao.
 *
 * Script Properties obrigatorias:
 *   SPREADSHEET_ID
 *   SYNC_SHARED_SECRET
 *
 * O contrato e exclusivamente JSON. O segredo nunca deve ser colocado no
 * HTML/JavaScript do portal; somente o backend Python envia estas chamadas.
 */

var SERVICE_NAME = "apps-script";
var MAX_BATCH_SIZE = 100;
var LOCK_TIMEOUT_MS = 5000;

var SHEET_ORDER = [
  "CONFERENCIAS",
  "ITENS",
  "BIPAGENS",
  "TENTATIVAS",
  "CONFIRMACOES",
  "DIVERGENCIAS",
  "SOBRAS_RESUMO",
  "SOBRAS",
  "TRATAMENTOS_SOBRA",
  "ITENS_TRATAMENTO",
  "AUDITORIA"
];

var SHEET_DEFINITIONS = {
  CONFERENCIAS: {
    source: "conference",
    fields: [
      "id_conferencia", "local_id", "codigo_palete", "source_filename",
      "status", "conference_status", "created_at", "started_at",
      "finished_at", "cancelled_at", "updated_at", "duration_seconds",
      "productivity_boxes_per_hour", "total_expected", "total_confirmed",
      "total_missing", "total_scans", "total_duplicates", "total_surpluses",
      "pending_surpluses", "pallet_class", "final_justification",
      "source_fingerprint", "content_hash", "import_agenda", "import_origin",
      "import_operation", "imported_shift", "imported_at", "print_status",
      "is_reconference", "previous_id_conferencia", "reconference_reason",
      "reconference_authorized_at", "reconference_authorized_by",
      "collaborator_registration", "collaborator_name", "collaborator_shift",
      "started_by", "finished_by", "cancelled_by"
    ],
    textFields: [
      "id_conferencia", "local_id", "codigo_palete", "source_filename",
      "source_fingerprint", "content_hash", "import_agenda",
      "previous_id_conferencia", "reconference_authorized_by",
      "collaborator_registration", "started_by", "finished_by", "cancelled_by"
    ]
  },
  ITENS: {
    source: "items",
    fields: [
      "id_conferencia", "local_id", "caixa_estoque", "ds_classe", "status",
      "confirmed_at", "confirmed_by", "confirmed_attempt"
    ],
    textFields: ["id_conferencia", "local_id", "caixa_estoque", "confirmed_by"]
  },
  BIPAGENS: {
    source: "scans",
    fields: [
      "id_conferencia", "local_id", "caixa_estoque", "classification",
      "scanned_at", "attempt_number", "collaborator_registration",
      "collaborator_name"
    ],
    textFields: [
      "id_conferencia", "local_id", "caixa_estoque", "collaborator_registration"
    ]
  },
  TENTATIVAS: {
    source: "attempts",
    fields: [
      "id_conferencia", "local_id", "attempt_number", "status", "started_at",
      "finished_at", "created_at", "started_by", "restarted_by", "finished_by"
    ],
    textFields: [
      "id_conferencia", "local_id", "started_by", "restarted_by", "finished_by"
    ]
  },
  CONFIRMACOES: {
    source: "confirmations",
    fields: [
      "id_conferencia", "local_id", "attempt_number", "item_local_id",
      "caixa_estoque", "collaborator_registration", "collaborator_name",
      "confirmed_at"
    ],
    textFields: [
      "id_conferencia", "local_id", "item_local_id", "caixa_estoque",
      "collaborator_registration"
    ]
  },
  DIVERGENCIAS: {
    source: "divergences",
    fields: [
      "id_conferencia", "local_id", "attempt_number", "divergence_type",
      "caixa_estoque", "description", "status", "found_at", "found_by",
      "resolved_at", "resolved_by", "resolution_note"
    ],
    textFields: [
      "id_conferencia", "local_id", "caixa_estoque", "found_by", "resolved_by"
    ]
  },
  SOBRAS_RESUMO: {
    source: "surplus_summaries",
    fields: [
      "id_conferencia", "local_id", "attempt_number", "caixa_estoque",
      "first_seen_at", "last_seen_at", "attempts", "first_scanned_by"
    ],
    textFields: [
      "id_conferencia", "local_id", "caixa_estoque", "first_scanned_by"
    ]
  },
  SOBRAS: {
    source: "surpluses",
    fields: [
      "id_conferencia", "local_id", "attempt_number", "unexpected_carton_id",
      "scan_event_id", "caixa_estoque", "scanned_at", "scan_order",
      "treatment_status", "corrected_by_registration", "corrected_by_name",
      "corrected_at", "treatment_method"
    ],
    textFields: [
      "id_conferencia", "local_id", "unexpected_carton_id", "scan_event_id",
      "caixa_estoque", "corrected_by_registration"
    ]
  },
  TRATAMENTOS_SOBRA: {
    source: "treatments",
    fields: [
      "id_conferencia", "local_id", "attempt_number", "collaborator_registration",
      "collaborator_name", "surplus_count", "confirmed_at",
      "final_conference_status"
    ],
    textFields: ["id_conferencia", "local_id", "collaborator_registration"]
  },
  ITENS_TRATAMENTO: {
    source: "treatment_items",
    fields: [
      "id_conferencia", "local_id", "confirmation_id", "unexpected_carton_id",
      "caixa_estoque", "scanned_at", "scan_order"
    ],
    textFields: [
      "id_conferencia", "local_id", "confirmation_id", "unexpected_carton_id",
      "caixa_estoque"
    ]
  },
  AUDITORIA: {
    source: "audit",
    fields: [
      "id_conferencia", "local_id", "registration", "profile", "ip_address",
      "action", "content_hash", "justification", "result", "created_at",
      "collaborator_name"
    ],
    textFields: [
      "id_conferencia", "local_id", "registration", "ip_address", "content_hash"
    ]
  }
};

/**
 * O health check autenticado tambem usa POST. GET nunca devolve HTML.
 */
function doGet() {
  return jsonResponse_({
    ok: false,
    service: SERVICE_NAME,
    error: {
      code: "METHOD_NOT_ALLOWED",
      message: "Utilize uma requisicao POST autenticada."
    }
  });
}

function doPost(event) {
  try {
    var request = parseRequest_(event);
    authenticate_(request);
    var action = String(request.action || "").trim().toLowerCase();
    if (action === "health") {
      return jsonResponse_(handleHealth_());
    }
    if (action === "sync") {
      return jsonResponse_(handleSync_(request));
    }
    throw appError_("INVALID_ACTION", "Acao de integracao invalida.");
  } catch (error) {
    return jsonResponse_(errorResponse_(error));
  }
}

function handleHealth_() {
  return withScriptLock_(function() {
    var spreadsheet = openSpreadsheet_();
    ensureSheets_(spreadsheet);
    SpreadsheetApp.flush();
    return {
      ok: true,
      service: SERVICE_NAME,
      sheets: true,
      checked_at: new Date().toISOString(),
      tabs: SHEET_ORDER.slice()
    };
  });
}

function handleSync_(request) {
  var conferences = validateBatchEnvelope_(request.conferences);
  return withScriptLock_(function() {
    var spreadsheet = openSpreadsheet_();
    var sheets = ensureSheets_(spreadsheet);
    var snapshots = snapshotSheets_(sheets);
    var resultById = Object.create(null);
    var pendingWrites = [];

    conferences.forEach(function(payload) {
      var conferenceId = String(payload.id_conferencia);
      try {
        validateConferencePayload_(payload);
        var committedState = committedConferenceState_(snapshots, payload);
        if (committedState === "COMPLETE") {
          resultById[conferenceId] = {
            id_conferencia: conferenceId,
            status: "ALREADY_EXISTS"
          };
          return;
        }
        if (committedState === "CONFLICT") {
          throw appError_(
            "EXISTING_DATA_MISMATCH",
            "O ID_CONFERENCIA ja existe com outro conteudo."
          );
        }
        pendingWrites.push(payload);
      } catch (error) {
        resultById[conferenceId] = {
          id_conferencia: conferenceId,
          status: "ERROR",
          error_code: publicErrorCode_(error)
        };
      }
    });

    if (pendingWrites.length) {
      try {
        synchronizeConferences_(sheets, snapshots, pendingWrites);
        pendingWrites.forEach(function(payload) {
          var conferenceId = String(payload.id_conferencia);
          resultById[conferenceId] = {
            id_conferencia: conferenceId,
            status: "INSERTED"
          };
        });
      } catch (error) {
        pendingWrites.forEach(function(payload) {
          var conferenceId = String(payload.id_conferencia);
          try {
            var retrySnapshots = snapshotSheets_(sheets);
            var retryState = committedConferenceState_(retrySnapshots, payload);
            if (retryState === "COMPLETE") {
              resultById[conferenceId] = {
                id_conferencia: conferenceId,
                status: "ALREADY_EXISTS"
              };
              return;
            }
            if (retryState === "CONFLICT") {
              throw appError_(
                "EXISTING_DATA_MISMATCH",
                "O ID_CONFERENCIA ja existe com outro conteudo."
              );
            }
            synchronizeConferences_(sheets, retrySnapshots, [payload]);
            resultById[conferenceId] = {
              id_conferencia: conferenceId,
              status: "INSERTED"
            };
          } catch (individualError) {
            resultById[conferenceId] = {
              id_conferencia: conferenceId,
              status: "ERROR",
              error_code: publicErrorCode_(individualError)
            };
          }
        });
      }
    }

    var results = conferences.map(function(payload) {
      return resultById[String(payload.id_conferencia)];
    });

    var inserted = countStatus_(results, "INSERTED");
    var alreadyExists = countStatus_(results, "ALREADY_EXISTS");
    var errors = results.filter(function(item) {
      return item.status === "ERROR";
    }).map(function(item) {
      return {id_conferencia: item.id_conferencia, code: item.error_code};
    });

    return {
      ok: true,
      service: SERVICE_NAME,
      sheets: true,
      received: conferences.length,
      inserted: inserted,
      already_exists: alreadyExists,
      errors: errors,
      results: results
    };
  });
}

function synchronizeConferences_(sheets, snapshots, payloads) {
  var conferenceIds = payloads.map(function(payload) {
    return String(payload.id_conferencia);
  });
  removeConferenceRows_(sheets, snapshots, conferenceIds);
  appendRelationsBatch_(sheets, payloads);
  SpreadsheetApp.flush();
  appendConferencesBatch_(sheets.CONFERENCIAS, payloads);
  SpreadsheetApp.flush();
}

function appendConferencesBatch_(sheet, payloads) {
  var definition = SHEET_DEFINITIONS.CONFERENCIAS;
  var rows = payloads.map(function(payload) {
    return rowValues_(definition, payload.id_conferencia, payload.conference);
  });
  appendRows_(sheet, definition, rows);
}

function appendRelationsBatch_(sheets, payloads) {
  SHEET_ORDER.slice(1).forEach(function(sheetName) {
    var definition = SHEET_DEFINITIONS[sheetName];
    var rows = [];
    payloads.forEach(function(payload) {
      payload[definition.source].forEach(function(item) {
        rows.push(rowValues_(definition, payload.id_conferencia, item));
      });
    });
    appendRows_(sheets[sheetName], definition, rows);
  });
}

function appendRows_(sheet, definition, rows) {
  if (!rows.length) {
    return;
  }
  var startRow = Math.max(2, sheet.getLastRow() + 1);
  ensureGridSize_(
    sheet,
    startRow + rows.length - 1,
    definition.fields.length
  );
  var range = sheet.getRange(startRow, 1, rows.length, definition.fields.length);
  var textRanges = definition.textFields.map(function(field) {
    var column = definition.fields.indexOf(field) + 1;
    var columnName = columnName_(column);
    return columnName + startRow + ":" + columnName + (startRow + rows.length - 1);
  });
  if (textRanges.length) {
    sheet.getRangeList(textRanges).setNumberFormat("@");
  }
  range.setValues(rows);
}

function columnName_(column) {
  var name = "";
  var remaining = column;
  while (remaining > 0) {
    remaining -= 1;
    name = String.fromCharCode(65 + (remaining % 26)) + name;
    remaining = Math.floor(remaining / 26);
  }
  return name;
}

function rowValues_(definition, conferenceId, source) {
  return definition.fields.map(function(field) {
    if (field === "id_conferencia") {
      return safeCellValue_(conferenceId);
    }
    return safeCellValue_(source[field]);
  });
}

function safeCellValue_(value) {
  var normalized = cellValue_(value);
  if (typeof normalized === "string" && /^\s*[=+\-@]/.test(normalized)) {
    // O apostrofo faz o Sheets manter o conteudo como texto, sem executar
    // formulas recebidas de arquivos ou campos preenchidos por usuarios.
    return "'" + normalized;
  }
  return normalized;
}

function cellValue_(value) {
  if (value === null || typeof value === "undefined") {
    return "";
  }
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return value;
  }
  throw appError_("INVALID_PAYLOAD", "O lote possui um valor nao escalar.");
}

function validateBatchEnvelope_(value) {
  if (!Array.isArray(value)) {
    throw appError_("INVALID_PAYLOAD", "O campo conferences deve ser uma lista.");
  }
  if (value.length > MAX_BATCH_SIZE) {
    throw appError_("BATCH_TOO_LARGE", "O lote excede o limite permitido.");
  }
  var seen = Object.create(null);
  value.forEach(function(payload) {
    if (!isPlainObject_(payload)) {
      throw appError_("INVALID_PAYLOAD", "Conferencia invalida no lote.");
    }
    var conferenceId = validateConferenceId_(payload.id_conferencia);
    if (Object.prototype.hasOwnProperty.call(seen, conferenceId)) {
      throw appError_("DUPLICATE_CONFERENCE", "O lote repete um ID_CONFERENCIA.");
    }
    seen[conferenceId] = true;
  });
  return value;
}

function validateConferencePayload_(payload) {
  var conferenceId = validateConferenceId_(payload.id_conferencia);
  if (!isPlainObject_(payload.conference)) {
    throw appError_("INVALID_PAYLOAD", "Dados da conferencia ausentes.");
  }
  validateObjectFields_(payload.conference, SHEET_DEFINITIONS.CONFERENCIAS.fields);
  if (String(payload.conference.id_conferencia) !== conferenceId) {
    throw appError_("CONFERENCE_ID_MISMATCH", "ID_CONFERENCIA divergente no payload.");
  }
  if (String(payload.conference.conference_status) !== "FINALIZADA") {
    throw appError_("CONFERENCE_NOT_FINISHED", "Somente conferencias finalizadas podem ser sincronizadas.");
  }

  SHEET_ORDER.slice(1).forEach(function(sheetName) {
    var definition = SHEET_DEFINITIONS[sheetName];
    var rows = payload[definition.source];
    if (!Array.isArray(rows)) {
      throw appError_("INVALID_PAYLOAD", "Relacao ausente: " + definition.source + ".");
    }
    var seenLocalIds = {};
    rows.forEach(function(row) {
      if (!isPlainObject_(row)) {
        throw appError_("INVALID_PAYLOAD", "Linha relacional invalida.");
      }
      validateObjectFields_(row, definition.fields);
      var localId = String(row.local_id === null || typeof row.local_id === "undefined" ? "" : row.local_id).trim();
      if (!localId) {
        throw appError_("INVALID_CHILD_ID", "Registro relacionado sem identificador local.");
      }
      var compositeKey = conferenceId + "\u001f" + localId;
      if (Object.prototype.hasOwnProperty.call(seenLocalIds, compositeKey)) {
        throw appError_("DUPLICATE_CHILD", "Registro relacionado duplicado no lote.");
      }
      seenLocalIds[compositeKey] = true;
    });
  });
}

function validateObjectFields_(object, fields) {
  fields.forEach(function(field) {
    if (field === "id_conferencia") {
      return;
    }
    if (!Object.prototype.hasOwnProperty.call(object, field)) {
      throw appError_("INVALID_PAYLOAD", "Campo obrigatorio ausente: " + field + ".");
    }
    cellValue_(object[field]);
  });
}

function validateConferenceId_(value) {
  var conferenceId = String(value === null || typeof value === "undefined" ? "" : value).trim();
  if (!/^[A-Za-z0-9_-]{1,120}$/.test(conferenceId)) {
    throw appError_("INVALID_CONFERENCE_ID", "ID_CONFERENCIA invalido.");
  }
  return conferenceId;
}

function ensureSheets_(spreadsheet) {
  var sheets = {};
  SHEET_ORDER.forEach(function(sheetName) {
    var definition = SHEET_DEFINITIONS[sheetName];
    var sheet = spreadsheet.getSheetByName(sheetName);
    if (!sheet) {
      sheet = spreadsheet.insertSheet(sheetName);
    }
    ensureHeader_(sheet, definition);
    sheets[sheetName] = sheet;
  });
  return sheets;
}

function ensureHeader_(sheet, definition) {
  var expected = definition.fields.map(function(field) {
    return field.toUpperCase();
  });
  if (sheet.getLastRow() === 0) {
    ensureGridSize_(sheet, 1, expected.length);
    sheet.getRange(1, 1, 1, expected.length).setValues([expected]);
    sheet.getRange(1, 1, 1, expected.length).setFontWeight("bold");
    sheet.setFrozenRows(1);
    return;
  }
  var lastColumn = sheet.getLastColumn();
  if (lastColumn !== expected.length) {
    throw appError_("SHEET_SCHEMA_MISMATCH", "Uma aba possui cabecalho incompativel.");
  }
  var actual = sheet.getRange(1, 1, 1, expected.length).getDisplayValues()[0];
  for (var index = 0; index < expected.length; index += 1) {
    if (String(actual[index]).trim() !== expected[index]) {
      throw appError_("SHEET_SCHEMA_MISMATCH", "Uma aba possui cabecalho incompativel.");
    }
  }
  sheet.setFrozenRows(1);
}

function ensureGridSize_(sheet, requiredRows, requiredColumns) {
  var missingRows = requiredRows - sheet.getMaxRows();
  if (missingRows > 0) {
    sheet.insertRowsAfter(sheet.getMaxRows(), missingRows);
  }
  var missingColumns = requiredColumns - sheet.getMaxColumns();
  if (missingColumns > 0) {
    sheet.insertColumnsAfter(sheet.getMaxColumns(), missingColumns);
  }
}

function snapshotSheets_(sheets) {
  var snapshots = {};
  SHEET_ORDER.forEach(function(sheetName) {
    var sheet = sheets[sheetName];
    var lastRow = sheet.getLastRow();
    var rows = lastRow < 2
      ? []
      : sheet.getRange(2, 1, lastRow - 1, sheet.getLastColumn()).getDisplayValues();
    var byConference = Object.create(null);
    rows.forEach(function(values, index) {
      var conferenceId = String(values[0]);
      if (!Object.prototype.hasOwnProperty.call(byConference, conferenceId)) {
        byConference[conferenceId] = [];
      }
      byConference[conferenceId].push({row_number: index + 2, values: values});
    });
    snapshots[sheetName] = {by_conference: byConference};
  });
  return snapshots;
}

function committedConferenceState_(snapshots, payload) {
  var conferenceId = String(payload.id_conferencia);
  var conferenceRows = snapshotRows_(snapshots.CONFERENCIAS, conferenceId);
  if (!conferenceRows.length) {
    return "ABSENT";
  }
  if (conferenceRows.length !== 1) {
    return "CONFLICT";
  }

  var definition = SHEET_DEFINITIONS.CONFERENCIAS;
  var stored = conferenceRows[0].values;
  var identityFields = ["local_id", "content_hash", "source_fingerprint"];
  for (var identityIndex = 0; identityIndex < identityFields.length; identityIndex += 1) {
    var field = identityFields[identityIndex];
    var column = definition.fields.indexOf(field);
    if (column < 0 || String(stored[column]) !== String(cellValue_(payload.conference[field]))) {
      return "CONFLICT";
    }
  }

  for (var sheetIndex = 1; sheetIndex < SHEET_ORDER.length; sheetIndex += 1) {
    var sheetName = SHEET_ORDER[sheetIndex];
    var childDefinition = SHEET_DEFINITIONS[sheetName];
    var existingRows = snapshotRows_(snapshots[sheetName], conferenceId);
    var existingIds = existingRows.map(function(row) {
      return String(row.values[1]);
    }).sort();
    var expectedIds = payload[childDefinition.source].map(function(row) {
      return String(row.local_id);
    }).sort();
    if (existingIds.length !== expectedIds.length) {
      return "INCOMPLETE";
    }
    for (var rowIndex = 0; rowIndex < expectedIds.length; rowIndex += 1) {
      if (existingIds[rowIndex] !== expectedIds[rowIndex]) {
        return "INCOMPLETE";
      }
    }
  }
  return "COMPLETE";
}

function snapshotRows_(snapshot, conferenceId) {
  return snapshot && Object.prototype.hasOwnProperty.call(
    snapshot.by_conference,
    conferenceId
  ) ? snapshot.by_conference[conferenceId] : [];
}

function removeConferenceRows_(sheets, snapshots, conferenceIds) {
  SHEET_ORDER.forEach(function(sheetName) {
    var matchingRows = [];
    conferenceIds.forEach(function(conferenceId) {
      snapshotRows_(snapshots[sheetName], conferenceId).forEach(function(row) {
        matchingRows.push(row.row_number);
      });
    });
    deleteRowsBottomUp_(sheets[sheetName], matchingRows);
  });
}

function deleteRowsBottomUp_(sheet, rowNumbers) {
  var matchingRows = rowNumbers.slice().sort(function(left, right) {
    return left - right;
  });
  if (!matchingRows.length) {
    return;
  }

  // Exclui blocos contiguos de baixo para cima para manter os indices estaveis.
  var blockEnd = matchingRows[matchingRows.length - 1];
  var blockStart = blockEnd;
  for (var index = matchingRows.length - 2; index >= -1; index -= 1) {
    var current = index >= 0 ? matchingRows[index] : null;
    if (current !== null && current === blockStart - 1) {
      blockStart = current;
      continue;
    }
    sheet.deleteRows(blockStart, blockEnd - blockStart + 1);
    if (current !== null) {
      blockStart = current;
      blockEnd = current;
    }
  }
}

function openSpreadsheet_() {
  var spreadsheetId = String(
    PropertiesService.getScriptProperties().getProperty("SPREADSHEET_ID") || ""
  ).trim();
  if (!spreadsheetId) {
    throw appError_("NOT_CONFIGURED", "A integracao ainda nao foi configurada.");
  }
  try {
    return SpreadsheetApp.openById(spreadsheetId);
  } catch (error) {
    throw appError_("SHEETS_UNAVAILABLE", "Nao foi possivel acessar a planilha configurada.");
  }
}

function authenticate_(request) {
  var configured = String(
    PropertiesService.getScriptProperties().getProperty("SYNC_SHARED_SECRET") || ""
  );
  if (!configured) {
    throw appError_("NOT_CONFIGURED", "A integracao ainda nao foi configurada.");
  }
  var received = String(request.shared_secret === null || typeof request.shared_secret === "undefined" ? "" : request.shared_secret);
  if (!constantTimeEquals_(configured, received)) {
    throw appError_("UNAUTHORIZED", "Credencial de integracao invalida.");
  }
}

function constantTimeEquals_(left, right) {
  var first = String(left);
  var second = String(right);
  var length = Math.max(first.length, second.length);
  var difference = first.length ^ second.length;
  for (var index = 0; index < length; index += 1) {
    var firstCode = index < first.length ? first.charCodeAt(index) : 0;
    var secondCode = index < second.length ? second.charCodeAt(index) : 0;
    difference |= firstCode ^ secondCode;
  }
  return difference === 0;
}

function withScriptLock_(operation) {
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(LOCK_TIMEOUT_MS)) {
    throw appError_("SYNC_BUSY", "Uma sincronizacao ja esta em andamento.");
  }
  try {
    return operation();
  } finally {
    lock.releaseLock();
  }
}

function parseRequest_(event) {
  var content = event && event.postData ? event.postData.contents : "";
  if (!content) {
    throw appError_("INVALID_JSON", "Corpo JSON ausente.");
  }
  var parsed;
  try {
    parsed = JSON.parse(content);
  } catch (error) {
    throw appError_("INVALID_JSON", "Corpo JSON invalido.");
  }
  if (!isPlainObject_(parsed)) {
    throw appError_("INVALID_JSON", "O corpo deve ser um objeto JSON.");
  }
  return parsed;
}

function isPlainObject_(value) {
  return value !== null && Object.prototype.toString.call(value) === "[object Object]";
}

function countStatus_(results, status) {
  return results.reduce(function(total, item) {
    return total + (item.status === status ? 1 : 0);
  }, 0);
}

function appError_(code, message) {
  var error = new Error(message);
  error.publicCode = code;
  return error;
}

function publicErrorCode_(error) {
  return error && error.publicCode ? String(error.publicCode) : "SHEETS_WRITE_FAILED";
}

function errorResponse_(error) {
  var code = publicErrorCode_(error);
  var messages = {
    INVALID_JSON: "A solicitacao recebida e invalida.",
    INVALID_ACTION: "A acao solicitada nao e suportada.",
    INVALID_PAYLOAD: "O lote recebido possui dados invalidos.",
    INVALID_CONFERENCE_ID: "O lote possui um ID_CONFERENCIA invalido.",
    DUPLICATE_CONFERENCE: "O lote repete uma conferencia.",
    BATCH_TOO_LARGE: "O lote excede o limite permitido.",
    UNAUTHORIZED: "Credencial de integracao invalida.",
    NOT_CONFIGURED: "A integracao ainda nao foi configurada.",
    SYNC_BUSY: "Uma sincronizacao ja esta em andamento.",
    SHEETS_UNAVAILABLE: "Nao foi possivel acessar o Google Sheets.",
    SHEET_SCHEMA_MISMATCH: "A estrutura das abas e incompativel.",
    EXISTING_DATA_MISMATCH: "O ID_CONFERENCIA ja existe com outro conteudo.",
    SHEETS_WRITE_FAILED: "Nao foi possivel concluir a gravacao."
  };
  return {
    ok: false,
    service: SERVICE_NAME,
    sheets: false,
    error: {
      code: code,
      message: messages[code] || messages.SHEETS_WRITE_FAILED
    }
  };
}

function jsonResponse_(payload) {
  return ContentService
    .createTextOutput(JSON.stringify(payload))
    .setMimeType(ContentService.MimeType.JSON);
}
