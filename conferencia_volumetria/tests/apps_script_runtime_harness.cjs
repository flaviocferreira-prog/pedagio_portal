"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");


class MemoryRange {
  constructor(sheet, row, column, rowCount, columnCount) {
    this.sheet = sheet;
    this.row = row;
    this.column = column;
    this.rowCount = rowCount;
    this.columnCount = columnCount;
  }

  setValues(rows) {
    assert.equal(rows.length, this.rowCount);
    if (
      this.sheet.rejectConferenceId
      && rows.some((values) => String(values[0]) === this.sheet.rejectConferenceId)
    ) {
      throw new Error("controlled sheet rejection");
    }
    this.sheet.setValuesCalls += 1;
    rows.forEach((values, rowOffset) => {
      assert.equal(values.length, this.columnCount);
      values.forEach((value, columnOffset) => {
        this.sheet.setCell(
          this.row + rowOffset,
          this.column + columnOffset,
          value
        );
      });
    });
    return this;
  }

  getDisplayValues() {
    const rows = [];
    for (let rowOffset = 0; rowOffset < this.rowCount; rowOffset += 1) {
      const values = [];
      for (
        let columnOffset = 0;
        columnOffset < this.columnCount;
        columnOffset += 1
      ) {
        const value = this.sheet.getCell(
          this.row + rowOffset,
          this.column + columnOffset
        );
        values.push(value === null || typeof value === "undefined" ? "" : String(value));
      }
      rows.push(values);
    }
    return rows;
  }

  setFontWeight() {
    return this;
  }

  setNumberFormat() {
    return this;
  }

  createTextFinder(searchValue) {
    const range = this;
    const finder = {
      matchEntire: false,
      caseSensitive: false,
      matchEntireCell(value) {
        this.matchEntire = Boolean(value);
        return this;
      },
      matchCase(value) {
        this.caseSensitive = Boolean(value);
        return this;
      },
      useRegularExpression() {
        return this;
      },
      findNext() {
        const target = String(searchValue);
        const values = range.getDisplayValues();
        for (const row of values) {
          for (const rawValue of row) {
            const candidate = String(rawValue);
            const left = this.caseSensitive ? candidate : candidate.toLowerCase();
            const right = this.caseSensitive ? target : target.toLowerCase();
            if (this.matchEntire ? left === right : left.includes(right)) {
              return {};
            }
          }
        }
        return null;
      },
    };
    return finder;
  }
}


class MemorySheet {
  constructor(name) {
    this.name = name;
    this.rows = [];
    this.maxRows = 1000;
    this.maxColumns = 26;
    this.frozenRows = 0;
    this.setValuesCalls = 0;
    this.rejectConferenceId = null;
  }

  setCell(row, column, value) {
    while (this.rows.length < row) {
      this.rows.push([]);
    }
    while (this.rows[row - 1].length < column) {
      this.rows[row - 1].push("");
    }
    this.rows[row - 1][column - 1] = value;
  }

  getCell(row, column) {
    const values = this.rows[row - 1];
    return values && column <= values.length ? values[column - 1] : "";
  }

  getLastRow() {
    for (let index = this.rows.length - 1; index >= 0; index -= 1) {
      if (this.rows[index].some((value) => value !== "" && value !== null)) {
        return index + 1;
      }
    }
    return 0;
  }

  getLastColumn() {
    let lastColumn = 0;
    for (const row of this.rows) {
      for (let index = row.length - 1; index >= 0; index -= 1) {
        if (row[index] !== "" && row[index] !== null) {
          lastColumn = Math.max(lastColumn, index + 1);
          break;
        }
      }
    }
    return lastColumn;
  }

  getMaxRows() {
    return this.maxRows;
  }

  getMaxColumns() {
    return this.maxColumns;
  }

  getRange(row, column, rowCount = 1, columnCount = 1) {
    return new MemoryRange(this, row, column, rowCount, columnCount);
  }

  getRangeList(_ranges) {
    return {
      setNumberFormat() {
        return this;
      },
    };
  }

  setFrozenRows(value) {
    this.frozenRows = value;
  }

  insertRowsAfter(_after, count) {
    this.maxRows += count;
  }

  insertColumnsAfter(_after, count) {
    this.maxColumns += count;
  }

  deleteRows(startRow, count) {
    this.rows.splice(startRow - 1, count);
  }

  dataRows() {
    return Math.max(0, this.getLastRow() - 1);
  }
}


class MemorySpreadsheet {
  constructor() {
    this.sheets = new Map();
  }

  getSheetByName(name) {
    return this.sheets.get(name) || null;
  }

  insertSheet(name) {
    const sheet = new MemorySheet(name);
    this.sheets.set(name, sheet);
    return sheet;
  }
}


function buildRuntime() {
  const spreadsheet = new MemorySpreadsheet();
  const runtime = {
    properties: {
      SPREADSHEET_ID: "spreadsheet-test",
      SYNC_SHARED_SECRET: "shared-test-secret",
    },
    spreadsheet,
    lockAvailable: true,
    lockAttempts: 0,
    lockReleases: 0,
    flushes: 0,
  };

  const context = {
    console,
    Date,
    JSON,
    Math,
    Object,
    String,
    Array,
    Error,
    PropertiesService: {
      getScriptProperties() {
        return {
          getProperty(name) {
            return Object.prototype.hasOwnProperty.call(runtime.properties, name)
              ? runtime.properties[name]
              : null;
          },
        };
      },
    },
    LockService: {
      getScriptLock() {
        return {
          tryLock() {
            runtime.lockAttempts += 1;
            return runtime.lockAvailable;
          },
          releaseLock() {
            runtime.lockReleases += 1;
          },
        };
      },
    },
    SpreadsheetApp: {
      openById(spreadsheetId) {
        if (spreadsheetId !== runtime.properties.SPREADSHEET_ID) {
          throw new Error("unknown spreadsheet");
        }
        return spreadsheet;
      },
      flush() {
        runtime.flushes += 1;
      },
    },
    ContentService: {
      MimeType: {JSON: "application/json"},
      createTextOutput(content) {
        return {
          content,
          mimeType: null,
          setMimeType(mimeType) {
            this.mimeType = mimeType;
            return this;
          },
        };
      },
    },
  };
  context.globalThis = context;
  vm.createContext(context);
  const codePath = path.resolve(__dirname, "..", "google_apps_script", "Code.gs");
  vm.runInContext(fs.readFileSync(codePath, "utf8"), context, {
    filename: codePath,
  });
  return {context, runtime};
}


function invoke(context, request) {
  const output = context.doPost({
    postData: {contents: JSON.stringify(request)},
  });
  assert.equal(output.mimeType, "application/json");
  return JSON.parse(output.content);
}


function completePayload() {
  const id = "CONF-NODE-001";
  return {
    id_conferencia: id,
    conference: {
      id_conferencia: id,
      local_id: 1,
      codigo_palete: id,
      source_filename: "conferencia.csv",
      status: "COMPLETED",
      conference_status: "FINALIZADA",
      created_at: "2026-08-10T10:00:00Z",
      started_at: "2026-08-10T10:01:00Z",
      finished_at: "2026-08-10T10:02:00Z",
      cancelled_at: null,
      updated_at: "2026-08-10T10:02:00Z",
      duration_seconds: 60,
      productivity_boxes_per_hour: 60,
      total_expected: 1,
      total_confirmed: 1,
      total_missing: 0,
      total_scans: 1,
      total_duplicates: 0,
      total_surpluses: 0,
      pending_surpluses: 0,
      pallet_class: "CLASSE A",
      final_justification: null,
      source_fingerprint: "fingerprint-node-001",
      content_hash: "content-node-001",
      import_agenda: "AGENDA TESTE",
      import_origin: "PORTAL",
      import_operation: "DIGITAL",
      imported_shift: "T1",
      imported_at: "2026-08-10T10:00:00Z",
      print_status: "AVAILABLE",
      is_reconference: false,
      previous_id_conferencia: null,
      reconference_reason: null,
      reconference_authorized_at: null,
      reconference_authorized_by: null,
      collaborator_registration: "000123",
      collaborator_name: "OPERADOR NODE",
      collaborator_shift: "T1",
      started_by: "000123",
      finished_by: "000123",
      cancelled_by: null,
    },
    items: [{
      local_id: 11,
      caixa_estoque: "000001",
      ds_classe: "=IMPORTXML(\"https://invalid.example\", \"//secret\")",
      status: "CONFIRMED",
      confirmed_at: "2026-08-10T10:01:30Z",
      confirmed_by: "000123",
      confirmed_attempt: 1,
    }],
    scans: [{
      local_id: 21,
      caixa_estoque: "000001",
      classification: "MATCHED",
      scanned_at: "2026-08-10T10:01:30Z",
      attempt_number: 1,
      collaborator_registration: "000123",
      collaborator_name: "OPERADOR NODE",
    }],
    attempts: [],
    confirmations: [],
    divergences: [],
    surplus_summaries: [],
    surpluses: [],
    treatments: [],
    treatment_items: [],
    audit: [],
  };
}


function syncRequest(payload) {
  return {
    action: "sync",
    shared_secret: "shared-test-secret",
    conferences: Array.isArray(payload) ? payload : [payload],
  };
}


function payloadWithId(conferenceId, localOffset) {
  const payload = JSON.parse(JSON.stringify(completePayload()));
  payload.id_conferencia = conferenceId;
  payload.conference.id_conferencia = conferenceId;
  payload.conference.local_id += localOffset;
  payload.conference.codigo_palete = conferenceId;
  payload.conference.source_fingerprint = "fingerprint-" + conferenceId;
  payload.conference.content_hash = "content-" + conferenceId;
  payload.items[0].local_id += localOffset;
  payload.scans[0].local_id += localOffset;
  return payload;
}


function run() {
  const {context, runtime} = buildRuntime();

  const unauthorized = invoke(context, {
    action: "health",
    shared_secret: "invalid-secret",
  });
  assert.equal(unauthorized.ok, false);
  assert.equal(unauthorized.error.code, "UNAUTHORIZED");
  assert.equal(runtime.lockAttempts, 0);

  runtime.lockAvailable = false;
  const busy = invoke(context, {
    action: "health",
    shared_secret: "shared-test-secret",
  });
  assert.equal(busy.ok, false);
  assert.equal(busy.error.code, "SYNC_BUSY");
  assert.equal(runtime.lockAttempts, 1);
  assert.equal(runtime.lockReleases, 0);
  runtime.lockAvailable = true;

  const payload = completePayload();
  const inserted = invoke(context, syncRequest(payload));
  assert.equal(inserted.ok, true);
  assert.equal(inserted.inserted, 1);
  assert.equal(inserted.already_exists, 0);
  assert.deepEqual(inserted.results, [{
    id_conferencia: payload.id_conferencia,
    status: "INSERTED",
  }]);
  assert.equal(runtime.spreadsheet.getSheetByName("CONFERENCIAS").dataRows(), 1);
  assert.equal(runtime.spreadsheet.getSheetByName("ITENS").dataRows(), 1);
  assert.equal(runtime.spreadsheet.getSheetByName("BIPAGENS").dataRows(), 1);
  assert.equal(
    runtime.spreadsheet.getSheetByName("ITENS").getCell(2, 3),
    "000001"
  );
  assert.equal(
    runtime.spreadsheet.getSheetByName("ITENS").getCell(2, 4),
    "'=IMPORTXML(\"https://invalid.example\", \"//secret\")"
  );

  const rowCounts = new Map();
  for (const [name, sheet] of runtime.spreadsheet.sheets.entries()) {
    rowCounts.set(name, sheet.dataRows());
  }
  const alreadyExists = invoke(context, syncRequest(payload));
  assert.equal(alreadyExists.inserted, 0);
  assert.equal(alreadyExists.already_exists, 1);
  assert.equal(alreadyExists.results[0].status, "ALREADY_EXISTS");
  for (const [name, sheet] of runtime.spreadsheet.sheets.entries()) {
    assert.equal(sheet.dataRows(), rowCounts.get(name));
  }

  runtime.spreadsheet.getSheetByName("ITENS").deleteRows(2, 1);
  assert.equal(runtime.spreadsheet.getSheetByName("ITENS").dataRows(), 0);
  const repaired = invoke(context, syncRequest(payload));
  assert.equal(repaired.inserted, 1);
  assert.equal(repaired.already_exists, 0);
  assert.equal(repaired.results[0].status, "INSERTED");
  assert.equal(runtime.spreadsheet.getSheetByName("CONFERENCIAS").dataRows(), 1);
  assert.equal(runtime.spreadsheet.getSheetByName("ITENS").dataRows(), 1);
  assert.equal(runtime.spreadsheet.getSheetByName("BIPAGENS").dataRows(), 1);
  assert.equal(runtime.lockAttempts, 4);
  assert.equal(runtime.lockReleases, 3);

  const firstBatchPayload = payloadWithId("CONF-NODE-002", 100);
  const secondBatchPayload = payloadWithId("CONF-NODE-003", 200);
  const conferenceSheet = runtime.spreadsheet.getSheetByName("CONFERENCIAS");
  const itemSheet = runtime.spreadsheet.getSheetByName("ITENS");
  const scanSheet = runtime.spreadsheet.getSheetByName("BIPAGENS");
  const writesBeforeBatch = {
    conferences: conferenceSheet.setValuesCalls,
    items: itemSheet.setValuesCalls,
    scans: scanSheet.setValuesCalls,
  };
  const insertedBatch = invoke(
    context,
    syncRequest([firstBatchPayload, secondBatchPayload])
  );
  assert.equal(insertedBatch.inserted, 2);
  assert.deepEqual(
    insertedBatch.results.map((item) => item.status),
    ["INSERTED", "INSERTED"]
  );
  assert.equal(conferenceSheet.setValuesCalls, writesBeforeBatch.conferences + 1);
  assert.equal(itemSheet.setValuesCalls, writesBeforeBatch.items + 1);
  assert.equal(scanSheet.setValuesCalls, writesBeforeBatch.scans + 1);
  assert.equal(conferenceSheet.dataRows(), 3);
  assert.equal(itemSheet.dataRows(), 3);
  assert.equal(scanSheet.dataRows(), 3);

  const isolatedPayload = payloadWithId("CONF-NODE-004", 300);
  const poisonPayload = payloadWithId("CONF-NODE-POISON", 400);
  itemSheet.rejectConferenceId = poisonPayload.id_conferencia;
  const isolatedResult = invoke(
    context,
    syncRequest([isolatedPayload, poisonPayload])
  );
  assert.deepEqual(
    isolatedResult.results.map((item) => item.status),
    ["INSERTED", "ERROR"]
  );
  assert.equal(isolatedResult.results[1].error_code, "SHEETS_WRITE_FAILED");
  assert.equal(conferenceSheet.dataRows(), 4);
  assert.equal(itemSheet.dataRows(), 4);
  assert.equal(scanSheet.dataRows(), 4);
  itemSheet.rejectConferenceId = null;

  return {ok: true, scenarios: 7};
}


if (require.main === module) {
  process.stdout.write(JSON.stringify(run()) + "\n");
}

module.exports = {run};
