/* Run actual UI handlers in headless Chrome, using fake Notes and library APIs only.
   Usage: node tests/frontend_regressions.js [path/to/Chrome] */
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { spawnSync } = require('node:child_process');
const chrome = process.argv[2] || process.env.CHROME_BIN || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const root = path.resolve(__dirname, '..');
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'notes-ui-regressions-'));

async function browserTests() {
  const checks = [];
  function check(value, message) { if (!value) throw new Error(message); checks.push(message); }
  const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  let calls = [], confirms = [], confirmAnswer = true;
  window.confirm = message => { confirms.push(message); return confirmAnswer; };
  window.fetch = async () => { throw new Error('Unexpected real network access'); };
  let fakeNotes = [], config = { descriptions: { Existing: 'Keep this description' }, neverSend: ['Private'], hasKey: true };
  let stored = null, scanGroups = [], lastMerge = null, failSave = false, bodyFailures = new Set();
  api = async (route, body) => {
    calls.push({ route, body: body === undefined ? null : structuredClone(body) });
    if (route === '/api/data') return structuredClone(stored || DB);
    if (route === '/api/save') { if (failSave) throw new Error('Simulated save failure'); const rev = (stored?.rev || DB.rev || 0) + 1; stored = {...structuredClone(body), rev}; return { ok: true, rev }; }
    if (route === '/api/notes/list') return { notes: fakeNotes };
    if (route === '/api/notes/folders') return { folders: [...new Set(fakeNotes.map(n => n.folder))], defaultFolder: 'Notes' };
    if (route === '/api/notes/restore/info') return {};
    if (route === '/api/notes/body') return { results: body.ids.map(id => bodyFailures.has(id)
      ? {id, ok:false, error:'Simulated read failure'} : {id, ok:true, body:`${id}\nImported body ${id}`}) };
    if (route === '/api/jev/config') {
      if (body) config = { ...config, ...structuredClone(body) };
      return structuredClone(config);
    }
    if (route === '/api/similar/scan') return { groups: structuredClone(scanGroups), compared: 3, tooShort: 0, lastMerge };
    if (route === '/api/similar/merge') { lastMerge = { groups:body.groups.length, notes:body.groups.flat().length }; return { ok: true, groups:body.groups.length, removed:1 }; }
    if (route === '/api/similar/undo') { lastMerge = null; return { ok:true, groups:1, restored:2 }; }
    throw new Error('Unstubbed route ' + route);
  };
  DB = { rev:0, projects: [], prompts: [], importedNoteIds: [] };
  dataLoaded = true; serverRevisionReady = true; journalOwner = 'TEST_FRONTEND'; CATEGORY_JOURNAL = JOURNAL_PREFIX + journalOwner;
  draftStore = async () => {};
  renderSide(); fillProjSelects();

  // Real DOM: count uses non-whitespace characters and follows the selected range.
  $('edBody').value = '你好 world\n  123';
  $('edBody').setSelectionRange(0, 0); updEdCount();
  check($('edCount').textContent.includes('10') && $('edCount').textContent.includes('2'), 'full text count ignores whitespace and counts lines');
  $('edBody').setSelectionRange(0, 2); $('edBody').dispatchEvent(new Event('select'));
  check($('edCount').textContent.includes('2') && !$('edCount').textContent.includes('10'), 'selection count follows editor selection');
  curPrompt = { active: 1, versions: [{ v: 1, content: 'abc' }, { v: 2, content: 'abcdef' }] }; curVer = 1; renderVers(); loadVer();
  $('verRow').querySelectorAll('.vchip')[1].click();
  check($('edCount').textContent.includes('6'), 'version switching refreshes the count');

  DB.projects = ['<b>x</b>', '<img src=x onerror=alert(1)>']; renderSide();
  check([...$('sideList').querySelectorAll('.proj .n')].some(n => n.textContent === '<b>x</b>') &&
    [...$('sideList').querySelectorAll('.proj .n')].some(n => n.textContent === '<img src=x onerror=alert(1)>') &&
    !$('sideList').querySelector('.proj .n b,.proj .n img'), 'category HTML is displayed as literal text');
  DB.projects = []; renderSide();

  // Import folders runs the real batch import handler, including disabled controls.
  fakeNotes = [{ id:'a', name:'a', folder:'Recipes', date:'2026-01-01' }, { id:'b', name:'b', folder:'Research', date:'2026-01-02' }, { id:'c', name:'c', folder:'Notes', date:'2026-01-03' }];
  allNotes = fakeNotes; impSel = new Set(['a','b','c']);
  $('impUseFolder').checked = true; $('impUseFolder').onchange();
  check($('impProj').disabled && $('impNewProj').disabled, 'folder import disables conflicting project controls');
  await $('impGo').onclick();
  check(same(DB.projects, ['Recipes','Research']), 'folder import creates each non-default project');
  check(same(DB.prompts.map(p => p.project), ['Recipes','Research','']), 'default Notes folder imports as unassigned');
  check(stored.prompts.length === 3, 'folder import persists all three notes');
  fakeNotes.push({ id:'d', name:'Retry me', folder:'Research', date:'2026-01-04' });
  allNotes = fakeNotes; impSel = new Set(['d']); bodyFailures.add('d');
  await $('btnImport').onclick();
  await $('impGo').onclick();
  check(!DB.prompts.some(p => p.srcNoteId === 'd') && !DB.importedNoteIds.includes('d') &&
    impSel.has('d') && !$('impFailures').hidden, 'failed body read remains selected and visible without import');
  bodyFailures.delete('d'); await $('impRetryFailed').onclick();
  check(DB.prompts.some(p => p.srcNoteId === 'd') && DB.importedNoteIds.includes('d'),
    'retry imports the formerly failed note after a successful read');
  $('impUseFolder').checked = false; $('impUseFolder').onchange();
  check(!$('impProj').disabled && !$('impNewProj').disabled, 'turning folder import off restores project controls');

  // A renamed Notes folder must be offered before the move plan, and accepting it
  // changes only the local project label and eliminates unnecessary moves.
  DB = { projects:['Old recipes'], prompts:[1,2,3].map(i => ({ id:'p'+i, project:'Old recipes', title:'Recipe '+i, srcNoteId:'n'+i, active:1, versions:[{v:1,content:'Body '+i}] })), importedNoteIds:[] };
  fakeNotes = [1,2,3].map(i => ({ id:'n'+i,name:'Recipe '+i,folder:'New recipes' }));
  const beforeBodies = DB.prompts.map(p => p.versions);
  await openOrg();
  check($('orgRename').querySelectorAll('.orgRenameGo').length === 1, 'three notes in a renamed folder show the warning');
  check(Object.values(orgPlan).flat().length === 3, 'rename warning accompanies the original three-move plan');
  await $('orgRename').querySelector('.orgRenameGo').onclick();
  check(same(DB.projects,['New recipes']) && DB.prompts.every(p => p.project === 'New recipes'), 'rename alignment updates the project and its note labels');
  check(Object.values(orgPlan).flat().length === 0, 'rename alignment removes all unnecessary moves');
  check(same(DB.prompts.map(p => p.versions),beforeBodies), 'rename alignment leaves all note bodies unchanged');
  DB.projects = ['Old recipes']; DB.prompts.forEach(p => p.project = 'Old recipes'); fakeNotes[2].folder = 'Elsewhere';
  await openOrg(); check(!$('orgRename').children.length, 'scattered notes below 80 percent do not suggest a rename');

  // Sensitive setting handlers must round-trip each field without altering terms.
  config = { hasKey:true, descriptions:{}, neverSend:['Private'], customSecrets:['Internal Project'], useBuiltinSecrets:false, builtinLabels:['API keys'], sendSensitive:false };
  await $('btnOrganize').onclick();
  check($('smartOverlay').classList.contains('show'), 'smart organize button opens the Jev settings panel');
  check(!$('jvBuiltin').checked && $('jvCustomSecret').value === 'Internal Project' && $('jvSecretFolders').value === 'Private', 'sensitive settings load the saved scope');
  check(getComputedStyle($('jvCustomSecret')).minHeight === '58px', 'sensitive textarea keeps a readable explicit height');
  $('jvCustomSecret').value = 'alpha\n\nbeta'; $('jvSecretFolders').value = 'Private\nSecret';
  $('jvSendSecret').checked = true;
  // Avoid the unrelated status refresh after saving configuration.
  jevRefresh = async () => {};
  await $('jvSave').onclick();
  check(same(config.customSecrets,['alpha','beta']) && same(config.neverSend,['Private','Secret']) && config.sendSensitive && !config.useBuiltinSecrets, 'sensitive settings save all editable scope fields');

  // Setup must preserve existing configuration and never erase an omitted key.
  DB = { projects:[], prompts:[], importedNoteIds:[] }; config = { descriptions:{ Existing:'Keep' }, neverSend:['Private'], hasKey:true };
  localStorage.removeItem('notesOrganizerWizardDone'); openWizard();
  const name = $('wzCats').querySelector('.wzName'); name.value = 'Custom project'; name.dispatchEvent(new Event('input'));
  check($('wzCats').querySelector('.wzPick').checked, 'editing a preset automatically selects it');
  $('wzOwn').value = 'Just a name\nAnother: Specific notes about another subject'; $('wzKey').value = '';
  await $('wzDone').onclick();
  const secretName = document.documentElement.lang === 'en' ? 'Accounts & passwords' : '账号密码';
  check(DB.projects.includes('Custom project') && DB.projects.includes('Just a name') && DB.projects.includes('Another') && DB.projects.includes(secretName), 'wizard creates edited, custom, and private projects');
  check(config.descriptions.Another === 'Specific notes about another subject' && !config.descriptions['Just a name'], 'wizard saves concrete descriptions and leaves name-only rows blank');
  check(config.descriptions.Existing === 'Keep' && same(config.neverSend,['Private',secretName]), 'wizard preserves existing descriptions and excluded folders');
  check(!Object.hasOwn(calls.filter(c => c.route === '/api/jev/config' && c.body).at(-1).body,'apiKey'), 'blank wizard key is omitted from the config request');
  check(localStorage.getItem('notesOrganizerWizardDone') === '1' && !$('wzOverlay').classList.contains('show'), 'successful wizard save marks setup complete');
  DB = {projects:[],prompts:[],importedNoteIds:[]}; localStorage.removeItem('notesOrganizerWizardDone'); openWizard(); failSave = true;
  await $('wzDone').onclick();
  check(localStorage.getItem('notesOrganizerWizardDone') !== '1' && $('wzOverlay').classList.contains('show'), 'failed library save keeps the wizard open and incomplete');
  failSave = false; skipWizard();
  check(localStorage.getItem('notesOrganizerWizardDone') === '1', 'skip and close persist the wizard dismissal');
  let scheduled = []; const realTimer = window.setTimeout; window.setTimeout = (fn, delay) => { scheduled.push({fn,delay}); return 1; };
  DB = {projects:[],prompts:[],importedNoteIds:[]}; maybeOpenWizard(); check(scheduled.length === 0,'dismissed empty libraries do not reopen setup');
  localStorage.removeItem('notesOrganizerWizardDone'); DB.projects = ['Already used']; maybeOpenWizard(); check(scheduled.length === 0,'existing libraries never auto-open setup');
  DB.projects = []; maybeOpenWizard(); check(scheduled.length === 1 && scheduled[0].delay === 400,'new empty libraries schedule setup after 400ms');
  window.setTimeout = realTimer;

  // Similar grouping uses real rendered checkboxes and change/detail handlers.
  const members = [1,2,3].map(i => ({ id:'s'+i,title:'Version '+i,project:'Research',date:'2026-01-0'+i,length:150,body:'same\n'+(i===1?'old':'new')+'\nend' }));
  scanGroups = [{ reason:'body', members, scores:[{a:0,b:1,score:.95}] }];
  $('simTh').value = 90; $('simRatio').value = 1.5; $('simMin').value = 0;
  await simScan();
  check(SIM.picked.size === 0 && $('simGo').disabled && !$('simList').querySelector('.simGroup').checked,'scans start with no groups selected');
  check(calls.filter(c => c.route === '/api/similar/scan').at(-1).body.minLength === 0,'zero minimum length is passed through');
  const group = $('simList').querySelector('.simGroup'); group.checked = true; group.onchange();
  check(same(simSelected(),[['s1','s2','s3']]) && !$('simGo').disabled,'selecting a group includes its members and enables merging');
  const member = $('simList').querySelector('.simMember[data-m="1"]'); member.checked = false; member.onchange();
  check(same(simSelected(),[['s1','s3']]) && $('simGo').textContent.includes('2'),'excluding a member updates merge payload and counts');
  let toggle = $('simList').querySelector('.simGroup'); toggle.checked = false; toggle.onchange();
  toggle = $('simList').querySelector('.simGroup'); toggle.checked = true; toggle.onchange();
  check([...$('simList').querySelectorAll('.simMember')].every(c => c.checked) && simSelected()[0].length === 3, 'reselecting a group keeps visible membership consistent with the merge payload');
  const link = $('simList').querySelector('.simDiff'); link.onclick({preventDefault(){}});
  check($('simDetail-0-1').innerHTML.includes('background:var(--blue)') && $('simDetail-0-1').innerHTML.includes('line-through'),'expanded changes highlight added and deleted lines');
  check(simDiff('x\n'.repeat(700),'x\n'.repeat(700)) === null,'large line comparisons stop before allocating an oversized matrix');
  check(simDiffHtml('<script>','<img>').includes('&lt;img&gt;'),'comparison output escapes note HTML');
  $('simNone').onclick(); check(SIM.picked.size === 0 && $('simGo').disabled,'clear selection disables merging');
  $('simAll').onclick(); check(simSelected().length === 1,'select all chooses the available groups');
  confirmAnswer = false; const mergeCalls = calls.filter(c => c.route === '/api/similar/merge').length;
  await $('simGo').onclick(); check(calls.filter(c => c.route === '/api/similar/merge').length === mergeCalls,'cancelled merge sends no request');
  confirmAnswer = true; await $('simGo').onclick();
  check(calls.filter(c => c.route === '/api/similar/merge').length === mergeCalls+1 && SIM.picked.size === 0,'confirmed merge refreshes the library and rescans with cleared selection');
  check(confirms.at(-1).includes('3') && confirms.at(-1).includes('1'),'merge confirmation states note and group counts');
  SIM.lastMerge = null; await simScan();
  check(SIM.lastMerge.notes === 3, 'rescanning reloads undo counts from the merge log');
  confirmAnswer = false; await $('simUndo').onclick(); check(!calls.some(c => c.route === '/api/similar/undo'),'cancelled undo sends no request');
  confirmAnswer = true; await $('simUndo').onclick();
  check(calls.some(c => c.route === '/api/similar/undo') && confirms.at(-1).includes('3'),'confirmed undo includes the known note count and refreshes state');
  $('simAll').onclick(); scanGroups = []; await simScan(); check($('simGo').disabled && SIM.picked.size === 0,'a rescan cannot retain stale merge selections');

  // Mirror extension and sidebar operations remain available on the active project.
  DB = { projects:['Research'], prompts:[], importedNoteIds:[] }; curProj = 'Research'; renderSide();
  check($('sideList').querySelector('.ops').children.length === 2,'active projects expose open-folder and copy-path actions');
  const select = $('mirrorExt'); select.value = '.md'; await select.onchange();
  check(stored.mirrorExt === '.md','mirror extension change persists the selected format');
  check([...$('sideList').querySelector('.ops').children].map(e => e.textContent).join('') === '📂📋','sidebar actions use the folder and clipboard icons');
  return checks;
}

try {
  for (const locale of ['en','zh-CN']) {
    let html = fs.readFileSync(path.join(root, `app.${locale}.html`), 'utf8');
    const start = html.indexOf('\n(async () => {');
    const end = html.indexOf('\n})();', start) + '\n})();'.length;
    if (start < 0 || end < start) throw new Error('Startup boundary not found');
    html = html.slice(0,start) + html.slice(end);
    html = html.replace('<script src="language.js"></script>', '');
    const runner = `<script>(${browserTests.toString()})().then(checks => { document.body.innerHTML = '<pre id="test-result"></pre>'; document.getElementById('test-result').textContent = JSON.stringify({ok:true,checks}); }).catch(error => { document.body.innerHTML = '<pre id="test-result"></pre>'; document.getElementById('test-result').textContent = JSON.stringify({ok:false,error:error.stack}); });</script>`;
    html = html.replace('</body>', runner + '</body>');
    const file = path.join(tmp, `${locale}.html`); fs.writeFileSync(file,html);
    const result = spawnSync(chrome,['--headless','--disable-gpu','--no-first-run','--no-default-browser-check',`--user-data-dir=${path.join(tmp,'profile-'+locale)}`,'--virtual-time-budget=12000','--dump-dom','file://'+file], {encoding:'utf8',timeout:35000,maxBuffer:4*1024*1024});
    const match = result.stdout?.match(/<pre id="test-result">([\s\S]*?)<\/pre>/);
    if (!match) throw new Error(`No ${locale} test result: ${result.error || result.stderr}\n${result.stdout?.slice(-1000)}`);
    const report = JSON.parse(match[1].replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&'));
    if (!report.ok) throw new Error(`${locale}: ${report.error}`);
    console.log(`${locale}: ${report.checks.length} browser regression checks passed`);
  }
} finally { fs.rmSync(tmp,{recursive:true,force:true}); }
