from pathlib import Path
import sys

path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/app/web/index.html")
html = path.read_text(encoding="utf-8")


def replace_exact(label: str, old: str, new: str) -> None:
    global html
    count = html.count(old)
    if count != 1:
        raise SystemExit(
            f"{label}: expected exactly one patch target, found {count}. "
            "The pinned upstream UI may have changed."
        )
    html = html.replace(old, new, 1)


replace_exact(
    "state",
    "let deviceAliases = {};",
    "let deviceAliases = {};\nlet haDevices = [];",
)

replace_exact(
    "ingress-safe fetch",
    "let cfg = {};",
    """let cfg = {};
/* Ingress-safe fetch: relative API base + CSRF header for state-changing calls. */
(function(){
  const nativeFetch=window.fetch.bind(window);
  window.fetch=function(input,init){
    if(typeof input==='string'&&input.charAt(0)==='/')input='.'+input;
    if(init&&init.method&&String(init.method).toUpperCase()!=='GET'){
      const h=init.headers;
      if(h&&typeof h.set==='function'){h.set('X-Requested-With','xiaoai-bridge');}
      else{init.headers=Object.assign({},h||{},{'X-Requested-With':'xiaoai-bridge'});}
    }
    return nativeFetch(input,init).then(function(r){
      if(r.status===401&&!window.__bridgeAuthReloaded){
        window.__bridgeAuthReloaded=true;
        document.documentElement.innerHTML='<body style="font-family:system-ui;padding:40px;color:#111">会话已失效，正在返回登录页…</body>';
        setTimeout(function(){location.reload()},600);
      }
      return r;
    });
  };
})();""",
)

replace_exact(
    "clear login credentials button",
    '<button class="btn btn-primary" onclick="saveXiaomi()">&darr;保存</button>',
    '<button class="btn" onclick="clearXiaomiLogin()">&#128465; 清除登录凭据</button>\n      '
    '<button class="btn btn-primary" onclick="saveXiaomi()">&darr;保存</button>',
)

replace_exact(
    "clear login credentials function",
    "// ═══════════ Save ═══════════",
    r"""// ═══════════ Save ═══════════
async function clearXiaomiLogin(){
  if(!confirm('确定要清除已保存的小米登录凭据吗？\n将删除 auth.json、.mi.token 与配置中的 Cookie，之后需要重新扫码登录。'))return;
  const a=document.getElementById('xiaomi-alert');
  try{
    const r=await fetch('/api/xiaomi/clear-login',{method:'POST'});
    const d=await r.json();
    showAlert(a,d.ok?'ok':'err',(d.ok?'✓ ':'✗ ')+(d.msg||''));
    if(d.ok){
      try{
        const fresh=await (await fetch('/api/config')).json();
        cfg.xiaomi=(fresh&&fresh.xiaomi)||{};
        // 清除凭据会改写 xiaomi 分区，服务端版本随之变化；必须同步到 saveCfg 真正读取的
        // sectionRevisions，否则下一次保存会被 CAS 判成「另一窗口修改」并 409。
        const revision=fresh&&fresh._section_revisions&&fresh._section_revisions.xiaomi;
        if(revision)sectionRevisions.xiaomi=revision;
        safeSet('mi-username',(cfg.xiaomi||{}).username||'');
        safeSet('mi-password',(cfg.xiaomi||{}).password||'');
        safeSet('mi-cookie-text',(cfg.xiaomi||{}).cookie_text||'');
      }catch(e){}
    }
  }catch(e){showAlert(a,'err','✗ '+e.message);}
}""",
)

replace_exact(
    "initial entity load",
    "await loadConfig();\n  renderRules();",
    "await loadConfig();\n  renderRules();\n  bindRulesDirty();\n  loadRuleDevices().then(syncRuleEntityOptions);",
)

replace_exact(
    "rules page refresh",
    "if(name==='logs')fetchLogs();if(name==='devices')loadDevices();if(name==='schedules')loadSchedules();",
    "if(name==='logs')fetchLogs();if(name==='devices')loadDevices();if(name==='schedules')loadSchedules();if(name==='rules')loadRuleDevices().then(syncRuleEntityOptions);",
)

replace_exact(
    "compact rule list styling",
    "/* ── Rule List ── */",
    """/* ── Rule List ── */
.rule-item.collapsed .rule-grid{display:none}
.rule-item.collapsed .rule-head{margin-bottom:0}
.rule-head{gap:10px}
.rule-summary{flex:1;min-width:0;line-height:1.5}
.rule-summary-pattern{font-size:13px;font-weight:600;color:var(--txt-primary);overflow-wrap:anywhere}
.rule-summary-meta{font-size:11px;color:var(--txt-muted);margin-top:3px}
.rule-summary-targets{font-size:11px;color:var(--txt-secondary);margin-top:4px;overflow-wrap:anywhere}
.rule-expand{background:var(--surface-bg);border:1px solid var(--border-default);border-radius:var(--r-sm);color:var(--txt-secondary);cursor:pointer;padding:4px 9px;white-space:nowrap}
.rule-expand:hover{border-color:var(--border-accent);color:var(--txt-primary)}
""",
)

replace_exact(
    "rule search control",
    '<div class="rule-list" id="rule-list"></div>',
    '<div class="test-row"><input type="search" id="rule-filter" placeholder="搜索语句或设备" oninput="filterRules()"><span id="rule-count" style="white-space:nowrap;color:var(--txt-muted);font-size:12px"></span></div>\n      <div class="rule-list" id="rule-list"></div>',
)

replace_exact(
    "unsaved edit indicator",
    '<button class="btn btn-primary" onclick="saveRules()">&darr;保存</button>',
    '<button class="btn btn-primary" onclick="saveRules()">&darr;保存全部</button>'
    '<span id="rules-dirty" style="display:none;color:var(--orange);font-size:12px;align-self:center">&#9679; 有未保存修改</span>',
)

replace_exact(
    'rule picker styles',
    r'''</style>''',
    r""".entity-picker{border:1px solid var(--border-default);border-radius:var(--r-sm);background:var(--surface-bg);padding:8px;display:flex;flex-direction:column;gap:6px}
.entity-toolbar{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.entity-toolbar input[type=search]{flex:1 1 140px;min-width:120px}
.entity-count{font-size:11px;color:var(--txt-muted);white-space:nowrap}
.entity-options{max-height:168px;overflow:auto;display:flex;flex-direction:column;gap:2px;padding-right:2px}
.entity-option{display:flex;align-items:center;gap:6px;padding:3px 4px;border-radius:var(--r-sm);cursor:pointer;font-size:12px;line-height:1.5}
.entity-option:hover{background:var(--hover-bg)}
.entity-option input[type=checkbox]{margin:0;flex:none}
.entity-option .entity-name{color:var(--txt-primary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.entity-option .entity-id{color:var(--txt-muted);font-size:10px;margin-left:auto;white-space:nowrap}
.entity-option.stale .entity-name{color:var(--txt-muted)}
.entity-empty{font-size:11px;color:var(--txt-muted);padding:2px 4px}
.rule-actions{display:flex;gap:6px;align-items:center;white-space:nowrap}
.rule-actions .rule-expand,.rule-actions .rule-save,.rule-actions .rule-del{background:var(--surface-bg);border:1px solid var(--border-default);border-radius:var(--r-sm);color:var(--txt-secondary);cursor:pointer;padding:4px 9px;white-space:nowrap;font-size:12px}
.rule-actions .rule-save{border-color:var(--border-accent);color:var(--accent)}
.rule-actions .rule-save:hover{background:var(--accent);border-color:var(--accent);color:#fff}
.rule-actions .rule-expand:hover{border-color:var(--border-accent);color:var(--txt-primary)}
.rule-chip{font-size:10px;color:var(--orange);border:1px solid var(--orange);border-radius:999px;padding:1px 7px;white-space:nowrap}
.btn-mini{font-size:11px;padding:3px 8px}
</style>""",
)

replace_exact(
    'rule helpers',
    r'''// ═══════════ DOMAIN INFO ═══════════''',
    r"""// ═══════════ Rule entities (checkbox picker) ═══════════
async function loadRuleDevices(){
  try{
    const r=await fetch('/api/devices');
    const d=await r.json();
    haDevices=d.ok&&Array.isArray(d.devices)?d.devices:[];
  }catch(e){
    haDevices=[];
  }
}
function ruleEntityIds(rule){
  const value=((rule||{}).action||{}).entity_id;
  return Array.isArray(value)?value.filter(Boolean):(value?[value]:[]);
}
function ruleTargetNames(rule){
  return ruleEntityIds(rule).map(id=>{
    const device=haDevices.find(item=>item.entity_id===id);
    return deviceAliases[id]||(device&&device.name)||id;
  });
}
function ruleName(rule){
  const value=(rule||{}).name;
  return typeof value==='string'?value.trim():'';
}
function ruleTitle(rule,index){
  const named=ruleName(rule);
  if(named)return named;
  const pattern=String((rule||{}).pattern||'').trim();
  if(pattern)return pattern;
  return '新规则 #'+(index+1);
}
function ruleSearchText(rule){
  return [ruleName(rule),rule.pattern||'',...ruleEntityIds(rule),...ruleTargetNames(rule)].join(' ').toLowerCase();
}
function ruleSummaryHtml(rule){
  const action=(rule||{}).action||{};
  const info=DINFO[action.domain]||{};
  const operation=(info.s||{})[action.service]||action.service||'未设置操作';
  const targets=ruleTargetNames(rule);
  const pattern=String(rule.pattern||'').trim();
  const shownPattern=pattern||'未填写触发语句';
  return '<div class="rule-summary-pattern">'+escHtml(shownPattern)+'</div>'+
    '<div class="rule-summary-meta">'+escHtml(info.name||action.domain||'设备')+' · '+escHtml(operation)+' · '+targets.length+' 个设备</div>'+
    '<div class="rule-summary-targets">'+escHtml(targets.join('、')||'尚未选择设备')+'</div>';
}
function entityOptionItems(domain,current){
  const selected=new Set((current||[]).filter(Boolean));
  const list=(haDevices||[]).filter(d=>d.domain===domain)
    .slice()
    .sort((a,b)=>String(deviceAliases[a.entity_id]||a.name||a.entity_id).localeCompare(String(deviceAliases[b.entity_id]||b.name||b.entity_id),'zh-CN'));
  const rows=list.map(d=>({
    id:d.entity_id,
    label:deviceAliases[d.entity_id]||d.name||d.entity_id,
    stale:false,
  }));
  for(const id of selected){
    if(!rows.some(row=>row.id===id))rows.push({id,label:id,stale:true});
  }
  return rows;
}
function entityRowHtml(row,checked){
  const search=(String(row.label||'')+' '+row.id).toLowerCase();
  return '<label class="entity-option'+(row.stale?' stale':'')+'" data-search="'+escHtml(search)+'">'+
    '<input type="checkbox" class="r-e" value="'+escHtml(row.id)+'"'+(checked?' checked':'')+'>'+
    '<span class="entity-name">'+escHtml(row.label)+(row.stale?'（当前，未在设备列表）':'')+'</span>'+
    '<code class="entity-id">'+escHtml(row.id)+'</code></label>';
}
function entityPickerHtml(domain,current){
  const selected=new Set((current||[]).filter(Boolean));
  const rows=entityOptionItems(domain,current);
  let h='<div class="entity-picker" data-domain="'+escHtml(domain)+'">'+
    '<div class="entity-toolbar">'+
      '<input type="search" class="entity-search" placeholder="搜索名称或 entity_id" oninput="filterEntityOptions(this)">'+
      '<button type="button" class="btn btn-mini" onclick="selectAllEntities(this)">全选</button>'+
      '<button type="button" class="btn btn-mini" onclick="clearEntities(this)">清空</button>'+
      '<span class="entity-count">已选择 '+selected.size+' 个设备</span>'+
    '</div>'+
    '<div class="entity-options">'+rows.map(row=>entityRowHtml(row,selected.has(row.id))).join('')+'</div>'+
    '<div class="entity-empty"'+(rows.length?' style="display:none"':'')+'>未读取到可用设备（HA 离线时可先保存已选实体）</div>'+
  '</div>';
  return h;
}
function entityPickerOf(scope){
  return scope?(scope.closest?scope.closest('.entity-picker'):null)||(scope.querySelector?scope.querySelector('.entity-picker'):null)||scope:null;
}
function entityIdsOf(scope){
  const picker=entityPickerOf(scope);
  if(!picker)return [];
  return Array.from(picker.querySelectorAll('.r-e')).filter(box=>box.checked).map(box=>box.value).filter(Boolean);
}
function setEntitySelection(scope,ids){
  const picker=entityPickerOf(scope);
  if(!picker)return;
  const chosen=new Set((ids||[]).filter(Boolean));
  picker.querySelectorAll('.r-e').forEach(box=>{box.checked=chosen.has(box.value)});
  updateEntityCount(picker);
}
function updateEntityCount(picker){
  if(!picker)return 0;
  const count=picker.querySelector('.entity-count');
  const total=Array.from(picker.querySelectorAll('.r-e')).filter(box=>box.checked).length;
  if(count)count.textContent='已选择 '+total+' 个设备';
  return total;
}
function filterEntityOptions(input){
  const picker=input?input.closest('.entity-picker'):null;
  if(!picker)return 0;
  const query=String(input.value||'').trim().toLowerCase();
  let shown=0;
  picker.querySelectorAll('.entity-option').forEach(row=>{
    const hit=!query||String(row.dataset.search||'').includes(query);
    row.style.display=hit?'':'none';
    if(hit)shown++;
  });
  return shown;
}
function visibleEntityOptions(picker){
  return picker?Array.from(picker.querySelectorAll('.entity-option')).filter(row=>row.style.display!=='none'):[];
}
function selectAllEntities(btn){
  const picker=entityPickerOf(btn);
  if(!picker)return 0;
  const rows=visibleEntityOptions(picker);
  rows.forEach(row=>{const box=row.querySelector('.r-e');if(box)box.checked=true});
  return updateEntityCount(picker);
}
function clearEntities(btn){
  const picker=entityPickerOf(btn);
  if(!picker)return 0;
  visibleEntityOptions(picker).forEach(row=>{const box=row.querySelector('.r-e');if(box)box.checked=false});
  return updateEntityCount(picker);
}
function parseRuleExtra(raw){
  // "其他(JSON)" is merged into the action. A typo used to be swallowed silently
  // and a JSON key could even override the visible form fields; both are refused
  // here so a rule can never be saved with a different action than the UI shows.
  const reserved=['domain','service','entity_id','reply'];
  const value=String(raw||'').trim();
  if(!value)return {ok:true,value:{}};
  let parsed;
  try{parsed=JSON.parse(value)}catch(e){return {ok:false,error:'不是合法 JSON：'+e.message}}
  if(!parsed||typeof parsed!=='object'||Array.isArray(parsed))return {ok:false,error:'必须是 JSON 对象'};
  const conflicts=Object.keys(parsed).filter(key=>reserved.indexOf(key)>=0);
  if(conflicts.length)return {ok:false,error:'不能覆盖界面字段：'+conflicts.join(', ')};
  return {ok:true,value:parsed};
}
function ruleExtraIssue(row){
  return (row&&row.dataset&&row.dataset.extraError)?row.dataset.extraError:'';
}
function firstRuleExtraIssue(){
  const rows=document.querySelectorAll('#rule-list .rule-item');
  for(const row of rows){const issue=ruleExtraIssue(row);if(issue)return {row:row,issue:issue}}
  return null;
}
function ruleSignature(draft){try{return JSON.stringify(draft||{})}catch(e){return String(Date.now())}}
function ruleDraftFromRow(row){
  const ids=entityIdsOf(row);
  const action={
    domain:(row.querySelector('.r-d')||{}).value||'light',
    service:(row.querySelector('.r-s')||{}).value||'turn_on',
    entity_id:ids.length===1?ids[0]:(ids.length?ids:''),
    reply:((row.querySelector('.r-r')||{}).value||'').trim()||'好的',
  };
  row.querySelectorAll('.r-xk').forEach(inp=>{const v=(inp.value||'').trim();if(v)action[inp.dataset.key]=v});
  const extra=((row.querySelector('.r-j')||{}).value||'').trim();
  const parsedExtra=parseRuleExtra(extra);
  if(parsedExtra.ok){Object.assign(action,parsedExtra.value);delete row.dataset.extraError}
  else{row.dataset.extraError=parsedExtra.error}
  const rule={pattern:((row.querySelector('.r-p')||{}).value||'').trim(),action};
  const name=((row.querySelector('.r-n')||{}).value||'').trim();
  if(name)rule.name=name;
  return rule;
}
function collectRuleDrafts(){const rows=document.querySelectorAll('#rule-list .rule-item');rows.forEach((row,index)=>{const state=ruleState[index];if(state)state.draft=ruleDraftFromRow(row)});return ruleState.map(state=>cloneRule(state.draft))}
function collectRulesFromUI(){
  const items=document.querySelectorAll('#rule-list .rule-item');
  cfg.intent_rules=Array.from(items).map(row=>ruleDraftFromRow(row));
  return cfg.intent_rules;
}
function filterRules(){
  const query=((document.getElementById('rule-filter')||{}).value||'').trim().toLowerCase();
  const rows=document.querySelectorAll('#rule-list .rule-item');
  let shown=0;
  rows.forEach(row=>{
    const visible=!query||String(row.dataset.search||'').includes(query);
    row.style.display=visible?'':'none';
    if(visible)shown++;
  });
  const count=document.getElementById('rule-count');
  if(count)count.textContent=shown+'/'+rows.length+' 条';
}
function toggleRule(button){
  const row=button.closest('.rule-item');
  if(!row)return;
  const collapsed=row.classList.toggle('collapsed');
  button.textContent=collapsed?'编辑':'收起';
  const index=Number(row.dataset.idx);
  if(!Number.isNaN(index)&&ruleState[index])ruleState[index].open=!collapsed;
}
function setRulesDirty(on){
  const flag=document.getElementById('rules-dirty');
  if(flag)flag.style.display=on?'':'none';
}
function anyRuleDirty(){
  return ruleState.some(state=>state.dirty)||ruleDeleted.length>0||ruleBulkPending;
}
function markRulesDirty(){setRulesDirty(anyRuleDirty())}
function bindRulesDirty(){
  const list=document.getElementById('rule-list');
  if(!list||list.dataset.dirtyBound)return;
  list.dataset.dirtyBound='1';
  list.addEventListener('input',onRuleEdited);
  list.addEventListener('change',onRuleEdited);
}
function onRuleEdited(event){
  const target=event&&event.target;
  const row=target&&target.closest?target.closest('.rule-item'):null;
  if(!row)return;
  const index=Number(row.dataset.idx);
  if(Number.isNaN(index)||!ruleState[index])return;
  ruleState[index].dirty=true;
  ruleState[index].draft=ruleDraftFromRow(row);
  renderRuleHead(row,ruleState[index],index);
  setRulesDirty(anyRuleDirty());
}
function renderRuleHead(row,state,index){
  if(!row||!state)return;
  const title=row.querySelector('.rule-title');
  if(title)title.textContent=ruleTitle(state.draft,index);
  const summary=row.querySelector('.rule-summary');
  if(summary)summary.innerHTML=ruleSummaryHtml(state.draft);
  const chip=row.querySelector('.rule-chip');
  if(chip)chip.style.display=state.dirty?'':'none';
  row.dataset.search=ruleSearchText(state.draft);
}
function entityOptionsSig(domain){
  const rows=entityOptionItems(domain,[]);
  return String(domain)+'|'+rows.map(row=>row.id+'='+row.label).join('|');
}
function entityPickerMarkup(domain,current,sig){
  return entityPickerHtml(domain,current).replace('<div class="entity-picker"',
    '<div class="entity-picker" data-sig="'+escHtml(sig)+'"');
}
function syncRuleEntityOptions(){
  const rows=document.querySelectorAll('#rule-list .rule-item');
  rows.forEach(row=>{
    const picker=row.querySelector('.entity-picker');
    if(!picker)return;
    const domain=(row.querySelector('.r-d')||{}).value||'light';
    const sig=entityOptionsSig(domain);
    if(picker.dataset.sig===sig)return;
    const current=entityIdsOf(row);
    picker.outerHTML=entityPickerMarkup(domain,current,sig);
  });
  return rows.length;
}
let ruleState=[];
let ruleBaseline=[];
let ruleDeleted=[];
let ruleBulkPending=false;
let ruleSeq=0;
let ruleStateReady=false;
function nextRuleUid(){ruleSeq+=1;return 'rule-'+ruleSeq}
function cloneRule(rule){
  try{return JSON.parse(JSON.stringify(rule||{}))}catch(e){return {pattern:'',action:{domain:'light',service:'turn_on',entity_id:'',reply:'好的'}}}
}
function ruleStateInit(rules){
  ruleBaseline=[];
  ruleState=(rules||[]).map(rule=>{
    const uid=nextRuleUid();
    ruleBaseline.push({uid,rule:cloneRule(rule)});
    return {uid,base:true,draft:cloneRule(rule),dirty:false,open:false};
  });
  ruleDeleted=[];
  ruleBulkPending=false;
  ruleStateReady=true;
}
function ruleIndexByUid(uid){
  return ruleState.findIndex(state=>state.uid===uid);
}
function persistRules(mergedRules){
  cfg.intent_rules=mergedRules;
  return saveCfg('rules');
}

async function saveOneRule(i){
  if(!ruleStateReady)ruleStateInit(cfg.intent_rules||[]);
  const alerts=document.getElementById('rules-alert');
  if(ruleBulkPending){showAlert(alerts,'err','✗ 已导入新的规则列表，请先点「保存全部」');return false}
  if(ruleDeleted.length>0){showAlert(alerts,'err','✗ 有规则等待删除，请先点「保存全部」');return false}
  const rows=document.querySelectorAll('#rule-list .rule-item');
  const row=rows[i];const state=ruleState[i];
  if(!row||!state)return false;
  const extraIssue=ruleExtraIssue(row);
  if(extraIssue){showAlert(alerts,'err','✗ 其他(JSON) 有误：'+escHtml(extraIssue));return false}
  state.draft=ruleDraftFromRow(row);
  const snapshot=cloneRule(state.draft);
  const signature=ruleSignature(snapshot);
  const merged=ruleBaseline.map(entry=>cloneRule(entry.rule));
  const pos=ruleBaseline.findIndex(entry=>entry.uid===state.uid);
  if(pos>=0)merged[pos]=cloneRule(snapshot);else merged.push(cloneRule(snapshot));
  const ok=await persistRules(merged);
  if(!ok){state.dirty=true;setRulesDirty(true);return false}
  if(pos>=0)ruleBaseline[pos].rule=cloneRule(snapshot);
  else ruleBaseline.push({uid:state.uid,rule:cloneRule(snapshot)});
  state.base=true;state.open=true;
  const live=row.isConnected?ruleDraftFromRow(row):state.draft;
  state.draft=live;
  state.dirty=ruleSignature(live)!==signature;
  cfg.intent_rules=merged;
  if(row.isConnected)renderRuleHead(row,state,i);
  setRulesDirty(anyRuleDirty());
  if(state.dirty)showAlert(alerts,'ok','✓ 已保存，但保存期间产生的新修改尚未保存');
  else showAlert(alerts,'ok','✓ 已保存：'+escHtml(ruleName(state.draft)||state.draft.pattern||('规则 #'+(i+1))));
  return true;
}


// ═══════════ DOMAIN INFO ═══════════""",
)

replace_exact(
    'rules render from drafts',
    r'''function renderRules(){const rules=cfg.intent_rules||[];const list=document.getElementById('rule-list');list.innerHTML='';rules.forEach((r,i)=>{list.appendChild(buildRule(r,i))})}''',
    r"""function renderRules(){const rules=cfg.intent_rules||[];if(!ruleStateReady)ruleStateInit(rules);const list=document.getElementById('rule-list');if(!list)return;list.innerHTML='';ruleState.forEach((state,index)=>{list.appendChild(buildRule(state.draft,index,state))});if(!ruleState.length)list.innerHTML='<div class="empty-state">暂无规则，点「+新增」添加</div>';filterRules()}""",
)

replace_exact(
    'rule row with checkbox picker and per-rule save',
    r'''function buildRule(rule,i){const a=rule.action||{};const d=a.domain||'light';const info=DINFO[d]||DINFO.light;const skip=new Set(['domain','service','entity_id','reply','temperature','brightness_pct','delay_minutes','hvac_mode']);const div=document.createElement('div');div.className='rule-item';div.dataset.idx=i;let h='<div class="rule-head"><span class="rule-num">#'+(i+1)+'</span><button class="rule-del" onclick="deleteRule('+i+')">✕</button></div><div class="rule-grid"><div class="field full"><label>你说的话</label><input type="text" class="r-p" value="'+escHtml(rule.pattern||'')+'" placeholder="打开客厅灯"></div><div class="field" style="grid-column:1/-1"><label>设备类型</label><div class="domain-select">';Object.entries(DINFO).forEach(([k,v])=>{h+='<button class="domain-btn '+(k===d?'active':'')+'" onclick="switchD(this,\''+k+'\')"><span class="domain-icon">'+v.icon+'</span>'+v.name+'</button>'});h+='</div><input type="hidden" class="r-d" value="'+escHtml(d)+'"></div><div class="field"><label>操作</label><select class="r-s" onchange="updX(this)">';Object.entries(info.s).forEach(([k,v])=>{h+='<option value="'+k+'" '+(a.service===k?'selected':'')+'>'+v+'</option>'});h+='</select></div><div class="field"><label>实体ID</label><input type="text" class="r-e" value="'+escHtml(a.entity_id||'')+'" placeholder="climate.xxx"></div><div class="extra-fields">';info.x.forEach(x=>{h+='<div class="field"><label>'+x.l+'</label><input type="text" class="r-xk" data-key="'+x.k+'" value="'+escHtml(a[x.k]||'')+'" placeholder="'+(x.p||'')+'"></div>'});h+='</div><div class="field full"><label>回复</label><input type="text" class="r-r" value="'+escHtml(a.reply||'好的')+'" placeholder="好的"></div><div class="field full"><label>其他(JSON)</label><input type="text" class="r-j" value="'+escHtml(extraJ(a,skip))+'" placeholder=\'{"brightness_pct":"{0}"}\'></div></div>';div.innerHTML=h;return div}''',
    r"""function buildRule(rule,index,state){const a=(rule||{}).action||{};const d=a.domain||'light';const info=DINFO[d]||DINFO.light;const skip=new Set(['domain','service','entity_id','reply','temperature','brightness_pct','delay_minutes','hvac_mode']);const ids=ruleEntityIds(rule);const div=document.createElement('div');div.className='rule-item'+(state&&state.open?'':' collapsed');div.dataset.idx=index;if(state)div.dataset.uid=state.uid;div.dataset.search=ruleSearchText(rule);let h='<div class="rule-head"><span class="rule-num">#'+(index+1)+'</span><div class="rule-summary-wrap"><div class="rule-title">'+escHtml(ruleTitle(rule,index))+'</div><div class="rule-summary">'+ruleSummaryHtml(rule)+'</div></div><span class="rule-chip"'+(state&&state.dirty?'':' style="display:none"')+'>未保存</span><span class="rule-actions"><button class="rule-expand" onclick="toggleRule(this)">'+(state&&state.open?'收起':'编辑')+'</button><button class="rule-save" onclick="saveOneRule('+index+')">保存</button><button class="rule-del" onclick="deleteRule('+index+')">删除</button></span></div><div class="rule-grid"><div class="field full"><label>规则名称（可选）</label><input type="text" class="r-n" value="'+escHtml(ruleName(rule))+'" placeholder="客厅灯组开灯"></div><div class="field full"><label>触发语句（支持匹配表达式）</label><input type="text" class="r-p" value="'+escHtml((rule||{}).pattern||'')+'" placeholder="打开客厅灯"></div><div class="field" style="grid-column:1/-1"><label>设备类型</label><div class="domain-select">';Object.entries(DINFO).forEach(([k,v])=>{h+='<button class="domain-btn '+(k===d?'active':'')+'" onclick="switchD(this,\''+k+'\')"><span class="domain-icon">'+v.icon+'</span>'+v.name+'</button>'});h+='</div><input type="hidden" class="r-d" value="'+escHtml(d)+'"></div><div class="field"><label>操作</label><select class="r-s" onchange="updX(this)">';Object.entries(info.s).forEach(([k,v])=>{h+='<option value="'+k+'" '+(a.service===k?'selected':'')+'>'+v+'</option>'});h+='</select></div><div class="field full"><label>实体 ID（可多选）</label>'+entityPickerHtml(d,ids)+'</div><div class="extra-fields">';info.x.forEach(x=>{h+='<div class="field"><label>'+x.l+'</label><input type="text" class="r-xk" data-key="'+x.k+'" value="'+escHtml(a[x.k]||'')+'" placeholder="'+(x.p||'')+'"></div>'});h+='</div><div class="field full"><label>回复</label><input type="text" class="r-r" value="'+escHtml(a.reply||'好的')+'" placeholder="好的"></div><div class="field full"><label>其他(JSON)</label><input type="text" class="r-j" value="'+escHtml(extraJ(a,skip))+'"></div></div>';div.innerHTML=h;return div}""",
)

replace_exact(
    'domain switch keeps drafts',
    r'''function switchD(btn,d){btn.parentElement.querySelectorAll('.domain-btn').forEach(b=>b.classList.remove('active'));btn.classList.add('active');const item=btn.closest('.rule-item');item.querySelector('.r-d').value=d;const info=DINFO[d];const sel=item.querySelector('.r-s');sel.innerHTML=Object.entries(info.s).map(([k,v])=>'<option value="'+k+'">'+v+'</option>').join('');item.querySelector('.extra-fields').innerHTML=info.x.length?info.x.map(x=>'<div class="field"><label>'+x.l+'</label><input type="text" class="r-xk" data-key="'+x.k+'" value="" placeholder="'+(x.p||'')+'"></div>').join(''):''}''',
    r"""function switchD(btn,d){const item=btn?btn.closest('.rule-item'):null;if(!item)return;const hidden=item.querySelector('.r-d');const prev=hidden?hidden.value:'';if(prev===d)return;btn.parentElement.querySelectorAll('.domain-btn').forEach(b=>b.classList.remove('active'));btn.classList.add('active');if(hidden)hidden.value=d;const info=DINFO[d];const current=entityIdsOf(item).filter(id=>String(id).split('.')[0]===d);const picker=item.querySelector('.entity-picker');if(picker){picker.outerHTML=entityPickerHtml(d,current)}const sel=item.querySelector('.r-s');if(sel){sel.innerHTML=Object.entries(info.s).map(([k,v])=>'<option value="'+k+'">'+v+'</option>').join('');sel.value=Object.keys(info.s)[0]}const extra=item.querySelector('.extra-fields');if(extra){extra.innerHTML=info.x.length?info.x.map(x=>'<div class="field"><label>'+x.l+'</label><input type="text" class="r-xk" data-key="'+x.k+'" value="" placeholder="'+(x.p||'')+'"></div>').join(''):''}const index=Number(item.dataset.idx);if(!Number.isNaN(index)&&ruleState[index]){ruleState[index].draft=ruleDraftFromRow(item);ruleState[index].dirty=true;renderRuleHead(item,ruleState[index],index)}setRulesDirty(anyRuleDirty());return current.length}""",
)

replace_exact(
    'collect rules from checkbox picker',
    r'''function collectRulesFromUI(){const items=document.querySelectorAll('.rule-item');cfg.intent_rules=Array.from(items).map(el=>{const a={domain:el.querySelector('.r-d').value,service:el.querySelector('.r-s').value,entity_id:el.querySelector('.r-e').value.trim(),reply:el.querySelector('.r-r').value.trim()||'好的'};el.querySelectorAll('.r-xk').forEach(inp=>{const v=inp.value.trim();if(v)a[inp.dataset.key]=v});const j=el.querySelector('.r-j').value.trim();if(j){try{Object.assign(a,JSON.parse(j))}catch{}}return{pattern:el.querySelector('.r-p').value.trim(),action:a}})}''',
    r"""function collectRulesFromUI(){const items=document.querySelectorAll('#rule-list .rule-item');cfg.intent_rules=Array.from(items).map(row=>ruleDraftFromRow(row));return cfg.intent_rules}""",
)

replace_exact(
    'add rule as a draft',
    r'''function addRule(){collectRulesFromUI();cfg.intent_rules=cfg.intent_rules||[];cfg.intent_rules.push({pattern:'',action:{domain:'light',service:'turn_on',entity_id:'',reply:'好的'}});renderRules();const last=document.querySelector('.rule-list')?.lastElementChild;if(last)last.scrollIntoView({behavior:'smooth'})}''',
    r"""function addRule(){if(!ruleStateReady)ruleStateInit(cfg.intent_rules||[]);else collectRuleDrafts();ruleState.push({uid:nextRuleUid(),base:false,draft:{pattern:'',action:{domain:'light',service:'turn_on',entity_id:'',reply:'好的'}},dirty:true,open:true});renderRules();markRulesDirty();const last=document.querySelector('#rule-list .rule-item:last-child');if(last)last.scrollIntoView({behavior:'smooth'})}""",
)

replace_exact(
    'delete rule keeps other drafts',
    r'''function deleteRule(i){collectRulesFromUI();cfg.intent_rules.splice(i,1);renderRules()}''',
    r"""function deleteRule(i){collectRuleDrafts();const state=ruleState[i];if(!state)return;if(state.base)ruleDeleted.push(state.uid);ruleState.splice(i,1);renderRules();markRulesDirty()}""",
)

replace_exact(
    'save all rules from drafts',
    r'''async function saveRules(){collectRulesFromUI();await saveCfg('rules')}''',
    r"""async function saveRules(){
  if(!ruleStateReady)ruleStateInit(cfg.intent_rules||[]);
  const alerts=document.getElementById('rules-alert');
  const issue=firstRuleExtraIssue();
  if(issue){showAlert(alerts,'err','✗ 其他(JSON) 有误：'+escHtml(issue.issue));return false}
  collectRuleDrafts();
  const savedUids=[];
  const snapshot=[];
  ruleBaseline.forEach(entry=>{
    if(ruleDeleted.indexOf(entry.uid)>=0)return;
    const state=ruleState[ruleIndexByUid(entry.uid)];
    savedUids.push(entry.uid);
    snapshot.push(cloneRule(state?state.draft:entry.rule));
  });
  ruleState.forEach(state=>{
    if(!state.base){savedUids.push(state.uid);snapshot.push(cloneRule(state.draft))}
  });
  const ok=await persistRules(snapshot);
  if(!ok){setRulesDirty(true);return false}
  ruleBaseline=savedUids.map((uid,index)=>({uid:uid,rule:cloneRule(snapshot[index])}));
  ruleDeleted=[];ruleBulkPending=false;
  ruleState.forEach(state=>{
    const saved=ruleBaseline.find(entry=>entry.uid===state.uid);
    state.base=!!saved;
    state.dirty=!saved||ruleSignature(state.draft)!==ruleSignature(saved.rule);
  });
  cfg.intent_rules=snapshot;
  document.querySelectorAll('#rule-list .rule-item').forEach((row,index)=>{
    if(ruleState[index])renderRuleHead(row,ruleState[index],index);
  });
  const pending=anyRuleDirty();
  setRulesDirty(pending);
  showAlert(alerts,'ok',pending?'✓ 已保存，但保存期间产生的新修改尚未保存':'✓ 已保存全部规则');
  return true;
}
""",
)

replace_exact(
    'export rules from drafts',
    r'''function exportRules(){collectRulesFromUI();const b=new Blob([JSON.stringify(cfg.intent_rules,null,2)],{type:'application/json'});const a=document.createElement('a');a.href=URL.createObjectURL(b);a.download='rules-'+(new Date().toISOString().slice(0,10))+'.json';a.click();URL.revokeObjectURL(a.href);showAlert(document.getElementById('rules-alert'),'ok','✓ 已导出')}''',
    r"""function exportRules(){collectRuleDrafts();const rules=ruleState.map(state=>cloneRule(state.draft));const b=new Blob([JSON.stringify(rules,null,2)],{type:'application/json'});const a=document.createElement('a');a.href=URL.createObjectURL(b);a.download='rules-'+(new Date().toISOString().slice(0,10))+'.json';a.click();URL.revokeObjectURL(a.href);showAlert(document.getElementById('rules-alert'),'ok','✓ 已导出')}""",
)

replace_exact(
    'import rules as pending drafts',
    r'''function importRules(){const inp=document.createElement('input');inp.type='file';inp.accept='.json';inp.onchange=async()=>{try{const t=await inp.files[0].text();const r=JSON.parse(t);if(!Array.isArray(r))throw Error('需JSON数组');cfg.intent_rules=r;renderRules();showAlert(document.getElementById('rules-alert'),'ok','✓ 导入'+r.length+'条')}catch(e){showAlert(document.getElementById('rules-alert'),'err','✗ '+e.message)}};inp.click()}''',
    r"""function importRules(){const inp=document.createElement('input');inp.type='file';inp.accept='.json';inp.onchange=async()=>{try{const text=await inp.files[0].text();const rules=JSON.parse(text);if(!Array.isArray(rules))throw Error('需JSON数组');ruleState=rules.map(rule=>({uid:nextRuleUid(),base:false,draft:cloneRule(rule),dirty:true,open:false}));ruleBaseline=[];ruleDeleted=[];ruleBulkPending=true;ruleStateReady=true;cfg.intent_rules=rules;renderRules();markRulesDirty();showAlert(document.getElementById('rules-alert'),'ok','✓ 导入'+rules.length+'条，点「保存全部」生效')}catch(e){showAlert(document.getElementById('rules-alert'),'err','✗ '+e.message)}};inp.click()}""",
)

replace_exact(
    "save result must report failure and be awaitable",
    "showAlert(a,d.ok?'ok':'err',(d.ok?'✓ ':'✗ ')+(d.ok?'保存成功':'保存失败'))}catch(e){showAlert(a,'err','✗ '+e.message)}setTimeout(()=>a.classList.remove('show'),3000)}",
    "const ok=!!(d&&d.ok);showAlert(a,ok?'ok':'err',(ok?'✓ ':'✗ ')+(ok?'保存成功':(d.msg||'保存失败')));setTimeout(()=>a.classList.remove('show'),3000);return ok}catch(e){showAlert(a,'err','✗ '+e.message);setTimeout(()=>a.classList.remove('show'),3000);return false}}",
)

replace_exact(
    "allow QR-only Xiaomi connection test",
    "if(!gv('mi-username')&&!(cfg.xiaomi||{}).username){showAlert(a,'err','✗ 先填写账号');return}",
    "",
)

replace_exact(
    "show QR login failure honestly",
    "showAlert(a,'ok','✓ '+d.msg);const r2=await fetch('/api/test/xiaomi'",
    "showAlert(a,d.ok?'ok':'err',(d.ok?'✓ ':'✗ ')+d.msg);if(!d.ok)return;const r2=await fetch('/api/test/xiaomi'",
)

replace_exact(
    "reveal direct access token button",
    '<button class="btn btn-primary" onclick="saveAdvanced()">&darr;保存</button>',
    '<button class="btn" onclick="revealDirectToken()">&#128273; 显示直连令牌</button>\n'
    '      <button class="btn btn-primary" onclick="saveAdvanced()">&darr;保存</button>',
)

replace_exact(
    "reveal direct access token function",
    "function toggleAISettings(){",
    """function revealDirectToken(){
  const a=document.getElementById('adv-alert');
  fetch('/api/auth/token').then(r=>r.json()).then(d=>{
    if(!d.ok){showAlert(a,'err','✗ '+(d.msg||'无法获取直连令牌'));return;}
    showAlert(a,'ok','直连令牌：'+d.token);
    if(navigator.clipboard&&navigator.clipboard.writeText){
      navigator.clipboard.writeText(d.token).catch(()=>{});
    }
  }).catch(e=>showAlert(a,'err','✗ '+e.message));
}

function toggleAISettings(){""",
)

# ════════════════ Xiaomi 页面重构（1.0.12）：扫码 / 账号密码+验证码 / 高级 Cookie ════════════════
replace_exact(
    "xiaomi login: three entries (QR / password + code / advanced cookie)",
    r'''      <div class="card">
        <div class="card-head"><div class="card-title"><span>&#9673;</span>登录信息</div></div>
        <div class="form-grid">
          <div class="field"><label>账号（手机号/邮箱）</label><input type="text" id="mi-username" placeholder="your@email.com"></div>
          <div class="field"><label>密码</label><div class="input-row"><input type="password" id="mi-password" placeholder="&bull;&bull;&bull;&bull;&bull;&bull;&bull;&bull;"><button class="btn" onclick="testXiaomi()">&#9889;测试</button><button class="btn btn-primary" onclick="qrLogin()">&#128247;扫码</button></div></div>
        </div>
      </div>
      <div class="card">
        <div class="card-head"><div class="card-title"><span>&#9673;</span>Cookie登录 <span style="color:var(--green);text-transform:none;font-weight:400;font-size:10px;letter-spacing:0">免短信</span></div></div>
        <div style="font-size:12px;color:var(--txt-muted);line-height:1.8;margin-bottom:10px">
          浏览器已登录小米时直接复制 Cookie 跳过短信。<br>
          ① 打开 <a href="https://account.xiaomi.com" target="_blank" style="color:var(--accent)">account.xiaomi.com</a><br>
          ② <b style="color:var(--txt-secondary)">F12</b> &rarr; <b style="color:var(--txt-secondary)">Application</b> &rarr; <b style="color:var(--txt-secondary)">Cookies</b><br>
          ③ 复制所有 Cookie（含 userId / passToken / serviceToken）<br>
          ④ 粘贴下方 &rarr; 点「测试」
        </div>
        <div class="field"><label>Cookie 文本</label><textarea id="mi-cookie-text" rows="2" placeholder="userId=xxx; passToken=xxx; serviceToken=xxx; ..."></textarea></div>
      </div>
      <div id="mi-sts-card" class="card" style="display:none"><div class="card-head"><div class="card-title"><span>&#128241;</span>短信验证</div></div><div style="font-size:12px;color:var(--txt-muted);line-height:1.8">① 点击上方链接验证<br>② 成功后复制 Cookie 到上方输入框 &rarr; 测试</div></div>
''',
    r"""      <div class="card" id="mi-qr-card">
        <div class="card-head"><div class="card-title"><span>&#128247;</span>扫码登录（推荐）</div></div>
        <div style="font-size:12px;color:var(--txt-muted);line-height:1.8;margin-bottom:10px">
          用米家 App 扫码，不需要密码、也不涉及短信验证码，是最稳妥的登录方式。
        </div>
        <div class="input-row">
          <button class="btn btn-primary" id="mi-qr-start" onclick="qrLogin()">&#128247; 扫码登录</button>
          <button class="btn" onclick="clearXiaomiLogin()">&#128465; 清除登录凭据</button>
        </div>
        <div class="qr-box" id="qr-area" style="display:none;margin-top:12px">
          <div style="font-size:12px;color:var(--txt-muted);margin-bottom:8px" id="qr-msg">用米家App扫码</div>
          <img id="qr-img" src="" style="width:170px;height:170px;border:2px solid var(--border-default);border-radius:var(--r-lg);background:#fff" alt="QR">
          <div style="margin-top:6px;font-size:11px;color:var(--txt-dim)" id="qr-status"></div>
        </div>
      </div>
      <div class="card" id="mi-password-card">
        <div class="card-head"><div class="card-title"><span>&#9673;</span>账号密码登录</div></div>
        <div style="font-size:12px;color:var(--txt-muted);line-height:1.8;margin-bottom:10px">
          小米要求二次验证时，验证码直接在这里提交，不需要打开浏览器。
        </div>
        <div class="form-grid">
          <div class="field"><label>账号（手机号/邮箱）</label><input type="text" id="mi-username" placeholder="your@email.com" autocomplete="username"></div>
          <div class="field"><label>密码</label><input type="password" id="mi-password" placeholder="&bull;&bull;&bull;&bull;&bull;&bull;&bull;&bull;" autocomplete="current-password"></div>
        </div>
        <div class="input-row" style="margin-top:10px">
          <button class="btn btn-primary" id="mi-login-submit" onclick="startPasswordLogin()">&#128273; 登录</button>
          <span id="mi-login-status" style="font-size:11px;color:var(--txt-muted)"></span>
        </div>
        <div id="mi-otp-panel" style="display:none;margin-top:12px;padding:12px;background:var(--page-bg);border:1px solid var(--border-default);border-radius:var(--r-md)">
          <div style="font-size:12px;color:var(--txt-secondary);line-height:1.8" id="mi-otp-method"></div>
          <div class="input-row" style="margin-top:8px">
            <button class="btn" id="mi-otp-send" onclick="sendOtp()">获取验证码</button>
            <span id="mi-otp-cooldown" style="font-size:11px;color:var(--txt-muted)"></span>
          </div>
          <div class="field" style="margin-top:8px"><label>验证码</label><input id="mi-otp-code" type="text" inputmode="numeric" maxlength="8" autocomplete="one-time-code" placeholder="4-8 位数字"></div>
          <div class="input-row" style="margin-top:8px">
            <button class="btn btn-primary" id="mi-otp-verify" onclick="verifyOtp()">提交验证码</button>
            <button class="btn" id="mi-otp-restart" onclick="restartOtp()">重新开始</button>
          </div>
          <div id="mi-otp-status" style="font-size:12px;color:var(--txt-muted);margin-top:8px"></div>
        </div>
      </div>
      <details class="card" id="mi-advanced-card">
        <summary class="card-title"><span>&#9881;</span>高级选项：手动 Cookie 登录</summary>
        <div style="font-size:12px;color:var(--txt-muted);line-height:1.8;margin:10px 0">
          仅在扫码与账号密码都不可用时使用：浏览器已登录小米时，可以复制 Cookie 手动登录。<br>
          ① 打开 <a href="https://account.xiaomi.com" target="_blank" style="color:var(--accent)">account.xiaomi.com</a> 并登录<br>
          ② <b style="color:var(--txt-secondary)">F12</b> &rarr; <b style="color:var(--txt-secondary)">Application</b> &rarr; <b style="color:var(--txt-secondary)">Cookies</b><br>
          ③ 复制 <b style="color:var(--txt-secondary)">userId</b> 与 <b style="color:var(--txt-secondary)">passToken</b> 两行；<b>不要</b>复制 serviceToken（浏览器里那个属于网页登录，粘进来会导致校验失败）<br>
          ④ 把这两行分别填到下面两个框里（不用写 userId= 这种前缀）&rarr; 点「验证 Cookie」
        </div>
        <div class="field"><label>userId</label><input id="mi-cookie-userid" placeholder="数字，例如 1234567890" autocomplete="off" oninput="syncCookieText()"></div>
        <div class="field" style="margin-top:8px"><label>passToken</label><input id="mi-cookie-passtoken" placeholder="V1: 开头的那一串" autocomplete="off" oninput="syncCookieText()"></div>
        <textarea id="mi-cookie-text" style="display:none"></textarea>
        <div class="input-row" style="margin-top:8px">
          <button class="btn btn-primary" id="mi-cookie-submit" onclick="verifyCookieLogin()">&#10003; 验证 Cookie</button>
          <span id="mi-cookie-status" style="font-size:11px;color:var(--txt-muted)"></span>
        </div>
        <div id="mi-sts-card" style="margin-top:14px;padding-top:12px;border-top:1px dashed var(--border-default)">
          <div style="font-size:12px;color:var(--txt-muted);line-height:1.8;margin-bottom:8px">
            兼容：粘贴浏览器跳转地址（可能因会话绑定返回 401，不推荐）
          </div>
          <div class="field"><label>验证完成后的跳转地址</label><input id="mi-sts-url" placeholder="https://api2.mina.mi.com/sts?..."></div>
          <div class="input-row" style="margin-top:8px">
            <button class="btn" id="mi-sts-submit" onclick="completeSmsVerify()">&#10003;完成验证</button>
            <span id="mi-sts-status" style="font-size:11px;color:var(--txt-muted)"></span>
          </div>
        </div>
      </details>
""",
)

replace_exact(
    "xiaomi login: drop the standalone QR card (now inside the recommended entry)",
    r'''      <div class="card" id="qr-area" style="display:none"><div class="card-head"><div class="card-title"><span>&#128247;</span>扫码登录</div></div><div class="qr-box"><div style="font-size:12px;color:var(--txt-muted);margin-bottom:8px" id="qr-msg">用米家App扫码</div><img id="qr-img" src="" style="width:170px;height:170px;border:2px solid var(--border-default);border-radius:var(--r-lg);background:#fff" alt="QR"><div style="margin-top:6px;font-size:11px;color:var(--txt-dim)" id="qr-status"></div></div></div>
''',
    "",
)

replace_exact(
    "xiaomi login: collapsed advanced card styling",
    '.qr-box{text-align:center;padding:16px;background:var(--page-bg);border-radius:var(--r-lg);border:1px dashed var(--border-default)}',
    """.qr-box{text-align:center;padding:16px;background:var(--page-bg);border-radius:var(--r-lg);border:1px dashed var(--border-default)}
/* Xiaomi login (1.0.12): advanced entry stays collapsed, OTP code is spaced out */
#mi-advanced-card>summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:8px}
#mi-advanced-card>summary::-webkit-details-marker{display:none}
#mi-otp-panel input{letter-spacing:2px;font-variant-numeric:tabular-nums}
""",
)

# ════════════════ Xiaomi 登录脚本（1.0.12） ════════════════
replace_exact(
    "xiaomi login: OTP + cookie handlers",
    "async function qrLogin(){",
    r"""// ═══════════ Xiaomi account/password + verification code (1.0.12) ═══════════
/* One shared state object: the panel only ever reflects what the backend said. */
let _otp={state:'idle',sent:false,cooldown:0,timer:null};
function otpEl(id){return document.getElementById(id);}
function otpText(id,text){const el=otpEl(id);if(el)el.textContent=text==null?'':String(text);}
function otpShow(show){const panel=otpEl('mi-otp-panel');if(panel)panel.style.display=show?'':'none';}
function otpReset(){
  if(_otp.timer){clearInterval(_otp.timer);_otp.timer=null;}
  _otp.state='idle';_otp.sent=false;_otp.cooldown=0;
  const code=otpEl('mi-otp-code');if(code)code.value='';
  otpText('mi-otp-method','');otpText('mi-otp-cooldown','');otpText('mi-otp-status','');
  const send=otpEl('mi-otp-send');if(send){send.disabled=false;send.textContent='获取验证码';}
  otpShow(false);
}
function otpSetCooldown(seconds){
  if(_otp.timer){clearInterval(_otp.timer);_otp.timer=null;}
  _otp.cooldown=Math.max(0,Math.round(Number(seconds)||0));
  const paint=function(){
    const send=otpEl('mi-otp-send');
    if(_otp.cooldown>0){
      if(send){send.disabled=true;send.textContent='获取验证码（'+_otp.cooldown+'s）';}
      otpText('mi-otp-cooldown','请 '+_otp.cooldown+' 秒后再获取');
    }else{
      if(send){send.disabled=false;send.textContent=_otp.sent?'重新获取验证码':'获取验证码';}
      otpText('mi-otp-cooldown','');
    }
  };
  paint();
  if(_otp.cooldown>0){
    _otp.timer=setInterval(function(){
      _otp.cooldown=Math.max(0,_otp.cooldown-1);
      if(_otp.cooldown<=0){clearInterval(_otp.timer);_otp.timer=null;}
      paint();
    },1000);
  }
}
async function startPasswordLogin(){
  const alertBox=otpEl('xiaomi-alert');
  const username=String((otpEl('mi-username')||{}).value||'').trim();
  const password=String((otpEl('mi-password')||{}).value||'');
  if(!username||!password){showAlert(alertBox,'err','✗ 先填写小米账号和密码');return;}
  otpReset();
  otpText('mi-login-status','⟳ 正在登录小米账号...');
  showAlert(alertBox,'warn','⟳ 正在登录小米账号...');
  const btn=otpEl('mi-login-submit');if(btn)btn.disabled=true;
  try{
    const r=await fetch('/api/test/xiaomi/otp/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:username,password:password,use_saved:false})});
    const d=await r.json();
    if(d&&d.ok&&d.need_otp){
      _otp.state='pending';_otp.sent=false;
      otpShow(true);
      const method=d.method==='Email'?'邮箱':'手机短信';
      otpText('mi-otp-method','验证方式：'+method+(d.phone_hint?'（'+d.phone_hint+'）':''));
      otpText('mi-otp-status','');
      otpText('mi-login-status','');
      otpSetCooldown(d.can_resend_in||0);
      showAlert(alertBox,'warn','📱 '+(d.msg||'需要验证码'));
      return;
    }
    if(d&&d.ok){
      otpReset();
      otpText('mi-login-status','');
      showAlert(alertBox,'ok','✓ '+(d.msg||'登录成功'));
      if(d.devices&&d.devices.length>0)renderDeviceList(d.devices);
      return;
    }
    otpText('mi-login-status','');
    showAlert(alertBox,'err','✗ '+((d&&d.msg)||'登录失败'));
  }catch(e){
    otpText('mi-login-status','');
    showAlert(alertBox,'err','✗ '+e.message);
  }finally{
    if(btn)btn.disabled=false;
  }
}
async function sendOtp(){
  const alertBox=otpEl('xiaomi-alert');
  if(_otp.state!=='pending'){showAlert(alertBox,'err','✗ 请先点击「登录」');return;}
  if(_otp.cooldown>0){otpText('mi-otp-cooldown','请 '+_otp.cooldown+' 秒后再获取');return;}
  otpText('mi-otp-status','⟳ 正在发送验证码...');
  try{
    const r=await fetch('/api/test/xiaomi/otp/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({})});
    const d=await r.json();
    if(d&&d.ok&&d.sent){
      _otp.sent=true;
      otpText('mi-otp-status',d.msg||'验证码已发送');
      otpSetCooldown(d.can_resend_in||60);
      return;
    }
    if(d&&d.action==='wait'){
      otpSetCooldown(d.retry_after||60);
      otpText('mi-otp-status',d.msg||'请稍后再试');
      return;
    }
    if(d&&d.action==='restart'){
      otpReset();
      showAlert(alertBox,'err','✗ '+((d&&d.msg)||'验证会话已失效，请重新点击「登录」'));
      return;
    }
    otpText('mi-otp-status','✗ '+((d&&d.msg)||'发送失败'));
  }catch(e){
    otpText('mi-otp-status','✗ '+e.message);
  }
}
async function verifyOtp(){
  const alertBox=otpEl('xiaomi-alert');
  const codeInput=otpEl('mi-otp-code');
  const code=String((codeInput&&codeInput.value)||'').replace(/\s+/g,'');
  if(!/^\d{4,8}$/.test(code)){otpText('mi-otp-status','✗ 请输入 4-8 位数字验证码');return;}
  otpText('mi-otp-status','⟳ 正在提交验证码...');
  const btn=otpEl('mi-otp-verify');if(btn)btn.disabled=true;
  try{
    const r=await fetch('/api/test/xiaomi/otp/verify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:code})});
    const d=await r.json();
    if(d&&d.ok){
      otpReset();
      showAlert(alertBox,'ok','✓ '+(d.msg||'登录成功'));
      if(d.devices&&d.devices.length>0)renderDeviceList(d.devices);
      return;
    }
    if(d&&d.action==='retry_code'){
      const left=(typeof d.attempts_left==='number')?d.attempts_left:null;
      otpText('mi-otp-status','✗ '+((d.msg)||'验证码不正确')+(left===null?'':'（还可尝试 '+left+' 次）'));
      return;
    }
    if(d&&d.action==='restart'){
      otpReset();
      showAlert(alertBox,'err','✗ '+((d&&d.msg)||'验证会话已失效，请重新点击「登录」'));
      return;
    }
    otpText('mi-otp-status','✗ '+((d&&d.msg)||'验证失败'));
    if(d&&d.action==='unsupported'){showAlert(alertBox,'err','✗ '+(d.msg||'当前流程暂不支持'));}
  }catch(e){
    otpText('mi-otp-status','✗ '+e.message);
  }finally{
    if(btn)btn.disabled=false;
  }
}
async function restartOtp(){
  const alertBox=otpEl('xiaomi-alert');
  try{
    await fetch('/api/test/xiaomi/otp/restart',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({})});
  }catch(e){}
  otpReset();
  const password=otpEl('mi-password');if(password)password.value='';
  showAlert(alertBox,'warn','已重新开始，请重新点击「登录」获取新的验证码');
}
function cookieFieldValue(id,key){
  const el=otpEl(id);let v=String((el&&el.value)||'').trim();
  if(!v)return '';
  const m=v.match(new RegExp(key+'\\s*=\\s*([^;\\s]+)','i'));
  if(m)v=m[1];
  return v.replace(/^["']|["']$/g,'').trim();
}
function syncCookieText(){
  const user=cookieFieldValue('mi-cookie-userid','userId');
  const token=cookieFieldValue('mi-cookie-passtoken','passToken');
  const box=otpEl('mi-cookie-text');
  if(box)box.value=(user&&token)?('userId='+user+'; passToken='+token):'';
}
function prefillCookieFields(){
  try{
    const box=otpEl('mi-cookie-text');
    if(!box)return;
    const text=String(box.value||'');
    const user=(text.match(/userId\s*=\s*([^;\s]+)/)||[])[1]||'';
    const token=(text.match(/passToken\s*=\s*([^;\s]+)/)||[])[1]||'';
    const userInput=otpEl('mi-cookie-userid');
    const tokenInput=otpEl('mi-cookie-passtoken');
    if(userInput&&!userInput.value)userInput.value=user;
    if(tokenInput&&!tokenInput.value)tokenInput.value=token;
  }catch(e){}
}
function bindCookiePrefill(){
  try{
    const card=otpEl('mi-advanced-card');
    if(card&&typeof card.addEventListener==='function')card.addEventListener('toggle',prefillCookieFields);
    prefillCookieFields();
  }catch(e){}
}
if(typeof document!=='undefined')bindCookiePrefill();
async function verifyCookieLogin(){
  const alertBox=otpEl('xiaomi-alert');
  const user=cookieFieldValue('mi-cookie-userid','userId');
  const token=cookieFieldValue('mi-cookie-passtoken','passToken');
  if(!user||!token){
    otpText('mi-cookie-status','✗ 请检查 userId 与 passToken 两个输入框');
    showAlert(alertBox,'err','✗ 请分别填写 userId 与 passToken（userId 与 passToken 都是必需的）');
    return;
  }
  const text='userId='+user+'; passToken='+token;
  otpText('mi-cookie-status','⟳ 正在校验 Cookie...');
  const btn=otpEl('mi-cookie-submit');if(btn)btn.disabled=true;
  try{
    const r=await fetch('/api/test/xiaomi/cookie-login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cookie_text:text})});
    const d=await r.json();
    if(d&&d.ok){
      otpReset();
      const userInput=otpEl('mi-cookie-userid');if(userInput)userInput.value='';
      const tokenInput=otpEl('mi-cookie-passtoken');if(tokenInput)tokenInput.value='';
      const box=otpEl('mi-cookie-text');if(box)box.value='';
      otpText('mi-cookie-status','');
      showAlert(alertBox,'ok','✓ '+(d.msg||'Cookie 登录成功'));
      if(d.devices&&d.devices.length>0)renderDeviceList(d.devices);
      return;
    }
    otpText('mi-cookie-status','✗ '+((d&&d.action==='fix_cookie')?'请检查 userId 与 passToken':'Cookie 可能已过期，可重试'));
    showAlert(alertBox,'err','✗ '+((d&&d.msg)||'Cookie 登录失败'));
  }catch(e){
    otpText('mi-cookie-status','✗ '+e.message);
    showAlert(alertBox,'err','✗ '+e.message);
  }finally{
    if(btn)btn.disabled=false;
  }
}
async function qrLogin(){""",
)

# ════════════════ 兼容：旧的「粘贴浏览器跳转地址」处理函数 ════════════════
replace_exact(
    "sms verify: finish-verification request handler",
    "async function qrLogin(){",
    """async function completeSmsVerify(){
  const a=document.getElementById('xiaomi-alert');
  const st=document.getElementById('mi-sts-status');
  const box=document.getElementById('mi-sts-url');
  const btn=document.getElementById('mi-sts-submit');
  const url=((box&&box.value)||'').trim();
  if(!url){showAlert(a,'err','✗ 先粘贴短信验证完成后的跳转地址');return;}
  if(st)st.textContent='⟳ 验证中，请稍候...';
  if(btn)btn.disabled=true;
  showAlert(a,'warn','⟳ 正在完成短信验证（兼容路径，可能失败）...');
  try{
    const r=await fetch('/api/test/xiaomi/verify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cookie_text:url})});
    const d=await r.json();
    if(d.ok){
      showAlert(a,'ok','✓ '+(d.msg||'短信验证完成'));
      const card=document.getElementById('mi-sts-card');
      if(card)card.style.display='none';
      if(box)box.value='';
      if(st)st.textContent='';
      if(d.devices&&d.devices.length>0)renderDeviceList(d.devices);
    }else{
      showAlert(a,'err','✗ '+(d.msg||'验证失败'));
      if(st)st.textContent=(d.retry===false?'请重新点击「登录」':'可重试');
    }
  }catch(e){
    showAlert(a,'err','✗ '+e.message);
    if(st)st.textContent='可重试';
  }finally{
    if(btn)btn.disabled=false;
  }
}
async function qrLogin(){""",
)

# ──────────────── Section-scoped saves and safe rule testing ────────────────
replace_exact(
    "send the active config section, not a stale whole-config overwrite",
    "body:JSON.stringify({config:cfg})",
    "body:JSON.stringify({config:cfg,section:sec})",
)

replace_exact(
    "show rule-test execution choice",
    '<div class="test-result" id="test-result">点击测试查看匹配</div>',
    '<label style="display:flex;align-items:center;gap:8px;font-size:12px;margin:8px 0"><input type="checkbox" id="test-execute"> 实际执行此规则（可能操作真实设备或创建自动化）</label><div class="test-result" id="test-result">默认只预览匹配，不执行设备操作</div>',
)

replace_exact(
    "require confirmation for live test execution",
    "const r=await fetch('/api/test/rule',{method:'POST'",
    "const execute=!!((document.getElementById('test-execute')||{}).checked);if(execute&&!confirm('将实际调用 Home Assistant，可能开关设备或创建自动化。确定继续？'))return;const r=await fetch('/api/test/rule',{method:'POST'",
)

replace_exact(
    "pass explicit execute flag to the rule-test endpoint",
    "body:JSON.stringify({text:t,rules:cfg.intent_rules||[]})",
    "body:JSON.stringify({text:t,rules:cfg.intent_rules||[],execute:execute})",
)

# ──────────────── CAS: per-section versioned UI writes ────────────────
replace_exact(
    "remember revision tokens per section",
    "let cfg = {};\n/* Ingress-safe fetch:",
    "let cfg = {};\nlet sectionRevisions = {};\n/* Ingress-safe fetch:",
)

replace_exact(
    "separate revision metadata from editable config",
    "cfg=await r.json();applyConfigToUI()",
    "cfg=await r.json();sectionRevisions=cfg._section_revisions||{};delete cfg._section_revisions;applyConfigToUI()",
)

replace_exact(
    "save only after CAS precondition and preserve unsaved drafts on conflict",
    """async function saveCfg(sec){const map={xiaomi:'xiaomi-alert',ha:'ha-alert',rules:'rules-alert',adv:'adv-alert'};const a=document.getElementById(map[sec]||'dash-alert');try{const r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({config:cfg,section:sec})});const d=await r.json();const ok=!!(d&&d.ok);showAlert(a,ok?'ok':'err',(ok?'✓ ':'✗ ')+(ok?'保存成功':(d.msg||'保存失败')));setTimeout(()=>a.classList.remove('show'),3000);return ok}catch(e){showAlert(a,'err','✗ '+e.message);setTimeout(()=>a.classList.remove('show'),3000);return false}}""",
    """async function saveCfg(sec){
  const map={xiaomi:'xiaomi-alert',ha:'ha-alert',rules:'rules-alert',adv:'adv-alert'};
  const a=document.getElementById(map[sec]||'dash-alert');
  const revision=sectionRevisions[sec];
  if(!revision){
    showAlert(a,'err','✗ 未获取到配置版本，保存已取消。请备份编辑内容后刷新页面');
    return false;
  }
  try{
    const r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({config:cfg,section:sec,revision:revision})});
    const d=await r.json();
    if(r.status===409||d.code==='config_conflict'){
      showAlert(a,'err','⚠ 此分区已在另一窗口修改，服务器拒绝覆盖。你当前未保存的编辑仍在页面中；请先备份，再刷新页面处理冲突。');
      return false;
    }
    if(!r.ok||!d.ok){
      showAlert(a,'err','✗ '+(d.msg||'保存失败'));
      return false;
    }
    if(!d.revision){
      showAlert(a,'err','✗ 服务器未返回新的配置版本，请刷新页面确认保存结果');
      return false;
    }
    sectionRevisions[sec]=d.revision;
    showAlert(a,'ok','✓ 保存成功');
    return true;
  }catch(e){
    showAlert(a,'err','✗ '+e.message);
    return false;
  }
}""",
)

# Refresh the Xiaomi CAS token after a successful server-side credential write,
# but never reload the whole form: other tabs can contain unsaved edits.
replace_exact(
    "Xiaomi-only revision refresh helper",
    "async function startPasswordLogin(){",
    """async function refreshXiaomiRevision(){
  try{
    const r=await fetch('/api/config');
    if(!r.ok)return false;
    const d=await r.json();
    const revision=d&&d._section_revisions&&d._section_revisions.xiaomi;
    if(!revision)return false;
    sectionRevisions.xiaomi=revision;
    return true;
  }catch(e){return false;}
}
async function startPasswordLogin(){""",
)
replace_exact(
    "successful password login refreshes Xiaomi CAS only",
    """    if(d&&d.ok){
      otpReset();
      otpText('mi-login-status','');""",
    """    if(d&&d.ok){
      otpReset();
      await refreshXiaomiRevision();
      otpText('mi-login-status','');""",
)
replace_exact(
    "successful OTP verification refreshes Xiaomi CAS only",
    """    if(d&&d.ok){
      otpReset();
      showAlert(alertBox,'ok','✓ '+(d.msg||'登录成功'));""",
    """    if(d&&d.ok){
      otpReset();
      await refreshXiaomiRevision();
      showAlert(alertBox,'ok','✓ '+(d.msg||'登录成功'));""",
)
replace_exact(
    "successful Cookie login refreshes Xiaomi CAS only",
    """    if(d&&d.ok){
      otpReset();
      const userInput=otpEl('mi-cookie-userid');if(userInput)userInput.value='';""",
    """    if(d&&d.ok){
      otpReset();
      await refreshXiaomiRevision();
      const userInput=otpEl('mi-cookie-userid');if(userInput)userInput.value='';""",
)
replace_exact(
    "successful QR polling refreshes Xiaomi CAS only",
    "showAlert(a,d.ok?'ok':'err',(d.ok?'✓ ':'✗ ')+d.msg);if(!d.ok)return;const r2",
    "showAlert(a,d.ok?'ok':'err',(d.ok?'✓ ':'✗ ')+d.msg);if(!d.ok)return;await refreshXiaomiRevision();const r2",
)
replace_exact(
    "successful legacy SMS completion refreshes Xiaomi CAS only",
    """      showAlert(a,'ok','✓ '+(d.msg||'短信验证完成'));
      const card=""",
    """      await refreshXiaomiRevision();
      showAlert(a,'ok','✓ '+(d.msg||'短信验证完成'));
      const card=""",
)

# A QR scan is followed by a legacy device probe that may rotate serviceToken;
# refresh CAS after that second request, never before it.
replace_exact(
    "refresh Xiaomi CAS after QR follow-up instead of before",
    "await refreshXiaomiRevision();const r2=await fetch('/api/test/xiaomi'",
    "const r2=await fetch('/api/test/xiaomi'",
)
replace_exact(
    "QR follows probe with fresh Xiaomi revision",
    "const d2=await r2.json();if(d2.ok&&d2.devices&&d2.devices.length>0)renderDeviceList(d2.devices)",
    "const d2=await r2.json();await refreshXiaomiRevision();if(d2.ok&&d2.devices&&d2.devices.length>0)renderDeviceList(d2.devices)",
)
replace_exact(
    "manual Xiaomi test also refreshes CAS after success",
    "if(d.ok&&d.devices&&d.devices.length>0)renderDeviceList(d.devices)}catch(e){showAlert(a,'err','✗ '+e.message)}}",
    "if(d.ok&&!d.need_verify)await refreshXiaomiRevision();if(d.ok&&d.devices&&d.devices.length>0)renderDeviceList(d.devices)}catch(e){showAlert(a,'err','✗ '+e.message)}}",
)

replace_exact(
    "schedules list deletes by the HA config id",
    "const id=s.entity_id.replace('automation.','');",
    "const id=(s.config_id||'').trim()||s.entity_id.replace('automation.','');",
)
replace_exact(
    "schedules delete warns when the reload failed",
    "if(d.ok){showAlert(a,'ok','已删除');loadSchedules()}else showAlert(a,'err',d.detail||'失败')",
    "if(d.ok){showAlert(a,d.reloaded===false?'warn':'ok',d.reloaded===false?('⚠ '+(d.msg||'配置已删除，但 HA 重新加载失败')):'已删除');loadSchedules()}else showAlert(a,'err',d.detail||'失败')",
)
replace_exact(
    "schedules empty state mentions the new-task form",
    '<span style="font-size:11px">对小爱说「每天七点半打开热水器」</span>',
    '<span style="font-size:11px">对小爱说「每天七点半打开热水器」，或点上方「＋ 新建定时任务」</span>',
)

# ════════════════ 定时任务：页面表单创建（与语音路径共用 _create_ha_automation） ════════════════
replace_exact(
    "schedules page: new-task form",
    r'''      <div class="btn-group"><button class="btn btn-primary" onclick="loadSchedules()">&#128260;刷新</button><span style="color:var(--txt-muted);font-size:12px;margin-left:8px" id="schedule-count"></span></div>''',
    r'''      <div class="btn-group"><button class="btn btn-primary" onclick="scheduleFormToggle()">&#43; 新建定时任务</button><button class="btn btn-primary" onclick="loadSchedules()">&#128260;刷新</button><button class="btn" onclick="cleanupSchedules()">清理已完成任务</button><span style="color:var(--txt-muted);font-size:12px;margin-left:8px" id="schedule-count"></span></div>
      <div id="schedule-form" style="display:none;margin:12px 0;padding:14px;background:var(--page-bg);border:1px solid var(--border-default);border-radius:var(--r-md)">
        <div style="font-size:14px;font-weight:600;color:var(--txt-primary);margin-bottom:10px">新建定时任务</div>
        <div class="field"><label>选择设备</label>
          <input id="sch-search" placeholder="搜索设备名称…" oninput="scheduleDeviceFilter()" style="margin-bottom:6px">
          <select id="sch-device" onchange="scheduleDeviceChanged()"><option value="">正在读取设备…</option></select>
          <div id="sch-device-hint" style="font-size:11px;color:var(--txt-dim);margin-top:4px">设备列表来自 Home Assistant，只显示可以开关或切换的实体。</div>
        </div>
        <div class="field"><label>执行操作</label><div id="sch-action-wrap" style="display:flex;flex-wrap:wrap;gap:14px"></div></div>
        <div class="form-grid">
          <div class="field"><label>执行时间（24 小时制）</label><input type="time" id="sch-time" step="60" value="07:30" onchange="schedulePreview()"></div>
          <div class="field"><label>重复方式</label><select id="sch-days" onchange="scheduleDaysChanged()"><option value="daily">每天</option><option value="weekdays">工作日</option><option value="weekends">周末</option><option value="custom">自定义星期</option></select></div>
        </div>
        <div class="field" id="sch-days-custom-wrap" style="display:none"><label>选择星期</label>
          <div id="sch-weekdays" style="display:flex;gap:12px;flex-wrap:wrap"></div>
          <input id="sch-days-custom" type="hidden" value="">
        </div>
        <div style="margin:12px 0;padding:10px 12px;background:var(--card-bg);border:1px dashed var(--border-default);border-radius:var(--r-md)">
          <div style="font-size:11px;color:var(--txt-muted);margin-bottom:4px">任务预览</div>
          <div id="sch-preview" style="font-size:13px;color:var(--txt-primary)">先选择设备</div>
        </div>
        <div class="input-row" style="margin-top:8px"><button class="btn btn-primary" id="sch-create" onclick="createSchedule()">&#10003; 创建</button><button class="btn" onclick="scheduleCheck()">检查任务配置</button><button class="btn" onclick="scheduleFormToggle()">取消</button><span id="sch-status" style="font-size:11px;color:var(--txt-muted)"></span></div>
        <details id="sch-advanced" style="margin-top:12px">
          <summary style="cursor:pointer;font-size:12px;color:var(--txt-muted)">高级模式（直接指定 domain / service / entity_id / JSON，熟悉 HA 的用户使用）</summary>
          <div class="form-grid" style="margin-top:8px">
            <div class="field"><label>域 domain</label><input id="sch-domain" value="light" placeholder="light"></div>
            <div class="field"><label>服务 service</label><input id="sch-service" value="turn_on" placeholder="turn_on"></div>
          </div>
          <div class="field"><label>实体 entity_id（多个用逗号分隔）</label><input id="sch-entity" placeholder="light.xiaomi_xxx"></div>
          <div class="field"><label>服务参数（JSON，可留空；如 select.select_option 需要 option）</label><input id="sch-data" placeholder='{"option": "Heat"}'></div>
        </details>
        <div style="font-size:11px;color:var(--txt-dim);margin-top:6px">时间以 Home Assistant 配置的时区为准。任务会显示在下方列表；也可以用语音「每天七点半打开热水器」创建。</div>
      </div>''',
)

replace_exact(
    "schedules page: form handlers",
    "async function deleteSchedule(id){",
    r'''// 设备域 → 可执行的操作。普通用户只看到中文，不接触 domain/service。
var SCHEDULE_ACTIONS={
  light:[['turn_on','打开'],['turn_off','关闭'],['toggle','切换']],
  switch:[['turn_on','打开'],['turn_off','关闭'],['toggle','切换']],
  fan:[['turn_on','打开'],['turn_off','关闭'],['toggle','切换']],
  input_boolean:[['turn_on','打开'],['turn_off','关闭'],['toggle','切换']],
  automation:[['turn_on','启用'],['turn_off','停用'],['toggle','切换']],
  climate:[['turn_on','打开'],['turn_off','关闭']],
  cover:[['open_cover','打开'],['close_cover','关闭'],['stop_cover','停止']],
  media_player:[['media_play','播放'],['media_pause','暂停'],['turn_off','关闭']],
  scene:[['turn_on','激活']],
  script:[['turn_on','运行']],
  vacuum:[['start','开始清扫'],['return_to_base','回充'],['stop','停止']]
};
var SCHEDULE_WEEKDAYS=[['1','一'],['2','二'],['3','三'],['4','四'],['5','五'],['6','六'],['7','日']];
var scheduleDaysDraft=[];
var haServiceCache={};
function scheduleActionsFor(domain){
  var base=SCHEDULE_ACTIONS[domain]||[['turn_on','打开'],['turn_off','关闭']];
  var known=haServiceCache[domain];
  if(Array.isArray(known)&&known.length){
    var only=base.filter(function(p){return known.indexOf(p[0])>=0;});
    if(only.length)return only;
  }
  return base;
}
function loadHaServices(){
  return fetch('/api/ha/services').then(function(r){return r.json()}).then(function(d){
    if(d&&d.ok&&d.services)haServiceCache=d.services;
    return haServiceCache;
  }).catch(function(){return haServiceCache});
}
function scheduleDeviceList(){
  var list=(typeof haDevices!=='undefined'&&Array.isArray(haDevices))?haDevices:[];
  return list.filter(function(d){
    var domain=String((d&&d.domain)||String((d&&d.entity_id)||'').split('.')[0]);
    return Object.prototype.hasOwnProperty.call(SCHEDULE_ACTIONS,domain);
  });
}
function scheduleDeviceFilter(){
  var box=document.getElementById('sch-search');var q=box?String(box.value||'').trim().toLowerCase():'';
  scheduleRenderDevices(q);
}
function scheduleRenderDevices(query){
  var sel=document.getElementById('sch-device');if(!sel)return;
  var current=sel.value;
  var rows=scheduleDeviceList();
  if(query){rows=rows.filter(function(d){return (String(d.name||'')+' '+String(d.entity_id||'')).toLowerCase().indexOf(query)>=0;});}
  var html='<option value="">请选择设备</option>';
  rows.forEach(function(d){
    var id=String(d.entity_id||'');
    var name=String(d.name||id);
    var state=d.state?'（'+(d.state==='on'?'开':'关')+'）':'';
    html+='<option value="'+escAttr(id)+'">'+escHtml(name+state)+'</option>';
  });
  sel.innerHTML=html;
  if(current&&rows.some(function(d){return d.entity_id===current;}))sel.value=current;
  var hint=document.getElementById('sch-device-hint');
  if(hint)hint.textContent=rows.length?('共 '+rows.length+' 台设备可选。') : '没有匹配的设备：请先在「设备管理」页同步 Home Assistant。';
}
function scheduleSelectedDevice(){
  var sel=document.getElementById('sch-device');var id=sel?String(sel.value||''):'';
  if(!id)return null;
  var list=scheduleDeviceList();
  for(var i=0;i<list.length;i++){if(list[i].entity_id===id)return list[i];}
  return {entity_id:id,name:id,domain:String(id).split('.')[0]};
}
function scheduleDeviceChanged(){scheduleRenderActions();schedulePreview();}
function scheduleRenderActions(){
  var wrap=document.getElementById('sch-action-wrap');if(!wrap)return;
  var dev=scheduleSelectedDevice();
  var actions=scheduleActionsFor(dev?String(dev.domain||''):'');
  var picked=wrap.getAttribute('data-service')||'';
  if(!actions.some(function(a){return a[0]===picked;}))picked=actions[0][0];
  wrap.setAttribute('data-service',picked);
  wrap.innerHTML=actions.map(function(a){
    var checked=a[0]===picked?' checked':'';
    return '<label class="toggle-label"><input type="radio" name="sch-action" value="'+escAttr(a[0])+'"'+checked+' onchange="scheduleActionChanged(this.value)"> '+escHtml(a[1])+'</label>';
  }).join('');
}
function scheduleActionChanged(service){
  var wrap=document.getElementById('sch-action-wrap');
  if(wrap)wrap.setAttribute('data-service',String(service||''));
  schedulePreview();
}
function scheduleCurrentService(){
  var wrap=document.getElementById('sch-action-wrap');
  return wrap?String(wrap.getAttribute('data-service')||''):'';
}
function scheduleDaysChanged(){
  var sel=document.getElementById('sch-days');var wrap=document.getElementById('sch-days-custom-wrap');
  if(!wrap||!sel)return;
  wrap.style.display=sel.value==='custom'?'block':'none';
  if(sel.value==='custom')scheduleRenderWeekdays();
  schedulePreview();
}
function scheduleRenderWeekdays(){
  var box=document.getElementById('sch-weekdays');if(!box)return;
  box.innerHTML=SCHEDULE_WEEKDAYS.map(function(d){
    var checked=scheduleDaysDraft.indexOf(d[0])>=0?' checked':'';
    return '<label class="toggle-label"><input type="checkbox" class="sch-wd" value="'+d[0]+'"'+checked+' onchange="scheduleWeekdayChanged(this.value,this.checked)"> 周'+d[1]+'</label>';
  }).join('');
  scheduleSyncWeekdayValue();
}
function scheduleWeekdayChanged(day,on){
  var value=String(day||'');
  var i=scheduleDaysDraft.indexOf(value);
  if(on&&i<0)scheduleDaysDraft.push(value);
  if(!on&&i>=0)scheduleDaysDraft.splice(i,1);
  scheduleDaysDraft.sort();
  scheduleSyncWeekdayValue();
  schedulePreview();
}
function scheduleSyncWeekdayValue(){
  var custom=document.getElementById('sch-days-custom');
  if(custom)custom.value=scheduleDaysDraft.join(',');
}
function scheduleDaysText(days){
  if(days==='daily')return '每天';
  if(days==='weekdays')return '工作日';
  if(days==='weekends')return '周末';
  var map={'1':'一','2':'二','3':'三','4':'四','5':'五','6':'六','7':'日'};
  var parts=String(days||'').split(',').map(function(x){return String(x).trim();}).filter(Boolean);
  if(!parts.length)return '自定义星期';
  return '每周'+parts.map(function(x){return map[x]||x;}).join('、');
}
function schedulePreview(){
  var box=document.getElementById('sch-preview');if(!box)return;
  var dev=scheduleSelectedDevice();
  var timeEl=document.getElementById('sch-time');
  var time=timeEl?String(timeEl.value||''):'';
  var days=scheduleDaysValue();
  var service=scheduleCurrentService();
  var label='';
  var actions=scheduleActionsFor(dev?String(dev.domain||''):'');
  actions.forEach(function(a){if(a[0]===service)label=a[1];});
  if(!dev){box.textContent='先选择设备';return;}
  box.textContent=scheduleDaysText(days)+' '+(time||'--:--')+'，'+(label||service)+'「'+(dev.name||dev.entity_id)+'」';
}
function scheduleFormToggle(){loadHaServices().then(scheduleRenderActions);
  var f=document.getElementById('schedule-form');if(!f)return;
  var show=f.style.display==='none';
  f.style.display=show?'block':'none';
  if(!show)return;
  scheduleDaysDraft=[];
  if(typeof loadRuleDevices==='function'){Promise.resolve(loadRuleDevices()).then(function(){scheduleRenderDevices('');scheduleRenderActions();schedulePreview();});}
  else{scheduleRenderDevices('');scheduleRenderActions();schedulePreview();}
  var t=document.getElementById('sch-time');if(t)t.focus();
}
function scheduleDaysValue(){
  var sel=document.getElementById('sch-days');
  var custom=document.getElementById('sch-days-custom');
  var v=(sel&&sel.value)||'daily';
  if(v!=='custom')return v;
  var manual=((custom&&custom.value)||'').trim();
  return manual||scheduleDaysDraft.join(',');
}
function scheduleAdvancedOpen(){
  var box=document.getElementById('sch-advanced');
  return !!(box&&box.open);
}
function scheduleCheck(){
  var a=document.getElementById('schedules-alert');var st=document.getElementById('sch-status');
  var problem=scheduleValidate();
  if(st)st.textContent=problem?'✗ '+problem:'✓ 配置可用';
  if(a)showAlert(a,problem?'err':'ok',problem?('✗ '+problem):('✓ '+schedulePreviewText()));
  return !problem;
}
function schedulePreviewText(){
  var box=document.getElementById('sch-preview');
  return box?String(box.textContent||''):'';
}
function scheduleValidate(){
  var timeEl=document.getElementById('sch-time');
  var time=timeEl?String(timeEl.value||'').trim():'';
  if(!/^\d{1,2}:\d{2}(:\d{2})?$/.test(time))return '时间格式无效，请用 HH:MM（24 小时制）';
  var days=scheduleDaysValue();
  if(days==='custom'||!days)return '自定义重复至少要选一天';
  if(scheduleAdvancedOpen()){
    var domain=String((document.getElementById('sch-domain')||{}).value||'').trim();
    if(!domain)return '高级模式需要填写 domain';
    return '';
  }
  var dev=scheduleSelectedDevice();
  if(!dev)return '请先选择设备';
  if(!scheduleCurrentService())return '请选择执行操作';
  return '';
}
async function createSchedule(){
  const a=document.getElementById('schedules-alert');const st=document.getElementById('sch-status');const btn=document.getElementById('sch-create');
  const val=function(id){const el=document.getElementById(id);return el?String(el.value||'').trim():'';};
  let time=val('sch-time');let domain=val('sch-domain');let service=val('sch-service');let entity=val('sch-entity');
  if(!scheduleAdvancedOpen()){
    const dev=scheduleSelectedDevice();
    if(!dev){showAlert(a,'err','✗ 请先选择设备');return;}
    domain=String(dev.domain||String(dev.entity_id||'').split('.')[0]);
    service=scheduleCurrentService();
    entity=String(dev.entity_id||'');
  }
  const daysSel=document.getElementById('sch-days');const daysRaw=scheduleDaysValue();
  const dataRaw=val('sch-data');let extra={};
  if(dataRaw){try{extra=JSON.parse(dataRaw)}catch(e){showAlert(a,'err','✗ 服务参数不是合法 JSON');return;}}
  if(daysSel&&daysSel.value==='custom'&&!daysRaw){showAlert(a,'err','✗ 自定义重复必须填写星期（如 1,3,5 或 mon,wed）');return;}
  if(!/^\d{1,2}:\d{2}(:\d{2})?$/.test(time)){showAlert(a,'err','✗ 时间格式无效，请用 HH:MM（24 小时制）');return;}
  if(!entity&&['light','switch','climate','fan','cover','media_player','scene','script','input_boolean'].indexOf(domain)>=0){showAlert(a,'err','✗ 这个服务必须填写实体 entity_id');return;}
  if(btn)btn.disabled=true;if(st)st.textContent='⟳ 正在创建...';
  try{
    const r=await fetch('/api/automations',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({time:time,days:daysRaw,domain:domain,service:service,entity_id:entity,data:extra})});
    const d=await r.json().catch(function(){return {};});
    if(!r.ok){
      if(r.status===504){if(st)st.textContent='';showAlert(a,'warn','⚠ '+(d&&d.detail||'创建结果待确认，请先去 HA 核对，不要直接重复提交'));return;}
      throw new Error((d&&d.detail)||('HTTP '+r.status));
    }
    if(st)st.textContent='';
    showAlert(a,d.reloaded===false?'warn':'ok',(d.reloaded===false?'⚠ ':'✓ ')+(d.msg||'已创建定时任务'));
    scheduleFormToggle();loadSchedules();
  }catch(e){if(st)st.textContent='✗ '+e.message;showAlert(a,'err','✗ 创建失败: '+e.message);}
  finally{if(btn)btn.disabled=false;}
}
async function deleteSchedule(id){''',
)

# 日志页原来固定只要 80 行（约几分钟），而「登录态续期是否每天成功」要回看数天。
# 后端 /api/logs 已改为读磁盘日志尾部（最多 1000 行），这里把页面请求量提到 500。
replace_exact(
    "log page reads more than 80 lines",
    "fetch('/api/logs?n=80')",
    "fetch('/api/logs?n=500')",
)

replace_exact(
    "listening scope card",
    '''<div id="mi-device-list" class="card" style="display:none"><div class="card-head"><div class="card-title"><span>&#9670;</span>发现设备 <span id="mi-device-count" style="text-transform:none;font-weight:400;font-size:10px"></span></div></div><div id="mi-devices"></div></div>''',
    '''<div id="mi-device-list" class="card" style="display:none"><div class="card-head"><div class="card-title"><span>&#9670;</span>发现设备 <span id="mi-device-count" style="text-transform:none;font-weight:400;font-size:10px"></span></div></div><div id="mi-devices"></div></div>
      <div id="mi-scope-card" class="card" style="display:none">
        <div class="card-head"><div class="card-title"><span>&#127911;</span>监听范围 <span id="mi-scope-count" style="text-transform:none;font-weight:400;font-size:10px"></span></div></div>
        <div style="font-size:12px;color:var(--txt-muted);line-height:1.7;margin-bottom:8px">只影响“听哪几台音箱说话”，与“设备管理”页里的 Home Assistant 实体无关。修改后需要重启桥接器才会生效。</div>
        <div class="field"><label class="toggle-label"><input type="radio" name="mi-scope-mode" id="mi-scope-mode-all" value="all" onchange="setMiScopeMode('all')"> 监听全部小爱音箱（含以后新加的）</label></div>
        <div class="field"><label class="toggle-label"><input type="radio" name="mi-scope-mode" id="mi-scope-mode-selected" value="selected" onchange="setMiScopeMode('selected')"> 仅监听所选音箱</label></div>
        <div id="mi-scope-list" style="margin:8px 0"></div>
        <div id="mi-scope-status" style="font-size:12px;color:var(--txt-muted);line-height:1.7"></div>
      </div>''',
)

replace_exact(
    "listening scope state and rendering",
    '''if(devices.length>0){const f=devices[0];selectDevice(f.hardware||f.model||'',f.deviceID||f.did||'')}''',
    '''renderMiScope(devices)''',
)

replace_exact(
    "listening scope helpers",
    '''function selectDevice(hw,did){if(hw)document.getElementById('mi-hardware').value=hw;if(did)document.getElementById('mi-did').value=did}''',
    '''function selectDevice(hw,did){if(hw)document.getElementById('mi-hardware').value=hw;if(did)document.getElementById('mi-did').value=did}

function miScopeNormIds(raw){var out=[];if(raw==null)return out;if(typeof raw==='string'){raw=raw.split(',');}if(!Array.isArray(raw))return out;raw.forEach(function(v){if(typeof v!=='string')return;var s=v.trim();if(!s)return;if(out.indexOf(s)<0)out.push(s);});return out}
function miScopeSaved(){var mi=(cfg&&cfg.xiaomi)||{};var ids=miScopeNormIds(mi.device_ids);if(mi.device_scope==='all')return{mode:'all',ids:[]};if(mi.device_scope==='selected')return{mode:'selected',ids:ids};var single=String(mi.device_id||mi.device_did||'').trim();if(ids.length)return{mode:'selected',ids:ids};if(single)return{mode:'selected',ids:[single],legacy:true};return{mode:'all',ids:[]}}
function miScopeInit(){if(globalThis.miScopeDraft)return globalThis.miScopeDraft;var s=miScopeSaved();globalThis.miScopeDraft={mode:s.mode,ids:s.ids.slice(),legacy:!!s.legacy};return globalThis.miScopeDraft}
function miScopeReset(){globalThis.miScopeDraft=null}
function miScopeRows(){var d=miScopeInit();var seen={};var rows=[];(globalThis.miScopeDevices||[]).forEach(function(dev){if(!dev||typeof dev!=='object')return;var id=String(dev.deviceID||dev.did||'').trim();if(!id||seen[id])return;seen[id]=1;rows.push({id:id,name:String(dev.name||'未知音箱'),hw:String(dev.hardware||dev.model||''),missing:false})});d.ids.forEach(function(id){if(seen[id])return;seen[id]=1;rows.push({id:id,name:'（当前未发现）',hw:'',missing:true})});return rows}
function setMiScopeMode(mode){if(mode!=='all'&&mode!=='selected')return;var d=miScopeInit();d.mode=mode;d.legacy=false;renderMiScope();miScopeStatus()}
function toggleMiScopeIndex(index,on){var rows=miScopeRows();var row=rows[index];if(!row)return;var d=miScopeInit();if(d.mode!=='selected')return;var i=d.ids.indexOf(row.id);if(on&&i<0)d.ids.push(row.id);if(!on&&i>=0)d.ids.splice(i,1);d.legacy=false;renderMiScope();miScopeStatus()}
function miScopeSelectAll(){var d=miScopeInit();d.mode='selected';d.legacy=false;d.ids=miScopeRows().filter(function(r){return!r.missing}).map(function(r){return r.id});renderMiScope();miScopeStatus()}
function miScopeClearAll(){var d=miScopeInit();d.mode='selected';d.legacy=false;d.ids=[];renderMiScope();miScopeStatus()}
function miScopeCollect(){var d=miScopeInit();if(d.mode==='all')return{ok:true,mode:'all',ids:[]};if(!d.ids.length)return{ok:false,mode:'selected',ids:[],msg:'“仅监听所选”至少要选一台音箱'};return{ok:true,mode:'selected',ids:d.ids.slice()}}
function miScopeDirty(){var s=miScopeSaved();var d=miScopeInit();if(s.mode!==d.mode)return true;if(s.mode==='all')return false;if(s.ids.length!==d.ids.length)return true;for(var i=0;i<s.ids.length;i++){if(d.ids.indexOf(s.ids[i])<0)return true}return false}
function miScopeStatus(){var box=document.getElementById('mi-scope-status');var cnt=document.getElementById('mi-scope-count');var d=miScopeInit();var rows=miScopeRows();var missing=rows.filter(function(r){return r.missing}).length;if(cnt)cnt.textContent='('+(d.mode==='all'?'全部':d.ids.length+' 台')+')';if(!box)return;var text='';if(d.mode==='all'){text='当前监听：全部小爱音箱。新发现的音箱会自动纳入。'}else if(!d.ids.length){text='当前监听：未选择任何音箱，保存会被拒绝。'}else{text='当前监听：已选 '+d.ids.length+' 台。'}if(missing>0)text+=' 其中 '+missing+' 台当前未发现，已保留配置。';if(d.legacy)text+=' 这是旧版单台配置，保存后会转为“仅监听所选”。';text+=miScopeDirty()?' 有未保存的修改。':' 已保存。';box.textContent=text}
function renderMiScope(devices){if(devices!==undefined)globalThis.miScopeDevices=devices||[];var card=document.getElementById('mi-scope-card');var list=document.getElementById('mi-scope-list');if(!card||!list)return;card.style.display='';var d=miScopeInit();var all=document.getElementById('mi-scope-mode-all');var sel=document.getElementById('mi-scope-mode-selected');if(all)all.checked=d.mode==='all';if(sel)sel.checked=d.mode==='selected';var rows=miScopeRows();list.innerHTML=rows.map(function(r,i){var checked=(d.mode==='selected'&&d.ids.indexOf(r.id)>=0)?' checked':'';var disabled=(d.mode==='all')?' disabled':'';return'<label class="rule-item" style="display:flex;align-items:center;gap:8px;cursor:pointer"><input type="checkbox" class="mi-scope-box" data-index="'+i+'"'+checked+disabled+' onchange="toggleMiScopeIndex('+i+',this.checked)"><span style="color:var(--txt-primary);font-size:14px">'+escHtml(r.name)+'</span><span style="color:var(--txt-muted);font-size:11px">'+escHtml(r.hw)+'</span><span style="color:var(--txt-dim);font-size:10px;font-family:JetBrains Mono,monospace">'+escHtml(r.id.length>12?r.id.substring(0,12)+'...':r.id)+'</span></label>'}).join('');miScopeStatus()}''',
)

replace_exact(
    "save the listening scope with the xiaomi section",
    '''async function saveXiaomi(){const o=cfg.xiaomi||{};cfg.xiaomi={username:gv('mi-username'),password:gv('mi-password'),hardware:gv('mi-hardware'),device_did:gv('mi-did'),tts_reply:document.getElementById('mi-tts').checked,cookie_text:o.cookie_text||''};await saveCfg('xiaomi')}''',
    '''async function saveXiaomi(){var o=cfg.xiaomi||{};var scope=miScopeCollect();if(!scope.ok){showAlert(document.getElementById('mi-scope-status')||document.getElementById('xiaomi-alert'),'err','✗ '+scope.msg);return false}var next={username:gv('mi-username'),password:gv('mi-password'),hardware:gv('mi-hardware'),tts_reply:document.getElementById('mi-tts').checked,cookie_text:o.cookie_text||''};next.device_scope=scope.mode;next.device_ids=(scope.mode==='all')?[]:scope.ids;next.device_id='';next.device_did='';cfg.xiaomi=next;var ok=await saveCfg('xiaomi');if(ok){miScopeReset();renderMiScope();}else{cfg.xiaomi=o;miScopeStatus();var st=document.getElementById('mi-scope-status');if(st)st.textContent='✗ 保存失败，修改仍保留。'+(st.textContent||'');}return ok}''',
)

replace_exact(
    "initialise the listening scope draft from the saved config",
    '''  safeSet('mi-did',mi.device_did||'');safeCheck('mi-tts',mi.tts_reply!==false);''',
    '''  safeSet('mi-did',mi.device_did||'');safeCheck('mi-tts',mi.tts_reply!==false);miScopeReset();''',
)

replace_exact(
    "schedules list shows next run, source, HA-not-loaded and can pause",
    'async function loadSchedules(){const a=document.getElementById(\'schedules-alert\');try{const r=await fetch(\'/api/automations\');const d=await r.json();if(!d.ok){showAlert(a,\'err\',d.msg);return}document.getElementById(\'schedule-count\').textContent=d.count+\'个\';const list=document.getElementById(\'schedule-list\');if(!d.automations.length){list.innerHTML=\'<div class="empty-state">暂无<br><span style="font-size:11px">对小爱说「每天七点半打开热水器」，或点上方「＋ 新建定时任务」</span></div>\';return}list.innerHTML=d.automations.map(s=>{const name=escHtml(s.alias||s.entity_id);const eid=escHtml(s.entity_id);const id=(s.config_id||\'\').trim()||s.entity_id.replace(\'automation.\',\'\');const st=s.state===\'on\';const last=s.last_triggered?new Date(s.last_triggered).toLocaleString(\'zh-CN\'):\'从未\';return\'<div class="rule-item"><div class="device-item"><div class="device-info"><div class="device-name">\'+name+\'</div><div class="device-meta">\'+eid+\'</div><div style="font-size:10px;color:var(--txt-dim);margin-top:2px">上次: \'+last+\'</div></div><div style="display:flex;align-items:center;gap:10px"><span class="schedule-state \'+(st?\'on\':\'off\')+\'">\'+(st?\'启用\':\'禁用\')+\'</span><button class="rule-del" onclick="deleteSchedule(\\\'\'+escAttr(id)+\'\\\')">✕</button></div></div></div>\'}).join(\'\')}catch(e){showAlert(a,\'err\',\'加载失败: \'+e.message)}}\n',
    'async function loadSchedules(){\n  const a=document.getElementById(\'schedules-alert\');\n  try{\n    const r=await fetch(\'/api/automations\');\n    const d=await r.json();\n    if(!d.ok){showAlert(a,\'err\',d.msg);return}\n    document.getElementById(\'schedule-count\').textContent=d.count+\'个\';\n    const list=document.getElementById(\'schedule-list\');\n    if(!d.automations.length){\n      list.innerHTML=\'<div class="empty-state">暂无<br><span style="font-size:11px">对小爱说「每天七点半打开热水器」，或点上方「＋ 新建定时任务」</span></div>\';\n      return;\n    }\n    list.innerHTML=d.automations.map(function(s){\n      const name=escHtml(s.alias||s.entity_id||\'（未命名任务）\');\n      const eid=escHtml(s.entity_id||\'\');\n      const id=(s.config_id||\'\').trim()||String(s.entity_id||\'\').replace(\'automation.\',\'\');\n      const on=s.state===\'on\';\n      const last=s.last_triggered?new Date(s.last_triggered).toLocaleString(\'zh-CN\'):\'从未\';\n      const next=s.next_run?escHtml(s.next_run):\'—\';\n      const missing=s.status===\'not_loaded\';\n      const src=s.source===\'voice\'?\'语音\':(s.source===\'form\'?\'面板\':\'\');\n      const badge=missing?\'<span class="schedule-state off" style="margin-left:6px">HA 未加载</span>\':\'\';\n      const toggle=missing?\'\':\'<button class="rule-del" title="暂停/启用" data-id="\'+escAttr(id)+\'" data-on="\'+(on?\'false\':\'true\')+\'" onclick="toggleSchedule(this.dataset.id,this.dataset.on)">\'+(on?\'⏸\':\'▶\')+\'</button>\';\n      return \'<div class="rule-item"><div class="device-item"><div class="device-info">\'\n        +\'<div class="device-name">\'+name+badge+\'</div>\'\n        +\'<div class="device-meta">\'+(eid||\'（配置已写入，HA 尚未加载）\')+\'</div>\'\n        +\'<div style="font-size:10px;color:var(--txt-dim);margin-top:2px">下次: \'+next+\' · 上次: \'+last+(src?\' · 来源: \'+src:\'\')+\'</div>\'\n        +\'</div><div style="display:flex;align-items:center;gap:10px">\'\n        +\'<span class="schedule-state \'+(on?\'on\':\'off\')+\'">\'+(on?\'启用\':\'禁用\')+\'</span>\'\n        +toggle\n        +\'<button class="rule-del" onclick="deleteSchedule(\\\'\'+escAttr(id)+\'\\\')">✕</button>\'\n        +\'</div></div></div>\';\n    }).join(\'\');\n  }catch(e){showAlert(a,\'err\',\'加载失败: \'+e.message)}\n}\nasync function toggleSchedule(id,enabled){\n  const a=document.getElementById(\'schedules-alert\');\n  try{\n    const r=await fetch(\'/api/automations/\'+encodeURIComponent(id)+\'/toggle\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({enabled:enabled===true||enabled===\'true\'})});\n    const d=await r.json().catch(function(){return {}});\n    if(d&&d.ok){showAlert(a,\'ok\',d.msg||\'已切换\');loadSchedules()}\n    else{showAlert(a,\'err\',(d&&d.detail)||\'切换失败\')}\n  }catch(e){showAlert(a,\'err\',\'切换失败: \'+e.message)}\n}\n',
)

# ════════════════ 定时任务 v2：房间筛选 + 一次性任务 + 编辑 ════════════════
# 这里追加的是「覆盖层」：同名函数在 script 末尾重新声明，后声明者生效。
# 这样不用改动上游内联 HTML 里的大段函数，避免把字符串字面量切断。
replace_exact(
    "schedules form: edit banner and title id",
    '''      <div id="schedule-form" style="display:none;margin:12px 0;padding:14px;background:var(--page-bg);border:1px solid var(--border-default);border-radius:var(--r-md)">
        <div style="font-size:14px;font-weight:600;color:var(--txt-primary);margin-bottom:10px">新建定时任务</div>''',
    '''      <div id="schedule-form" style="display:none;margin:12px 0;padding:14px;background:var(--page-bg);border:1px solid var(--border-default);border-radius:var(--r-md)">
        <div id="sch-edit-banner" style="display:none;margin-bottom:10px;padding:8px 10px;background:var(--card-bg);border:1px solid var(--border-default);border-radius:var(--r-md);font-size:12px;color:var(--txt-primary)">正在修改：<span id="sch-edit-name"></span> <button class="btn" style="margin-left:8px;padding:2px 8px" onclick="cancelScheduleEdit()">取消修改</button></div>
        <div id="sch-form-title" style="font-size:14px;font-weight:600;color:var(--txt-primary);margin-bottom:10px">新建定时任务</div>''',
)

replace_exact(
    "schedules form: room filter above the device picker",
    '''        <div class="field"><label>选择设备</label>
          <input id="sch-search" placeholder="搜索设备名称…" oninput="scheduleDeviceFilter()" style="margin-bottom:6px">
          <select id="sch-device" onchange="scheduleDeviceChanged()"><option value="">正在读取设备…</option></select>''',
    '''        <div class="field"><label>选择设备</label>
          <div class="form-grid" style="margin-bottom:6px">
            <select id="sch-area" onchange="scheduleDeviceFilter()"><option value="">全部房间</option></select>
            <input id="sch-search" placeholder="搜索设备名称…" oninput="scheduleDeviceFilter()">
          </div>
          <select id="sch-device" onchange="scheduleDeviceChanged()"><option value="">正在读取设备…</option></select>''',
)

replace_exact(
    "schedules form: one-shot repeat option and date field",
    '''          <div class="field"><label>重复方式</label><select id="sch-days" onchange="scheduleDaysChanged()"><option value="daily">每天</option><option value="weekdays">工作日</option><option value="weekends">周末</option><option value="custom">自定义星期</option></select></div>
        </div>''',
    '''          <div class="field"><label>重复方式</label><select id="sch-days" onchange="scheduleDaysChanged()"><option value="daily">每天</option><option value="weekdays">工作日</option><option value="weekends">周末</option><option value="custom">自定义星期</option><option value="once">仅一次（指定日期）</option></select></div>
          <div class="field" id="sch-date-wrap" style="display:none"><label>执行日期</label><input type="date" id="sch-date" onchange="schedulePreview()"></div>
        </div>''',
)

replace_exact(
    "schedules page v2 overrides (areas, one-shot, edit)",
    "\ninit();",
    r'''
// ════════════════ 定时任务 v2：房间筛选 / 一次性任务 / 编辑 ════════════════
// 同名函数在脚本末尾重新声明：后声明者生效（不改上游内联 HTML 的大段函数）。
var scheduleItems = [];
var scheduleEditId = '';
var haAreas = null;

function scheduleAreaValue(){
  var sel=document.getElementById('sch-area');
  return sel?String(sel.value||''):'';
}

function loadHaAreas(){
  if(haAreas!==null)return Promise.resolve(haAreas);
  return fetch('/api/ha/areas').then(function(r){return r.json()}).then(function(d){
    var sel=document.getElementById('sch-area');
    if(d&&d.ok&&d.supported&&Array.isArray(d.areas)&&d.areas.length){
      haAreas=d.areas;
      if(sel){
        sel.innerHTML='<option value="">全部房间</option>'+d.areas.map(function(a){
          return '<option value="'+escAttr(a.id)+'">'+escHtml(a.name)+'（'+((a.entities||[]).length)+'）</option>';
        }).join('');
        sel.style.display='';
      }
    }else{
      haAreas=[];
      if(sel)sel.style.display='none';
    }
    return haAreas;
  }).catch(function(){
    haAreas=[];
    var sel=document.getElementById('sch-area');
    if(sel)sel.style.display='none';
    return haAreas;
  });
}

function scheduleDeviceList(){
  var list=(typeof haDevices!=='undefined'&&Array.isArray(haDevices))?haDevices:[];
  var rows=list.filter(function(d){
    var domain=String((d&&d.domain)||String((d&&d.entity_id)||'').split('.')[0]);
    return Object.prototype.hasOwnProperty.call(SCHEDULE_ACTIONS,domain);
  });
  var area=scheduleAreaValue();
  if(area&&haAreas&&haAreas.length){
    var allowed={};
    haAreas.forEach(function(a){
      if(a.id===area)(a.entities||[]).forEach(function(entity){allowed[String(entity)]=1;});
    });
    rows=rows.filter(function(d){return allowed[String((d&&d.entity_id)||'')];});
  }
  return rows;
}

function scheduleDaysText(days){
  if(days==='once')return '仅一次';
  if(days==='daily')return '每天';
  if(days==='weekdays')return '工作日';
  if(days==='weekends')return '周末';
  var map={'1':'一','2':'二','3':'三','4':'四','5':'五','6':'六','7':'日'};
  var parts=String(days||'').split(',').map(function(x){return String(x).trim();}).filter(Boolean);
  if(!parts.length)return '自定义星期';
  return '每周'+parts.map(function(x){return map[x]||x;}).join('、');
}

function scheduleDaysChanged(){
  var sel=document.getElementById('sch-days');
  var wrap=document.getElementById('sch-days-custom-wrap');
  var dateWrap=document.getElementById('sch-date-wrap');
  if(!sel)return;
  var once=sel.value==='once';
  if(wrap)wrap.style.display=sel.value==='custom'?'block':'none';
  if(dateWrap)dateWrap.style.display=once?'block':'none';
  if(sel.value==='custom')scheduleRenderWeekdays();
  if(once){
    var d=document.getElementById('sch-date');
    if(d&&!d.value){
      var t=new Date(Date.now()+86400000);
      d.value=t.getFullYear()+'-'+('0'+(t.getMonth()+1)).slice(-2)+'-'+('0'+t.getDate()).slice(-2);
    }
  }
  schedulePreview();
}

function schedulePreview(){
  var box=document.getElementById('sch-preview');if(!box)return;
  var dev=scheduleSelectedDevice();
  var timeEl=document.getElementById('sch-time');
  var time=timeEl?String(timeEl.value||''):'';
  var days=scheduleDaysValue();
  var service=scheduleCurrentService();
  var label='';
  var actions=scheduleActionsFor(dev?String(dev.domain||''):'');
  actions.forEach(function(a){if(a[0]===service)label=a[1];});
  if(!dev){box.textContent='先选择设备';return;}
  var when=time;
  if(days==='once'){
    var dateEl=document.getElementById('sch-date');
    when=((dateEl&&String(dateEl.value||''))||'未选日期')+' '+(time||'--:--');
  }
  box.textContent=scheduleDaysText(days)+' '+(when||'--:--')+'，'+(label||service)+'「'+(dev.name||dev.entity_id)+'」';
}

function scheduleValidate(){
  var timeEl=document.getElementById('sch-time');
  var time=timeEl?String(timeEl.value||'').trim():'';
  if(!/^\d{1,2}:\d{2}(:\d{2})?$/.test(time))return '时间格式无效，请用 HH:MM（24 小时制）';
  var days=scheduleDaysValue();
  if(days==='once'){
    var dateEl=document.getElementById('sch-date');
    var date=dateEl?String(dateEl.value||'').trim():'';
    if(!/^\d{4}-\d{2}-\d{2}$/.test(date))return '一次性任务需要选择日期';
    var when=new Date(date+'T'+(time.length===5?time+':00':time));
    if(isNaN(when.getTime()))return '日期格式无效';
    // HA's timezone may differ from this browser; the server validates the moment.
  }else if(days==='custom'||!days){
    return '自定义重复至少要选一天';
  }
  if(scheduleAdvancedOpen()){
    var domain=String((document.getElementById('sch-domain')||{}).value||'').trim();
    if(!domain)return '高级模式需要填写 domain';
    return '';
  }
  var dev=scheduleSelectedDevice();
  if(!dev)return '请先选择设备';
  if(!scheduleCurrentService())return '请选择执行操作';
  return '';
}

function cancelScheduleEdit(){
  scheduleEditId='';
  var banner=document.getElementById('sch-edit-banner');
  if(banner)banner.style.display='none';
  var title=document.getElementById('sch-form-title');
  if(title)title.textContent='新建定时任务';
  var btn=document.getElementById('sch-create');
  if(btn)btn.innerHTML='&#10003; 创建';
}

function scheduleFormToggle(){
  var form=document.getElementById('schedule-form');
  if(!form)return Promise.resolve(false);
  var show=form.style.display==='none';
  form.style.display=show?'block':'none';
  if(!show){cancelScheduleEdit();return Promise.resolve(false);}
  cancelScheduleEdit();
  var timeEl=document.getElementById('sch-time');
  if(timeEl)timeEl.focus();
  return Promise.resolve(loadHaServices()).then(function(){
    return loadHaAreas();
  }).then(function(){
    if(typeof loadRuleDevices==='function')return Promise.resolve(loadRuleDevices());
  }).then(function(){
    scheduleRenderDevices('');scheduleRenderActions();schedulePreview();
    return true;
  });
}

function editSchedule(id){
  var item=null;
  (scheduleItems||[]).forEach(function(s){if(String(s.config_id||'')===String(id))item=s;});
  if(!item)return;
  var opening=document.getElementById('schedule-form');
  opening=opening&&opening.style.display==='none'?scheduleFormToggle():Promise.resolve(true);
  opening.then(function(){
    scheduleEditId=String(id);
    var multi=Array.isArray(item.entity);
    var entity=multi?item.entity.join(','):String(item.entity||'').trim();
    if(!entity)entity=String(item.entity_id||'').replace(/^automation\./,'');
    var sel=document.getElementById('sch-device');
    if(sel&&entity){
      var found=false;
      for(var i=0;i<sel.options.length;i++){if(sel.options[i].value===entity)found=true;}
      if(!found)sel.insertAdjacentHTML('beforeend','<option value="'+escAttr(entity)+'">'+escHtml(entity)+'</option>');
      sel.value=entity;
    }
    scheduleDeviceChanged();
    if(item.service)scheduleActionChanged(String(item.service));
    var timeEl=document.getElementById('sch-time');
    if(timeEl&&item.time)timeEl.value=String(item.time).slice(0,5);
    var daysEl=document.getElementById('sch-days');
    var wd=Array.isArray(item.weekdays)?item.weekdays:[];
    var dateEl=document.getElementById('sch-date');
    if(item.once){
      if(daysEl)daysEl.value='once';
      if(dateEl&&item.once_at)dateEl.value=String(item.once_at).slice(0,10);
    }else if(!wd.length){
      if(daysEl)daysEl.value='daily';
    }else if(wd.join(',')==='mon,tue,wed,thu,fri'){
      if(daysEl)daysEl.value='weekdays';
    }else if(wd.join(',')==='sat,sun'){
      if(daysEl)daysEl.value='weekends';
    }else{
      if(daysEl)daysEl.value='custom';
      var dayMap={mon:'1',tue:'2',wed:'3',thu:'4',fri:'5',sat:'6',sun:'7'};
      scheduleDaysDraft=wd.map(function(day){return dayMap[day]||day;});
      scheduleRenderWeekdays();
    }
    scheduleDaysChanged();
    var dm=document.getElementById('sch-domain');
    var svcEl=document.getElementById('sch-service');
    var entEl=document.getElementById('sch-entity');
    var dataEl=document.getElementById('sch-data');
    if(dm&&item.domain)dm.value=item.domain;
    if(svcEl&&item.service)svcEl.value=item.service;
    if(entEl)entEl.value=entity;
    if(dataEl)dataEl.value=(item.data&&Object.keys(item.data).length)?JSON.stringify(item.data):'';
    var simple=(scheduleActionsFor(item.domain)||[]).map(function(a){return a[0];});
    var adv=document.getElementById('sch-advanced');
    if(adv&&(multi||(item.service&&simple.indexOf(String(item.service))<0)))adv.open=true;
    var banner=document.getElementById('sch-edit-banner');
    var nameEl=document.getElementById('sch-edit-name');
    var title=document.getElementById('sch-form-title');
    var btn=document.getElementById('sch-create');
    if(nameEl)nameEl.textContent=String(item.alias||id);
    if(banner)banner.style.display='';
    if(title)title.textContent='修改定时任务';
    if(btn)btn.innerHTML='&#10003; 保存修改';
    schedulePreview();
    var formEl=document.getElementById('schedule-form');
    if(formEl&&formEl.scrollIntoView)formEl.scrollIntoView({block:'start'});
  });
}

async function createSchedule(){
  const a=document.getElementById('schedules-alert');
  const st=document.getElementById('sch-status');
  const btn=document.getElementById('sch-create');
  const val=function(id){const el=document.getElementById(id);return el?String(el.value||'').trim():'';};
  let time=val('sch-time');let domain=val('sch-domain');let service=val('sch-service');let entity=val('sch-entity');
  if(!scheduleAdvancedOpen()){
    const dev=scheduleSelectedDevice();
    if(!dev){showAlert(a,'err','✗ 请先选择设备');return;}
    domain=String(dev.domain||String(dev.entity_id||'').split('.')[0]);
    service=scheduleCurrentService();
    entity=String(dev.entity_id||'');
  }
  const daysRaw=scheduleDaysValue();
  const once=daysRaw==='once';
  const dateRaw=val('sch-date');
  const dataRaw=val('sch-data');let extra={};
  if(dataRaw){try{extra=JSON.parse(dataRaw)}catch(e){showAlert(a,'err','✗ 服务参数不是合法 JSON');return;}}
  if(!once&&daysRaw==='custom'&&!val('sch-days-custom')){showAlert(a,'err','✗ 自定义重复必须填写星期（如 1,3,5 或 mon,wed）');return;}
  if(!/^\d{1,2}:\d{2}(:\d{2})?$/.test(time)){showAlert(a,'err','✗ 时间格式无效，请用 HH:MM（24 小时制）');return;}
  if(once&&!/^\d{4}-\d{2}-\d{2}$/.test(dateRaw)){showAlert(a,'err','✗ 一次性任务需要选择日期');return;}
  if(!entity&&['light','switch','climate','fan','cover','media_player','scene','script','input_boolean'].indexOf(domain)>=0){showAlert(a,'err','✗ 这个服务必须填写实体 entity_id');return;}
  const editing=scheduleEditId;
  const payload={time:time,days:once?'daily':daysRaw,domain:domain,service:service,entity_id:entity,data:extra,once:once};
  if(once)payload.date=dateRaw;
  if(btn)btn.disabled=true;if(st)st.textContent=editing?'⟳ 正在保存...':'⟳ 正在创建...';
  try{
    const url=editing?('/api/automations/'+encodeURIComponent(editing)):'/api/automations';
    const r=await fetch(url,{method:editing?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const d=await r.json().catch(function(){return {};});
    if(!r.ok){
      if(r.status===504){if(st)st.textContent='';showAlert(a,'warn','⚠ '+(d&&d.detail||'结果待确认，请先去 HA 核对，不要直接重复提交'));return;}
      throw new Error((d&&d.detail)||('HTTP '+r.status));
    }
    if(st)st.textContent='';
    showAlert(a,d.reloaded===false?'warn':'ok',(d.reloaded===false?'⚠ ':'✓ ')+(d.msg||(editing?'已保存修改':'已创建定时任务')));
    cancelScheduleEdit();
    scheduleFormToggle();
    loadSchedules();
  }catch(e){if(st)st.textContent='✗ '+e.message;showAlert(a,'err','✗ '+(editing?'保存失败':'创建失败')+': '+e.message);}
  finally{if(btn)btn.disabled=false;}
}

async function cleanupSchedules(){
  const a=document.getElementById('schedules-alert');
  try{
    const r=await fetch('/api/automations/cleanup',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    const d=await r.json();
    if(!r.ok||!d.ok)throw new Error(d.detail||d.msg||'清理失败');
    showAlert(a,'ok','已清理 '+d.removed+' 个已完成任务');
    await loadSchedules();
  }catch(e){showAlert(a,'err','清理失败: '+e.message);}
}

async function loadSchedules(){
  const a=document.getElementById('schedules-alert');
  try{
    const r=await fetch('/api/automations');
    const d=await r.json();
    if(!d.ok){showAlert(a,'err',d.msg);return}
    scheduleItems=Array.isArray(d.automations)?d.automations:[];
    document.getElementById('schedule-count').textContent=d.count+'个';
    const list=document.getElementById('schedule-list');
    if(!scheduleItems.length){
      list.innerHTML='<div class="empty-state">暂无<br><span style="font-size:11px">对小爱说「每天七点半打开热水器」，或点上方「＋ 新建定时任务」</span></div>';
      return;
    }
    list.innerHTML=scheduleItems.map(function(s){
      const name=escHtml(s.alias||s.entity_id||'（未命名任务）');
      const eid=escHtml(s.entity_id||'');
      const id=(s.config_id||'').trim()||String(s.entity_id||'').replace('automation.','');
      const on=s.state==='on';
      const last=s.last_triggered?new Date(s.last_triggered).toLocaleString('zh-CN'):'从未';
      const next=s.next_run?escHtml(s.next_run):'—';
      const missing=s.status==='not_loaded';
      const done=s.status==='done';
      const expired=s.status==='expired';
      const src=s.source==='voice'?'语音':(s.source==='form'?'面板':'');
      const onceTag=s.once?' · 一次性':'';
      const label=done?'已完成':(expired?'已过期':(on?'启用':'禁用'));
      const badge=missing?'<span class="schedule-state off" style="margin-left:6px">HA 未加载</span>':(expired?'<span class="schedule-state off" style="margin-left:6px">已过期未执行</span>':'');
      const pause=(missing||done||expired)?'':'<button class="rule-del" title="暂停/启用" data-id="'+escAttr(id)+'" data-on="'+(on?'false':'true')+'" onclick="toggleSchedule(this.dataset.id,this.dataset.on)">'+(on?'⏸':'▶')+'</button>';
      const edit=done?'':'<button class="rule-del" title="修改任务" data-id="'+escAttr(id)+'" onclick="editSchedule(this.dataset.id)">✎</button>';
      return '<div class="rule-item"><div class="device-item"><div class="device-info">'
        +'<div class="device-name">'+name+badge+'</div>'
        +'<div class="device-meta">'+(eid||'（配置已写入，HA 尚未加载）')+'</div>'
        +'<div style="font-size:10px;color:var(--txt-dim);margin-top:2px">下次: '+next+' · 上次: '+last+(src?' · 来源: '+src:'')+onceTag+'</div>'
        +'</div><div style="display:flex;align-items:center;gap:10px">'
        +'<span class="schedule-state '+(on?'on':'off')+'">'+label+'</span>'+pause+edit
        +'<button class="rule-del" title="删除" onclick="deleteSchedule(\''+escAttr(id)+'\')">✕</button>'
        +'</div></div></div>';
    }).join('');
  }catch(e){showAlert(a,'err','加载失败: '+e.message)}
}

init();''',
)

path.write_text(html, encoding="utf-8")
print("Patched web/index.html with HA entity dropdowns")
print("Patched web/index.html with ingress-safe fetch, clear-login button and rule edit guards")
print("Patched web/index.html with a 500-line log page")
