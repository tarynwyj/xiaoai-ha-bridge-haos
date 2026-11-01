#!/usr/bin/env python3
"""Regression tests for the xiaoai_bridge web UI patches (patch_web.py).

The interesting behaviour of the patched page lives in inline JavaScript, so
this test does three things:

1. rebuilds web/index.html from the *pinned pristine* upstream file through
   patch_web.py (so every patch anchor is proven to match exactly once),
2. extracts the real JS functions out of the built HTML and runs them in Node
   against a tiny hand written DOM stub (no npm install, no jsdom), and
3. AST-extracts ``class IntentParser`` out of the built bridge.py and proves the
   backend rule matcher ignores the new (UI only) ``name`` field.

Run it (Windows, python 3.11 on PATH):

    python xiaoai_bridge\\test_web_patch.py ^
        --pristine <workspace>\\tmp\\upstream-pinned\\web\\index.html

    # or test an already built file without rebuilding
    python xiaoai_bridge\\test_web_patch.py --html <path>\\web\\index.html

    # optional: point the behavioural matcher check at a specific bridge build
    python xiaoai_bridge\\test_web_patch.py --html <path>\\web\\index.html ^
        --bridge <path>\\bridge.py

``--bridge`` defaults to ``<repo>/../build-main/bridge.py`` when that file
exists; otherwise the matcher check prints a SKIP line and is not counted.

Exit codes: 0 = everything passed, 1 = test failure, 2 = usage / IO error.
The Node harness is written to a temporary file and executed as
``node <harness>.mjs <built html>``; it exits non-zero when an assertion fails.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PATCH_SCRIPT = Path(__file__).resolve().with_name("patch_web.py")

# <repo>/../build-main/bridge.py — the build the page is normally shipped with.
DEFAULT_BRIDGE = Path(__file__).resolve().parents[2] / "build-main" / "bridge.py"

# Names of the JS functions that must be present in the built HTML and that the
# Node harness exercises.  Missing names are a hard failure (the patch drifted).
EXTRACT_FUNCTIONS = [
    # 通用
    "escHtml",
    "extraJ",
    # 实体勾选框选择器
    "loadRuleDevices",
    "entityOptionItems",
    "entityRowHtml",
    "entityPickerHtml",
    "entityPickerOf",
    "entityIdsOf",
    "setEntitySelection",
    "updateEntityCount",
    "filterEntityOptions",
    "visibleEntityOptions",
    "selectAllEntities",
    "clearEntities",
    # 规则草稿 / 标题 / 摘要
    "ruleEntityIds",
    "ruleTargetNames",
    "ruleName",
    "ruleTitle",
    "ruleSearchText",
    "ruleSummaryHtml",
    "ruleDraftFromRow",
    "collectRuleDrafts",
    # 渲染 / 交互
    "renderRules",
    "buildRule",
    "renderRuleHead",
    "toggleRule",
    "filterRules",
    "syncRuleEntityOptions",
    # 脏标记
    "setRulesDirty",
    "markRulesDirty",
    "anyRuleDirty",
    "bindRulesDirty",
    "onRuleEdited",
    # 草稿状态机 / 保存
    "nextRuleUid",
    "cloneRule",
    "ruleStateInit",
    "ruleIndexByUid",
    "persistRules",
    "saveOneRule",
    "collectRulesFromUI",
    "saveCfg",
    "saveRules",
    "addRule",
    "deleteRule",
    "exportRules",
    "importRules",
    # 其他页面
    "switchD",
    "showPage",
]

EXTRACT_CONSTS = ["const DINFO"]


# ───────────────────────────── JS source scanning ─────────────────────────────

_REGEX_PREV_CHARS = set("(,=:[!&|?{};+-*%~^<>")
_REGEX_KEYWORDS = {
    "return", "typeof", "instanceof", "in", "of", "new", "delete", "void",
    "case", "do", "else", "yield", "await", "throw",
}


def _skip_string(src: str, i: int) -> int:
    quote = src[i]
    i += 1
    n = len(src)
    while i < n:
        ch = src[i]
        if ch == "\\":
            i += 2
            continue
        if ch == quote:
            return i + 1
        i += 1
    raise ValueError("unterminated string literal")


def _regex_allowed(src: str, i: int) -> bool:
    j = i - 1
    while j >= 0 and src[j] in " \t\r\n":
        j -= 1
    if j < 0:
        return True
    ch = src[j]
    if ch in _REGEX_PREV_CHARS:
        return True
    if ch.isalnum() or ch in "_$":
        k = j
        while k >= 0 and (src[k].isalnum() or src[k] in "_$"):
            k -= 1
        return src[k + 1:j + 1] in _REGEX_KEYWORDS
    return False


def _skip_regex(src: str, i: int) -> int:
    i += 1  # opening slash
    n = len(src)
    in_class = False
    while i < n:
        ch = src[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "[":
            in_class = True
        elif ch == "]":
            in_class = False
        elif ch == "/" and not in_class:
            return i + 1
        elif ch == "\n":
            return i
        i += 1
    raise ValueError("unterminated regular expression")


def _scan_balanced(src: str, start: int) -> int:
    """Return the index of the '}' that closes the '{' at *start*."""
    if src[start] != "{":
        raise ValueError("expected '{' at scan start")
    depth = 0
    i = start
    n = len(src)
    while i < n:
        ch = src[i]
        if ch in "\"'`":
            i = _skip_string(src, i)
            continue
        if ch == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if ch == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if ch == "/" and _regex_allowed(src, i):
            i = _skip_regex(src, i)
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError("unbalanced braces")


def _extract_function(html: str, name: str) -> str:
    pattern = r"(?<![\w$.])(?:async\s+)?function\s+" + re.escape(name) + r"\s*\("
    m = re.search(pattern, html)
    if not m:
        raise ValueError(f"function {name}() not found in the built HTML")
    brace = html.find("{", m.end())
    if brace < 0:
        raise ValueError(f"function {name}() has no body")
    end = _scan_balanced(html, brace)
    return html[m.start():end + 1]


def _extract_const(html: str, prefix: str) -> str:
    idx = html.find(prefix)
    if idx < 0:
        raise ValueError(f"{prefix}... not found in the built HTML")
    brace = html.find("{", idx)
    if brace < 0:
        raise ValueError(f"{prefix} has no object literal")
    end = _scan_balanced(html, brace)
    return html[idx:end + 1]


def extract_js_source(html: str) -> str:
    chunks = ["// ===== extracted from the built index.html ====="]
    for prefix in EXTRACT_CONSTS:
        chunks.append(_extract_const(html, prefix) + ";")
    for name in EXTRACT_FUNCTIONS:
        chunks.append(_extract_function(html, name))
    return "\n".join(chunks)


# ─────────────────────────── source level invariants ───────────────────────────

def source_invariants(html: str) -> list[tuple[str, bool, str]]:
    saved_rules = html.count("saveCfg('rules')")
    return [
        (
            "no NodeList array-method misuse in the page script",
            not re.search(r"querySelectorAll\([^)]*\)\s*\.\s*(?:filter|map|reduce|some|every|find|sort)\(", html),
            "a querySelectorAll(...) result is used as an array (NodeList has no .filter/.map in browsers)",
        ),
        (
            "destructive device-load re-render removed",
            "loadRuleDevices().then(renderRules)" not in html,
            "found loadRuleDevices().then(renderRules)",
        ),
        (
            "device load / rules tab only sync entity options",
            html.count("loadRuleDevices().then(syncRuleEntityOptions)") == 2,
            f"expected 2 call sites, found {html.count('loadRuleDevices().then(syncRuleEntityOptions)')}",
        ),
        (
            "init binds dirty listeners after renderRules",
            "renderRules();\n  bindRulesDirty();\n  loadRuleDevices().then(syncRuleEntityOptions);" in html,
            "init block does not bind dirty listeners",
        ),
        (
            "unsaved edit indicator markup present",
            'id="rules-dirty"' in html,
            "missing #rules-dirty indicator",
        ),
        (
            "rules collect is scoped to #rule-list",
            "document.querySelectorAll('#rule-list .rule-item')" in html
            and "document.querySelectorAll('.rule-item')" not in html,
            "collectRulesFromUI still queries every .rule-item in the document",
        ),
        (
            "checkbox picker markup is produced by entityPickerHtml",
            'class="r-e"' in html
            and 'type="checkbox"' in html
            and 'onclick="selectAllEntities(this)"' in html
            and 'onclick="clearEntities(this)"' in html
            and 'oninput="filterEntityOptions(this)"' in html,
            "entityPickerHtml no longer renders the checkbox rows / toolbar",
        ),
        (
            "legacy multi-select entity input is gone",
            "按住 Ctrl" not in html and 'multiple size="6"' not in html,
            "the old <select multiple size=6> picker or its Ctrl hint is still present",
        ),
        (
            "picker renders the 已选择 N 个设备 counter",
            "已选择 '+selected.size+' 个设备" in html,
            "entityPickerHtml does not render the selected counter",
        ),
        (
            "per-rule save button wired to saveOneRule",
            'onclick="saveOneRule(' in html and "function saveOneRule(" in html,
            "per-rule save button or saveOneRule() missing",
        ),
        (
            "save-all button is labelled 保存全部",
            "保存全部" in html and 'onclick="saveRules()"' in html,
            "缺少 保存全部 按钮",
        ),
        (
            "rule name field rendered",
            'class="r-n"' in html,
            "缺少 input.r-n 规则名称输入框",
        ),
        (
            "rule title priority implemented",
            "function ruleTitle(" in html
            and "const named=ruleName(rule)" in html
            and "'新规则 #'" in html,
            "ruleTitle() 没有实现 name > pattern > 新规则 #N 的优先级",
        ),
        (
            "rule summary shows trigger / domain / operation / device count",
            "rule-summary-pattern" in html and "' 个设备'" in html,
            "ruleSummaryHtml 缺少触发语句 / 类型 · 操作 · N 个设备 结构",
        ),
        (
            "rule search text includes the rule name",
            "ruleName(rule),rule.pattern" in html,
            "ruleSearchText 没有把规则名称纳入搜索",
        ),
        (
            "zero entity selection saves upstream-compatible empty string",
            "entity_id:ids.length===1?ids[0]:(ids.length?ids:'')" in html,
            "ruleDraftFromRow does not normalise the zero-selection case",
        ),
        (
            "optional name only written when filled in",
            "if(name)rule.name=name" in html,
            "ruleDraftFromRow always/never writes the name field",
        ),
        (
            "sync rebuilds the picker in place and keeps the selection",
            "const current=entityIdsOf(row)" in html and "picker.outerHTML=next" in html,
            "syncRuleEntityOptions no longer preserves the checkbox selection",
        ),
        (
            "structural rule changes mark the page dirty",
            "renderRules();markRulesDirty()" in html
            and "renderRules();markRulesDirty();showAlert" in html,
            "addRule/deleteRule/importRules do not flag unsaved changes",
        ),
        (
            "per-rule save refuses while a bulk change is pending",
            "if(ruleBulkPending){showAlert" in html
            and "if(ruleDeleted.length>0){showAlert" in html,
            "saveOneRule 没有在导入 / 删除待保存时拒绝单条保存",
        ),
        (
            "failed save returns false and never re-renders",
            "return ok}catch(e)" in html and "return false}}" in html,
            "saveCfg does not report the save result",
        ),
        (
            "saveCfg('rules') still posts the whole config",
            saved_rules >= 1,
            f"saveCfg('rules') occurrences: {saved_rules}",
        ),
        (
            "ingress-safe fetch wrapper installed",
            "input.charAt(0)==='/'" in html
            and "X-Requested-With" in html
            and "window.__bridgeAuthReloaded" in html,
            "relative API base / CSRF header wrapper missing",
        ),
        (
            "clear saved login credentials button added",
            'onclick="clearXiaomiLogin()"' in html
            and "async function clearXiaomiLogin()" in html
            and "'/api/xiaomi/clear-login'" in html,
            "clear-login button or handler missing",
        ),
        (
            "clear-login button sits before the save button",
            html.find('onclick="clearXiaomiLogin()"') < html.find('onclick="saveXiaomi()"'),
            "clear-login button is not before saveXiaomi()",
        ),
        (
            "xiaomi cookie_text round-trip untouched",
            "cookie_text:o.cookie_text||''" in html,
            "saveXiaomi() cookie_text handling changed",
        ),
    ]


# ─────────────────────────────── node harness ────────────────────────────────

# A deliberately tiny DOM subset: enough for buildRule / ruleDraftFromRow /
# filterRules / switchD / renderRules / saveOneRule to run for real.
# No jsdom, no npm.  勾选框（input.checked）、style 属性与 outerHTML 替换都在这里补齐。
DOM_STUB = r"""
// ===== minimal DOM stub (subset sufficient for the rules page code) =====
const VOID_TAGS = new Set(['input','br','img','hr','meta','link','source','area','base','col','embed','param','track','wbr']);

class ClassList {
  constructor(){ this.set = new Set(); }
  add(...names){ for (const name of names) this.set.add(name); }
  remove(...names){ for (const name of names) this.set.delete(name); }
  contains(name){ return this.set.has(name); }
  toggle(name, force){
    const has = this.set.has(name);
    const on = force === undefined ? !has : !!force;
    if (on) this.set.add(name); else this.set.delete(name);
    return on;
  }
}

function camelKey(name){ return name.replace(/-([a-z])/g, (_m, c) => c.toUpperCase()); }

class TextNode {
  constructor(text){
    this.text = text;
    this.parentElement = null;
    this.children = [];
  }
  get textContent(){ return this.text; }
}

class El {
  constructor(tag){
    this.tagName = String(tag || 'div').toUpperCase();
    this.children = [];
    this.parentElement = null;
    this.attrs = Object.create(null);
    this.dataset = Object.create(null);
    this.style = Object.create(null);
    this.id = '';
    this._value = undefined;
    this._checked = undefined;
    this.multiple = false;
    this.classList = new ClassList();
    this.listeners = Object.create(null);
    this._text = '';
  }
  get className(){ return Array.from(this.classList.set).join(' '); }
  set className(value){ this.classList.set = new Set(String(value == null ? '' : value).split(/\s+/).filter(Boolean)); }
  get textContent(){
    if (this.children.length) return this.children.map(child => child.textContent).join('');
    return this._text;
  }
  set textContent(value){ this.children = []; this._text = String(value == null ? '' : value); }
  // <select> 的 value 取选中项；其他元素就是写入值（初值来自 value 属性）。
  get value(){
    if (this.tagName === 'SELECT'){
      if (this._value !== undefined) return this._value;
      const options = queryAll(this, 'option');
      const chosen = options.find(option => option.hasAttribute('selected')) || options[0];
      return chosen ? (chosen.attrs.value || '') : '';
    }
    return this._value === undefined ? '' : this._value;
  }
  set value(value){ this._value = String(value == null ? '' : value); }
  // 勾选框：checked 属性 = 初始勾选；测试可以直接改 box.checked。
  get checked(){
    return this._checked === undefined ? this.hasAttribute('checked') : this._checked;
  }
  set checked(value){
    this._checked = !!value;
    if (this._checked) this.attrs.checked = '';
    else delete this.attrs.checked;
  }
  get innerHTML(){ return this.children.map(serializeNode).join(''); }
  set innerHTML(value){
    this.children = [];
    this._text = '';
    for (const node of parseHTML(String(value == null ? '' : value))) this.appendChild(node);
  }
  get outerHTML(){ return serializeNode(this); }
  set outerHTML(value){
    // syncRuleEntityOptions() 用 picker.outerHTML=next 就地换掉整个 picker。
    const parent = this.parentElement;
    if (!parent) return;
    const index = parent.children.indexOf(this);
    if (index < 0) return;
    const nodes = parseHTML(String(value == null ? '' : value));
    for (const node of nodes) node.parentElement = parent;
    parent.children.splice(index, 1, ...nodes);
    this.parentElement = null;
  }
  appendChild(node){ node.parentElement = this; this.children.push(node); return node; }
  addEventListener(type, fn){ (this.listeners[type] = this.listeners[type] || []).push(fn); }
  // 事件按 DOM 语义向上冒泡：页面把 input/change 监听绑在 #rule-list 上。
  dispatchEvent_(type, ev){
    const event = ev || { type, target: this };
    let node = this;
    let fired = 0;
    while (node){
      const list = (node.listeners && node.listeners[type]) || [];
      for (const fn of list) fn(event);
      fired += list.length;
      node = node.parentElement;
    }
    return fired;
  }
  hasAttribute(name){ return Object.prototype.hasOwnProperty.call(this.attrs, name); }
  getAttribute(name){ return this.hasAttribute(name) ? this.attrs[name] : null; }
  setAttribute(name, value){ this.attrs[name] = String(value); applyAttr(this, name, String(value)); }
  closest(selector){
    const compound = parseCompound(selector);
    let node = this;
    while (node){
      if (node.tagName && matchesCompound(node, compound)) return node;
      node = node.parentElement;
    }
    return null;
  }
  querySelector(selector){ return queryAll(this, selector)[0] || null; }
  querySelectorAll(selector){ return nodeList(queryAll(this, selector)); }
  scrollIntoView(){}
  click(){}
  get selectedOptions(){
    if (this.tagName !== 'SELECT') return [];
    const options = queryAll(this, 'option');
    if (this.multiple || this.hasAttribute('multiple')) {
      return options.filter(option => option.hasAttribute('selected'));
    }
    const chosen = options.find(option => option.hasAttribute('selected')) || options[0];
    return chosen ? [chosen] : [];
  }
}

class Doc {
  constructor(){
    this.documentElement = new El('html');
    this.body = new El('body');
    this.documentElement.appendChild(this.body);
  }
  createElement(tag){ return new El(tag); }
  createTextNode(text){ return new TextNode(text); }
  getElementById(id){ return descendantElements(this.documentElement).find(el => el.id === id) || null; }
  querySelector(selector){ return queryAll(this.documentElement, selector)[0] || null; }
  querySelectorAll(selector){ return nodeList(queryAll(this.documentElement, selector)); }
  addEventListener(){}
}

function serializeNode(node){
  if (node instanceof TextNode) return node.text;
  const tag = node.tagName.toLowerCase();
  const attrs = Object.keys(node.attrs)
    .map(name => node.attrs[name] === '' ? ' ' + name : ' ' + name + '="' + node.attrs[name] + '"')
    .join('');
  return '<' + tag + attrs + '>' + node.children.map(serializeNode).join('') + '</' + tag + '>';
}

function decodeEntities(text){
  return text
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&amp;/g, '&');
}

function findTagEnd(src, start){
  let quote = null;
  for (let i = start; i < src.length; i += 1){
    const ch = src[i];
    if (quote){
      if (ch === quote) quote = null;
    } else if (ch === '"' || ch === "'"){
      quote = ch;
    } else if (ch === '>'){
      return i;
    }
  }
  return -1;
}

function parseHTML(input){
  const root = [];
  const stack = [{ children: root, el: null }];
  const pushText = text => {
    if (!text) return;
    const frame = stack[stack.length - 1];
    const node = new TextNode(decodeEntities(text));
    node.parentElement = frame.el;
    frame.children.push(node);
  };
  let i = 0;
  while (i < input.length){
    const lt = input.indexOf('<', i);
    if (lt < 0){ pushText(input.slice(i)); break; }
    if (lt > i) pushText(input.slice(i, lt));
    if (input.startsWith('<!--', lt)){
      const end = input.indexOf('-->', lt + 4);
      i = end < 0 ? input.length : end + 3;
      continue;
    }
    const gt = findTagEnd(input, lt);
    if (gt < 0){ pushText(input.slice(lt)); break; }
    const raw = input.slice(lt + 1, gt);
    i = gt + 1;
    if (raw.charAt(0) === '/'){
      const closing = raw.slice(1).trim().toUpperCase();
      for (let k = stack.length - 1; k >= 1; k -= 1){
        if (stack[k].el && stack[k].el.tagName === closing){ stack.length = k; break; }
      }
      continue;
    }
    const nameMatch = /^([A-Za-z][\w:-]*)/.exec(raw);
    if (!nameMatch) continue;
    const el = new El(nameMatch[1]);
    parseAttrs(raw.slice(nameMatch[0].length), el);
    const frame = stack[stack.length - 1];
    el.parentElement = frame.el;
    frame.children.push(el);
    if (!VOID_TAGS.has(el.tagName.toLowerCase()) && !/\/\s*$/.test(raw)){
      stack.push({ children: el.children, el });
    }
  }
  return root;
}

function parseAttrs(raw, el){
  let i = 0;
  while (i < raw.length){
    while (i < raw.length && /\s/.test(raw[i])) i += 1;
    if (i >= raw.length) break;
    let j = i;
    while (j < raw.length && !/[=\s\/]/.test(raw[j])) j += 1;
    const name = raw.slice(i, j).toLowerCase();
    i = j;
    while (i < raw.length && /\s/.test(raw[i])) i += 1;
    let value = '';
    if (raw[i] === '='){
      i += 1;
      while (i < raw.length && /\s/.test(raw[i])) i += 1;
      const quote = raw[i];
      if (quote === '"' || quote === "'"){
        const end = raw.indexOf(quote, i + 1);
        value = raw.slice(i + 1, end < 0 ? raw.length : end);
        i = end < 0 ? raw.length : end + 1;
      } else {
        let k = i;
        while (k < raw.length && !/\s/.test(raw[k])) k += 1;
        value = raw.slice(i, k);
        i = k;
      }
    }
    if (!name) break;
    el.attrs[name] = value;
    applyAttr(el, name, value);
  }
}

function applyAttr(el, name, value){
  if (name === 'id') el.id = value;
  else if (name === 'class') el.className = value;
  else if (name === 'value') el.value = value;
  else if (name === 'checked') el._checked = true;
  else if (name === 'multiple') el.multiple = true;
  else if (name === 'style'){
    String(value).split(';').forEach(part => {
      const m = /^\s*([\w-]+)\s*:\s*(.*?)\s*$/.exec(part);
      if (m) el.style[camelKey(m[1])] = m[2];
    });
  }
  else if (name === 'style'){
    for (const part of String(value).split(';')){
      const at = part.indexOf(':');
      if (at <= 0) continue;
      const key = part.slice(0, at).trim();
      if (key) el.style[key] = part.slice(at + 1).trim();
    }
  }
  else if (name.startsWith('data-')) el.dataset[camelKey(name.slice(5))] = value;
}

function parseCompound(selector){
  const compound = { tag: '', id: '', classes: [], attrs: [], pseudos: [] };
  let i = 0;
  while (i < selector.length){
    const ch = selector[i];
    if (ch === '#'){
      const m = /^[\w-]+/.exec(selector.slice(i + 1));
      if (!m) { i += 1; continue; }
      compound.id = m[0];
      i += 1 + m[0].length;
    } else if (ch === '.'){
      const m = /^[\w-]+/.exec(selector.slice(i + 1));
      if (!m) { i += 1; continue; }
      compound.classes.push(m[0]);
      i += 1 + m[0].length;
    } else if (ch === '['){
      const end = selector.indexOf(']', i);
      const body = selector.slice(i + 1, end < 0 ? selector.length : end);
      const m = /^([\w-]+)\s*(?:=\s*(?:"([^"]*)"|'([^']*)'|([^\]]*)))?$/.exec(body);
      if (m){
        const value = m[2] !== undefined ? m[2] : (m[3] !== undefined ? m[3] : m[4]);
        compound.attrs.push({ name: m[1], value });
      }
      i = end < 0 ? selector.length : end + 1;
    } else if (ch === ':'){
      const m = /^[\w-]+/.exec(selector.slice(i + 1));
      if (!m) { i += 1; continue; }
      compound.pseudos.push(m[0]);
      i += 1 + m[0].length;
    } else if (ch === '*'){
      i += 1;
    } else {
      const m = /^[\w-]+/.exec(selector.slice(i));
      if (!m) { i += 1; continue; }
      compound.tag = m[0].toUpperCase();
      i += m[0].length;
    }
  }
  return compound;
}

function attrValue(el, name){
  if (name === 'id') return el.id;
  if (name === 'class') return el.className;
  if (Object.prototype.hasOwnProperty.call(el.attrs, name)) return el.attrs[name];
  if (name.startsWith('data-')){
    const key = camelKey(name.slice(5));
    if (Object.prototype.hasOwnProperty.call(el.dataset, key)) return String(el.dataset[key]);
  }
  return null;
}

function matchesCompound(el, compound){
  if (!el || !el.tagName) return false;
  if (compound.tag && compound.tag !== '*' && el.tagName !== compound.tag) return false;
  if (compound.id && el.id !== compound.id) return false;
  for (const name of compound.classes) if (!el.classList.contains(name)) return false;
  for (const attr of compound.attrs){
    const value = attrValue(el, attr.name);
    if (value === null) return false;
    if (attr.value !== undefined && String(value) !== attr.value) return false;
  }
  for (const pseudo of compound.pseudos){
    if (pseudo === 'last-child'){
      const parent = el.parentElement;
      if (!parent || parent.children[parent.children.length - 1] !== el) return false;
    } else if (pseudo === 'first-child'){
      const parent = el.parentElement;
      if (!parent || parent.children[0] !== el) return false;
    } else if (pseudo === 'checked'){
      if (!el.checked) return false;
    } else if (pseudo === 'disabled'){
      if (!el.hasAttribute('disabled')) return false;
    } else if (pseudo === 'selected'){
      if (!el.hasAttribute('selected')) return false;
    }
  }
  return true;
}

function descendantElements(root){
  const out = [];
  const walk = node => {
    for (const child of node.children || []){
      if (child instanceof El){ out.push(child); walk(child); }
    }
  };
  walk(root);
  return out;
}

function matchChain(el, parts, index){
  if (!matchesCompound(el, parts[index])) return false;
  if (index === 0) return true;
  let parent = el.parentElement;
  while (parent){
    if (matchChain(parent, parts, index - 1)) return true;
    parent = parent.parentElement;
  }
  return false;
}

function queryAll(root, selector){
  const parts = String(selector).trim().split(/\s+/).filter(Boolean).map(parseCompound);
  if (!parts.length) return [];
  const last = parts.length - 1;
  return descendantElements(root).filter(el => matchChain(el, parts, last));
}

// A real browser returns a NodeList from querySelectorAll: it has forEach/length/item
// and an iterator, but NO .filter/.map/.reduce/.find. Returning a plain Array here hid
// a shipped bug where the checkbox helpers called querySelectorAll(...).filter(...).
class NodeListLike {
  constructor(items){
    this.length = items.length;
    for (let i = 0; i < items.length; i += 1) this[i] = items[i];
  }
  item(i){ return this[i] || null; }
  forEach(cb, thisArg){ for (let i = 0; i < this.length; i += 1) cb.call(thisArg, this[i], i, this); }
  *[Symbol.iterator](){ for (let i = 0; i < this.length; i += 1) yield this[i]; }
}
function nodeList(items){ return new NodeListLike(items); }

// ===== page globals the extracted code expects =====
const document = new Doc();
let cfg = {};
let haDevices = [];
let deviceAliases = {};
let __alerts = [];

// 规则草稿状态：生产代码里是 script 级 let，harness 必须自己声明并逐例重置。
let ruleState = [];
let ruleBaseline = [];
let ruleDeleted = [];
let ruleBulkPending = false;
let ruleStateReady = false;
let ruleSeq = 0;

// fetch 打桩：保存类用例靠它捕获 POST /api/config 的整包 cfg。
let __fetchCalls = [];
let __fetchResponder = null;
function stubFetch(responder){ __fetchCalls = []; __fetchResponder = responder || null; }
globalThis.fetch = function(url, init){
  let body = null;
  if (init && typeof init.body === 'string'){
    try { body = JSON.parse(init.body); } catch (e) { body = init.body; }
  }
  const call = { url, init: init || null, body };
  __fetchCalls.push(call);
  const payload = __fetchResponder ? __fetchResponder(call) : { ok: true };
  return Promise.resolve({ ok: true, status: 200, json: async function(){ return payload; } });
};

// exportRules() 用 Blob + URL.createObjectURL：抓下 blob 以便断言导出内容。
let __lastBlob = null;
if (typeof Blob === 'undefined'){
  globalThis.Blob = class Blob {
    constructor(parts){ this.parts = (parts || []).map(String); }
    async text(){ return this.parts.join(''); }
  };
}
URL.createObjectURL = function(blob){ __lastBlob = blob; return 'blob:test'; };
URL.revokeObjectURL = function(){};

function showAlert(target, kind, message){
  __alerts.push({ kind, message });
  if (target) target.className = 'alert ' + kind + ' show';
}
function fetchLogs(){}
function loadDevices(){}
function loadSchedules(){}

// Do not let the page's 3s alert timeouts keep the process alive.
globalThis.setTimeout = function(){ return 0; };
"""

HARNESS_TESTS = r"""
// ============================== harness helpers ==============================
let __passes = 0;
let __failures = 0;
const __lines = [];

function check(name, condition, detail){
  if (condition){
    __passes += 1;
    __lines.push('PASS ' + name);
  } else {
    __failures += 1;
    __lines.push('FAIL ' + name + (detail ? ' :: ' + detail : ''));
  }
}

function checkEq(name, actual, expected){
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  check(name, a === e, 'expected ' + e + ', got ' + a);
}

// 包一层 renderRules：证明"不该重渲染"的路径确实没有重渲染。
const realRenderRules = renderRules;
let __renderCount = 0;
renderRules = function(){
  __renderCount += 1;
  return realRenderRules();
};

function resetDom(){
  document.body.children = [];
  document.body._text = '';
  cfg = {};
  haDevices = [];
  deviceAliases = {};
  __alerts = [];
  __fetchCalls = [];
  __fetchResponder = null;
  __lastBlob = null;
  ruleState = [];
  ruleBaseline = [];
  ruleDeleted = [];
  ruleBulkPending = false;
  ruleStateReady = false;
  ruleSeq = 0;
  __renderCount = 0;
}

const RULES_PAGE_HTML =
  '<div class="sidebar-item" data-page="rules">意图规则</div>' +
  '<div class="page" id="page-rules">' +
    '<div id="rules-alert" class="alert"></div>' +
    '<div class="btn-group">' +
      '<button class="btn btn-primary" onclick="saveRules()">保存全部</button>' +
      '<span id="rules-dirty" style="display:none">● 有未保存修改</span>' +
    '</div>' +
    '<div class="test-row"><input type="search" id="rule-filter">' +
      '<span id="rule-count"></span></div>' +
    '<div class="rule-list" id="rule-list"></div>' +
  '</div>';

function rows(){ return Array.from(document.querySelectorAll('#rule-list .rule-item')); }
function boxes(row){ return row ? Array.from(row.querySelectorAll('.r-e')) : []; }
function checkedIds(row){ return boxes(row).filter(box => box.checked).map(box => box.value); }
function setChecked(row, ids){
  const chosen = new Set(ids || []);
  boxes(row).forEach(box => { box.checked = chosen.has(box.value); });
}
function pickerOf(row){ return row ? row.querySelector('.entity-picker') : null; }
function pickerIds(row){ return Array.from(row.querySelectorAll('.entity-option .entity-id')).map(el => el.textContent); }
function pickerCount(row){ const el = row ? row.querySelector('.entity-count') : null; return el ? el.textContent : null; }
function chipOf(row){ return row ? row.querySelector('.rule-chip') : null; }
function optionValues(el){ return Array.from(el.querySelectorAll('option')).map(option => option.attrs.value); }
function postedCalls(){ return __fetchCalls.filter(call => call.body && call.body.config); }
function postedRules(){
  const calls = postedCalls();
  return calls.length ? calls[calls.length - 1].body.config.intent_rules : null;
}
function cloneJson(value){ return JSON.parse(JSON.stringify(value)); }
function makeRulesPage(rules, options){
  const opts = options || {};
  resetDom();
  if (opts.devices) haDevices = opts.devices.slice();
  if (opts.aliases) deviceAliases = Object.assign({}, opts.aliases);
  document.body.innerHTML = RULES_PAGE_HTML + (opts.extraHtml || '');
  cfg = { intent_rules: rules || [] };
  realRenderRules();
  bindRulesDirty();  // 和页面 init 一样：renderRules(); bindRulesDirty();
  return rows();
}
// importRules() 自己造 <input type=file>，这里抓住它并塞入文件内容。
async function importFromJson(text){
  const realCreate = document.createElement;
  let input = null;
  document.createElement = function(tag){
    const el = realCreate.call(document, tag);
    if (String(tag).toLowerCase() === 'input') input = el;
    return el;
  };
  try { importRules(); } finally { document.createElement = realCreate; }
  if (!input) throw new Error('importRules() did not create a file input');
  input.files = [{ text: async function(){ return text; } }];
  await input.onchange();
  return input;
}

const LIGHT_DEVICES = [
  { entity_id: 'light.a', domain: 'light', name: 'A灯' },
  { entity_id: 'light.b', domain: 'light', name: 'B灯' },
  { entity_id: 'climate.ac', domain: 'climate', name: '空调' },
  { entity_id: 'switch.c', domain: 'switch', name: '开关C' },
];

const tests = [];
function test(name, fn){ tests.push({ name, fn }); }

// ================================== tests ==================================

// --- G1: ruleEntityIds keeps string/array/missing forms straight -------------
test('ruleEntityIds: string stays a one element list', () => {
  checkEq('ruleEntityIds(string)', ruleEntityIds({ action: { entity_id: 'light.a' } }), ['light.a']);
});
test('ruleEntityIds: array is preserved', () => {
  checkEq('ruleEntityIds(array)', ruleEntityIds({ action: { entity_id: ['light.a', 'light.b'] } }), ['light.a', 'light.b']);
});
test('ruleEntityIds: empty / missing -> []', () => {
  checkEq('ruleEntityIds(empty string)', ruleEntityIds({ action: { entity_id: '' } }), []);
  checkEq('ruleEntityIds(missing)', ruleEntityIds({ action: {} }), []);
});

// --- G2: checkbox picker markup (replaces the old <select multiple>) --------
test('entityPickerHtml: checkbox rows, checked state, aliases, domain filter', () => {
  resetDom();
  haDevices = LIGHT_DEVICES.slice();
  deviceAliases = { 'light.b': '客厅灯' };
  const one = entityPickerHtml('light', ['light.a']);
  check('picker renders checkbox rows',
    one.includes('type="checkbox"') && one.includes('class="r-e"'), one);
  check('picker has no legacy multi-select',
    !one.includes('<select') && !one.includes('multiple') && !one.includes('按住 Ctrl'), one);
  check('picker has a search box wired to filterEntityOptions',
    one.includes('class="entity-search"') && one.includes('filterEntityOptions(this)'), one);
  check('picker has wired 全选 / 清空 buttons',
    one.includes('selectAllEntities(this)') && one.includes('clearEntities(this)') &&
    one.includes('全选') && one.includes('清空'), one);
  check('single selection marks exactly one box',
    (one.match(/class="r-e" value="[^"]*" checked/g) || []).length === 1, one);
  check('single selection marks the right box',
    one.includes('value="light.a" checked') && !one.includes('value="light.b" checked'), one);
  check('picker shows the selected counter', one.includes('已选择 1 个设备'), one);
  check('picker uses the alias as the label',
    one.includes('客厅灯') && one.includes('<code class="entity-id">light.b</code>'), one);
  check('picker lists only the requested domain',
    one.includes('light.a') && one.includes('light.b') &&
    !one.includes('climate.ac') && !one.includes('switch.c'), one);
  const many = entityPickerHtml('light', ['light.a', 'light.b']);
  check('multi selection marks both boxes',
    many.includes('value="light.a" checked') && many.includes('value="light.b" checked'), many);
  check('multi selection counter', many.includes('已选择 2 个设备'), many);
  const none = entityPickerHtml('light', []);
  check('no selection marks no box',
    (none.match(/class="r-e" value="[^"]*" checked/g) || []).length === 0, none);
  check('no selection counter is zero', none.includes('已选择 0 个设备'), none);
});
test('entityPickerHtml: stale current entity and empty device list', () => {
  resetDom();
  haDevices = LIGHT_DEVICES.slice();
  const stale = entityPickerHtml('light', ['light.legacy']);
  check('unknown current entity is kept checked', /value="light\.legacy" checked/.test(stale), stale);
  check('stale row is flagged',
    stale.includes('entity-option stale') && stale.includes('（当前，未在设备列表）'), stale);
  resetDom();
  haDevices = [];
  const empty = entityPickerHtml('light', []);
  check('empty device list shows the empty hint', empty.includes('未读取到可用设备'), empty);
  check('empty device list keeps the zero counter', empty.includes('已选择 0 个设备'), empty);
  const kept = entityPickerHtml('light', ['light.a']);
  check('saved selection survives an empty device list',
    /value="light\.a" checked/.test(kept) && kept.includes('已选择 1 个设备'), kept);
});

// --- G3: checkbox selection -> entity_id string vs array --------------------
test('checkbox selection: single -> string, multi -> array', () => {
  const list = makeRulesPage([
    { pattern: '开灯', action: { domain: 'light', service: 'turn_on', entity_id: 'light.a', reply: '好的' } },
    { pattern: '全开', action: { domain: 'light', service: 'turn_on', entity_id: ['light.a', 'light.b'], reply: '好的' } },
    { pattern: '空选', action: { domain: 'light', service: 'turn_on', entity_id: '', reply: '好的' } },
  ], { devices: LIGHT_DEVICES });
  checkEq('every rule renders two candidate boxes', list.map(row => boxes(row).length), [2, 2, 2]);
  checkEq('string selection checks one box', checkedIds(list[0]), ['light.a']);
  checkEq('array selection checks both boxes', checkedIds(list[1]), ['light.a', 'light.b']);
  checkEq('empty selection checks nothing', checkedIds(list[2]), []);
  checkEq('entityIdsOf reads the checked boxes', entityIdsOf(list[1]), ['light.a', 'light.b']);
  checkEq('draft: one checkbox -> string', ruleDraftFromRow(list[0]).action.entity_id, 'light.a');
  checkEq('draft: two checkboxes -> array', ruleDraftFromRow(list[1]).action.entity_id, ['light.a', 'light.b']);
  checkEq('draft: zero checkboxes -> empty string', ruleDraftFromRow(list[2]).action.entity_id, '');
  setChecked(list[0], ['light.a', 'light.b']);
  updateEntityCount(pickerOf(list[0]));
  checkEq('manual multi check -> array', ruleDraftFromRow(list[0]).action.entity_id, ['light.a', 'light.b']);
  checkEq('manual multi check updates the counter', pickerCount(list[0]), '已选择 2 个设备');
  setChecked(list[0], ['light.b']);
  updateEntityCount(pickerOf(list[0]));
  checkEq('manual single check -> string', ruleDraftFromRow(list[0]).action.entity_id, 'light.b');
  checkEq('manual single check updates the counter', pickerCount(list[0]), '已选择 1 个设备');
  setEntitySelection(list[0], ['light.a']);
  checkEq('setEntitySelection replaces the whole selection', checkedIds(list[0]), ['light.a']);
  checkEq('setEntitySelection updates the counter', pickerCount(list[0]), '已选择 1 个设备');
});

// --- G4: picker toolbar (全选 / 清空 / 搜索) --------------------------------
test('picker toolbar: 全选 / 清空 / 搜索 / 已选择计数', () => {
  const list = makeRulesPage([
    { pattern: '开灯', action: { domain: 'light', service: 'turn_on', entity_id: 'light.a', reply: '好的' } },
  ], { devices: LIGHT_DEVICES, aliases: { 'light.b': '客厅灯' } });
  const row = list[0];
  const picker = pickerOf(row);
  const buttons = Array.from(picker.querySelectorAll('button'));
  const allBtn = buttons.find(button => String(button.attrs.onclick || '') === 'selectAllEntities(this)');
  const clearBtn = buttons.find(button => String(button.attrs.onclick || '') === 'clearEntities(this)');
  const searchBox = picker.querySelector('.entity-search');
  check('全选 button exists and is wired', !!allBtn);
  check('清空 button exists and is wired', !!clearBtn);
  check('search box is wired to filterEntityOptions',
    !!searchBox && String(searchBox.attrs.oninput || '') === 'filterEntityOptions(this)');
  checkEq('the initial counter follows the saved selection', pickerCount(row), '已选择 1 个设备');

  searchBox.value = '客厅';
  checkEq('搜索只留下匹配的行', filterEntityOptions(searchBox), 1);
  checkEq('搜索按 data-search 隐藏其他行',
    visibleEntityOptions(picker).map(el => el.dataset.search), ['客厅灯 light.b']);
  checkEq('搜索不改变已选择计数', pickerCount(row), '已选择 1 个设备');
  checkEq('全选只勾选可见行', selectAllEntities(allBtn), 2);
  checkEq('全选把可见行也勾上', checkedIds(row).slice().sort(), ['light.a', 'light.b']);
  checkEq('全选之后计数文本更新', pickerCount(row), '已选择 2 个设备');

  searchBox.value = '';
  checkEq('清空搜索后所有行可见', filterEntityOptions(searchBox), 2);
  checkEq('清空按钮取消全部勾选', clearEntities(clearBtn), 0);
  checkEq('清空之后没有勾选', checkedIds(row), []);
  checkEq('清空之后计数归零', pickerCount(row), '已选择 0 个设备');

  searchBox.value = '客厅';
  checkEq('清空后再次搜索仍只剩一行', filterEntityOptions(searchBox), 1);
  checkEq('全选只勾选当前可见的那一行', selectAllEntities(allBtn), 1);
  checkEq('全选结果的勾选集合', checkedIds(row), ['light.b']);
  searchBox.value = '';
  filterEntityOptions(searchBox);
  checkEq('搜索清空后勾选保持不变', checkedIds(row), ['light.b']);
});

// --- G5: text / summary helpers -------------------------------------------
test('ruleSearchText / ruleTargetNames / ruleSummaryHtml', () => {
  resetDom();
  haDevices = LIGHT_DEVICES.slice();
  deviceAliases = { 'light.b': '客厅灯' };
  checkEq('ruleTargetNames(string)', ruleTargetNames({ action: { entity_id: 'light.b' } }), ['客厅灯']);
  checkEq('ruleTargetNames(array)', ruleTargetNames({ action: { entity_id: ['light.a', 'light.b'] } }), ['A灯', '客厅灯']);
  checkEq('ruleTargetNames(unknown)', ruleTargetNames({ action: { entity_id: 'light.zzz' } }), ['light.zzz']);
  const text = ruleSearchText({ name: '客厅总控', pattern: '打开客厅灯', action: { entity_id: ['light.a', 'light.b'] } });
  check('ruleSearchText includes the name, pattern, ids and names',
    text.includes('客厅总控') && text.includes('打开客厅灯') &&
    text.includes('light.a') && text.includes('客厅灯'), text);
  check('ruleSearchText is lower case', text === text.toLowerCase(), text);
  const summary = ruleSummaryHtml({
    pattern: '打开客厅灯',
    action: { domain: 'light', service: 'turn_on', entity_id: ['light.a', 'light.b'] },
  });
  check('summary shows the trigger sentence', summary.includes('打开客厅灯'), summary);
  check('summary shows domain, operation and device count',
    summary.includes('灯光') && summary.includes('2 个设备'), summary);
  check('summary lists the target names',
    summary.includes('A灯') && summary.includes('客厅灯'), summary);
  check('summary marks an empty trigger sentence',
    ruleSummaryHtml({ pattern: '', action: {} }).includes('未填写触发语句'),
    ruleSummaryHtml({ pattern: '', action: {} }));
});

// --- G6: collectRulesFromUI ------------------------------------------------
test('collectRulesFromUI: checkbox selection round-trip', () => {
  const list = makeRulesPage([
    { pattern: '开灯', action: { domain: 'light', service: 'turn_on', entity_id: 'light.a', reply: '好的' } },
    { pattern: '全开', action: { domain: 'light', service: 'turn_on', entity_id: ['light.a', 'light.b'], reply: '好的' } },
    { pattern: '空选', action: { domain: 'light', service: 'turn_on', entity_id: '', reply: '好的' } },
  ], { devices: LIGHT_DEVICES });
  checkEq('fixture built three rows', list.length, 3);
  collectRulesFromUI();
  checkEq('collect: rule count', cfg.intent_rules.length, 3);
  checkEq('collect: single entity id stays a string', cfg.intent_rules[0].action.entity_id, 'light.a');
  checkEq('collect: multiple entity ids stay an array', cfg.intent_rules[1].action.entity_id, ['light.a', 'light.b']);
  checkEq('collect: zero selection becomes an empty string', cfg.intent_rules[2].action.entity_id, '');
  checkEq('collect: pattern preserved', cfg.intent_rules[0].pattern, '开灯');
  checkEq('collect: reply default applied', cfg.intent_rules[2].action.reply, '好的');
  checkEq('collect: service preserved', cfg.intent_rules[0].action.service, 'turn_on');
  checkEq('collect: no name key when the field is empty', 'name' in cfg.intent_rules[0], false);
});
test('collectRulesFromUI: ignores .rule-item nodes from other pages', () => {
  makeRulesPage(
    [{ pattern: '开灯', action: { domain: 'light', service: 'turn_on', entity_id: 'light.a', reply: '好的' } }],
    {
      devices: LIGHT_DEVICES,
      extraHtml:
        '<div class="page" id="page-devices"><div class="rule-list" id="device-list">' +
          '<div class="rule-item"><div class="device-item">设备页的 rule-item</div></div>' +
        '</div></div>',
    }
  );
  checkEq('fixture has an out of scope .rule-item', document.querySelectorAll('.rule-item').length, 2);
  collectRulesFromUI();
  checkEq('collect ignores device page rows', cfg.intent_rules.length, 1);
  checkEq('collect keeps the intent rule', cfg.intent_rules[0].pattern, '开灯');
});
test('collectRulesFromUI: extra fields and JSON extras survive', () => {
  makeRulesPage([
    { pattern: '调亮', action: { domain: 'light', service: 'turn_on', entity_id: 'light.a', brightness_pct: '{0}', reply: '好的' } },
  ], { devices: LIGHT_DEVICES });
  collectRulesFromUI();
  checkEq('collect keeps extra HA field', cfg.intent_rules[0].action.brightness_pct, '{0}');
  checkEq('collect keeps entity id', cfg.intent_rules[0].action.entity_id, 'light.a');
});

// --- G7: filterRules respects dataset.search and count ---------------------
test('filterRules: filters on dataset.search and updates the count', () => {
  const list = makeRulesPage([
    { pattern: '打开客厅灯', action: { domain: 'light', service: 'turn_on', entity_id: 'light.b', reply: '好的' } },
    { pattern: '关闭卧室灯', action: { domain: 'light', service: 'turn_off', entity_id: 'light.a', reply: '好的' } },
    { pattern: '开空调', action: { domain: 'climate', service: 'set_temperature', entity_id: 'climate.ac', reply: '好的' } },
  ], { devices: LIGHT_DEVICES, aliases: { 'light.b': '客厅灯' } });
  checkEq('fixture built 3 rows', list.length, 3);
  check('rows carry dataset.search', list.every(row => typeof row.dataset.search === 'string' && row.dataset.search.length > 0));
  document.getElementById('rule-filter').value = '客厅';
  filterRules();
  checkEq('filter shows matching row', list[0].style.display, '');
  checkEq('filter hides non matching row', list[1].style.display, 'none');
  checkEq('filter count text', document.getElementById('rule-count').textContent, '1/3 条');
  document.getElementById('rule-filter').value = 'light.a';
  filterRules();
  checkEq('filter matches raw entity id', list[1].style.display, '');
  checkEq('filter by entity id count', document.getElementById('rule-count').textContent, '1/3 条');
  document.getElementById('rule-filter').value = '';
  filterRules();
  check('filter cleared shows every row', list.every(row => row.style.display === ''),
    list.map(row => row.style.display).join(','));
  checkEq('filter cleared count', document.getElementById('rule-count').textContent, '3/3 条');
});

// --- G8: editing one rule must not corrupt the others ---------------------
test('switchD rebuilds only that rule and keeps other rules intact', () => {
  const list = makeRulesPage([
    { pattern: '开灯', action: { domain: 'light', service: 'turn_on', entity_id: 'light.a', brightness_pct: '{0}', reply: '好的' } },
    { pattern: '关灯', action: { domain: 'light', service: 'turn_off', entity_id: ['light.a', 'light.b'], reply: '收到' } },
  ], { devices: LIGHT_DEVICES });
  checkEq('two rows built', list.length, 2);
  list[0].querySelector('.r-p').value = '开灯(改)';
  const buttons = Array.from(list[0].querySelectorAll('.domain-btn'));
  const climateButton = buttons.find(button => String(button.attrs.onclick || '').includes("'climate'"));
  check('climate domain button exists', !!climateButton);
  switchD(climateButton, 'climate');
  const row = rows()[0];
  checkEq('switchD rewrites the hidden domain field', row.querySelector('.r-d').value, 'climate');
  checkEq('switchD updates the candidate list for the new domain', pickerIds(row).includes('climate.ac'), true);
  checkEq('switchD clears the old-domain entity', row.querySelectorAll('.entity-option.stale').length, 0);
  checkEq('switchD clears the old-domain checkboxes', checkedIds(row), []);
  checkEq('switchD rebuilds that rule service options',
    optionValues(row.querySelector('.r-s')), ['set_hvac_mode', 'turn_off', 'set_temperature']);
  checkEq('switchD rebuilds that rule extra fields', row.querySelectorAll('.r-xk').length, 3);
  checkEq('switchD keeps the in progress pattern edit', row.querySelector('.r-p').value, '开灯(改)');
  checkEq('switchD keeps the reply field', row.querySelector('.r-r').value, '好的');
  checkEq('other rule candidate list untouched', pickerIds(rows()[1]).slice().sort(), ['light.a', 'light.b']);
  checkEq('other rule selection untouched', checkedIds(rows()[1]), ['light.a', 'light.b']);
  checkEq('other rule pattern untouched', rows()[1].querySelector('.r-p').value, '关灯');
  checkEq('other rule service options untouched',
    optionValues(rows()[1].querySelector('.r-s')), ['turn_on', 'turn_off', 'toggle']);
  checkEq('other rule extra fields untouched', rows()[1].querySelectorAll('.r-xk').length, 1);
  collectRulesFromUI();
  checkEq('collect: edited rule kept its pattern', cfg.intent_rules[0].pattern, '开灯(改)');
  checkEq('collect: edited rule uses the new domain', cfg.intent_rules[0].action.domain, 'climate');
  // 设备类型切换只保留新域里合法的实体：旧域的 light.a 必须被清掉。
  checkEq('collect: edited rule has no entity after the domain switch',
    cfg.intent_rules[0].action.entity_id, '');
  checkEq('collect: other rule untouched after collecting',
    cfg.intent_rules[1].action.entity_id, ['light.a', 'light.b']);
  checkEq('collect: other rule pattern untouched after collecting', cfg.intent_rules[1].pattern, '关灯');
});

// --- F1: async device load must not clobber edits --------------------------
test('syncRuleEntityOptions refreshes options without re-rendering the list', () => {
  haDevices = [];
  const list = makeRulesPage([
    { pattern: '开灯', action: { domain: 'light', service: 'turn_on', entity_id: 'light.a', reply: '好的' } },
  ]);
  rows()[0].querySelector('.r-p').value = '未保存的修改';
  haDevices = LIGHT_DEVICES.slice();
  const before = __renderCount;
  const touched = syncRuleEntityOptions();
  checkEq('sync touches the rendered rows', touched, 1);
  checkEq('sync does not call renderRules', __renderCount, before);
  checkEq('sync keeps the unsaved pattern edit', rows()[0].querySelector('.r-p').value, '未保存的修改');
  checkEq('sync keeps the saved entity selection', checkedIds(rows()[0]), ['light.a']);
  checkEq('sync fills in the freshly loaded device options',
    pickerIds(rows()[0]).slice().sort(), ['light.a', 'light.b']);
  checkEq('sync keeps the selected counter', pickerCount(rows()[0]), '已选择 1 个设备');
  const again = syncRuleEntityOptions();
  checkEq('sync is repeatable', again, 1);
  checkEq('sync does not reset the selection on a second run', checkedIds(rows()[0]), ['light.a']);
  checkEq('fixture row count is stable', list.length, 1);
});

// --- F2: entering the rules tab must not clobber edits ---------------------
test('showPage(rules) reloads entity options without re-rendering', async () => {
  haDevices = [];
  const list = makeRulesPage([
    { pattern: '开灯', action: { domain: 'light', service: 'turn_on', entity_id: 'light.a', reply: '好的' } },
  ]);
  rows()[0].querySelector('.r-p').value = '切页前改的';
  const calls = { load: 0, logs: 0, devices: 0, schedules: 0 };
  const realLoadRuleDevices = loadRuleDevices;
  loadRuleDevices = async function(){
    calls.load += 1;
    haDevices = LIGHT_DEVICES.slice();
  };
  fetchLogs = function(){ calls.logs += 1; };
  loadDevices = function(){ calls.devices += 1; };
  loadSchedules = function(){ calls.schedules += 1; };
  const before = __renderCount;
  showPage('rules');
  await new Promise(resolve => setImmediate(resolve));
  checkEq('showPage(rules) does not call renderRules', __renderCount, before);
  checkEq('showPage(rules) reloads the device list', calls.load, 1);
  checkEq('showPage(rules) keeps the unsaved edit', rows()[0].querySelector('.r-p').value, '切页前改的');
  checkEq('showPage(rules) refreshes the candidate list',
    pickerIds(rows()[0]).slice().sort(), ['light.a', 'light.b']);
  checkEq('showPage(rules) keeps the entity selection', checkedIds(rows()[0]), ['light.a']);
  showPage('logs');
  checkEq('showPage(logs) still fetches logs', calls.logs, 1);
  loadRuleDevices = realLoadRuleDevices;
  checkEq('fixture row count is stable', list.length, 1);
});

// --- S1: 单条保存只提交 baseline + 这一条 ----------------------------------
test('saveOneRule posts the baseline with only that rule replaced', async () => {
  const list = makeRulesPage([
    { pattern: '开灯', action: { domain: 'light', service: 'turn_on', entity_id: 'light.a', reply: '好的' } },
    { pattern: '关灯', action: { domain: 'light', service: 'turn_off', entity_id: 'light.b', reply: '收到' } },
  ], { devices: LIGHT_DEVICES });
  list[0].querySelector('.r-p').value = '开灯(改)';
  list[1].querySelector('.r-p').value = '关灯(未保存)';
  stubFetch();
  const ok = await saveOneRule(0);
  checkEq('saveOneRule reports success', ok, true);
  checkEq('saveOneRule posts exactly once', __fetchCalls.length, 1);
  checkEq('saveOneRule posts to /api/config', __fetchCalls[0].url, '/api/config');
  const posted = postedRules();
  checkEq('both rules are posted', posted.length, 2);
  checkEq('saved rule carries the edit', posted[0].pattern, '开灯(改)');
  checkEq("other rule's saved value is posted untouched", posted[1].pattern, '关灯');
  checkEq("other rule's unsaved draft is not posted",
    posted.some(rule => rule.pattern === '关灯(未保存)'), false);
  checkEq("other row's DOM keeps its draft", list[1].querySelector('.r-p').value, '关灯(未保存)');
  checkEq('baseline updated for the saved rule', ruleBaseline[0].rule.pattern, '开灯(改)');
  checkEq('baseline untouched for the other rule', ruleBaseline[1].rule.pattern, '关灯');
  checkEq('saved row no longer shows the unsaved chip', chipOf(rows()[0]).style.display, 'none');
});

// --- S2: 草稿隔离：编辑 A、编辑 B、只保存 B -------------------------------
test('draft isolation: saving B leaves A untouched', async () => {
  const list = makeRulesPage([
    { pattern: 'A', action: { domain: 'light', service: 'turn_on', entity_id: 'light.a', reply: '好的' } },
    { pattern: 'B', action: { domain: 'light', service: 'turn_on', entity_id: 'light.b', reply: '好的' } },
  ], { devices: LIGHT_DEVICES });
  list[0].querySelector('.r-p').value = 'A(草稿)';
  list[0].querySelector('.r-p').dispatchEvent_('input');
  list[1].querySelector('.r-p').value = 'B(已保存)';
  stubFetch();
  const ok = await saveOneRule(1);
  checkEq('saveOneRule(B) reports success', ok, true);
  checkEq("A's DOM draft is untouched", list[0].querySelector('.r-p').value, 'A(草稿)');
  checkEq("A's chip is still visible", chipOf(list[0]).style.display, '');
  checkEq("A's draft is still not part of the baseline", ruleBaseline[0].rule.pattern, 'A');
  checkEq("B's draft is saved", ruleState[1].draft.pattern, 'B(已保存)');
  checkEq("B's chip is hidden again", chipOf(rows()[1]).style.display, 'none');
  const posted = postedRules();
  checkEq('posted A is the baseline value', posted[0].pattern, 'A');
  checkEq('posted B is the draft', posted[1].pattern, 'B(已保存)');
  checkEq('the page still shows the unsaved marker because A is dirty',
    document.getElementById('rules-dirty').style.display, '');
});

// --- S3: 保存全部 ---------------------------------------------------------
test('saveRules posts every draft and replaces the baseline', async () => {
  const list = makeRulesPage([
    { pattern: 'A', action: { domain: 'light', service: 'turn_on', entity_id: 'light.a', reply: '好的' } },
    { pattern: 'B', action: { domain: 'light', service: 'turn_off', entity_id: 'light.b', reply: '好的' } },
  ], { devices: LIGHT_DEVICES });
  list[0].querySelector('.r-p').value = 'A2';
  list[1].querySelector('.r-p').value = 'B2';
  list[0].querySelector('.r-p').dispatchEvent_('input');
  stubFetch();
  const before = __renderCount;
  const ok = await saveRules();
  checkEq('saveRules reports success', ok, true);
  checkEq('saveRules posts exactly once', __fetchCalls.length, 1);
  const posted = postedRules();
  checkEq('both drafts are posted', posted.map(rule => rule.pattern), ['A2', 'B2']);
  checkEq('baseline replaced by the drafts', ruleBaseline.map(entry => entry.rule.pattern), ['A2', 'B2']);
  checkEq('successful save-all re-renders once', __renderCount - before, 1);
  checkEq('unsaved marker cleared', document.getElementById('rules-dirty').style.display, 'none');
  checkEq('rows re-rendered from the saved drafts',
    rows().map(row => row.querySelector('.r-p').value), ['A2', 'B2']);
  checkEq('chips hidden after save-all',
    rows().map(row => chipOf(row).style.display), ['none', 'none']);
  checkEq('no delete is pending any more', ruleDeleted, []);
});

// --- S4: 保存失败 ---------------------------------------------------------
test('failed per-rule save keeps edits, chip and marker', async () => {
  const list = makeRulesPage([
    { pattern: '开灯', action: { domain: 'light', service: 'turn_on', entity_id: 'light.a', reply: '好的' } },
  ], { devices: LIGHT_DEVICES });
  list[0].querySelector('.r-p').value = '改到一半';
  list[0].querySelector('.r-p').dispatchEvent_('input');
  const row = rows()[0];
  stubFetch(function(){ return { ok: false, msg: '服务器拒绝' }; });
  const ok = await saveOneRule(0);
  checkEq('saveOneRule reports the failure', ok, false);
  checkEq('failed save does not re-render', __renderCount, 0);
  checkEq('the same DOM node is kept', rows()[0] === row, true);
  checkEq('failed save keeps the edited text in the DOM', rows()[0].querySelector('.r-p').value, '改到一半');
  checkEq('failed save keeps the unsaved chip', chipOf(rows()[0]).style.display, '');
  checkEq('failed save keeps the global unsaved marker',
    document.getElementById('rules-dirty').style.display, '');
  checkEq('failed save leaves the baseline untouched', ruleBaseline[0].rule.pattern, '开灯');
  check('failed save shows an error alert', __alerts.some(alert => alert.kind === 'err'),
    JSON.stringify(__alerts));
});

// --- S5: 规则名称 ---------------------------------------------------------
test('rule name: title priority, payload and reload', async () => {
  checkEq('title prefers the name', ruleTitle({ name: '客厅灯组开灯', pattern: '打开客厅灯' }, 0), '客厅灯组开灯');
  checkEq('title falls back to the pattern', ruleTitle({ name: '   ', pattern: '打开客厅灯' }, 0), '打开客厅灯');
  checkEq('title falls back to 新规则 #N', ruleTitle({ pattern: '   ' }, 2), '新规则 #3');
  checkEq('ruleName ignores non strings', ruleName({ name: 42 }), '');
  const list = makeRulesPage([
    { name: '客厅灯组开灯', pattern: '打开客厅灯', action: { domain: 'light', service: 'turn_on', entity_id: 'light.a', reply: '好的' } },
    { pattern: '关灯', action: { domain: 'light', service: 'turn_off', entity_id: 'light.b', reply: '好的' } },
    { pattern: '', action: { domain: 'light', service: 'turn_on', entity_id: '', reply: '好的' } },
  ], { devices: LIGHT_DEVICES });
  checkEq('rendered title uses the name',
    list[0].querySelector('.rule-title').textContent, '客厅灯组开灯');
  checkEq('rendered title falls back to the pattern',
    list[1].querySelector('.rule-title').textContent, '关灯');
  checkEq('rendered title falls back to 新规则 #N',
    list[2].querySelector('.rule-title').textContent, '新规则 #3');
  checkEq('the name field is prefilled', list[0].querySelector('.r-n').value, '客厅灯组开灯');
  checkEq('rule number badge is rendered', list[0].querySelector('.rule-num').textContent, '#1');
  list[1].querySelector('.r-n').value = '关灯命令';
  stubFetch();
  await saveOneRule(1);
  checkEq('the name is posted', postedRules()[1].name, '关灯命令');
  const reloaded = makeRulesPage(cloneJson(cfg.intent_rules), { devices: LIGHT_DEVICES });
  checkEq('the name survives a reload', reloaded[1].querySelector('.r-n').value, '关灯命令');
  checkEq('the title survives a reload',
    reloaded[1].querySelector('.rule-title').textContent, '关灯命令');
});

// --- S6: 不带 name 的历史 JSON -------------------------------------------
test('legacy JSON without name still edits, saves and exports', async () => {
  const list = makeRulesPage([
    { pattern: '打开客厅灯', action: { domain: 'light', service: 'turn_on', entity_id: 'light.a', reply: '好的' } },
  ], { devices: LIGHT_DEVICES });
  checkEq('an empty name field is rendered', list[0].querySelector('.r-n').value, '');
  checkEq('the draft has no name property', 'name' in ruleDraftFromRow(list[0]), false);
  checkEq('the pattern is preserved', ruleDraftFromRow(list[0]).pattern, '打开客厅灯');
  list[0].querySelector('.r-p').value = '打开客厅灯吧';
  stubFetch();
  const ok = await saveOneRule(0);
  checkEq('the legacy rule can still be saved', ok, true);
  const posted = postedRules()[0];
  checkEq('the saved rule has no name property', 'name' in posted, false);
  checkEq('the saved rule keeps the edited pattern', posted.pattern, '打开客厅灯吧');
  __lastBlob = null;
  exportRules();
  check('export produced a blob', !!__lastBlob);
  const exported = JSON.parse(await __lastBlob.text());
  checkEq('export contains one rule', exported.length, 1);
  checkEq('the exported legacy rule has no name property', 'name' in exported[0], false);
  checkEq('export keeps the pattern', exported[0].pattern, '打开客厅灯吧');
});

// --- S7: 触发语句仍是匹配键，name 不替代它 -------------------------------
test('the trigger sentence stays the match key (name never replaces it)', async () => {
  const list = makeRulesPage([
    { pattern: '打开客厅灯', action: { domain: 'light', service: 'turn_on', entity_id: 'light.a', reply: '好的' } },
  ], { devices: LIGHT_DEVICES });
  list[0].querySelector('.r-n').value = '客厅灯组';
  list[0].querySelector('.r-p').value = '打开客厅灯';
  stubFetch();
  await saveOneRule(0);
  const posted = postedRules()[0];
  checkEq('the pattern is still emitted for the matcher', posted.pattern, '打开客厅灯');
  checkEq('the name is emitted separately', posted.name, '客厅灯组');
  checkEq('the name did not overwrite the pattern', posted.pattern === posted.name, false);
  check('the search text still contains the pattern',
    ruleSearchText(posted).includes('打开客厅灯'), ruleSearchText(posted));
  checkEq('the DOM keeps the trigger sentence', rows()[0].querySelector('.r-p').value, '打开客厅灯');
});

// --- S8: 导入 / 导出 ------------------------------------------------------
test('export/import round-trip keeps names and gates per-rule save', async () => {
  const list = makeRulesPage([
    { name: '客厅灯组', pattern: '打开客厅灯', action: { domain: 'light', service: 'turn_on', entity_id: ['light.a', 'light.b'], reply: '好的' } },
  ], { devices: LIGHT_DEVICES });
  __lastBlob = null;
  exportRules();
  check('export produced a blob', !!__lastBlob);
  const payload = JSON.parse(await __lastBlob.text());
  checkEq('export keeps the name', payload[0].name, '客厅灯组');
  checkEq('export keeps the pattern', payload[0].pattern, '打开客厅灯');
  checkEq('export keeps the multi entity ids', payload[0].action.entity_id, ['light.a', 'light.b']);

  const imported = cloneJson(payload);
  imported[0].pattern = '导入后改的';
  await importFromJson(JSON.stringify(imported));
  checkEq('imported rows are rendered', rows().length, 1);
  checkEq('import keeps the name', rows()[0].querySelector('.r-n').value, '客厅灯组');
  checkEq('import keeps the multi selection', checkedIds(rows()[0]), ['light.a', 'light.b']);
  checkEq('import marks the page dirty', document.getElementById('rules-dirty').style.display, '');
  checkEq('import is pending (ruleBulkPending)', ruleBulkPending, true);
  stubFetch();
  const refused = await saveOneRule(0);
  checkEq('per-rule save is refused while an import is pending', refused, false);
  checkEq('the refusal does not POST', __fetchCalls.length, 0);
  check('the refusal points at 保存全部',
    __alerts.some(alert => String(alert.message).includes('保存全部')), JSON.stringify(__alerts));
  const ok = await saveRules();
  checkEq('save-all accepts the imported drafts', ok, true);
  checkEq('save-all clears the pending flag', ruleBulkPending, false);
  checkEq('save-all posted the imported rule', postedRules()[0].pattern, '导入后改的');
  checkEq('save-all kept the name', postedRules()[0].name, '客厅灯组');
  checkEq('fixture started with one row', list.length, 1);
});

// --- S9: 删除 + 设备类型切换 ---------------------------------------------
test('delete and domain switch keep other drafts', async () => {
  const list = makeRulesPage([
    { pattern: '开灯', action: { domain: 'light', service: 'turn_on', entity_id: 'light.a', reply: '好的' } },
    { pattern: '关灯', action: { domain: 'light', service: 'turn_off', entity_id: 'light.b', reply: '收到' } },
  ], { devices: LIGHT_DEVICES });
  rows()[1].querySelector('.r-p').value = '关灯(草稿)';
  rows()[1].querySelector('.r-n').value = '关灯名';
  const firstUid = ruleState[0].uid;
  deleteRule(0);
  checkEq('the row is removed', rows().length, 1);
  checkEq('the deleted uid is recorded', ruleDeleted, [firstUid]);
  checkEq('the remaining row is renumbered', rows()[0].dataset.idx, 0);
  checkEq('the remaining draft is preserved', rows()[0].querySelector('.r-p').value, '关灯(草稿)');
  checkEq('the remaining name is preserved', rows()[0].querySelector('.r-n').value, '关灯名');
  checkEq('delete marks the page dirty',
    document.getElementById('rules-dirty').style.display, '');
  stubFetch();
  const refused = await saveOneRule(0);
  checkEq('per-rule save is refused after a delete', refused, false);
  checkEq('the refusal does not POST', __fetchCalls.length, 0);

  const buttons = Array.from(rows()[0].querySelectorAll('.domain-btn'));
  const switchButton = buttons.find(button => String(button.attrs.onclick || '').includes("'climate'"));
  switchD(switchButton, 'climate');
  const row = rows()[0];
  checkEq('the domain switch updated the hidden field', row.querySelector('.r-d').value, 'climate');
  checkEq('the candidate list follows the new domain', pickerIds(row).includes('climate.ac'), true);
  checkEq('the trigger sentence survives the switch', row.querySelector('.r-p').value, '关灯(草稿)');
  checkEq('the rule name survives the switch', row.querySelector('.r-n').value, '关灯名');
  checkEq('the reply survives the switch', row.querySelector('.r-r').value, '收到');
  checkEq('the switch marks the page dirty',
    document.getElementById('rules-dirty').style.display, '');
  checkEq('the fixture is still two rows', list.length, 2);
});

// --- S10: 新增规则可以单独保存 -------------------------------------------
test('addRule creates a draft that can be saved on its own', async () => {
  const list = makeRulesPage([
    { pattern: '开灯', action: { domain: 'light', service: 'turn_on', entity_id: 'light.a', reply: '好的' } },
  ], { devices: LIGHT_DEVICES });
  addRule();
  checkEq('a second row appears', rows().length, 2);
  checkEq('the new row starts expanded', rows()[1].classList.contains('collapsed'), false);
  checkEq('the new row is a draft (base=false)', ruleState[1].base, false);
  checkEq('the new row marks the page dirty',
    document.getElementById('rules-dirty').style.display, '');
  rows()[1].querySelector('.r-p').value = '新语句';
  stubFetch();
  const ok = await saveOneRule(1);
  checkEq('the new draft can be saved on its own', ok, true);
  const posted = postedRules();
  checkEq('both rules are posted', posted.length, 2);
  checkEq('the new draft is appended', posted[1].pattern, '新语句');
  checkEq('the new draft joined the baseline', ruleBaseline.length, 2);
  checkEq('the original rule is untouched', posted[0].pattern, '开灯');
  checkEq('the fixture started with one row', list.length, 1);
});

// --- unsaved marker wiring --------------------------------------------------
test('bindRulesDirty shows the marker on input/change and binds once', () => {
  makeRulesPage([
    { pattern: '开灯', action: { domain: 'light', service: 'turn_on', entity_id: 'light.a', reply: '好的' } },
  ], { devices: LIGHT_DEVICES });
  const indicator = document.getElementById('rules-dirty');
  indicator.style.display = 'none';
  bindRulesDirty();
  const ruleList = document.getElementById('rule-list');
  checkEq('input listener bound once', (ruleList.listeners.input || []).length, 1);
  checkEq('change listener bound once', (ruleList.listeners.change || []).length, 1);
  rows()[0].querySelector('.r-p').dispatchEvent_('input');
  checkEq('typing in a rule shows the unsaved marker', indicator.style.display, '');
  checkEq('typing marks that row dirty', ruleState[0].dirty, true);
  checkEq('typing shows that row chip', chipOf(rows()[0]).style.display, '');
  setRulesDirty(false);
  rows()[0].querySelector('.r-s').dispatchEvent_('change');
  checkEq('changing a select shows the unsaved marker again', indicator.style.display, '');
  bindRulesDirty();
  checkEq('bindRulesDirty is idempotent', (ruleList.listeners.input || []).length, 1);
});

// --- runner -----------------------------------------------------------------
(async () => {
  for (const entry of tests){
    try {
      await entry.fn();
    } catch (error) {
      __failures += 1;
      __lines.push('FAIL ' + entry.name + ' (threw) :: ' + String(error && error.stack || error));
    }
  }
  console.log(__lines.join('\n'));
  console.log('TESTS: ' + tests.length + ' cases');
  console.log('RESULT: ' + __passes + ' passed, ' + __failures + ' failed');
  if (__failures > 0) process.exitCode = 1;
})();
"""


def build_harness(html: str) -> str:
    return "\n\n".join([
        DOM_STUB,
        extract_js_source(html),
        HARNESS_TESTS,
    ])


# ──────────────────────────── backend matcher checks ────────────────────────────

def _extract_intent_parser(bridge_path: Path):
    """AST-extract ``class IntentParser`` so the matcher can be exercised alone."""
    source = bridge_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(bridge_path))
    node = None
    for item in tree.body:
        if isinstance(item, ast.ClassDef) and item.name == "IntentParser":
            node = item
            break
    if node is None:
        raise ValueError(f"class IntentParser not found in {bridge_path}")
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace: dict = {"re": re}
    exec(compile(module, f"<{bridge_path.name}:IntentParser>", "exec"), namespace)
    return namespace["IntentParser"], node


def _name_reads(node: ast.AST) -> list[str]:
    """Every place the extracted class could read the rule ``name`` field."""
    hits: list[str] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and sub.value == "name":
            hits.append("string literal 'name'")
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr == "get"
            and any(isinstance(arg, ast.Constant) and arg.value == "name" for arg in sub.args)
        ):
            hits.append(".get('name')")
    return hits


def bridge_checks(bridge_path: Path) -> list[tuple[str, bool, str]]:
    """Prove the backend matcher ignores the UI-only ``name`` field."""
    entity = {"entity_id": "light.a"}
    plain_rule = {"pattern": "打开客厅灯", "action": {"domain": "light", "service": "turn_on", **entity}}
    named_rule = {
        "pattern": "打开客厅灯",
        "name": "客厅灯组开灯",
        "action": {"domain": "light", "service": "turn_on", **entity},
    }

    try:
        parser_cls, node = _extract_intent_parser(bridge_path)
    except (SyntaxError, ValueError, TypeError, OSError) as exc:
        return [("IntentParser extracted from bridge.py", False, f"{type(exc).__name__}: {exc}")]

    checks: list[tuple[str, bool, str]] = [("IntentParser extracted from bridge.py", True, "")]
    named_action = parser_cls([named_rule]).parse("打开客厅灯")
    plain_action = parser_cls([plain_rule]).parse("打开客厅灯")
    checks.append((
        "a rule carrying a name still matches its trigger sentence",
        named_action is not None,
        f"parse('打开客厅灯') -> {named_action!r}",
    ))
    checks.append((
        "the name does not change the matched action",
        named_action == plain_action,
        f"named={named_action!r} plain={plain_action!r}",
    ))
    checks.append((
        "the named rule keeps the same domain / service",
        isinstance(named_action, dict)
        and named_action.get("domain") == "light"
        and named_action.get("service") == "turn_on",
        f"parse -> {named_action!r}",
    ))
    miss = parser_cls([named_rule]).parse("今天天气不错")
    checks.append((
        "a non matching sentence still does not match",
        miss is None,
        f"parse('今天天气不错') -> {miss!r}",
    ))
    reads = _name_reads(node)
    checks.append((
        "the name field is never read by the matcher",
        not reads,
        f"name lookups: {reads}",
    ))
    return checks


# ────────────────────────────────── runner ──────────────────────────────────

def run_patch(src_html: Path, out_html: Path) -> subprocess.CompletedProcess:
    shutil.copyfile(src_html, out_html)
    return subprocess.run(
        [sys.executable, str(PATCH_SCRIPT), str(out_html)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--html", help="already built (patched) web/index.html to test")
    parser.add_argument("--pristine", help="pinned pristine upstream web/index.html; rebuild via patch_web.py and test that build")
    parser.add_argument("--node", default=None, help="node executable (default: node from PATH)")
    parser.add_argument(
        "--bridge",
        default=None,
        help=f"built bridge.py used for the IntentParser matcher check (default: {DEFAULT_BRIDGE})",
    )
    parser.add_argument("--keep", action="store_true", help="keep the temporary build/harness directory")
    args = parser.parse_args(argv)

    if not args.html and not args.pristine:
        parser.error("give --pristine <upstream html> (recommended) or --html <built html>")

    node = args.node or shutil.which("node")
    if not node:
        print("FAIL: node executable not found on PATH (pass --node <path>)")
        return 2
    if not PATCH_SCRIPT.is_file():
        print(f"FAIL: patch script not found: {PATCH_SCRIPT}")
        return 2

    failures = 0
    workdir = Path(tempfile.mkdtemp(prefix="web_patch_test_"))
    built_path = workdir / "index.html"
    try:
        if args.pristine:
            pristine = Path(args.pristine).resolve()
            if not pristine.is_file():
                print(f"FAIL: pristine file not found: {pristine}")
                return 2
            print(f"[rebuild] pristine: {pristine}")
            first = run_patch(pristine, built_path)
            print(f"[rebuild] python {PATCH_SCRIPT.name} -> exit {first.returncode}")
            if first.stdout.strip():
                print(first.stdout.rstrip())
            if first.stderr.strip():
                print(first.stderr.rstrip())
            if first.returncode != 0:
                print("FAIL: patch_web.py refused the pinned upstream HTML")
                return 1
            pristine_bytes = pristine.read_bytes()
            built_bytes = built_path.read_bytes()
            if built_bytes == pristine_bytes:
                print("FAIL: patch_web.py reported success but did not change the file")
                failures += 1
            print(f"[rebuild] bytes: pristine={len(pristine_bytes)} built={len(built_bytes)}")

            second = run_patch(pristine, workdir / "index-twice.html")
            third = subprocess.run(
                [sys.executable, str(PATCH_SCRIPT), str(workdir / "index-twice.html")],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            check_double = second.returncode == 0 and third.returncode != 0
            print(f"[rebuild] double patching refused: {'yes' if check_double else 'NO'}"
                  f" (second run exit {third.returncode})")
            if not check_double:
                print("FAIL: patch_web.py did not refuse to patch an already patched file")
                failures += 1
        else:
            built_path = Path(args.html).resolve()
            if not built_path.is_file():
                print(f"FAIL: html file not found: {built_path}")
                return 2
            print(f"[test] html: {built_path}")

        html = built_path.read_text(encoding="utf-8")

        print("\n[source invariants]")
        for name, ok, detail in source_invariants(html):
            print(f"  {'PASS' if ok else 'FAIL'} {name}" + ("" if ok else f" :: {detail}"))
            if not ok:
                failures += 1

        print("\n[js harness]")
        try:
            harness = build_harness(html)
        except ValueError as exc:
            print(f"FAIL: could not build the JS harness: {exc}")
            return 1
        harness_path = workdir / "web_patch_harness.mjs"
        harness_path.write_text(harness, encoding="utf-8")
        proc = subprocess.run(
            [node, str(harness_path), str(built_path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if proc.stdout.strip():
            print(proc.stdout.rstrip())
        if proc.stderr.strip():
            print("[node stderr]")
            print(proc.stderr.rstrip())
        if proc.returncode != 0:
            print(f"FAIL: node harness exited {proc.returncode}")
            failures += 1

        print("\n[bridge intent matcher]")
        bridge_path = Path(args.bridge).resolve() if args.bridge else DEFAULT_BRIDGE
        if not bridge_path.is_file():
            if args.bridge:
                print(f"  FAIL bridge file not found: {bridge_path}")
                failures += 1
            else:
                print(f"  SKIP bridge.py not found: {bridge_path}")
        else:
            print(f"  using {bridge_path}")
            for name, ok, detail in bridge_checks(bridge_path):
                print(f"  {'PASS' if ok else 'FAIL'} {name}" + ("" if ok else f" :: {detail}"))
                if not ok:
                    failures += 1

        if args.keep:
            print(f"\n[keep] workdir: {workdir}")
            workdir = None  # type: ignore[assignment]
        print()
        if failures:
            print(f"SUMMARY: FAIL ({failures} failing check group(s))")
            return 1
        print("SUMMARY: PASS (rebuild + source invariants + js harness + bridge matcher)")
        return 0
    finally:
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
