"""Read-only merchant entry inspection from observed PDD detail-page DOM.

Evidence: 2026-10-08 r2-detail-evidence.json, div.Lng9KDck > span 客服.
Conversation evidence: chat_detail.html?goods_id,mall_sn; textarea.input-content
and div.list-container.chat-msg-provider. Send is exact visible 发送, only
after explicit text input, as approved in the round-2 review.
"""
import json
import re

ENTRY_JS = r"""(() => {
  const args = __ARGS__;
  const status = JSON.parse(__STATUS__);
  const base = {...status,url:location.origin+location.pathname};
  if (status.loginRequired || status.riskControl || status.notSupported) return JSON.stringify(base);
  const actual = new URL(location.href);
  const visible = el => {
    if (!el.getClientRects().length) return false;
    const css = window.getComputedStyle(el);
    return css.display!=='none' && css.visibility!=='hidden' && css.visibility!=='collapse';
  };
  const safe = target => {
    const forbidden = /立即购买|购买|结算|提交订单|付款|支付|领券购买|checkout|buy|confirm_order|order/i;
    for(let node=target;node;node=node.parentElement) {
      const interactive = node.matches && node.matches('button, a, [role="button"]');
      const own = node===target || interactive || !node.childNodes ? (node.textContent||'') : [...node.childNodes].filter(n=>n.nodeType===3).map(n=>n.textContent||'').join(' ');
      if(forbidden.test(own+' '+(node.getAttribute('href')||'')+' '+(node.getAttribute('action')||'')+' '+(node.getAttribute('aria-label')||''))) return false;
    }
    return true;
  };
  if(args.phase!=='entry') {
    if(actual.protocol!=='https:' || actual.hostname!=='mobile.yangkeduo.com' || actual.username || actual.password || actual.port && actual.port!=='443' ||
       actual.pathname!=='/chat_detail.html' || actual.searchParams.getAll('goods_id').length!==1 || actual.searchParams.get('goods_id')!==args.goods_id ||
       actual.searchParams.getAll('mall_sn').length!==1 || !args.mall_sn || actual.searchParams.get('mall_sn')!==args.mall_sn) return JSON.stringify({...base,issue:'unexpected_redirect'});
    const containers=[...document.querySelectorAll('div.list-container.chat-msg-provider')].filter(visible);
    if(containers.length!==1) return JSON.stringify({...base,issue:'ambiguous_control'});
    const rows=[...containers[0].children].map(el=>(el.textContent||'').trim()).filter(Boolean);
    const messages=rows.slice(-20).map(text=>({text:text.slice(0,1000)}));
    const result={...base,goodsId:args.goods_id,merchant_identity_verified:true,messages,message_count:rows.length};
    if(args.phase==='read') return JSON.stringify(result);
    const editors=[...document.querySelectorAll('textarea.input-content')].filter(el=>visible(el)&&!el.disabled);
    if(editors.length!==1 || (args.phase==='draft' ? !!editors[0].value : editors[0].value!==args.text)) return JSON.stringify({...result,issue:'ambiguous_control'});
    if(!safe(editors[0])) return JSON.stringify({...result,issue:'unsafe_control'});
    const editor=editors[0];
    if(args.phase==='draft') {
      const setter=typeof HTMLTextAreaElement!=='undefined' && Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set;
      if(setter) setter.call(editor,args.text); else editor.value=args.text;
      editor.dispatchEvent(new Event('input',{bubbles:true}));
      editor.dispatchEvent(new Event('change',{bubbles:true}));
      return JSON.stringify({...result,input_written:true});
    }
    const send=[...document.querySelectorAll('div.chat-input-provider > div.text-container > div.send-button')].filter(el=>visible(el)&&(el.textContent||'').trim()==='发送');
    if(send.length!==1) return JSON.stringify({...result,input_written:true,issue:send.length>1?'ambiguous_control':'unsupported'});
    if(!safe(send[0])) return JSON.stringify({...result,input_written:true,issue:'unsafe_control'});
    send[0].click();
    return JSON.stringify({...result,clicked:true,input_written:true});
  }
  if (actual.protocol!=='https:' || actual.hostname!=='mobile.yangkeduo.com' || actual.username || actual.password ||
      actual.port && actual.port!=='443' || !['/goods2.html','/goods.html'].includes(actual.pathname) ||
      actual.searchParams.getAll('goods_id').length!==1 || actual.searchParams.get('goods_id')!==args.goods_id)
    return JSON.stringify({...base,issue:'unexpected_redirect'});
  const candidates = [...document.querySelectorAll('div.Lng9KDck > span')]
    .filter(el=>visible(el) && (el.textContent||'').trim()==='客服');
  if(candidates.length!==1) return JSON.stringify({...base,issue:candidates.length>1?'ambiguous_control':'unsupported'});
  const target = candidates[0];
  const forbidden = /立即购买|购买|结算|提交订单|付款|支付|领券购买|checkout|buy|confirm_order|order/i;
  for(let node=target;node;node=node.parentElement) {
    const interactive = node.matches && node.matches('button, a, [role="button"]');
    const own = node===target || interactive || !node.childNodes ? (node.textContent||'') : [...node.childNodes].filter(n=>n.nodeType===3).map(n=>n.textContent||'').join(' ');
    if(forbidden.test(own+' '+(node.getAttribute('href')||'')+' '+(node.getAttribute('action')||'')+' '+(node.getAttribute('aria-label')||'')))
      return JSON.stringify({...base,issue:'unsafe_control'});
  }
  if(args.click) target.click();
  return JSON.stringify({...base,goodsId:args.goods_id,entry_available:true,clicked:args.click,merchant_identity_verified:false});
})()"""


def build_script(goods_id, status_script, phase="entry", text="", click=False, mall_sn=""):
    if not isinstance(goods_id, str) or not re.fullmatch(r"[0-9]{1,20}", goods_id):
        raise ValueError("invalid product identity")
    if phase not in {"entry", "read", "draft", "send"} or type(click) is not bool or not isinstance(text,str) or len(text)>500 or phase in {"draft","send"} and not text.strip() or not isinstance(mall_sn,str) or len(mall_sn)>512:
        raise ValueError("invalid merchant action")
    return ENTRY_JS.replace("__STATUS__", status_script).replace("__ARGS__", json.dumps({"goods_id": goods_id,"phase":phase,"text":text,"click":click,"mall_sn":mall_sn},separators=(",",":"),ensure_ascii=False))


def matches_script(code, status_script):
    if not isinstance(code, str) or len(code) > 30000:
        return False
    match = re.search(r'const args = (\{"goods_id":"[0-9]{1,20}","phase":.*?\});\n', code)
    if not match: return False
    try:
        args=json.loads(match.group(1))
        return code==build_script(args['goods_id'],status_script,args['phase'],args['text'],args['click'],args['mall_sn'])
    except (ValueError,KeyError,TypeError): return False
