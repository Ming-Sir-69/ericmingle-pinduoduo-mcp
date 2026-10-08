"""Observed PDD React control is recognized without relying on obfuscated classes."""
import json
import shutil
import subprocess
import pytest
from test_local_adapter import adapter_module, FakeBridge


def run_native(*, current='收藏', wanted=True, click=True, handler=True, child_handler=False, toast=False, duplicate=False, app=False):
    fixture=dict(current=current,handler=handler,child_handler=child_handler,toast=toast,duplicate=duplicate,app=app)
    runner=r'''
const f=JSON.parse(process.argv[1]);let clicks=0;
global.location={origin:'https://mobile.yangkeduo.com',hostname:'mobile.yangkeduo.com',pathname:'/goods2.html',href:'https://mobile.yangkeduo.com/goods2.html?goods_id=123'};
const root={tagName:'DIV',textContent:f.current,parentElement:null,childNodes:[],getAttribute(k){return k==='class'?(f.toast?'toast':'obfuscated-current'):null},getClientRects(){return [{}]},matches(){return false},click(){clicks++}};
if(f.handler)root.__reactProps$sample={onClick(){}};
const span={...root,tagName:'SPAN',parentElement:root};delete span.__reactProps$sample;
if(f.child_handler)span.__reactProps$child={onClick(){}};
root.childNodes=[{nodeType:1,textContent:f.current}];
const unrelated={...root};if(f.handler)unrelated.__reactProps$sample={onClick(){}};
global.window={getComputedStyle(){return {display:'block',visibility:'visible'}}};
global.document={title:'商品',body:{innerText:f.app?'前往APP查看价格':'商品'},querySelectorAll(s){if(s.includes('input[')||s.includes('dialog'))return [];if(s==='button, a, [role="button"], [aria-label]')return [];return f.duplicate?[root,span,unrelated]:[root,span]}};
console.log(JSON.stringify({result:JSON.parse(eval(require('fs').readFileSync(0,'utf8'))),clicks}));
'''
    out=subprocess.run([shutil.which('node'),'-e',runner,json.dumps(fixture)],input=adapter_module().favorite_script('123',wanted,click),text=True,capture_output=True,check=True)
    return json.loads(out.stdout)


def test_native_react_ancestor_and_span_are_one_clickable_control():
    assert run_native()['clicks']==1
    assert run_native(child_handler=True)['clicks']==1
    assert run_native(current='已收藏')['result']['confirmed'] is True
    assert run_native(current='已收藏',wanted=False)['clicks']==1
    assert run_native(current='收藏',wanted=False)['result']['confirmed'] is True


@pytest.mark.parametrize('fixture,issue', [({'handler':False},'unsupported'),({'toast':True},'unsupported'),({'duplicate':True},'ambiguous_control')])
def test_plain_text_toast_and_duplicate_controls_never_click(fixture,issue):
    out=run_native(**fixture)
    assert out['clicks']==0 and out['result']['issue']==issue


def test_app_downgrade_is_explicit_unsupported_with_evidence():
    out=run_native(app=True)
    assert out['clicks']==0 and out['result']['issue']=='unsupported'
    assert out['result']['evidence']['app_prompt']=='前往APP查看价格'


@pytest.mark.asyncio
async def test_downgraded_detail_returns_app_evidence_without_a_click(tmp_path):
    bridge=FakeBridge(raw={'appRequired':True,'name':'测试商品'})
    result=await adapter_module().LocalPinduoduoAdapter(tmp_path,bridge=bridge).favorite('123')
    assert result['status']=='unsupported'
    assert result['evidence']['app_prompt']=='前往APP查看价格'
    assert all(not adapter_module().is_favorite_script(args.get('code')) for action,args in bridge.calls if action=='evaluate')
