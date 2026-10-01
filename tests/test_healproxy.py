#!/usr/bin/env python3
"""Regressionstests fuer healproxy (gufo Schicht 2) — ohne Modell, ohne Port.

Die Fall-Buchstaben folgen der Doku (docs/gufo.md §8.29-§8.31), damit ein
Fehlschlag dort nachlesbar ist:

  A  geschlossener Markup-Leak, in 7-Byte-Brocken geheilt -> sauberer tool_call
  B  markup-freier Content (html-artige Tags) -> unbearbeitet, kein Heal
  C  Salvage ohne parameter-Closer -> getrennte Argumente (§8.29)
  D  wörtlicher parameter-Anfang im Wert -> bleibt EIN Parameter
  E  Prosa-Erwähnung der Framing-Tokens -> nichts geht verloren (§8.30)
  F  verschachteltes tool_call-Ende im Argumentwert -> Pfad UND Inhalt heil
  G  usage-Chunk und echter tool_calls-Delta -> unveraendert durchgereicht
  H  Prosa mit function=-Platzhalter -> bleibt Prosa, kein Bogus-Call (§8.31)
  S* Request-Sanitizer (ungueltige Funktionsnamen)
  I* dokumentierte Lücken und Eigenheiten — pinnt Verhalten, damit eine
     spaetere Aufweichung nicht lautlos durchgeht

Die Framing-Tokens werden hier ausschliesslich aus Einzelzeichen gebaut
(chr(60) ...): ein wörtliches Vorkommen im Quelltext dieser Datei würde vom
Harness-Sanitizer der Schreibenden zerschnitten (gleicher Mechanismus wie §8.30).

Aufruf:  tests/test_healproxy.py [--src PFAD]   (Default: ../healproxy.py)
Heal-Logs gehen in ein tempdir; die Produktions-logs/healed.jsonl wird nicht
geschrieben (Grösse wird vor/nach verglichen). Exit 0 = alles grün.
"""
import argparse
import importlib.util
import io
import json
import os
import tempfile

LT, GT, SL = chr(60), chr(62), chr(47)
ELL = chr(0x2026)
TC = LT + "tool_call" + GT
TE = LT + SL + "tool_call" + GT
FF = LT + "function="
FE = LT + SL + "function" + GT
PE = LT + "parameter="
PC = LT + SL + "parameter" + GT

BASE = {"id": "c1", "object": "chat.completion.chunk", "created": 7, "model": "m"}

RESULTS = []


def ok(label, cond, *info):
    RESULTS.append((bool(cond), label))
    print(("PASS " if cond else "FAIL ") + label, *([] if cond else info))


def block(name, params, close_block=True, close_vals=True):
    """Baut einen Call aus den Einzelfragmenten des Qwen-Framings."""
    parts = [TC, FF + name + GT]
    for k, v in params.items():
        parts.append(PE + k + GT + v + (PC if close_vals else ""))
    if close_block:
        parts += [FE, TE]
    return "\n".join(parts)


def stream(hp, text, chunk=None, fr="stop"):
    """Faellt Text durch HealStream und extrahiert Prosa, Calls, finish."""
    hs = hp.HealStream()
    evs = []
    if chunk:
        for i in range(0, len(text), chunk):
            evs += hs.feed(BASE, text[i:i + chunk])
    else:
        evs = hs.feed(BASE, text)
    evs += hs.finish(BASE, fr)
    prose = "".join(c["choices"][0]["delta"].get("content") or "" for c in evs)
    calls = []
    for c in evs:
        d = c["choices"][0]["delta"]
        if "tool_calls" in d:
            fn = d["tool_calls"][0]["function"]
            calls.append((fn["name"], json.loads(fn["arguments"])))
    fin = [c["choices"][0]["finish_reason"] for c in evs
           if c["choices"][0].get("finish_reason")]
    return prose, calls, fin


def sse(hp, chunks, extra_events=()):
    """Faellt Brocken durch den echten Proxy-Pfad Handler._stream_healed."""
    lines = []
    for text in chunks:
        lines.append(("data: " + json.dumps({**BASE, "choices": [
            {"index": 0, "delta": {"content": text}}]}) + "\n\n").encode())
    for ev in extra_events:
        lines.append(("data: " + json.dumps({**BASE, **ev}) + "\n\n").encode())
    lines.append(("data: " + json.dumps({**BASE, "choices": [
        {"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}) + "\n\n").encode())
    lines.append(b"data: [DONE]\n\n")

    class Resp:
        def __init__(self, data):
            self.f = io.BytesIO(data)

        def readline(self):
            return self.f.readline()

    class Fake:
        _write_event = hp.Handler._write_event

    fake = Fake()
    fake.wfile = io.BytesIO()
    hp.Handler._stream_healed(fake, Resp(b"".join(lines)), 999)
    raw = fake.wfile.getvalue().decode()
    evs = [json.loads(l[5:].strip()) for l in raw.splitlines()
           if l.startswith("data:") and "[DONE]" not in l]
    prose = "".join(e["choices"][0]["delta"].get("content") or ""
                    for e in evs if e.get("choices"))
    calls = []
    for e in evs:
        for ch in e.get("choices") or []:
            for tc in ch["delta"].get("tool_calls") or []:
                fn = tc["function"]
                calls.append((fn["name"], fn["arguments"], tc.get("id")))
    return raw, prose, calls, evs


# --------------------------------------------------------------- HealStream
def test_stream(hp):
    prose, calls, fin = stream(hp, "Ich suche die Datei. " + block(
        "glob", {"path": "/tmp/x", "gitignore": "False"}), chunk=7)
    ok("A  Leak in 7-Brocken -> sauberer tool_call",
       calls == [("glob", {"path": "/tmp/x", "gitignore": False})]
       and prose == "Ich suche die Datei. " and fin == ["tool_calls"], calls, repr(prose), fin)

    text = "Hallo " + LT + "b" + GT + "fett" + LT + SL + "b" + GT + " tools?"
    prose, calls, fin = stream(hp, text)
    ok("B  html-artige Tags bleiben unbearbeitet",
       calls == [] and prose == text and fin == ["stop"], calls, repr(prose), fin)

    prose, calls, fin = stream(hp, "Antwort. " + block(
        "edit", {"path": "/tmp/y", "Truearg": "True", "n": "42"},
        close_block=False, close_vals=False))
    ok("C  Salvage ohne Closer -> getrennte Argumente",
       calls == [("edit", {"path": "/tmp/y", "Truearg": True, "n": 42})]
       and prose == "Antwort. " and fin == ["tool_calls"], calls, repr(prose))
    prose, calls, _ = stream(hp, "X " + block(
        "run", {"cmd": "ls", "flag": "True"}, close_block=False, close_vals=False))
    ok("C2 Salvage gemischt -> beide Argumente",
       calls == [("run", {"cmd": "ls", "flag": True})], calls)

    prose, calls, fin = stream(hp, block("note", {"text": "nutzung: " + PE + "x" + GT + "y"}))
    ok("D  woertlicher parameter-Anfang im Wert -> ein Parameter",
       calls == [("note", {"text": "nutzung: " + PE + "x" + GT + "y"})], calls)

    prose_expect = ("Die " + TC + " und " + TE + " sind Framing-Tokens, "
                    + FF + "name" + GT + " beschreibt den Call.")
    prose, calls, fin = stream(hp, prose_expect)
    ok("E  Prosa-Erwaehnung -> kein Zeichen verloren",
       calls == [] and prose == prose_expect and fin == ["stop"], calls, repr(prose))

    big = "x" * 5000 + " rand " + TC + " nur prose"
    prose, calls, fin = stream(hp, big)
    ok("E2 grosser Prosapuffer -> frueh freigegeben",
       calls == [] and prose == big, calls, len(prose))

    midword = "Der text" + LT + "tool_callout" + GT + "token ist kaputt"
    prose, calls, fin = stream(hp, midword)
    ok("E3 Praefix im Wort (tool_callout) -> kein Heal",
       calls == [] and prose == midword, calls)

    inner = "vor " + TC + "MITTE" + TE + " nach"
    prose, calls, fin = stream(hp, "Ich erklaere: " + block(
        "write", {"path": "/tmp/p.txt", "content": inner}))
    ok("F  verschachteltes Ende im Wert -> Pfad und Inhalt heil",
       calls == [("write", {"path": "/tmp/p.txt", "content": inner})]
       and prose == "Ich erklaere: ", calls)

    two = block("glob", {"p": "1"}) + "\n" + block("read", {"p": "2"})
    prose, calls, fin = stream(hp, "a" + two + "b")
    ok("F2 zwei Calls in einem Stream",
       calls == [("glob", {"p": 1}), ("read", {"p": 2})] and prose == "a\nb", calls, repr(prose))

    prosa = ("Fix: der exakte Fall " + FF + ELL + GT + " bleibt Prosa, kein Call. Und "
             + PE + "pfad" + GT + "/tmp" + PC)
    for chunk in (None, 7, 5):
        prose, calls, fin = stream(hp, prosa, chunk=chunk)
        ok("H  function=-Platzhalter bleibt Text (chunk=%s)" % chunk,
           calls == [] and prose == prosa and fin == ["stop"], calls, repr(prose[:60]), fin)

    closed = "Beispiel " + TC + FF + ELL + GT + PE + "x" + GT + "1" + PC + FE + TE + " ende"
    prose, calls, fin = stream(hp, closed)
    ok("H2 geschlossener Platzhalter-Block -> Prosa",
       calls == [] and closed in prose and fin == ["stop"], calls)

    prose, calls, _ = stream(hp, block("edit", {"path": "/tmp/y"}))
    ok("H3 positiver Kontrollfall edit heilt weiter",
       len(calls) == 1 and calls[0][0] == "edit", calls)
    prose, calls, _ = stream(hp, block("a.b-c_1", {"p": "v"}))
    ok("H4 erlaubte Satzzeichen im Namen heilen",
       len(calls) == 1 and calls[0][0] == "a.b-c_1", calls)


# ---------------------------------------------------------------- SSE-Pfad
def test_sse_path(hp):
    leak = "Ich suche die Datei. " + block("glob", {"path": "/tmp/x", "gitignore": "False"})
    nested = "Doku: " + block("write", {"path": "/tmp/a.txt",
                                        "content": "text " + TC + "inner" + TE + " ende"})
    real = {"type": "function", "name": "read", "arguments": '{"path":"/tmp/real"}'}
    raw, prose, calls, evs = sse(
        hp, [leak[:12], leak[12:40], leak[40:], " " + nested],
        extra_events=[{"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 9, "id": "call_real", "type": "function", "function": real}]}}]}])
    names = [c[0] for c in calls]
    ok("G  echter tool_calls-Delta unveraendert",
       len(calls) == 3 and calls[2][1] == real["arguments"] and calls[2][2] == "call_real", calls)
    ok("G  zwei healed Calls + echter Call, Prosa intakt",
       names == ["glob", "write", "read"] and prose == "Ich suche die Datei.  Doku: "
       and raw.rstrip().endswith("data: [DONE]"), names, repr(prose))

    usage = {"prompt_tokens": 20830, "completion_tokens": 120,
             "prompt_tokens_details": {"cached_tokens": 20480}}
    raw, prose, calls, evs = sse(hp, ["hallo"], extra_events=[{"choices": [], "usage": usage}])
    got = [e.get("usage") for e in evs if e.get("usage")]
    ok("G2 usage-Chunk unveraendert durchgereicht", got == [usage] and prose == "hallo", got, prose)


# --------------------------------------------------------------- Sanitizer
def _body(**kw):
    d = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    d.update(kw)
    return json.dumps(d, ensure_ascii=False).encode("utf-8")


def _tc_body(name):
    return _body(messages=[
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": name, "arguments": '{"a":1}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"}])


def _tool_body(name):
    return _body(tools=[{"type": "function", "function": {
        "name": name, "description": "d", "parameters": {"type": "object"}}}])


def test_sanitizer(hp):
    clean = _body(tools=[{"type": "function", "function": {"name": "web_search", "parameters": {}}}])
    out, n = hp.sanitize_tool_names(clean)
    ok("S1 sauberer Body byte-identisch (n=0)", out == clean and n == 0, n)

    out, n = hp.sanitize_tool_names(_tc_body(ELL))
    got = json.loads(out)["messages"][1]["tool_calls"][0]["function"]["name"]
    ok("S2 messages.tool_calls mit Platzhalter -> '_'", got == "_" and n == 1, repr(got), n)

    out, _ = hp.sanitize_tool_names(_tool_body("tool/" + ELL))
    ok("S3 tools[].function.name bereinigt",
       json.loads(out)["tools"][0]["function"]["name"] == "tool__")
    out, _ = hp.sanitize_tool_names(_tool_body(""))
    ok("S4 leerer Name -> 'tool'", json.loads(out)["tools"][0]["function"]["name"] == "tool")

    out, _ = hp.sanitize_tool_names(_tool_body("x" * 60 + ELL + "y" * 20))
    got = json.loads(out)["tools"][0]["function"]["name"]
    ok("S5 Ueberlaenge -> 64 Zeichen und gueltig", len(got) == 64 and hp.NAME_RE.match(got), len(got))

    ok("S6 Nicht-JSON unveraendert", hp.sanitize_tool_names(b"garbage {") == (b"garbage {", 0))
    ok("S7 JSON-Liste unveraendert", hp.sanitize_tool_names(b"[1,2,3]") == (b"[1,2,3]", 0))

    once, _ = hp.sanitize_tool_names(_tc_body(ELL))
    twice, n2 = hp.sanitize_tool_names(once)
    ok("S8 idempotent", twice == once and n2 == 0, n2)

    arghaltig = _body(messages=[{"role": "assistant", "content": None, "tool_calls": [
        {"id": "c", "type": "function", "function": {
            "name": "write", "arguments": json.dumps({"t": "wort " + ELL + " ende"})}}]}])
    out, n = hp.sanitize_tool_names(arghaltig)
    ok("S9 arguments-Inhalt unangetastet", out == arghaltig and n == 0, n)

    two = _body(messages=[{"role": "assistant", "content": None, "tool_calls": [
        {"id": "a", "type": "function", "function": {"name": ELL, "arguments": "{}"}},
        {"id": "b", "type": "function", "function": {"name": "ok_name", "arguments": "{}"}},
        {"id": "c", "type": "function", "function": {"name": "?" + ELL, "arguments": "{}"}}]}])
    out, n = hp.sanitize_tool_names(two)
    ok("S10 zwei ungueltige Namen -> count 2", n == 2, n)

    names = ["read", "web_search", "telegram_send", "a.b-c_1", "x" * 64]
    ok("S11 gueltige Namen bleiben unveraendert",
       all(hp.sanitize_tool_names(_tool_body(nm)) == (_tool_body(nm), 0) for nm in names))


# ------------------------------------------------- Lücken und Eigenheiten
def test_known_gaps(hp):
    tc_choice = _body(tool_choice={"type": "function", "function": {"name": ELL}})
    out, n = hp.sanitize_tool_names(tc_choice)
    ok("I1 tool_choice.function.name wird NICHT bereinigt (Luecke §8.31)",
       out == tc_choice and n == 0, n)

    out, n = hp.sanitize_tool_names(_tool_body("browser:click"))
    ok("I2 NAME_RE strenger als gufo 'printable ASCII' (Doppelpunkt -> '_')",
       n == 1 and json.loads(out)["tools"][0]["function"]["name"] == "browser_click", n)

    prose, calls, fin = stream(hp, "Antwort " + TC + FF + "ev", fr="length")
    ok("I3 abgeschnittener Name ohne '>' -> bleibt Text",
       calls == [] and prose == "Antwort " + TC + FF + "ev", calls, repr(prose))

    prose, calls, fin = stream(hp, "Antwort " + TC + FF + "ev" + GT + PE + "alu", fr="length")
    ok("I4 abgeschnittener Name MIT '>' -> Bogus-Tool 'ev' (Restrisiko)",
       calls == [("ev", {})] and fin == ["tool_calls"], calls, fin)

    args = json.loads(hp.parse_qwen_call(
        FF + "x" + GT + PE + "sha" + GT + "0012" + PC + PE + "port" + GT + "8080" + PC + FE
    )["arguments"])
    ok("I5 Wert-Coercion frisst fuehrende Nullen (bekannter Nit)",
       args == {"sha": 12, "port": 8080}, args)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), os.pardir, "healproxy.py"))
    args = ap.parse_args()
    src = os.path.realpath(args.src)

    prod_log = os.path.join(os.path.dirname(src), "logs", "healed.jsonl")
    size_before = os.path.getsize(prod_log) if os.path.exists(prod_log) else -1

    os.environ["HEALPROXY_DIR"] = tempfile.mkdtemp(prefix="healproxy-tests-")
    spec = importlib.util.spec_from_file_location("hp_under_test", src)
    hp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hp)  # main-guard: kein Serverstart

    print("quelle:", src)
    print("heal-logs ->", os.environ["HEALPROXY_DIR"])
    test_stream(hp)
    test_sse_path(hp)
    test_sanitizer(hp)
    test_known_gaps(hp)

    size_after = os.path.getsize(prod_log) if os.path.exists(prod_log) else -1
    fails = [label for good, label in RESULTS if not good]
    print("\n%d/%d gruen" % (len(RESULTS) - len(fails), len(RESULTS)))
    if fails:
        print("fehlgeschlagen:", ", ".join(fails))
    if size_before != size_after:
        print("WARNUNG: Produktions-Log geschrieben:", prod_log)
        return 1
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())