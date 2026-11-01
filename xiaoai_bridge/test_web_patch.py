#!/usr/bin/env python3
"""Regression tests for the xiaoai_bridge web UI patches (patch_web.py).

The interesting behaviour of the patched page lives in inline JavaScript, so
this test does two things:

1. rebuilds web/index.html from the *pinned pristine* upstream file through
   patch_web.py (so every patch anchor is proven to match exactly once), and
2. extracts the real JS functions out of the built HTML and runs them in Node
   against a tiny hand written DOM stub (no npm install, no jsdom).

Run it (Windows, python 3.11 on PATH):

    python xiaoai_bridge\\test_web_patch.py ^
        --pristine <workspace>\\tmp\\upstream-pinned\\web\\index.html

    # or test an already built file without rebuilding
    python xiaoai_bridge\\test_web_patch.py --html <path>\\web\\index.html

Exit codes: 0 = everything passed, 1 = test failure, 2 = usage / IO error.
The Node harness is written to a temporary file and executed as
``node <harness>.mjs <built html>``; it exits non-zero when an assertion fails.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PATCH_SCRIPT = Path(__file__).resolve().with_name("patch_web.py")

# Names of the JS functions that must be present in the built HTML and that the
# Node harness exercises.  Missing names are a hard failure (the patch drifted).
EXTRACT_FUNCTIONS = [
    "escHtml",
    "extraJ",
    "loadRuleDevices",
    "entityOptions",
    "ruleEntityIds",
    "ruleTargetNames",
    "ruleSearchText",
    "ruleSummaryHtml",
    "toggleRule",
    "filterRules",
    "setRulesDirty",
    "markRulesDirty",
    "bindRulesDirty",
    "syncRuleEntityOptions",
    "buildRule",
    "renderRules",
    "switchD",
    "collectRulesFromUI",
    "addRule",
    "deleteRule",
    "saveCfg",
    "saveRules",
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
    saved = html.count("saveCfg('rules')")
    return [
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
            "zero entity selection saves upstream-compatible empty string",
            "entity_id:ids.length===1?ids[0]:(ids.length?ids:'')" in html,
            "entity_id collector does not normalise the zero-selection case",
        ),
        (
            "failed save returns false and never re-renders",
            "if(ok){setRulesDirty(false);renderRules()}return ok" in html
            and "return ok}catch(e)" in html
            and "return false}}" in html,
            "saveCfg/saveRules do not gate the re-render on the save result",
        ),
        (
            "structural rule changes mark the page dirty",
            "renderRules();markRulesDirty()" in html
            and "renderRules();markRulesDirty();const last=" in html
            and "renderRules();markRulesDirty();showAlert" in html,
            "addRule/deleteRule/importRules do not flag unsaved changes",
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
        (
            "multi-select entity input kept",
            '<select class="r-e" multiple size="6">' in html,
            "entity multi-select markup missing",
        ),
        (
            "save button still wired",
            saved == 1,
            f"saveCfg('rules') occurrences: {saved}",
        ),
    ]


# ─────────────────────────────── node harness ────────────────────────────────

# A deliberately tiny DOM subset: enough for buildRule / collectRulesFromUI /
# filterRules / switchD / renderRules to run for real.  No jsdom, no npm.
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
    this.value = '';
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
  get innerHTML(){ return this.children.map(serializeNode).join(''); }
  set innerHTML(value){
    this.children = [];
    this._text = '';
    for (const node of parseHTML(String(value == null ? '' : value))) this.appendChild(node);
  }
  appendChild(node){ node.parentElement = this; this.children.push(node); return node; }
  addEventListener(type, fn){ (this.listeners[type] = this.listeners[type] || []).push(fn); }
  dispatchEvent_(type, ev){
    const list = this.listeners[type] || [];
    for (const fn of list) fn(ev || { type, target: this });
    return list.length;
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
  querySelectorAll(selector){ return queryAll(this, selector); }
  scrollIntoView(){}
  get selectedOptions(){
    if (this.tagName !== 'SELECT') return [];
    const options = this.querySelectorAll('option');
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
  querySelectorAll(selector){ return queryAll(this.documentElement, selector); }
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
  else if (name === 'multiple') el.multiple = true;
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

// ===== page globals the extracted code expects =====
const document = new Doc();
let cfg = {};
let haDevices = [];
let deviceAliases = {};
let __alerts = [];

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

function resetDom(){
  document.body.children = [];
  document.body._text = '';
  cfg = {};
  haDevices = [];
  deviceAliases = {};
  __alerts = [];
}

const realRenderRules = renderRules;
let __renderCount = 0;
renderRules = function(){
  __renderCount += 1;
  return realRenderRules();
};

const RULES_PAGE_HTML =
  '<div class="sidebar-item" data-page="rules">意图规则</div>' +
  '<div class="page" id="page-rules">' +
    '<div id="rules-alert" class="alert"></div>' +
    '<div class="btn-group">' +
      '<button class="btn btn-primary" onclick="saveRules()">保存</button>' +
      '<span id="rules-dirty" style="display:none">有未保存修改</span>' +
    '</div>' +
    '<div class="test-row"><input type="search" id="rule-filter">' +
      '<span id="rule-count"></span></div>' +
    '<div class="rule-list" id="rule-list"></div>' +
  '</div>';

function makeRulesPage(rules, options){
  const opts = options || {};
  resetDom();
  if (opts.devices) haDevices = opts.devices.slice();
  if (opts.aliases) deviceAliases = Object.assign({}, opts.aliases);
  document.body.innerHTML = RULES_PAGE_HTML + (opts.extraHtml || '');
  cfg = { intent_rules: rules || [] };
  realRenderRules();
  return document.querySelectorAll('#rule-list .rule-item');
}

function optionValues(el){
  return el.querySelectorAll('option').map(option => option.attrs.value);
}

function selectedValues(el){
  return el.selectedOptions.map(option => option.value);
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

// --- G2: entityOptions + search helpers accept string and array -------------
test('entityOptions: string and array selections', () => {
  resetDom();
  haDevices = LIGHT_DEVICES.slice();
  deviceAliases = { 'light.b': '客厅灯' };
  const one = entityOptions('light', 'light.a');
  check('entityOptions(string) marks that one selected',
    one.includes('value="light.a" selected') && !one.includes('value="light.b" selected'), one);
  const many = entityOptions('light', ['light.a', 'light.b']);
  check('entityOptions(array) marks each selected',
    many.includes('value="light.a" selected') && many.includes('value="light.b" selected'), many);
  check('entityOptions uses aliases in labels', many.includes('客厅灯 · light.b'), many);
  check('entityOptions only lists the requested domain',
    !many.includes('climate.ac') && !many.includes('switch.c'), many);
});
test('entityOptions: unknown current entity is kept as an option', () => {
  resetDom();
  haDevices = LIGHT_DEVICES.slice();
  const options = entityOptions('light', 'light.legacy');
  check('entityOptions keeps unknown current selection',
    options.includes('value="light.legacy" selected') && options.includes('（当前）'), options);
});
test('entityOptions: empty device list degrades gracefully', () => {
  resetDom();
  haDevices = [];
  check('entityOptions shows placeholder with no selection',
    entityOptions('light', '').includes('未读取到此类型实体'), entityOptions('light', ''));
  const kept = entityOptions('light', ['light.a']);
  check('entityOptions keeps saved selection without device list',
    kept.includes('value="light.a" selected'), kept);
  check('entityOptions does not show disabled placeholder when a selection exists',
    !kept.includes('未读取到此类型实体'), kept);
});
test('ruleSearchText / ruleTargetNames handle string vs array', () => {
  resetDom();
  haDevices = LIGHT_DEVICES.slice();
  deviceAliases = { 'light.b': '客厅灯' };
  checkEq('ruleTargetNames(string)', ruleTargetNames({ action: { entity_id: 'light.b' } }), ['客厅灯']);
  checkEq('ruleTargetNames(array)', ruleTargetNames({ action: { entity_id: ['light.a', 'light.b'] } }), ['A灯', '客厅灯']);
  checkEq('ruleTargetNames(unknown)', ruleTargetNames({ action: { entity_id: 'light.zzz' } }), ['light.zzz']);
  const text = ruleSearchText({ pattern: '打开客厅灯', action: { entity_id: ['light.a', 'light.b'] } });
  check('ruleSearchText includes pattern, ids and names',
    text.includes('打开客厅灯') && text.includes('light.a') && text.includes('客厅灯'), text);
  check('ruleSearchText is lower case', text === text.toLowerCase(), text);
});

// --- G3: collectRulesFromUI entity_id normalisation -------------------------
test('collectRulesFromUI: string / array / zero selection round-trip', () => {
  makeRulesPage([
    { pattern: '开灯', action: { domain: 'light', service: 'turn_on', entity_id: 'light.a', reply: '好的' } },
    { pattern: '全开', action: { domain: 'light', service: 'turn_on', entity_id: ['light.a', 'light.b'], reply: '好的' } },
    { pattern: '空选', action: { domain: 'light', service: 'turn_on', entity_id: '', reply: '好的' } },
  ], { devices: LIGHT_DEVICES });
  collectRulesFromUI();
  checkEq('collect: rule count', cfg.intent_rules.length, 3);
  checkEq('collect: single entity id stays a string', cfg.intent_rules[0].action.entity_id, 'light.a');
  checkEq('collect: multiple entity ids stay an array', cfg.intent_rules[1].action.entity_id, ['light.a', 'light.b']);
  checkEq('collect: zero selection becomes an empty string', cfg.intent_rules[2].action.entity_id, '');
  checkEq('collect: pattern preserved', cfg.intent_rules[0].pattern, '开灯');
  checkEq('collect: reply default applied', cfg.intent_rules[2].action.reply, '好的');
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

// --- G4: filterRules respects dataset.search and count ----------------------
test('filterRules: filters on dataset.search and updates the count', () => {
  const rows = makeRulesPage([
    { pattern: '打开客厅灯', action: { domain: 'light', service: 'turn_on', entity_id: 'light.b', reply: '好的' } },
    { pattern: '关闭卧室灯', action: { domain: 'light', service: 'turn_off', entity_id: 'light.a', reply: '好的' } },
    { pattern: '开空调', action: { domain: 'climate', service: 'set_temperature', entity_id: 'climate.ac', reply: '好的' } },
  ], { devices: LIGHT_DEVICES, aliases: { 'light.b': '客厅灯' } });
  checkEq('fixture built 3 rows', rows.length, 3);
  check('rows carry dataset.search', rows.every(row => typeof row.dataset.search === 'string' && row.dataset.search.length > 0));
  document.getElementById('rule-filter').value = '客厅';
  filterRules();
  checkEq('filter shows matching row', rows[0].style.display, '');
  checkEq('filter hides non matching row', rows[1].style.display, 'none');
  checkEq('filter count text', document.getElementById('rule-count').textContent, '1/3 条');
  document.getElementById('rule-filter').value = 'light.a';
  filterRules();
  checkEq('filter matches raw entity id', rows[1].style.display, '');
  checkEq('filter by entity id count', document.getElementById('rule-count').textContent, '1/3 条');
  document.getElementById('rule-filter').value = '';
  filterRules();
  check('filter cleared shows every row', rows.every(row => row.style.display === ''),
    rows.map(row => row.style.display).join(','));
  checkEq('filter cleared count', document.getElementById('rule-count').textContent, '3/3 条');
});

// --- G5: editing one rule must not corrupt the others ----------------------
test('switchD rebuilds only that rule and keeps other rules intact', () => {
  const rows = makeRulesPage([
    { pattern: '开灯', action: { domain: 'light', service: 'turn_on', entity_id: 'light.a', brightness_pct: '{0}', reply: '好的' } },
    { pattern: '关灯', action: { domain: 'light', service: 'turn_off', entity_id: 'light.b', reply: '收到' } },
  ], { devices: LIGHT_DEVICES });
  checkEq('two rows built', rows.length, 2);
  rows[0].querySelector('.r-p').value = '开灯(改)';
  const buttons = rows[0].querySelectorAll('.domain-btn');
  const climateButton = buttons.find(button => String(button.attrs.onclick || '').includes("'climate'"));
  check('climate domain button exists', !!climateButton);
  switchD(climateButton, 'climate');
  checkEq('switchD rewrites the hidden domain field', rows[0].querySelector('.r-d').value, 'climate');
  checkEq('switchD rebuilds that rule entity options for the new domain',
    optionValues(rows[0].querySelector('.r-e')), ['climate.ac']);
  checkEq('switchD rebuilds that rule service options',
    optionValues(rows[0].querySelector('.r-s')), ['set_hvac_mode', 'turn_off', 'set_temperature']);
  checkEq('switchD rebuilds that rule extra fields',
    rows[0].querySelectorAll('.r-xk').length, 3);
  checkEq('switchD keeps the in progress pattern edit', rows[0].querySelector('.r-p').value, '开灯(改)');
  checkEq('other rule entity options untouched', optionValues(rows[1].querySelector('.r-e')), ['light.a', 'light.b']);
  checkEq('other rule entity selection untouched', selectedValues(rows[1].querySelector('.r-e')), ['light.b']);
  checkEq('other rule pattern untouched', rows[1].querySelector('.r-p').value, '关灯');
  checkEq('other rule service options untouched',
    optionValues(rows[1].querySelector('.r-s')), ['turn_on', 'turn_off', 'toggle']);
  checkEq('other rule extra fields untouched', rows[1].querySelectorAll('.r-xk').length, 1);
  collectRulesFromUI();
  checkEq('collect: edited rule kept its pattern', cfg.intent_rules[0].pattern, '开灯(改)');
  checkEq('collect: edited rule uses the new domain', cfg.intent_rules[0].action.domain, 'climate');
  checkEq('collect: edited rule has no entity after the domain switch', cfg.intent_rules[0].action.entity_id, '');
  checkEq('collect: other rule untouched after collecting', cfg.intent_rules[1].action.entity_id, 'light.b');
  checkEq('collect: other rule pattern untouched after collecting', cfg.intent_rules[1].pattern, '关灯');
});

// --- F1: async device load must not clobber edits ---------------------------
test('syncRuleEntityOptions refreshes options without re-rendering the list', () => {
  haDevices = [];
  const rows = makeRulesPage([
    { pattern: '开灯', action: { domain: 'light', service: 'turn_on', entity_id: 'light.a', reply: '好的' } },
  ]);
  rows[0].querySelector('.r-p').value = '未保存的修改';
  haDevices = LIGHT_DEVICES.slice();
  const before = __renderCount;
  const touched = syncRuleEntityOptions();
  checkEq('sync touches the rendered rows', touched, 1);
  checkEq('sync does not call renderRules', __renderCount, before);
  checkEq('sync keeps the unsaved pattern edit', rows[0].querySelector('.r-p').value, '未保存的修改');
  checkEq('sync keeps the saved entity selection', selectedValues(rows[0].querySelector('.r-e')), ['light.a']);
  checkEq('sync fills in the freshly loaded device options',
    optionValues(rows[0].querySelector('.r-e')), ['light.a', 'light.b']);
  const again = syncRuleEntityOptions();
  checkEq('sync is repeatable', again, 1);
  checkEq('sync does not reset the selection on a second run',
    selectedValues(rows[0].querySelector('.r-e')), ['light.a']);
});

// --- F2: entering the rules tab must not clobber edits ---------------------
test('showPage(rules) reloads entity options without re-rendering', async () => {
  haDevices = [];
  const rows = makeRulesPage([
    { pattern: '开灯', action: { domain: 'light', service: 'turn_on', entity_id: 'light.a', reply: '好的' } },
  ]);
  rows[0].querySelector('.r-p').value = '切页前改的';
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
  checkEq('showPage(rules) keeps the unsaved edit', rows[0].querySelector('.r-p').value, '切页前改的');
  checkEq('showPage(rules) refreshes the entity options',
    optionValues(rows[0].querySelector('.r-e')), ['light.a', 'light.b']);
  checkEq('showPage(rules) keeps the entity selection',
    selectedValues(rows[0].querySelector('.r-e')), ['light.a']);
  showPage('logs');
  checkEq('showPage(logs) still fetches logs', calls.logs, 1);
  loadRuleDevices = realLoadRuleDevices;
});

// --- F3: a failed save must not look like a refresh ------------------------
test('failed save keeps the edits and the unsaved marker', async () => {
  haDevices = LIGHT_DEVICES.slice();
  const rows = makeRulesPage([
    { pattern: '开灯', action: { domain: 'light', service: 'turn_on', entity_id: 'light.a', reply: '好的' } },
  ]);
  rows[0].querySelector('.r-p').value = '改到一半';
  markRulesDirty();
  checkEq('dirty marker is visible after an edit', document.getElementById('rules-dirty').style.display, '');
  const realSaveCfg = saveCfg;
  saveCfg = async function(){ return false; };
  const before = __renderCount;
  const ok = await saveRules();
  checkEq('saveRules reports the failure', ok, false);
  checkEq('failed save does not re-render (no fake refresh)', __renderCount, before);
  checkEq('failed save keeps the edited text in the DOM', rows[0].querySelector('.r-p').value, '改到一半');
  checkEq('failed save keeps the unsaved marker', document.getElementById('rules-dirty').style.display, '');
  checkEq('failed save keeps the in-memory edit', cfg.intent_rules[0].pattern, '改到一半');
  saveCfg = realSaveCfg;
});
test('successful save re-renders once and clears the unsaved marker', async () => {
  haDevices = LIGHT_DEVICES.slice();
  const rows = makeRulesPage([
    { pattern: '开灯', action: { domain: 'light', service: 'turn_on', entity_id: 'light.a', reply: '好的' } },
  ]);
  rows[0].querySelector('.r-p').value = '改到一半';
  markRulesDirty();
  const realSaveCfg = saveCfg;
  saveCfg = async function(){ return true; };
  const before = __renderCount;
  const ok = await saveRules();
  checkEq('saveRules reports success', ok, true);
  checkEq('successful save re-renders exactly once', __renderCount - before, 1);
  checkEq('successful save clears the unsaved marker',
    document.getElementById('rules-dirty').style.display, 'none');
  checkEq('successful save renders the collected edit',
    document.querySelector('#rule-list .rule-item .r-p').value, '改到一半');
  saveCfg = realSaveCfg;
});

// --- unsaved marker wiring --------------------------------------------------
test('bindRulesDirty shows the marker on input/change and binds once', () => {
  makeRulesPage([
    { pattern: '开灯', action: { domain: 'light', service: 'turn_on', entity_id: 'light.a', reply: '好的' } },
  ]);
  const indicator = document.getElementById('rules-dirty');
  indicator.style.display = 'none';
  bindRulesDirty();
  const list = document.getElementById('rule-list');
  checkEq('input listener bound once', (list.listeners.input || []).length, 1);
  checkEq('change listener bound once', (list.listeners.change || []).length, 1);
  list.dispatchEvent_('input');
  checkEq('typing in a rule shows the unsaved marker', indicator.style.display, '');
  setRulesDirty(false);
  list.dispatchEvent_('change');
  checkEq('changing a select shows the unsaved marker again', indicator.style.display, '');
  bindRulesDirty();
  checkEq('bindRulesDirty is idempotent', (list.listeners.input || []).length, 1);
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

        if args.keep:
            print(f"\n[keep] workdir: {workdir}")
            workdir = None  # type: ignore[assignment]
        print()
        if failures:
            print(f"SUMMARY: FAIL ({failures} failing check group(s))")
            return 1
        print("SUMMARY: PASS (rebuild + source invariants + js harness)")
        return 0
    finally:
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
