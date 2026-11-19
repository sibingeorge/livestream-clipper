import json
from conftest import BASE
from clipper.detection import process_windows, robust_z


def seed(db, sid, n_base=20, spike=None, chat_spike=None, loud_spike=None):
    """windows every 10 s; baseline chat 1/win, -30 dB. spike index gets chat and/or loudness boosts."""
    for i in range(n_base + 2):
        t = BASE + i * 10
        loud = -30.0
        chat_n = 1
        if i == spike:
            chat_n, loud = chat_spike, loud_spike
        db.execute("INSERT INTO windows(stream_id,t_start,t_end,chat_count,loudness_db) VALUES (?,?,?,0,?)",
                   (sid, t, t + 10, loud))
        db.insert_many("INSERT INTO chat_messages(stream_id,ts,author,text) VALUES (?,?,?,?)",
                       [(sid, t + 1 + k * 0.1, f"a{k % 12}", "omg no way") for k in range(chat_n)])


def test_robust_z_flat_history_does_not_divide_by_zero():
    assert robust_z([1, 1, 1, 1], 1, 1.0) == 0
    assert robust_z([-30.0] * 10, -10.0, 1.5) > 5


def test_both_spike_makes_one_candidate(cfg, db):
    seed(db, 1, spike=15, chat_spike=40, loud_spike=-10.0)
    ids = process_windows(db, 1, cfg, now=BASE + 10_000)
    assert len(ids) == 1
    c = db.one("SELECT * FROM candidates")
    assert c["event_ts"] == BASE + 160 and c["score"] >= cfg.scoring.min_score
    assert "chat z" in " ".join(json.loads(c["reasons"]))


def test_single_signal_does_not_trigger(cfg, db):
    seed(db, 1, spike=15, chat_spike=40, loud_spike=-30.0)   # chat only
    seed(db, 2, spike=15, chat_spike=1, loud_spike=-10.0)    # audio only
    assert process_windows(db, 1, cfg, now=BASE + 10_000) == []
    assert process_windows(db, 2, cfg, now=BASE + 10_000) == []


def test_warmup_no_candidates(cfg, db):
    seed(db, 1, n_base=6, spike=5, chat_spike=40, loud_spike=-10.0)
    assert process_windows(db, 1, cfg, now=BASE + 10_000) == []


def test_cooldown_dedupes_adjacent_spikes(cfg, db):
    seed(db, 1, spike=15, chat_spike=40, loud_spike=-10.0)
    process_windows(db, 1, cfg, now=BASE + 10_000)
    from clipper.detection import _add_candidate
    # a second adjacent spike (10 s later) with a lower score must not create a second candidate
    assert _add_candidate(db, 1, BASE + 170, 3.1, 2.6, [("a", "x")], cfg, False) is None
    assert db.one("SELECT COUNT(*) n FROM candidates")["n"] == 1


def test_degraded_chat_requires_flag(cfg, db):
    seed(db, 1, spike=15, chat_spike=1, loud_spike=0.0)
    assert process_windows(db, 1, cfg, chat_available=False, now=BASE + 10_000) == []
    cfg.detection.audio_only_fallback = True
    seed(db, 2, spike=15, chat_spike=1, loud_spike=0.0)
    assert len(process_windows(db, 2, cfg, chat_available=False, now=BASE + 10_000)) == 1
