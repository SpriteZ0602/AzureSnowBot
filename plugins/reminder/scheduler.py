"""
提醒调度器
──────────
核心逻辑：增删查 + asyncio 定时触发 + JSON 持久化。

架构参考 OpenClaw 的 cron 调度器：
  - 持久化到文件，重启不丢失
  - 到期后通过 Bot 实例主动推送消息
  - 支持群聊 / 私聊两种发送目标
  - 支持一次性提醒和每日定时提醒
  - 提醒触发时调用 LLM 生成上下文相关的提醒消息
"""

import json
import asyncio
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, field, asdict

import httpx
from nonebot import get_bot
from nonebot.log import logger

from ..chunker import send_chunked_raw

# Admin QQ 号，用于判断提醒触发后是否重置主动发言计时器
_admin_number: str = ""

# ──────────────────── 持久化路径 ────────────────────
REMINDERS_FILE = Path("data/reminders.json")
REMINDERS_FILE.parent.mkdir(parents=True, exist_ok=True)

# ──────────────────── LLM 配置（延迟读取） ────────────────────
_llm_config: dict | None = None


def _get_llm_config() -> dict:
    """延迟获取 LLM 配置，统一从 plugins.llm 读取"""
    global _llm_config
    if _llm_config is None:
        from ..llm import API_KEY, BASE_URL, MODEL
        _llm_config = {
            "api_key": API_KEY,
            "base_url": BASE_URL,
            "model": MODEL,
        }
    return _llm_config


# ──────────────────── 数据结构 ────────────────────

@dataclass
class ReminderJob:
    """一条提醒任务"""
    id: str                # 短 UUID
    chat_type: str         # "group" / "private"
    target_id: str         # group_id 或 user_id
    user_id: str           # 创建者 QQ 号
    creator_name: str      # 创建者昵称
    message: str           # 提醒内容
    fire_at: str           # ISO 格式触发时间
    created_at: str        # ISO 格式创建时间
    recurring: str = ""    # 空 = 一次性，"daily" = 每日定时
    daily_time: str = ""   # HH:MM 格式，仅 recurring="daily" 时有效


# ──────────────────── 运行时状态 ────────────────────
_jobs: dict[str, ReminderJob] = {}
_tasks: dict[str, asyncio.Task] = {}
_lock = asyncio.Lock()  # 保护 _jobs 和 _save() 的并发访问


# ──────────────────── 持久化 ────────────────────

def _save() -> None:
    """保存所有提醒到磁盘"""
    data = {jid: asdict(job) for jid, job in _jobs.items()}
    REMINDERS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _load() -> dict[str, ReminderJob]:
    """从磁盘读取提醒"""
    if not REMINDERS_FILE.exists():
        return {}
    try:
        data = json.loads(REMINDERS_FILE.read_text(encoding="utf-8"))
        return {jid: ReminderJob(**info) for jid, info in data.items()}
    except Exception as e:
        logger.error(f"加载提醒数据失败: {e}")
        return {}


# ──────────────────── LLM 生成提醒消息 ────────────────────

async def _generate_reminder_message(job: ReminderJob) -> str:
    """
    复用当前对话的 system prompt + 聊天历史，
    追加一条提醒指令让 LLM 以当前人格身份生成提醒消息。
    失败时回退到固定格式。
    """
    fallback = f"提醒 {job.creator_name}：{job.message}"

    try:
        cfg = _get_llm_config()
        if not cfg["api_key"]:
            return fallback

        # 加载当前对话的 system prompt 和聊天历史
        system_prompt, history = _load_conversation(job)

        # 组装 messages：原 system prompt + 最近聊天记录 + 提醒指令
        messages = [{"role": "system", "content": system_prompt}]

        # 取最近的聊天记录（避免超长）
        recent = history[-20:]
        for m in recent:
            role = m.get("role", "")
            content = m.get("content", "")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})

        # 追加提醒指令（作为 user 消息，触发 LLM 回复）
        reminder_instruction = (
            f"现在是 {datetime.now().strftime('%Y-%m-%d %H:%M')}，"
            f"你之前答应过要提醒 {job.creator_name} 去做这件事：“{job.message}”。"
            f"现在时间到了，请你提醒他，去做“{job.message}”这件事。"
        )
        messages.append({"role": "user", "content": reminder_instruction})

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{cfg['base_url']}/chat/completions",
                headers={
                    "Authorization": f"Bearer {cfg['api_key']}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": cfg["model"],
                    "messages": messages,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            from ..token_stats import record_usage
            record_usage("reminder", data.get("usage"))
            reply = data["choices"][0]["message"]["content"].strip()
            if reply:
                return reply

    except Exception as e:
        logger.warning(f"LLM 生成提醒消息失败，使用固定格式: {e}")

    return fallback


def _load_conversation(job: ReminderJob) -> tuple[str, list[dict]]:
    """
    加载当前对话的 system prompt 和聊天历史。
    返回 (system_prompt, history_messages)。
    """
    try:
        if job.chat_type == "group":
            from ..persona.manager import (
                get_active_persona, load_persona_prompt, load_history,
            )
            persona = get_active_persona(job.target_id)
            prompt = load_persona_prompt(persona, job.target_id) or "你是一个有用的助手。"
            history = load_history(job.target_id, persona)
            return prompt, history
        else:
            from ..chat.handler import (
                load_history as chat_load_history,
                load_admin_prompt,
            )
            prompt = load_admin_prompt() or "你是一个有用的助手。"
            history = chat_load_history(job.target_id)
            return prompt, history
    except Exception as e:
        logger.warning(f"加载对话上下文失败: {e}")
        return "你是一个有用的助手。", []


# ──────────────────── 写入聊天历史 ────────────────────

def _append_to_history(job: ReminderJob, text: str) -> None:
    """将提醒消息写入对应会话的聊天历史，以便后续对话 LLM 可见。"""
    try:
        msg = {"role": "assistant", "content": text}
        if job.chat_type == "group":
            from ..persona.manager import get_active_persona, append_message
            persona = get_active_persona(job.target_id)
            append_message(job.target_id, msg, persona)
        else:
            from ..chat.handler import append_message as chat_append
            chat_append(job.target_id, msg)
    except Exception as e:
        logger.warning(f"提醒写入聊天历史失败 [{job.id}]: {e}")


def _reset_proactive_if_admin(job: ReminderJob) -> None:
    """如果提醒是发给 admin 私聊的，重置主动发言计时器防止时间重叠。"""
    global _admin_number
    if not _admin_number:
        from nonebot import get_driver
        _admin_number = str(getattr(get_driver().config, "admin_number", ""))
    if job.chat_type == "private" and _admin_number and job.target_id == _admin_number:
        from ..chat.proactive import reset_idle_timer
        reset_idle_timer()


# ──────────────────── 触发逻辑 ────────────────────

async def _fire(job: ReminderJob) -> None:
    """等待到期后发送提醒消息（一次性提醒）"""
    fire_at = datetime.fromisoformat(job.fire_at)
    delay = (fire_at - datetime.now()).total_seconds()

    if delay > 0:
        await asyncio.sleep(delay)

    # 从运行时状态中移除
    async with _lock:
        _jobs.pop(job.id, None)
        _tasks.pop(job.id, None)
        _save()

    try:
        bot = get_bot()

        # 调用 LLM 生成提醒消息
        text = await _generate_reminder_message(job)

        # 补发标记
        actual_delay = (datetime.now() - fire_at).total_seconds()
        if actual_delay > 60:
            text = f"（延迟提醒）{text}"

        # 分条发送
        await send_chunked_raw(bot, job.chat_type, int(job.target_id), text)

        # 写入聊天历史
        _append_to_history(job, text)

        # Admin 私聊提醒触发后重置主动发言计时器，防止时间重叠
        _reset_proactive_if_admin(job)

        logger.info(f"提醒已触发: [{job.id}] {job.message}")
    except Exception as e:
        logger.error(f"提醒发送失败 [{job.id}]: {e}")


async def _fire_daily(job: ReminderJob) -> None:
    """每日定时提醒循环：每天在指定时刻触发，永不自动删除。"""
    while job.id in _jobs:
        # 计算下一次触发时间
        fire_at = _next_daily_fire(job.daily_time)
        delay = (fire_at - datetime.now()).total_seconds()

        if delay > 0:
            await asyncio.sleep(delay)

        # 检查是否已被取消
        if job.id not in _jobs:
            break

        # 更新 fire_at 到当前触发时间（供 list 显示）
        async with _lock:
            job.fire_at = fire_at.isoformat()
            _save()

        try:
            bot = get_bot()
            text = await _generate_reminder_message(job)

            # 分条发送
            await send_chunked_raw(bot, job.chat_type, int(job.target_id), text)

            # 写入聊天历史
            _append_to_history(job, text)

            # Admin 私聊提醒触发后重置主动发言计时器，防止时间重叠
            _reset_proactive_if_admin(job)

            logger.info(f"每日提醒已触发: [{job.id}] {job.daily_time} | {job.message}")
        except Exception as e:
            logger.error(f"每日提醒发送失败 [{job.id}]: {e}")

        # 短暂等待避免同一分钟内重复触发
        await asyncio.sleep(61)


def _next_daily_fire(time_str: str) -> datetime:
    """根据 HH:MM 字符串计算今天或明天的下一次触发时间。"""
    hour, minute = map(int, time_str.split(":"))
    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


# ──────────────────── 公开 API ────────────────────

# ──────────────────── 去重窗口（秒） ────────────────────
_DEDUP_WINDOW = 120  # 同一会话 + 同一事项，120 秒内视为重复


def _find_duplicate_oneshot(
    chat_type: str, target_id: str, message: str,
) -> ReminderJob | None:
    """检查是否已存在相同的一次性提醒（同会话 + 同事项 + 近期创建）。"""
    now = datetime.now()
    for job in _jobs.values():
        if (
            job.chat_type == chat_type
            and job.target_id == target_id
            and job.message == message
            and job.recurring == ""
        ):
            created = datetime.fromisoformat(job.created_at)
            if (now - created).total_seconds() < _DEDUP_WINDOW:
                return job
    return None


def _find_duplicate_daily(
    chat_type: str, target_id: str, message: str, daily_time: str,
) -> ReminderJob | None:
    """检查是否已存在完全相同的每日提醒（同会话 + 同事项 + 同时刻）。"""
    for job in _jobs.values():
        if (
            job.chat_type == chat_type
            and job.target_id == target_id
            and job.recurring == "daily"
            and job.daily_time == daily_time
        ):
            return job
    return None


def add_reminder(
    chat_type: str,
    target_id: str,
    user_id: str,
    creator_name: str,
    message: str,
    delay_minutes: float,
) -> tuple[str, str]:
    """
    添加一条一次性提醒。

    返回 (job_id, 触发时间字符串)。
    若检测到重复，直接返回已存在的提醒 ID 和「已存在」标记。
    """
    # ── 防重：同会话 + 同事项 + 近期创建 ──
    dup = _find_duplicate_oneshot(chat_type, target_id, message)
    if dup:
        fire_str = datetime.fromisoformat(dup.fire_at).strftime("%H:%M:%S")
        logger.warning(f"提醒去重: [{dup.id}] 已存在相同一次性提醒「{message}」")
        return dup.id, fire_str

    job_id = uuid.uuid4().hex[:8]
    now = datetime.now()
    fire_at = now + timedelta(minutes=delay_minutes)

    job = ReminderJob(
        id=job_id,
        chat_type=chat_type,
        target_id=target_id,
        user_id=user_id,
        creator_name=creator_name,
        message=message,
        fire_at=fire_at.isoformat(),
        created_at=now.isoformat(),
    )
    _jobs[job_id] = job
    _save()

    task = asyncio.get_running_loop().create_task(_fire(job))
    _tasks[job_id] = task

    fire_str = fire_at.strftime("%H:%M:%S")
    logger.info(f"提醒已设置: [{job_id}] {delay_minutes}分钟后 → {fire_str} | {message}")
    return job_id, fire_str


def add_daily_reminder(
    chat_type: str,
    target_id: str,
    user_id: str,
    creator_name: str,
    message: str,
    daily_time: str,
) -> tuple[str, str]:
    """
    添加一条每日定时提醒。

    参数:
        daily_time: "HH:MM" 格式

    返回 (job_id, 下次触发时间字符串)。
    """
    # ── 防重：同会话 + 同事项 + 同时刻 ──
    dup = _find_duplicate_daily(chat_type, target_id, message, daily_time)
    if dup:
        fire_str = datetime.fromisoformat(dup.fire_at).strftime("%m-%d %H:%M")
        logger.warning(f"提醒去重: [{dup.id}] 已存在相同每日提醒「{message}」@ {daily_time}")
        return dup.id, fire_str

    job_id = uuid.uuid4().hex[:8]
    now = datetime.now()
    next_fire = _next_daily_fire(daily_time)

    job = ReminderJob(
        id=job_id,
        chat_type=chat_type,
        target_id=target_id,
        user_id=user_id,
        creator_name=creator_name,
        message=message,
        fire_at=next_fire.isoformat(),
        created_at=now.isoformat(),
        recurring="daily",
        daily_time=daily_time,
    )
    _jobs[job_id] = job
    _save()

    task = asyncio.get_running_loop().create_task(_fire_daily(job))
    _tasks[job_id] = task

    fire_str = next_fire.strftime("%m-%d %H:%M")
    logger.info(f"每日提醒已设置: [{job_id}] 每天 {daily_time} | {message}")
    return job_id, fire_str


def cancel_reminder(job_id: str) -> bool:
    """取消一条提醒（一次性或每日）。返回是否成功。"""
    job = _jobs.pop(job_id, None)
    if not job:
        return False
    task = _tasks.pop(job_id, None)
    if task and not task.done():
        task.cancel()
    _save()
    logger.info(f"提醒已取消: [{job_id}]")
    return True


def get_reminders(chat_type: str, target_id: str) -> list[ReminderJob]:
    """获取指定会话的所有待触发提醒。"""
    return [
        j for j in _jobs.values()
        if j.chat_type == chat_type and j.target_id == target_id
    ]


def get_all_reminders() -> list[ReminderJob]:
    """获取所有待触发提醒。"""
    return list(_jobs.values())


def clear_reminders(chat_type: str, target_id: str) -> int:
    """取消指定会话的所有提醒，返回取消数量。"""
    to_remove = [
        jid for jid, j in _jobs.items()
        if j.chat_type == chat_type and j.target_id == target_id
    ]
    for jid in to_remove:
        _jobs.pop(jid, None)
        task = _tasks.pop(jid, None)
        if task and not task.done():
            task.cancel()
    if to_remove:
        _save()
        logger.info(f"已清除 {len(to_remove)} 条提醒 ({chat_type}:{target_id})")
    return len(to_remove)


async def reload_reminders() -> None:
    """启动时重新加载持久化的提醒并调度。"""
    global _jobs
    loaded = _load()
    now = datetime.now()
    scheduled = 0

    for job_id, job in loaded.items():
        _jobs[job_id] = job
        if job.recurring == "daily":
            # 每日提醒：重新进入循环
            task = asyncio.get_running_loop().create_task(_fire_daily(job))
        else:
            # 一次性提醒：无论是否过期都调度
            task = asyncio.get_running_loop().create_task(_fire(job))
        _tasks[job_id] = task

        fire_at = datetime.fromisoformat(job.fire_at)
        if fire_at > now or job.recurring == "daily":
            scheduled += 1

    overdue = len(loaded) - scheduled
    if loaded:
        logger.info(f"已加载 {len(loaded)} 条提醒（{scheduled} 条待触发，{overdue} 条即将补发）")
