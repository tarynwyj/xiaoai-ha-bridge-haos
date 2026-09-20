from pathlib import Path

p = Path("/app/web/index.html")
s = p.read_text(encoding="utf-8")

replacements = [
    (
        "let deviceAliases = {};",
        "let deviceAliases = {};\nlet haDevices = [];"
    ),
    (
        "await loadConfig();\n  renderRules();",
        "await loadConfig();\n  await loadRuleDevices();\n  renderRules();"
    ),
    (
        "if(name==='logs')fetchLogs();if(name==='devices')loadDevices();if(name==='schedules')loadSchedules();",
        "if(name==='logs')fetchLogs();if(name==='devices')loadDevices();if(name==='schedules')loadSchedules();if(name==='rules')loadRuleDevices().then(renderRules);"
    ),
    (
        "// ═══════════ DOMAIN INFO ═══════════",
        """// ═══════════ Rule entity selector ═══════════
async function loadRuleDevices(){
  try{
    const r=await fetch('/api/devices');
    const d=await r.json();
    haDevices=d.ok?(d.devices||[]):[];
  }catch(e){haDevices=[]}
}
function entityOptions(domain,current){
  const list=haDevices.filter(d=>d.domain===domain);
  let out='';
  const exists=list.some(d=>d.entity_id===current);
  if(current&&!exists)out+='<option value="'+escHtml(current)+'" selected>'+escHtml(current)+'（当前）</option>';
  if(!current)out+='<option value="">请选择实体</option>';
  if(!list.length){
    if(!current)out+='<option value="" disabled>未读取到此类型实体</option>';
    return out;
  }
  out+=list.map(d=>'<option value="'+escHtml(d.entity_id)+'" '+(d.entity_id===current?'selected':'')+'>'+escHtml((d.alias||d.name||d.entity_id)+' · '+d.entity_id)+'</option>').join('');
  return out;
}

// ═══════════ DOMAIN INFO ═══════════"""
    ),
    (
        "h+='</select></div><div class="field"><label>实体ID</label><input type="text" class="r-e" value="'+escHtml(a.entity_id||'')+'" placeholder="climate.xxx"></div><div class="extra-fields">';",
        "h+='</select></div><div class="field"><label>实体ID</label><select class="r-e">'+entityOptions(d,a.entity_id||'')+'</select></div><div class="extra-fields">';"
    ),
    (
        "const info=DINFO[d];const sel=item.querySelector('.r-s');",
        "const info=DINFO[d];const ent=item.querySelector('.r-e');if(ent)ent.innerHTML=entityOptions(d,'');const sel=item.querySelector('.r-s');"
    ),
]

for old, new in replacements:
    if old not in s:
        raise SystemExit(f"Patch target not found: {old[:100]}")
    s = s.replace(old, new, 1)

p.write_text(s, encoding="utf-8")
print("Patched web/index.html with HA entity dropdowns")
