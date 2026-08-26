# sbot

用 Telegram 管理 v2board 面板与 v2node 服务器的运维 bot。

只服务白名单里的用户,所有操作都在对话里用 inline 按钮完成,不需要登面板后台、
也不需要 ssh 上机器。

## 功能

| 模块 | 能做什么 |
| --- | --- |
| 🖥 服务器管理 | 登记服务器(密码 / 私钥)、连通性检测、装 / 卸 v2node、开机自启与状态查看、防火墙端口体检与放行 |
| 🎛 面板管理 | 登记 v2board 面板、同步节点通信凭据、节点列表(分页)、上下架 / 删除 / 复制、按协议引导式新增与编辑节点(shadowsocks / vless / anytls / hysteria2) |
| 🌐 DNS 管理 | Cloudflare 账户、zone 与记录的增删改查 |
| 📦 安装包分发 | 从私有 GitHub Release 拉取选定资产,上传到阿里云 OSS 并刷新 `latest/`;可录入多个存储桶并切换当前发布使用的那个 |
| 🛠 远程配置 | 编辑腾讯云 COS 上的 JSON 配置文件(整份替换或改单个字段);可录入多个存储桶并切换当前使用的那个 |
| 📜 操作日志 | 分页浏览、只看失败,超过保留期自动清理 |
| 🔄 更新重启 | 拉取最新代码或切换分支后重启进程(依赖 systemd 拉起) |

## 快速开始

```bash
git clone <repo> && cd <repo>
python -m venv .venv && source .venv/bin/activate
pip install -r sbot/requirements.txt

cp .env.example sbot/.env    # 然后按下面说明填写
python -m sbot.main
```

`.env` 放在 `sbot/` 目录下(与 `config.py` 同级)。生成加密密钥:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### 环境变量

必填:

| 变量 | 说明 |
| --- | --- |
| `BOT_TOKEN` | BotFather 给的 token |
| `ALLOWED_USER_IDS` | 允许使用的 Telegram user id,逗号分隔。**不在名单里的消息会被直接忽略** |
| `CRED_ENCRYPTION_KEY` | Fernet 密钥,用于加密所有入库凭据。**换了这个密钥,已存的凭据都解不开** |

可选:见 `.env.example`,包含数据库路径、SSH 超时、并发度、日志级别,以及
OSS / COS 的凭据(也可以后在 bot 里交互录入,存库的配置优先于 `.env`)。

### 以 systemd 运行

```ini
[Unit]
Description=sbot
After=network.target

[Service]
WorkingDirectory=/opt/sbot
ExecStart=/opt/sbot/.venv/bin/python -m sbot.main
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`Restart=always` 是必须的——「🔄 更新重启」靠进程退出后被自动拉起来加载新代码。

## 安全模型

- **访问控制**:`main._wrap_with_auth` 在启动时给所有已注册 handler 套上白名单
  装饰器,新增 handler 不会漏掉鉴权。
- **凭据加密**:SSH 密码、私钥口令、面板密码、面板通信 api_key、DNS token、
  GitHub PAT、OSS/COS secret 一律 Fernet 加密入库;私钥本身不入库,只存路径。
- **聊天记录**:密码 / 口令类输入收到后立即从聊天里删除。
- **主机密钥**:TOFU。首次连接记录指纹到 `servers.host_key`,之后每次连接都要求
  一致;校验发生在认证之前,指纹不符时凭据不会外发。服务器重装后到
  「修改服务器 → 🔑 重置主机密钥」显式确认。
- **日志脱敏**:操作日志的 detail 写库前统一过 `core.redact`,token / 密码 /
  带口令的 URL 会被打码。
- **命令构造**:所有远程命令集中在 `services/` 里拼装,端口等参数先转 int;
  远程 `config.json` 只走 JSON 解析 → 改对象 → 序列化,不做字符串替换,
  写回前备份、重启失败自动回滚。

## 代码结构

```
sbot/
  main.py              装配 + 注册 handler + 全局错误处理
  config.py            .env 读取与校验
  core/                ssh(含 TOFU 主机密钥)/ crypto / auth / redact / timeutil
  db/                  models 与 crud(含建表后的补列迁移)
  services/            v2board API、v2node 安装/卸载/配置、防火墙、CF、OSS、COS、GitHub
  handlers/            每个功能一个模块;common.py 放共享的分页、safe_edit、callback 前缀
    edit_panel_node/   节点新增/编辑对话流(按 options/state/steps_*/confirm/conversation 分层)
tests/                 pytest 用例
```

几个跨模块的约定:

- **加列**:改完 model,必须在 `crud.ADDED_COLUMNS` 登记一份,老库才会被 ALTER。
- **列表**:统一用 `common.paginate` + `pager_row`,不要再写「仅显示前 N 个」。
- **改消息**:用 `common.safe_edit`,它会把「内容没变」的 BadRequest 当正常情况。
- **连发多条 SSH 命令**:用 `async with ctx.ssh.connection(server) as conn`,
  不要每条命令都新建连接。

## 开发

```bash
pip install -r sbot/requirements.txt -r requirements-dev.txt
pytest          # 149 个用例,含真实 asyncssh 服务端的主机密钥/连接复用测试
ruff check .
```

测试不联网、不碰真服务器:数据库用临时 SQLite,Telegram 侧用 `tests/conftest.py`
里的假对象,SSH 侧在本机起一个 asyncssh 服务端。
