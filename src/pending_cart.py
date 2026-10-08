"""MCP-owned pending purchases validated against real PDD product reads.

This is neither a synchronized platform cart nor an order draft. No order,
checkout, payment or arbitrary platform request is issued by this module.
"""
import asyncio
from contextlib import contextmanager
import hashlib
import json
import math
import re
import sqlite3
import time
from urllib.parse import parse_qs, urlsplit


class PendingCart:
    def __init__(self, watchlist, read_product, validate_url, clock=time.time):
        self.store, self.read_product, self.validate_url, self.clock = watchlist, read_product, validate_url, clock
        self.lock = asyncio.Lock()

    @contextmanager
    def db(self):
        with self.store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS pdd_pending_cart(item_id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
            yield db

    def result(self, data, *, status='ok', error_code=None):
        output={'ok':status=='ok','status':status,'platform':'pinduoduo','scope':'mcp_pending_purchase',
                'platform_synced':False,'data':data,'fields':{},'message':'本MCP管理的本地待购车；非平台同步购物车，不创建订单。价格仅是观测展示价，库存未确认；请本人核对规格、运费与优惠后下单。'}
        if error_code: output['error_code']=error_code
        return output

    async def add(self, url_or_id, sku_text=None, qty=1):
        try:
            url=self.validate_url(url_or_id)
            if type(qty) is not int or not 1<=qty<=3:
                raise ValueError()
            if sku_text is not None and (not isinstance(sku_text,str) or not 1<=len(sku_text.strip())<=500):
                raise ValueError()
        except (ValueError,TypeError):
            return self.result({},status='error',error_code='invalid_input')
        async with self.lock:
            observed=await self.read_product(url)
            if not isinstance(observed,dict) or observed.get('ok') is not True:
                return observed if isinstance(observed,dict) else self.result({},status='error',error_code='product_unverified')
            product=observed.get('data') if isinstance(observed.get('data'),dict) else observed
            goods_id=parse_qs(urlsplit(url).query)['goods_id'][0]
            try:
                source_url=self.validate_url(product.get('url',''))
            except (ValueError,TypeError):
                source_url=None
            if product.get('goodsId')!=goods_id or source_url!=url or product.get('state') in {'not_found','error'}:
                return self.result({},status='error',error_code='product_unverified')
            amount=product.get('price')
            if type(amount) not in {int,float} or not math.isfinite(amount) or amount<0:
                amount=None
            title=product.get('name') if isinstance(product.get('name'),str) and product.get('name').strip() else None
            image=product.get('image_url') if isinstance(product.get('image_url'),str) else None
            if title is None and amount is None and image is None:
                return self.result({},status='error',error_code='product_unverified')
            skus=[]
            for sku in product.get('skus',[]):
                if not isinstance(sku,dict) or not re.fullmatch(r'[0-9]{1,20}',str(sku.get('sku_id',''))): continue
                specs=sku.get('specs',[])
                if not isinstance(specs,list) or not specs or any(not isinstance(s,str) or not s.strip() for s in specs): continue
                skus.append({'sku_id':str(sku['sku_id']),'text':' / '.join(specs)})
            selected=None
            if skus:
                if sku_text is None:
                    return self.result({'sku_options':skus},status='needs_sku')
                matches=[sku for sku in skus if sku['text']==sku_text]
                if len(matches)!=1:
                    return self.result({'sku_options':skus},status='error',error_code='sku_mismatch')
                selected=matches[0]
            elif sku_text is not None:
                return self.result({'sku_options':[]},status='error',error_code='sku_unknown')
            identity=url+'|'+(selected['sku_id'] if selected else '')+'|'+(selected['text'] if selected else '')
            item_id=hashlib.sha256(identity.encode()).hexdigest()
            item={'item_id':item_id,'url':url,'goods_id':goods_id,'qty':qty,
                  'sku_id':selected['sku_id'] if selected else None,'sku_text':selected['text'] if selected else None,
                  'sku_status':'verified' if selected else 'unknown','title':title,'display_price':amount,
                  'price_scope':'display_not_settlement','price_status':'observed' if amount is not None else 'unknown',
                  'price_is_from':bool(product.get('price_is_from')),'image_url':image,'observed_at':self.clock(),
                  'source':'pinduoduo_product_page','source_tool':'get_pinduoduo_product',
                  'stock_status':'sold_out' if product.get('soldOut') is True else 'unknown','platform_synced':False}
            try:
                with self.db() as db:
                    db.execute('INSERT INTO pdd_pending_cart VALUES(?,?) ON CONFLICT(item_id) DO UPDATE SET payload=excluded.payload',
                               (item_id,json.dumps(item,ensure_ascii=False,separators=(',',':'),allow_nan=False)))
            except (OSError,sqlite3.Error):
                return self.result({},status='error',error_code='local_store_unavailable')
            return self.result({'item':item,'quantity_semantics':'set_desired_quantity'})

    def list(self):
        try:
            with self.db() as db:
                rows=db.execute('SELECT payload FROM pdd_pending_cart ORDER BY item_id LIMIT 500').fetchall()
                count=db.execute('SELECT COUNT(*) FROM pdd_pending_cart').fetchone()[0]
            items=[json.loads(row[0]) for row in rows]
        except (OSError,sqlite3.Error,ValueError):
            return self.result({},status='error',error_code='local_store_unavailable')
        return self.result({'items':items,'count':count,'complete':count==len(items),'network_requested':False})

    def remove(self, item_id):
        if not isinstance(item_id,str) or not re.fullmatch(r'[a-f0-9]{64}',item_id):
            return self.result({},status='error',error_code='invalid_input')
        try:
            with self.db() as db:
                removed=db.execute('DELETE FROM pdd_pending_cart WHERE item_id=?',(item_id,)).rowcount==1
        except (OSError,sqlite3.Error):
            return self.result({},status='error',error_code='local_store_unavailable')
        return self.result({'item_id':item_id,'removed':removed,'already':not removed,'network_requested':False})


def register_pending_cart(mcp, cart):
    @mcp.tool(annotations={'readOnlyHint':False,'destructiveHint':False,'idempotentHint':True,'openWorldHint':True})
    async def pending_cart_add(url_or_id:str,sku_text:str|None=None,qty:int=1)->dict:
        """拼多多本地待购（非平台购物车）：真实读取指定商品后设置所需数量1–3。已知SKU须精确匹配，未知规格如实标记；不创建订单。"""
        return await cart.add(url_or_id,sku_text,qty)

    @mcp.tool(annotations={'readOnlyHint':True,'openWorldHint':False})
    async def pending_cart_list()->dict:
        """列出拼多多本地待购（非平台购物车）。展示价为入车时快照、非结算价，库存未知；不联网。"""
        return cart.list()

    @mcp.tool(annotations={'readOnlyHint':False,'destructiveHint':True,'idempotentHint':True,'openWorldHint':False})
    async def pending_cart_remove(item_id:str)->dict:
        """按精确item_id移除拼多多本地待购（非平台购物车）条目；不删除平台购物车、不联网。"""
        return cart.remove(item_id)
