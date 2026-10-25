/**
 * Leads sheet web app — lets the lead agent server read and write this sheet
 * without a Google Cloud service account.
 *
 * Setup (in the leads Google Sheet):
 *   1. Extensions → Apps Script → paste this file (replace everything).
 *      For a new, empty sheet: choose "setupSheet" in the function list → Run
 *      (creates and formats all tabs; see the bottom of this file).
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
  applyLinks_(sh, rows);
}

// Show names/titles as clickable links instead of separate URL columns.
// key = column that is displayed, value = (usually hidden) column with the URL.
const LINKS = {
  'Job Title': 'Job Link',
  'Latest Job': 'Latest Job Link',
  'Contact Name': 'Contact LinkedIn',
  'Alt Contact Name': 'Alt Contact LinkedIn',
  'Contact': 'Contact LinkedIn',
  'Alt Contact': 'Alt Contact LinkedIn',
  'Company Name': 'Company LinkedIn'
};

function applyLinks_(sh, rows) {
  if (rows.length < 2) return;
  const headers = rows[0];
  Object.keys(LINKS).forEach(function (textCol) {
    const t = headers.indexOf(textCol);
    const u = headers.indexOf(LINKS[textCol]);
    if (t < 0 || u < 0) return;
    const rich = rows.slice(1).map(function (r) {
      const text = r[t] || '';
      const url = r[u] || '';
      const b = SpreadsheetApp.newRichTextValue().setText(text);
      if (text && /^https?:\/\//.test(url)) b.setLinkUrl(url);
      return [b.build()];
    });
    sh.getRange(2, t + 1, rich.length, 1).setRichTextValues(rich);
  });
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
  // Columns may have moved since last time: drop old dropdowns first.
  sh.getRange(1, 1, sh.getMaxRows(), sh.getMaxColumns()).clearDataValidations();
  const rule = SpreadsheetApp.newDataValidation().requireValueInList(list, true).setAllowInvalid(true).build();
  sh.getRange(2, col + 1, sh.getMaxRows() - 1, 1).setDataValidation(rule);
}


/* ===========================================================================
 * One-time setup for a NEW, empty sheet.
 * In the Apps Script editor choose "setupSheet" in the function list → Run.
 * Safe to run again: existing tabs keep their data; only formatting is reapplied.
 * ======================================================================== */

const JOBS_TAB = 'Jobs';
const HEADERS = {
  'Weekly': ["Company Name", "Company Size", "Outreach Track", "Latest Job", "Latest Job Link", "Other Jobs", "Outreach Phrasing", "Contact", "Contact LinkedIn", "Alt Contact", "Alt Contact LinkedIn", "Previous Outreach"],
  'Companies': ["Company Name", "Target", "Outreach Status", "Contact Name", "Contact Title", "Contact LinkedIn", "Alt Contact Name", "Alt Contact Title", "Alt Contact LinkedIn", "Latest Job", "Latest Job Link", "Category", "Outreach Phrasing", "Open Jobs", "Outreach Track", "Company Size", "Company Type", "Last Job Found", "Status Updated", "Contact Found", "Company LinkedIn", "Last Enriched"],
  'Jobs': ["Company Name", "Job Title", "Category", "Target", "Contact Name", "Contact Title", "Contact LinkedIn", "Outreach Status", "Status Updated", "Job Link", "Publish Date", "Found Date", "Company Size", "Company Type", "Alt Contact Name", "Alt Contact Title", "Alt Contact LinkedIn", "Outreach Phrasing", "Job Summary", "Source", "Last Checked", "Company LinkedIn"],
  'Do Not Contact': ["Company Name", "Reason"],
  'Blocked Companies': ["Company", "Name Variants"],
  'Archive': ["Company Name", "Job Title", "Category", "Job Link", "Publish Date", "Found Date", "Last Checked", "Closed Date", "Outreach Status", "Source", "Job Summary"],
  'Companies Archive': ["Company Name", "Target", "Outreach Status", "Contact Name", "Contact Title", "Contact LinkedIn", "Alt Contact Name", "Alt Contact Title", "Alt Contact LinkedIn", "Latest Job", "Latest Job Link", "Category", "Outreach Phrasing", "Open Jobs", "Outreach Track", "Company Size", "Company Type", "Last Job Found", "Status Updated", "Contact Found", "Company LinkedIn", "Last Enriched"]
};
const TAB_ORDER = ['Weekly', 'Companies', 'Jobs', 'Do Not Contact', 'Blocked Companies', 'Archive', 'Companies Archive', 'מדריך'];
const TAB_COLORS = {
  'Weekly': '#34a853', 'Companies': '#4285f4', 'Jobs': '#7baaf7', 'Do Not Contact': '#fb8c00',
  'Blocked Companies': '#fb8c00', 'Archive': '#9e9e9e', 'Companies Archive': '#9e9e9e', 'מדריך': '#8e24aa'
};
const STATUSES = ["לא נשלח", "בקשת חברות נשלחה", "הודעה ראשונה נשלחה", "פולואפ נשלח", "ענה: חיובי", "ענה: שלילי", "לא רלוונטי"];
const BLOCKED_SEED = [["מטריקס", "Matrix, Matrix IT, מטריקס"], ["NVIDIA", "NVIDIA, NVIDIA AI, Nvidia, אנבידיה, אינבידיה"], ["ONE", "ONE Technologies, One1, וואן טכנולוגיות"], ["SQLink", "SQLink, SQLink Group, אס.קיו.לינק, SQ Link"], ["התעשייה האווירית", "IAI, IAI - Israel Aerospace Industries, Israel Aerospace Industries, התעשייה האווירית, תעשייה אווירית"], ["אלעד מערכות", "Elad Software, Elad Software Systems, Elad Systems, אלעד מערכות"], ["קבוצת יעל", "Yael Group, Yael Software, קבוצת יעל"], ["Commit", "Commit, Commit Software, קומיט"], ["בינת", "Bynet, Bynet Data Communications, בינת, בינת סמך"], ["Intel", "Intel, Intel Corporation, אינטל"], ["Comblack", "Comblack, ComBlack, קומבלק"]];
// Columns people may edit (yellow header). Everything else is written by the agent.
const EDITABLE = {
  'Companies': ['Outreach Status', 'Status Updated', 'Contact Name', 'Contact Title', 'Contact LinkedIn',
                'Alt Contact Name', 'Alt Contact Title', 'Alt Contact LinkedIn'],
  'Companies Archive': ['Outreach Status', 'Status Updated'],
  'Do Not Contact': ['Company Name', 'Reason'],
  'Blocked Companies': ['Company', 'Name Variants']
};
const WIDTHS = {
  'Company Name': 190, 'Job Title': 250, 'Latest Job': 230, 'Job Summary': 320, 'Other Jobs': 260,
  'Category': 150, 'Target': 75, 'Outreach Status': 165, 'Outreach Track': 95, 'Outreach Phrasing': 170,
  'Contact Name': 150, 'Contact Title': 190, 'Alt Contact Name': 150, 'Alt Contact Title': 190,
  'Contact': 230, 'Alt Contact': 230, 'Previous Outreach': 190, 'Name Variants': 420, 'Reason': 300,
  'Company Size': 95, 'Company Type': 120, 'Open Jobs': 75, 'Source': 85
};
// Technical columns: kept (the agent needs them) but hidden to keep the view short.
// Unhide any time: select the columns around the arrows in the header → right-click → Unhide.
const HIDDEN_COLUMNS = {
  'Weekly': ['Outreach Track', 'Latest Job Link', 'Contact LinkedIn', 'Alt Contact LinkedIn'],
  'Companies': ['Contact LinkedIn', 'Alt Contact LinkedIn', 'Latest Job Link', 'Category', 'Outreach Phrasing',
                'Outreach Track', 'Company Type', 'Last Job Found', 'Contact Found', 'Company LinkedIn', 'Last Enriched'],
  'Companies Archive': ['Contact LinkedIn', 'Alt Contact LinkedIn', 'Latest Job Link', 'Category', 'Outreach Phrasing',
                        'Outreach Track', 'Company Type', 'Last Job Found', 'Contact Found', 'Company LinkedIn', 'Last Enriched'],
  'Jobs': ['Contact LinkedIn', 'Job Link', 'Company Size', 'Company Type', 'Alt Contact Name', 'Alt Contact Title',
           'Alt Contact LinkedIn', 'Outreach Phrasing', 'Job Summary', 'Source', 'Last Checked', 'Company LinkedIn',
           'Status Updated'],
  'Archive': ['Job Link', 'Source', 'Job Summary', 'Last Checked']
};
// Tabs you rarely need: hidden (open them from the ☰ "All sheets" button, bottom-left).
const HIDDEN_TABS = ['Blocked Companies', 'Archive', 'Companies Archive', 'מדריך'];

const NOTES = {
  'Target': 'כן = בקהל היעד (חברת מוצר 50–200 עובדים; Salesforce/monday: כל חברה מ־50 עובדים). לא ידוע = הסוכן ינסה שוב.',
  'Outreach Status': 'מעדכנים כאן בלבד (בלשונית Companies). הסוכן לא דורס ערך שעודכן ידנית.',
  'Status Updated': 'מתמלא אוטומטית כשהסטטוס משתנה. אפשר לתקן ידנית.',
  'Contact Name': 'אפשר להקליד איש קשר ידנית — הסוכן לא יחליף אותו.',
  'Outreach Track': 'פיתוח = CTO / VP R&D. מערכות = משרות Salesforce / monday.com (VP Operations / RevOps / COO / CIO).',
  'Outreach Phrasing': 'המילים שנכנסות להודעה במקום [תחום].',
  'Company Size': 'טווח עובדים כפי שמופיע בלינקדאין.',
  'Found Date': 'התאריך שבו הסוכן מצא את המשרה.',
  'Publish Date': 'התאריך שבו החברה פרסמה את המשרה.',
  'Last Checked': 'הפעם האחרונה שהסוכן וידא שהמשרה עדיין פתוחה.',
  'Contact Found': 'מתי הסוכן מצא את איש הקשר. אחרי 60 יום ומשרה חדשה — נבדק מחדש.',
  'Name Variants': 'כל השמות של החברה, מופרדים בפסיק (עברית ואנגלית).'
};

function setupSheet() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  Object.keys(HEADERS).forEach(function (tab) { setupTab_(ss, tab, HEADERS[tab]); });
  setupGuide_(ss);
  // Remove the default empty first tab ("Sheet1" / "גיליון1").
  ss.getSheets().forEach(function (sh) {
    if (TAB_ORDER.indexOf(sh.getName()) < 0 && sh.getLastRow() === 0 && ss.getSheets().length > 1) ss.deleteSheet(sh);
  });
  TAB_ORDER.forEach(function (name, i) {
    const sh = ss.getSheetByName(name);
    if (sh) { ss.setActiveSheet(sh); ss.moveActiveSheet(i + 1); }
  });
  ss.setActiveSheet(ss.getSheetByName('Weekly'));
  HIDDEN_TABS.forEach(function (name) {
    const sh = ss.getSheetByName(name);
    if (sh) sh.hideSheet();
  });
  SpreadsheetApp.getUi().alert('הגיליון מוכן ✔');
}

function setupTab_(ss, tab, headers) {
  const sh = sheet_(ss, tab, true);
  const width = headers.length;
  if (sh.getMaxColumns() < width) sh.insertColumnsAfter(sh.getMaxColumns(), width - sh.getMaxColumns());
  if (sh.getMaxColumns() > width) sh.deleteColumns(width + 1, sh.getMaxColumns() - width);
  if (sh.getLastRow() === 0) sh.getRange(1, 1, 1, width).setValues([headers]);
  if (tab === 'Blocked Companies' && sh.getLastRow() < 2) {
    sh.getRange(2, 1, BLOCKED_SEED.length, 2).setValues(BLOCKED_SEED);
  }

  const all = sh.getRange(1, 1, sh.getMaxRows(), width);
  all.setFontFamily('Arial').setFontSize(10).setVerticalAlignment('middle')
     .setWrapStrategy(SpreadsheetApp.WrapStrategy.CLIP).setNumberFormat('@');
  const head = sh.getRange(1, 1, 1, width);
  head.setFontWeight('bold').setFontColor('#ffffff').setBackground('#1f3864');
  sh.setRowHeight(1, 30);
  sh.setFrozenRows(1);
  sh.setFrozenColumns(1);

  const editable = EDITABLE[tab] || [];
  headers.forEach(function (h, i) {
    const cell = sh.getRange(1, i + 1);
    sh.setColumnWidth(i + 1, WIDTHS[h] || (/Link|LinkedIn/.test(h) ? 110 : (/Date|Checked|Updated|Found|Enriched/.test(h) ? 100 : 130)));
    if (editable.indexOf(h) >= 0) cell.setBackground('#ffe599').setFontColor('#1f1f1f');
    if (NOTES[h]) cell.setNote(NOTES[h]);
  });

  // Colors for Target and Outreach Status.
  const rules = [];
  const tCol = headers.indexOf('Target');
  if (tCol >= 0) {
    const r = sh.getRange(2, tCol + 1, sh.getMaxRows() - 1, 1);
    rules.push(SpreadsheetApp.newConditionalFormatRule().whenTextEqualTo('כן').setBackground('#d9ead3').setFontColor('#274e13').setBold(true).setRanges([r]).build());
    rules.push(SpreadsheetApp.newConditionalFormatRule().whenTextEqualTo('לא ידוע').setBackground('#fff2cc').setRanges([r]).build());
    rules.push(SpreadsheetApp.newConditionalFormatRule().whenTextEqualTo('לא').setFontColor('#999999').setRanges([r]).build());
  }
  const sCol = headers.indexOf('Outreach Status');
  if (sCol >= 0) {
    const r = sh.getRange(2, sCol + 1, sh.getMaxRows() - 1, 1);
    rules.push(SpreadsheetApp.newConditionalFormatRule().whenTextEqualTo('ענה: חיובי').setBackground('#b6d7a8').setRanges([r]).build());
    rules.push(SpreadsheetApp.newConditionalFormatRule().whenTextEqualTo('ענה: שלילי').setBackground('#f4cccc').setRanges([r]).build());
    rules.push(SpreadsheetApp.newConditionalFormatRule().whenTextEqualTo('לא רלוונטי').setFontColor('#999999').setRanges([r]).build());
    ['בקשת חברות נשלחה', 'הודעה ראשונה נשלחה', 'פולואפ נשלח'].forEach(function (v) {
      rules.push(SpreadsheetApp.newConditionalFormatRule().whenTextEqualTo(v).setBackground('#cfe2f3').setRanges([r]).build());
    });
    if (tab === 'Companies' || tab === 'Companies Archive') {
      const rule = SpreadsheetApp.newDataValidation().requireValueInList(STATUSES, true).setAllowInvalid(true).build();
      r.setDataValidation(rule);
    }
  }
  sh.setConditionalFormatRules(rules);

  // Hide technical columns (by the header names actually in the sheet).
  const actual = sh.getRange(1, 1, 1, Math.max(sh.getLastColumn(), 1)).getDisplayValues()[0];
  sh.showColumns(1, sh.getMaxColumns());
  (HIDDEN_COLUMNS[tab] || []).forEach(function (h) {
    const i = actual.indexOf(h);
    if (i >= 0) sh.hideColumns(i + 1);
  });

  // No protection: remove any left over from an earlier setup.
  sh.getProtections(SpreadsheetApp.ProtectionType.SHEET).forEach(function (p) { p.remove(); });
  sh.getProtections(SpreadsheetApp.ProtectionType.RANGE).forEach(function (p) { p.remove(); });
  if (TAB_COLORS[tab]) sh.setTabColor(TAB_COLORS[tab]);
}

function setupGuide_(ss) {
  const sh = sheet_(ss, 'מדריך', true);
  sh.clear();
  sh.setRightToLeft(true);
  const rows = [
    ['סוכן הלידים – מדריך', '', ''],
    ['', '', ''],
    ['לשונית', 'מה יש בה', 'מי עורך'],
    ['Weekly', 'רשימת הפנייה של השבוע: שורה לכל חברה (ולכל מסלול). חברות חדשות למעלה; חברות שכבר פנו אליהן ופרסמו משרה חדשה – למטה, בצבע.', 'נבנית מחדש בכל יום ראשון ב־8:00. לא עורכים כאן.'],
    ['Companies', 'כל חברה שמגייסת עכשיו: גודל, סוג, האם בקהל היעד, איש קשר + חלופי, סטטוס פנייה. חברה עם משרות פיתוח וגם Salesforce/monday מופיעה פעמיים (מסלול פיתוח ומסלול מערכות).', 'מעדכנים את סטטוס הפנייה כאן. אפשר להקליד איש קשר ידנית – הסוכן לא יחליף אותו. עמודות עם כותרת צהובה = מותר לערוך.'],
    ['Jobs', 'כל משרה פתוחה, עם הקטגוריה ואיש הקשר המתאים לסוג המשרה (מועתק מ־Companies).', 'הסוכן כותב מחדש בכל ריצה. לא עורכים כאן.'],
    ['Do Not Contact', 'חברות שלא פונים אליהן (כבר בשיחה, בקמפיין אחר וכו׳).', 'מוסיפים שם חברה ומשפט סיבה.'],
    ['Blocked Companies', 'חברות שלא אוספים מהן משרות בכלל, עם כל וריאציות השם.', 'מוסיפים שורה. בריצה הבאה המשרות שלהן נמחקות.'],
    ['Archive', 'משרות שנסגרו, עם תאריך הסגירה.', 'לקריאה בלבד.'],
    ['Companies Archive', 'חברות שאין להן כרגע משרות פתוחות, עם ההיסטוריה שלהן. חוזרות ל־Companies כשהן מפרסמות משרה חדשה.', 'לקריאה בלבד.'],
    ['', '', ''],
    ['סטטוס פנייה', 'משמעות', ''],
    ['לא נשלח', 'עוד לא פנו (ברירת מחדל לחברה חדשה)', ''],
    ['בקשת חברות נשלחה', 'נשלחה בקשת חברות בלינקדאין', ''],
    ['הודעה ראשונה נשלחה', 'נשלחה הודעה אחרי שהבקשה אושרה', ''],
    ['פולואפ נשלח', 'נשלחה הודעת המשך', ''],
    ['ענה: חיובי', 'יש עניין', ''],
    ['ענה: שלילי', 'אין עניין', ''],
    ['לא רלוונטי', 'לא לפנות שוב', ''],
    ['', '', ''],
    ['Target', 'כן = בקהל היעד · לא = מחוץ לקהל היעד · לא ידוע = עוד לא נמצא מידע, הסוכן ינסה שוב', ''],
    ['לוח זמנים', 'הסוכן רץ כל יומיים ב־8:00. הקובץ השבועי נבנה בכל יום ראשון ב־8:00 ונשלחת הודעה בסלאק.', ''],
    ['איש קשר חלופי', 'פונים אליו רק אם הראשון לא אישר את בקשת החברות תוך שבוע.', ''],
    ['לפני פנייה', 'כדאי להציץ בפרופיל הלינקדאין ולוודא שזה האדם הנכון בחברה הנכונה.', ''],
    ['קישורים', 'שם המשרה, שם איש הקשר ושם החברה הם קישורים – לחיצה פותחת את המודעה / הפרופיל בלינקדאין.', ''],
    ['מוסתר', 'עמודות טכניות ולשוניות שכמעט לא צריך (ארכיון, חברות חסומות, המדריך הזה) מוסתרות. פותחים לשונית מוסתרת דרך ☰ בפינה השמאלית התחתונה.', '']
  ];
  sh.getRange(1, 1, rows.length, 3).setValues(rows);
  sh.getRange(1, 1, rows.length, 3).setFontFamily('Arial').setFontSize(10).setWrap(true).setVerticalAlignment('top');
  sh.getRange('A1').setFontSize(16).setFontWeight('bold');
  [3, 12].forEach(function (r) { sh.getRange(r, 1, 1, 3).setFontWeight('bold').setFontColor('#ffffff').setBackground('#1f3864'); });
  sh.getRange('A4:A10').setFontWeight('bold');
  sh.getRange('A21:A24').setFontWeight('bold');
  sh.setColumnWidth(1, 170); sh.setColumnWidth(2, 520); sh.setColumnWidth(3, 380);
  sh.setTabColor(TAB_COLORS['מדריך']);
}
