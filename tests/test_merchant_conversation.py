import json
import shutil
import subprocess

import pytest
from test_local_adapter import FakeBridge, adapter_module, own_tab

URL = "https://mobile.yangkeduo.com/goods2.html?goods_id=123"
CHAT = "https://mobile.yangkeduo.com/chat_detail.html?goods_id=123&mall_sn=store123"


class ChatBridge(FakeBridge):
    def __init__(self, *, wrong=False, verified=False):
        super().__init__(tabs=[own_tab(URL)])
        self.sends = 0
        self.verified, self.wrong = verified, wrong

    async def command(self, action, args=None):
        args = args or {}
        if action == "evaluate" and 'const args = {"goods_id"' in args.get("code", ""):
            self.calls.append((action, args))
            code = args['code']
            if '"phase":"entry"' in code:
                if '"click":true' in code:
                    self.tabs[0]['url'] = CHAT.replace('goods_id=123','goods_id=456') if self.wrong else CHAT
                value = {'url':URL,'goodsId':'123','entry_available':True,'clicked':'"click":true' in code}
            elif '"phase":"send"' in code:
                self.sends += 1
                value = {'url':CHAT,'goodsId':'123','clicked':True,'message_count':0}
            else:
                value = {'url':CHAT,'goodsId':'123','merchant_identity_verified':True,'messages':[{'text':'请问规格'}] if self.verified and self.sends else [],'message_count':int(self.verified and self.sends>0)}
            return {'value':json.dumps(value)}
        return await super().command(action,args)


@pytest.mark.asyncio
@pytest.mark.parametrize('verified', [False,True])
async def test_send_once_readback_and_persistent_duplicate_protection(tmp_path, verified):
    module=adapter_module()
    bridge=ChatBridge(verified=verified)
    api=module.LocalPinduoduoAdapter(tmp_path,bridge=bridge)
    result=await api.contact_merchant(URL,'请问规格')
    assert result['status']==('ok' if verified else 'unverified')
    assert bridge.sends==1
    restarted=module.LocalPinduoduoAdapter(tmp_path,bridge=bridge)
    again=await restarted.contact_merchant(URL,'请问规格')
    assert again['status']==('ok' if verified else 'unverified') and bridge.sends==1


@pytest.mark.asyncio
async def test_native_entry_wrong_goods_stops_without_send(tmp_path):
    bridge=ChatBridge(wrong=True)
    out=await adapter_module().LocalPinduoduoAdapter(tmp_path,bridge=bridge).contact_merchant(URL,'请问规格')
    assert out['error_code']=='unexpected_redirect' and bridge.sends==0


@pytest.mark.asyncio
async def test_messages_reads_identified_existing_conversation(tmp_path):
    bridge=ChatBridge()
    bridge.tabs=[own_tab(CHAT)]
    out=await adapter_module().LocalPinduoduoAdapter(tmp_path,bridge=bridge).merchant_messages(URL)
    assert out['ok'] is True and out['messages']==[]
    assert bridge.sends==0


@pytest.mark.asyncio
async def test_existing_unbound_wrong_shop_is_replaced_with_native_entry(tmp_path):
    bridge=ChatBridge(verified=True)
    bridge.tabs=[own_tab(CHAT.replace('store123','otherstore'))]
    out=await adapter_module().LocalPinduoduoAdapter(tmp_path,bridge=bridge).contact_merchant(URL,'请问规格')
    assert out['ok'] is True
    assert any(action=='navigate' and args['url']==URL for action,args in bridge.calls)


@pytest.mark.asyncio
async def test_shop_changed_after_draft_never_sends(tmp_path):
    class ChangedShop(ChatBridge):
        async def command(self,action,args=None):
            value=await super().command(action,args)
            if action=='evaluate' and '"phase":"draft"' in (args or {}).get('code',''):
                self.tabs[0]['url']=CHAT.replace('store123','otherstore')
            return value
    bridge=ChangedShop(verified=True)
    out=await adapter_module().LocalPinduoduoAdapter(tmp_path,bridge=bridge).contact_merchant(URL,'请问规格')
    assert out['error_code']=='unexpected_redirect' and bridge.sends==0


@pytest.mark.parametrize('labels,expected', [([],False),(['发送','发送'],False),(['发送'],True)])
def test_send_script_unique_control_only_once(labels,expected):
    module=adapter_module()
    runner=r'''
const labels=JSON.parse(process.argv[1]);let clicks=0;
global.location={origin:'https://mobile.yangkeduo.com',pathname:'/chat_detail.html',href:'https://mobile.yangkeduo.com/chat_detail.html?goods_id=123&mall_sn=store123'};
const editor={value:'请问规格',disabled:false,getClientRects(){return [{}]},dispatchEvent(){},getAttribute(){return null}};
const provider={children:[],getClientRects(){return [{}]}};
const controls=labels.map(text=>({textContent:text,parentElement:null,getAttribute(){return null},getClientRects(){return [{}]},querySelectorAll(){return []},click(){clicks++}}));
global.Event=class {};
global.window={getComputedStyle(){return {display:'block',visibility:'visible'}}};
global.document={title:'客服',body:{innerText:''},querySelectorAll(s){if(s==='textarea.input-content')return [editor];if(s==='div.list-container.chat-msg-provider')return [provider];if(s==='div.chat-input-provider > div.text-container > div.send-button')return controls;return []}};
const result=JSON.parse(eval(require('fs').readFileSync(0,'utf8')));console.log(JSON.stringify({result,clicks}));
'''
    try: code=module.merchant_script('123',phase='send',text='请问规格',mall_sn='store123')
    except TypeError: pytest.fail('conversation send script not implemented')
    out=subprocess.run([shutil.which('node'),'-e',runner,json.dumps(labels)],input=code,text=True,capture_output=True,check=True)
    assert json.loads(out.stdout)['clicks']==int(expected)
