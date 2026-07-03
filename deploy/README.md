# 部署到 VPS + Cloudflare 子域名（pocket.arknove.com）

把 Pocket 跑在一台有公网 IP 的 VPS（如 Vultr 新加坡 / 美国节点）上，用
Cloudflare Tunnel 暴露成 `https://pocket.arknove.com`。

**为什么放 VPS（而不是家里/国内服务器）**
- 海外节点直连 Hyperliquid / Telegram / Moralis / CoinGecko —— 排行榜不再超时，
  Telegram **不用配代理**（`TELEGRAM_PROXY` 留空即可）。
- 有公网 IP，配 Cloudflare 简单；Tunnel 方案还能不开任何入站端口、隐藏真实 IP。
- VPS 常年在线。

---

## 一、在 VPS 上部署应用（Ubuntu/Debian 示例）

```bash
# 1) 依赖
sudo apt update && sudo apt install -y python3 python3-venv python3-pip git

# 2) 拉代码（私有仓库：用 GitHub 个人访问令牌 PAT，或把本地目录 scp 上来）
sudo mkdir -p /opt/pocket && sudo chown $USER /opt/pocket
git clone https://<GITHUB_USER>:<PAT>@github.com/worth0307-cmyk/pocket.git /opt/pocket
cd /opt/pocket
git checkout claude/wallet-address-tracker-hpn2tx   # 或先把 PR 合到 main 再用 main

# 3) 虚拟环境 + 依赖
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt

# 4) 配置（复制模板后编辑）
cp .env.example .env
nano .env
```

`.env` 关键项（海外 VPS）：
```ini
TELEGRAM_BOT_TOKEN=你的bot token
TELEGRAM_CHAT_ID=你的chat id
TELEGRAM_PROXY=                 # 海外直连，留空！不要填代理
BOT_ENABLED=true               # 要 TG 推送就开
ETHERSCAN_API_KEY=...
MORALIS_API_KEY=...
HELIUS_API_KEY=...             # 需要 Solana 才填
WEB_HOST=127.0.0.1             # 只监听本机，隧道连它，别用 0.0.0.0
WEB_PORT=8000
WEB_TOKEN=一串足够长的随机串     # 必设！否则面板对全网裸奔
```

测试：`.venv/bin/python main.py`，日志出现 dashboard + bot 正常即可 Ctrl+C。

### 装成开机自启服务
把 `deploy/pocket.service` 复制到 systemd（按需改 User/路径）：
```bash
sudo cp deploy/pocket.service /etc/systemd/system/pocket.service
sudo systemctl daemon-reload
sudo systemctl enable --now pocket
sudo systemctl status pocket        # 看是否 running
journalctl -u pocket -f             # 实时日志
```

---

## 二、用 Cloudflare Tunnel 暴露（推荐：不开端口、免证书、隐藏 IP）

前提：`arknove.com` 已托管在 Cloudflare（NS 指向 Cloudflare）。

```bash
# 安装 cloudflared（Debian/Ubuntu）
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -o cf.deb
sudo dpkg -i cf.deb

cloudflared tunnel login                 # 浏览器授权，选 arknove.com
cloudflared tunnel create pocket         # 生成隧道ID + 凭据 json（记下ID）
```

把 `deploy/cloudflared-config.example.yml` 复制为
`~/.cloudflared/config.yml`，填入隧道ID与凭据路径，然后：

```bash
cloudflared tunnel route dns pocket pocket.arknove.com   # 自动建 DNS 记录
cloudflared tunnel run pocket                            # 先前台测试
sudo cloudflared service install                         # 测通后装成开机服务
```

访问 **https://pocket.arknove.com** → 输入 `WEB_TOKEN` → 完成。HTTPS 由
Cloudflare 边缘自动签发，应用侧无需任何证书配置。

> 备选（有公网 IP 且想走端口）：Nginx 反代 8000 + Cloudflare 橙云 A 记录 +
> Let's Encrypt（Full strict）。步骤更多，一般不如 Tunnel 省事。

---

## 三、迁移已有跟踪清单

不用手动重加：
- 在旧实例点 **导出** 下载 `pocket-wallets-*.json`
- 在新实例（pocket.arknove.com）点 **导入** 选该文件即可

或直接把旧的 `wallets.db` scp 到 `/opt/pocket/` 覆盖（停服务后再拷）。

---

## 四、安全清单
- **必设 `WEB_TOKEN`**（公网可达，不设=谁都能看你的清单）。
- `WEB_HOST=127.0.0.1`，让 Tunnel 连本机，别监听 0.0.0.0。
- 防火墙只放 SSH：`sudo ufw allow OpenSSH && sudo ufw enable`（Tunnel 是出站连接，
  无需开放 80/443/8000）。
- 定期 `git pull && systemctl restart pocket` 更新。
