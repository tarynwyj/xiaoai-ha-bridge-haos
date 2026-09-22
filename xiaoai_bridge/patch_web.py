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
  const list=haDevices
    .filter(d=>d.domain===domain)
    .sort((a,b)=>String(a.name||a.entity_id).localeCompare(String(b.name||b.entity_id),'zh-CN'));
  let out='';
  const exists=list.some(d=>d.entity_id===current);
  if(current&&!exists){
    out+='<option value="'+escHtml(current)+'" selected>'+escHtml(current)+'（当前）</option>';
  }
  if(!current){
    out+='<option value="">请选择实体</option>';
  }
  if(!list.length){
    if(!current)out+='<option value="" disabled>未读取到此类型实体</option>';
    return out;
  }
  out+=list.map(d=>{
    const label=(deviceAliases[d.entity_id]||d.name||d.entity_id)+' · '+d.entity_id;
    return '<option value="'+escHtml(d.entity_id)+'" '+(d.entity_id===current?'selected':'')+'>'+escHtml(label)+'</option>';
  }).join('');
  return out;
}

// ═══════════ DOMAIN INFO ═══════════""",
)

replace_exact(
    "entity input to select",
    '''h+='</select></div><div class="field"><label>实体ID</label><input type="text" class="r-e" value="'+escHtml(a.entity_id||'')+'" placeholder="climate.xxx"></div><div class="extra-fields">';''',
    '''h+='</select></div><div class="field"><label>实体ID</label><select class="r-e">'+entityOptions(d,a.entity_id||'')+'</select></div><div class="extra-fields">';''',
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

path.write_text(html, encoding="utf-8")
print("Patched web/index.html with HA entity dropdowns")
