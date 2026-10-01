# healproxy

A ~400 line reverse proxy that sits between an agent (or any OpenAI client)
and an OpenAI-compatible inference server, and repairs two failure modes we
hit running a Qwen-family coding model 24/7 for weeks.

Both are workarounds for model/server behaviour, not features. The proxy is a
deliberately thin shim: it only rewrites what is provably broken, leaves
everything else byte-for-byte alone, and logs every single rewrite to
`logs/healed.jsonl` so the log stays an honest audit trail.

## Failure mode 1 — the model leaks its own tool-call framing into `content`

Instead of structured `tool_calls`, the assistant message arrives as prose
containing the raw call framing (a `tool_call` block wrapping `function=NAME`
and `parameter=KEY` entries). Every affected turn then *describes* the action
instead of performing it, and `finish_reason` stays `stop`.

healproxy watches the SSE stream and:

- only ever buffers a suffix that could still become the start of the framing
  marker, so clean prose streams through with no latency penalty;
- treats the `function` close plus `tool_call` close as the block terminator,
  so a stray inner close **inside an argument value** does not cut the block in
  half (this exact bug once produced a `write` call with a mangled `path` that
  the client then executed);
- converts a complete block into real `tool_calls` deltas and sets
  `finish_reason: tool_calls`;
- leaves text that merely *mentions* the tokens alone, and releases a buffer
  that does not look like a structured call instead of swallowing it (an
  earlier build silently discarded 71 characters of a final report this way);
- coerces Python literals (`True` / `False` / `None`) and numeric-looking
  values, and synthesises call ids and monotonic `index` values;
- forwards `usage` chunks, reasoning deltas and genuine `tool_calls` untouched.

## Failure mode 2 — one bad function name bricks the whole session

Once a tool call whose `function.name` is not a valid name (we got the
ellipsis character, `…`) is stored in the message history, the backend rejects
**every** later request with

    400 function names require 1-64 printable ASCII ...   (invalid_messages)

and there is no server-side way out: the conversation is dead, along with the
long prefix cache behind it.

Two defences:

- the healer only accepts plausible names (`[A-Za-z0-9_.-]{1,64}`), so prose
  like `function=…` in an explanatory sentence stays prose;
- `sanitize_tool_names()` rewrites invalid names in outgoing
  `messages[].tool_calls[].function.name` and `tools[].function.name`
  (`…` becomes `_`, empty becomes `tool`, over-long is truncated to 64), which
  revives already-poisoned sessions without editing session files. Clean
  bodies are returned byte-identical and are not re-serialised
  (measured: 0.34 ms for a 458 KiB body, 1.38 ms when it has to rewrite).

## Quick start

    python3 healproxy.py                  # defaults: 127.0.0.1:8085 -> 127.0.0.1:8080

Then point the client at `http://127.0.0.1:8085/v1`. See
`healproxy.service.example` for a systemd user unit.

| Env | Default | Meaning |
| --- | --- | --- |
| `HEALPROXY_BIND` | `127.0.0.1` | bind address |
| `HEALPROXY_PORT` | `8085` | listen port |
| `HEALPROXY_TARGET_HOST` | `127.0.0.1` | backend host |
| `HEALPROXY_TARGET_PORT` | `8080` | backend port |
| `HEALPROXY_DIR` | `~/tool/log-proxy/logs` | heal log directory |
| `HEALPROXY_REQ_LOG` | `0` | `1` appends request bodies to `requests.jsonl` |
| `HEALPROXY_RESP_LOG` | `0` | `1` writes raw response bytes to `resp-N.bin` |

The last two contain prompts and model output. Keep them off unless you are
deliberately debugging a leak.

Only streaming `POST .../chat/completions` responses are parsed; everything
else is proxied verbatim.

## Tests

    tests/test_healproxy.py        # 35 cases, offline: no model, no port, no GPU
    tests/abuse_sockets.py 8085    # live: 8 aborted sockets, asserts a silent journal

The suite exercises the healer through the real `Handler._stream_healed` path
as well as directly, covering: a leak chunked into 7-byte pieces, HTML-ish
content that must stay untouched, salvage of a truncated stream, a literal
`parameter=` inside a quoted value, prose that mentions the framing tokens, a
nested inner close inside an argument value, two calls in one stream, `usage`
passthrough, the placeholder-name cases, all sanitizer shapes, and five cases
that pin currently *known* limitations so a future change cannot silently
regress them. Exit code is non-zero on any failure.

`abuse_sockets.py` covers the other direction: clients that reset mid-request
(naked connect, half a request line, headers with `Content-Length` and no
body). Without `Handler.handle()` catching `ConnectionResetError` these dump a
~12-line traceback each into the journal.

## Verified against

`gufo` 0.2.0 serving a Qwen3.8-Flash-Next GGUF (with MTP speculative decoding)
on a Strix Halo box; clients are an agent runtime and a Telegram bridge. The
proxy itself is model-agnostic and only depends on OpenAI-compatible SSE.

## Known limitations

Pinned by the `I*` tests rather than fixed:

- `tool_choice.function.name` is **not** sanitized; an invalid name there
  still yields a 400.
- `NAME_RE` is stricter than the backend's "printable ASCII" rule: a legal name
  containing a colon or a space gets rewritten, and the client can then no
  longer resolve the call. Harmless for `snake_case` tool sets, worth knowing
  for MCP-style `namespace:tool` names.
- If the stream is cut in the middle of a name but the `>` already got through,
  the salvage path heals a plausible-but-wrong tool name (e.g. `ev`) with empty
  arguments. One lost turn, no session damage.
- Value coercion eats leading zeros (`"0012"` becomes `12`).

Comments in `healproxy.py` reference an internal ops log by section number
(`§8.30` and friends); those documents are not published, so treat them as
incident labels.

## Not for

Anything that needs an authoritative audit of what the model actually emitted:
a healed call is a reconstruction. Read `logs/healed.jsonl` — the kinds are
`healed`, `healed-partial`, `released-prose`, `released-unparsed-call`,
`sanitized-tool-names` — and prefer fixing the server prompt/template when you
can.

## License

MIT — see `LICENSE`.
