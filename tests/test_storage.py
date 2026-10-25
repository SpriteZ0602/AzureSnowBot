"""写入中断保留旧文件、并发字段更新及损坏数据保护。"""

import json
from concurrent.futures import ThreadPoolExecutor
import pytest
from botcore import storage


def test_replace_failure_keeps_old_file_and_cleans_temp(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text('{"old": true}', encoding="utf-8")
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(storage.os, "replace", fail)
    with pytest.raises(OSError):
        storage.atomic_write_json(path, {"new": True})
    assert json.loads(path.read_text(encoding="utf-8")) == {"old": True}
    assert list(tmp_path.iterdir()) == [path]


def test_concurrent_field_updates_are_not_lost(tmp_path):
    path = tmp_path / "config.json"
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda n: storage.update_json(path, {str(n): n}), range(50)))
    assert storage.load_json(path) == {str(n): n for n in range(50)}


def test_corrupt_config_cannot_be_replaced_by_update(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"broken":', encoding="utf-8")
    with pytest.raises(ValueError):
        storage.update_json(path, {"last_message_at": "now"})
    assert path.read_text(encoding="utf-8") == '{"broken":'
