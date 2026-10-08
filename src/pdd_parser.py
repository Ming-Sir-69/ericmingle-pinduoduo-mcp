"""Pure parser extracts from goesByhc/cn-scraper-mcp, MIT.
Copyright (c) 2026 goes_by. See docs/UPSTREAM-LICENSE.txt.
Only constants and result parsers; no browser/authentication implementation.
"""
from typing import Any

class PDDRateLimitError(Exception):
    """PDD rate-limited the session — '系统繁忙' detected.

    This is the single-search limitation: PDD allows ONE query per
    browser session. After that, every query returns '系统繁忙'.
    You MUST create a new PDDEngine instance to search again.
    """
    pass

class PDDAuthError(Exception):
    """PDD cookie expired or invalid — login required.

    PDDAccessToken + pdd_user_id are short-lived (~1 hour).
    Re-harvest cookies from a logged-in mobile browser session.
    """
    pass
SEARCH_EXTRACT_JS = '\n(function(){\n  // PDD mobile search page data extraction\n  var bodyText = (document.body && document.body.innerText || \'\').substring(0, 5000);\n  var url = window.location.href;\n  var title = document.title || \'\';\n\n  // ── Rate-limit detection ─────────────────────────\n  var rateLimited = false;\n  if (bodyText.indexOf(\'系统繁忙\') >= 0 || bodyText.indexOf(\'网络异常\') >= 0) {\n    rateLimited = true;\n  }\n\n  // ── Login redirect detection ─────────────────────\n  var ogTitle = \'\';\n  var ogMeta = document.querySelector(\'meta[property="og:title"]\');\n  if (ogMeta) ogTitle = ogMeta.getAttribute(\'content\') || \'\';\n\n  // ── Extract product items ────────────────────────\n  var items = [];\n\n  // PDD mobile search results are often in divs with data-goods-id or class containing \'goods\'\n  var goodsCards = document.querySelectorAll(\'[data-goods-id], .goods-item, [class*="goods-item"], [class*="search-result"] div[class*="goods"]\');\n  if (goodsCards.length === 0) {\n    // Fallback: scan all divs looking for ¥ price patterns\n    goodsCards = document.querySelectorAll(\'div\');\n  }\n\n  goodsCards.forEach(function(el){\n    var text = el.innerText || \'\';\n    // Quick check: has a ¥ price\n    if (text.indexOf(\'¥\') < 0 && text.indexOf(\'￥\') < 0) return;\n\n    var goodsId = el.getAttribute(\'data-goods-id\') || \'\';\n\n    // Try to find name — first line before price\n    var lines = text.split(\'\\n\').map(function(l){ return l.trim(); }).filter(function(l){ return l.length > 0; });\n    var name = \'\';\n    var price = null;\n    var sold = 0;\n\n    for (var i = 0; i < lines.length; i++) {\n      var line = lines[i];\n      var priceMatch = line.match(/[¥￥]\\s*([\\d,.]+)/);\n      if (priceMatch && !price) {\n        price = parseFloat(priceMatch[1].replace(/,/g, \'\'));\n        // Name is the line before the price\n        if (i > 0 && lines[i-1].indexOf(\'¥\') < 0 && lines[i-1].indexOf(\'￥\') < 0) {\n          name = lines[i-1];\n        }\n      }\n      // Sold count\n      var soldMatch = line.match(/(\\d+[\\.\\d]*万?\\+?)\\s*件/);\n      if (soldMatch && !sold) {\n        var s = soldMatch[1];\n        if (s.indexOf(\'万\') >= 0) {\n          sold = Math.round(parseFloat(s) * 10000);\n        } else {\n          sold = parseInt(s.replace(\'+\', \'\'), 10);\n        }\n      }\n    }\n\n    // Fallback name from first meaningful line\n    if (!name && lines.length > 0) {\n      for (var j = 0; j < Math.min(lines.length, 5); j++) {\n        var l = lines[j];\n        if (l.indexOf(\'¥\') < 0 && l.indexOf(\'￥\') < 0 && l.length > 3) {\n          name = l;\n          break;\n        }\n      }\n    }\n\n    if (name || goodsId) {\n      items.push({\n        goodsId: goodsId,\n        name: name,\n        price: price,\n        sold: sold\n      });\n    }\n  });\n\n  return JSON.stringify({\n    url: url,\n    title: title,\n    ogTitle: ogTitle,\n    pageText: bodyText,\n    rateLimited: rateLimited,\n    itemCount: items.length,\n    items: items\n  });\n})()\n'
DETAIL_EXTRACT_JS = '\n(function(){\n  var bodyText = (document.body && document.body.innerText || \'\').substring(0, 10000);\n  var url = window.location.href;\n  var title = document.title || \'\';\n\n  // ── Sold-out detection ───────────────────────────\n  var soldOut = false;\n  if (bodyText.indexOf(\'商品已售罄\') >= 0 || bodyText.indexOf(\'已卖光\') >= 0) {\n    soldOut = true;\n  }\n\n  // ── Login-gated ──────────────────────────────────\n  var ogTitle = \'\';\n  var ogMeta = document.querySelector(\'meta[property="og:title"]\');\n  if (ogMeta) ogTitle = ogMeta.getAttribute(\'content\') || \'\';\n  var loginGated = (ogTitle === \'拼多多商城\' && bodyText.indexOf(\'¥\') < 0 && bodyText.indexOf(\'￥\') < 0);\n\n  // ── Extract: name, price, spec info from body text ─\n  var lines = bodyText.split(\'\\n\').map(function(l){ return l.trim(); }).filter(function(l){ return l.length > 0; });\n  var name = \'\';\n  var price = null;\n  var origPrice = null;\n  var sales = \'\';\n  var specs = [];\n\n  for (var i = 0; i < lines.length; i++) {\n    var line = lines[i];\n    // Price detection\n    var priceMatch = line.match(/[¥￥]\\s*([\\d,.]+)/);\n    if (priceMatch) {\n      var p = parseFloat(priceMatch[1].replace(/,/g, \'\'));\n      if (price === null) {\n        price = p;\n      } else if (origPrice === null && p < price) {\n        origPrice = price;\n        price = p;\n      } else if (origPrice === null) {\n        origPrice = p;\n      }\n    }\n    // Name: first non-price, non-blank line that looks like a product name\n    if (!name && line.indexOf(\'¥\') < 0 && line.indexOf(\'￥\') < 0 && line.length > 5) {\n      name = line;\n    }\n    // Sales\n    var soldMatch = line.match(/(\\d+[\\.\\d]*万?\\+?)\\s*件/);\n    if (soldMatch) {\n      sales = soldMatch[1];\n    }\n    // Specs (lines containing 颜色/尺码/规格)\n    if (line.indexOf(\'颜色\') >= 0 || line.indexOf(\'尺码\') >= 0 || line.indexOf(\'规格\') >= 0) {\n      specs.push(line);\n    }\n  }\n\n  return JSON.stringify({\n    url: url,\n    title: title,\n    ogTitle: ogTitle,\n    loginGated: loginGated,\n    soldOut: soldOut,\n    name: name,\n    price: price,\n    origPrice: origPrice,\n    sales: sales,\n    specs: specs,\n    pageText: bodyText.substring(0, 3000)\n  });\n})()\n'

class PDDEngine:

    def _process_search_result(self, raw: dict, keyword: str, limit: int) -> dict:
        """Process raw CDP extraction data into structured results.

        Args:
            raw: Dict from SEARCH_EXTRACT_JS evaluation
            keyword: Original search keyword
            limit: Max items to return

        Returns:
            Structured search result dict
        """
        if raw.get('rateLimited'):
            raise PDDRateLimitError("拼多多返回 '系统繁忙' — 已达到单次搜索限制。\nPDD mobile search 每个浏览器会话只允许一次搜索。\n请创建新的 PDDEngine() 实例重新搜索。")
        page_text = raw.get('pageText', '')
        og_title = raw.get('ogTitle', '')
        item_count = raw.get('itemCount', 0)
        items_raw = raw.get('items', [])
        if og_title == '拼多多商城' and item_count == 0:
            raise PDDAuthError(f'拼多多 cookie 已过期（页面重定向至登录）。\nCookie 文件: {self.cookies_path}\nPDDAccessToken 有效期约 1 小时，请重新从手机浏览器导出。')
        if item_count == 0 and len(items_raw) == 0:
            if '系统繁忙' in page_text or '网络异常' in page_text:
                raise PDDRateLimitError("拼多多返回 '系统繁忙' — 已达到单次搜索限制。\n请创建新的 PDDEngine() 实例重新搜索。")
            return {'keyword': keyword, 'count': 0, 'items': [], 'state': 'empty'}
        seen_ids: set = set()
        items_out: list[dict[str, Any]] = []
        for it in items_raw:
            goods_id = (it.get('goodsId') or '').strip()
            if not goods_id and it.get('name'):
                goods_id = f"name:{hash(it['name']) & 4294967295}"
            if not goods_id or goods_id in seen_ids:
                continue
            seen_ids.add(goods_id)
            price = it.get('price')
            if price is not None and (not isinstance(price, (int, float))):
                try:
                    price = float(price)
                except (TypeError, ValueError):
                    price = None
            url = ''
            if goods_id and (not goods_id.startswith('name:')):
                url = f'https://mobile.yangkeduo.com/goods2.html?goods_id={goods_id}'
            items_out.append({'goodsId': goods_id if not goods_id.startswith('name:') else '', 'name': it.get('name', ''), 'price': price, 'sold': it.get('sold', 0), 'url': url})
        return {'keyword': keyword, 'count': len(items_out), 'items': items_out[:limit], 'state': 'ok'}

    def product_detail(self, url_or_id: str) -> dict:
        if raw_result.get('loginGated'):
            raise PDDAuthError('拼多多 cookie 已过期（商品页重定向至登录）。\nPDDAccessToken 有效期约 1 小时，请刷新 cookie。')
        if raw_result.get('soldOut'):
            return {'goodsId': goods_id, 'name': raw_result.get('name'), 'price': raw_result.get('price'), 'origPrice': raw_result.get('origPrice'), 'sales': raw_result.get('sales', ''), 'specs': raw_result.get('specs', []), 'url': detail_url, 'soldOut': True, 'state': 'sold_out'}
        if not raw_result.get('name') and (not raw_result.get('price')):
            page_text = raw_result.get('pageText', '')
            if '商品已下架' in page_text or '不存在' in page_text:
                return {'goodsId': goods_id, 'name': None, 'price': None, 'origPrice': None, 'sales': '', 'specs': [], 'url': detail_url, 'soldOut': False, 'state': 'not_found'}
        return {'goodsId': goods_id, 'name': raw_result.get('name'), 'price': raw_result.get('price'), 'origPrice': raw_result.get('origPrice'), 'sales': raw_result.get('sales', ''), 'specs': raw_result.get('specs', []), 'url': detail_url, 'soldOut': False, 'state': 'ok'}
