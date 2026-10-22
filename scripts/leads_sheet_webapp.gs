/**
 * Leads sheet web app — lets the lead agent server read and write this sheet
 * without a Google Cloud service account.
 *
 * Setup (in the leads Google Sheet):
 *   1. Extensions → Apps Script → paste this file (replace everything).
 *   2. Replace PASTE_TOKEN_HERE below with the same value as
 *      LEADS_SHEET_WEBAPP_TOKEN in the server's .env.
 *   3. Deploy → New deployment → type "Web app"
 *        Execute as: Me
 *        Who has access: Anyone
 *      → Deploy → Authorize → copy the "Web app URL" into
 *      LEADS_SHEET_WEBAPP_URL in the server's .env.
 *
 * Every request must carry the token, so the URL alone is not enough to use it.
 * After editing this script: Deploy → Manage deployments → Edit → Version: New version.
 */

const TOKEN = 'PASTE_TOKEN_HERE';

function doPost(e) {
  let req;
  try {
    req = JSON.parse(e.postData.contents);
  } catch (err) {
    return reply_({ error: 'bad request' });
  }
  if (!req || req.token !== TOKEN || TOKEN === 'PASTE_TOKEN_HERE') {
    return reply_({ error: 'unauthorized' });
  }
  const lock = LockService.getDocumentLock();
  lock.waitLock(30000);
  try {
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    switch (req.action) {
      case 'ping':
        return reply_({ ok: true, name: ss.getName(), url: ss.getUrl() });
      case 'read':
        return reply_(read_(ss, req.tab));
      case 'write':
        write_(ss, req.tab, req.values, !!req.reset_colors);
        return reply_({ ok: true });
      case 'append':
        append_(ss, req.tab, req.headers, req.rows);
        return reply_({ ok: true });
      case 'highlight':
        highlight_(ss, req.tab, req.rows, req.color);
        return reply_({ ok: true });
      case 'dropdown':
        dropdown_(ss, req.tab, req.column, req.list);
        return reply_({ ok: true });
      default:
        return reply_({ error: 'unknown action: ' + req.action });
    }
  } catch (err) {
    return reply_({ error: String(err) });
  } finally {
    lock.releaseLock();
  }
}

function reply_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

function sheet_(ss, tab, create) {
  let sh = ss.getSheetByName(tab);
  if (!sh && create) sh = ss.insertSheet(tab);
  return sh;
}

function pad_(values) {
  const width = values.reduce(function (m, r) { return Math.max(m, r.length); }, 1);
  return values.map(function (r) {
    const row = r.map(function (v) { return v === null || v === undefined ? '' : String(v); });
    while (row.length < width) row.push('');
    return row;
  });
}

function read_(ss, tab) {
  const sh = sheet_(ss, tab, false);
  if (!sh) return { exists: false };
  if (sh.getLastRow() === 0) return { exists: true, values: [] };
  return { exists: true, values: sh.getDataRange().getDisplayValues() };
}

function write_(ss, tab, values, resetColors) {
  const sh = sheet_(ss, tab, true);
  const rows = pad_(values && values.length ? values : [['']]);
  const width = rows[0].length;
  if (sh.getMaxColumns() < width) sh.insertColumnsAfter(sh.getMaxColumns(), width - sh.getMaxColumns());
  if (sh.getMaxRows() < rows.length + 1) sh.insertRowsAfter(sh.getMaxRows(), rows.length + 1 - sh.getMaxRows());
  sh.getRange(1, 1, sh.getMaxRows(), sh.getMaxColumns()).clearContent();
  if (resetColors && sh.getMaxRows() > 1) {
    sh.getRange(2, 1, sh.getMaxRows() - 1, sh.getMaxColumns()).setBackground(null).setFontWeight('normal');
  }
  const range = sh.getRange(1, 1, rows.length, width);
  range.setNumberFormat('@');
  range.setValues(rows);
  sh.getRange(1, 1, 1, width).setFontWeight('bold');
}

function append_(ss, tab, headers, rows) {
  const sh = sheet_(ss, tab, true);
  const head = pad_([headers])[0];
  sh.getRange(1, 1, 1, head.length).setValues([head]).setFontWeight('bold');
  if (!rows || !rows.length) return;
  const body = pad_(rows.map(function (r) { return r.slice(0, head.length); }));
  const start = Math.max(sh.getLastRow(), 1) + 1;
  if (sh.getMaxRows() < start + body.length) sh.insertRowsAfter(sh.getMaxRows(), start + body.length - sh.getMaxRows());
  const range = sh.getRange(start, 1, body.length, head.length);
  range.setNumberFormat('@');
  range.setValues(body);
}

function highlight_(ss, tab, rowNumbers, color) {
  const sh = sheet_(ss, tab, false);
  if (!sh || !rowNumbers || !rowNumbers.length) return;
  const width = Math.max(sh.getLastColumn(), 1);
  rowNumbers.forEach(function (n) { sh.getRange(n, 1, 1, width).setBackground(color || '#fff2cc'); });
}

function dropdown_(ss, tab, column, list) {
  const sh = sheet_(ss, tab, false);
  if (!sh || sh.getLastColumn() === 0) return;
  const headers = sh.getRange(1, 1, 1, sh.getLastColumn()).getDisplayValues()[0];
  const col = headers.indexOf(column);
  if (col < 0 || sh.getMaxRows() < 2) return;
  const rule = SpreadsheetApp.newDataValidation().requireValueInList(list, true).setAllowInvalid(true).build();
  sh.getRange(2, col + 1, sh.getMaxRows() - 1, 1).setDataValidation(rule);
}
