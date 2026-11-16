from lectern.captions import lang_from_filename, parse_json3, parse_subtitles, to_vtt

ROLLING = """WEBVTT
Kind: captions
Language: en

00:00:00.160 --> 00:00:02.869 align:start position:0%
 
hi<00:00:00.480><c> everyone</c><00:00:00.880><c> so</c>

00:00:02.869 --> 00:00:02.879 align:start position:0%
hi everyone so
 

00:00:02.879 --> 00:00:05.150 align:start position:0%
hi everyone so
by<00:00:03.159><c> now</c><00:00:03.360><c> you</c>

00:00:05.150 --> 00:00:05.160 align:start position:0%
by now you
 

00:00:05.160 --> 00:00:07.000 align:start position:0%
by now you
have seen
"""


def test_rolling_youtube_vtt_is_deduplicated():
    segs = parse_subtitles(ROLLING)
    assert [s.text for s in segs] == ["hi everyone so", "by now you", "have seen"]
    assert segs[0].start == 0.16 and abs(segs[0].end - 2.869) < 1e-6
    assert segs[1].start == 2.879


def test_srt():
    srt = "1\r\n00:00:01,000 --> 00:00:02,500\r\n<i>Hello</i> &amp; welcome\r\n\r\n2\r\n00:00:03,000 --> 00:00:04,000\r\nBye\r\n"
    segs = parse_subtitles(srt)
    assert [(s.start, s.end, s.text) for s in segs] == [(1.0, 2.5, "Hello & welcome"), (3.0, 4.0, "Bye")]


def test_json3():
    doc = '{"events":[{"tStartMs":0,"dDurationMs":2000,"segs":[{"utf8":"hello"},{"utf8":" world"}]},' \
          '{"tStartMs":1500,"aAppend":1,"segs":[{"utf8":"\\n"}]},{"tStartMs":2000,"dDurationMs":1000,"segs":[{"utf8":"again"}]}]}'
    segs = parse_json3(doc)
    assert [(s.start, s.end, s.text) for s in segs] == [(0.0, 2.0, "hello world"), (2.0, 3.0, "again")]


def test_round_trip():
    segs = parse_subtitles(to_vtt(parse_subtitles(ROLLING)))
    assert [s.text for s in segs] == ["hi everyone so", "by now you", "have seen"]


def test_lang_from_filename():
    assert lang_from_filename("Talk", "Talk.en.vtt") == "en"
    assert lang_from_filename("Talk", "Talk.en-orig.vtt") == "en"
    assert lang_from_filename("Talk", "Talk.hi-IN.srt") == "hi"
    assert lang_from_filename("Talk", "Talk.vtt") is None
    assert lang_from_filename("Talk", "Talk.English.srt") is None
