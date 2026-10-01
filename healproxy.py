#!/usr/bin/env python3
"""healproxy — Reverse-Proxy vor gufo mit Qwen-Markup-Healing ("Schicht 2").

Lauscht auf 127.0.0.1:8085, leitet an gufo (:8080) weiter. Im SSE-Content-Stream
erkanntes <tool_call>-Markup wird in echte OpenAI-tool_calls-Deltas umgeschrieben
(finish_reason -> "tool_calls"); bereits korrekte Antworten laufen unveraendert
durch. Jeder Heal landet in logs/healed.jsonl. Env:
  HEALPROXY_DIR      Log-Verzeichnis (default ~/tool/log-proxy/logs)
  HEALPROXY_REQ_LOG  1 = Request-Bodies als JSONL mitschreiben
  HEALPROXY_RESP_LOG 1 = rohe Antwort-Bytes als resp-N.bin mitschreiben
"""
import http.client
import json
import os
import random
import re
import string
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TARGET = (os.environ.get("HEALPROXY_TARGET_HOST", "127.0.0.1"),
          int(os.environ.get("HEALPROXY_TARGET_PORT", "8080")))
BIND = (os.environ.get("HEALPROXY_BIND", "127.0.0.1"),
        int(os.environ.get("HEALPROXY_PORT", "8085")))
LOGDIR = os.environ.get("HEALPROXY_DIR", os.path.expanduser("~/tool/log-proxy/logs"))
os.makedirs(LOGDIR, exist_ok=True)
LOCK = threading.Lock()
SEQ = [0]
REQ_LOG = os.environ.get("HEALPROXY_REQ_LOG", "0") == "1"
RESP_LOG = os.environ.get("HEALPROXY_RESP_LOG", "0") == "1"

MARK = "<tool_call"
END = "</tool_call>"


def log_heal(rec):
    rec["ts"] = time.strftime("%F %T")
    with LOCK:
        with open(os.path.join(LOGDIR, "healed.jsonl"), "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def new_call_id():
    return "call_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=20))


def convert_value(v):
    v = v.strip("\n")
    try:
        return json.loads(v)
    except Exception:
        pass
    if v == "True":
        return True
    if v == "False":
        return False
    if v == "None":
        return None
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    if re.fullmatch(r"-?\d+\.\d+", v):
        return float(v)
    return v


NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
NAME_SEARCH_RE = re.compile(r"<function=([A-Za-z0-9_.\-]{1,64})\s*>")


def sanitize_tool_names(body_bytes):
    """Ersetzt ungueltige Funktionsnamen in Chat-Completions-Bodies.
    gufo lehnt sonst mit HTTP 400 ab („function names require 1-64 printable
    ASCII ..."); ein einmal in der History gespeicherter Bogus-Name brickt
    damit jeden weiteren Turn (Vorfall 18:13, §8.31). Liefert (bytes, n)."""
    try:
        data = json.loads(body_bytes)
    except Exception:
        return body_bytes, 0
    if not isinstance(data, dict):
        return body_bytes, 0
    fixed = [0]

    def fix(name):
        if isinstance(name, str) and NAME_RE.match(name):
            return name
        fixed[0] += 1
        cleaned = re.sub(r"[^A-Za-z0-9_.\-]", "_", name or "")[:64]
        return cleaned or "tool"

    for msg in (data.get("messages") or []):
        for tc in (msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            if "name" in fn:
                fn["name"] = fix(fn.get("name"))
    for tool in (data.get("tools") or []):
        fn = tool.get("function") or {}
        if "name" in fn:
            fn["name"] = fix(fn.get("name"))
    if not fixed[0]:
        return body_bytes, 0
    log_heal({"kind": "sanitized-tool-names", "count": fixed[0]})
    return json.dumps(data, ensure_ascii=False).encode("utf-8"), fixed[0]


def looks_like_call(text, allow_unclosed=False):
    """Nur strukturiertes Call-Markup heilen. Reine Erwaehnungen von
    <tool_call>/<function=... in Prosa bleiben Content — sonst wird beim
    Stream-Ende legitimer Text verworfen (Vorfall 30.09. ~17:30, §8.30).
    Der Funktionsname muss ein plausibler Tool-Name sein; Platzhalter wie
    „…" duerfen keinen Call erzeugen (Vorfall 18:13, §8.31)."""
    if "<function=" not in text:
        return False
    if not NAME_SEARCH_RE.search(text):
        return False
    if "</function>" in text:
        return True
    return allow_unclosed and "<parameter=" in text


def parse_qwen_call(text, allow_unclosed=False):
    """allow_unclosed: nur fuer den Salvage-Pfad (Stream-Abbruch ohne
    </parameter>-Abschluesse). Dann beendet auch ein folgendes <parameter=
    den Wert (Lookahead) — sonst frisst der erste Parameter alles bis zum
    String-Ende. In geschlossenen Bloecken wuerde der Lookahead legitime
    Werte zerschneiden, die woertlich '<parameter=' enthalten."""
    m = NAME_SEARCH_RE.search(text)
    if not m:
        return None
    tail = r"(?:</parameter>|(?=<parameter=)|$)" if allow_unclosed else r"(?:</parameter>|$)"
    args = {}
    for pm in re.finditer(r"<parameter=([^>]+)>(.*?)" + tail, text, re.S):
        args[pm.group(1).strip()] = convert_value(pm.group(2))
    return {"name": m.group(1), "arguments": json.dumps(args, ensure_ascii=False)}


class HealStream:
    """Erkennt Markup im Content-Stream; haelt nur moegliche Praefixe zurueck."""

    def __init__(self):
        self.pending = ""
        self.healed = 0
        self.next_index = 0

    def _chunk(self, base, delta):
        return {
            "id": base.get("id"),
            "object": "chat.completion.chunk",
            "created": base.get("created"),
            "model": base.get("model"),
            "choices": [{"index": 0, "delta": delta}],
        }

    def content_event(self, base, text):
        return self._chunk(base, {"content": text})

    def toolcall_event(self, base, call, kind):
        ev = self._chunk(base, {"tool_calls": [{
            "index": self.next_index,
            "id": new_call_id(),
            "type": "function",
            "function": call,
        }]})
        self.next_index += 1
        self.healed += 1
        log_heal({"kind": kind, "call": call})
        return ev

    def feed(self, base, text):
        out = []
        self.pending += text
        while True:
            i = self.pending.find(MARK)
            if i == -1:
                hold = 0
                for length in range(min(len(self.pending), len(MARK)), 0, -1):
                    if MARK.startswith(self.pending[-length:]):
                        hold = length
                        break
                cut = len(self.pending) - hold
                if cut > 0:
                    out.append(self.content_event(base, self.pending[:cut]))
                self.pending = self.pending[cut:]
                break
            if i > 0:
                out.append(self.content_event(base, self.pending[:i]))
                self.pending = self.pending[i:]
            # Block-Ende bevorzugt als Struktur-Paar </function>…</tool_call>
            # erkennen — ein einzelnes inneres </tool_call> (z. B. Markup-
            # Beispiele im content-Argument) darf den Block nicht zerschneiden
            # (Vorfall 30.09.: verstuemmelter path wurde ausgefuehrt).
            pair = re.search(r"</function>\s*</tool_call>", self.pending)
            if pair:
                cut = pair.end()
            else:
                j = self.pending.find(END)
                if j == -1:
                    # Kein Ende in Sicht: sieht der Puffer (noch) nicht nach einem
                    # strukturierten Call aus, ist es Prosa — frueh freigeben, statt
                    # bis zum Stream-Ende zu halten.
                    if "<function=" not in self.pending and len(self.pending) > 4096:
                        hold = 0
                        for length in range(min(len(self.pending), len(MARK)), 0, -1):
                            if MARK.startswith(self.pending[-length:]):
                                hold = length
                                break
                        cut = len(self.pending) - hold
                        if cut > 0:
                            out.append(self.content_event(base, self.pending[:cut]))
                        self.pending = self.pending[cut:]
                    break
                cut = j + len(END)
            block = self.pending[:cut]
            self.pending = self.pending[cut:]
            if looks_like_call(block):
                call = parse_qwen_call(block)
                if call:
                    out.append(self.toolcall_event(base, call, "healed"))
                else:
                    # strukturiert aussehend, aber unparsebar: lieber zeigen als verlieren
                    log_heal({"kind": "released-unparsed-call", "block": block[:400]})
                    out.append(self.content_event(base, block))
            else:
                log_heal({"kind": "released-prose", "block": block[:200]})
                out.append(self.content_event(base, block))
        return out

    def finish(self, base_ev, finish_reason):
        out = []
        if self.pending:
            if self.pending.lstrip().startswith(MARK):
                if looks_like_call(self.pending, allow_unclosed=True):
                    call = parse_qwen_call(self.pending, allow_unclosed=True)
                    if call:
                        out.append(self.toolcall_event(base_ev, call, "healed-partial"))
                    else:
                        log_heal({"kind": "released-unparsed-partial",
                                  "text": self.pending[:400]})
                        out.append(self.content_event(base_ev, self.pending))
                else:
                    log_heal({"kind": "released-prose-partial", "text": self.pending[:200]})
                    out.append(self.content_event(base_ev, self.pending))
            else:
                out.append(self.content_event(base_ev, self.pending))
            self.pending = ""
        fin = dict(base_ev)
        fin["choices"] = [{"index": 0, "delta": {},
                          "finish_reason": "tool_calls" if self.healed else (finish_reason or "stop")}]
        out.append(fin)
        return out


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def handle(self):
        # Faengt Resets beim Request-Line-Read (vor jedem Handler) und bei
        # Response-Writes ab — sonst Traceback-Rauschen im Journal bei
        # Client-Aborts (Befund omp-Review, 30.09.).
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _log_request(self, body, n):
        if not REQ_LOG:
            return
        rec = {"seq": n, "ts": time.strftime("%F %T"), "method": self.command,
               "path": self.path, "body_len": len(body)}
        if body:
            try:
                rec["body"] = json.loads(body)
            except Exception:
                rec["body_raw"] = body.decode("utf-8", "replace")[:20000]
        with LOCK:
            with open(os.path.join(LOGDIR, "requests.jsonl"), "a") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def _forward_headers(self):
        return {k: v for k, v in self.headers.items()
                if k.lower() not in ("host", "connection", "content-length",
                                     "transfer-encoding", "accept-encoding")}

    def _proxy(self):
        try:
            body = self._read_body()
        except (ConnectionResetError, BrokenPipeError, OSError):
            return
        with LOCK:
            SEQ[0] += 1
            n = SEQ[0]
        self._log_request(body, n)
        if self.command == "POST" and self.path.endswith("/chat/completions"):
            body, _ = sanitize_tool_names(body)
        conn = http.client.HTTPConnection(*TARGET, timeout=900)
        rfile = None
        try:
            conn.request(self.command, self.path, body=body, headers=self._forward_headers())
            resp = conn.getresponse()
            ctype = resp.getheader("Content-Type") or ""
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() in ("connection", "transfer-encoding",
                                 "content-length", "keep-alive"):
                    continue
                self.send_header(k, v)
            self.send_header("Connection", "close")
            self.end_headers()
            if (self.command == "POST" and "text/event-stream" in ctype
                    and self.path.endswith("/chat/completions")):
                self._stream_healed(resp, n)
            else:
                if RESP_LOG and self.command == "POST":
                    rfile = open(os.path.join(LOGDIR, f"resp-{n}.bin"), "wb")
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    if rfile:
                        rfile.write(chunk)
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            if rfile:
                rfile.close()
            conn.close()

    def _write_event(self, ev):
        self.wfile.write(("data: " + json.dumps(ev, ensure_ascii=False) + "\n\n").encode("utf-8"))
        self.wfile.flush()

    def _stream_healed(self, resp, n):
        healer = HealStream()
        rfile = None
        if RESP_LOG:
            rfile = open(os.path.join(LOGDIR, f"resp-{n}.bin"), "wb")
        while True:
            line = resp.readline()
            if not line:
                break
            if rfile:
                rfile.write(line)
            s = line.decode("utf-8", "replace")
            if not s.startswith("data:"):
                self.wfile.write(line)
                self.wfile.flush()
                continue
            payload = s[5:].strip()
            if payload == "[DONE]" or not payload:
                self.wfile.write(line)
                self.wfile.flush()
                continue
            try:
                ev = json.loads(payload)
            except Exception:
                self.wfile.write(line)
                self.wfile.flush()
                continue
            choices = ev.get("choices") or []
            ch = choices[0] if choices else None
            out = []
            if ch and isinstance(ch.get("delta"), dict):
                d = ch["delta"]
                if isinstance(d.get("content"), str) and d["content"]:
                    out += healer.feed(ev, d["content"])
                if d.get("tool_calls") is not None:
                    out.append(ev)
                if ch.get("finish_reason"):
                    out += healer.finish(ev, ch["finish_reason"])
                if not out and "content" not in d and "tool_calls" not in d:
                    out.append(ev)
            else:
                out.append(ev)
            for e in out:
                self._write_event(e)
        if rfile:
            rfile.close()

    do_GET = _proxy
    do_POST = _proxy


if __name__ == "__main__":
    print(f"healproxy on {BIND[0]}:{BIND[1]} -> {TARGET[0]}:{TARGET[1]} logdir {LOGDIR}", flush=True)
    ThreadingHTTPServer(BIND, Handler).serve_forever()
