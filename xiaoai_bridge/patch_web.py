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
    '<button class="btn btn-primary" onclick="saveRules()">&darr;保存</button>'
    '<span id="rules-dirty" style="display:none;color:var(--orange);font-size:12px;align-self:center">&#9679; 有未保存修改</span>',
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

// ═══════════ Rule edit state (never clobber unsaved edits) ═══════════
function setRulesDirty(on){
  const flag=document.getElementById('rules-dirty');
  if(flag)flag.style.display=on?'':'none';
}
function markRulesDirty(){setRulesDirty(true)}
function bindRulesDirty(){
  const list=document.getElementById('rule-list');
  if(!list||list.dataset.dirtyBound)return;
  list.dataset.dirtyBound='1';
  list.addEventListener('input',markRulesDirty);
  list.addEventListener('change',markRulesDirty);
}
function syncRuleEntityOptions(){
  const rows=document.querySelectorAll('#rule-list .rule-item');
  rows.forEach(row=>{
    const sel=row.querySelector('.r-e');
    if(!sel)return;
    const domain=(row.querySelector('.r-d')||{}).value||'light';
    const current=Array.from(sel.selectedOptions||[]).map(o=>o.value).filter(Boolean);
    const next=entityOptions(domain,current.length===1?current[0]:current);
    if(sel.dataset.sig!==next){sel.dataset.sig=next;sel.innerHTML=next}
  });
  return rows.length;
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
    "save rules without clobbering failed edits",
    "async function saveRules(){collectRulesFromUI();await saveCfg('rules')}",
    "async function saveRules(){collectRulesFromUI();const ok=await saveCfg('rules');if(ok){setRulesDirty(false);renderRules()}return ok}",
)

replace_exact(
    "open newly added rule",
    "renderRules();const last=document.querySelector('.rule-list')?.lastElementChild;if(last)last.scrollIntoView({behavior:'smooth'})}",
    "renderRules();markRulesDirty();const last=document.querySelector('#rule-list .rule-item:last-child');if(last){toggleRule(last.querySelector('.rule-expand'));last.scrollIntoView({behavior:'smooth'})}}",
)

replace_exact(
    "entity input to select",
    '''h+='</select></div><div class="field"><label>实体ID</label><input type="text" class="r-e" value="'+escHtml(a.entity_id||'')+'" placeholder="climate.xxx"></div><div class="extra-fields">';''',
    '''h+='</select></div><div class="field"><label>实体ID（可多选）</label><select class="r-e" multiple size="6">'+entityOptions(d,a.entity_id||'')+'</select><small>按住 Ctrl 选择多个设备</small></div><div class="extra-fields">';''',
)

replace_exact(
    "collect only intent rule rows and preserve multiple entities",
    "const items=document.querySelectorAll('.rule-item');cfg.intent_rules=Array.from(items).map(el=>{const a={domain:el.querySelector('.r-d').value,service:el.querySelector('.r-s').value,entity_id:el.querySelector('.r-e').value.trim(),reply:el.querySelector('.r-r').value.trim()||'好的'};",
    "const items=document.querySelectorAll('#rule-list .rule-item');cfg.intent_rules=Array.from(items).map(el=>{const ids=Array.from(el.querySelector('.r-e').selectedOptions||[]).map(o=>o.value).filter(Boolean);const a={domain:el.querySelector('.r-d').value,service:el.querySelector('.r-s').value,entity_id:ids.length===1?ids[0]:(ids.length?ids:''),reply:el.querySelector('.r-r').value.trim()||'好的'};",
)

replace_exact(
    "domain switch refresh",
    "const info=DINFO[d];const sel=item.querySelector('.r-s');",
    "const info=DINFO[d];const ent=item.querySelector('.r-e');if(ent)ent.innerHTML=entityOptions(d,'');const sel=item.querySelector('.r-s');",
)

replace_exact(
    "save result must report failure and be awaitable",
    "showAlert(a,d.ok?'ok':'err',(d.ok?'✓ ':'✗ ')+(d.ok?'保存成功':'保存失败'))}catch(e){showAlert(a,'err','✗ '+e.message)}setTimeout(()=>a.classList.remove('show'),3000)}",
    "const ok=!!(d&&d.ok);showAlert(a,ok?'ok':'err',(ok?'✓ ':'✗ ')+(ok?'保存成功':(d.msg||'保存失败')));setTimeout(()=>a.classList.remove('show'),3000);return ok}catch(e){showAlert(a,'err','✗ '+e.message);setTimeout(()=>a.classList.remove('show'),3000);return false}}",
)

replace_exact(
    "delete rule marks unsaved changes",
    "function deleteRule(i){collectRulesFromUI();cfg.intent_rules.splice(i,1);renderRules()}",
    "function deleteRule(i){collectRulesFromUI();cfg.intent_rules.splice(i,1);renderRules();markRulesDirty()}",
)

replace_exact(
    "import rules marks unsaved changes",
    "cfg.intent_rules=r;renderRules();showAlert",
    "cfg.intent_rules=r;renderRules();markRulesDirty();showAlert",
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
