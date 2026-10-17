# AstrBot 桥接插件

只做 HTTP 客户端，不在 Bot 进程里跑任何翻译逻辑——服务器只有 1.7 GB 内存，
AstrBot 自己就占了约 690 MB，插件里再加载 lxml 或并发任务很容易把机器人一起拖死。

## 安装

AstrBot 4.x 的插件市场走 `raw.githubusercontent.com`，部分网络环境不通。用 scp 传目录最稳：

```bash
scp -r clients/astrbot-plugin aliyun:/tmp/slowtrans-plugin
ssh aliyun 'sudo mv /tmp/slowtrans-plugin /srv/astrbot/data/plugins/astrbot_plugin_slowtrans \
  && sudo chown -R deploy:deploy /srv/astrbot/data/plugins/astrbot_plugin_slowtrans'
ssh aliyun 'sudo systemctl restart astrbot'
```

（插件目录名必须以 `astrbot_plugin_` 开头，这是 AstrBot 的加载规则。）

然后在面板的插件配置里填：

- `api_base`: `http://127.0.0.1:8790`
- `token`: 与服务器 `/etc/slowtrans/env` 的 `SLOWTRANS_TOKEN` 相同

## 命令

| 命令 | 作用 |
|---|---|
| `/翻译 <服务器上的epub路径> [zh-CN]` | 提交任务，返回 job_id |
| `/译进度 [job_id]` | 不带参数看最近 5 个任务 |
| `/译暂停 <job_id>` | 当前批次结束后停 |
| `/译继续 <job_id>` | 重新排队 |

任务完成时插件会主动往发起命令的会话推一条消息（依赖 AstrBot 4.x 的
`context.send_message`；如果你的版本签名不同，会降级为“只能手动查”，不会报错崩掉）。

## 路径注意事项

`<服务器上的epub路径>` 指的是 ECS 上的路径，比如 `/srv/webdav/data/books/xxx.epub`。
QQ 里直接发文件给 Bot 是另一个工作流（先把文件落到服务器），本插件不处理上传。
