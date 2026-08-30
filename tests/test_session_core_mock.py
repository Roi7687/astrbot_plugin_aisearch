import sys, os, asyncio, types, traceback, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
stub_cb = types.SimpleNamespace(launch_async=lambda **kw: None)
sys.modules['cloakbrowser'] = stub_cb
sys.modules['markdownify'] = types.SimpleNamespace(markdownify=lambda html, **kw: "[md]")

from core.config import MODE_NORMAL, MODE_VISION, CONVERSATIONS_FILE
from core.session_core import DeepSeekSessionCore, Conversation

def write_conv_file(data):
    with open(CONVERSATIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

async def main():
    # 1. 初始化：多会话 + 本地 id 计数
    core = DeepSeekSessionCore()
    assert core.conversations == {}, "init"
    assert core.next_id == 1
    assert core.current_id is None
    assert core.current_mode == MODE_NORMAL
    assert core.current_conversation is None

    # 2. 销毁不存在的会话
    ok = await core.destroy_conversation(1)
    assert ok is False, "destroy missing"

    # 3. 手工登记一个会话并检查摘要
    conv = Conversation(local_id=1, mode=MODE_NORMAL, session_id="test-123",
                        url="https://chat.deepseek.com/a/chat/s/test-123",
                        message_count=2, created_at=100.0, last_active=200.0)
    core.conversations[1] = conv
    core.current_id = 1
    core._save_conversations()
    s = core.conversation_summary(1)
    assert s["exists"] and s["session_id"] == "test-123" and s["message_count"] == 2, str(s)
    assert s["is_current"] is True and s["local_id"] == 1 and s["label"] == "普通对话", str(s)
    s2 = core.conversation_summary(2)
    assert s2["exists"] is False, str(s2)

    # 4. 列表摘要按 id 升序
    core.conversations[2] = Conversation(local_id=2, mode=MODE_VISION, message_count=1,
                                         created_at=1.0, last_active=1.0)
    ids = [it["local_id"] for it in core.list_summary()]
    assert ids == [1, 2], ids

    # 5. 销毁后删除记录（v2.2.11 起不再保留已销毁会话）
    ok = await core.destroy_conversation(1)
    assert ok is True
    assert 1 not in core.conversations, "destroy removes record"
    assert core.current_id is None, "destroy current resets"
    ok = await core.destroy_conversation(1)
    assert ok is False, "destroy twice"

    # 6. 重启加载（新格式；已销毁记录不保留）
    core2 = DeepSeekSessionCore()
    assert 1 not in core2.conversations, "reload drops destroyed"
    assert core2.conversations[2].mode == MODE_VISION
    assert core2.current_id is None, "reload current_id"

    # 7. 旧版双槽位格式自动迁移为本地 id（normal=1, vision=2）
    write_conv_file({
        "normal": {"mode": "normal", "session_id": "old-n",
                   "url": "https://chat.deepseek.com/a/chat/s/old-n",
                   "message_count": 3, "created_at": 1.0, "last_active": 10.0,
                   "destroyed": False},
        "vision": {"mode": "vision", "session_id": "old-v",
                   "url": "https://chat.deepseek.com/a/chat/s/old-v",
                   "message_count": 1, "created_at": 2.0, "last_active": 20.0,
                   "destroyed": False},
    })
    core3 = DeepSeekSessionCore()
    assert set(core3.conversations) == {1, 2}, list(core3.conversations)
    assert core3.conversations[1].mode == MODE_NORMAL
    assert core3.conversations[2].mode == MODE_VISION
    assert core3.next_id == 3, "next_id after migrate"
    assert core3.current_id == 2, "migrate picks latest alive"

    # 7b. 旧格式中含已销毁槽位：迁移后清理（v2.2.11）
    write_conv_file({
        "normal": {"mode": "normal", "message_count": 1,
                   "created_at": 1.0, "last_active": 1.0, "destroyed": True},
        "vision": {"mode": "vision", "message_count": 1,
                   "created_at": 2.0, "last_active": 2.0, "destroyed": False},
    })
    core3b = DeepSeekSessionCore()
    assert set(core3b.conversations) == {2}, list(core3b.conversations)
    assert core3b.current_id == 2, "migrate picks alive after cleanup"

    # 8. 会话 id 提取
    assert core._extract_session_id("https://chat.deepseek.com/a/chat/s/abc-123-456") == "abc-123-456"
    assert core._extract_session_id("https://chat.deepseek.com/s/dead-beef-1234") == "dead-beef-1234"
    assert core._extract_session_id("https://chat.deepseek.com/") == ""

    # 9. _mark_active 发送后回写会话 URL / ID，并更新 current
    import types as _t
    core.page = _t.SimpleNamespace(url="https://chat.deepseek.com/a/chat/s/abc-123-456")
    conv3 = Conversation(local_id=9, mode=MODE_VISION, created_at=1.0, last_active=1.0)
    core.conversations[9] = conv3
    core._mark_active(conv3)
    assert conv3.message_count == 1
    assert conv3.session_id == "abc-123-456"
    assert "/a/chat/s/" in conv3.url
    assert core.current_id == 9
    assert core.current_mode == MODE_VISION

    # 10. 切换不存在的 id 报错
    try:
        await core.switch_conversation(999)
        raise AssertionError("switch missing id should raise")
    except Exception as e:
        assert "999" in str(e), str(e)

    if os.path.exists(CONVERSATIONS_FILE):
        os.remove(CONVERSATIONS_FILE)
    print("ALL MOCK TESTS PASSED")

try:
    asyncio.run(main())
except Exception:
    traceback.print_exc()
    sys.exit(1)
