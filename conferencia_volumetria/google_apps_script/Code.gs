const TIME_ZONE = 'America/Sao_Paulo';
const CONFERENCES_SHEET = 'CONFERENCIAS';
const BOXES_SHEET = 'CAIXAS_CONFERENCIA';
const SCANS_SHEET = 'BIPAGENS';

const CONFERENCE_HEADERS = [
  'ID_CONFERENCIA', 'ASSINATURA_ARQUIVO', 'PALETE', 'NOME_ARQUIVO',
  'MATRICULA', 'COLABORADOR', 'TURNO', 'IMPORTADO_EM', 'INICIADO_EM',
  'FINALIZADO_EM', 'DURACAO_SEGUNDOS', 'TOTAL_ESPERADAS', 'TOTAL_BIPAGENS',
  'TOTAL_OK', 'TOTAL_FALTAS', 'TOTAL_SOBRAS', 'TOTAL_DUPLICADAS',
  'STATUS_CONFERENCIA', 'COMPUTADOR', 'STATUS_SINCRONIZACAO', 'SINCRONIZADO_EM'
];

const BOX_HEADERS = [
  'ID_ITEM', 'ID_CONFERENCIA', 'SEQUENCIA', 'CODIGO_ORIGINAL',
  'CODIGO_NORMALIZADO', 'DS_CLASSE', 'STATUS_FINAL', 'BIPADO_EM', 'ORDEM_BIPAGEM'
];

const SCAN_HEADERS = [
  'ID_EVENTO', 'ID_CONFERENCIA', 'ORDEM', 'CODIGO_LIDO',
  'CODIGO_NORMALIZADO', 'RESULTADO', 'BIPADO_EM'
];

function doGet(event) {
  try {
    const properties = PropertiesService.getScriptProperties();
    const spreadsheetId = properties.getProperty('PLANILHA_ID');
    const secretConfigured = Boolean(properties.getProperty('SYNC_SECRET'));
    if (!spreadsheetId || !secretConfigured) {
      return htmlPage_(
        'CONFIGURAÇÃO PENDENTE',
        '<p>Configure PLANILHA_ID e SYNC_SECRET nas propriedades do script.</p>'
      );
    }
    const spreadsheet = SpreadsheetApp.openById(spreadsheetId);
    return htmlPage_(
      'INTEGRAÇÃO DISPONÍVEL',
      '<p>Planilha acessível: <strong>' + escapeHtml_(spreadsheet.getName()) + '</strong>.</p>'
    );
  } catch (error) {
    console.error('Falha no diagnóstico da integração: ' + safeError_(error));
    return htmlPage_(
      'INTEGRAÇÃO INDISPONÍVEL',
      '<p>Não foi possível acessar a planilha configurada.</p>'
    );
  }
}

function doPost(event) {
  let parentOrigin = '';
  let returnUrl = '';
  let nonce = '';
  let conferenceId = '';
  let attemptId = '';
  try {
    const parameters = event && event.parameter ? event.parameter : {};
    if (String(parameters.modo || '') !== 'popup') {
      throw new Error('GOOGLE_SYNC_FAILED');
    }
    const payloadText = String(parameters.payload || '');
    const receivedSignature = String(parameters.assinatura || '').trim().toLowerCase();
    conferenceId = String(parameters.id_conferencia || '');
    attemptId = String(parameters.attempt_id || '');
    parentOrigin = validateParentOrigin_(parameters.parent_origin);
    returnUrl = validateReturnUrl_(parameters.return_url, parentOrigin);
    nonce = String(parameters.nonce || '');
    const popupToken = String(parameters.popup_token || '').trim().toLowerCase();
    if (!payloadText || !receivedSignature || !conferenceId || !parentOrigin || !returnUrl
        || !/^[A-Za-z0-9_-]{20,200}$/.test(nonce) || !/^[A-Za-z0-9_-]{10,200}$/.test(attemptId) || !popupToken) {
      throw new Error('GOOGLE_PAYLOAD_INVALID');
    }

    const properties = PropertiesService.getScriptProperties();
    const secret = String(properties.getProperty('SYNC_SECRET') || '');
    const spreadsheetId = String(properties.getProperty('PLANILHA_ID') || '');
    if (!secret || !spreadsheetId) {
      throw new Error('GOOGLE_APPS_SCRIPT_UNAVAILABLE');
    }
    const calculatedSignature = hmacHex_(payloadText, secret);
    if (!constantTimeEquals_(calculatedSignature, receivedSignature)) {
      throw new Error('GOOGLE_SIGNATURE_INVALID');
    }

    let payload;
    try {
      payload = JSON.parse(payloadText);
    } catch (error) {
      throw new Error('GOOGLE_PAYLOAD_INVALID');
    }
    validatePayload_(payload);
    if (String(payload.conferencia.id_conferencia) !== conferenceId) {
      throw new Error('GOOGLE_CONFERENCE_ID_MISMATCH');
    }
    const popupText = 'POPUP|' + conferenceId + '|' + parentOrigin + '|' + returnUrl + '|' + nonce;
    if (!constantTimeEquals_(hmacHex_(popupText, secret), popupToken)) {
      throw new Error('GOOGLE_TOKEN_INVALID');
    }
    validateCorporateUser_();

    const lock = LockService.getScriptLock();
    lock.waitLock(30000);
    try {
      let result;
      try {
        result = performSynchronization_(spreadsheetId, secret, payload);
      } catch (sheetError) {
        console.error('Falha ao gravar no Sheets: ' + safeError_(sheetError));
        throw new Error('GOOGLE_SHEETS_WRITE_FAILED');
      }
      return popupRedirectPage_(result, returnUrl, nonce, attemptId);
    } finally {
      lock.releaseLock();
    }
  } catch (error) {
    console.error('Falha na sincronização: ' + safeError_(error));
    const known = [
      'GOOGLE_AUTH_REQUIRED', 'GOOGLE_DOMAIN_NOT_ALLOWED', 'GOOGLE_APPS_SCRIPT_UNAVAILABLE',
      'GOOGLE_PAYLOAD_INVALID', 'GOOGLE_SIGNATURE_INVALID', 'GOOGLE_TOKEN_INVALID',
      'GOOGLE_CONFERENCE_ID_MISMATCH', 'GOOGLE_SHEETS_WRITE_FAILED', 'SYNC_RECONCILIATION_REQUIRED'
    ];
    const errorCode = known.indexOf(safeError_(error)) >= 0 ? safeError_(error) : 'GOOGLE_SYNC_FAILED';
    return popupErrorPage_(
      conferenceId, attemptId, parentOrigin, returnUrl, nonce, errorCode,
      errorCode === 'GOOGLE_AUTH_REQUIRED'
        ? 'Entre na conta Google corporativa e tente novamente.'
        : 'O Google Sheets não concluiu a gravação.'
    );
  }
}

function validateCorporateUser_() {
  const email = String(Session.getActiveUser().getEmail() || '').trim().toLowerCase();
  if (!email) {
    throw new Error('GOOGLE_AUTH_REQUIRED');
  }
  if (!/@fisia\.com\.br$/.test(email)) throw new Error('GOOGLE_DOMAIN_NOT_ALLOWED');
}

function performSynchronization_(spreadsheetId, secret, payload) {
  const spreadsheet = SpreadsheetApp.openById(spreadsheetId);
  const conferencesSheet = ensureSheet_(spreadsheet, CONFERENCES_SHEET, CONFERENCE_HEADERS);
  const boxesSheet = ensureSheet_(spreadsheet, BOXES_SHEET, BOX_HEADERS);
  const scansSheet = ensureSheet_(spreadsheet, SCANS_SHEET, SCAN_HEADERS);
  const conference = payload.conferencia;
  const conferenceId = String(conference.id_conferencia);
  const existing = findConference_(conferencesSheet, conferenceId);
  let synchronizedAt;
  let message;
  let savedBoxes = 0;
  let savedScans = 0;

  if (existing) {
    if (!matchesExistingConference_(conferencesSheet, existing.row, conference)) {
      throw new Error('SYNC_RECONCILIATION_REQUIRED');
    }
    synchronizedAt = existing.synchronizedAt || saoPauloTimestamp_();
    if (!existing.synchronizedAt) {
      conferencesSheet.getRange(existing.row, 21).setValue(synchronizedAt);
    }
    message = 'A conferência já estava sincronizada.';
  } else {
    removeRowsByConference_(boxesSheet, conferenceId, 2);
    removeRowsByConference_(scansSheet, conferenceId, 2);
    const boxRows = payload.caixas.map(boxRow_);
    const scanRows = payload.bipagens.map(scanRow_);
    appendRows_(boxesSheet, boxRows, [4, 5]);
    appendRows_(scansSheet, scanRows, [4, 5]);
    savedBoxes = boxRows.length;
    savedScans = scanRows.length;
    synchronizedAt = saoPauloTimestamp_();
    appendRows_(conferencesSheet, [conferenceRow_(conference, synchronizedAt)], [1, 2, 3, 4, 5]);
    message = 'Conferência gravada com sucesso.';
  }

  SpreadsheetApp.flush();

  const receiptText = conferenceId + '|SINCRONIZADO|' + synchronizedAt;
  const receiptSignature = hmacHex_(receiptText, secret);
  return {
    id_conferencia: conferenceId,
    caixas_salvas: existing ? payload.caixas.length : savedBoxes,
    bipagens_salvas: existing ? payload.bipagens.length : savedScans,
    sincronizado_em: synchronizedAt,
    already_existing: Boolean(existing),
    message: message,
    receipt: {
      id_conferencia: conferenceId,
      status: 'SINCRONIZADO',
      sincronizado_em: synchronizedAt,
      assinatura_recibo: receiptSignature
    }
  };
}

function validatePayload_(payload) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new Error('Estrutura do payload inválida.');
  }
  if (!payload.conferencia || !Array.isArray(payload.caixas) || !Array.isArray(payload.bipagens)) {
    throw new Error('Payload incompleto.');
  }
  const conference = payload.conferencia;
  const conferenceId = String(conference.id_conferencia || '');
  if (!/^[A-Za-z0-9_-]{1,100}$/.test(conferenceId)) {
    throw new Error('ID da conferência inválido.');
  }
  if (conference.status_conferencia !== 'FINALIZADA') {
    throw new Error('Somente conferências finalizadas podem ser sincronizadas.');
  }
  const requiredConferenceFields = [
    'assinatura_arquivo', 'palete', 'nome_arquivo', 'matricula', 'colaborador',
    'turno', 'importado_em', 'iniciado_em', 'finalizado_em', 'duracao_segundos',
    'total_esperadas', 'total_bipagens', 'total_ok', 'total_faltas',
    'total_sobras', 'total_duplicadas', 'computador'
  ];
  requiredConferenceFields.forEach(function(field) {
    if (!Object.prototype.hasOwnProperty.call(conference, field)) {
      throw new Error('Campo obrigatório ausente na conferência.');
    }
  });
  payload.caixas.forEach(function(box) {
    if (!box || String(box.id_conferencia || '') !== conferenceId
        || !box.id_item || !Object.prototype.hasOwnProperty.call(box, 'codigo_original')
        || !Object.prototype.hasOwnProperty.call(box, 'codigo_normalizado')) {
      throw new Error('Caixa inválida no payload.');
    }
  });
  payload.bipagens.forEach(function(scan) {
    if (!scan || String(scan.id_conferencia || '') !== conferenceId
        || !scan.id_evento || !Object.prototype.hasOwnProperty.call(scan, 'codigo_lido')
        || !Object.prototype.hasOwnProperty.call(scan, 'codigo_normalizado')) {
      throw new Error('Bipagem inválida no payload.');
    }
  });
}

function conferenceRow_(item, synchronizedAt) {
  return [
    String(item.id_conferencia), String(item.assinatura_arquivo), String(item.palete),
    String(item.nome_arquivo), String(item.matricula), String(item.colaborador),
    String(item.turno), String(item.importado_em), String(item.iniciado_em),
    String(item.finalizado_em), number_(item.duracao_segundos), number_(item.total_esperadas),
    number_(item.total_bipagens), number_(item.total_ok), number_(item.total_faltas),
    number_(item.total_sobras), number_(item.total_duplicadas),
    String(item.status_conferencia), String(item.computador), 'SINCRONIZADO', synchronizedAt
  ];
}

function boxRow_(item) {
  return [
    String(item.id_item), String(item.id_conferencia), number_(item.sequencia),
    String(item.codigo_original), String(item.codigo_normalizado), String(item.ds_classe || 'NÃO INFORMADO'),
    String(item.status_final), String(item.bipado_em || ''), number_(item.ordem_bipagem)
  ];
}

function scanRow_(item) {
  return [
    String(item.id_evento), String(item.id_conferencia), number_(item.ordem),
    String(item.codigo_lido), String(item.codigo_normalizado),
    String(item.resultado), String(item.bipado_em || '')
  ];
}

function ensureSheet_(spreadsheet, name, headers) {
  const sheet = spreadsheet.getSheetByName(name) || spreadsheet.insertSheet(name);
  if (sheet.getLastRow() === 0) {
    sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
    sheet.setFrozenRows(1);
  } else {
    const currentHeaders = sheet.getRange(1, 1, 1, headers.length).getDisplayValues()[0];
    if (currentHeaders.join('\u001f') !== headers.join('\u001f')) {
      throw new Error('Cabeçalhos incompatíveis na aba ' + name + '.');
    }
  }
  return sheet;
}

function appendRows_(sheet, rows, textColumns) {
  if (!rows.length) return;
  const startRow = sheet.getLastRow() + 1;
  textColumns.forEach(function(column) {
    sheet.getRange(startRow, column, rows.length, 1).setNumberFormat('@');
  });
  sheet.getRange(startRow, 1, rows.length, rows[0].length).setValues(rows);
}

function findConference_(sheet, conferenceId) {
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return null;
  const ids = sheet.getRange(2, 1, lastRow - 1, 1).getDisplayValues();
  for (let index = 0; index < ids.length; index += 1) {
    if (String(ids[index][0]) === conferenceId) {
      const row = index + 2;
      return {
        row: row,
        synchronizedAt: String(sheet.getRange(row, 21).getDisplayValue() || '')
      };
    }
  }
  return null;
}

function matchesExistingConference_(sheet, row, conference) {
  const values = sheet.getRange(row, 1, 1, 21).getDisplayValues()[0];
  return String(values[0]) === String(conference.id_conferencia)
    && String(values[1]) === String(conference.assinatura_arquivo)
    && String(values[4]) === String(conference.matricula)
    && String(values[9]) === String(conference.finalizado_em)
    && String(values[11]) === String(conference.total_esperadas)
    && String(values[12]) === String(conference.total_bipagens);
}

function removeRowsByConference_(sheet, conferenceId, idColumn) {
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return;
  const ids = sheet.getRange(2, idColumn, lastRow - 1, 1).getDisplayValues();
  for (let index = ids.length - 1; index >= 0; index -= 1) {
    if (String(ids[index][0]) === conferenceId) {
      sheet.deleteRow(index + 2);
    }
  }
}

function hmacHex_(text, secret) {
  const bytes = Utilities.computeHmacSha256Signature(
    String(text), String(secret), Utilities.Charset.UTF_8
  );
  return bytes.map(function(value) {
    const unsigned = value < 0 ? value + 256 : value;
    return ('0' + unsigned.toString(16)).slice(-2);
  }).join('');
}

function constantTimeEquals_(first, second) {
  first = String(first);
  second = String(second);
  let difference = first.length ^ second.length;
  const length = Math.max(first.length, second.length);
  for (let index = 0; index < length; index += 1) {
    difference |= (first.charCodeAt(index) || 0) ^ (second.charCodeAt(index) || 0);
  }
  return difference === 0;
}

function saoPauloTimestamp_() {
  return Utilities.formatDate(new Date(), TIME_ZONE, "yyyy-MM-dd'T'HH:mm:ssXXX");
}

function validateParentOrigin_(value) {
  const origin = String(value || '').trim();
  return /^https?:\/\/[A-Za-z0-9.\-\[\]:]+$/.test(origin) ? origin : '';
}

function validateReturnUrl_(value, parentOrigin) {
  const returnUrl = String(value || '').trim();
  return returnUrl === parentOrigin + '/sincronizacao/confirmar/' ? returnUrl : '';
}

function popupRedirectPage_(result, returnUrl, nonce, attemptId) {
  const receipt = result.receipt;
  const fields = {
    conference_id: receipt.id_conferencia,
    attempt_id: attemptId,
    status: 'SUCCESS',
    sincronizado_em: receipt.sincronizado_em,
    assinatura_recibo: receipt.assinatura_recibo,
    ja_sincronizado: result.already_existing ? 'true' : 'false',
    nonce: nonce
  };
  const query = Object.keys(fields).map(function(key) {
    return encodeURIComponent(key) + '=' + encodeURIComponent(fields[key]);
  }).join('&');
  const completeUrl = returnUrl + '?' + query;
  const safeUrl = JSON.stringify(completeUrl).replace(/</g, '\\u003c');
  const inputs = Object.keys(fields).map(function(key) {
    return '<input type="hidden" name="' + escapeHtml_(key) + '" value="'
      + escapeHtml_(fields[key]) + '">';
  }).join('');
  const html = [
    '<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">',
    '<meta name="viewport" content="width=device-width,initial-scale=1">',
    '<title>Sincronização concluída</title></head><body>',
    '<main><h1>Sincronização concluída</h1>',
    '<p>', escapeHtml_(result.message), '</p>',
    '<p>Confirmando o recibo no sistema local...</p>',
    '<form id="local-confirmation" method="get" action="', escapeHtml_(returnUrl), '" target="_top">',
    inputs, '</form></main>',
    '<script>(function(){"use strict";',
    'const url=', safeUrl, ';',
    'document.getElementById("local-confirmation").submit();',
    'setTimeout(function(){window.top.location.href=url;},300);',
    '})();<\/script></body></html>'
  ].join('');
  return HtmlService.createHtmlOutput(html).setTitle('Sincronização concluída');
}

function popupErrorPage_(conferenceId, attemptId, parentOrigin, returnUrl, nonce, code, message) {
  if (conferenceId && attemptId && parentOrigin && returnUrl && nonce) {
    return popupResultRedirectPage_({
      conference_id: conferenceId, attempt_id: attemptId, status: 'ERROR', nonce: nonce,
      error_code: code, message: message
    }, returnUrl);
  }
  return htmlPage_('SINCRONIZAÇÃO NÃO CONCLUÍDA', '<p>' + escapeHtml_(message) + '</p><script>setTimeout(function(){window.close();},700);</script>');
}

function popupResultRedirectPage_(fields, returnUrl) {
  const query = Object.keys(fields).map(function(key) {
    return encodeURIComponent(key) + '=' + encodeURIComponent(String(fields[key] || ''));
  }).join('&');
  const completeUrl = returnUrl + '?' + query;
  const safeUrl = JSON.stringify(completeUrl).replace(/</g, '\\u003c');
  const inputs = Object.keys(fields).map(function(key) {
    return '<input type="hidden" name="' + escapeHtml_(key) + '" value="'
      + escapeHtml_(String(fields[key] || '')) + '">';
  }).join('');
  return HtmlService.createHtmlOutput(
    '<!doctype html><meta charset="utf-8"><main><p>Retornando o resultado ao sistema...</p>'
    + '<form id="local-result" method="get" action="' + escapeHtml_(returnUrl) + '" target="_top">'
    + inputs + '</form><script>(function(){document.getElementById("local-result").submit();'
    + 'setTimeout(function(){window.top.location.href=' + safeUrl + ';},300);})();<\/script></main>'
  ).setTitle('Resultado da sincronização');
}

function htmlPage_(title, body) {
  return HtmlService.createHtmlOutput(
    '<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">'
    + '<meta name="viewport" content="width=device-width,initial-scale=1">'
    + '<title>' + escapeHtml_(title) + '</title><style>'
    + 'body{font:16px Arial,sans-serif;background:#f4f6f8;color:#152536;margin:0;padding:32px}'
    + 'main{max-width:680px;margin:auto;background:#fff;padding:32px;border-radius:12px;box-shadow:0 8px 30px #0002}'
    + 'h1{font-size:24px}dt{font-weight:700;margin-top:12px}dd{margin:4px 0}'
    + '.button{display:inline-block;background:#005eb8;color:#fff;text-decoration:none;padding:12px 18px;border-radius:6px;font-weight:700}'
    + '</style></head><body><main><h1>' + escapeHtml_(title) + '</h1>' + body + '</main></body></html>'
  ).setTitle(title);
}

function escapeHtml_(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function number_(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : 0;
}

function safeError_(error) {
  return error && error.message ? String(error.message) : 'Erro não identificado.';
}
