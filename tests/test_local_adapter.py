"""Offline WebBridge boundary tests. No live browser or authentication calls."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import shutil
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


def adapter_module():
    if "pinduoduo_local_server" not in sys.modules:
        path = ROOT / "src" / "server.py"
        spec = importlib.util.spec_from_file_location("pinduoduo_local_server", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return sys.modules["pinduoduo_local_server"]


class FakeBridge:
    def __init__(self, tabs=None, raw=None, status=None, network=None, redirected_url=None):
        self.tabs = tabs or []
        self.raw = raw or {}
        self.status_data = status or {}
        self.network = network if network is not None else {"count": 0, "requests": []}
        self.redirected_url = redirected_url
        self.calls = []
        self.closed = False

    async def command(self, action, args=None):
        args = args or {}
        self.calls.append((action, args))
        if action == "list_tabs":
            return {"success": True, "tabs": self.tabs}
        if action == "find_tab":
            assert args.get("active") is not True
            return {"success": True, "tabId": self.tabs[0]["tabId"], "url": self.tabs[0]["url"]}
        if action == "navigate":
            url = self.redirected_url or args["url"]
            self.tabs = [{"tabId": 123, "url": url, "title": "拼多多", "active": True, "groupTitle": "拼多多MCP"}]
            await asyncio.sleep(0)
            return {"success": True, "url": url, "tabId": 123}
        if action == "snapshot":
            return {"url": self.tabs[0]["url"], "title": "拼多多", "tree": []}
        if action == "evaluate":
            if args["code"] == adapter_module().STATUS_JS:
                raw = {"url": self.tabs[0]["url"], "title": "拼多多", "loginHint": False, **self.status_data}
            else:
                raw = {"url": self.tabs[0]["url"], **self.raw}
            return {"type": "string", "value": json.dumps(raw)}
        if action == "network":
            assert args == {"cmd": "list"}
            if isinstance(self.network, Exception):
                raise self.network
            return self.network
        if action == "close_session":
            self.tabs = []
            return {"success": True}
        raise AssertionError(f"Unexpected bridge action {action}")

    async def aclose(self):
        self.closed = True


def own_tab(url="https://mobile.yangkeduo.com/login.html"):
    return {"tabId": 123, "url": url, "title": "拼多多", "active": True, "groupTitle": "拼多多MCP"}


@pytest.mark.parametrize("value, expected", [
    ("123456789", "123456789"),
    ("https://mobile.yangkeduo.com/goods2.html?goods_id=123&share=anything", "123"),
])
def test_product_input_canonicalizes_only_official_goods(value, expected):
    assert adapter_module().validate_product_input(value) == expected


@pytest.mark.parametrize("value", [
    "https://taobao.com/goods2.html?goods_id=123",
    "https://mobile.yangkeduo.com.evil.example/goods2.html?goods_id=123",
    "https://mobile.yangkeduo.com@evil.example/goods2.html?goods_id=123",
    "https://evil.example@mobile.yangkeduo.com/goods2.html?goods_id=123",
    "http://mobile.yangkeduo.com/goods2.html?goods_id=123",
    "https://mobile.yangkeduo.com:8841/goods2.html?goods_id=123",
    "https://mobile.yangkeduo.com/login.html?goods_id=123",
    "https://mobile.yangkeduo.com/goods2.html?goods_id=123&goods_id=456",
    "https://mobile.yangkeduo.com/goods2.html?goods_id=123x",
    "//mobile.yangkeduo.com/goods2.html?goods_id=123",
    "123<script>", "", "１２３",
])
def test_product_input_rejects_cross_platform_and_ambiguous_urls(value):
    with pytest.raises(ValueError):
        adapter_module().validate_product_input(value)



@pytest.mark.asyncio
async def test_bridge_uses_real_envelope_fixed_session_and_loopback_without_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    received = []
    def handle(request):
        received.append((str(request.url), json.loads(request.content)))
        return httpx.Response(200, json={"ok": True, "data": {"success": True, "tabs": []}})
    client = adapter_module().BridgeClient(transport=httpx.MockTransport(handle))
    assert await client.command("list_tabs") == {"success": True, "tabs": []}
    assert received == [("http://127.0.0.1:10086/command", {"action": "list_tabs", "args": {}, "session": "commerce-mcp-pinduoduo"})]
    assert client._client.trust_env is False
    await client.aclose()


@pytest.mark.asyncio
async def test_bridge_does_not_echo_error_messages_or_accept_credential_commands():
    module = adapter_module()
    def handle(request):
        return httpx.Response(200, json={"ok": False, "error": {"code": "NO_TAB", "message": "SECRET_PHONE_13812345678"}})
    client = module.BridgeClient(transport=httpx.MockTransport(handle))
    with pytest.raises(module.BridgeError) as error:
        await client.command("list_tabs")
    assert "SECRET_PHONE" not in str(error.value)
    for action, args in [("cdp", {"method": "Network.getCookies"}), ("fill", {}), ("network", {"cmd": "detail"}), ("network", {"cmd": "start"}), ("evaluate", {"code": "document.cookie"})]:
        with pytest.raises(ValueError):
            await client.command(action, args)
    await client.aclose()


@pytest.mark.asyncio
async def test_mcp_preserves_existing_tools_and_adds_native_favorite_pair():
    tools = await adapter_module().mcp.list_tools()
    by_name = {tool.name: tool.inputSchema.get("properties", {}) for tool in tools}
    assert set(by_name) == {"login_pinduoduo", "status_pinduoduo", "search_pinduoduo", "get_pinduoduo_product", "close_pinduoduo_browser", "favorite_pinduoduo_item", "unfavorite_pinduoduo_item", "watchlist_upsert", "watchlist_list", "watchlist_check", "contact_merchant", "merchant_messages", "notification_configure", "notification_status", "notify_owner"}
    assert set(by_name["search_pinduoduo"]) == {"keyword", "limit"}
    assert set(by_name["get_pinduoduo_product"]) == {"url_or_id"}
    assert not by_name["login_pinduoduo"] and not by_name["status_pinduoduo"]


@pytest.mark.asyncio
async def test_status_without_task_page_is_passive_and_does_not_create_profile(tmp_path):
    bridge = FakeBridge()
    api = adapter_module().LocalPinduoduoAdapter(tmp_path, bridge=bridge)
    result = await api.status()
    assert result["network"] == "existing-browser"
    assert result["browser_running"] is False
    assert result["login_verified"] is False
    assert bridge.calls == [("list_tabs", {})]
    assert not (tmp_path / "runtime").exists()


@pytest.mark.asyncio
async def test_concurrent_login_creates_one_task_tab_in_existing_brave_without_form_snapshot(tmp_path):
    bridge = FakeBridge()
    api = adapter_module().LocalPinduoduoAdapter(tmp_path, bridge=bridge)
    results = await asyncio.gather(api.login(), api.login())
    navigations = [args for action, args in bridge.calls if action == "navigate"]
    assert len(navigations) == 1
    assert len([args for args in navigations if args.get("newTab")]) == 1
    assert navigations[0] == {"url": "https://mobile.yangkeduo.com/login.html", "newTab": True, "group_title": "拼多多MCP"}
    assert [result["state"] for result in results] == ["login_opened", "login_reused"]
    assert all(result["network"] == "existing-browser" and result["login_verified"] is False for result in results)
    assert all(action not in {"snapshot", "fill", "click", "cdp"} for action, args in bridge.calls)
    assert not (tmp_path / "runtime").exists()


@pytest.mark.asyncio
async def test_session_with_borrowed_or_wrong_group_tab_is_never_navigated_or_closed(tmp_path):
    for tab in [{**own_tab(), "borrowed": True}, {**own_tab(), "groupTitle": "其他用户页"}]:
        bridge = FakeBridge(tabs=[tab])
        api = adapter_module().LocalPinduoduoAdapter(tmp_path, bridge=bridge)
        assert (await api.login())["code"] == "session_conflict"
        assert (await api.close())["code"] == "session_conflict"
        assert all(action not in {"navigate", "find_tab", "evaluate", "close_session"} for action, args in bridge.calls)


@pytest.mark.asyncio
async def test_status_redacts_url_title_and_returns_only_failed_request_metadata(tmp_path):
    bridge = FakeBridge(
        tabs=[own_tab("https://mobile.yangkeduo.com/login.html?phone=13812345678&token=SECRET#code=OTP")],
        status={"url": "https://mobile.yangkeduo.com/login.html?phone=13812345678&token=SECRET#code=OTP", "title": "拼多多 13812345678 token=SECRET", "smsSendFailed": True},
        network={"count": 3, "requests": [
            {"requestId": "private", "url": "https://mobile.yangkeduo.com/api/sms/send?phone=13812345678&token=SECRET", "method": "POST", "status": 403, "mimeType": "application/json", "completed": True, "headers": {"Authorization": "SECRET"}, "requestBody": "PHONE=13812345678"},
            {"url": "https://mobile.yangkeduo.com/api/phone/13812345678", "status": 0, "error": "net::ERR_CONNECTION_RESET"},
            {"url": "https://mobile.yangkeduo.com/ok", "status": 200},
        ]})
    api = adapter_module().LocalPinduoduoAdapter(tmp_path, bridge=bridge)
    result = await api.status()
    encoded = json.dumps(result)
    assert result["page_url"] == "https://mobile.yangkeduo.com/login.html"
    assert "13812345678" not in encoded and "SECRET" not in encoded and "OTP" not in encoded
    assert result["sms_send_failed_hint"] is True
    diagnostic = result["network_diagnostics"]
    assert diagnostic["state"] == "captured"
    assert diagnostic["reason_known"] is False
    assert diagnostic["failed_requests"] == [
        {"path": "/api/sms/send", "http_status": 403},
        {"path": "/api/phone/[redacted]", "http_status": 0, "error_code": "net::ERR_CONNECTION_RESET"},
    ]
    assert all(action not in {"snapshot", "click", "fill", "cdp"} for action, args in bridge.calls)
    assert not any(action == "network" and args.get("cmd") != "list" for action, args in bridge.calls)


@pytest.mark.asyncio
async def test_network_not_captured_does_not_mean_bridge_disconnected(tmp_path):
    module = adapter_module()
    bridge = FakeBridge(tabs=[own_tab()], network=module.BridgeError("bridge_error"))
    result = await module.LocalPinduoduoAdapter(tmp_path, bridge=bridge).status()
    assert result["browser_running"] is True
    assert result["network_diagnostics"]["state"] == "not_captured"
    assert result["network_diagnostics"]["failed_requests"] == []


@pytest.mark.asyncio
async def test_invalid_product_is_rejected_before_any_bridge_action(tmp_path):
    bridge = FakeBridge()
    result = await adapter_module().LocalPinduoduoAdapter(tmp_path, bridge=bridge).product("https://jd.com/goods2.html?goods_id=123")
    assert result["code"] == "invalid_input"
    assert not bridge.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("status, code", [
    ({"url": "https://mobile.yangkeduo.com/login.html", "loginRequired": True}, "login_required"),
    ({"riskControl": True}, "risk_control"),
    ({"notSupported": True}, "not_supported"),
    ({"url": "https://taobao.com/"}, "unexpected_redirect"),
])
async def test_search_reports_login_risk_desktop_support_and_redirect_before_extraction(tmp_path, status, code):
    bridge = FakeBridge(status=status, raw={"itemCount": 1, "items": [{"goodsId": "123", "name": "测试", "price": 1}]})
    result = await adapter_module().LocalPinduoduoAdapter(tmp_path, bridge=bridge).search("测试", 2)
    assert result["state"] == "error" and result["code"] == code
    evaluations = [args for action, args in bridge.calls if action == "evaluate"]
    assert all("localPddStatus" in args["code"] for args in evaluations)
    assert len([1 for action, args in bridge.calls if action == "navigate"]) == 1


@pytest.mark.asyncio
async def test_risk_control_is_sticky_across_close_and_reopen_without_reset(tmp_path):
    bridge = FakeBridge(status={"riskControl": True})
    api = adapter_module().LocalPinduoduoAdapter(tmp_path, bridge=bridge)
    first = await api.search("测试", 1)
    second = await api.search("再次", 1)
    assert first["code"] == second["code"] == "risk_control"
    await api.close()
    third = await api.search("再次", 1)
    assert third["code"] == "risk_control"
    assert len([1 for action, args in bridge.calls if action == "navigate"]) == 1


@pytest.mark.asyncio
async def test_status_page_risk_signal_stops_next_navigation(tmp_path):
    bridge = FakeBridge(tabs=[own_tab()], status={"riskControl": True})
    api = adapter_module().LocalPinduoduoAdapter(tmp_path, bridge=bridge)
    assert (await api.status())["risk_control_detected"] is True
    assert (await api.product("123"))["code"] == "risk_control"
    assert not any(action == "navigate" for action, args in bridge.calls)


@pytest.mark.asyncio
async def test_search_reuses_fixed_parser_without_phone_token_or_query_output(tmp_path):
    bridge = FakeBridge(raw={"itemCount": 3, "items": [
        {"goodsId": "123", "name": "测试商品A", "price": "9.9", "sold": "100"},
        {"goodsId": "123", "name": "重复", "price": "9.9", "sold": "100"},
        {"goodsId": "456", "name": "测试商品B", "price": "12", "sold": "20"},
    ]})
    api = adapter_module().LocalPinduoduoAdapter(tmp_path, bridge=bridge)
    result = await api.search("儿童 书桌", 1)
    assert result["count"] == 1
    assert result["items"] == [{"goodsId": "123", "name": "测试商品A", "price": 9.9, "sold": "100", "url": "https://mobile.yangkeduo.com/goods2.html?goods_id=123"}]
    actions = [action for action, args in bridge.calls]
    assert "snapshot" in actions
    assert "search_key=%E5%84%BF%E7%AB%A5+%E4%B9%A6%E6%A1%8C" in next(args["url"] for action, args in bridge.calls if action == "navigate")


@pytest.mark.asyncio
async def test_detail_uses_canonical_url_and_reports_unknown_page(tmp_path):
    bridge = FakeBridge(raw={"name": "儿童实木书桌", "price": 99.0, "specs": ["规格：原木"], "pageText": "SECRET_TOKEN"})
    api = adapter_module().LocalPinduoduoAdapter(tmp_path, bridge=bridge)
    result = await api.product("https://mobile.yangkeduo.com/goods2.html?goods_id=123&share_token=SECRET_TOKEN")
    assert result["state"] == "ok" and result["goodsId"] == "123"
    assert "SECRET_TOKEN" not in json.dumps(result)
    assert next(args["url"] for action, args in bridge.calls if action == "navigate") == "https://mobile.yangkeduo.com/goods2.html?goods_id=123"
    empty = FakeBridge(raw={"itemCount": 0, "items": []})
    assert (await adapter_module().LocalPinduoduoAdapter(tmp_path, bridge=empty).search("测试"))["code"] == "parse_error"


@pytest.mark.asyncio
async def test_explicit_close_only_closes_fixed_session_and_shutdown_does_not_close_tabs(tmp_path):
    bridge = FakeBridge(tabs=[own_tab()])
    api = adapter_module().LocalPinduoduoAdapter(tmp_path, bridge=bridge)
    await api.shutdown()
    assert bridge.closed
    assert not any(action == "close_session" for action, args in bridge.calls)
    bridge.closed = False
    result = await api.close()
    assert result["browser_running"] is False
    assert [action for action, args in bridge.calls] == ["list_tabs", "close_session"]


def test_stateless_http_keeps_existing_brave_tab_after_requests_and_app_exit(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    module = adapter_module()
    bridge = FakeBridge()
    api = module.LocalPinduoduoAdapter(tmp_path, bridge=bridge)
    monkeypatch.setattr(module, "adapter", api)
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    with TestClient(module.create_http_app(), base_url="http://127.0.0.1:8841") as client:
        login = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "login_pinduoduo", "arguments": {}}})
        assert login.json()["result"]["structuredContent"]["browser_running"] is True
        status = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "status_pinduoduo", "arguments": {}}})
        assert status.json()["result"]["structuredContent"]["browser_running"] is True
    assert bridge.tabs
    assert bridge.closed
    assert not any(action == "close_session" for action, args in bridge.calls)


@pytest.mark.asyncio
async def test_status_redacts_otp_title_and_reports_outside_page_without_claiming_ready(tmp_path):
    bridge = FakeBridge(tabs=[own_tab()], status={"title": "拼多多 验证码 123456 token PRIVATE_AUTH"})
    result = await adapter_module().LocalPinduoduoAdapter(tmp_path, bridge=bridge).status()
    assert "123456" not in result["page_title"] and "PRIVATE_AUTH" not in result["page_title"]
    outside = FakeBridge(tabs=[own_tab()], status={"url": "https://taobao.com/?token=PRIVATE_AUTH"})
    result = await adapter_module().LocalPinduoduoAdapter(tmp_path, bridge=outside).status()
    assert result["code"] == "unexpected_redirect"
    assert "PRIVATE_AUTH" not in json.dumps(result)


@pytest.mark.asyncio
async def test_search_drops_invalid_goods_ids_and_never_returns_auth_like_item_urls(tmp_path):
    bridge = FakeBridge(raw={"itemCount": 2, "items": [
        {"goodsId": "123&token=PRIVATE_AUTH", "name": "不合法的商品链接", "price": 1, "sold": 0},
        {"goodsId": "456", "name": "正常商品", "price": 2, "sold": 1},
    ]})
    result = await adapter_module().LocalPinduoduoAdapter(tmp_path, bridge=bridge).search("测试", 2)
    assert result["count"] == 1 and result["items"][0]["goodsId"] == "456"
    assert "PRIVATE_AUTH" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("login_hint", [False, True])
async def test_login_preserves_existing_auth_or_logged_in_page_without_navigation(tmp_path, login_hint):
    current = "https://mobile.yangkeduo.com/login.html?step=verify" if not login_hint else "https://mobile.yangkeduo.com/personal.html"
    bridge = FakeBridge(tabs=[own_tab(current)], status={"loginHint": login_hint, "loginRequired": not login_hint})
    result = await adapter_module().LocalPinduoduoAdapter(tmp_path, bridge=bridge).login()
    assert result["state"] == "login_reused"
    assert result["login_hint"] is login_hint
    assert bridge.tabs[0]["url"] == current
    assert not any(action in {"navigate", "snapshot", "click", "fill", "cdp"} for action, args in bridge.calls)
    assert all("localPddStatus" in args["code"] for action, args in bridge.calls if action == "evaluate")


@pytest.mark.asyncio
async def test_search_and_detail_share_thirty_second_navigation_window_without_delaying_status_or_login(tmp_path):
    now = [100.0]
    bridge = FakeBridge(raw={"itemCount": 1, "items": [{"goodsId": "123", "name": "普通商品", "price": 1, "sold": 0}]})
    api = adapter_module().LocalPinduoduoAdapter(tmp_path, bridge=bridge, clock=lambda: now[0])
    assert (await api.search("普通商品", 1))["state"] == "ok"
    after_first = len(bridge.calls)
    early = await api.product("123")
    assert early["code"] == "cooldown" and early["retry_after_s"] == 30
    assert len(bridge.calls) == after_first
    assert (await api.status())["browser_running"] is True
    assert (await api.login())["state"] == "login_reused"
    assert len([1 for action, args in bridge.calls if action == "navigate"]) == 1
    now[0] = 129.5
    before_second = len(bridge.calls)
    assert (await api.product("123"))["retry_after_s"] == 1
    assert len(bridge.calls) == before_second
    now[0] = 130.0
    bridge.raw = {"name": "普通商品", "price": 1, "specs": []}
    assert (await api.product("123"))["state"] == "ok"
    assert len([1 for action, args in bridge.calls if action == "navigate"]) == 2
    assert (await api.search("另一商品"))["code"] == "cooldown"


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["authInputPresent", "loginOverlayPresent"])
async def test_same_url_authentication_inputs_or_overlay_block_snapshot_and_product_extraction(tmp_path, flag):
    bridge = FakeBridge(status={flag: True}, raw={"name": "看似正常商品", "price": 1})
    result = await adapter_module().LocalPinduoduoAdapter(tmp_path, bridge=bridge).product("123")
    assert result["code"] == "login_required"
    assert not any(action == "snapshot" for action, args in bridge.calls)
    assert all("localPddStatus" in args["code"] for action, args in bridge.calls if action == "evaluate")


@pytest.mark.parametrize("kind, required", [
    ("password", True), ("one-time-code", True), ("overlay", True),
    ("hidden-password", False), ("hidden-one-time-code", False),
    ("hidden-overlay", False), ("visibility-hidden-password", False), ("disabled-password", False),
])
def test_actual_status_javascript_detects_authentication_without_reading_secret_values(kind, required):
    import subprocess
    # Execute the real fixed JS in a controlled DOM. Input.value/Cookie access
    # throws, so a future credential read makes this behavioral test fail.
    runner = r'''
const kind = process.argv[1];
const input = {disabled:kind==='disabled-password', getClientRects(){return kind.startsWith('hidden-') ? [] : [{}];}};
Object.defineProperty(input, 'value', { get() { throw new Error('input value read'); } });
const overlay = {textContent:'请登录', getClientRects(){return kind==='hidden-overlay' ? [] : [{}];}};
global.window = {getComputedStyle(){return {display:'block',visibility:kind==='visibility-hidden-password' ? 'hidden':'visible'};}};
global.location = {origin:'https://mobile.yangkeduo.com', pathname:'/goods2.html'};
global.document = {
  body:{innerText:'普通商品 ¥1'}, title:'普通商品',
  querySelector(selector){
    if(kind.endsWith('password') && selector.includes('password')) return input;
    if(kind.endsWith('one-time-code') && selector.includes('one-time-code')) return input;
    return null;
  },
  querySelectorAll(selector){
    if(selector.includes('input[')) return kind.endsWith('password') || kind.endsWith('one-time-code') ? [input] : [];
    return kind.endsWith('overlay') ? [overlay] : [];
  }
};
Object.defineProperty(document, 'cookie', { get(){throw new Error('Cookie read');} });
const code = require('fs').readFileSync(0, 'utf8');
console.log(eval(code));
'''
    result = subprocess.run([shutil.which("node"), "-e", runner, kind], input=adapter_module().STATUS_JS, text=True, capture_output=True, check=True)
    status = json.loads(result.stdout)
    assert status["loginRequired"] is required
    assert "input value" not in result.stdout


@pytest.mark.asyncio
async def test_real_page_wide_div_false_positive_without_goods_id_is_not_a_success(tmp_path):
    bridge = FakeBridge(raw={"itemCount": 1, "items": [
        {"goodsId": "", "name": "桌面手机支架", "price": None, "sold": 50000000, "url": ""},
    ]})
    result = await adapter_module().LocalPinduoduoAdapter(tmp_path, bridge=bridge).search("桌面手机支架", 1)
    assert result["state"] == "error"
    assert result["code"] == "parse_error"
    assert "items" not in result


@pytest.mark.asyncio
async def test_real_goods_id_with_canonical_url_and_missing_price_is_explicitly_partial(tmp_path):
    bridge = FakeBridge(raw={"itemCount": 1, "items": [
        {"goodsId": "123456", "name": "桌面手机支架", "price": None, "sold": "100"},
    ]})
    result = await adapter_module().LocalPinduoduoAdapter(tmp_path, bridge=bridge).search("桌面手机支架", 1)
    assert result["state"] == "ok" and result["count"] == 1
    assert result["items"][0]["url"] == "https://mobile.yangkeduo.com/goods2.html?goods_id=123456"
    assert result["items"][0]["price"] is None
    assert result["items"][0]["price_status"] == "not_exposed"


def run_loaded_ssr_search_script(item_price=646, risk=None, list_risk=None):
    import subprocess
    fixture = {"price": item_price, "risk": risk or {}, "listRisk": list_risk or {}}
    runner = r'''
const fixture = JSON.parse(process.argv[1]);
const item = {goodsID:'123456789',goodsName:'【领臣】手机金属支架折叠360度升降旋转支撑架桌面懒人通用直播追剧',price:fixture.price,salesTip:'本店已拼27.4万+'};
for(const key of ['logData','searchId','phoneRankInfo','linkURL'])
  Object.defineProperty(item,key,{enumerable:true,get(){throw new Error('forbidden context read: '+key);}});
const data = {...fixture.risk,ssrListData:{...fixture.listRisk,list:[item]}};
Object.defineProperty(data,'authContext',{enumerable:true,get(){throw new Error('auth context read');}});
global.window = {rawData:{stores:{store:{data}}},location:{origin:'https://mobile.yangkeduo.com',pathname:'/search_result.html',href:'https://mobile.yangkeduo.com/search_result.html?private_tracking=SECRET'}};
global.location = window.location;
global.document = {title:'桌面手机支架',body:{innerText:'¥\n6\n.46'},querySelector(){return null;},querySelectorAll(){throw new Error('SSR must take priority over generic div scan');}};
Object.defineProperty(document,'cookie',{get(){throw new Error('Cookie read');}});
for(const key of ['localStorage','sessionStorage']) Object.defineProperty(window,key,{get(){throw new Error('storage read');}});
global.fetch = () => {throw new Error('fetch called');};
global.XMLHttpRequest = function(){throw new Error('XHR called');};
console.log(eval(require('fs').readFileSync(0,'utf8')));
'''
    result = subprocess.run([shutil.which("node"), "-e", runner, json.dumps(fixture)], input=adapter_module().SEARCH_EXTRACT_JS, text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


def test_loaded_public_ssr_price_matches_observed_dom_fen_to_yuan_without_reading_context():
    raw = run_loaded_ssr_search_script()
    assert raw["itemCount"] == 1
    item = raw["items"][0]
    assert item["goodsId"] == "123456789"
    assert item["price"] == 6.46
    observed_dom_price = "¥\n6\n.46"
    assert observed_dom_price.replace("\n", "") == "¥6.46"
    assert item["price_unit"] == "yuan" and item["source_price_unit"] == "fen"
    assert item["price_source"] == "ssrListData.list.price"
    assert item["url"] == "https://mobile.yangkeduo.com/goods2.html?goods_id=123456789"
    assert item["sold"] is None and item["sold_status"] == "not_product_sales"
    assert item["sales_tip"] == "本店已拼27.4万+" and item["sales_tip_scope"] == "shop_cumulative"
    assert "SECRET" not in json.dumps(raw)


@pytest.mark.parametrize("flag", ["isRisk", "isBlack"])
@pytest.mark.parametrize("scope", ["data", "ssrListData"])
def test_loaded_ssr_risk_flags_stop_before_product_output(flag, scope):
    kwargs = {"risk" if scope == "data" else "list_risk": {flag: True}}
    raw = run_loaded_ssr_search_script(**kwargs)
    assert raw["riskControl"] is True
    assert raw["items"] == []


@pytest.mark.asyncio
async def test_ssr_metadata_survives_pure_parser_and_missing_price_remains_explicit(tmp_path):
    raw = run_loaded_ssr_search_script(item_price=None)
    bridge = FakeBridge(raw=raw)
    result = await adapter_module().LocalPinduoduoAdapter(tmp_path, bridge=bridge).search("桌面手机支架", 1)
    item = result["items"][0]
    assert item["price"] is None and item["price_status"] == "not_exposed"
    assert item["price_source"] == "ssrListData.list.price" and item["source_price_unit"] == "fen"
    assert item["sold"] is None and item["sold_status"] == "not_product_sales"
    assert item["sales_tip_scope"] == "shop_cumulative"


@pytest.mark.asyncio
async def test_business_read_preserves_existing_same_url_auth_form_before_navigation(tmp_path):
    current = "https://mobile.yangkeduo.com/goods2.html?goods_id=987654321"
    bridge = FakeBridge(tabs=[own_tab(current)], status={"authInputPresent": True, "loginRequired": True})
    result = await adapter_module().LocalPinduoduoAdapter(tmp_path, bridge=bridge).search("桌面手机支架", 1)
    assert result["code"] == "login_required"
    assert bridge.tabs[0]["url"] == current
    assert not any(action in {"navigate", "snapshot"} for action, args in bridge.calls)
    assert all("localPddStatus" in args["code"] for action, args in bridge.calls if action == "evaluate")


def run_visible_detail_script(description=None, auth=False):
    import subprocess
    fixture = {"description": description, "auth": auth}
    runner = r'''
const f=JSON.parse(process.argv[1]);
const input={disabled:false,getClientRects(){return [{}];}};
Object.defineProperty(input,'value',{get(){throw new Error('authentication value read');}});
const img={naturalWidth:320,naturalHeight:320,getClientRects(){return [{}];},getBoundingClientRect(){return {width:320,height:320};}};
Object.defineProperty(img,'src',{get(){if(f.auth)throw new Error('product extraction on auth page');return 'https://img.pddpic.com/public-product.jpg?private_sig=SECRET';}});
global.location={origin:'https://mobile.yangkeduo.com',pathname:'/goods2.html',href:'https://mobile.yangkeduo.com/goods2.html?goods_id=123456789'};
global.window={location,getComputedStyle(){return {display:'block',visibility:'visible'};}};
Object.defineProperty(window,'rawData',{get(){throw new Error('unnecessary state read');}});
global.document={title:'拼多多商城',body:{innerText:'大促价\n¥\n6\n.46\n限时直降 发起拼单'},
  querySelector(selector){
    if(selector==='meta[property="og:title"]')return {getAttribute(){return '拼多多商城';}};
    if(selector.includes('description') && f.description)return {getAttribute(){return f.description;}};
    return null;
  },
  querySelectorAll(selector){
    if(selector.includes('input['))return f.auth?[input]:[];
    if(selector==='img')return [img];
    return [];
  }
};
Object.defineProperty(document,'cookie',{get(){throw new Error('Cookie read');}});
global.fetch=()=>{throw new Error('fetch called');};
global.XMLHttpRequest=function(){throw new Error('XHR called');};
console.log(eval(require('fs').readFileSync(0,'utf8')));
'''
    result = subprocess.run([shutil.which("node"), "-e", runner, json.dumps(fixture)], input=adapter_module().DETAIL_EXTRACT_JS, text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


def test_generic_og_store_title_alone_is_not_authentication_evidence():
    api = adapter_module().LocalPinduoduoAdapter(bridge=FakeBridge())
    assert api._page_issue({"ogTitle": "拼多多商城"}, "https://mobile.yangkeduo.com/goods2.html?goods_id=123") is None


def test_detail_generic_og_and_visible_footer_price_do_not_invent_name_or_login():
    raw = run_visible_detail_script(description="拼多多商城，更多实惠")
    assert raw["loginGated"] is False
    assert raw["name"] is None and raw["name_status"] == "not_exposed"
    assert raw["price"] == 6.46 and raw["price_unit"] == "yuan"
    assert raw["image_url"] == "https://img.pddpic.com/public-product.jpg"
    assert raw["specs"] == [] and raw["specs_status"] == "missing"
    assert "SECRET" not in json.dumps(raw)


def test_detail_explicit_product_description_is_used_without_image_name_guess():
    raw = run_visible_detail_script(description="领臣折叠金属手机支架")
    assert raw["name"] == "领臣折叠金属手机支架" and raw["name_source"] == "meta_description"


def test_detail_visible_authentication_stops_before_product_fields_without_reading_values():
    raw = run_visible_detail_script(auth=True)
    assert raw["loginRequired"] is True and raw["loginGated"] is True
    assert not raw.get("image_url")


@pytest.mark.asyncio
async def test_same_product_page_is_reused_without_navigation_and_missing_name_is_partial(tmp_path):
    current = "https://mobile.yangkeduo.com/goods2.html?goods_id=123456789&share=existing"
    raw = run_visible_detail_script()
    bridge = FakeBridge(tabs=[own_tab(current)], raw=raw)
    api = adapter_module().LocalPinduoduoAdapter(tmp_path, bridge=bridge)
    result = await api.product("123456789")
    assert result["state"] == "partial"
    assert result["name"] is None and result["name_status"] == "not_exposed"
    assert result["price"] == 6.46 and result["image_url"] == "https://img.pddpic.com/public-product.jpg"
    assert result["goodsId"] == "123456789" and result["reused_current_page"] is True
    assert bridge.tabs[0]["url"] == current
    assert not any(action == "navigate" for action, args in bridge.calls)
    assert (await api.product("123456789"))["code"] == "cooldown"
