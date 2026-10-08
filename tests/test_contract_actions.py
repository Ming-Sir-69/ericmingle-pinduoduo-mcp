"""Offline regressions for safe shopping contracts; never contacts PDD."""
import json
import os
import shutil
import subprocess

import pytest
from test_local_adapter import adapter_module, FakeBridge, own_tab


@pytest.mark.asyncio
async def test_contract_keeps_legacy_fields_and_missing_detail_images(tmp_path):
    bridge = FakeBridge(raw={"name": "手机支架", "price": 2, "specs": [],
                             "image_url": "https://mobile.yangkeduo.com/goods2.html"})
    result = await adapter_module().LocalPinduoduoAdapter(tmp_path, bridge=bridge).product("123")
    assert result["ok"] is True and result["status"] == "partial"
    assert result["data"]["goodsId"] == result["goodsId"] == "123"
    assert result["image_url"] is None
    assert result["fields"]["images"] == result["fields"]["specs"] == "missing"


@pytest.mark.asyncio
async def test_persistent_risk_only_status_with_confirmed_login_clears(tmp_path):
    module = adapter_module()
    bridge = FakeBridge(tabs=[own_tab("https://mobile.yangkeduo.com/goods2.html?goods_id=123")], status={"riskControl": True})
    first = module.LocalPinduoduoAdapter(tmp_path, bridge=bridge)
    await first.status()
    restarted = module.LocalPinduoduoAdapter(tmp_path, bridge=bridge)
    assert (await restarted.product("123"))["status"] == "risk_control"
    bridge.status_data = {"loginHint": True}
    await restarted.status()
    assert restarted._risk_blocked is True
    bridge.status_data = {"loginVerified": True, "loginHint": True}
    await restarted.login()
    assert restarted._risk_blocked is True
    bridge.status_data["riskControl"] = True
    await restarted.status()
    assert restarted._risk_blocked is True
    bridge.status_data.pop("riskControl")
    assert (await restarted.status())["login_verified"] is True
    assert module.LocalPinduoduoAdapter(tmp_path, bridge=bridge)._risk_blocked is False


def run_detail(ssr, image="https://mobile.yangkeduo.com/goods2.html"):
    script = r'''
const f=JSON.parse(process.argv[1]);
global.location={origin:'https://mobile.yangkeduo.com',pathname:'/goods2.html',href:'https://mobile.yangkeduo.com/goods2.html?goods_id=123'};
const img={src:f.image,getClientRects(){return [{}]},getBoundingClientRect(){return {width:320,height:320}}};
global.window={location,rawData:f.ssr,getComputedStyle(){return {display:'block',visibility:'visible'}}};
global.document={title:'商品',body:{innerText:'手机支架 ¥2'},querySelector(){return null},querySelectorAll(s){return s==='img'?[img]:[]}};
Object.defineProperty(document,'cookie',{get(){throw new Error('Cookie read')}});
console.log(eval(require('fs').readFileSync(0,'utf8')));
'''
    execution = subprocess.run([shutil.which("node"), "-e", script, json.dumps({"ssr": ssr, "image": image})], input=adapter_module().DETAIL_EXTRACT_JS, text=True, capture_output=True, check=True)
    return json.loads(execution.stdout)


def test_actual_ssr_specs_and_html_image_rejection():
    raw = run_detail({"store": {"initDataObj": {"goods": {"goodsID": "123", "goodsName": "手机支架",
        "skus": [{"skuId": "777", "specs": [{"spec_key": "颜色", "spec_value": "白色"}]}]}}}})
    assert raw["specs"] == ["颜色：白色"]
    assert raw["specs_status"] == "ok"
    assert raw["image_url"] is None
    assert raw["skus"][0]["sku_id"] == "777"
    assert run_detail({})["specs_status"] == "missing"


@pytest.mark.asyncio
async def test_cart_unsupported_and_invalid_favorite_never_operate_browser(tmp_path):
    api = adapter_module().LocalPinduoduoAdapter(tmp_path, bridge=FakeBridge())
    result = await api.add_to_cart("123", qty=1)
    assert result["ok"] is False and result["fields"]["cart"] == "unsupported"
    assert result["error_code"] == "unsupported"
    assert (await api.favorite("https://evil.example/goods2.html?goods_id=123"))["error_code"] == "invalid_input"
    assert not api.bridge.calls


def test_favorite_script_cannot_click_purchase_or_wrong_product():
    module = adapter_module()
    runner = r'''
const f=JSON.parse(process.argv[1]);let clicked=0;
global.location={origin:'https://mobile.yangkeduo.com',pathname:'/goods2.html',href:'https://mobile.yangkeduo.com/goods2.html?goods_id='+f.id};
const button={textContent:f.label,getAttribute(){return null},getClientRects(){return [{}]},click(){clicked++}};
global.window={location,getComputedStyle(){return {display:'block',visibility:'visible'}}};
global.document={title:'商品',body:{innerText:'手机支架 ¥2'},querySelectorAll(s){return s.includes('input[')||s.includes('dialog')?[]:[button]}};
const result=JSON.parse(eval(require('fs').readFileSync(0,'utf8')));console.log(JSON.stringify({result,clicked}));
'''
    for fixture in [{"id": "123", "label": "立即购买"}, {"id": "456", "label": "收藏"}]:
        out = subprocess.run([shutil.which("node"), "-e", runner, json.dumps(fixture)], input=module.favorite_script("123", True), text=True, capture_output=True, check=True)
        assert json.loads(out.stdout)["clicked"] == 0


def run_favorite_fixture(fixture, wanted=True, click=True):
    runner = r'''
const f=JSON.parse(process.argv[1]);let clicks=0;
global.location={origin:f.origin||'https://mobile.yangkeduo.com',hostname:'mobile.yangkeduo.com',pathname:'/goods2.html',href:(f.origin||'https://mobile.yangkeduo.com')+'/goods2.html?goods_id='+(f.id||'123')};
const parent=f.sibling?{textContent:'收藏 立即购买',childNodes:[{nodeType:1,textContent:'收藏'},{nodeType:1,textContent:'立即购买'}],getAttribute(){return null},matches(){return false},parentElement:null}:f.parent?{textContent:f.parent,childNodes:f.parentNested?[{nodeType:1,textContent:f.parent}]:[{nodeType:3,textContent:f.parent}],getAttribute(s){return s==='href'?f.parentHref||null:null},matches(){return !!f.parentInteractive},parentElement:null}:null;
const buttons=(f.labels||[]).map(text=>({textContent:text,parentElement:parent,getAttribute(s){return s==='href'?f.href||null:null},getClientRects(){return [{}]},click(){clicks++}}));
const row={getClientRects(){return [{}]},querySelectorAll(){return buttons}};
const auth={disabled:false,getClientRects(){return [{}]}};
global.window={location,getComputedStyle(){return {display:'block',visibility:'visible'}}};
global.document={title:'手机支架',body:{innerText:f.risk?'安全验证':'手机支架'},querySelectorAll(s){
  if(s.includes('input['))return f.auth?[auth]:[];
  if(s.includes('dialog'))return [];
  if(s.includes('data-goods-id'))return Array(f.rows||0).fill(row);
  return buttons;
}};
const result=JSON.parse(eval(require('fs').readFileSync(0,'utf8')));console.log(JSON.stringify({result,clicks}));
'''
    out = subprocess.run([shutil.which("node"), "-e", runner, json.dumps(fixture)], input=adapter_module().favorite_script("123", wanted, click), text=True, capture_output=True, check=True)
    return json.loads(out.stdout)


@pytest.mark.parametrize("fixture", [
    {"labels": []}, {"labels": ["收藏", "收藏"]},
    {"labels": ["收藏"], "parent": "立即购买"},
    {"labels": ["收藏"], "href": "/checkout"},
    {"labels": ["收藏"], "href": "/buy_now.html"},
    {"labels": ["收藏"], "parent": "https://example.invalid/order.html"},
    {"labels": ["收藏"], "origin": "https://evil.example"},
    {"labels": ["收藏"], "id": "456"},
    {"labels": ["收藏"], "auth": True}, {"labels": ["收藏"], "risk": True},
])
def test_actual_favorite_script_rejects_unsafe_controls_before_click(fixture):
    assert run_favorite_fixture(fixture)["clicks"] == 0


def test_actual_favorite_script_clicks_once_or_confirms_existing_state():
    assert run_favorite_fixture({"labels": ["收藏"]})["clicks"] == 1
    existing = run_favorite_fixture({"labels": ["已收藏"]})
    assert existing["clicks"] == 0 and existing["result"]["confirmed"] is True
    assert run_favorite_fixture({"labels": ["收藏"]}, click=False)["clicks"] == 0


def test_unfavorite_exact_detail_explicit_state_and_ambiguous_controls():
    assert run_favorite_fixture({"labels": ["已收藏", "已收藏"]}, wanted=False)["clicks"] == 0
    assert run_favorite_fixture({"labels": ["已收藏"]}, wanted=False)["clicks"] == 1
    existing = run_favorite_fixture({"labels": ["收藏"]}, wanted=False)
    assert existing["clicks"] == 0 and existing["result"]["confirmed"] is True


class ActionBridge(FakeBridge):
    def __init__(self, *, confirmed=False, checkout=False):
        super().__init__(tabs=[own_tab("https://mobile.yangkeduo.com/goods2.html?goods_id=123")], raw={"name": "手机支架", "price": 2})
        self.confirmed = confirmed
        self.checkout = checkout
        self.action_clicks = 0

    async def command(self, action, args=None):
        args = args or {}
        if action == "evaluate" and adapter_module().is_favorite_script(args["code"]):
            self.calls.append((action, args))
            click = '"click":true' in args["code"]
            if click:
                self.action_clicks += 1
                if self.checkout:
                    self.tabs[0]["url"] = "https://mobile.yangkeduo.com/order_checkout.html"
            value = {"url": "https://mobile.yangkeduo.com/goods2.html", "clicked": click,
                     "confirmed": self.confirmed and self.action_clicks > 0, "can_click": True}
            return {"value": json.dumps(value)}
        return await super().command(action, args)


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed, checkout, expected", [(False, False, "unverified"), (True, False, "ok"), (False, True, "error")])
async def test_favorite_readback_once_and_checkout_stops(tmp_path, confirmed, checkout, expected):
    bridge = ActionBridge(confirmed=confirmed, checkout=checkout)
    result = await adapter_module().LocalPinduoduoAdapter(tmp_path, bridge=bridge).favorite("123")
    assert result["status"] == expected
    assert bridge.action_clicks == 1
    if checkout:
        assert result["error_code"] == "checkout_page_reached"


@pytest.mark.asyncio
async def test_favorite_lock_cooldown_and_cross_site_prevent_clicks(tmp_path):
    module = adapter_module()
    bridge = ActionBridge()
    api = module.LocalPinduoduoAdapter(tmp_path, bridge=bridge)
    api._navigation_cooldown()
    assert (await api.favorite("123"))["status"] == "cooldown"
    assert not bridge.calls
    api._risk_blocked = True
    assert (await api.favorite("123"))["status"] == "risk_control"
    assert not bridge.calls
    other = ActionBridge()
    other.tabs[0]["url"] = "https://evil.example/goods2.html?goods_id=123"
    out = await module.LocalPinduoduoAdapter(tmp_path / "other", bridge=other).favorite("123")
    assert out["error_code"] == "unexpected_redirect" and other.action_clicks == 0


def test_bridge_only_accepts_exact_generated_favorite_scripts():
    module = adapter_module()
    code = module.favorite_script("123", True, True)
    module.BridgeClient._validate_command("evaluate", {"code": code})
    for malicious in [code+';document.cookie', code.replace('target.click()', 'fetch("https://evil.example")')]:
        with pytest.raises(ValueError):
            module.BridgeClient._validate_command("evaluate", {"code": malicious})


@pytest.mark.parametrize("value", [
    "https://img.pddpic.com/item.jpg.html", "https://img.pddpic.com/item.jpg!evil.html",
    "https://mobile.yangkeduo.com/goods2.html", "https://evil.example/item.jpg",
    "https://img.pddpic.com/item.jpg?token=secret", "https://img.pddpic.com/item.webp",
])
def test_image_resource_urls_exclude_pages_and_strip_queries(value):
    out = adapter_module().image_url(value)
    if value.endswith(".webp"):
        assert out == value
    elif "?token" in value:
        assert out == "https://img.pddpic.com/item.jpg"
    else:
        assert out is None


def test_actual_status_only_explicit_account_control_confirms_login():
    runner = r'''
const mode=process.argv[1];const logout={textContent:'退出登录',disabled:false,getClientRects(){return [{}]}};
global.location={origin:'https://mobile.yangkeduo.com',pathname:'/personal.html'};
global.window={getComputedStyle(){return {display:'block',visibility:'visible'}}};
global.document={title:'个人中心',body:{innerText:'个人中心 商品标题：已登录可用'},querySelectorAll(s){return s==='button, a, [role="button"]' && mode==='explicit'?[logout]:[]}};
console.log(eval(require('fs').readFileSync(0,'utf8')));
'''
    for mode, verified in [("hint", False), ("explicit", True)]:
        out = subprocess.run([shutil.which("node"), "-e", runner, mode], input=adapter_module().STATUS_JS, text=True, capture_output=True, check=True)
        assert json.loads(out.stdout)["loginVerified"] is verified


@pytest.mark.asyncio
async def test_favorite_controls_disappearing_after_click_are_unverified(tmp_path):
    class DisappearingBridge(ActionBridge):
        async def command(self, action, args=None):
            args = args or {}
            if action == "evaluate" and adapter_module().is_favorite_script(args["code"]) and self.action_clicks > 0:
                self.calls.append((action, args))
                return {"value": json.dumps({"url": "https://mobile.yangkeduo.com/goods2.html", "issue": "unsupported"})}
            return await super().command(action, args)
    bridge = DisappearingBridge()
    out = await adapter_module().LocalPinduoduoAdapter(tmp_path, bridge=bridge).favorite("123")
    assert out["status"] == "unverified" and out["data"]["clicked"] is True
    assert bridge.action_clicks == 1


@pytest.mark.asyncio
async def test_local_watchlist_quotes_only_follow_service_reads(tmp_path):
    module = adapter_module()
    bridge = FakeBridge(raw={"name": "手机支架", "price": 2, "specs": []})
    api = module.LocalPinduoduoAdapter(tmp_path, bridge=bridge)
    canonical = "https://mobile.yangkeduo.com/goods2.html?goods_id=123"
    api.watchlist.upsert(canonical, "文具", 3)
    assert api.watchlist.check()["data"]["matches"] == []
    await api.product("123")
    matches = api.watchlist.check()["data"]["matches"]
    assert len(matches) == 1 and matches[0]["url"] == canonical and matches[0]["price"] == 2
    before = len(bridge.calls)
    api.watchlist.list("文具")
    api.watchlist.check()
    assert len(bridge.calls) == before


@pytest.mark.asyncio
async def test_quote_store_failure_does_not_fail_product_read(tmp_path):
    api = adapter_module().LocalPinduoduoAdapter(tmp_path, bridge=FakeBridge(raw={"name": "手机支架", "price": 2}))
    def unavailable(_):
        raise OSError("unavailable")
    api.watchlist.observe = unavailable
    output = await api.product("123")
    assert output["ok"] is True and output["price"] == 2


def test_favorite_sibling_buy_button_is_separate_but_interactive_ancestor_is_checked():
    assert run_favorite_fixture({"labels": ["收藏"], "sibling": True})["clicks"] == 1
    assert run_favorite_fixture({"labels": ["收藏"], "parent": "立即购买", "parentNested": True, "parentInteractive": True})["clicks"] == 0
    assert run_favorite_fixture({"labels": ["收藏"], "parent": "普通容器", "parentHref": "/checkout"})["clicks"] == 0
