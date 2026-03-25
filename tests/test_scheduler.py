"""
tests/test_scheduler.py
────────────────────────
测试提醒调度器的纯逻辑部分:
  - _next_daily_fire: 计算下次每日触发时间
  - _find_duplicate_oneshot: 一次性提醒去重
  - _find_duplicate_daily: 每日提醒去重
  - add_reminder / add_daily_reminder: 防重验证
  - cancel_reminder / get_reminders: 取消与查询
"""

import sys
import os
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta

# 将项目根目录加入 sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ── Mock 掉 NoneBot 依赖，避免导入时需要运行时 ──
# 在导入 scheduler 之前先 patch 掉这些模块
_nonebot_mock = MagicMock()
_nonebot_mock.get_driver.return_value = MagicMock()
_nonebot_mock.get_bot.return_value = MagicMock()
_nonebot_mock.log.logger = MagicMock()

sys.modules.setdefault("nonebot", _nonebot_mock)
sys.modules.setdefault("nonebot.log", MagicMock(logger=MagicMock()))
sys.modules.setdefault("nonebot.adapters", MagicMock())
sys.modules.setdefault("nonebot.adapters.onebot", MagicMock())
sys.modules.setdefault("nonebot.adapters.onebot.v11", MagicMock())

# Mock chunker 模块 (它依赖 nonebot.adapters.onebot.v11)
sys.modules.setdefault("plugins.chunker", MagicMock())

from plugins.reminder.scheduler import (
    ReminderJob,
    _next_daily_fire,
    _find_duplicate_oneshot,
    _find_duplicate_daily,
    _jobs,
    _tasks,
    _DEDUP_WINDOW,
    _save,
)

import pytest


# ──────────────────── 工具函数 ────────────────────

def _make_job(
    *,
    job_id: str = "test0001",
    chat_type: str = "group",
    target_id: str = "123456",
    user_id: str = "111",
    creator_name: str = "测试用户",
    message: str = "开会",
    fire_at: str | None = None,
    created_at: str | None = None,
    recurring: str = "",
    daily_time: str = "",
) -> ReminderJob:
    now = datetime.now()
    return ReminderJob(
        id=job_id,
        chat_type=chat_type,
        target_id=target_id,
        user_id=user_id,
        creator_name=creator_name,
        message=message,
        fire_at=fire_at or (now + timedelta(minutes=30)).isoformat(),
        created_at=created_at or now.isoformat(),
        recurring=recurring,
        daily_time=daily_time,
    )


def _clear_jobs():
    """清空全局 _jobs 和 _tasks"""
    _jobs.clear()
    _tasks.clear()


# ──────────────────── _next_daily_fire ────────────────────

class TestNextDailyFire:
    """测试每日触发时间计算"""

    def test_future_time_today(self):
        """今天还没过的时刻应返回今天"""
        now = datetime.now()
        future_hour = 23  # 大概率还没到 23:59
        future_min = 59
        result = _next_daily_fire("23:59")
        assert result.hour == 23
        assert result.minute == 59
        assert result.second == 0
        # 如果当前已经是 23:59+ 则应该返回明天
        if now.hour > 23 or (now.hour == 23 and now.minute >= 59):
            assert result.date() == (now + timedelta(days=1)).date()
        else:
            assert result.date() == now.date()

    def test_past_time_returns_tomorrow(self):
        """已过的时刻应返回明天"""
        result = _next_daily_fire("00:00")
        now = datetime.now()
        # 00:00 几乎一定已经过了（除非恰好在午夜运行）
        if now.hour == 0 and now.minute == 0:
            pytest.skip("恰好午夜运行，跳过")
        assert result.date() == (now + timedelta(days=1)).date()
        assert result.hour == 0
        assert result.minute == 0

    def test_exact_format(self):
        """返回值的秒和微秒应为 0"""
        result = _next_daily_fire("12:30")
        assert result.second == 0
        assert result.microsecond == 0
        assert result.minute == 30

    def test_single_digit_hour(self):
        """单数字小时解析"""
        result = _next_daily_fire("8:05")
        assert result.hour == 8
        assert result.minute == 5


# ──────────────────── 一次性提醒去重 ────────────────────

class TestDeduplicateOneshot:
    """测试一次性提醒去重逻辑"""

    def setup_method(self):
        _clear_jobs()

    def teardown_method(self):
        _clear_jobs()

    def test_no_duplicate_when_empty(self):
        """空列表时不报重复"""
        result = _find_duplicate_oneshot("group", "123", "开会")
        assert result is None

    def test_finds_exact_duplicate(self):
        """相同会话 + 相同事项 + 近期创建 → 应检出重复"""
        job = _make_job(message="喝水", chat_type="group", target_id="123")
        _jobs[job.id] = job
        result = _find_duplicate_oneshot("group", "123", "喝水")
        assert result is not None
        assert result.id == job.id

    def test_different_message_no_dup(self):
        """不同事项 → 不重复"""
        job = _make_job(message="喝水")
        _jobs[job.id] = job
        result = _find_duplicate_oneshot("group", "123456", "吃饭")
        assert result is None

    def test_different_chat_no_dup(self):
        """不同会话 → 不重复"""
        job = _make_job(message="喝水", target_id="111")
        _jobs[job.id] = job
        result = _find_duplicate_oneshot("group", "222", "喝水")
        assert result is None

    def test_different_chat_type_no_dup(self):
        """不同会话类型 → 不重复"""
        job = _make_job(message="喝水", chat_type="group", target_id="123")
        _jobs[job.id] = job
        result = _find_duplicate_oneshot("private", "123", "喝水")
        assert result is None

    def test_old_job_outside_window_no_dup(self):
        """超出去重窗口的旧提醒 → 不重复"""
        old_time = (datetime.now() - timedelta(seconds=_DEDUP_WINDOW + 10)).isoformat()
        job = _make_job(message="喝水", created_at=old_time)
        _jobs[job.id] = job
        result = _find_duplicate_oneshot("group", "123456", "喝水")
        assert result is None

    def test_daily_job_not_matched(self):
        """每日提醒不应被一次性去重匹配"""
        job = _make_job(message="签到", recurring="daily", daily_time="09:00")
        _jobs[job.id] = job
        result = _find_duplicate_oneshot("group", "123456", "签到")
        assert result is None


# ──────────────────── 每日提醒去重 ────────────────────

class TestDeduplicateDaily:
    """测试每日提醒去重逻辑"""

    def setup_method(self):
        _clear_jobs()

    def teardown_method(self):
        _clear_jobs()

    def test_no_duplicate_when_empty(self):
        result = _find_duplicate_daily("group", "123", "签到", "09:00")
        assert result is None

    def test_finds_exact_duplicate(self):
        """完全匹配：同会话 + 同事项 + 同时刻"""
        job = _make_job(
            message="签到", recurring="daily", daily_time="09:00",
            chat_type="group", target_id="123",
        )
        _jobs[job.id] = job
        result = _find_duplicate_daily("group", "123", "签到", "09:00")
        assert result is not None
        assert result.id == job.id

    def test_different_time_no_dup(self):
        """同事项不同时刻 → 不重复（用户可能想要多个时间点）"""
        job = _make_job(
            message="签到", recurring="daily", daily_time="09:00",
            target_id="123",
        )
        _jobs[job.id] = job
        result = _find_duplicate_daily("group", "123", "签到", "18:00")
        assert result is None

    def test_different_message_no_dup(self):
        """同时刻不同事项 → 不重复"""
        job = _make_job(
            message="签到", recurring="daily", daily_time="09:00",
            target_id="123",
        )
        _jobs[job.id] = job
        result = _find_duplicate_daily("group", "123", "喝水", "09:00")
        assert result is None

    def test_oneshot_job_not_matched(self):
        """一次性提醒不应被每日去重匹配"""
        job = _make_job(message="签到", recurring="", target_id="123")
        _jobs[job.id] = job
        result = _find_duplicate_daily("group", "123", "签到", "09:00")
        assert result is None


# ──────────────────── cancel_reminder ────────────────────

class TestCancelReminder:
    """测试取消提醒"""

    def setup_method(self):
        _clear_jobs()

    def teardown_method(self):
        _clear_jobs()

    def test_cancel_existing(self):
        from plugins.reminder.scheduler import cancel_reminder
        job = _make_job(job_id="abc123")
        _jobs[job.id] = job
        # 不创建真实 task，放一个 mock
        mock_task = MagicMock()
        mock_task.done.return_value = False
        _tasks[job.id] = mock_task

        with patch("plugins.reminder.scheduler._save"):
            result = cancel_reminder("abc123")

        assert result is True
        assert "abc123" not in _jobs
        mock_task.cancel.assert_called_once()

    def test_cancel_nonexistent(self):
        from plugins.reminder.scheduler import cancel_reminder
        result = cancel_reminder("nonexistent")
        assert result is False


# ──────────────────── get_reminders ────────────────────

class TestGetReminders:
    """测试查询提醒"""

    def setup_method(self):
        _clear_jobs()

    def teardown_method(self):
        _clear_jobs()

    def test_empty(self):
        from plugins.reminder.scheduler import get_reminders
        result = get_reminders("group", "123")
        assert result == []

    def test_filters_by_chat(self):
        from plugins.reminder.scheduler import get_reminders
        job1 = _make_job(job_id="a", chat_type="group", target_id="111", message="A")
        job2 = _make_job(job_id="b", chat_type="group", target_id="222", message="B")
        job3 = _make_job(job_id="c", chat_type="private", target_id="111", message="C")
        _jobs["a"] = job1
        _jobs["b"] = job2
        _jobs["c"] = job3

        result = get_reminders("group", "111")
        assert len(result) == 1
        assert result[0].id == "a"

    def test_returns_all_for_same_chat(self):
        from plugins.reminder.scheduler import get_reminders
        job1 = _make_job(job_id="x", target_id="111", message="A")
        job2 = _make_job(job_id="y", target_id="111", message="B")
        _jobs["x"] = job1
        _jobs["y"] = job2

        result = get_reminders("group", "111")
        assert len(result) == 2


# ──────────────────── ReminderJob 数据结构 ────────────────────

class TestReminderJob:
    """测试 ReminderJob 数据结构"""

    def test_default_fields(self):
        job = _make_job()
        assert job.recurring == ""
        assert job.daily_time == ""

    def test_daily_fields(self):
        job = _make_job(recurring="daily", daily_time="09:30")
        assert job.recurring == "daily"
        assert job.daily_time == "09:30"

    def test_asdict(self):
        from dataclasses import asdict
        job = _make_job(job_id="test", message="测试")
        d = asdict(job)
        assert d["id"] == "test"
        assert d["message"] == "测试"
        assert isinstance(d, dict)
