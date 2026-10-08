#!/usr/bin/env python3
"""Personal Pinduoduo MCP using the existing Brave Kimi WebBridge session.

PDD extractors and pure parsers derive from goesByhc/cn-scraper-mcp
f878f78f844ea55b48bdc39166ce96a07c7b8939, MIT, Copyright (c) 2026 goes_by.
See docs/UPSTREAM-LICENSE.txt. No upstream authentication/CDP code is imported.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import copy
import hashlib
import json
import importlib.util
import math
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from mcp.server.fastmcp import FastMCP

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
LOGIN_URL = "https://mobile.yangkeduo.com/login.html"
OFFICIAL_HOST = "mobile.yangkeduo.com"
BRIDGE_URL = os.environ.get("WEBBRIDGE_URL", os.environ.get("KIMI_WEBBRIDGE_URL", "http://127.0.0.1:10086/command"))
SESSION = os.environ.get("WEBBRIDGE_SESSION", os.environ.get("PDD_SESSION", "commerce-mcp-pinduoduo"))
GROUP_TITLE = os.environ.get("PDD_GROUP_TITLE", "拼多多MCP")
MCP_HOST = os.environ.get("MCP_HOST", os.environ.get("PDD_MCP_HOST", "127.0.0.1"))
MCP_PORT = int(os.environ.get("MCP_PORT", os.environ.get("PDD_MCP_PORT", "8841")))
ERROR_MESSAGES = {
    "unsupported": "拼多多当前网页未暴露可安全操作的原生能力；请本人使用官方 App。",
    "action_unconfirmed": "点击后页面未确认收藏状态，停止操作；不自动重试。",
    "ambiguous_control": "原生商品控件匹配不唯一，未点击。",
    "unsafe_control": "目标或其祖先含购买/结算控件，未点击。",
    "checkout_page_reached": "页面进入下单/结算范围，已停止，不进行后续操作。",
    "invalid_input": "仅接受拼多多官方商品 URL 或数字 goods_id；搜索关键词需为 1–100 字，limit 为 1–10。",
    "bridge_unavailable": "Kimi WebBridge 当前不可用；请确认日常 Brave 扩展连接，不自动切换浏览器环境。",
    "bridge_error": "Kimi WebBridge 未完成本次只读操作；不自动重试，不回显底层认证信息。",
    "session_conflict": "固定任务 session 内存在借用页、其他标签组或多个页面；已停止，未操作用户其他页。",
    "login_required": "当前页面要求登录，请本人在日常 Brave 的拼多多MCP任务页完成认证。",
    "risk_control": "拼多多返回验证、系统繁忙或访问限制；已停止自动查询，不重试或重置环境。",
    "not_supported": "当前普通桌面网页提示不支持该访问方式；不修改浏览器身份或伪造移动环境。",
    "unexpected_redirect": "任务页跳转到允许范围之外，已停止读取。",
    "parse_error": "页面未提供可确认的商品结果；不能据此判定为空或读取成功。",
    "cooldown": "商品导航间隔不足 30 秒；请按 retry_after_s 稍后自行调用，服务不自动等待或重试。",
}



def tool_result(payload: dict[str, Any], fields: dict[str, str] | None = None) -> dict[str, Any]:
    """Minimal result contract, retaining the existing top-level payload."""
    payload = {k: v for k, v in payload.items() if k not in {"ok", "status", "data", "error_code", "fields"}}
    code = payload.get("code", payload.get("page_issue"))
    status = {"login_required": "auth_required", "risk_control": "risk_control", "cooldown": "cooldown"}.get(code)
    if not status:
        state = payload.get("state")
        status = "error" if code or state == "error" else "not_found" if state == "not_found" else "partial" if state == "partial" or fields and "missing" in fields.values() else "ok"
    output = {**payload, "ok": status in {"ok", "partial"}, "status": status, "data": payload}
    if code:
        output["error_code"] = code
    if fields is not None:
        output["fields"] = fields
    return output


def image_url(value: Any) -> str | None:
    """Only known product image resources, never HTML or navigation URLs."""
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
            return None
        if re.search(r"\.html?", parsed.path, re.I) or not re.search(r"\.(?:jpe?g|png|webp|gif|avif)(?:$|!)", parsed.path, re.I):
            return None
        if not re.search(r"(?:^|\.)(?:pddpic\.com|yangkeduo\.com|pinduoduo\.com)$", parsed.hostname or ""):
            return None
        return parsed.scheme + "://" + parsed.netloc + parsed.path
    except ValueError:
        return None


def _is_official_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme == "https" and parsed.hostname == OFFICIAL_HOST
            and parsed.username is None and parsed.password is None
            and parsed.port in (None, 443)
        )
    except ValueError:
        return False


def validate_product_input(value: str) -> str:
    """Return a goods ID and discard share parameters before any browser work."""
    if not isinstance(value, str) or value != value.strip():
        raise ValueError("invalid product")
    if re.fullmatch(r"[0-9]{1,20}", value):
        return value
    if not _is_official_url(value):
        raise ValueError("not an official product URL")
    parsed = urlsplit(value)
    if parsed.path not in {"/goods2.html", "/goods.html"}:
        raise ValueError("not a product page")
    ids = parse_qs(parsed.query, keep_blank_values=True).get("goods_id", [])
    if len(ids) != 1 or not re.fullmatch(r"[0-9]{1,20}", ids[0]):
        raise ValueError("invalid goods ID")
    return ids[0]


def _load_pure_upstream() -> dict[str, Any]:
    """Compile only literal constants, exceptions, and side-effect-free parsers.

    Deliberately skip all module imports, the PDDEngine constructor, browser
    lifecycle methods, and cookie access. This keeps the fixed reference intact
    without executing its multi-platform authentication module.
    """
    source = WORKSPACE_ROOT / "src/pdd_parser.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    namespace: dict[str, Any] = {"Any": Any}
    selected: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {"SEARCH_EXTRACT_JS", "DETAIL_EXTRACT_JS"}:
                    namespace[target.id] = ast.literal_eval(node.value)
        elif isinstance(node, ast.ClassDef) and node.name in {"PDDRateLimitError", "PDDAuthError"}:
            selected.append(copy.deepcopy(node))
        elif isinstance(node, ast.ClassDef) and node.name == "PDDEngine":
            for method in node.body:
                if isinstance(method, ast.FunctionDef) and method.name == "_process_search_result":
                    selected.append(copy.deepcopy(method))
                elif isinstance(method, ast.FunctionDef) and method.name == "product_detail":
                    for index, statement in enumerate(method.body):
                        test = statement.test if isinstance(statement, ast.If) else None
                        if (isinstance(test, ast.Call) and isinstance(test.func, ast.Attribute)
                                and isinstance(test.func.value, ast.Name) and test.func.value.id == "raw_result"
                                and test.args and isinstance(test.args[0], ast.Constant)
                                and test.args[0].value == "loginGated"):
                            detail = ast.parse("def _process_detail_result(self, raw_result, goods_id, detail_url):\n    pass\n").body[0]
                            detail.body = copy.deepcopy(method.body[index:])
                            selected.append(detail)
                            break
    module = ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[]))
    exec(compile(module, str(source), "exec"), namespace)
    required = {"SEARCH_EXTRACT_JS", "DETAIL_EXTRACT_JS", "_process_search_result", "_process_detail_result", "PDDRateLimitError", "PDDAuthError"}
    if not required.issubset(namespace):
        raise RuntimeError("Fixed PDD reference does not match the adapter contract")
    return namespace


UPSTREAM = _load_pure_upstream()
PARSER_CONTEXT = SimpleNamespace(cookies_path="[日常 Brave 任务页，不读取 Cookie]")


STATUS_JS = r"""(() => {
  const text = (document.body && document.body.innerText || '').slice(0, 10000);
  const path = location.pathname.toLowerCase();
  const visible = el => {
    if (!el.getClientRects().length) return false;
    const style = window.getComputedStyle(el);
    return style.display !== 'none' && style.visibility !== 'hidden' && style.visibility !== 'collapse';
  };
  const authInputPresent = [...document.querySelectorAll('input[type="password"], input[autocomplete="one-time-code"], input[autocomplete="current-password"], input[autocomplete="new-password"]')]
    .some(el => !el.disabled && visible(el));
  const loginOverlayPresent = [...document.querySelectorAll('[role="dialog"], [aria-modal="true"], [class*="login-modal"], [class*="login-mask"], [class*="login-popup"]')]
    .some(el => visible(el) && /登录|验证码|手机号/.test(el.textContent || ''));
  return JSON.stringify({
    localPddStatus: true,
    url: location.origin + location.pathname,
    title: (document.title || '').slice(0, 200),
    loginHint: /个人中心|我的订单|退出登录|已登录/.test(text),
    loginVerified: [...document.querySelectorAll('button, a, [role="button"]')].some(el => visible(el) && /^(退出登录|退出账号|退出账户)$/.test((el.textContent || '').trim())) && !authInputPresent && !loginOverlayPresent,
    authInputPresent: authInputPresent,
    loginOverlayPresent: loginOverlayPresent,
    loginRequired: authInputPresent || loginOverlayPresent || /login/.test(path) || /请先登录|登录后查看|手机号登录|请输入验证码/.test(text),
    riskControl: /系统繁忙|网络异常|安全验证|请完成验证|异常访问|访问过于频繁|访问频繁|滑块验证/.test(text) || /captcha|verify|risk/.test(path),
    notSupported: /仅支持手机|请使用手机浏览器|不支持当前浏览器|请在手机端打开|暂不支持电脑|请使用拼多多APP访问/.test(text),
    smsSendFailed: /发送失败/.test(text)
  });
})()"""
SEARCH_EXTRACT_JS = r"""(() => {
  const root = window.rawData;
  const data = root && root.stores && root.stores.store && root.stores.store.data;
  const ssr = data && data.ssrListData;
  const base = {url:window.location.origin + window.location.pathname,
    title:(document.title || '').slice(0,200),ogTitle:'',pageText:'',items:[],itemCount:0};
  if ((data && (data.isRisk === true || data.isBlack === true)) ||
      (ssr && (ssr.isRisk === true || ssr.isBlack === true)))
    return JSON.stringify({...base,riskControl:true,rateLimited:true});
  if (ssr && Array.isArray(ssr.list)) {
    if (data.hasError === true || ssr.hasError === true)
      return JSON.stringify({...base,hasError:true});
    const items = [];
    for (const goods of ssr.list) {
      if (!goods || typeof goods !== 'object') continue;
      const id = goods.goodsID;
      if (typeof id !== 'string' && !(typeof id === 'number' && Number.isSafeInteger(id))) continue;
      const goodsId = String(id);
      if (!/^[0-9]{1,20}$/.test(goodsId) || typeof goods.goodsName !== 'string' || !goods.goodsName.trim()) continue;
      const sourcePrice = goods.price;
      const validPrice = typeof sourcePrice === 'number' && Number.isFinite(sourcePrice) && Number.isInteger(sourcePrice) && sourcePrice >= 0;
      const salesTip = typeof goods.salesTip === 'string' ? goods.salesTip.slice(0,200) : '';
      const shopCumulative = /^本店已拼/.test(salesTip);
      items.push({goodsId:goodsId,name:goods.goodsName,price:validPrice ? sourcePrice / 100 : null,
        url:'https://mobile.yangkeduo.com/goods2.html?goods_id=' + goodsId,
        data_source:'ssrListData.list',price_source:'ssrListData.list.price',
        price_unit:'yuan',source_price_unit:'fen',
        price_status:validPrice ? 'ok' : sourcePrice == null ? 'not_exposed' : 'unparseable',
        sold:null,sold_status:shopCumulative ? 'not_product_sales' : 'unknown',
        sales_tip:salesTip,sales_tip_scope:shopCumulative ? 'shop_cumulative' : 'unknown'});
    }
    return JSON.stringify({...base,itemCount:items.length,items:items});
  }
  return __DOM_FALLBACK__;
})()""".replace("__DOM_FALLBACK__", UPSTREAM["SEARCH_EXTRACT_JS"])
DETAIL_EXTRACT_JS = r"""(() => {
  const status = JSON.parse(__STATUS_SCRIPT__);
  if (status.loginRequired || status.riskControl || status.notSupported)
    return JSON.stringify({...status,loginGated:status.loginRequired});
  const text = (document.body && document.body.innerText || '').slice(0,10000);
  const meta = selector => {
    const node = document.querySelector(selector);
    return node ? (node.getAttribute('content') || '').trim() : '';
  };
  const clearName = name => name.length >= 4 && name.length <= 300 &&
    !/拼多多|商城|下载|更多实惠|一起拼|正品保证|百亿补贴/.test(name);
  let name = null, nameSource = null;
  for (const selector of ['meta[property="og:description"]','meta[name="description"]','meta[property="og:title"]']) {
    const candidate = meta(selector);
    if (clearName(candidate)) {name=candidate;nameSource=selector.includes('description')?'meta_description':'meta_title';break;}
  }
  const promotion = text.match(/大促价\s*[¥￥]\s*([0-9,]+(?:\s*\.\s*[0-9]{1,2})?)/);
  const currency = promotion || text.match(/[¥￥]\s*([0-9,]+(?:\s*\.\s*[0-9]{1,2})?)/);
  const parsed = currency ? Number(currency[1].replace(/[\s,]/g,'')) : null;
  const price = parsed !== null && Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
  let imageUrl = null, largest = 0;
  for (const img of document.querySelectorAll('img')) {
    if (!img.getClientRects().length) continue;
    const style = window.getComputedStyle(img), rect = img.getBoundingClientRect();
    if (style.display==='none' || style.visibility==='hidden' || rect.width<80 || rect.height<80) continue;
    try {
      const image = new URL(img.currentSrc || img.src, window.location.href);
      if (!/^https?:$/.test(image.protocol) || !/(^|\.)(pddpic\.com|yangkeduo\.com|pinduoduo\.com)$/.test(image.hostname) || /\.html?/i.test(image.pathname) || !/\.(jpe?g|png|webp|gif|avif)($|!)/i.test(image.pathname)) continue;
      if (rect.width*rect.height>largest) {largest=rect.width*rect.height;imageUrl=image.origin+image.pathname;}
    } catch (_) {}
  }
  let specs = [], skus = [], goods = null;
  // Whitelist public product subtrees; never enumerate authentication/context state.
  try {
    const root = window.rawData;
    const data = root && root.stores && root.stores.store && root.stores.store.data;
    const init = root && root.store && root.store.initDataObj;
    const candidates = [init && init.goods, data && data.goods, data && data.goodsData,
      root && root.goods, root && root.goodsData];
    const expected = new URL(location.href).searchParams.get('goods_id');
    for (const candidate of candidates) {
      if (!candidate || typeof candidate !== 'object') continue;
      const id = String(candidate.goodsID || candidate.goodsId || candidate.goods_id || '');
      if (id && id !== expected) continue;
      goods = candidate; break;
    }
    if (goods) {
      const publicName = goods.goodsName || goods.goods_name;
      if (typeof publicName === 'string' && clearName(publicName)) {name=publicName;nameSource='loaded_ssr_goods';}
      const rows = goods.skus || goods.skuList || goods.sku_list;
      if (Array.isArray(rows)) for (const sku of rows.slice(0,100)) {
        if (!sku || typeof sku !== 'object') continue;
        const values = [];
        if (Array.isArray(sku.specs)) for (const spec of sku.specs.slice(0,20)) {
          if (!spec || typeof spec !== 'object') continue;
          const key = spec.spec_key || spec.specKey || spec.spec_name || spec.specName;
          const value = spec.spec_value || spec.specValue || spec.spec_value_name || spec.specValueName;
          if (typeof key === 'string' && typeof value === 'string' && key.length<100 && value.length<200)
            values.push(key+'：'+value);
        }
        for (const value of values) if (!specs.includes(value)) specs.push(value);
        const id = String(sku.skuId || sku.sku_id || '');
        if (/^[0-9]{1,20}$/.test(id)) skus.push({sku_id:id,specs:values});
      }
    }
  } catch (_) {} // Absent/versioned SSR state is missing, never invented.
  const soldOut = /商品已售罄|已卖光/.test(text);
  const notFound = /商品已下架|商品不存在|该商品不存在/.test(text);
  return JSON.stringify({...status,ogTitle:meta('meta[property="og:title"]'),loginGated:false,
    observed_goods_id:new URL(location.href).searchParams.getAll('goods_id').length===1?new URL(location.href).searchParams.get('goods_id'):null,
    name:name,name_status:name?'ok':'not_exposed',name_source:nameSource,
    price:price,price_status:price===null?'not_exposed':'ok',price_unit:'yuan',
    price_source:price===null?null:promotion?'visible_promotion_text':'visible_currency_text',
    image_url:imageUrl,image_status:imageUrl?'available':'not_exposed',
    origPrice:null,sales:null,sales_status:'not_exposed',specs:specs,skus:skus,specs_status:specs.length?'ok':'missing',
    soldOut:soldOut,pageText:notFound?'商品已下架':''});
})()""".replace("__STATUS_SCRIPT__", STATUS_JS)

FAVORITE_JS = r"""(() => {
  const args = __ACTION_ARGS__;
  const status = JSON.parse(__STATUS_SCRIPT__);
  const base = {url:location.origin+location.pathname, ...status};
  if (status.loginRequired || status.riskControl || status.notSupported) return JSON.stringify(base);
  const actual = new URL(location.href);
  if (actual.protocol !== 'https:' || actual.hostname !== 'mobile.yangkeduo.com' ||
      actual.username || actual.password || actual.port && actual.port!=='443' ||
      !['/goods2.html','/goods.html'].includes(actual.pathname) ||
      actual.searchParams.getAll('goods_id').length!==1 || actual.searchParams.get('goods_id')!==args.goods_id)
    return JSON.stringify({...base,issue:'unexpected_redirect'});
  const visible = el => {
    if (!el.getClientRects().length) return false;
    const css = window.getComputedStyle(el);
    return css.display!=='none' && css.visibility!=='hidden' && css.visibility!=='collapse';
  };
  const label = el => (el.getAttribute('aria-label') || el.textContent || '').trim();
  const scope = document;
  const buttons = [...scope.querySelectorAll('button, a, [role="button"], [aria-label]')].filter(el=>visible(el) && /^(收藏|收藏商品|已收藏|取消收藏)$/.test(label(el)));
  if (buttons.length!==1) return JSON.stringify({...base,issue:buttons.length>1?'ambiguous_control':'unsupported'});
  const target = buttons[0];
  const forbidden = /立即购买|购买|结算|提交订单|付款|支付|领券购买|checkout|buy|confirm_order|order/i;
  for (let node=target; node; node=node.parentElement) {
    const interactive = node.matches && node.matches('button, a, [role="button"]');
    const ownText = node===target || interactive || !node.childNodes ? (node.textContent||'') : [...node.childNodes].filter(child=>child.nodeType===3).map(child=>child.textContent||'').join(' ');
    if (forbidden.test(ownText+' '+(node.getAttribute('href')||'')+' '+(node.getAttribute('action')||'')+' '+(node.getAttribute('aria-label')||'')))
      return JSON.stringify({...base,issue:'unsafe_control'});
  }
  const pressed = target.getAttribute('aria-pressed');
  const current = pressed==='true' || /^(已收藏|取消收藏)$/.test(label(target)) ? true : pressed==='false' || /^(收藏|收藏商品)$/.test(label(target)) ? false : null;
  if (current===null || pressed==='true' && /^(收藏|收藏商品)$/.test(label(target)) || pressed==='false' && /^(已收藏|取消收藏)$/.test(label(target)))
    return JSON.stringify({...base,issue:'ambiguous_control'});
  if (current === args.wanted) return JSON.stringify({...base,confirmed:true,favorited:current,changed:false});
  if (!args.click) return JSON.stringify({...base,confirmed:false,favorited:current,can_click:true});
  target.click();
  return JSON.stringify({...base,clicked:true});
})()""".replace("__STATUS_SCRIPT__", STATUS_JS)


def favorite_script(goods_id: str, wanted: bool, click: bool = False) -> str:
    if not re.fullmatch(r"[0-9]{1,20}", goods_id) or not isinstance(wanted, bool) or not isinstance(click, bool):
        raise ValueError("invalid favorite action")
    return FAVORITE_JS.replace("__ACTION_ARGS__", json.dumps({"goods_id":goods_id,"wanted":wanted,"click":click}, separators=(",", ":")))


def is_favorite_script(code: Any) -> bool:
    if not isinstance(code, str) or len(code)>30000:
        return False
    match = re.search(r'const args = (\{"goods_id":"[0-9]{1,20}","wanted":(?:true|false),"click":(?:true|false)\});', code)
    if not match:
        return False
    args = json.loads(match.group(1))
    return code == favorite_script(args["goods_id"], args["wanted"], args["click"])


FIXED_EVALUATIONS = {STATUS_JS, SEARCH_EXTRACT_JS, DETAIL_EXTRACT_JS}

merchant_spec = importlib.util.spec_from_file_location("pdd_merchant_dom", WORKSPACE_ROOT / "src/merchant_dom.py")
merchant_module = importlib.util.module_from_spec(merchant_spec)
merchant_spec.loader.exec_module(merchant_module)


def merchant_script(goods_id: str, phase="entry", text="", click=False, mall_sn="") -> str:
    return merchant_module.build_script(goods_id, STATUS_JS, phase, text, click, mall_sn)


def redact_text(value: Any, limit: int = 200) -> str:
    text = str(value or "")[:limit]
    text = re.sub(r"(?<!\d)1[3-9][0-9]{9}(?!\d)", "[redacted]", text)
    return re.sub(r"(?i)(token|secret|authorization|验证码|手机号|code|OTP)\s*(?:[:=]\s*|\s+)[^\s,;]+", r"\1=[redacted]", text)


def safe_path(value: str) -> str:
    try:
        path = urlsplit(value).path
    except ValueError:
        return "/[redacted]"
    clean = []
    previous = ""
    for segment in path.split("/"):
        redact = (
            previous.lower() in {"phone", "mobile", "token", "code", "id", "user", "session", "auth"}
            or not re.fullmatch(r"[A-Za-z0-9._-]{0,48}", segment)
            or bool(re.search(r"[0-9]{6,}|[a-fA-F0-9]{16,}", segment))
            or len(segment) > 32
        )
        clean.append("[redacted]" if redact else segment)
        previous = segment
    return "/".join(clean)[:300] or "/"


def safe_page_url(value: str) -> str:
    return "https://mobile.yangkeduo.com" + safe_path(value) if _is_official_url(value) else "[outside-allowed-site]"


class BridgeError(Exception):
    """Only a local safe error category, never the raw bridge message."""
    def __init__(self, code: str = "bridge_error"):
        self.code = code if code in ERROR_MESSAGES else "bridge_error"
        super().__init__(self.code)


class BridgeClient:
    def __init__(self, transport=None):
        self._client = httpx.AsyncClient(timeout=20, trust_env=False, transport=transport)

    @staticmethod
    def _validate_command(action: str, args: dict[str, Any]):
        if action in {"list_tabs", "snapshot", "close_session"} and not args:
            return
        if action == "network" and args == {"cmd": "list"}:
            return
        if action == "evaluate" and set(args) == {"code"} and (args["code"] in FIXED_EVALUATIONS or is_favorite_script(args["code"]) or merchant_module.matches_script(args["code"], STATUS_JS)):
            return
        if action == "find_tab" and set(args) == {"url", "active"} and args["active"] is False and _is_official_url(args["url"]):
            return
        if action == "navigate" and set(args) == {"url", "newTab", "group_title"}:
            if (_is_official_url(args["url"]) and isinstance(args["newTab"], bool)
                    and args["group_title"] == GROUP_TITLE and urlsplit(args["url"]).path in {"/login.html", "/search_result.html", "/goods2.html", "/goods.html"}):
                return
        raise ValueError("Bridge command is outside the fixed personal-shopping task contract")

    async def command(self, action: str, args: dict[str, Any] | None = None):
        args = args or {}
        self._validate_command(action, args)
        try:
            response = await self._client.post(BRIDGE_URL, json={"action": action, "args": args, "session": SESSION})
            response.raise_for_status()
            envelope = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BridgeError("bridge_unavailable") from exc
        if not isinstance(envelope, dict) or envelope.get("ok") is not True:
            raise BridgeError("bridge_error")
        return envelope.get("data")

    async def aclose(self):
        await self._client.aclose()


def decode_evaluation(data: Any) -> dict[str, Any]:
    value = data.get("value") if isinstance(data, dict) else None
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise BridgeError("parse_error")
    return value


class LocalPinduoduoAdapter:
    def __init__(self, root: Path = WORKSPACE_ROOT, bridge=None, clock=None):
        # root identifies this independent service; no browser profile is created.
        self.root = Path(root).resolve()
        self.bridge = bridge if bridge is not None else BridgeClient()
        self._lock = asyncio.Lock()
        self.data_dir = Path(os.environ.get("MCP_DATA_DIR", os.environ.get("PDD_DATA_DIR", str(self.root / "runtime"))))
        self.watchlist = watchlist_module.Watchlist("pinduoduo", canonical_product_url, self.data_dir)
        self.notifications = notifications_module.Notifications(self.watchlist, self.product)
        self.pending_cart = pending_cart_module.PendingCart(self.watchlist, self.product, canonical_product_url)
        self._risk_file = self.data_dir / "risk-lock.json"
        self.__risk_blocked = self._risk_file.exists()
        self._clock = clock if clock is not None else time.monotonic
        self._last_business_navigation = None

    def _cache_quote(self, output):
        try:
            self.watchlist.observe(output)
        except Exception:
            output["local_quote_cache"] = "unavailable"
        return output

    @property
    def _risk_blocked(self):
        return self.__risk_blocked

    @_risk_blocked.setter
    def _risk_blocked(self, blocked):
        self.__risk_blocked = bool(blocked)
        if blocked:
            self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = self._risk_file.with_suffix(".tmp")
            temporary.write_text('{"risk_control":true}\n', encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(self._risk_file)
        else:
            self._risk_file.unlink(missing_ok=True)

    def _base_status(self, running: bool = False) -> dict[str, Any]:
        return {
            "platform": "pinduoduo", "backend": "kimi-webbridge", "network": "existing-browser",
            "session": SESSION, "group_title": GROUP_TITLE, "profile_path": "existing-brave-profile",
            "browser_running": running, "login_verified": False, "login_hint": False,
            "risk_control_detected": self._risk_blocked,
            "capabilities": {"cart": "unsupported", "pending_cart": "mcp_managed_local", "favorite": "implemented_unverified", "detail": "implemented", "search": "implemented", "merchant_send": "implemented_unverified", "merchant_messages": "implemented_unverified"},
        }

    def _error(self, code: str) -> dict[str, Any]:
        return tool_result({**self._base_status(), "state": "error", "code": code, "error": ERROR_MESSAGES[code]})

    async def _task_tab(self):
        data = await self.bridge.command("list_tabs")
        tabs = data.get("tabs") if isinstance(data, dict) else None
        if not isinstance(tabs, list):
            raise BridgeError()
        if not tabs:
            return None
        if (len(tabs) != 1 or not isinstance(tabs[0], dict) or tabs[0].get("borrowed")
                or tabs[0].get("groupTitle") != GROUP_TITLE):
            raise BridgeError("session_conflict")
        return tabs[0]

    async def _select_task_tab(self, tab):
        if not _is_official_url(str(tab.get("url", ""))):
            raise BridgeError("unexpected_redirect")
        selected = await self.bridge.command("find_tab", {"url": tab["url"], "active": False})
        if (not isinstance(selected, dict) or selected.get("success") is False
                or selected.get("borrowed") or selected.get("tabId") != tab.get("tabId")):
            raise BridgeError("session_conflict")

    async def _navigate(self, url: str):
        if not _is_official_url(url):
            raise BridgeError("invalid_input")
        tab = await self._task_tab()
        if tab is not None:
            await self._select_task_tab(tab)
        data = await self.bridge.command("navigate", {"url": url, "newTab": tab is None, "group_title": GROUP_TITLE})
        if not isinstance(data, dict) or data.get("success") is False:
            raise BridgeError()
        if not _is_official_url(str(data.get("url", ""))):
            raise BridgeError("unexpected_redirect")

    def _page_issue(self, raw: dict[str, Any], observed_url: str) -> str | None:
        if not _is_official_url(observed_url):
            return "unexpected_redirect"
        path = urlsplit(observed_url).path.lower()
        text = str(raw.get("pageText", ""))
        if (raw.get("riskControl") or raw.get("rateLimited") or any(word in text for word in (
                "系统繁忙", "网络异常", "安全验证", "请完成验证", "异常访问", "访问过于频繁", "滑块验证"))
                or any(word in path for word in ("captcha", "verify", "risk"))):
            self._risk_blocked = True
            return "risk_control"
        if raw.get("notSupported"):
            return "not_supported"
        if (raw.get("authInputPresent") or raw.get("loginOverlayPresent") or raw.get("loginRequired") or "login" in path or
                (not raw.get("itemCount") and raw.get("price") is None and any(word in text for word in ("请先登录", "登录后查看", "手机号登录", "请输入验证码")))):
            return "login_required"
        return None

    async def _observe(self, tab):
        await self._select_task_tab(tab)
        raw = decode_evaluation(await self.bridge.command("evaluate", {"code": STATUS_JS}))
        issue = self._page_issue(raw, str(raw.get("url", "")))
        return raw, issue

    async def _network_diagnostics(self):
        result = {"state": "not_captured", "failed_requests": [], "reason_known": False,
                  "message": "仅已有失败请求路径及 HTTP/网络码；不读取请求头、正文、手机号或 token，无法据此确定短信业务拒绝原因。"}
        try:
            data = await self.bridge.command("network", {"cmd": "list"})
        except Exception:
            return result
        if not isinstance(data, dict) or not isinstance(data.get("requests"), list):
            return result
        result["state"] = "captured"
        failed = []
        for request in data["requests"][-100:]:
            if not isinstance(request, dict) or not _is_official_url(str(request.get("url", ""))):
                continue
            status = request.get("status")
            error = request.get("errorCode", request.get("error", ""))
            if not ((isinstance(status, int) and (status >= 400 or status == 0)) or error):
                continue
            row = {"path": safe_path(request["url"])}
            if isinstance(status, int):
                row["http_status"] = status
            if error:
                row["error_code"] = error if isinstance(error, str) and re.fullmatch(r"(?:net::)?ERR_[A-Z0-9_]{1,64}", error) else "unknown_error"
            if row not in failed:
                failed.append(row)
        result["failed_requests"] = failed[:20]
        return result

    async def _status(self, tab=None, allow_clear=False):
        if tab is None:
            tab = await self._task_tab()
        if tab is None:
            return tool_result({**self._base_status(), "state": "no_task_page", "page_issue": "risk_control" if self._risk_blocked else None, "message": "本 session 尚无任务页；login 工具将只创建自己的拼多多MCP页。"})
        raw, issue = await self._observe(tab)
        if issue == "unexpected_redirect":
            raise BridgeError(issue)
        confirmed = raw.get("loginVerified") is True and issue is None
        if allow_clear and confirmed:
            self._risk_blocked = False
        if self._risk_blocked and issue is None:
            issue = "risk_control"
        return tool_result({
            **self._base_status(True), "login_verified": confirmed, "state": "page_attention" if issue else "ready", "page_issue": issue, "page_url": safe_page_url(str(raw.get("url", ""))),
            "page_title": redact_text(raw.get("title", "")), "login_hint": bool(raw.get("loginHint")),
            "login_required_hint": issue == "login_required", "not_supported_hint": issue == "not_supported",
            "sms_send_failed_hint": bool(raw.get("smsSendFailed")),
            "network_diagnostics": await self._network_diagnostics(),
            "message": "页面迹象只供参考；本人认证，商品搜索与详情读取需单独验收。",
        })

    async def status(self):
        async with self._lock:
            try:
                return await self._status(allow_clear=True)
            except BridgeError as exc:
                return self._error(exc.code)
            except Exception:
                return self._error("bridge_error")

    async def login(self):
        async with self._lock:
            try:
                tab = await self._task_tab()
                reused = tab is not None
                if not reused:
                    if self._risk_blocked:
                        return self._error("risk_control")
                    await self._navigate(LOGIN_URL)
                status = await self._status(tab)
                return tool_result({**status, "state": "login_reused" if reused else "login_opened", "login_url": LOGIN_URL,
                        "message": "已保留当前任务页，认证由本人操作。" if reused else "请本人在日常 Brave 的拼多多MCP标签组完成登录；服务不填写手机号、不发送短信、不处理验证码。"})
            except BridgeError as exc:
                return self._error(exc.code)
            except Exception:
                return self._error("bridge_error")

    async def _read(self, url: str, extractor: str, reuse_product_page: bool = False):
        current = await self._task_tab()
        reused = False
        if current is not None:
            _, issue = await self._observe(current)
            if issue:
                return {"_issue": issue}
            if reuse_product_page:
                try:
                    reused = validate_product_input(current["url"]) == validate_product_input(url)
                except ValueError:
                    pass
        if not reused:
            await self._navigate(url)
        tab = current if reused else await self._task_tab()
        if tab is None:
            raise BridgeError()
        raw_status, issue = await self._observe(tab)
        if issue:
            return {"_issue": issue}
        # Semantic snapshot only on a business page after authentication checks.
        snapshot = await self.bridge.command("snapshot")
        if not isinstance(snapshot, dict) or not _is_official_url(str(snapshot.get("url", ""))):
            return {"_issue": "unexpected_redirect"}
        if "login" in urlsplit(snapshot["url"]).path.lower():
            return {"_issue": "login_required"}
        if extractor==DETAIL_EXTRACT_JS:
            try:
                if validate_product_input(snapshot['url'])!=validate_product_input(url):
                    return {'_issue':'unexpected_redirect'}
            except ValueError:
                return {'_issue':'unexpected_redirect'}
        raw = decode_evaluation(await self.bridge.command("evaluate", {"code": extractor}))
        if extractor==DETAIL_EXTRACT_JS and raw.get('observed_goods_id',validate_product_input(url))!=validate_product_input(url):
            return {'_issue':'unexpected_redirect'}
        issue = self._page_issue(raw, str(raw.get("url", "")))
        raw["_reused_current_page"] = reused
        return {"_issue": issue} if issue else raw

    def _navigation_cooldown(self):
        now = self._clock()
        if self._last_business_navigation is not None:
            remaining = 30.0 - (now - self._last_business_navigation)
            if remaining > 0:
                return tool_result({**self._error("cooldown"), "retry_after_s": math.ceil(remaining)})
        self._last_business_navigation = now
        return None

    async def search(self, keyword: str, limit: int = 5):
        if (not isinstance(keyword, str) or not 1 <= len(keyword.strip()) <= 100
                or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10):
            return self._error("invalid_input")
        keyword = keyword.strip()
        async with self._lock:
            if self._risk_blocked:
                return self._error("risk_control")
            cooldown = self._navigation_cooldown()
            if cooldown:
                return cooldown
            try:
                url = "https://mobile.yangkeduo.com/search_result.html?" + urlencode({"search_key": keyword, "search_type": "goods"})
                raw = await self._read(url, SEARCH_EXTRACT_JS)
                if "_issue" in raw:
                    return self._error(raw["_issue"])
                if not raw.get("items") and not any(word in raw.get("pageText", "") for word in ("暂无商品", "没有找到", "未找到相关商品")):
                    return self._error("parse_error")
                had_items = bool(raw.get("items"))
                raw["items"] = [{**item, "goodsId": str(item["goodsId"])} for item in raw.get("items", [])
                                if isinstance(item, dict) and re.fullmatch(r"[0-9]{1,20}", str(item.get("goodsId", "")))
                                and isinstance(item.get("name"), str) and item["name"].strip()]
                if (raw.get("itemCount") or had_items) and not raw["items"]:
                    return self._error("parse_error")
                # Authentication was checked explicitly; generic OG branding is not an auth gate.
                raw["ogTitle"] = ""
                result = UPSTREAM["_process_search_result"](PARSER_CONTEXT, raw, keyword, limit)
                sources = {source["goodsId"]: source for source in reversed(raw["items"])}
                for item in result["items"]:
                    # The pure parser constructs the official URL from this numeric ID.
                    if validate_product_input(item["url"]) != item["goodsId"]:
                        return self._error("parse_error")
                    item["name"] = redact_text(item["name"], 500)
                    source = sources[item["goodsId"]]
                    if source.get("data_source") == "ssrListData.list":
                        for key in ("data_source", "price_source", "price_unit", "source_price_unit", "price_status", "sold_status", "sales_tip_scope"):
                            item[key] = source[key]
                        item["sold"] = None
                        item["sales_tip"] = redact_text(source.get("sales_tip", ""))
                    if item["price"] is None:
                        if "price_status" not in item:
                            item["price_status"] = "not_exposed" if source.get("price") is None else "unparseable"
                result["count"] = len(result["items"])
                return self._cache_quote(tool_result({"platform": "pinduoduo", **result}))
            except UPSTREAM["PDDRateLimitError"]:
                self._risk_blocked = True
                return self._error("risk_control")
            except UPSTREAM["PDDAuthError"]:
                return self._error("login_required")
            except BridgeError as exc:
                return self._error(exc.code)
            except Exception:
                return self._error("bridge_error")

    async def product(self, url_or_id: str):
        try:
            goods_id = validate_product_input(url_or_id)
        except ValueError:
            return self._error("invalid_input")
        async with self._lock:
            if self._risk_blocked:
                return self._error("risk_control")
            cooldown = self._navigation_cooldown()
            if cooldown:
                return cooldown
            try:
                url = f"https://mobile.yangkeduo.com/goods2.html?goods_id={goods_id}"
                raw = await self._read(url, DETAIL_EXTRACT_JS, reuse_product_page=True)
                if "_issue" in raw:
                    return self._error(raw["_issue"])
                if raw.get("name") in ("", "拼多多", "拼多多商城"):
                    raw["name"] = None
                if (not raw.get("soldOut") and not raw.get("name") and raw.get("price") is None and not raw.get("image_url")
                        and not any(word in raw.get("pageText", "") for word in ("商品已下架", "不存在"))):
                    return self._error("parse_error")
                raw["loginGated"] = False
                result = UPSTREAM["_process_detail_result"](PARSER_CONTEXT, raw, goods_id, url)
                if result.get("name"):
                    result["name"] = redact_text(result["name"], 500)
                result["specs"] = [redact_text(spec, 300) for spec in result.get("specs", [])]
                result["name_status"] = "ok" if result.get("name") else "not_exposed"
                result["price_status"] = raw.get("price_status", "ok" if result.get("price") is not None else "not_exposed")
                result["specs_status"] = "ok" if result["specs"] else "missing"
                for key in ("name_source", "price_source", "price_unit", "image_url", "image_status", "sales_status"):
                    if key in raw:
                        result[key] = raw[key]
                result["image_url"] = image_url(result.get("image_url"))
                result["image_status"] = "available" if result["image_url"] else "missing"
                result["skus"] = [{"sku_id": str(sku["sku_id"]), "specs": [redact_text(v, 300) for v in sku.get("specs", [])]}
                                  for sku in raw.get("skus", []) if isinstance(sku, dict) and re.fullmatch(r"[0-9]{1,20}", str(sku.get("sku_id", "")))]
                fields = {"name": "ok" if result.get("name") else "missing", "price": "ok" if result.get("price") is not None else "missing",
                          "images": "ok" if result["image_url"] else "missing", "specs": "ok" if result["specs"] else "missing"}
                result["reused_current_page"] = raw["_reused_current_page"]
                if result["state"] == "ok" and (not result.get("name") or result.get("price") is None):
                    result["name"] = result.get("name") or None
                    result["state"] = "partial"
                return self._cache_quote(tool_result({"platform": "pinduoduo", **result}, fields))
            except UPSTREAM["PDDAuthError"]:
                return self._error("login_required")
            except BridgeError as exc:
                return self._error(exc.code)
            except Exception:
                return self._error("bridge_error")

    async def add_to_cart(self, url_or_id: str, qty: int = 1):
        # Capability result only: no placeholder MCP cart tools on PDD.
        try:
            goods_id = validate_product_input(url_or_id)
            if isinstance(qty, bool) or not isinstance(qty, int) or not 1 <= qty <= 3:
                raise ValueError()
        except ValueError:
            return self._error("invalid_input")
        return tool_result({"platform": "pinduoduo", "state": "error", "code": "unsupported", "goodsId": goods_id,
                       "reason": "当前拼多多网页购物流程未提供已核实的原生购物车；不会用收藏或本地清单代替加购。"}, {"cart": "unsupported"})

    async def contact_merchant(self, url: str, text: str):
        """Validate a specific merchant request; currently no verified send DOM."""
        if (not isinstance(text, str) or not 1 <= len(text.strip()) <= 500
                or len(text) > 500 or any(ord(char) < 32 and char not in "\n\t" for char in text)):
            return self._error("invalid_input")
        return await self._merchant_inspect(url, text)

    async def merchant_messages(self, url: str):
        return await self._merchant_inspect(url)

    async def _merchant_inspect(self, url: str, text=None):
        try:
            # Merchant authorization names a URL, never an implicit current page or numeric ID.
            if not isinstance(url, str) or not _is_official_url(url):
                raise ValueError()
            goods_id = validate_product_input(url)
        except ValueError:
            return self._error("invalid_input")
        canonical = canonical_product_url(url)
        async with self._lock:
            if self._risk_blocked:
                return self._error("risk_control")
            cooldown = self._navigation_cooldown()
            if cooldown:
                return cooldown
            event_id = hashlib.sha256((canonical+'|'+text).encode()).hexdigest() if text else None
            with self.watchlist.connect() as db:
                db.execute('CREATE TABLE IF NOT EXISTS merchant_bindings(goods_id TEXT PRIMARY KEY,mall_sn TEXT)')
                bound=db.execute('SELECT mall_sn FROM merchant_bindings WHERE goods_id=?',(goods_id,)).fetchone()
            expected_mall = bound[0] if bound else ''
            if event_id:
                with self.watchlist.connect() as db:
                    db.execute('CREATE TABLE IF NOT EXISTS merchant_sends(id TEXT PRIMARY KEY,state TEXT)')
                    old=db.execute('SELECT state FROM merchant_sends WHERE id=?',(event_id,)).fetchone()
                if old:
                    return {"ok":old[0]=='verified',"status":"ok" if old[0]=='verified' else "unverified","data":{"already":True,"sent":True if old[0]=='verified' else None},"already":True,"sent":True if old[0]=='verified' else None}
            try:
                tab = await self._task_tab()
                if tab:
                    _, issue = await self._observe(tab)
                    if issue:
                        return self._error(issue)
                reuse = False
                if tab:
                    try:
                        current_url=tab.get("url", "")
                        parsed=urlsplit(current_url)
                        values=parse_qs(parsed.query)
                        reuse = _is_official_url(current_url) and (validate_product_input(current_url)==goods_id if parsed.path!='/chat_detail.html' else bool(expected_mall) and values.get('goods_id')==[goods_id] and values.get('mall_sn')==[expected_mall])
                    except ValueError:
                        pass
                if not reuse:
                    await self._navigate(canonical)
                    tab = await self._task_tab()
                if not tab:
                    return self._error("unexpected_redirect")
                async def inspect(phase, click=False):
                    current=await self._task_tab()
                    if not current: raise BridgeError('unexpected_redirect')
                    current_path=urlsplit(str(current.get('url',''))).path.lower()
                    if re.search(r'checkout|order|payment|cashier|pay\.html',current_path): raise BridgeError('checkout_page_reached')
                    if phase=='entry':
                        if validate_product_input(current.get('url',''))!=goods_id: raise BridgeError('unexpected_redirect')
                    else:
                        values=parse_qs(urlsplit(str(current.get('url',''))).query,keep_blank_values=True)
                        if current_path!='/chat_detail.html' or values.get('goods_id')!=[goods_id] or not expected_mall or values.get('mall_sn')!=[expected_mall]: raise BridgeError('unexpected_redirect')
                    await self._select_task_tab(current)
                    raw=decode_evaluation(await self.bridge.command('evaluate',{'code':merchant_script(goods_id,phase,(text or '') if phase in {'draft','send'} else '',click,expected_mall if phase!='entry' else '')}))
                    issue=self._page_issue(raw,str(raw.get('url',''))) or raw.get('issue')
                    if issue: raise BridgeError(issue if issue in ERROR_MESSAGES else 'parse_error')
                    if raw.get('goodsId')!=goods_id: raise BridgeError('unexpected_redirect')
                    return raw
                if urlsplit(tab['url']).path!='/chat_detail.html':
                    await inspect('entry')
                    await inspect('entry',click=True)
                    await asyncio.sleep(0.3)
                    arrived=await self._task_tab()
                    values=parse_qs(urlsplit(str(arrived.get('url','')) if arrived else '').query,keep_blank_values=True)
                    if values.get('goods_id')!=[goods_id] or len(values.get('mall_sn',[]))!=1 or not values['mall_sn'][0] or len(values['mall_sn'][0])>512:
                        return self._error('unexpected_redirect')
                    expected_mall=values['mall_sn'][0]
                    with self.watchlist.connect() as db:
                        db.execute('INSERT OR REPLACE INTO merchant_bindings VALUES(?,?)',(goods_id,expected_mall))
                before=await inspect('read')
                messages=[{'text':redact_text(m.get('text',''),1000)} for m in before.get('messages',[]) if isinstance(m,dict)]
                if text is None:
                    return tool_result({'platform':'pinduoduo','goodsId':goods_id,'url':canonical,'messages':messages,'merchant_identity_verified':True,'state':'ok'})
                with self.watchlist.connect() as db:
                    db.execute('INSERT INTO merchant_sends VALUES(?,?)',(event_id,'unknown'))
                try:
                    await inspect('draft')
                    await asyncio.sleep(0.1)
                    clicked=await inspect('send')
                    await asyncio.sleep(0.3)
                    after=await inspect('read')
                    confirmed=clicked.get('clicked') is True and after.get('message_count',0)>before.get('message_count',0) and bool(after.get('messages')) and text in after['messages'][-1].get('text','')
                except BridgeError as exc:
                    if exc.code in {'risk_control','login_required','unexpected_redirect','checkout_page_reached'}: raise
                    confirmed=False
                if confirmed:
                    with self.watchlist.connect() as db: db.execute('UPDATE merchant_sends SET state=? WHERE id=?',('verified',event_id))
                return {'ok':confirmed,'status':'ok' if confirmed else 'unverified','data':{'goodsId':goods_id,'sent':True if confirmed else None,'automatic_retry':False},'goodsId':goods_id,'sent':True if confirmed else None,'automatic_retry':False,'draft_may_remain':not confirmed}
            except BridgeError as exc:
                return self._error(exc.code)
            except ValueError:
                return self._error("unexpected_redirect")
            except Exception:
                return self._error("bridge_error")

    async def favorite(self, url_or_id: str, wanted: bool = True):
        try:
            goods_id = validate_product_input(url_or_id)
            if not isinstance(wanted, bool):
                raise ValueError()
        except ValueError:
            return self._error("invalid_input")
        async with self._lock:
            if self._risk_blocked:
                return self._error("risk_control")
            cooldown = self._navigation_cooldown()
            if cooldown:
                return cooldown
            try:
                url = f"https://mobile.yangkeduo.com/goods2.html?goods_id={goods_id}"
                raw = await self._read(url, DETAIL_EXTRACT_JS, reuse_product_page=True)
                if "_issue" in raw:
                    return self._error(raw["_issue"])
                async def inspect(click=False):
                    tab = await self._task_tab()
                    if tab is None or validate_product_input(tab.get("url", "")) != goods_id:
                        raise BridgeError("unexpected_redirect")
                    await self._select_task_tab(tab)
                    state = decode_evaluation(await self.bridge.command("evaluate", {"code": favorite_script(goods_id, wanted, click)}))
                    issue = self._page_issue(state, str(state.get("url", "")))
                    if issue:
                        raise BridgeError(issue)
                    if state.get("issue"):
                        raise BridgeError(state["issue"])
                    return state
                before = await inspect()
                if before.get("confirmed") is True:
                    return tool_result({"platform": "pinduoduo", "goodsId": goods_id, "state": "ok", "favorited": wanted,
                                   "changed": False, "already": True, "url": url}, {"favorite": "ok"})
                clicked = await inspect(click=True)
                # Only passive observation after one click; never repeat a write.
                tab = await self._task_tab()
                if tab and re.search(r"checkout|order|payment|cashier|pay\.html", urlsplit(str(tab.get("url", ""))).path, re.I):
                    return self._error("checkout_page_reached")
                try:
                    after = await inspect()
                except BridgeError as exc:
                    if exc.code not in {"unsupported", "ambiguous_control", "unsafe_control", "bridge_error", "parse_error", "bridge_unavailable"}:
                        raise
                    after = {}
                if after.get("confirmed") is True:
                    return tool_result({"platform": "pinduoduo", "goodsId": goods_id, "state": "ok", "favorited": wanted,
                                   "changed": clicked.get("clicked") is True, "url": url}, {"favorite": "ok"})
                output = self._error("action_unconfirmed")
                output.update({"status": "unverified", "clicked": clicked.get("clicked") is True})
                output["data"]["clicked"] = clicked.get("clicked") is True
                return output
            except BridgeError as exc:
                return self._error(exc.code)
            except ValueError:
                return self._error("unexpected_redirect")
            except Exception:
                return self._error("bridge_error")

    async def close(self):
        async with self._lock:
            try:
                tab = await self._task_tab()
                if tab is not None:
                    await self.bridge.command("close_session")
                return tool_result({**self._base_status(), "state": "closed", "message": "已仅关闭拼多多MCP任务 session；日常 Brave 与其持续会话保留。"})
            except BridgeError as exc:
                return self._error(exc.code)
            except Exception:
                return self._error("bridge_error")

    async def shutdown(self):
        # The task's HTTP client is ours; the user's Brave and tabs are not.
        await self.notifications.close()
        await self.bridge.aclose()


def canonical_product_url(value: str) -> str:
    return "https://mobile.yangkeduo.com/goods2.html?goods_id=" + validate_product_input(value)


watchlist_spec = importlib.util.spec_from_file_location("pdd_local_watchlist", WORKSPACE_ROOT / "src/watchlist.py")
watchlist_module = importlib.util.module_from_spec(watchlist_spec)
watchlist_spec.loader.exec_module(watchlist_module)
notifications_spec = importlib.util.spec_from_file_location("pdd_notifications", WORKSPACE_ROOT / "src/notifications.py")
notifications_module = importlib.util.module_from_spec(notifications_spec)
notifications_spec.loader.exec_module(notifications_module)
pending_cart_spec = importlib.util.spec_from_file_location("pdd_pending_cart", WORKSPACE_ROOT / "src/pending_cart.py")
pending_cart_module = importlib.util.module_from_spec(pending_cart_spec)
pending_cart_spec.loader.exec_module(pending_cart_module)
adapter = LocalPinduoduoAdapter()
mcp = FastMCP(
    "pinduoduo-personal", host=MCP_HOST, port=MCP_PORT,
    streamable_http_path="/mcp", stateless_http=True, json_response=True, log_level="WARNING",
    instructions="个人拼多多读取与原生收藏工具，沿用日常 Brave 的固定任务标签组。本人认证，遇风控停止；不读取凭据。",
)


watchlist_module.register_watchlist(mcp, adapter.watchlist)
notifications_module.register_notifications(mcp, adapter.notifications)
pending_cart_module.register_pending_cart(mcp, adapter.pending_cart)


@mcp.tool()
async def login_pinduoduo() -> dict[str, Any]:
    """在日常 Brave 的拼多多MCP任务页打开官方登录入口，由本人认证。"""
    return await adapter.login()


@mcp.tool()
async def status_pinduoduo() -> dict[str, Any]:
    """被动查看脱敏页面迹象及已有失败请求元数据；不导航、填表或读取凭据。"""
    return await adapter.status()


@mcp.tool()
async def search_pinduoduo(keyword: str, limit: int = 5) -> dict[str, Any]:
    """只读搜索拼多多商品，limit 1–10；普通页面不支持或风控时如实停止。"""
    return await adapter.search(keyword, limit)


@mcp.tool()
async def get_pinduoduo_product(url_or_id: str) -> dict[str, Any]:
    """读取一件拼多多商品；仅接受官方商品 URL 或数字 goods_id。"""
    return await adapter.product(url_or_id)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True})
async def favorite_pinduoduo_item(url_or_id: str) -> dict[str, Any]:
    """在准确商品页点击唯一原生收藏控件；需 App/控件不明确则 unsupported，尚未真实验收。"""
    return await adapter.favorite(url_or_id, wanted=True)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
async def unfavorite_pinduoduo_item(url_or_id: str) -> dict[str, Any]:
    """在准确商品详情页取消原生收藏，必须有唯一明确收藏状态控件；不替代为本地收藏。"""
    return await adapter.favorite(url_or_id, wanted=False)


@mcp.tool()
async def close_pinduoduo_browser() -> dict[str, Any]:
    """仅明确调用时关闭本服务的任务 session；不关闭日常 Brave。"""
    return await adapter.close()


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
async def contact_merchant(url: str, text: str) -> dict[str, Any]:
    """显式官方商品 URL + 1–500 字正文；由原生客服进入匹配商品会话，唯一发送一次后读回。同正文持久去重，未知不重发；真实发送尚未验收。"""
    return await adapter.contact_merchant(url, text)


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def merchant_messages(url: str) -> dict[str, Any]:
    """打开指定商品的官方客服会话，核对goods_id/mall_sn，读取最多20条消息容器项；不输入、不发送。"""
    return await adapter.merchant_messages(url)


def create_http_app():
    app = mcp.streamable_http_app()
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def process_lifespan(application):
        try:
            async with original_lifespan(application) as state:
                adapter.notifications.start()
                yield state
        finally:
            await adapter.shutdown()

    app.router.lifespan_context = process_lifespan
    security_spec = importlib.util.spec_from_file_location("pdd_http_security", WORKSPACE_ROOT / "src/http_security.py")
    security_module = importlib.util.module_from_spec(security_spec)
    security_spec.loader.exec_module(security_module)
    return security_module.protect_http(app, host=MCP_HOST, port=MCP_PORT)


async def run_server(stdio: bool = False):
    if stdio:
        try:
            adapter.notifications.start()
            await mcp.run_stdio_async()
        finally:
            await adapter.shutdown()
        return
    import uvicorn
    config = uvicorn.Config(create_http_app(), host=MCP_HOST, port=MCP_PORT, log_level="warning")
    await uvicorn.Server(config).serve()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stdio", action="store_true", help="Use stdio MCP instead of loopback HTTP")
    args = parser.parse_args()
    os.umask(0o077)
    asyncio.run(run_server(stdio=args.stdio))


if __name__ == "__main__":
    main()
