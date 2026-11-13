"""在独立进程中使用真实 NoneBot 加载插件，覆盖模块级 mock 看不到的循环导入。"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBE = r'''
import sys
from collections import Counter
from pathlib import Path
import nonebot
from nonebot.adapters.onebot.v11 import Adapter
from nonebot.plugin import PluginManager

root, first = Path(sys.argv[1]), sys.argv[2]
nonebot.init(log_level="ERROR", _env_file=None, admin_number="123456", group_whitelist=[])
driver = nonebot.get_driver()
driver.register_adapter(Adapter)

def load_in_order(self):
    names = sorted(self.available_plugins)
    assert first in names, (first, names)
    ordered = [first] + [name for name in names if name != first]
    loaded = set()
    for name in ordered:
        plugin = self.load_plugin(name)
        assert plugin is not None, f"startup failed when {first} loaded first: {name}"
        loaded.add(plugin)
    return loaded

PluginManager.load_all_plugins = load_in_order
loaded = nonebot.load_plugins(str(root / "plugins"))
assert loaded
from plugins import proactive
from plugins.group import handler
from plugins.chat import handler as private_handler
from plugins.local_tools.manager import get_openai_tools
from plugins.group.utils import is_legacy_score_binding
from plugins.otoge_plugin import is_otoge_command, otoge_cmd, _score_arguments
from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent, Message
assert handler.reset_group_proactive_timer is proactive.reset_idle_timer
assert private_handler.reset_idle_timer is proactive.reset_idle_timer
tool_names = {tool["function"]["name"] for tool in get_openai_tools(chat_type="group")}
assert {"local__otoge_best_scores", "local__otoge_song_search"} <= tool_names
assert not ({"local__maimai_player_query", "local__maimai_song_search"} & tool_names)
assert otoge_cmd.block and otoge_cmd.priority < handler.group_chat.priority
assert _score_arguments("落雪 文字", "123456", ["987654"]) == ("lxns", "987654", "", True)
assert _score_arguments("水鱼 用户名 Player Name", "123456", []) == ("divingfish", "", "Player Name", False)
assert is_legacy_score_binding(" /Bind example ") and not is_legacy_score_binding("/binding")
event = PrivateMessageEvent.model_construct(user_id=123456, self_id=999999, message=Message("/mai\tb50"))
assert is_otoge_command(event)
event.user_id = 987654
assert not is_otoge_command(event)
group_event = GroupMessageEvent.model_construct(user_id=123456, self_id=999999, group_id=1, message=Message("/chu b50"))
assert not is_otoge_command(group_event)

# 导入失败后重新导入曾导致同名启动回调重复注册，不能只检查最终模块能导入。
hooks = Counter((fn.__module__, fn.__qualname__) for fn in driver._lifespan._startup_funcs)
for key in (("plugins.group_plugin", "_startup"), ("plugins.chat_plugin", "_start_heartbeat"),
            ("plugins.dashboard_plugin", "_mount_dashboard")):
    assert hooks[key] == 1, (key, hooks[key])
print("startup verified:", first)
'''


@pytest.mark.parametrize("first", ["proactive", "skill", "group", "chat", "persona", "sticker_commands", "group_plugin", "chat_plugin", "local_tools_plugin", "dashboard_plugin", "otoge_plugin"])
def test_real_plugin_loading_is_order_independent(first, tmp_path):
    # NoneBot 要求插件位于 cwd 下；复制源码以隔离模块 __file__ 派生的数据路径。
    shutil.copytree(ROOT / "plugins", tmp_path / "plugins", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(ROOT / "botcore", tmp_path / "botcore", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(tmp_path),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONHASHSEED": "23",
        "DASHBOARD_SECRET_KEY": "test-plugin-startup-secret-at-least-32-bytes",
        "DASHBOARD_PASSWORD_HASH": "",
        "LLM_PROVIDER": "deepseek",
    })
    # 仅加载，不执行 startup / shutdown，不连接 QQ 或 LLM；数据目录只落在 tmp_path。
    result = subprocess.run([sys.executable, "-c", PROBE, str(tmp_path), first], cwd=tmp_path,
                            env=env, capture_output=True, text=True, encoding="utf-8", timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "startup verified: " + first in result.stdout


def test_service_imports_do_not_register_plugins_or_require_driver(tmp_path):
    shutil.copytree(ROOT / "plugins", tmp_path / "plugins", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(ROOT / "botcore", tmp_path / "botcore", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    code = '''
import sys
import plugins.persona.manager
import plugins.skill.manager
import plugins.local_tools.manager
import plugins.reminder.scheduler
import plugins.otoge.prober
assert "plugins.group.handler" not in sys.modules
assert "plugins.skill.commands" not in sys.modules
assert "plugins.persona.commands" not in sys.modules
assert "plugins.chat.handler" not in sys.modules
assert "plugins.otoge_plugin" not in sys.modules
'''
    env = os.environ.copy()
    env["PYTHONPATH"] = str(tmp_path)
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env,
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
