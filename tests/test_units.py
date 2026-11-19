import json
import pytest

from clipper import rights
from clipper.captions import build_ass, group_words, ts
from clipper.chat import twitch, youtube
from clipper.config import load_config
from clipper.db import Database
from clipper.discovery.base import StreamInfo, register_streams
from clipper.discovery.twitch import parse_streams
from clipper.discovery.youtube import info_to_stream
from clipper.story import build_story, clean_messages
from clipper.transcribe import Word


def test_config_rejects_unknown_keys(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[detection]\nchat_zz = 3\n")
    with pytest.raises(ValueError, match="unknown config key"):
        load_config(p)
    p.write_text("[detection]\nchat_z = 4.5\n[clip]\nlength = 25\n")
    c = load_config(p)
    assert c.detection.chat_z == 4.5 and c.clip.length == 25 and c.clip.pre_roll == 20


def test_rights_default_deny_and_allowlist():
    db = Database(":memory:")
    assert not rights.check_source(db, "youtube", "UC1").allowed
    assert not rights.check_source(db, "youtube", None).allowed
    with pytest.raises(ValueError):
        rights.set_source(db, "youtube", "UC1", "allowed")  # basis required
    rights.set_source(db, "youtube", "UC1", "allowed", basis="written-permission")
    assert rights.check_source(db, "youtube", "UC1", {"is_live": True}).allowed
    assert not rights.check_source(db, "youtube", "UC1", {"availability": "subscriber_only"}).allowed
    assert not rights.check_source(db, "youtube", "UC1", {"age_limit": 18}).allowed
    rights.set_source(db, "youtube", "UC1", "denied")
    assert not rights.check_source(db, "youtube", "UC1").allowed


def test_register_streams_blocks_unlisted():
    db = Database(":memory:")
    rights.set_source(db, "twitch", "ok", "allowed", basis="owned")
    res = register_streams(db, [StreamInfo("twitch", "ok", "u", channel_id="ok", viewers=5, meta={"is_live": True}),
                                StreamInfo("twitch", "no", "u", channel_id="no")])
    assert [r[1] for r in res] == [True, False]
    assert db.one("SELECT state FROM streams WHERE video_id='no'")["state"] == "blocked"


def test_discovery_parsers_tolerate_garbage():
    assert info_to_stream({}) is None and info_to_stream(None) is None
    assert info_to_stream({"id": "a", "is_live": False}) is None
    s = info_to_stream({"id": "a", "title": "T", "concurrent_view_count": "123", "channel_id": "UC", "is_live": True})
    assert s.viewers == 123 and s.channel_id == "UC"
    assert parse_streams({"data": [{"user_login": "x", "viewer_count": "7"}, {"nope": 1}]})[0].viewers == 7
    assert parse_streams(None) == []


def test_twitch_irc_parse():
    line = "@display-name=Bob;tmi-sent-ts=1700000000123 :bob!bob@bob.tmi.twitch.tv PRIVMSG #chan :NO WAY"
    m = twitch.parse_line(line)
    assert (m.author, m.text) == ("Bob", "NO WAY") and abs(m.ts - 1700000000.123) < 1e-6
    assert twitch.parse_line(":tmi.twitch.tv 001 justinfan1 :Welcome") is None


def test_youtube_chat_parse():
    obj = {"replayChatItemAction": {"actions": [{"addChatItemAction": {"item": {"liveChatTextMessageRenderer": {
        "message": {"runs": [{"text": "omg "}, {"emoji": {"shortcuts": [":fire:"]}}]},
        "authorName": {"simpleText": "Al"}, "timestampUsec": "1700000000500000"}}}}]}}
    m = youtube.parse_line(json.dumps(obj))
    assert m.text == "omg :fire:" and m.author == "Al" and m.ts == 1700000000.5
    assert youtube.parse_line("garbage") is None and youtube.parse_line("{}") is None


def test_story_title_from_chat_and_blocklist():
    msgs = [(f"u{i}", t) for i, t in enumerate(["trailer is insane", "that trailer though", "trailer!!",
                                                "fuck this", "https://spam.example buy", "LUL"])]
    assert all("fuck" not in t for _, t in clean_messages(msgs))
    s = build_story(msgs, [], "Chan")
    assert "trailer" in s.title.lower() and len(s.title) <= 70
    assert "#GTA6" in s.hashtags
    assert build_story([], [], "").title == "The moment chat went wild"


def test_spammer_counts_once():
    msgs = [("spam", "pogchamp banana")] * 20 + [("a", "trailer"), ("b", "trailer"), ("c", "trailer")]
    assert "trailer" in build_story(msgs, []).title


def test_captions_grouping_and_ass():
    words = [Word(i * 0.3, i * 0.3 + 0.25, f"w{i}") for i in range(9)] + [Word(5.0, 5.4, "late")]
    g = group_words(words)
    assert [t.count(' ') + 1 for _, _, t in g] == [4, 4, 1, 1]  # 4+4+1, then 'late' after a long gap
    assert all(a < b for a, b, _ in g) and all(g[i][1] <= g[i + 1][0] + 1e-9 for i in range(len(g) - 1))
    assert ts(3661.5) == "1:01:01.50"
    ass = build_ass(words, "HOOK {x}", "Source: A", 1080, 1920, 30)
    assert "PlayResY: 1920" in ass and "Style: Hook" in ass and "{x}" not in ass.split("[Events]")[1]


def test_background_chatter_is_not_the_title():
    background = [(f"u{i}", "hello stream") for i in range(6)]
    spike = background[:2] + [(f"v{i}", "no way trailer is real") for i in range(8)]
    s = build_story(spike, [], "C", context=background, context_scale=1.0)
    assert "hello" not in s.title.lower() and ("trailer" in s.title.lower() or "way" in s.title.lower())
