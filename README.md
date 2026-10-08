# EricMingle 拼多多 MCP

独立单平台的个人购物 MCP，复用你日常浏览器中的 Kimi WebBridge 任务页。代码公开可查；EricMingle 自有增量采用 PolyForm Noncommercial 1.0.0，限许可允许的非商用用途。本组合分发不是 OSI 开源项目；上游 MIT 部分继续保持原有许可及商业使用权，见 LICENSE、NOTICE.md 与 licenses/。

## 运行

需要 Python 3.12 和已连接日常浏览器的 Kimi WebBridge；本仓库不包含该扩展或浏览器 Profile。本人完成平台登录与验证。先安装 `python -m pip install -r requirements.txt`，然后 `python src/server.py --stdio`。省略 `--stdio` 使用本机 HTTP，默认端口 8841，路径 `/mcp`。

`.env.example` 是变量示例，程序从进程环境读取。可用 `set -a; . ./.env; set +a` 导出自行创建的配置，再启动服务。`WEBBRIDGE_URL`、`WEBBRIDGE_SESSION`、`MCP_HOST`、`MCP_PORT`、`MCP_DATA_DIR` 可独立设置。HTTP 默认只监听本机；可选 `MCP_AUTH_TOKEN` 保护本机 HTTP；非 loopback 监听必须设置至少 24 字符令牌，外部 Host/Origin 还需明确加入 `MCP_ALLOWED_HOSTS`/`MCP_ALLOWED_ORIGINS`。这不替代云端 OAuth/HTTPS 网关，不应直接暴露未认证的 MCP。

## 能力与当前边界

本机真实验收：空待购车→当场读取真实商品页后入车→查询1条→删除→查询0条；恢复原状，末次状态无风控。标题/规格/库存未暴露时保留unknown。

每个MCP独立，只处理本平台；跨平台比价由调用智能体负责。

提供 pending_cart_add/list/remove：这是本MCP管理的待购车，不同步平台购物车。入车先当场读取真实商品页，持久保存商品/可见规格/数量；数量为设定值，重复相同请求不叠加。展示价非结算价，未暴露的标题、规格、库存明确unknown。

商家交流提供 merchant_messages/contact_merchant，先核对商品对应店铺与会话，单次发送后读回，已有草稿不覆盖；三站未向真实商家发送测试消息，实发仍未验收。本人完成认证、最终下单支付。

通知只发本人邮件，没有桌面弹窗分支或桌面兜底。把 notification-mail.example.json 复制到私人数据目录的 notification-mail.json，填写已有邮件MCP地址、本人发件/收件身份及必要headers；也可用MCP_MAIL_CONFIG指定私有文件。不要提交真实配置。notify_owner用于智能体确认交易条件后的邮件提醒；accepted是邮件工具报告发送，送达须收件箱读回。未知投递不盲重发。本人邮件链路已完成收件箱正文与本人确认验收。

notification_configure后台目标价监测默认关闭，每件至少30分钟刷新；同商品同目标只提醒一次，风险/认证即暂停。自动价格触发目前有离线测试，尚未长期运行或真实条件触发验收。本地分类清单与平台收藏/购物车分别存在。

已移除写动作中的网页长计时轮询，使用短动作+Python侧限时只读确认；真实站点样例成功不等于全部商品/长期免风控。当前仍依赖既有日常浏览器和WebBridge，本轮未迁NAS。

## Docker / NAS

`docker build -t ericmingle-pinduoduo-mcp .`，`docker run --rm -i --env-file .env -v mcp-pinduoduo-data:/app/data ericmingle-pinduoduo-mcp` 通过 stdio 使用。容器内的桥接地址必须指向实际可达的 WebBridge 宿主，不能把容器自己的 loopback 当作 Mac。当前后端仍依赖既有浏览器/WebBridge；Docker 模板不表示已完成 NAS 独立运行或 NAS 迁移。

## 验证

`python -m pip install -r requirements-dev.txt`，`python -m pytest -q tests`；`python scripts/scan_public.py .` 扫描隐私与敏感材料，命中仅输出位置和类型。DOM 合同测试还需要 PATH 中可用的 Node.js。测试不访问真实平台、不登录、不读真实凭据。

## 致谢

[上游 goesByhc/cn-scraper-mcp](https://github.com/goesByhc/cn-scraper-mcp) 的解析与实践提供基础，原版权与 MIT 文本完整保留。EricMingle 为个人维护标识，不代表平台官方服务。
