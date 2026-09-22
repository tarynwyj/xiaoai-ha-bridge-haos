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
    "initial entity load",
    "await loadConfig();\n  renderRules();",
    "await loadConfig();\n  renderRules();\n  loadRuleDevices().then(renderRules);",
)

replace_exact(
    "rules page refresh",
    "if(name==='logs')fetchLogs();if(name==='devices')loadDevices();if(name==='schedules')loadSchedules();",
    "if(name==='logs')fetchLogs();if(name==='devices')loadDevices();if(name==='schedules')loadSchedules();if(name==='rules')loadRuleDevices().then(renderRules);",
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
    "entity helper insertion",
    "// ═══════════ DOMAIN INFO ═══════════",
    """// ═══════════ Rule entity selector ═══════════
async function loadRuleDevices(){
  try{
    const r=await fetch('/api/devices');
    const d=await r.json();
    haDevices=d.ok&&Array.isArray(d.devices)?d.devices:[];
  }catch(e){
    haDevices=[];
  }
}
function entityOptions(domain,current){
  const selected=new Set(Array.isArray(current)?current:(current?[current]:[]));
  const list=haDevices
    .filter(d=>d.domain===domain)
    .sort((a,b)=>String(a.name||a.entity_id).localeCompare(String(b.name||b.entity_id),'zh-CN'));
  let out='';
  for(const id of selected){
    if(!list.some(d=>d.entity_id===id))out+='<option value="'+escHtml(id)+'" selected>'+escHtml(id)+'（当前）</option>';
  }
  if(!list.length){
    if(!selected.size)out+='<option value="" disabled>未读取到此类型实体</option>';
    return out;
  }
  out+=list.map(d=>{
    const label=(deviceAliases[d.entity_id]||d.name||d.entity_id)+' · '+d.entity_id;
    return '<option value="'+escHtml(d.entity_id)+'" '+(selected.has(d.entity_id)?'selected':'')+'>'+escHtml(label)+'</option>';
  }).join('');
  return out;
}
function ruleEntityIds(rule){
  const value=(rule.action||{}).entity_id;
  return Array.isArray(value)?value:(value?[value]:[]);
}
function ruleTargetNames(rule){
  return ruleEntityIds(rule).map(id=>{
    const device=haDevices.find(item=>item.entity_id===id);
    return deviceAliases[id]||(device&&device.name)||id;
  });
}
function ruleSearchText(rule){
  return [rule.pattern||'',...ruleEntityIds(rule),...ruleTargetNames(rule)].join(' ').toLowerCase();
}
function ruleSummaryHtml(rule){
  const action=rule.action||{};
  const info=DINFO[action.domain]||{};
  const operation=(info.s||{})[action.service]||action.service||'未设置操作';
  const targets=ruleTargetNames(rule);
  return '<div class="rule-summary-pattern">'+escHtml(rule.pattern||'未填写语句')+'</div>'+
    '<div class="rule-summary-meta">'+escHtml(info.name||action.domain||'设备')+' · '+escHtml(operation)+' · '+targets.length+' 个设备</div>'+
    '<div class="rule-summary-targets">'+escHtml(targets.join('、')||'尚未选择设备')+'</div>';
}
function toggleRule(button){
  const row=button.closest('.rule-item');
  row.classList.toggle('collapsed');
  button.textContent=row.classList.contains('collapsed')?'编辑':'收起';
}
function filterRules(){
  const query=(document.getElementById('rule-filter')?.value||'').trim().toLowerCase();
  const rows=document.querySelectorAll('#rule-list .rule-item');
  let shown=0;
  rows.forEach(row=>{
    const visible=!query||row.dataset.search.includes(query);
    row.style.display=visible?'':'none';
    if(visible)shown++;
  });
  const count=document.getElementById('rule-count');
  if(count)count.textContent=shown+'/'+rows.length+' 条';
}

// ═══════════ DOMAIN INFO ═══════════""",
)

replace_exact(
    "compact rule summary",
    '''div.className='rule-item';div.dataset.idx=i;let h='<div class="rule-head"><span class="rule-num">#'+(i+1)+'</span><button class="rule-del"''',
    '''div.className='rule-item collapsed';div.dataset.idx=i;div.dataset.search=ruleSearchText(rule);let h='<div class="rule-head"><span class="rule-num">#'+(i+1)+'</span><div class="rule-summary">'+ruleSummaryHtml(rule)+'</div><button class="rule-expand" onclick="toggleRule(this)">编辑</button><button class="rule-del"''',
)

replace_exact(
    "refresh rule count after render",
    "rules.forEach((r,i)=>{list.appendChild(buildRule(r,i))})}",
    "rules.forEach((r,i)=>{list.appendChild(buildRule(r,i))});filterRules()}",
)

replace_exact(
    "refresh summary after saving",
    "async function saveRules(){collectRulesFromUI();await saveCfg('rules')}",
    "async function saveRules(){collectRulesFromUI();await saveCfg('rules');renderRules()}",
)

replace_exact(
    "open newly added rule",
    "renderRules();const last=document.querySelector('.rule-list')?.lastElementChild;if(last)last.scrollIntoView({behavior:'smooth'})}",
    "document.getElementById('rule-filter').value='';renderRules();const last=document.querySelector('#rule-list .rule-item:last-child');if(last){toggleRule(last.querySelector('.rule-expand'));last.scrollIntoView({behavior:'smooth'})}}",
)

replace_exact(
    "entity input to select",
    '''h+='</select></div><div class="field"><label>实体ID</label><input type="text" class="r-e" value="'+escHtml(a.entity_id||'')+'" placeholder="climate.xxx"></div><div class="extra-fields">';''',
    '''h+='</select></div><div class="field"><label>实体ID（可多选）</label><select class="r-e" multiple size="6">'+entityOptions(d,a.entity_id||'')+'</select><small>按住 Ctrl 选择多个设备</small></div><div class="extra-fields">';''',
)

replace_exact(
    "collect only intent rule rows and preserve multiple entities",
    "const items=document.querySelectorAll('.rule-item');cfg.intent_rules=Array.from(items).map(el=>{const a={domain:el.querySelector('.r-d').value,service:el.querySelector('.r-s').value,entity_id:el.querySelector('.r-e').value.trim(),reply:el.querySelector('.r-r').value.trim()||'好的'};",
    "const items=document.querySelectorAll('#rule-list .rule-item');cfg.intent_rules=Array.from(items).map(el=>{const ids=Array.from(el.querySelector('.r-e').selectedOptions).map(o=>o.value).filter(Boolean);const a={domain:el.querySelector('.r-d').value,service:el.querySelector('.r-s').value,entity_id:ids.length===1?ids[0]:ids,reply:el.querySelector('.r-r').value.trim()||'好的'};",
)

replace_exact(
    "domain switch refresh",
    "const info=DINFO[d];const sel=item.querySelector('.r-s');",
    "const info=DINFO[d];const ent=item.querySelector('.r-e');if(ent)ent.innerHTML=entityOptions(d,'');const sel=item.querySelector('.r-s');",
)

replace_exact(
    "save error detail",
    "(d.ok?'保存成功':'保存失败')",
    "(d.ok?'保存成功':(d.msg||'保存失败'))",
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

path.write_text(html, encoding="utf-8")
print("Patched web/index.html with HA entity dropdowns")
