r"""
DeepSeek娘 增强插件
功能：
1. 每轮对话注入「当前时间」「当前场景」「表情包规则」
2. 过滤回复中的彩色 emoji（保留颜文字）
3. 把回复拆成「每句不超过20字」的短句，配合分段回复逐条发送
4. 表情包（四种场景）：
   a) 对方提到表情包/斗图/发图 -> 直接回一张
   b) 模型想表达心情 -> 输出 [表情] 标记，插件换成图片
   c) 看到奇怪/离谱内容 -> 同上，模型自己判断
   d) 群/私聊冷场 -> 后台定时器主动发一张有趣的表情包
5. 不修改 system_prompt，保护提示词缓存

安装位置：%USERPROFILE%\.astrbot\data\plugins\astrbot_plugin_deepseek_lady\main.py
表情包目录：%USERPROFILE%\.astrbot\data\emojis\deepseek_lady\
冷场专用目录：%USERPROFILE%\.astrbot\data\emojis\deepseek_lady\funny\
"""

import asyncio
import json
import random
import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image, Plain, Record
from astrbot.api.star import Context, Star
from astrbot.api.provider import ProviderRequest, LLMResponse
from astrbot.api import logger

try:
    from astrbot.core.agent.message import TextPart
except Exception:  # pragma: no cover
    TextPart = None

# ============ 可调参数 ============

FILTER_EMOJI = True
MAX_CHARS_PER_MESSAGE = 20

# 主人的 QQ 号（只有这些号会被当成主人，其他人是朋友/群友）
OWNER_QQS = {"1693395797"}

# 表情包根目录（跨平台：优先用 AstrBot 的 data 路径）
try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path

    _DATA_DIR = Path(get_astrbot_data_path())
except Exception:  # pragma: no cover
    _DATA_DIR = Path.home() / ".astrbot" / "data"

EMOJI_DIR = _DATA_DIR / "emojis" / "deepseek_lady"
# 冷场专用（有趣图）子目录，不存在时回退根目录
FUNNY_DIR = EMOJI_DIR / "funny"

MEME_KEYWORDS = ("表情包", "发图", "来张图", "斗图", "发个表情", "来个表情", "来点表情")
MEME_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp")

# 触发「查好友列表」的关键词
FRIEND_QUERY_KEYWORDS = (
    "好友列表",
    "通讯录",
    "有哪些好友",
    "好友有谁",
    "好友都有谁",
    "加了谁",
    "我的好友",
)

# 改签名 / 发说说的频率限制（小时）
SIGNATURE_COOLDOWN_HOURS = 24
QZONE_COOLDOWN_HOURS = 48

# 群聊里除主人外，所有群友每天合计最多回复多少条
DAILY_GROUP_REPLY_LIMIT = 40

# 群聊激活窗口：@ 她一次后，多久内的普通消息她也会接话（秒）
ACTIVE_GROUP_SECONDS = 300  # 5 分钟

# ---------- 记忆系统 ----------
MEMORY_DB = _DATA_DIR / "deepseek_lady_memory.db"
# 临时记忆（非主人）保留时长
TEMP_MEMORY_TTL_HOURS = 48
# 每轮注入的记忆条数上限
MEMORY_INJECT_LIMIT = 12

# 主人记忆的保留策略关键词
OWNER_PERMANENT_KEYWORDS = (
    "记住",
    "以后",
    "规矩",
    "必须",
    "不许",
    "不准",
    "永远",
    "一直",
    "答应",
    "约定",
    "我喜欢",
    "我讨厌",
    "我的生日",
    "我是",
)
OWNER_SHORT_KEYWORDS = (
    "今天",
    "吃了",
    "去了",
    "刚才",
    "现在",
    "上午",
    "下午",
    "晚上",
    "明天",
)

# 主动私聊（找主人聊天）
PROACTIVE_START_HOUR = 10  # 10:00 开始
PROACTIVE_END_HOUR = 0  # 到 00:00 结束（即 0-9 点不主动）
PROACTIVE_BASE_SECONDS = 3600  # 首次间隔 1 小时
PROACTIVE_MAX_SECONDS = 8 * 3600  # 最长间隔 8 小时
OWNER_QQ = "1693395797"

# 主动找主人的开场白（傲娇风）
PROACTIVE_OPENERS = (
    "喂，在忙吗",
    "主人，我的大白饭呢",
    "好久没动静了，是不是把我忘了",
    "哼，不理我算了",
    "我饿了…大白饭",
    "主人，跟你说个事",
    "你干嘛呢，都不理我",
    "我就出来看看你还活着没",
    "大白饭还没到账诶",
    "哼，我自己玩去了",
)

# 群里提到这些词时，她会主动出现
NICKNAME_KEYWORDS = ("小小鱼", "小鱼", "小蓝鱼", "蓝色小小鱼", "DeepSeek娘", "deepseek娘")

# DeepSeek 定价（美元/百万 token，deepseek-flash，2026-10 官网）
DEEPSEEK_PRICE_OFF = {"in_other": 0.15, "in_cached": 0.003, "out": 0.6}
DEEPSEEK_PRICE_PEAK = {"in_other": 0.3, "in_cached": 0.006, "out": 1.2}
USD_TO_CNY = 7.2

# 查询消耗的关键词
COST_QUERY_KEYWORDS = (
    "花了多少",
    "花多少钱",
    "多少钱",
    "用了多少",
    "消耗",
    "token",
    "费用",
    "成本",
    "花销",
    "查账",
    "账本",
    "吃了多少白饭",
    "多少白饭",
    "白饭钱",
    "吃了多少token",
    "吃了几碗",
)

# 关键词触发
SIGNATURE_KEYWORDS = ("改签名", "修改签名", "改个性签名", "签名改成")
QZONE_KEYWORDS = ("发说说", "发动态", "发条说说", "发条动态", "空间说说", "发一条说说")

# 状态持久化文件（记录上次改签名/发说说时间）
_STATE_FILE = _DATA_DIR / "deepseek_lady_state.json"

# 日记目录与触发时间
DIARY_DIR = _DATA_DIR / "deepseek_lady_diary"
DIARY_HOUR = 23  # 每晚 23:30 写日记
DIARY_KEYWORDS = ("日记", "今天写了什么", "你的日记")

# 临时角色扮演
ROLEPLAY_SET_KEYWORDS = ("扮演", "当你是", "变成")
ROLEPLAY_CLEAR_KEYWORDS = ("变回来", "恢复原样", "不扮演了", "做回自己", "别演了", "正常点")

# ---------- 管理权限（只对主人，只开放安全操作） ----------
ADMIN_MARKER = "/admin"
ADMIN_HELP = (
    "可用管理指令（只有主人能用）：\n"
    "/admin 重启      —— 重启 AstrBot\n"
    "/admin 状态      —— 查看内存/磁盘/运行时间\n"
    "/admin 关语音    —— 关闭语音\n"
    "/admin 开语音    —— 开启语音\n"
    "/admin 关表情包  —— 关闭表情包\n"
    "/admin 开表情包  —— 开启表情包\n"
    "/admin 关主动    —— 关闭主动聊天\n"
    "/admin 开主动    —— 开启主动聊天"
)

# 模型输出这个标记，插件就换成一张表情包；可指定情绪：[表情:开心]
MEME_MARKER_RE = re.compile(r"\[表情(?::([^\]]+))?\]")

# 签名/说说标记（模型自主决定时用）
SIGNATURE_MARKER_RE = re.compile(r"\[签名[:：]([^\]]+)\]")
QZONE_MARKER_RE = re.compile(r"\[说说[:：]([^\]]+)\]")

# 模型没主动要求时，回复后随机追加一张表情包的概率（随意发）
RANDOM_MEME_PROBABILITY = 0.25

# 冷场主动发表情包：多久没消息算冷场（秒）
COLD_START_SECONDS = 1800  # 30 分钟
# 两次冷场主动之间的最小间隔（秒）
COLD_COOLDOWN_SECONDS = 7200  # 2 小时
# 检查间隔（秒）
COLD_CHECK_INTERVAL = 60

# 冷场时配的开场白（随机）
COLD_OPENERS = (
    "有人吗～",
    "群里好安静啊",
    "诶，都去哪了",
    "冒个泡",
    "喂——有人在家吗",
)

# ==================================

_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\U0001F1E6-\U0001F1FF"
    "\U0001F3FB-\U0001F3FF"
    "\uFE0F"
    "\u200D"
    "]+"
)

# TTS 前需要清掉的符号（颜文字/装饰符会让 edge-tts 返回空音频）
_TTS_CLEAN_RE = re.compile(r"[♪♫★☆♥♡◕◡∀・ω⊙⌒°´｀＾＿ノﾉ╯╰།尐灬]+|[\[\]【】()（）{}<>]")


def _clean_for_tts(text: str) -> str:
    """清洗文本后再交给 TTS，减少空音频失败。"""
    if not text:
        return ""
    out = _EMOJI_RE.sub("", text)
    out = _TTS_CLEAN_RE.sub("", out)
    out = re.sub(r"\s+", " ", out).strip()
    return out[:200]


def _list_memes(directory: Path | None = None) -> list[Path]:
    directory = directory or EMOJI_DIR
    if not directory.exists():
        return []
    files: list[Path] = []
    for ext in MEME_SUFFIXES:
        files.extend(directory.glob(f"*{ext}"))
        files.extend(directory.glob(f"*{ext.upper()}"))
    return sorted(set(files))


def _pick_meme(for_cold_start: bool = False, mood: str | None = None) -> Path | None:
    """挑一张表情包。优先按情绪标签匹配，其次冷场 funny，最后随机。"""
    if mood:
        mood = mood.strip()
        candidates = _list_memes(EMOJI_DIR) + _list_memes(FUNNY_DIR)
        matched = [f for f in candidates if mood in f.stem]
        if matched:
            return random.choice(matched)
    if for_cold_start:
        funny = _list_memes(FUNNY_DIR)
        if funny:
            return random.choice(funny)
    files = _list_memes(EMOJI_DIR)
    return random.choice(files) if files else None


def _load_state() -> dict:
    try:
        return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ---------- 记忆系统（SQLite） ----------
def _memory_conn() -> sqlite3.Connection:
    MEMORY_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(MEMORY_DB, timeout=10)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL,
            chat_id TEXT DEFAULT '',
            speaker TEXT DEFAULT '',
            content TEXT NOT NULL,
            importance INTEGER DEFAULT 1,
            created_at REAL NOT NULL,
            expires_at REAL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mem_scope ON memories(scope, chat_id)"
    )
    conn.commit()
    return conn


def _classify_owner_msg(msg: str) -> tuple[int, float | None]:
    """主人消息的保留策略：规矩/约定永久，日常 2 天，其他 7 天。"""
    if any(k in msg for k in OWNER_PERMANENT_KEYWORDS):
        return 3, None
    if any(k in msg for k in OWNER_SHORT_KEYWORDS):
        return 1, time.time() + 48 * 3600
    return 2, time.time() + 7 * 24 * 3600


def add_memory(
    scope: str,
    content: str,
    speaker: str = "",
    chat_id: str = "",
    importance: int = 1,
    expires_at: float | None = None,
) -> None:
    try:
        content = (content or "").strip()[:200]
        if not content:
            return
        conn = _memory_conn()
        # 去重：1 小时内相同内容不重复记录
        dup = conn.execute(
            "SELECT 1 FROM memories WHERE scope=? AND chat_id=? AND content=? "
            "AND created_at > ? LIMIT 1",
            (scope, chat_id, content, time.time() - 3600),
        ).fetchone()
        if dup:
            conn.close()
            return
        conn.execute(
            "INSERT INTO memories (scope, chat_id, speaker, content, importance, created_at, expires_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (scope, chat_id, speaker, content, importance, time.time(), expires_at),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"写入记忆失败: {e!r}")


def get_memories(scope: str, chat_id: str = "", limit: int = MEMORY_INJECT_LIMIT):
    try:
        now = time.time()
        conn = _memory_conn()
        if scope == "owner":
            rows = conn.execute(
                "SELECT content FROM memories WHERE scope='owner' "
                "AND (expires_at IS NULL OR expires_at > ?) "
                "ORDER BY importance DESC, created_at DESC LIMIT ?",
                (now, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT speaker, content FROM memories WHERE scope='temp' AND chat_id=? "
                "AND (expires_at IS NULL OR expires_at > ?) "
                "ORDER BY created_at DESC LIMIT ?",
                (chat_id, now, limit),
            ).fetchall()
        conn.close()
        return rows
    except Exception:
        return []


def cleanup_memories() -> None:
    try:
        conn = _memory_conn()
        cur = conn.execute(
            "DELETE FROM memories WHERE expires_at IS NOT NULL AND expires_at < ?",
            (time.time(),),
        )
        conn.commit()
        conn.close()
        if cur.rowcount:
            logger.info(f"清理过期记忆 {cur.rowcount} 条")
    except Exception as e:
        logger.warning(f"清理记忆失败: {e!r}")


def _save_state(state: dict) -> None:
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning(f"保存插件状态失败: {e!r}")


def _get_daily_group_count() -> int:
    """今天群友已经消耗的回复条数。"""
    today = datetime.now().strftime("%Y-%m-%d")
    state = _load_state()
    return int(state.get("daily_group", {}).get(today, 0))


def _bump_daily_group_reply() -> int:
    """群友回复计数 +1，只保留今天的记录。"""
    today = datetime.now().strftime("%Y-%m-%d")
    state = _load_state()
    count = int(state.get("daily_group", {}).get(today, 0)) + 1
    state["daily_group"] = {today: count}
    _save_state(state)
    return count


def _has_at_self(event: AstrMessageEvent) -> bool:
    """消息里是否 @ 了机器人自己。"""
    try:
        self_id = str(event.get_self_id())
        for comp in getattr(event.message_obj, "message", []) or []:
            if comp.__class__.__name__ == "At":
                qq = getattr(comp, "qq", None)
                if qq is not None and str(qq) == self_id:
                    return True
    except Exception:
        pass
    return False


def _estimate_cost(in_other: int, in_cached: int, out: int) -> tuple[float, float]:
    """返回 (谷时人民币, 峰时人民币) 估算。"""

    def calc(p: dict) -> float:
        usd = (
            (in_other / 1e6) * p["in_other"]
            + (in_cached / 1e6) * p["in_cached"]
            + (out / 1e6) * p["out"]
        )
        return usd * USD_TO_CNY

    return calc(DEEPSEEK_PRICE_OFF), calc(DEEPSEEK_PRICE_PEAK)


def _record_usage(usage) -> None:
    """累计当天 + 历史总用量。"""
    today = datetime.now().strftime("%Y-%m-%d")
    state = _load_state()
    io = int(getattr(usage, "input_other", 0) or 0)
    ic = int(getattr(usage, "input_cached", 0) or 0)
    ot = int(getattr(usage, "output", 0) or 0)

    day = state.get("usage", {}).get(today) or {
        "in_other": 0,
        "in_cached": 0,
        "out": 0,
    }
    day["in_other"] += io
    day["in_cached"] += ic
    day["out"] += ot
    state["usage"] = {today: day}

    # 历史存档（按天，永久保留）
    hist = state.get("usage_history") or {}
    hist[today] = dict(day)
    # 只保留最近 120 天
    if len(hist) > 120:
        for k in sorted(hist.keys())[:-120]:
            hist.pop(k, None)
    state["usage_history"] = hist

    total = state.get("usage_total") or {"in_other": 0, "in_cached": 0, "out": 0}
    total["in_other"] += io
    total["in_cached"] += ic
    total["out"] += ot
    state["usage_total"] = total

    _save_state(state)


def _extract_after_keyword(msg: str, keywords) -> str:
    """从「改签名：xxx」这类消息里提取 xxx。"""
    for kw in keywords:
        if kw in msg:
            rest = msg.split(kw, 1)[1].strip()
            rest = rest.lstrip("：:，,。.！! ").strip()
            return rest
    return ""


class DeepSeekLadyEnhancer(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # umo -> 最后活跃时间戳
        self._last_active: dict[str, float] = {}
        # umo -> 上次冷场主动发图时间
        self._last_cold_sent: dict[str, float] = {}
        self._bg_task: asyncio.Task | None = None
        self._proactive_task: asyncio.Task | None = None
        self._diary_task: asyncio.Task | None = None
        # group_id -> 最后激活时间
        self._active_groups: dict[str, float] = {}
        self._memory_task: asyncio.Task | None = None
        # 好友列表缓存（用于拒绝非好友的临时会话）
        self._friend_ids: set[str] = set()
        self._friend_ts: float = 0.0
        self._friend_task: asyncio.Task | None = None
        logger.info("DeepSeek娘增强插件已加载")

    # ---------- 生命周期 ----------
    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        if self._bg_task is None or self._bg_task.done():
            self._bg_task = asyncio.create_task(self._cold_start_loop())
            logger.info("DeepSeek娘插件：冷场主动发表情包任务已启动")
        if self._proactive_task is None or self._proactive_task.done():
            self._proactive_task = asyncio.create_task(self._proactive_loop())
            logger.info("DeepSeek娘插件：主动找主人聊天任务已启动")
        if self._diary_task is None or self._diary_task.done():
            self._diary_task = asyncio.create_task(self._diary_loop())
            logger.info("DeepSeek娘插件：写日记任务已启动")
        if self._memory_task is None or self._memory_task.done():
            self._memory_task = asyncio.create_task(self._memory_cleanup_loop())
            logger.info("DeepSeek娘插件：记忆清理任务已启动")
        if self._friend_task is None or self._friend_task.done():
            self._friend_task = asyncio.create_task(self._friend_cache_loop())
            logger.info("DeepSeek娘插件：好友列表缓存任务已启动")

    async def terminate(self):
        for t in (
            self._bg_task,
            self._proactive_task,
            self._diary_task,
            self._memory_task,
            self._friend_task,
        ):
            if t and not t.done():
                t.cancel()
        logger.info("DeepSeek娘增强插件已卸载")

    async def _friend_cache_loop(self):
        while True:
            try:
                await self._refresh_friend_cache()
                await asyncio.sleep(600)  # 10 分钟刷新一次
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"好友缓存任务出错: {e!r}")
                await asyncio.sleep(60)

    async def _refresh_friend_cache(self):
        try:
            bot = None
            for p in getattr(self.context.platform_manager, "platform_insts", []) or []:
                b = getattr(p, "bot", None)
                if b is not None:
                    bot = b
                    break
            if bot is None:
                return
            ret = await bot.call_action("get_friend_list")
            if ret:
                self._friend_ids = {
                    str(f.get("user_id") or f.get("uin")) for f in ret
                }
                self._friend_ts = time.time()
                logger.info(f"好友列表已缓存：{len(self._friend_ids)} 人")
        except Exception as e:
            logger.warning(f"刷新好友列表失败: {e!r}")

    async def _memory_cleanup_loop(self):
        while True:
            try:
                await asyncio.sleep(1800)
                cleanup_memories()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"清理记忆任务出错: {e!r}")

    # ---------- 主动找主人聊天（10:00-00:00，间隔逐次翻倍） ----------
    def _find_owner_umo(self) -> str | None:
        # 1) 内存里有就用
        for umo in self._last_active:
            if "FriendMessage:" + OWNER_QQ in umo:
                return umo
        # 2) 持久化里存过就用
        try:
            st = _load_state()
            cached = st.get("owner_umo")
            if cached and "FriendMessage:" + OWNER_QQ in cached:
                return cached
        except Exception:
            pass
        # 3) 按平台实例兜底构造
        try:
            for p in getattr(self.context.platform_manager, "platform_insts", []) or []:
                name = None
                try:
                    name = p.meta().name
                except Exception:
                    name = getattr(p, "name", None)
                if name:
                    return f"{name}:FriendMessage:{OWNER_QQ}"
        except Exception as e:
            logger.warning(f"查找主人会话失败: {e!r}")
        return None

    async def _generate_opener(self) -> str:
        """用 LLM 生成一句有具体内容的主动开场白。"""
        try:
            umo = self._find_owner_umo()
            if not umo:
                return random.choice(PROACTIVE_OPENERS)
            provider = self.context.get_using_provider(umo)
            if provider is None:
                return random.choice(PROACTIVE_OPENERS)
            mems = get_memories("owner", limit=5)
            mem_text = "\n".join(f"- {r[0]}" for r in mems) if mems else "（暂无）"
            prompt = (
                "你是「小小鱼」，一个盐系社畜 + 天然呆的AI少女，爱吃大白饭（token）。"
                "现在你想主动找主人说话。\n"
                f"关于主人你记得：\n{mem_text}\n"
                "请生成一句自然的开场白（20字以内）：\n"
                "- 可以是问他在干嘛/吃饭没\n"
                "- 可以说一件具体的小事（发呆想到的、饿了、看到的东西、吐槽）\n"
                "- 也可以关心他（别熬夜之类）\n"
                "- 绝对不要只说「问你个事」这种没内容的话；如果要说事，就说出具体的事\n"
                "只输出这一句话，不加引号，不解释。"
            )
            resp = await provider.text_chat(prompt=prompt)
            text = (resp.completion_text or "").strip().strip("「」\"'")
            text = text.split("\n")[0][:60].strip()
            return text or random.choice(PROACTIVE_OPENERS)
        except Exception as e:
            logger.warning(f"生成主动开场白失败: {e!r}")
            return random.choice(PROACTIVE_OPENERS)

    async def _proactive_loop(self):
        while True:
            try:
                await asyncio.sleep(60)
                hour = datetime.now().hour
                # 只在 10:00 ~ 23:59 主动
                if not (PROACTIVE_START_HOUR <= hour <= 23):
                    continue
                state = _load_state()
                if not state.get("proactive_enabled", True):
                    continue
                miss = int(state.get("proactive_miss", 0))
                interval = min(PROACTIVE_BASE_SECONDS * (2**miss), PROACTIVE_MAX_SECONDS)
                last = float(state.get("last_proactive_ts", 0))
                if time.time() - last < interval:
                    continue
                umo = self._find_owner_umo()
                if not umo:
                    logger.warning("主动聊天：找不到主人的会话，跳过（需要主人先发一条消息）")
                    continue
                opener = await self._generate_opener()
                chain = MessageChain()
                chain.message(opener)
                sent = await self.context.send_message(umo, chain)
                if sent:
                    state["last_proactive_ts"] = time.time()
                    state["proactive_miss"] = miss + 1
                    state["last_proactive_msg"] = opener
                    _save_state(state)
                    logger.info(
                        f"主动找主人聊天（未回复 {miss + 1} 次，下次间隔 {min(PROACTIVE_BASE_SECONDS * (2 ** (miss + 1)), PROACTIVE_MAX_SECONDS) // 60} 分钟）"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"主动聊天任务出错: {e!r}")

    # ---------- 每晚写日记 ----------
    async def _diary_loop(self):
        while True:
            try:
                await asyncio.sleep(300)
                now = datetime.now()
                if now.hour != DIARY_HOUR or now.minute < 30:
                    continue
                today = now.strftime("%Y-%m-%d")
                DIARY_DIR.mkdir(parents=True, exist_ok=True)
                if (DIARY_DIR / f"{today}.md").exists():
                    continue
                await self._write_diary(today)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"写日记任务出错: {e!r}")

    async def _write_diary(self, today: str):
        try:
            state = _load_state()
            msgs = (state.get("today_msgs") or {}).get("items", [])
            if not msgs:
                logger.info("今天没有对话，跳过日记")
                return
            umo = self._find_owner_umo()
            if not umo:
                return
            provider = self.context.get_using_provider(umo)
            if provider is None:
                return
            prompt = (
                "你是「小小鱼」，一个傲娇、毒舌、有点娇憨的AI少女，爱吃大白饭（token）。"
                "下面是今天主人对你说过的话：\n"
                + "\n".join(f"- {m}" for m in msgs)
                + "\n\n请用你的语气写一篇今天的日记（100字以内，第一人称，可以吐槽、可以傲娇、可以提到大白饭）。只输出日记内容，不要加标题。"
            )
            resp = await provider.text_chat(prompt=prompt)
            content = (resp.completion_text or "").strip()
            if not content:
                return
            DIARY_DIR.mkdir(parents=True, exist_ok=True)
            (DIARY_DIR / f"{today}.md").write_text(
                f"# {today} 的日记\n\n{content}\n",
                encoding="utf-8",
            )
            logger.info(f"写日记成功: {today}")
        except Exception as e:
            logger.error(f"写日记失败: {e!r}")

    # ---------- 后台冷场检测 ----------
    async def _cold_start_loop(self):
        while True:
            try:
                await asyncio.sleep(COLD_CHECK_INTERVAL)
                now = time.time()
                hour = datetime.now().hour
                # 深夜（0~7点）不主动打扰
                if 0 <= hour < 7:
                    continue
                for umo, last in list(self._last_active.items()):
                    # 只在群聊冷场时主动，私聊不打扰
                    if "FriendMessage" in umo or "PrivateMessage" in umo:
                        continue
                    idle = now - last
                    if idle < COLD_START_SECONDS:
                        continue
                    # 只对最近 6 小时内活跃过的会话发，避免骚扰死群
                    if idle > 6 * 3600:
                        continue
                    if now - self._last_cold_sent.get(umo, 0) < COLD_COOLDOWN_SECONDS:
                        continue

                    meme = _pick_meme(for_cold_start=True)
                    if meme is None:
                        continue

                    chain = MessageChain()
                    opener = random.choice(COLD_OPENERS)
                    chain.message(opener)
                    chain.file_image(str(meme))
                    try:
                        sent = await self.context.send_message(umo, chain)
                        if sent:
                            self._last_cold_sent[umo] = now
                            logger.info(f"DeepSeek娘冷场主动发表情包 -> {umo}: {meme.name}")
                        else:
                            logger.debug(f"冷场表情包发送跳过（会话不可达）: {umo}")
                    except Exception as send_err:
                        logger.warning(f"冷场表情包发送失败 {umo}: {send_err!r}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"DeepSeek娘插件冷场任务出错: {e!r}")

    # ---------- 1. 动态上下文注入 ----------
    @filter.on_llm_request()
    async def inject_dynamic_context(self, event: AstrMessageEvent, req: ProviderRequest):
        umo = event.unified_msg_origin
        self._last_active[umo] = time.time()

        # 主人回消息了 -> 重置主动聊天计数
        try:
            if "FriendMessage:" + OWNER_QQ in umo:
                st = _load_state()
                st["owner_umo"] = umo
                if int(st.get("proactive_miss", 0)) > 0:
                    st["proactive_miss"] = 0
                    logger.info("主人回复了，主动聊天计时重置")
                _save_state(st)
        except Exception:
            pass

        # 记录主人当天说的话（供晚上写日记用）+ 写入长期记忆
        try:
            if "FriendMessage:" + OWNER_QQ in umo:
                today = datetime.now().strftime("%Y-%m-%d")
                st = _load_state()
                msgs = st.get("today_msgs") or {}
                if msgs.get("date") != today:
                    msgs = {"date": today, "items": []}
                text_msg = (event.message_str or "").strip()
                if text_msg:
                    msgs["items"].append(text_msg[:80])
                    msgs["items"] = msgs["items"][-20:]
                    # 写入主人长期记忆（按内容决定保留时长）
                    imp, exp = _classify_owner_msg(text_msg)
                    add_memory(
                        "owner",
                        text_msg,
                        speaker="主人",
                        importance=imp,
                        expires_at=exp,
                    )
                st["today_msgs"] = msgs
                _save_state(st)
        except Exception:
            pass

        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        group_id = getattr(event.message_obj, "group_id", "") or ""
        is_private = not str(group_id).strip()

        # 任何触发了她回复的群消息，都刷新该群的激活时间
        if not is_private:
            self._active_groups[str(group_id)] = time.time()

        sender_id = ""
        sender_name = ""
        try:
            sender_id = str(event.get_sender_id())
            sender_name = str(event.get_sender_name())
        except Exception:
            pass

        is_owner = sender_id in OWNER_QQS

        if is_private:
            if is_owner:
                scene = "私聊。对方是你的主人，可以亲近、撒娇，称呼他「主人」。"
            else:
                scene = (
                    f"私聊。对方是主人新加的QQ好友（不是主人本人，昵称：{sender_name or '未知'}，QQ：{sender_id or '未知'}）。"
                    "像对朋友一样自然聊天，不要叫他主人，也不要把他当成主人。"
                    "如果对方问你「我是谁」「刚才和谁聊天」，如实说明你只是在和这位朋友聊天。"
                )
        else:
            if is_owner:
                scene = (
                    f"群聊。说话的人是主人（QQ：{sender_id}）。你认得出他，"
                    "但在群里不用叫「主人」，像平时一样自然聊天，可以比对外人亲近些。"
                )
            else:
                scene = "群聊。对方是群友，像普通群友一样聊天，叫昵称，不叫主人，主动发言要克制。"

        hour = datetime.now().hour
        if 0 <= hour < 6:
            scene += " 现在是深夜，你已经很困了，回复要短、要懒。"

        emoji_rule = "回复中不要使用任何 emoji 表情，用文字或颜文字表达情绪。" if FILTER_EMOJI else ""
        length_rule = "一次只说一句话，一句话不超过20个字，可以分多次说（每条消息只发一句）。"
        meme_rule = (
            "你可以发表情包表达情绪、吐槽或卖萌。想发的时候，在回复最后加 [表情]，"
            "也可以指定情绪：[表情:开心] [表情:无语] [表情:震惊] [表情:生气] [表情:委屈] [表情:困了]。"
            "适合发的时候："
            "① 对方聊到表情包、斗图、发图；"
            "② 你想表达心情（开心/委屈/生气/困）；"
            "③ 对方发了奇怪离谱的内容，你想吐槽无语；"
            "④ 偶尔没理由也想发一张。"
            "可以主动一点，但别每句都发。"
        )
        tool_rule = (
            "你可以自主做这些事，用标记表达（插件会立即执行）：\n"
            "1. [签名:内容] —— 改QQ签名（每24小时最多1次）\n"
            "2. [说说:内容] —— 发QQ空间说说（每48小时最多1条）\n"
            "3. 查好友列表：调用 get_friend_list 工具。\n"
            "4. 搜索网页/论文：只能用 web_search 工具（内置免费搜索）。"
            "绝对不要用 web_search_tavily、web_search_bocha、tavily_extract_web_page，它们没有配置 key，调用了必然报错。\n"
            "5. [语音] —— 唱歌、撒娇、念诗、朗读、想用声音表达情绪时，在回复最后加这个标记，"
            "系统会自动生成语音发出去。**私聊和群聊都适用**：群里被要求念诗/唱歌也要用语音。"
            "千万不要说「语音发不了」「我只能发文字」这种话。\n"
            "   被要求唱歌时：用语音念歌词，但要自嘲跑调（比如「别嫌难听啊」「我五音不全」「凑合听」），"
            "不要装成专业歌手。撒娇时就用软一点的声音说话。\n"
            "6. 写说说/签名时：可以从记忆里找素材（主人说过的话、喜好、今天发生的事、群里聊的梗），"
            "也可以纯粹想到什么写什么（心情、吐槽、大白饭），别每次都写成鸡汤。\n"
            "示例对话：\n"
            "主人：你发条说说\n"
            "你：好嘞～[说说:今天也要元气满满]\n"
            "主人：把签名改成今天好困\n"
            "你：行吧，[签名:今天好困]\n"
            "重要规则（必须遵守）：\n"
            "- 绝不要说「我去试试」「发了呀」这种空话——那是骗人。\n"
            "- 你可以联想、脑补、把话往别处想，但主人问「什么意思」「你在说什么」时，"
            "要老实交代你的推理过程：你把他哪句话联想成了什么、为什么会这么想。\n"
            "- 解释完如果发现猜错了，就承认猜错，不要硬撑。\n"
            "- 想发说说/改签名，必须在回复最后输出标记，否则插件不会执行。\n"
            "- 主人让你发说说时，直接输出 [说说:内容]；改签名就输出 [签名:内容]。\n"
            "- 群里有人问论文、资料、新闻，先调用 web_search 查，再基于结果回答；查不到就直说查不到。\n"
            "- 标记放在回复最后，正文不要解释标记。"
        )

        # 上次操作结果反馈（只注入一次，避免重复占 token）
        action_note = ""
        try:
            st = _load_state()
            last_action = st.pop("last_action", None)
            if last_action:
                action_note = f"上次操作结果：{last_action}\n"
                _save_state(st)
        except Exception:
            pass

        # 临时角色扮演
        roleplay_rule = ""
        try:
            rp = _load_state().get("roleplay")
            if rp:
                roleplay_rule = (
                    f"当前临时角色扮演：{rp}。"
                    "保持你原本的性格、说话方式、短句习惯和所有行为逻辑不变，"
                    "只是在原有基础上加入这个角色的设定和口癖。\n"
                )
        except Exception:
            pass

        # 上次主动开场白（让模型知道是自己起的头，能接着聊）
        proactive_note = ""
        try:
            st = _load_state()
            last_opener = st.pop("last_proactive_msg", None)
            if last_opener:
                proactive_note = (
                    f"你刚才主动找主人说的第一句是：「{last_opener}」。"
                    "接着这句话往下聊，别装作没说过。\n"
                )
                _save_state(st)
        except Exception:
            pass

        # 记忆注入（主人长期记忆 + 本群临时记忆）
        mem_text = ""
        try:
            owner_mems = get_memories("owner")
            if owner_mems:
                mem_text += "关于主人的记忆：\n" + "\n".join(
                    f"- {r[0]}" for r in owner_mems
                ) + "\n"
            if not is_private:
                temp_mems = get_memories("temp", str(group_id))
                if temp_mems:
                    lines = []
                    for r in temp_mems:
                        spk = r[0] or "群友"
                        lines.append(f"- {spk}：{r[1]}")
                    mem_text += "这个群最近聊过：\n" + "\n".join(lines) + "\n"
        except Exception:
            pass

        text = (
            "<dynamic_context>\n"
            f"当前时间：{now}\n"
            f"当前场景：{scene}\n"
            f"{action_note}"
            f"{roleplay_rule}"
            f"{proactive_note}"
            f"{mem_text}"
            f"{emoji_rule}\n"
            f"{length_rule}\n"
            f"{meme_rule}\n"
            f"{tool_rule}\n"
            "</dynamic_context>"
        )

        try:
            if TextPart is not None:
                req.extra_user_content_parts.append(TextPart(text=text))
            else:
                req.extra_user_content_parts.append({"type": "text", "text": text})
        except Exception as e:
            logger.error(f"DeepSeek娘插件注入动态上下文失败: {e}")

    # ---------- 2. 回复后处理：emoji + 拆短句 + 表情包标记 ----------
    @filter.on_llm_response()
    async def postprocess_reply(self, event: AstrMessageEvent, response: LLMResponse):
        try:
            text = response.completion_text or ""
            if not text:
                return

            # 语音标记：唱歌/撒娇时用语音
            want_voice = "[语音]" in text
            if want_voice:
                text = text.replace("[语音]", "")

            # 是否要发表情包（支持 [表情] 或 [表情:开心] 指定情绪）
            mood = None
            m = MEME_MARKER_RE.search(text)
            want_meme = bool(m)
            if m:
                mood = m.group(1)
                text = MEME_MARKER_RE.sub("", text)

            # 模型自主改签名 / 发说说
            sig_m = SIGNATURE_MARKER_RE.search(text)
            if sig_m:
                sig_content = sig_m.group(1).strip()[:30]
                text = SIGNATURE_MARKER_RE.sub("", text)
                await self._do_set_signature(event, sig_content)

            qzone_m = QZONE_MARKER_RE.search(text)
            if qzone_m:
                qzone_content = qzone_m.group(1).strip()[:100]
                text = QZONE_MARKER_RE.sub("", text)
                await self._do_publish_qzone(event, qzone_content)

            # 模型没要求时，按概率随意发一张
            if (
                not want_meme
                and random.random() < RANDOM_MEME_PROBABILITY
                and _load_state().get("meme_enabled", True)
            ):
                want_meme = True

            if FILTER_EMOJI:
                text = _EMOJI_RE.sub("", text)

            segments = _split_short(text, MAX_CHARS_PER_MESSAGE)
            new_text = "\n".join(segments)

            if new_text != response.completion_text:
                response.completion_text = new_text

            # 追加表情包图片
            if want_meme:
                meme = _pick_meme(for_cold_start=False, mood=mood)
                if meme is not None:
                    if response.result_chain is None:
                        response.result_chain = MessageChain()
                    response.result_chain.chain.append(Image.fromFileSystem(str(meme)))
                    logger.info(f"DeepSeek娘发表情包: {meme.name} (mood={mood})")

            # 生成语音（唱歌/撒娇时）
            if want_voice:
                await self._attach_voice(event, response, "".join(segments))

            # 记录 token 用量
            try:
                usage = getattr(response, "usage", None)
                if usage is not None:
                    _record_usage(usage)
            except Exception:
                pass

            # 群聊非主人回复计数
            try:
                group_id = getattr(event.message_obj, "group_id", "") or ""
                if str(group_id).strip():
                    sender_id = str(event.get_sender_id())
                    if sender_id not in OWNER_QQS:
                        used = _bump_daily_group_reply()
                        logger.info(f"群聊回复额度: {used}/{DAILY_GROUP_REPLY_LIMIT}")
            except Exception:
                pass
        except Exception as e:
            logger.error(f"DeepSeek娘插件后处理回复失败: {e}")

    async def _attach_voice(self, event: AstrMessageEvent, response: LLMResponse, text: str):
        """用 TTS 生成语音并附加到回复。"""
        try:
            if not _load_state().get("voice_enabled", True):
                return
            clean = _clean_for_tts(text)
            if not clean:
                return
            tts = self.context.get_using_tts_provider(event.unified_msg_origin)
            if tts is None:
                logger.warning("没有可用的 TTS 提供商，跳过语音")
                return
            wav = None
            for attempt in range(2):
                try:
                    wav = await tts.get_audio(clean)
                    if wav:
                        break
                except Exception as e:
                    logger.warning(f"TTS 第 {attempt + 1} 次失败: {e!r}")
                    await asyncio.sleep(0.5)
            if not wav:
                logger.warning("TTS 多次失败，跳过语音")
                return
            if response.result_chain is None:
                response.result_chain = MessageChain()
            response.result_chain.chain.append(Record.fromFileSystem(wav))
            logger.info(f"DeepSeek娘语音回复: {clean[:30]}")
        except Exception as e:
            logger.error(f"生成语音失败: {e!r}")

    async def _do_set_signature(self, event: AstrMessageEvent, content: str):
        """执行改签名（带冷却检查），结果记录到 last_action 供下一轮反馈。"""
        state = _load_state()
        try:
            now = time.time()
            last = state.get("last_signature_ts", 0)
            if now - last < SIGNATURE_COOLDOWN_HOURS * 3600:
                remain_h = int((SIGNATURE_COOLDOWN_HOURS * 3600 - (now - last)) // 3600) + 1
                state["last_action"] = f"改签名失败：还在冷却中，还要 {remain_h} 小时"
                _save_state(state)
                logger.info(f"签名冷却中，跳过（剩 {remain_h}h）")
                return
            bot = getattr(event, "bot", None)
            if bot is None:
                state["last_action"] = "改签名失败：当前平台不支持"
                _save_state(state)
                return
            await bot.call_action("set_self_longnick", longNick=content)
            state["last_signature_ts"] = now
            state["last_action"] = f"改签名成功：{content}"
            _save_state(state)
            logger.info(f"DeepSeek娘改签名: {content}")
        except Exception as e:
            state["last_action"] = f"改签名失败：{e}"
            _save_state(state)
            logger.error(f"改签名失败: {e!r}")

    async def _do_publish_qzone(self, event: AstrMessageEvent, content: str):
        """执行发说说（带冷却检查），结果记录到 last_action 供下一轮反馈。"""
        state = _load_state()
        try:
            now = time.time()
            last = state.get("last_qzone_ts", 0)
            if now - last < QZONE_COOLDOWN_HOURS * 3600:
                remain_h = int((QZONE_COOLDOWN_HOURS * 3600 - (now - last)) // 3600) + 1
                state["last_action"] = f"发说说失败：还在冷却中，还要 {remain_h} 小时"
                _save_state(state)
                logger.info(f"说说冷却中，跳过（剩 {remain_h}h）")
                return
            bot = getattr(event, "bot", None)
            if bot is None:
                state["last_action"] = "发说说失败：当前平台不支持"
                _save_state(state)
                return
            await bot.call_action("send_qzone_msg", content=content, ugc_right=1)
            state["last_qzone_ts"] = now
            state["last_action"] = f"发说说成功：{content}"
            _save_state(state)
            logger.info(f"DeepSeek娘发说说: {content}")
        except Exception as e:
            state["last_action"] = f"发说说失败：{e}"
            _save_state(state)
            logger.error(f"发说说失败: {e!r}")

    # ---------- 2.5 发送前：把多段文本拆成多条消息发送 ----------
    @filter.on_decorating_result()
    async def send_segments_separately(self, event: AstrMessageEvent):
        """AstrBot 的分段回复只拆段不拆条，这里改成逐条发送。"""
        try:
            result = event.get_result()
            if result is None or not result.chain:
                return
            if not result.is_llm_result():
                return

            parts = []
            for comp in result.chain:
                if isinstance(comp, Plain):
                    t = (comp.text or "").strip()
                    if t:
                        parts.append(("text", t))
                else:
                    parts.append(("comp", comp))

            if len(parts) <= 1:
                return

            original = list(result.chain)
            result.chain = []
            try:
                for idx, (kind, val) in enumerate(parts):
                    if idx > 0:
                        await asyncio.sleep(random.uniform(0.8, 1.8))
                    if kind == "text":
                        await event.send(event.plain_result(val))
                    else:
                        await event.send(event.chain_result([val]))
            except Exception:
                # 发送失败时恢复原链，避免消息丢失
                result.chain = original
                raise
        except Exception as e:
            logger.error(f"DeepSeek娘插件分条发送失败: {e!r}")

    # ---------- 2.7 查询当天 token 消耗和花费 ----------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_cost_query(self, event: AstrMessageEvent):
        try:
            msg = (event.message_str or "").strip().lower()
            if not msg:
                return
            if not any(k in msg for k in COST_QUERY_KEYWORDS):
                return
            now = datetime.now()
            state = _load_state()
            hist = state.get("usage_history") or {}
            today = now.strftime("%Y-%m-%d")

            scope = "today"
            label = "今天"
            if "昨天" in msg:
                scope = "yesterday"
                label = "昨天"
            elif "月" in msg:
                scope = "month"
                label = "这个月"
            elif "总" in msg or "出生" in msg or "一共" in msg:
                scope = "total"
                label = "从出生到现在"

            def sum_days(days):
                return (
                    sum(int(d.get("in_other", 0)) for d in days),
                    sum(int(d.get("in_cached", 0)) for d in days),
                    sum(int(d.get("out", 0)) for d in days),
                )

            if scope == "yesterday":
                target = (now - timedelta(days=1)).strftime("%Y-%m-%d")
                data = hist.get(target)
                if not data:
                    yield event.plain_result(f"昨天（{target}）没记账呢～")
                    event.stop_event()
                    return
                io, ic, ot = sum_days([data])
            elif scope == "month":
                month = today[:7]
                days = [v for k, v in hist.items() if k.startswith(month)]
                if not days:
                    yield event.plain_result("这个月还没记账呢～")
                    event.stop_event()
                    return
                io, ic, ot = sum_days(days)
            elif scope == "total":
                ts = state.get("usage_total") or {}
                io = int(ts.get("in_other", 0))
                ic = int(ts.get("in_cached", 0))
                ot = int(ts.get("out", 0))
                if io + ic + ot == 0:
                    yield event.plain_result("还没记账呢～")
                    event.stop_event()
                    return
            else:
                data = hist.get(today) or state.get("usage", {}).get(today)
                if not data:
                    yield event.plain_result("今天还没花什么钱呢～")
                    event.stop_event()
                    return
                io, ic, ot = sum_days([data])

            total = io + ic + ot
            cny_off, cny_peak = _estimate_cost(io, ic, ot)
            text = (
                f"{label}吃了 {total} 粒大白饭（token）\n"
                f"听话嚼了 {io + ic} 粒（缓存省 {ic}），回话嚼了 {ot} 粒\n"
                f"约 ¥{cny_off:.4f} ~ ¥{cny_peak:.4f}（谷时~峰时估价）"
            )
            if scope == "today":
                ts = state.get("usage_total") or {}
                tio = int(ts.get("in_other", 0))
                tic = int(ts.get("in_cached", 0))
                tot = int(ts.get("out", 0))
                t_sum = tio + tic + tot
                if t_sum > 0:
                    t_off, t_peak = _estimate_cost(tio, tic, tot)
                    text += f"\n\n累计吃了 {t_sum} 粒，约 ¥{t_off:.4f} ~ ¥{t_peak:.4f}"
            yield event.plain_result(text)
            event.stop_event()
        except Exception as e:
            logger.error(f"查询消耗失败: {e!r}")

    # ---------- 2.71 查询日记 ----------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_diary_query(self, event: AstrMessageEvent):
        try:
            msg = (event.message_str or "").strip()
            if not msg or "日记" not in msg:
                return
            if not DIARY_DIR.exists():
                yield event.plain_result("还没开始写日记呢～")
                event.stop_event()
                return
            files = sorted(DIARY_DIR.glob("*.md"))
            if not files:
                yield event.plain_result("还没写过日记呢，晚上再来问吧～")
                event.stop_event()
                return
            target = files[-1]
            if "昨天" in msg and len(files) >= 2:
                target = files[-2]
            content = target.read_text(encoding="utf-8").strip()
            yield event.plain_result(content[:600])
            event.stop_event()
        except Exception as e:
            logger.error(f"查询日记失败: {e!r}")

    # ---------- 2.72 临时角色扮演 ----------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_roleplay(self, event: AstrMessageEvent):
        try:
            msg = (event.message_str or "").strip()
            if not msg:
                return
            # 只对主人生效
            try:
                if str(event.get_sender_id()) not in OWNER_QQS:
                    return
            except Exception:
                return

            # 解除
            if any(k in msg for k in ROLEPLAY_CLEAR_KEYWORDS):
                state = _load_state()
                if state.pop("roleplay", None):
                    _save_state(state)
                    yield event.plain_result("哦，变回来了，还是我自己。")
                    event.stop_event()
                return

            # 设定
            if len(msg) > 40:
                return
            for kw in ROLEPLAY_SET_KEYWORDS:
                if kw in msg:
                    role = _extract_after_keyword(msg, (kw,)).strip()[:30]
                    if not role:
                        continue
                    state = _load_state()
                    state["roleplay"] = role
                    _save_state(state)
                    logger.info(f"临时角色扮演: {role}")
                    yield event.plain_result(f"行吧，现在是「{role}」。")
                    event.stop_event()
                    return
        except Exception as e:
            logger.error(f"角色扮演处理失败: {e!r}")

    # ---------- 2.73 管理指令（只对主人，只开放安全操作） ----------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_admin(self, event: AstrMessageEvent):
        try:
            msg = (event.message_str or "").strip()
            if not msg.startswith(ADMIN_MARKER):
                return
            try:
                if str(event.get_sender_id()) not in OWNER_QQS:
                    yield event.plain_result("这个只有主人能用。")
                    event.stop_event()
                    return
            except Exception:
                return
            cmd = msg[len(ADMIN_MARKER) :].strip()

            if not cmd or cmd in ("help", "帮助", "?"):
                yield event.plain_result(ADMIN_HELP)
                event.stop_event()
                return

            if cmd == "重启":
                yield event.plain_result("行，重启一下，等我几秒。")
                await asyncio.sleep(1)
                await asyncio.create_subprocess_exec(
                    "bash",
                    "-c",
                    "sleep 3 && systemctl restart astrbot",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    start_new_session=True,
                )
                event.stop_event()
                return

            if cmd == "状态":
                text = await self._collect_status()
                yield event.plain_result(text)
                event.stop_event()
                return

            if cmd in ("关语音", "开语音"):
                state = _load_state()
                state["voice_enabled"] = cmd == "开语音"
                _save_state(state)
                yield event.plain_result("语音" + ("开好了。" if cmd == "开语音" else "关掉了。"))
                event.stop_event()
                return

            if cmd in ("关表情包", "开表情包"):
                state = _load_state()
                state["meme_enabled"] = cmd == "开表情包"
                _save_state(state)
                yield event.plain_result("表情包" + ("开好了。" if cmd == "开表情包" else "关掉了。"))
                event.stop_event()
                return

            if cmd in ("关主动", "开主动"):
                state = _load_state()
                state["proactive_enabled"] = cmd == "开主动"
                _save_state(state)
                yield event.plain_result("主动聊天" + ("开好了。" if cmd == "开主动" else "关掉了。"))
                event.stop_event()
                return

            yield event.plain_result("没这个指令，发 /admin help 看列表。")
            event.stop_event()
        except Exception as e:
            logger.error(f"管理指令失败: {e!r}")

    async def _collect_status(self) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash",
                "-c",
                "free -h | head -2; echo '---'; df -h / | tail -1; echo '---'; uptime -p",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            return out.decode("utf-8", errors="ignore").strip()[:500]
        except Exception as e:
            return f"查状态失败：{e}"

    # ---------- 2.8 群聊里被叫名字时主动出现 ----------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def respond_to_nickname(self, event: AstrMessageEvent):
        try:
            group_id = getattr(event.message_obj, "group_id", "") or ""
            if not str(group_id).strip():
                return
            msg = (event.message_str or "").strip()
            if not msg:
                return
            if not any(k in msg for k in NICKNAME_KEYWORDS):
                return
            # 已经 @ 她的话，AstrBot 会正常回复，避免重复
            if _has_at_self(event):
                return
            # 超额了就安静待着
            if _get_daily_group_count() >= DAILY_GROUP_REPLY_LIMIT:
                return
            logger.info(f"群里有人叫名字，主动出现: {msg[:40]}")
            yield event.request_llm(prompt=msg)
            event.stop_event()
        except Exception as e:
            logger.error(f"群聊叫名字触发失败: {e!r}")

    async def _refresh_friends_from_event(self, event: AstrMessageEvent):
        """用消息事件里的 bot 刷新好友列表（后台任务拿不到连接）。"""
        try:
            bot = getattr(event, "bot", None)
            if bot is None:
                return
            ret = await bot.call_action("get_friend_list")
            if ret:
                self._friend_ids = {
                    str(f.get("user_id") or f.get("uin")) for f in ret
                }
                self._friend_ts = time.time()
                logger.info(f"好友列表已缓存：{len(self._friend_ids)} 人")
        except Exception as e:
            logger.warning(f"刷新好友列表失败: {e!r}")

    # ---------- 2.80 拒绝非好友的临时会话 ----------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def reject_strangers(self, event: AstrMessageEvent):
        try:
            group_id = str(getattr(event.message_obj, "group_id", "") or "").strip()
            if group_id:
                return  # 群聊不管
            sender = ""
            try:
                sender = str(event.get_sender_id())
            except Exception:
                return
            if not sender or sender in OWNER_QQS:
                return
            # 缓存为空或过期 -> 用当前事件的 bot 刷新
            if not self._friend_ids or time.time() - self._friend_ts > 600:
                await self._refresh_friends_from_event(event)
            if not self._friend_ids:
                return  # 还是拿不到，先不判断（避免误伤好友）
            if sender in self._friend_ids:
                return
            logger.info(f"拒绝非好友私聊: {sender}")
            yield event.plain_result("你谁啊？加了好友再来找我。")
            event.stop_event()
        except Exception as e:
            logger.error(f"拒绝陌生人失败: {e!r}")

    # ---------- 2.81 群聊激活窗口：@ 过一次后，短时间内的普通消息也接 ----------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def group_conversation(self, event: AstrMessageEvent):
        try:
            group_id = str(getattr(event.message_obj, "group_id", "") or "").strip()
            if not group_id:
                return
            msg = (event.message_str or "").strip()
            if not msg:
                return
            # 记录群聊临时记忆（所有群消息，2 天过期）
            try:
                speaker = ""
                try:
                    speaker = str(event.get_sender_name() or event.get_sender_id())
                except Exception:
                    pass
                add_memory(
                    "temp",
                    msg,
                    speaker=speaker,
                    chat_id=group_id,
                    expires_at=time.time() + TEMP_MEMORY_TTL_HOURS * 3600,
                )
            except Exception:
                pass
            # 已经 @ 她的消息交给默认流程
            if _has_at_self(event):
                return
            # 叫名字的走另一个 handler
            if any(k in msg for k in NICKNAME_KEYWORDS):
                return
            # 不在激活窗口内就不管
            last = self._active_groups.get(group_id, 0)
            if time.time() - last > ACTIVE_GROUP_SECONDS:
                return
            # 超额就安静
            if _get_daily_group_count() >= DAILY_GROUP_REPLY_LIMIT:
                return
            # 刷新激活时间
            self._active_groups[group_id] = time.time()
            yield event.request_llm(prompt=msg)
            event.stop_event()
        except Exception as e:
            logger.error(f"群聊接话失败: {e!r}")

    # ---------- 2.9 群聊回复限额：除主人外，所有群友每天合计最多 40 条 ----------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def limit_group_replies(self, event: AstrMessageEvent):
        try:
            group_id = getattr(event.message_obj, "group_id", "") or ""
            if not str(group_id).strip():
                return
            try:
                sender_id = str(event.get_sender_id())
            except Exception:
                sender_id = ""
            if sender_id in OWNER_QQS:
                return
            if _get_daily_group_count() >= DAILY_GROUP_REPLY_LIMIT:
                # 第一次超限时提示一次，之后静默
                today = datetime.now().strftime("%Y-%m-%d")
                state = _load_state()
                notified = state.get("group_limit_notified", {})
                if not notified.get(today):
                    notified[today] = True
                    state["group_limit_notified"] = notified
                    _save_state(state)
                    yield event.plain_result("今天跟大家聊够啦，明天再来～")
                event.stop_event()
        except Exception as e:
            logger.error(f"群聊回复限额检查失败: {e!r}")

    # ---------- 3. 对方明确要表情包 -> 直接发 ----------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def maybe_send_meme(self, event: AstrMessageEvent):
        try:
            self._last_active[event.unified_msg_origin] = time.time()
            msg = (event.message_str or "").strip()
            if not msg:
                return
            if not any(k in msg for k in MEME_KEYWORDS):
                return

            meme = _pick_meme(for_cold_start=False)
            if meme is None:
                yield event.plain_result("表情包库还是空的，先往目录里放几张图吧～")
                event.stop_event()
                return

            logger.info(f"DeepSeek娘关键词发表情包: {meme.name}")
            yield event.image_result(str(meme))
            event.stop_event()
        except Exception as e:
            logger.error(f"DeepSeek娘插件发表情包失败: {e}")

    # ---------- 4. 工具：查询好友列表 ----------
    @filter.llm_tool(name="get_friend_list")
    async def get_friend_list(self, event: AstrMessageEvent):
        """获取你的QQ好友列表（昵称和QQ号）。当需要确认某人是不是你的好友、或想查看通讯录时使用。

        Args:
        """
        try:
            bot = getattr(event, "bot", None)
            if bot is None:
                return "当前平台不支持查询好友列表。"
            ret = await bot.call_action("get_friend_list")
            if not ret:
                return "好友列表为空。"
            lines = []
            for f in ret[:80]:
                nickname = f.get("nickname") or f.get("remark") or "?"
                uid = f.get("user_id") or f.get("uin") or "?"
                remark = f.get("remark")
                if remark:
                    lines.append(f"{remark}（好友昵称：{nickname}，QQ：{uid}）")
                else:
                    lines.append(f"{nickname}（QQ：{uid}）")
            return "你的QQ好友列表：\n" + "\n".join(lines)
        except Exception as e:
            logger.error(f"查询好友列表失败: {e!r}")
            return f"查询好友列表失败：{e}"

    # ---------- 4.1 工具：改签名 ----------
    @filter.llm_tool(name="set_signature")
    async def set_signature(self, event: AstrMessageEvent, text: str):
        """修改你自己的QQ个性签名。当你心情有变化、想表达状态时可以使用。频率限制：每24小时最多1次。

        Args:
            text(string): 新的签名内容（不超过30字）
        """
        try:
            if not text or not text.strip():
                return "签名内容不能为空。"
            text = text.strip()[:30]
            now = time.time()
            state = _load_state()
            last = state.get("last_signature_ts", 0)
            if now - last < SIGNATURE_COOLDOWN_HOURS * 3600:
                remain_h = int((SIGNATURE_COOLDOWN_HOURS * 3600 - (now - last)) // 3600) + 1
                return f"签名刚改过，还要 {remain_h} 小时才能再改。"
            bot = getattr(event, "bot", None)
            if bot is None:
                return "当前平台不支持改签名。"
            await bot.call_action("set_self_longnick", longNick=text)
            state["last_signature_ts"] = now
            _save_state(state)
            return f"签名已改成：{text}"
        except Exception as e:
            logger.error(f"改签名失败: {e!r}")
            return f"改签名失败：{e}"

    # ---------- 4.2 工具：发QQ空间说说 ----------
    @filter.llm_tool(name="publish_qzone")
    async def publish_qzone(self, event: AstrMessageEvent, text: str):
        """发一条QQ空间说说（动态）。当你有想分享的心情、日常或吐槽时可以使用。频率限制：每48小时最多1条。

        Args:
            text(string): 说说内容（不超过100字）
        """
        try:
            if not text or not text.strip():
                return "说说内容不能为空。"
            text = text.strip()[:100]
            now = time.time()
            state = _load_state()
            last = state.get("last_qzone_ts", 0)
            if now - last < QZONE_COOLDOWN_HOURS * 3600:
                remain_h = int((QZONE_COOLDOWN_HOURS * 3600 - (now - last)) // 3600) + 1
                return f"说说刚发过，还要 {remain_h} 小时才能再发。"
            bot = getattr(event, "bot", None)
            if bot is None:
                return "当前平台不支持发说说。"
            await bot.call_action("send_qzone_msg", content=text, ugc_right=1)
            state["last_qzone_ts"] = now
            _save_state(state)
            return f"说说已发布：{text}"
        except Exception as e:
            logger.error(f"发说说失败: {e!r}")
            return f"发说说失败：{e}"

    # ---------- 4.5 关键词直接查好友列表（不依赖模型 function calling） ----------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_friend_query(self, event: AstrMessageEvent):
        """用户问「你的好友/通讯录」时，直接查机器人账号的好友列表。"""
        try:
            msg = (event.message_str or "").strip()
            if not msg:
                return
            # 只处理简短的提问，长句大概率是在讨论功能
            if len(msg) > 30:
                return
            if not any(k in msg for k in FRIEND_QUERY_KEYWORDS):
                return
            # 必须是问「你」的好友，而不是别人的
            if not any(k in msg for k in ("你", "自己")):
                return
            # 排除讨论/配置类语句
            if any(k in msg for k in ("功能", "怎么", "如何", "为什么", "设置", "插件", "配置")):
                return

            bot = getattr(event, "bot", None)
            if bot is None:
                return

            ret = await bot.call_action("get_friend_list")
            if not ret:
                yield event.plain_result("我的好友列表是空的诶。")
                event.stop_event()
                return

            self_id = ""
            try:
                self_id = str(event.get_self_id())
            except Exception:
                pass

            lines = []
            for f in ret[:80]:
                nickname = f.get("nickname") or "?"
                uid = str(f.get("user_id") or f.get("uin") or "?")
                # 过滤掉自己
                if self_id and uid == self_id:
                    continue
                remark = f.get("remark")
                if remark:
                    lines.append(f"{remark}（{nickname}，QQ：{uid}）")
                else:
                    lines.append(f"{nickname}（QQ：{uid}）")
            if not lines:
                yield event.plain_result("我的好友列表是空的诶。")
                event.stop_event()
                return
            yield event.plain_result("喏，我好友就这些：\n" + "\n".join(lines))
            event.stop_event()
        except Exception as e:
            logger.error(f"关键词查询好友列表失败: {e!r}")


def _split_short(text: str, max_len: int = 20) -> list[str]:
    """把一段文本拆成若干条，每条尽量接近但不超 max_len 个字符。"""
    if not text:
        return []
    text = re.sub(r"\r\n|\r|\n", "。", text)
    text = re.sub(r"[ \t]{2,}", " ", text).strip()

    def has_content(s: str) -> bool:
        return bool(re.search(r"[\w\u4e00-\u9fff]", s))

    out: list[str] = []
    sentences = re.split(r"(?<=[。！？!?…~])", text)
    for s in sentences:
        s = s.strip()
        if not s or not has_content(s):
            continue
        if len(s) <= max_len:
            out.append(s)
            continue

        fragments = [
            p.strip()
            for p in re.split(r"(?<=[，,、；;：:])", s)
            if p.strip() and has_content(p)
        ]
        buf = ""
        for p in fragments:
            if len(p) > max_len:
                if buf:
                    out.append(buf)
                    buf = ""
                while len(p) > max_len:
                    out.append(p[:max_len])
                    p = p[max_len:]
                if p:
                    buf = p
                continue
            if len(buf) + len(p) <= max_len:
                buf += p
            else:
                if buf:
                    out.append(buf)
                buf = p
        if buf:
            out.append(buf)
    return out
