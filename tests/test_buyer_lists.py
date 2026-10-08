"""Necessary buyer search contracts, entirely offline."""
import pytest
from test_local_adapter import FakeBridge, adapter_module


@pytest.mark.asyncio
@pytest.mark.parametrize('sort,expected', [('price_asc',['2','1','3']), ('price_desc',['1','2','3'])])
async def test_search_sorts_loaded_results_before_limiting_and_places_unknown_last(tmp_path, sort, expected):
    bridge = FakeBridge(raw={'itemCount': 3, 'items': [
        {'goodsId':'1','name':'高价商品','price':20},
        {'goodsId':'2','name':'低价商品','price':5},
        {'goodsId':'3','name':'未知价格商品','price':None}]})
    result = await adapter_module().LocalPinduoduoAdapter(tmp_path, bridge=bridge).search('商品', 3, sort=sort)
    assert result['ok'] is True
    assert [x['goodsId'] for x in result['items']] == expected
    assert result['sort_scope'] == 'page_local'


@pytest.mark.asyncio
async def test_search_invalid_sort_returns_before_browser_work(tmp_path):
    bridge = FakeBridge()
    result = await adapter_module().LocalPinduoduoAdapter(tmp_path, bridge=bridge).search('商品', sort='bogus')
    assert result['error_code'] == 'invalid_input'
    assert bridge.calls == []


@pytest.mark.asyncio
async def test_favorite_list_reports_observed_app_boundary_without_navigating(tmp_path):
    bridge = FakeBridge()
    result = await adapter_module().LocalPinduoduoAdapter(tmp_path, bridge=bridge).favorite_list()
    assert result['status'] == 'unsupported' and result['ok'] is False
    assert result['scope'] == 'platform_native'
    assert result['evidence']['observed_path'] == '/portal.html'
    assert result['alternative'] == 'watchlist_list'
    assert bridge.calls == []


@pytest.mark.asyncio
async def test_conversation_list_reads_official_page_rows_and_redacts(tmp_path):
    bridge = FakeBridge(raw={'conversations': [
        {'name':'测试店铺13812345678','last_time':'09:30','last_message':'验证码:123456','unread':2}],
        'empty':False})
    result = await adapter_module().LocalPinduoduoAdapter(tmp_path, bridge=bridge).conversation_list()
    assert result['ok'] is True
    assert result['count'] == 1
    row = result['conversations'][0]
    assert row['name'] == '测试店铺[redacted]' and row['unread'] == 2
    assert '123456' not in row['last_message']
    assert [x[1]['url'] for x in bridge.calls if x[0]=='navigate'] == ['https://mobile.yangkeduo.com/chat_list.html']
    assert all(x[0] not in {'click','fill'} for x in bridge.calls)


@pytest.mark.asyncio
async def test_conversation_list_does_not_claim_empty_for_unknown_structure(tmp_path):
    result = await adapter_module().LocalPinduoduoAdapter(tmp_path, bridge=FakeBridge(raw={'conversations':[],'empty':False})).conversation_list()
    assert result['error_code'] == 'parse_error'


@pytest.mark.asyncio
async def test_conversation_list_rejects_redirect_to_other_official_page(tmp_path):
    bridge = FakeBridge(raw={'conversations':[{'name':'推荐商品'}]}, redirected_url='https://mobile.yangkeduo.com/portal.html')
    result = await adapter_module().LocalPinduoduoAdapter(tmp_path, bridge=bridge).conversation_list()
    assert result['error_code'] == 'unexpected_redirect'


def test_real_conversation_extractor_reads_visible_rows_and_avoids_credentials():
    import json
    import shutil
    import subprocess
    script = r'''
global.location={origin:'https://mobile.yangkeduo.com',pathname:'/chat_list.html'};
global.window={getComputedStyle(){return {display:'block',visibility:'visible'}}};
const row={getAttribute(){return null},querySelectorAll(){return []},getClientRects(){return [{}]},querySelector(s){if(s==='.msg-detail')return {innerText:'测试商家\n09:30\n规格消息'};return null}};
global.document={title:'聊天',body:{innerText:'聊天'},querySelectorAll(s){return s==='.msg-box'?[row]:[]}};
Object.defineProperty(document,'cookie',{get(){throw new Error('credential read')}});
console.log(eval(require('fs').readFileSync(0,'utf8')));
'''
    result = subprocess.run([shutil.which('node'),'-e',script], input=adapter_module().CONVERSATION_LIST_JS, capture_output=True, text=True, check=True)
    raw = json.loads(result.stdout)
    assert raw['conversations'] == [{'name':'测试商家','last_time':'09:30','last_message':'规格消息','unread':None,'goodsId':None,'source':'visible_msg_box'}]


@pytest.mark.asyncio
async def test_search_loaded_only_pages_never_guess_platform_url(tmp_path):
    bridge = FakeBridge(raw={'itemCount':4,'items':[{'goodsId':str(x),'name':f'商品{x}','price':x} for x in range(1,5)]})
    api = adapter_module().LocalPinduoduoAdapter(tmp_path, bridge=bridge)
    result = await api.search('商品', 2, page=2)
    assert [x['goodsId'] for x in result['items']] == ['3','4']
    assert result['page_scope'] == 'loaded_only' and result['has_next_page'] is False
    assert all('page=' not in args['url'] for action,args in bridge.calls if action=='navigate')
    api._last_business_navigation = None
    result = await api.search('商品', 2, page=3)
    assert result['status'] == 'unsupported' and result['reason'] == 'outside_loaded_results'


@pytest.mark.asyncio
async def test_confirmed_favorite_records_are_separate_and_persistent(tmp_path):
    from test_contract_actions import ActionBridge
    api = adapter_module().LocalPinduoduoAdapter(tmp_path, bridge=ActionBridge(confirmed=True))
    assert (await api.favorite('123'))['ok'] is True
    restarted = adapter_module().LocalPinduoduoAdapter(tmp_path, bridge=FakeBridge())
    result = await restarted.favorite_list()
    assert result['ok'] is True and result['platform_full_list'] is False
    assert result['source'] == 'mcp_confirmed_actions'
    assert result['items'][0]['goodsId'] == '123'
    assert restarted.watchlist.list()['data']['items'] == []
    api._last_business_navigation = None
    assert (await api.favorite('123', False))['ok'] is True
    assert (await restarted.favorite_list())['items'] == []


@pytest.mark.asyncio
async def test_unconfirmed_favorite_does_not_create_confirmed_record(tmp_path):
    from test_contract_actions import ActionBridge
    api = adapter_module().LocalPinduoduoAdapter(tmp_path, bridge=ActionBridge(confirmed=False))
    assert (await api.favorite('123'))['ok'] is False
    assert (await api.favorite_list())['status'] == 'unsupported'


@pytest.mark.asyncio
async def test_conversation_locators_accept_only_observed_numeric_product_id(tmp_path):
    bridge = FakeBridge(raw={'conversations':[{'name':'商家','goodsId':'123'},{'name':'另一商家','goodsId':'123&token=secret'}]})
    result = await adapter_module().LocalPinduoduoAdapter(tmp_path, bridge=bridge).conversation_list()
    assert result['conversations'][0]['product_url'] == 'https://mobile.yangkeduo.com/goods2.html?goods_id=123'
    assert result['conversations'][1]['product_url'] is None
    assert result['locator_status'] == 'partial'
