"""Merchant requests remain fail-closed until a real conversation is evidenced."""
import json
import shutil
import subprocess

import pytest
from test_local_adapter import FakeBridge, adapter_module, own_tab

PRODUCT = "https://mobile.yangkeduo.com/goods2.html?goods_id=123"


@pytest.mark.asyncio
async def test_merchant_tools_explicit_object_annotations_and_no_cart_placeholders():
    tools = {tool.name: tool for tool in await adapter_module().mcp.list_tools()}
    assert "contact_merchant" in tools and "merchant_messages" in tools
    assert set(tools["contact_merchant"].inputSchema["properties"]) == {"url", "text"}
    assert tools["contact_merchant"].annotations.destructiveHint is True
    assert tools["contact_merchant"].annotations.openWorldHint is True
    assert tools["contact_merchant"].annotations.idempotentHint is False
    assert tools["merchant_messages"].annotations.readOnlyHint is True
    assert not {"add_to_cart", "cart_list", "remove_from_cart"} & tools.keys()


@pytest.mark.asyncio
@pytest.mark.parametrize("url,text", [
    ("123", "询问规格"), ("https://evil.example/goods2.html?goods_id=123", "询问规格"),
    (PRODUCT, ""), (PRODUCT, " " * 5), (PRODUCT, "x" * 501), (PRODUCT, None),
    (PRODUCT, "请问\x00规格"), (PRODUCT + "&goods_id=456", "询问规格"),
])
async def test_invalid_merchant_object_and_text_never_touch_browser(tmp_path, url, text):
    module = adapter_module()
    assert hasattr(module.LocalPinduoduoAdapter, "contact_merchant")
    api = module.LocalPinduoduoAdapter(tmp_path, bridge=FakeBridge())
    result = await api.contact_merchant(url, text)
    assert result["error_code"] == "invalid_input"
    assert not api.bridge.calls


@pytest.mark.asyncio
async def test_merchant_lock_cooldown_and_identity_stop_before_entry(tmp_path):
    module = adapter_module()
    assert hasattr(module.LocalPinduoduoAdapter, "contact_merchant")
    bridge = FakeBridge(tabs=[own_tab(PRODUCT)])
    api = module.LocalPinduoduoAdapter(tmp_path, bridge=bridge)
    api._risk_blocked = True
    assert (await api.contact_merchant(PRODUCT, "请问规格"))["error_code"] == "risk_control"
    assert not bridge.calls
    api._risk_blocked = False
    api._navigation_cooldown()
    assert (await api.contact_merchant(PRODUCT, "请问规格"))["error_code"] == "cooldown"
    assert not bridge.calls
    api = module.LocalPinduoduoAdapter(tmp_path, bridge=FakeBridge(tabs=[own_tab("https://evil.example/")]))
    assert (await api.contact_merchant(PRODUCT, "请问规格"))["error_code"] == "unexpected_redirect"


@pytest.mark.asyncio
async def test_known_entry_without_chat_evidence_cannot_send_or_return_fake_messages(tmp_path):
    module = adapter_module()
    assert hasattr(module.LocalPinduoduoAdapter, "contact_merchant")
    bridge = FakeBridge(tabs=[own_tab(PRODUCT)], raw={"goodsId": "123", "entry_available": True})
    api = module.LocalPinduoduoAdapter(tmp_path, bridge=bridge, clock=lambda: 100)
    text = "PRIVATE message body"
    result = await api.contact_merchant(PRODUCT, text)
    assert result["ok"] is False
    assert text not in json.dumps(bridge.calls)
    assert all(action not in {"click", "fill"} for action, _ in bridge.calls)
    api._last_business_navigation = None
    messages = await api.merchant_messages(PRODUCT)
    assert messages["ok"] is False


def run_entry(fixture):
    module = adapter_module()
    assert hasattr(module, "merchant_script")
    runner = r'''
const f=JSON.parse(process.argv[1]);let clicks=0;
global.location={origin:f.origin||'https://mobile.yangkeduo.com',pathname:'/goods2.html',href:(f.origin||'https://mobile.yangkeduo.com')+'/goods2.html?goods_id='+(f.id||'123')};
const parent={textContent:f.parent||'',childNodes:[],getAttribute(){return f.href||null},matches(){return false},parentElement:null};
const controls=(f.labels||[]).map(text=>({textContent:text,parentElement:parent,getAttribute(){return null},getClientRects(){return [{}]},click(){clicks++}}));
global.window={getComputedStyle(){return {display:'block',visibility:'visible'}}};
global.document={title:'商品',body:{innerText:f.risk?'安全验证':'商品'},querySelectorAll(s){if(s==='div.Lng9KDck > span')return controls;return []}};
Object.defineProperty(document,'cookie',{get(){throw new Error('Cookie read')}});
const result=JSON.parse(eval(require('fs').readFileSync(0,'utf8')));console.log(JSON.stringify({result,clicks}));
'''
    out = subprocess.run([shutil.which("node"), "-e", runner, json.dumps(fixture)], input=module.merchant_script("123"), text=True, capture_output=True, check=True)
    return json.loads(out.stdout)


@pytest.mark.parametrize("fixture,issue", [
    ({"labels": []}, "unsupported"), ({"labels": ["客服", "客服"]}, "ambiguous_control"),
    ({"labels": ["平台客服"]}, "unsupported"), ({"labels": ["客服"], "id": "456"}, "unexpected_redirect"),
    ({"labels": ["客服"], "origin": "https://evil.example"}, "unexpected_redirect"),
    ({"labels": ["客服"], "href": "/checkout"}, "unsafe_control"),
])
def test_real_entry_script_rejects_ambiguous_or_wrong_product_without_click(fixture, issue):
    out = run_entry(fixture)
    assert out["result"]["issue"] == issue and out["clicks"] == 0


def test_real_entry_script_detects_observed_entry_without_opening_session():
    out = run_entry({"labels": ["客服"]})
    assert out["result"]["entry_available"] is True and out["result"]["goodsId"] == "123"
    assert out["clicks"] == 0


def test_bridge_accepts_only_exact_generated_merchant_inspection():
    module = adapter_module()
    assert hasattr(module, "merchant_script")
    script = module.merchant_script("123")
    module.BridgeClient._validate_command("evaluate", {"code": script})
    for unsafe in [script + ";document.cookie", script.replace("entry_available:true", "entry_available:target.click()")]:
        with pytest.raises(ValueError):
            module.BridgeClient._validate_command("evaluate", {"code": unsafe})
