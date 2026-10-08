# EricMingle 拼多多 MCP

独立单平台的个人购物 MCP，复用你日常浏览器中的 Kimi WebBridge 任务页。代码公开可查；EricMingle 自有增量采用 PolyForm Noncommercial 1.0.0，限许可允许的非商用用途。本组合分发不是 OSI 开源项目；上游 MIT 部分继续保持原有许可及商业使用权，见 LICENSE、NOTICE.md 与 licenses/。

## 运行

需要 Python 3.12 和已连接日常浏览器的 Kimi WebBridge；本仓库不包含该扩展或浏览器 Profile。本人完成平台登录与验证。先安装 `python -m pip install -r requirements.txt`，然后 `python src/server.py --stdio`。省略 `--stdio` 使用本机 HTTP，默认端口 8841，路径 `/mcp`。

`.env.example` 是变量示例，程序从进程环境读取。可用 `set -a; . ./.env; set +a` 导出自行创建的配置，再启动服务。`WEBBRIDGE_URL`、`WEBBRIDGE_SESSION`、`MCP_HOST`、`MCP_PORT`、`MCP_DATA_DIR` 可独立设置。HTTP 默认只监听本机；可选 `MCP_AUTH_TOKEN` 保护本机 HTTP；非 loopback 监听必须设置至少 24 字符令牌，外部 Host/Origin 还需明确加入 `MCP_ALLOWED_HOSTS`/`MCP_ALLOWED_ORIGINS`。这不替代云端 OAuth/HTTPS 网关，不应直接暴露未认证的 MCP。

## 能力与当前边界

首版共21个工具。真实检查已通过：搜索与limit、官方聊天首页会话概览、本MCP待购车增查删恢复、本人邮件送达。商品详情有早前真实读取证据；后来网页详情出现“前往APP查看价格”、底栏收藏/客服缺失。收藏及指定商品商家消息因此按实验能力交付，修复后未完成真实工具复验，不继续反复访问降级详情页。

`search_pinduoduo(keyword, limit=5, sort="default", page=1)`的limit为1–10；`page_scope=loaded_only`只对本次已加载结果按本次limit切片，不是平台网页翻页。超出该批结果返回unsupported及outside_loaded_results；loaded_count/has_next_page不代表全站结果。price_asc/price_desc只排序本页展示价，标sort_scope=page_local。

`favorite_pinduoduo_item`/`unfavorite_pinduoduo_item`的原生动作与“已收藏/收藏”两态已有真实页面证据；已修复精确原生/React控件识别、祖孙去重及单次点击后的短只读确认。App降级页返回unsupported及有限证据。修复后的完整工具往返和记录联动尚未复验。

`favorite_list(page=1)`是**本MCP确认收藏记录**：仅页面状态真实确认后才写入或移除，source=mcp_confirmed_actions、platform_full_list=false，每页20条；不含App历史收藏，不宣称平台全量同步。原生收藏列表入口本次观察为App提示；未有真实确认记录时保留该边界。该记录与watchlist分表，不能混称。

`watchlist_upsert/list/check/remove`为独立本地分类清单、目标价及报价缓存。remove幂等删除指定商品的清单、报价和通知刷新记录，不操作平台收藏、本MCP确认收藏记录或待购车。check只看近期缓存报价，不自行联网刷新。

`conversation_list()`真实读回5个可见会话的概览，对象名、时间、末条摘要未露出时保留null；交流仍用调用者提供的明确商品URL。`merchant_messages/contact_merchant`已有原生客服入口和匹配商品会话可读的页面证据，已修复容器尚未渲染的时序；修复后受详情App降级影响未复验。contact_merchant单次发送后读回、已有草稿不覆盖；**本轮未实际向商家发送消息，实发仍未验收。**

`pending_cart_add/list/remove`是本MCP管理的待购车，不同步平台购物车。入车当场读真实详情后持久保存商品、可见规格与期望数量，重复相同请求不累加。真实入车→查询1条→删除→查询0条已恢复原状；展示价非结算价，未暴露标题/规格/库存保留unknown。平台下单、支付由本人完成。

`notify_owner/notification_configure/notification_status`只发本人邮件，无桌面弹窗或兜底。将notification-mail.example.json复制到私人数据目录并填写已有邮件MCP配置，或用MCP_MAIL_CONFIG指定私人文件；真实配置不要提交。accepted仅表示邮件工具报告发送，送达须收件箱读回。本人邮件链路已确认送达；未知投递不盲重发。后台目标价监测默认关闭、每商品至少30分钟刷新，风险/认证即暂停；自动价格触发只做离线合同检查，未宣称长期运行验收。

另提供login_pinduoduo、status_pinduoduo、get_pinduoduo_product及close_pinduoduo_browser；只操作固定任务页、保留日常浏览器会话，由本人认证。未提供图片消息、文件上传、卖家发布或上下架。跨平台比价由调用智能体组合各独立MCP结果。当前仍依赖日常浏览器/WebBridge，未迁NAS。

## Docker / NAS

`docker build -t ericmingle-pinduoduo-mcp .`，`docker run --rm -i --env-file .env -v mcp-pinduoduo-data:/app/data ericmingle-pinduoduo-mcp` 通过 stdio 使用。容器内的桥接地址必须指向实际可达的 WebBridge 宿主，不能把容器自己的 loopback 当作 Mac。当前后端仍依赖既有浏览器/WebBridge；Docker 模板不表示已完成 NAS 独立运行或 NAS 迁移。

## 验证

`python -m pip install -r requirements-dev.txt`，`python -m pytest -q tests`；`python scripts/scan_public.py .` 扫描隐私与敏感材料，命中仅输出位置和类型。DOM 合同测试还需要 PATH 中可用的 Node.js。测试不访问真实平台、不登录、不读真实凭据。

## 致谢

[上游 goesByhc/cn-scraper-mcp](https://github.com/goesByhc/cn-scraper-mcp) 的解析与实践提供基础，原版权与 MIT 文本完整保留。EricMingle 为个人维护标识，不代表平台官方服务。

买方功能划分与回执机制参考 [DoLovya/xianyu-mcp-server](https://github.com/DoLovya/xianyu-mcp-server)。感谢其闲鱼买方流程；本项目未复制该项目的GPL源码，拼多多页面适配独立实现。
