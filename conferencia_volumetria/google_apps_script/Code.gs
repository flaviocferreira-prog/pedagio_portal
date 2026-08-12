const TABLE_KEYS = Object.freeze({
  CONFERENCIAS: 'ID_CONFERENCIA',
  CAIXAS_CONFERENCIA: 'ID_ITEM',
  BIPAGENS: 'ID_EVENTO',
  DIVERGENCIAS: 'ID_DIVERGENCIA',
  TENTATIVAS: 'ID_TENTATIVA',
  AUDITORIA: 'ID_AUDITORIA',
  COLABORADORES: 'ID_COLABORADOR',
});

function doPost(event) {
  const lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    const payload = JSON.parse(event.postData && event.postData.contents || '{}');
    const expectedToken = PropertiesService.getScriptProperties().getProperty('GOOGLE_SYNC_TOKEN');
    if (!expectedToken || payload.token !== expectedToken) return response_({ success: false, error: 'TOKEN_INVALIDO' });
    if (!payload.tables || !Array.isArray(payload.conference_ids)) return response_({ success: false, error: 'PAYLOAD_INVALIDO' });

    const spreadsheet = SpreadsheetApp.getActiveSpreadsheet();
    const totals = { inserted: 0, already_existing: 0 };
    let existingConferenceIds = [];
    Object.keys(TABLE_KEYS).forEach((sheetName) => {
      const records = Array.isArray(payload.tables[sheetName]) ? payload.tables[sheetName] : [];
      const result = upsertBatch_(spreadsheet.getSheetByName(sheetName), sheetName, records);
      totals.inserted += result.inserted;
      totals.already_existing += result.already_existing;
      if (sheetName === 'CONFERENCIAS') existingConferenceIds = result.existing_keys;
    });
    return response_({
      success: true,
      confirmed_conference_ids: payload.conference_ids.map(String),
      inserted: totals.inserted,
      already_existing_conference_ids: existingConferenceIds,
    });
  } catch (error) {
    return response_({ success: false, error: String(error && error.message || error) });
  } finally {
    lock.releaseLock();
  }
}

function upsertBatch_(sheet, sheetName, records) {
  if (!sheet) throw new Error(`ABA_AUSENTE:${sheetName}`);
  const key = TABLE_KEYS[sheetName];
  const existingHeaders = sheet.getLastColumn() ? sheet.getRange(1, 1, 1, sheet.getLastColumn()).getDisplayValues()[0] : [];
  const headers = mergeHeaders_(existingHeaders, key, records);
  if (headers.length !== existingHeaders.length || existingHeaders.some((value, index) => value !== headers[index])) {
    sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
  }
  if (!records.length) return { inserted: 0, already_existing: 0, existing_keys: [] };
  const keyIndex = headers.indexOf(key);
  const existingKeys = new Set();
  if (sheet.getLastRow() > 1) {
    sheet.getRange(2, keyIndex + 1, sheet.getLastRow() - 1, 1).getDisplayValues().flat().forEach((value) => existingKeys.add(String(value)));
  }
  const existingRecordKeys = records.filter((record) => existingKeys.has(String(record[key]))).map((record) => String(record[key]));
  const newRows = records.filter((record) => !existingKeys.has(String(record[key]))).map((record) => headers.map((header) => asText_(record[header])));
  if (newRows.length) sheet.getRange(sheet.getLastRow() + 1, 1, newRows.length, headers.length).setValues(newRows);
  return { inserted: newRows.length, already_existing: records.length - newRows.length, existing_keys: existingRecordKeys };
}

function mergeHeaders_(existing, key, records) {
  const values = existing.filter(String);
  if (!values.includes(key)) values.unshift(key);
  records.forEach((record) => Object.keys(record).forEach((header) => {
    if (!values.includes(header)) values.push(header);
  }));
  return values;
}

function asText_(value) {
  if (value === null || value === undefined) return '';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

function response_(body) {
  return ContentService.createTextOutput(JSON.stringify(body)).setMimeType(ContentService.MimeType.JSON);
}
