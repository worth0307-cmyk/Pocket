# Wallet Tracker · 链上钱包跟踪器

跟踪 **EVM（ETH/BSC/Base/Arbitrum/Polygon/Optimism）、Solana、比特币** 钱包的
买入 / 卖出 / 兑换 / 转账，查看余额，维护地址清单，并在监控的钱包有新动作时
自动推送 **Telegram 提醒**。同时附带一个 **Arkham 风格的深色网页面板**。

全部基于**免费**数据源：Etherscan V2 / Helius 免费档 / mempool.space（免 key）。

---

## 功能

| 功能 | 说明 |
| --- | --- |
| 行动轨迹 | 归一化为 买入/卖出/兑换/转入/转出，含金额、对手方、区块浏览器链接 |
| 余额信息 | 原生币余额；Solana 含 SPL 代币持仓；BTC 含历史交易数 |
| 地址清单 | 增删查，按链分组，支持备注 |
| 自动跟踪 + TG 提醒 | 定时轮询每个地址，发现新交易即推送到 Telegram |
| 网页面板 | DeBank 风浅色 UI：清单 / 持仓 / 动作流，**动作流自动刷新（30s）**，支持快速查询 |
| USD 估值 | 原生币 + 稳定币按 CoinGecko 免费价折算总值（无需 key） |
| 盈亏估算 | 从近期可见交易估算已实现盈亏（买入成本 vs 卖出回款），仅供参考 |

两个入口共用同一套后端与数据库：
- **Telegram 机器人**：用命令管理清单、查余额、查动作，并接收自动提醒。
- **网页面板**：浏览器里可视化查看，默认仅监听本机 `127.0.0.1:8000`。

---

## 买卖判定逻辑

把一笔交易里钱包的资金流向归一化后判断：

- 花出**原生币/稳定币** → 收到**代币**　=　**买入**
- 卖出**代币** → 收回**原生币/稳定币**　=　**卖出**
- **币换币**（无原生/稳定币腿）　=　**兑换 (Swap)**
- 单向资金流　=　**转入 / 转出**

> EVM 的判定基于 Etherscan 的普通交易 + ERC-20 转账记录组合，属启发式，
> 复杂聚合路由/合约交互可能归类为「其他」。Solana 优先采用 Helius 的解析结果。
> 比特币没有买卖概念，只有转入/转出。

---

## 快速开始

```bash
# 1. 安装依赖（建议虚拟环境）
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. 配置
cp .env.example .env
#   填入 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID（必填）
#   填入 ETHERSCAN_API_KEY（EVM）、HELIUS_API_KEY（Solana，可选）

# 3. 启动（同时跑机器人 + 网页面板）
python main.py
```

打开 <http://127.0.0.1:8000> 查看网页面板；在 Telegram 里给机器人发 `/start`。

### 自动推送提醒（Telegram）

跟踪清单里的钱包一有新成交，会自动推送到 Telegram：买入/卖出、开多/开空/平多/平空、
平仓已实现盈亏，大额（≥ `ALERT_LARGE_USD`）标 🔥，并带上钱包等级（巨鲸/超大户/…）。

- 每 `POLL_INTERVAL` 秒轮询一次（默认 60s），只推「新」成交，不重复。
- `ALERT_MIN_USD`（默认 $10K）以下的小额不推，避免刷屏。
- **国内必看**：`api.telegram.org` 被墙，需在 `.env` 设 `TELEGRAM_PROXY`
  （如 `http://127.0.0.1:7890` 或 `socks5://127.0.0.1:1080`）+ `BOT_ENABLED=true`。
  只用网页不推送时保持 `BOT_ENABLED=false` 即可。

### 免费 API Key 在哪拿（各 1 分钟）

- **Etherscan**（EVM 全链通用）：<https://etherscan.io/myapikey> → 注册 → New API Key
- **Helius**（Solana）：<https://dashboard.helius.dev> → 注册 → 复制 API Key
- **比特币**：无需 key

不填某个 key，对应链会自动跳过，不影响其他链。

---

## Telegram 命令

| 命令 | 说明 |
| --- | --- |
| `/add <链> <地址> [备注]` | 添加跟踪，例 `/add sol 9xQeWv...A1b2 聪明钱` |
| `/remove <编号>` 或 `/remove <链> <地址>` | 移除 |
| `/list` | 查看跟踪清单 |
| `/balance <编号>` 或 `/balance <链> <地址>` | 查余额 |
| `/history <编号> [条数]` 或 `/history <链> <地址> [条数]` | 查最近动作 |
| `/chains` | 查看支持的链与配置状态 |
| `/help` | 帮助 |

链代号：`eth` `bsc` `base` `arb` `polygon` `op` `sol` `btc`

---

## 部署到自己的服务器（systemd）

`/etc/systemd/system/wallet-tracker.service`：

```ini
[Unit]
Description=Wallet Tracker (Telegram bot + web dashboard)
After=network-online.target

[Service]
WorkingDirectory=/opt/wallet-tracker
ExecStart=/opt/wallet-tracker/.venv/bin/python main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now wallet-tracker
sudo journalctl -u wallet-tracker -f   # 看日志
```

> 网页要对外访问：把 `WEB_HOST=0.0.0.0` 并**务必**设置 `WEB_TOKEN`，
> 建议再加 Nginx 反向代理 + HTTPS。仅自己用就保持默认 `127.0.0.1` 最安全。

---

## 项目结构

```
main.py            入口：同时启动 TG 机器人 + 网页面板
bot.py             Telegram 命令处理
tracker.py         自动跟踪轮询 + 提醒推送
web.py             FastAPI 接口
static/index.html  深色网页面板（单文件）
config.py          环境配置
db.py              SQLite 清单 + 跟踪游标
formatting.py      Telegram 消息排版
analytics.py       USD 估值 + 盈亏估算
chains/            链适配层
  base.py          统一数据模型 / 接口
  evm.py           Etherscan V2（多 EVM 链）
  solana.py        Helius + Solana RPC
  bitcoin.py       mempool.space
  prices.py        CoinGecko 免费价（USD 估值/盈亏）
  __init__.py      链注册 / 工厂
  util.py          base58 / 金额格式化
```
