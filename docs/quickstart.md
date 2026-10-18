# 从零到一本译好的书

两条路线，用的**同一份代码、同一个 `Pipeline`**。区别只在"谁驱动它"：

| | 本地模式 | 服务器模式 |
|---|---|---|
| 驱动 | 你在终端里跑 `slowtrans run` | 常驻服务从队列里取任务 |
| 中断 | Ctrl+C，重跑自动续 | 面板/API 的 pause/resume |
| 观察 | 终端进度 | Web 面板、SSE、webhook |
| 适合 | 偶尔译一本、想盯着质量 | 挂机慢慢跑、多人用、要手机看进度 |

---

## 路线 A：本地（自己的电脑）

### 1. 装

需要 Python 3.11+。

```bash
git clone https://github.com/<you>/slowtrans.git
cd slowtrans
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate

# 只要命令行翻译：
pip install -e .

# 还想要网页面板：
pip install -e ".[api]"
```

> 只装核心包时 `slowtrans serve` 会提示你补装 `slowtrans[api]`，不会甩一堆 traceback。

### 2. 拿一个 API key

以硅基流动为例（免费模型够用）：注册 → 控制台 → API 密钥 → 复制 `sk-...`。

### 3. 告诉程序 key 在哪

三种方式，任选：

```bash
# a) 临时环境变量
export SILICONFLOW_API_KEY=sk-...

# Windows PowerShell
$env:SILICONFLOW_API_KEY="sk-..."

# b) 项目根目录写 .env（已在 .gitignore，不会被提交）
echo "SILICONFLOW_API_KEY=sk-..." > .env

# c) 想用面板的话，启动后在「设置」里填，落盘到 data/settings.json（600）
```

### 4. 先看结构，不花钱

```bash
slowtrans inspect --src book.epub
# 标题 / 语言 / 每章块数与段数，用来判断会不会太碎或太大
```

### 5. 试译 20 段

```bash
slowtrans run --src book.epub --out test.epub --limit 20
```

默认引擎是免费的 `Qwen/Qwen2.5-7B-Instruct`。看完这两章觉得可以，再跑全书。

### 6. 全书

```bash
slowtrans run --src book.epub --out book.zh.epub --from en --to zh-CN
```

- 中文书（日→中、英→中）不用改 `--to`
- 日语书加 `--from ja`
- 只要译文不要对照：加 `--mono`（这时只产出一个文件）

### 7. 产物

```
book.zh.epub        双语：原文段 + 译文段
book.zh.mono.epub   纯中文：给阅读器
```

直接用 Calibre / Reeden / Apple Books 打开即可。

### 8. 想要面板（可选）

```bash
slowtrans serve --port 8790
# 浏览器 http://127.0.0.1:8790/
```

面板里能：书库封面墙、任务进度、章节预览、下载两份成品、改术语、填 key。
书库目录在「设置」里改成你放 epub 的文件夹。

---

## 路线 B：服务器（挂机慢慢跑）

适合 1–2 GB 内存的小机器。实测常驻 36 MB，全量分段+组装峰值 45 MB。

### 1. 上传代码

```bash
# 本地执行（Windows 上 Git Bash 也能用）
tar -C /path/to/slowtrans -cf - --exclude=.venv --exclude=.git --exclude=data \
    src pyproject.toml deploy | ssh my-server 'mkdir -p /srv/slowtrans && tar -C /srv/slowtrans -xf -'
```

或者在服务器上 `git clone`。

### 2. 一键部署

```bash
ssh my-server
sudo mkdir -p /srv/slowtrans && sudo chown $USER /srv/slowtrans
# 把代码放到 /srv/slowtrans 后：
bash /srv/slowtrans/deploy/install.sh
```

脚本做四件事：建 venv、装依赖、装 `/etc/slowtrans/env` 模板、装 systemd unit。
（没有 rsync 的机器会自动改用 tar。）

### 3. 填 key 和 token

```bash
sudo nano /etc/slowtrans/env
```

```ini
SILICONFLOW_API_KEY=sk-...
SLOWTRANS_TOKEN=<随机串，面板和插件都用它>
```

`install.sh` 首次运行时会**自动生成一个令牌并打印出来**。想自己换：

```bash
python3 -c 'import secrets;print(secrets.token_urlsafe(32))'
```

随时查询当前令牌：

```bash
sudo grep SLOWTRANS_TOKEN /etc/slowtrans/env
```

> 令牌是**你自己定的门锁密码**，用来防止本机/隧道上的其他程序乱动你的书。
> 它和 `SILICONFLOW_API_KEY` 完全两回事——后者是平台发给你的通行证。
> 把这一行留空则关闭鉴权（仅回环时可接受，不推荐）。

> ⚠️ 这个文件必须是 **LF 换行**。在 Windows 上生成后传上去会带 `\r`，
> systemd 读到的 key 末尾有回车，所有 API 调用报 `Invalid header value`。

### 4. 启动

```bash
sudo systemctl start slowtrans
systemctl status slowtrans --no-pager
curl -s http://127.0.0.1:8790/api/v1/health
systemctl show slowtrans -p MemoryMax -p MemoryCurrent
```

### 5. 把书放上去

服务只读一个目录，写另一个目录，两边都跟别的服务无关：

```
/srv/slowtrans/
├── books/     你放书的地方（服务只读扫描）
├── outputs/   译好的 epub 落在这里（服务可写）
├── data/      SQLite、settings.json（服务可写）
└── venv/
```

```bash
scp book.epub my-server:/srv/slowtrans/books/
```

面板「设置」里可以改这两个目录，默认就是上面的 `books/` 和 `outputs/`。
**服务不会读写 WebDAV、网盘或任何其他服务的数据目录**——想让它和书库联动，
请用单独的同步任务（cron/rsync），而不是给翻译服务放宽权限。

### 6. 打开面板

服务只监听 `127.0.0.1`，从本机建隧道：

```bash
ssh -N -L 18790:127.0.0.1:8790 my-server
# 浏览器 http://127.0.0.1:18790/ ，token 填 SLOWTRANS_TOKEN
```

Windows 上可以用 `plink`/`ssh` 的快捷方式，或者把上面命令写进 `.bat`。

### 7. 提交任务

三种方式，效果一样：

```bash
# a) 面板：书库点封面 → 开始翻译
# b) 命令行（和服务共用同一个库，有任务锁防重复）
ssh my-server 'cd /srv/slowtrans && sudo -E venv/bin/slowtrans run \
    --src /srv/slowtrans/books/book.epub --out /srv/slowtrans/outputs/book.zh.epub'
# c) HTTP
curl -X POST http://127.0.0.1:8790/api/v1/jobs \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"rel":"book.epub","lang_to":"zh-CN"}'
```

### 8. 挂机长跑的注意点

- **免费模型会限流**：默认单并发、指数退避，遇到 429 会自己等，不用管。
- **想省钱/错峰**：`config.toml` 里设 `[scheduler] window = "00:00-08:00"`，
  窗口外自动暂停等待。
- **预算**：填 `budget_cny` 和 `price_in/price_out`，到点自动停。
- **改术语**：面板里改完，下次 resume 只重译命中该词的段落。
- **看进度**：面板、`/api/v1/jobs`、或 `journalctl -u slowtrans -f`。

---

## 两种模式能混用吗

能，而且刻意设计成这样：

- 同一个 `data/slowtrans.db`、同一个 `job_id` 算法、同一套缓存键
- 服务在跑的任务，CLI 也能看到进度（事件写同一张表）
- 但**不要同时跑同一个任务**——`jobs.locked_by` 会拦住第二个进程，
  它只会打印"任务正被 xxx 处理"然后退出

## 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| `slowtrans: command not found` | venv 没激活，或没用 `-e` 安装 |
| `serve` 报 `ModuleNotFoundError` | 只装了核心包，`pip install "slowtrans[api]"` |
| 面板 401 | token 和 `/etc/slowtrans/env` 里不一致 |
| 面板书库是空的 | 「设置」里的书库目录不对，或目录里没有 `.epub` |
| 所有 API 调用都 401/400 | key 末尾带 `\r`（env 用了 CRLF）或 key 过期 |
| 翻译很慢 | 免费模型正常速度约 10–16 段/分钟；换 `THUDM/GLM-4-9B-0414` 更快 |
| 某些段没译（保留原文） | 占位符校验没过，降级重译也失败，标记在 `review`，面板里能看到原因 |
| 书名/标题还是英文 | 说明用的是旧版本分段规则；升级后重跑会重置受影响的段 |
