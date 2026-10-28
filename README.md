# 小小鱼 · DeepSeek娘 QQ 机器人

把 DeepSeek 拟人化的「大肥鱼」养在 QQ 里：有记忆、有情绪、会发表情包、会主动找你、还能改签名发说说。

基于 [AstrBot](https://github.com/AstrBotDevs/AstrBot) + [NapCatQQ](https://github.com/NapNeko/NapCatQQ) + DeepSeek API。

## 架构

```
QQ 账号（NapCat 协议端）
   ↓  OneBot v11（反向 WebSocket）
AstrBot（平台 + 插件 + 人格）
   ↓
DeepSeek API（聊天）
智谱 GLM-4V-Plus（识图）
Edge TTS（语音）
```

## 功能

| 功能 | 说明 |
|---|---|
| 人格 | 盐系社畜 + 天然呆（见 `persona/system_prompt.txt`） |
| 短句 | 每条 ≤20 字，多句分多条发送 |
| 表情包 | 253 张图，按情绪标签选图，25% 概率随机发 |
| 识图 | 图片先由视觉模型转文字描述，再交给 DeepSeek |
| 语音 | 唱歌/撒娇/念诗时用 Edge TTS 发语音 |
| 记忆 | 主人长期记忆（按内容定过期）+ 群聊临时记忆（2 天） |
| 主动聊天 | 10:00-23:59，1 小时起，未回翻倍（上限 8 小时） |
| 说说/签名 | 模型输出标记，插件调 NapCat API 执行 |
| 记账 | 每日 token 消耗与费用估算 |
| 日记 | 每晚 23:30 用当天对话写日记 |
| 角色扮演 | 临时叠加角色，不改人设本体 |
| 管理权限 | `/admin` 重启/状态/开关功能（只对主人） |
| 防骚扰 | 非好友私聊直接拒绝 |

## 目录

```
plugin/           AstrBot 插件源码（复制到 data/plugins/astrbot_plugin_deepseek_lady/）
persona/          人格设定
deploy/           部署脚本与 systemd 服务
docs/             运维笔记
```

## 部署

见 [`docs/DEPLOY.md`](docs/DEPLOY.md)。

## 配置要点

插件顶部常量区可调，详见源文件注释。主要参数：

| 常量 | 默认 | 说明 |
|---|---|---|
| `MAX_CHARS_PER_MESSAGE` | 20 | 每条消息最大字数 |
| `RANDOM_MEME_PROBABILITY` | 0.25 | 随机发表情包概率 |
| `DAILY_GROUP_REPLY_LIMIT` | 40 | 群友每天合计回复上限 |
| `OWNER_QQ` | 需填 | 主人 QQ 号 |
| `SIGNATURE_COOLDOWN_HOURS` | 24 | 改签名冷却 |
| `QZONE_COOLDOWN_HOURS` | 48 | 发说说冷却 |
| `PROACTIVE_BASE_SECONDS` | 3600 | 主动聊天基础间隔 |

## 注意

- **不要提交任何 API Key / 密码**，`config/` 里只有模板
- NapCat 重启需要重新扫码
- 2G 内存是临界值，建议 4G

## License

自用项目，随手写的，仅供参考。
