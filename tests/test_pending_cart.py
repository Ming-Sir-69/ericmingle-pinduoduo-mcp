"""Local pending purchases always originate in this service's observed product."""
import importlib.util
from pathlib import Path

import pytest
from test_local_adapter import adapter_module, FakeBridge


def module():
    path=Path(__file__).parents[1]/'src/pending_cart.py'
    assert path.exists(), 'pending-purchase cart is not implemented'
    spec=importlib.util.spec_from_file_location('pdd_cart_test',path)
    obj=importlib.util.module_from_spec(spec);spec.loader.exec_module(obj)
    return obj


def cart(tmp_path, result=None):
    mod=adapter_module()
    api=mod.LocalPinduoduoAdapter(tmp_path,bridge=FakeBridge())
    observed=result or {'ok':True,'status':'partial','goodsId':'123','url':'https://mobile.yangkeduo.com/goods2.html?goods_id=123','name':None,'price':2.5,'image_url':'https://img.pddpic.com/item.jpg','skus':[]}
    calls=[]
    async def read(url):
        calls.append(url)
        return observed
    obj=module().PendingCart(api.watchlist,read,mod.canonical_product_url,clock=lambda:1000)
    return obj,calls


@pytest.mark.asyncio
async def test_add_uses_observed_product_fields_and_marks_local_unknown_stock(tmp_path):
    store,calls=cart(tmp_path)
    result=await store.add('123',qty=2)
    item=result['data']['item']
    assert calls==['https://mobile.yangkeduo.com/goods2.html?goods_id=123']
    assert result['scope']=='mcp_pending_purchase' and result['platform_synced'] is False
    assert item['qty']==2 and item['display_price']==2.5 and item['title'] is None
    assert item['source']=='pinduoduo_product_page' and item['stock_status']=='unknown'
    assert item['price_scope']=='display_not_settlement' and item['sku_status']=='unknown'


@pytest.mark.asyncio
@pytest.mark.parametrize('url,qty',[('https://evil.example/goods2.html?goods_id=123',1),('123',True),('123',0),('123',4)])
async def test_invalid_input_never_reads_or_mutates(tmp_path,url,qty):
    store,calls=cart(tmp_path)
    result=await store.add(url,qty=qty)
    assert result['error_code']=='invalid_input' and not calls
    assert store.list()['data']['items']==[]


@pytest.mark.asyncio
@pytest.mark.parametrize('observed',[
    {'ok':False,'status':'risk_control','error_code':'risk_control'},
    {'ok':True,'goodsId':'456','url':'https://mobile.yangkeduo.com/goods2.html?goods_id=456','price':2},
    {'ok':True,'goodsId':'123','url':'https://mobile.yangkeduo.com/goods2.html?goods_id=123','name':None,'price':None,'image_url':None},
])
async def test_failed_or_mismatched_observation_never_enters_cart(tmp_path,observed):
    store,_=cart(tmp_path,observed)
    result=await store.add('123')
    assert result['ok'] is False and store.list()['data']['items']==[]


@pytest.mark.asyncio
async def test_sku_required_exact_unique_and_not_invented(tmp_path):
    observed={'ok':True,'goodsId':'123','url':'https://mobile.yangkeduo.com/goods2.html?goods_id=123','price':2,'skus':[{'sku_id':'7','specs':['颜色：白色','大小：小']},{'sku_id':'8','specs':['颜色：黑色','大小：小']}]}
    store,_=cart(tmp_path,observed)
    assert (await store.add('123'))['status']=='needs_sku'
    assert not store.list()['data']['items']
    assert (await store.add('123',sku_text='白色'))['error_code']=='sku_mismatch'
    result=await store.add('123',sku_text='颜色：白色 / 大小：小')
    assert result['data']['item']['sku_id']=='7' and result['data']['item']['sku_status']=='verified'
    empty,_=cart(tmp_path/'empty')
    assert (await empty.add('123',sku_text='白色'))['error_code']=='sku_unknown'


@pytest.mark.asyncio
async def test_add_set_quantity_is_idempotent_and_persists_remove_exact_id(tmp_path):
    store,_=cart(tmp_path)
    first=await store.add('123',qty=2)
    await store.add('123',qty=2)
    restarted,_=cart(tmp_path)
    assert len(restarted.list()['data']['items'])==1
    assert restarted.list()['data']['items'][0]['qty']==2
    updated=await restarted.add('123',qty=3)
    assert updated['data']['item']['item_id']==first['data']['item']['item_id']
    assert restarted.list()['data']['items'][0]['qty']==3
    assert restarted.remove('123')['error_code']=='invalid_input'
    assert len(restarted.list()['data']['items'])==1
    assert restarted.remove(first['data']['item']['item_id'])['data']['removed'] is True
    assert restarted.list()['data']['items']==[]


@pytest.mark.asyncio
async def test_adapter_real_detail_reader_is_used_for_local_cart(tmp_path):
    api=adapter_module().LocalPinduoduoAdapter(tmp_path,bridge=FakeBridge(raw={'name':'真实支架','price':2,'specs':[]}))
    assert hasattr(api,'pending_cart')
    result=await api.pending_cart.add('123')
    assert result['ok'] is True and result['data']['item']['title']=='真实支架'
    assert any(action=='evaluate' and args['code']==adapter_module().DETAIL_EXTRACT_JS for action,args in api.bridge.calls)


@pytest.mark.asyncio
async def test_detail_official_redirect_to_other_goods_cannot_be_trusted(tmp_path):
    bridge=FakeBridge(raw={'name':'别的商品','price':2,'specs':[]},redirected_url='https://mobile.yangkeduo.com/goods2.html?goods_id=456')
    result=await adapter_module().LocalPinduoduoAdapter(tmp_path,bridge=bridge).product('123')
    assert result['error_code']=='unexpected_redirect' and result['ok'] is False


@pytest.mark.asyncio
async def test_detail_identity_changed_during_extraction_is_rejected(tmp_path):
    bridge=FakeBridge(raw={'name':'另一个商品','price':2,'specs':[],'observed_goods_id':'456'})
    result=await adapter_module().LocalPinduoduoAdapter(tmp_path,bridge=bridge).product('123')
    assert result['error_code']=='unexpected_redirect' and result['ok'] is False


@pytest.mark.asyncio
async def test_local_cart_tools_have_explicit_names_and_correct_write_annotations():
    tools={tool.name:tool for tool in await adapter_module().mcp.list_tools()}
    assert {'pending_cart_add','pending_cart_list','pending_cart_remove'}<=tools.keys()
    assert tools['pending_cart_add'].annotations.idempotentHint is True
    assert tools['pending_cart_list'].annotations.readOnlyHint is True
    assert tools['pending_cart_remove'].annotations.destructiveHint is True
    assert set(tools['pending_cart_add'].inputSchema['properties'])=={'url_or_id','sku_text','qty'}
