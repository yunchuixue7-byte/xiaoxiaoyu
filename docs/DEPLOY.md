# 部署指南

> 基于 Ubuntu 22.04，2核2G（最低），目标是一次性把机器人跑起来。

## 0. 前置

```bash
apt update && apt install -y python3.10 python3.10-venv ffmpeg xvfb imagemagick
```

- `ffmpeg`：TTS 转音频必需
- `xvfb`：NapCat 需要图形环境
- `imagemagick`：出问题时可截图排查

## 1. AstrBot

```bash
# 建 venv + 装 AstrBot（版本 4.14.x）
python3.10 -m venv /opt/astrbot-venv
/opt/astrbot-venv/bin/pip install astrbot -i https://pypi.tuna.tsinghua.edu.cn/simple

# 标记根目录（否则报「不是有效的 AstrBot 根目录」）
touch /root/.astrbot

# 首次运行（会生成 /root/data）
/opt/astrbot-venv/bin/astrbot run
```

Dashboard 若下载失败，手动下载 dist.zip 解压到 `/root/data/`：

```
https://astrbot-registry.soulter.top/download/astrbot-dashboard/v4.14.6/dist.zip
```

## 2. NapCatQQ

```bash
# 下载 NapCat.Shell.zip 解压到 /root/Napcat
# 用官方 install.sh 把 NapCat 注入 QQ
```

启动方式见 `deploy/napcat.service`（需要 `HOME` 和 `DISPLAY`，否则 SIGILL）。

首次启动会输出二维码，扫描登录 QQ。

## 3. 配置 OneBot 连接

编辑 `/root/Napcat/opt/QQ/resources/app/app_launcher/napcat/config/onebot11_<QQ号>.json`：

```json
{
  "network": {
    "websocketClients": [
      {
        "name": "astrbot",
        "enable": true,
        "url": "ws://127.0.0.1:6199/ws",
        "messagePostFormat": "array",
        "reportSelfMessage": false,
        "token": "",
        "debug": false,
        "heartInterval": 30000,
        "reconnectInterval": 3000
      }
    ]
  }
}
```

AstrBot 侧添加 `aiocqhttp` 平台，端口 6199。

## 4. 模型配置

`/root/data/cmd_config.json` 里配置：

| 项目 | 说明 |
|---|---|
| `provider_sources` | DeepSeek（api.deepseek.com/v1）、智谱（open.bigmodel.cn/api/paas/v4） |
| `provider` | `deepseek/deepseek-chat`（聊天）、`zhipu/glm-4v-plus`（识图）、`edge_tts`（语音） |
| `provider_settings.default_image_caption_provider_id` | `zhipu/glm-4v-plus` |
| `provider_settings.computer_use_runtime` | `none`（**必须**，否则模型会去调 shell） |
| `provider_tts_settings` | enable=true, provider_id=edge_tts, trigger_probability=0（由插件控制） |
| `provider_ltm_settings.group_icl_enable` | true（群聊上下文） |
| `wake_prefix` | `[]`（不要用 `/` 唤醒） |

参考 `deploy/cmd_config.template.json`（已脱敏）。

## 5. 插件

```bash
mkdir -p /root/data/plugins/astrbot_plugin_deepseek_lady
cp plugin/main.py plugin/metadata.yaml /root/data/plugins/astrbot_plugin_deepseek_lady/
```

改 `main.py` 顶部常量：
- `OWNER_QQ` 改成主人 QQ
- 其它按需

## 6. 人格

用 `persona/system_prompt.txt` 的内容，写入数据库：

```bash
/opt/astrbot-venv/bin/python - <<'EOF'
import sqlite3
conn = sqlite3.connect("/root/data/data_v4.db")
conn.execute(
    "UPDATE personas SET system_prompt=?, updated_at=datetime('now') WHERE persona_id='小小鱼'",
    (open("/root/persona/system_prompt.txt", encoding="utf-8").read(),),
)
conn.commit()
EOF
```

> 注意：人格的 `tools` / `skills` 字段必须是 **NULL**，否则会清空工具集。

## 7. systemd

```bash
cp deploy/*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now xvfb napcat astrbot
```

## 8. 验证

```bash
systemctl status astrbot napcat xvfb
journalctl -u astrbot -f
```

QQ 私聊机器人发一句话，有回复即成功。

## 常见问题

| 现象 | 原因 |
|---|---|
| QQ 启动 SIGILL | 缺 `Environment=HOME=/root` |
| TTS 失败 `No such file: ffmpeg` | 没装 ffmpeg |
| 模型说「没工具」 | 人格 `tools` 字段不是 NULL |
| 模型调 shell 被拒 | `computer_use_runtime` 没设成 none |
| 扫码后卡住 | 用错账号扫（必须用机器人号） |
| 内存爆掉 | 2G 太紧，加 swap 或升 4G |
