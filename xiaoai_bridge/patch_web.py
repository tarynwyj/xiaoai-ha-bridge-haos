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

// ═══════════ DOMAIN INFO ═══════════""",
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
