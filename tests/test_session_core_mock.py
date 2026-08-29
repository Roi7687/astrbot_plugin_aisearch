import sys, os, asyncio, types, traceback
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
stub_cb = types.SimpleNamespace(launch_async=lambda **kw: None)
sys.modules['cloakbrowser'] = stub_cb
sys.modules['markdownify'] = types.SimpleNamespace(markdownify=lambda html, **kw: "[md]")

from core.config import MODE_NORMAL, MODE_VISION, CONVERSATIONS_FILE
from core.session_core import DeepSeekSessionCore, Conversation

async def main():
    core = DeepSeekSessionCore()
    assert core.conversations == {}, "init"
    assert core.current_mode == MODE_NORMAL
    ok = await core.destroy_conversation(MODE_NORMAL)
    assert ok is False, "destroy missing"
    conv = Conversation(mode=MODE_NORMAL, session_id="test-123",
                        url="https://chat.deepseek.com/a/chat/s/test-123",
                        message_count=2, created_at=100.0, last_active=200.0)
    core.conversations[MODE_NORMAL] = conv
    core._save_conversations()
    s = core.conversation_summary(MODE_NORMAL)
    assert s["exists"] and s["session_id"] == "test-123" and s["message_count"] == 2, str(s)
    assert s["is_current"] is True
    s2 = core.conversation_summary(MODE_VISION)
    assert s2["exists"] is False, str(s2)
    ok = await core.destroy_conversation(MODE_NORMAL)
    assert ok is True
    assert core.conversations[MODE_NORMAL].destroyed is True
    core2 = DeepSeekSessionCore()
    assert core2.conversations[MODE_NORMAL].destroyed is True, "reload destroyed"
    assert core2.conversations[MODE_NORMAL].message_count == 2
    assert core._extract_session_id("https://chat.deepseek.com/a/chat/s/abc-123-456") == "abc-123-456"
    assert core._extract_session_id("https://chat.deepseek.com/s/dead-beef-1234") == "dead-beef-1234"
    assert core._extract_session_id("https://chat.deepseek.com/") == ""
    # _mark_active 发送后回写会话 URL / ID（用伪 page）
    import types as _t
    core.page = _t.SimpleNamespace(url="https://chat.deepseek.com/a/chat/s/abc-123-456")
    conv3 = Conversation(mode=MODE_VISION, created_at=1.0, last_active=1.0)
    core.conversations[MODE_VISION] = conv3
    core._mark_active(conv3)
    assert conv3.message_count == 1
    assert conv3.session_id == "abc-123-456"
    assert "/a/chat/s/" in conv3.url
    assert core.current_mode == MODE_VISION
    if os.path.exists(CONVERSATIONS_FILE):
        os.remove(CONVERSATIONS_FILE)
    print("ALL MOCK TESTS PASSED")

try:
    asyncio.run(main())
except Exception:
    traceback.print_exc()
    sys.exit(1)
