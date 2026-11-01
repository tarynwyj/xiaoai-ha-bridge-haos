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
    if(d.ok){await loadConfig();}
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
  return picker.querySelectorAll('.r-e').filter(box=>box.checked).map(box=>box.value).filter(Boolean);
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
  const total=picker.querySelectorAll('.r-e').filter(box=>box.checked).length;
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
  return picker?picker.querySelectorAll('.entity-option').filter(row=>row.style.display!=='none'):[];
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
  if(extra){try{Object.assign(action,JSON.parse(extra))}catch(e){}}
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
function syncRuleEntityOptions(){
  const rows=document.querySelectorAll('#rule-list .rule-item');
  rows.forEach(row=>{
    const picker=row.querySelector('.entity-picker');
    if(!picker)return;
    const current=entityIdsOf(row);
    const domain=(row.querySelector('.r-d')||{}).value||'light';
    const next=entityPickerHtml(domain,current);
    if(picker.dataset.sig!==next){
      picker.dataset.sig=next;
      picker.outerHTML=next;
    }
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

async function saveOneRule(i){if(!ruleStateReady)ruleStateInit(cfg.intent_rules||[]);const alerts=document.getElementById('rules-alert');if(ruleBulkPending){showAlert(alerts,'err','✗ 已导入新的规则列表，请先点「保存全部」');return false}if(ruleDeleted.length>0){showAlert(alerts,'err','✗ 有规则等待删除，请先点「保存全部」');return false}const rows=document.querySelectorAll('#rule-list .rule-item');const row=rows[i];const state=ruleState[i];if(!row||!state)return false;state.draft=ruleDraftFromRow(row);const merged=ruleBaseline.map(entry=>cloneRule(entry.rule));const pos=ruleBaseline.findIndex(entry=>entry.uid===state.uid);if(pos>=0)merged[pos]=cloneRule(state.draft);else merged.push(cloneRule(state.draft));const ok=await persistRules(merged);if(ok){if(pos>=0)ruleBaseline[pos].rule=cloneRule(state.draft);else ruleBaseline.push({uid:state.uid,rule:cloneRule(state.draft)});state.base=true;state.dirty=false;state.open=true;cfg.intent_rules=merged;renderRuleHead(row,state,i);setRulesDirty(anyRuleDirty());showAlert(alerts,'ok','✓ 已保存：'+escHtml(ruleName(state.draft)||state.draft.pattern||('规则 #'+(i+1))))}else{state.dirty=true;setRulesDirty(true)}return ok}

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
    r"""function buildRule(rule,index,state){const a=(rule||{}).action||{};const d=a.domain||'light';const info=DINFO[d]||DINFO.light;const skip=new Set(['domain','service','entity_id','reply','temperature','brightness_pct','delay_minutes','hvac_mode']);const ids=ruleEntityIds(rule);const div=document.createElement('div');div.className='rule-item'+(state&&state.open?'':' collapsed');div.dataset.idx=index;div.dataset.search=ruleSearchText(rule);let h='<div class="rule-head"><span class="rule-num">#'+(index+1)+'</span><div class="rule-summary-wrap"><div class="rule-title">'+escHtml(ruleTitle(rule,index))+'</div><div class="rule-summary">'+ruleSummaryHtml(rule)+'</div></div><span class="rule-chip"'+(state&&state.dirty?'':' style="display:none"')+'>未保存</span><span class="rule-actions"><button class="rule-expand" onclick="toggleRule(this)">'+(state&&state.open?'收起':'编辑')+'</button><button class="rule-save" onclick="saveOneRule('+index+')">保存</button><button class="rule-del" onclick="deleteRule('+index+')">删除</button></span></div><div class="rule-grid"><div class="field full"><label>规则名称（可选）</label><input type="text" class="r-n" value="'+escHtml(ruleName(rule))+'" placeholder="客厅灯组开灯"></div><div class="field full"><label>触发语句（支持匹配表达式）</label><input type="text" class="r-p" value="'+escHtml((rule||{}).pattern||'')+'" placeholder="打开客厅灯"></div><div class="field" style="grid-column:1/-1"><label>设备类型</label><div class="domain-select">';Object.entries(DINFO).forEach(([k,v])=>{h+='<button class="domain-btn '+(k===d?'active':'')+'" onclick="switchD(this,\''+k+'\')"><span class="domain-icon">'+v.icon+'</span>'+v.name+'</button>'});h+='</div><input type="hidden" class="r-d" value="'+escHtml(d)+'"></div><div class="field"><label>操作</label><select class="r-s" onchange="updX(this)">';Object.entries(info.s).forEach(([k,v])=>{h+='<option value="'+k+'" '+(a.service===k?'selected':'')+'>'+v+'</option>'});h+='</select></div><div class="field full"><label>实体 ID（可多选）</label><div class="entity-picker" data-domain="'+escHtml(d)+'">'+entityPickerHtml(d,ids)+'</div></div><div class="extra-fields">';info.x.forEach(x=>{h+='<div class="field"><label>'+x.l+'</label><input type="text" class="r-xk" data-key="'+x.k+'" value="'+escHtml(a[x.k]||'')+'" placeholder="'+(x.p||'')+'"></div>'});h+='</div><div class="field full"><label>回复</label><input type="text" class="r-r" value="'+escHtml(a.reply||'好的')+'" placeholder="好的"></div><div class="field full"><label>其他(JSON)</label><input type="text" class="r-j" value="'+escHtml(extraJ(a,skip))+'"></div></div>';div.innerHTML=h;return div}""",
)

replace_exact(
    'domain switch keeps drafts',
    r'''function switchD(btn,d){btn.parentElement.querySelectorAll('.domain-btn').forEach(b=>b.classList.remove('active'));btn.classList.add('active');const item=btn.closest('.rule-item');item.querySelector('.r-d').value=d;const info=DINFO[d];const sel=item.querySelector('.r-s');sel.innerHTML=Object.entries(info.s).map(([k,v])=>'<option value="'+k+'">'+v+'</option>').join('');item.querySelector('.extra-fields').innerHTML=info.x.length?info.x.map(x=>'<div class="field"><label>'+x.l+'</label><input type="text" class="r-xk" data-key="'+x.k+'" value="" placeholder="'+(x.p||'')+'"></div>').join(''):''}''',
    r"""function switchD(btn,d){btn.parentElement.querySelectorAll('.domain-btn').forEach(b=>b.classList.remove('active'));btn.classList.add('active');const item=btn.closest('.rule-item');if(!item)return;item.querySelector('.r-d').value=d;const info=DINFO[d];const allowed=new Set((haDevices||[]).filter(device=>device.domain===d).map(device=>device.entity_id));const current=entityIdsOf(item).filter(id=>allowed.has(id));const picker=item.querySelector('.entity-picker');if(picker){picker.dataset.domain=d;picker.dataset.sig='';picker.innerHTML=entityPickerHtml(d,current)}const sel=item.querySelector('.r-s');if(sel){sel.innerHTML=Object.entries(info.s).map(([k,v])=>'<option value="'+k+'">'+v+'</option>').join('');sel.value=Object.keys(info.s)[0]}const extra=item.querySelector('.extra-fields');if(extra){extra.innerHTML=info.x.length?info.x.map(x=>'<div class="field"><label>'+x.l+'</label><input type="text" class="r-xk" data-key="'+x.k+'" value="" placeholder="'+(x.p||'')+'"></div>').join(''):''}const index=Number(item.dataset.idx);if(!Number.isNaN(index)&&ruleState[index]){ruleState[index].draft=ruleDraftFromRow(item);ruleState[index].dirty=true;renderRuleHead(item,ruleState[index],index)}setRulesDirty(anyRuleDirty())}""",
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
    r"""async function saveRules(){if(!ruleStateReady)ruleStateInit(cfg.intent_rules||[]);collectRuleDrafts();const merged=[];ruleBaseline.forEach(entry=>{if(ruleDeleted.indexOf(entry.uid)>=0)return;const state=ruleState[ruleIndexByUid(entry.uid)];merged.push(cloneRule(state?state.draft:entry.rule))});ruleState.forEach(state=>{if(!state.base)merged.push(cloneRule(state.draft))});const ok=await persistRules(merged);if(ok){ruleStateInit(merged);cfg.intent_rules=merged;renderRules();setRulesDirty(false);showAlert(document.getElementById('rules-alert'),'ok','✓ 已保存全部规则')}return ok}""",
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
          ③ 复制 userId 与 passToken（有 serviceToken 也可以一起复制）<br>
          ④ 粘贴到下面 &rarr; 点「验证 Cookie」
        </div>
        <div class="field"><label>Cookie 文本</label><textarea id="mi-cookie-text" rows="3" placeholder="userId=xxx; passToken=xxx; serviceToken=xxx; ..."></textarea></div>
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
async function verifyCookieLogin(){
  const alertBox=otpEl('xiaomi-alert');
  const box=otpEl('mi-cookie-text');
  const text=String((box&&box.value)||'').trim();
  if(!text){showAlert(alertBox,'err','✗ 先粘贴小米 Cookie（至少包含 userId 与 passToken）');return;}
  otpText('mi-cookie-status','⟳ 正在校验 Cookie...');
  const btn=otpEl('mi-cookie-submit');if(btn)btn.disabled=true;
  try{
    const r=await fetch('/api/test/xiaomi/cookie-login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cookie_text:text})});
    const d=await r.json();
    if(d&&d.ok){
      otpReset();
      if(box)box.value='';
      otpText('mi-cookie-status','');
      showAlert(alertBox,'ok','✓ '+(d.msg||'Cookie 登录成功'));
      if(d.devices&&d.devices.length>0)renderDeviceList(d.devices);
      return;
    }
    otpText('mi-cookie-status','✗ '+((d&&d.action==='fix_cookie')?'请检查 Cookie 文本':'Cookie 可能已过期，可重试'));
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

path.write_text(html, encoding="utf-8")
print("Patched web/index.html with HA entity dropdowns")
print("Patched web/index.html with ingress-safe fetch, clear-login button and rule edit guards")
