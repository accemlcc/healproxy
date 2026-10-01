#!/usr/bin/env python3
"""Client-Abort-Sturm gegen healproxy — beweist, dass der Shim kein Journal-
Rauschen erzeugt (Handler.handle faengt ConnectionResetError & Co., §8.29).

Drei Abbruchklassen, die alle vor bzw. ausserhalb des Handlers landen:
  1. nackter Connect + RST              -> readline der Request-Line
  2. halbe Request-Line + RST           -> readline der Request-Line
  3. Header mit Content-Length, RST     -> Read des Body

Aufruf:  tests/abuse_sockets.py [PORT]
Port-Default: $HEALPROXY_PORT, sonst 8085 (M2; M1 nutzt 8102).
Zaehlt Journal-Zeilen und Tracebacks der Unit vor/nach dem Sturm.
Exit 0 = kein neues Rauschen.
"""
import os
import re
import socket
import subprocess
import sys
import time


NOISE = "Traceback|ConnectionReset|BrokenPipe|ConnectionAborted"
LINGER_ON = b"\x01\x00\x00\x00\x00\x00\x00\x00"


def journal_lines():
    r = subprocess.run(["journalctl", "--user", "-u", "healproxy", "--no-pager", "-q"],
                       capture_output=True, text=True)
    return r.stdout.count("\n") if r.returncode == 0 else -1


def conn(port):
    return socket.create_connection(("127.0.0.1", port), timeout=5)


def kill(sock):
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, LINGER_ON)
    sock.close()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("HEALPROXY_PORT", "8085"))
    before = journal_lines()
    try:                                # Reichweite pruefen — liegt im Messfenster
        kill(conn(port))
    except OSError as exc:
        print("FEHLER: healproxy auf 127.0.0.1:%d nicht erreichbar (%s)" % (port, exc))
        return 2
    n = 1
    for _ in range(3):
        kill(conn(port)); n += 1
    for _ in range(2):
        s = conn(port); s.sendall(b"GET /v1/mod"); kill(s); n += 1
    for _ in range(2):
        s = conn(port)
        s.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Length: 9000\r\n\r\n")
        kill(s); n += 1
    time.sleep(2)
    after = journal_lines()

    delta = after - before if before >= 0 and after >= 0 else -1
    noise = subprocess.run(
        ["journalctl", "--user", "-u", "healproxy", "--no-pager", "-q", "--since", "-40 seconds"],
        capture_output=True, text=True).stdout
    hits = len(re.findall(NOISE, noise))
    print("abgebrochene sockets: %d | journal-delta: %d | noise-treffer: %d" % (n, delta, hits))
    if hits or delta != 0:
        print(noise.strip()[-1500:])
        return 1
    print("ok: keine neuen Tracebacks, Journal still")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())