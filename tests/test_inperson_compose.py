

# ── brief, catered connection note (the "fun fact" callback) ─────────────────

def test_topic_note_leads_with_the_callback():
    # A topic-style note ("the Ottawa bagel spot") slots after "about" and the
    # invite stays brief (well under LinkedIn's 300-char cap).
    d = compose(_p(note="the Ottawa bagel spot"), _ev(label="SF Mixer"))
    assert "chatting about the Ottawa bagel spot" in d.note
    assert "SF Mixer" in d.note
    assert len(d.note) <= 300


def test_fact_note_reads_as_you_are():
    # A preposition-led "fact" note ("from Ottawa") becomes a "love that you're …"
    # callback instead of the awkward "chatting about from Ottawa", in both the
    # connection note and the post-accept DM.
    d = compose(_p(note="from Ottawa"), _ev(label="LinkedIn Local"))
    assert "love that you're from Ottawa" in d.note
    assert "chatting about from Ottawa" not in d.note
    assert "Love that you're from Ottawa" in d.message


def test_conversational_leadin_is_stripped():
    # "we talked about X" must not double up into "chatting about we talked about".
    d = compose(_p(note="we talked about rock climbing"), _ev(label="YC Day"))
    assert "chatting about rock climbing" in d.note
    assert "we talked about" not in d.note


def test_no_note_stays_generic_and_brief():
    d = compose(_p(note=None), _ev(label="Web Summit"))
    assert "Web Summit" in d.note
    assert "love that you're" not in d.note.lower()
    assert "chatting about" not in d.note.lower()
    assert len(d.note) <= 300
