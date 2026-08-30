---
name: automate-feishu-desktop-e2e
description: Drive a fixed, already logged-in Windows Feishu desktop chat from WSL for a real client-originated ingress/reply test, then verify platform persistence, Hermes/FIN admission, reply content, and desktop display. Use when API injection or self-tests are too synthetic; not for arbitrary chat automation, bulk messaging, or bypassing confirmation gates.
---

# Real Feishu Desktop E2E

Use the logged-in Windows desktop client as the test driver. Keep the procedure
harness-neutral: the same steps and evidence rules apply whether Claude Code,
Codex, or another agent runs them.

This test is deliberately narrow. It proves the real user boundary without
turning desktop automation into a general message sender.

## Freeze the test before touching the client

- Fix one user-authorized chat and one expected product behavior.
- Generate one fresh ASCII nonce for transport checks. For a product question,
  include that nonce in the question so the request and reply are correlatable.
- Keep only one test message in flight. Do not send the next turn until the
  current turn has a correlated reply or a bounded timeout has been recorded.
  A late reply belongs to its nonce and platform timestamp, not to whichever
  message is currently visible at the bottom of the chat.
- Query official Feishu history first and require zero exact matches for the
  outbound text or nonce. This gives the test a red-capable starting point.
- State the maximum permitted side effect. Read-only questions may be sent.
  Never send a confirmation, order, portfolio write, or other consequential
  follow-up unless the user authorized that exact action for that exact preview.
- Do not use `send_as_user`, bot send, HTTP ingress, private adapter injection,
  or direct database writes as a substitute for the desktop-originated step.

## Guard the Windows/WSL boundary

Do not assume inherited `WSL_INTEROP` is usable. Find working interop sockets
with a bounded harmless command:

```bash
for socket_path in /run/WSL/*_interop; do
  timeout 3 env WSL_INTEROP="$socket_path" \
    /mnt/c/Windows/System32/cmd.exe /d /c echo OK >/dev/null 2>&1 \
    && printf '%s\n' "$socket_path"
done
```

Several sockets may work. Choose one for this run and pass it explicitly to
each Windows command. If no socket works, report an interop blocker; restarting
Feishu or changing the product does not repair WSL interop.

Use Windows UI Automation against the Feishu process. Before composing, require:

- exactly one visible Feishu process with a non-zero `MainWindowHandle`;
- one visible, enabled, keyboard-focusable composer whose class contains
  `editor-kit-container`;
- one unambiguous target-chat condition based on an exact known marker plus
  control class/bounds, not marker count alone.

A chat marker may appear in both the header and echoed conversation content.
Abort if class, bounds, and the known marker cannot uniquely identify the target.
Do not fall back to screen coordinates or whichever Feishu window happens to
be foreground.

## Compose without leaking data

Activate the guarded PID, focus the guarded composer, and verify the focused
element still belongs to it.

Before entering text, require that the composer does not contain a prior draft.
After paste or key entry, require one exact full draft, including spaces between
a slash command and its argument. This prevents a fast second input from being
concatenated onto `/new` or another still-pending command.

Feishu's `editor-kit-container` often has no ValuePattern; its TextPattern
`DocumentRange` returns internal accessible text (a hint/label, observed
~12–26 chars, may contain “发送”) even when the composer is empty. Never treat
TextPattern document text as a user draft, and do not let a read-only diagnose
block on it. The only authoritative prior-draft check is the sentinel
select-all+copy gate during send (see below): save the clipboard object, set a
sentinel value, select-all + copy, and abort — leaving the draft untouched —
when the copied text differs from the sentinel and is non-whitespace. A
non-empty ValuePattern is the only read-only signal that counts as an unknown
draft.

For a short ASCII nonce, slow key entry is acceptable. For Chinese or a longer
question, use the Windows clipboard safely:

1. Save `Clipboard.GetDataObject()` in process memory without inspecting or
   printing its contents.
2. Set only the task text, paste with Ctrl+V, and wait for Feishu to render it.
3. Require exactly one exact draft inside the guarded composer.
4. Restore the previous clipboard object in `finally`, including on failure.

Never log clipboard contents, accessibility-tree text, screenshots, chat IDs,
credentials, raw message IDs, or unrelated conversation content.

## Send through the semantic send control

User-facing Feishu may advertise Enter as the send shortcut, but synthetic
`SendKeys.SendWait("{ENTER}")` or Ctrl+Enter can insert a newline or leave the
draft untouched. A successful key-call return is not proof of sending.

Prefer the Feishu UI Automation button that is visible, enabled, unique within
the guarded window, and whose class contains `send-button-container`. Require
`InvokePattern`, invoke it once, and then let downstream evidence decide whether
the send happened. Do not invoke a button selected only by position or label.

After the invocation, wait briefly and check official history. Also inspect only
the exact test text's relationship to the composer bounds when needed:

- exact text outside the composer can support `client_displayed`;
- exact text still inside the composer proves an unsent draft;
- ambiguous placement is `unknown`.

Never retry merely because PowerShell timed out, returned no data, or the UI
still looks busy. Retry only when official history has zero exact matches and UI
Automation proves the exact text remains in the guarded composer. Otherwise stop
to avoid duplicates.

If synchronous PowerShell loses its WSL return channel, a short hidden Windows
PowerShell child with UTF-16LE `-EncodedCommand` is acceptable. Keep all guards
inside that child. Its exit status is diagnostic only; official history remains
the send authority. Do not create a relay, listener, portproxy, browser-debug
port, scheduled task, or persistent helper.

## Verify the real chain

Use existing app credentials from an owner-only profile only in memory. Never
print or persist app ID, secret, access token, chat ID, user ID, or raw message
ID. Query the official `im.v1.message.list` history for the fixed chat:

1. Find the exact outbound text with `sender_type=user` and a non-empty message
   ID. Store only a hash of the ID in evidence.
2. Record its platform `create_time`.
3. Poll for a later app message in the same chat.
4. Correlate the reply by time, chat, nonce or expected question semantics; do
   not accept an unrelated later bot message.
5. Parse the message body and assert only expected field labels or values needed
   by the test. Do not dump the full reply when it contains portfolio or personal
   data.
6. Confirm the desktop accessibility tree displays the correlated reply using
   exact expected markers. Do not dump the tree.

For portfolio preview regressions, useful non-sensitive assertions include
`持仓预览`, `总资产:`, `可用资金:`, `成本价`, `现价`, `市值`, `可卖`, and
`确认更新持仓`. Assert recognized values in memory when required, but redact
them from evidence. Stop after the preview by default; do not send
`确认更新持仓`.

Feishu app replies are usually `msg_type=post`, not `text`: the body content is
JSON with nested paragraph/inline lists, and a flat `.text` lookup returns an
empty string. Parse the post content recursively (collect every `text` leaf
under `content`/`content_v2`) before correlating or asserting display markers.
Pass only a bounded leading window of the reply text to desktop UIA checks so
the Windows PowerShell command line stays short.

Keep these evidence levels separate:

1. `client_displayed`: the desktop shows the exact outbound message outside the
   composer.
2. `platform_persisted`: official history contains it as a real user message.
3. `hermes_admitted`: a deployed process-local handler, transcript, or FIN
   public-entry signal correlates it to the active instance.
4. `reply_correlated`: official history or delivery evidence contains a later,
   matching app reply.
5. `reply_displayed`: the desktop displays that correlated reply.

Report only the highest proven level and list lower levels actually checked.
WebSocket `connected`, an active PID, plugin discovery, `/new`, a successful UI
invoke, or a self-test does not prove admission or reply.

A missing SQLite row or null `platform_message_id` can be path-dependent and is
`unknown`, not proof of rejection. Prefer the earliest stable signal actually
owned by the deployed path. If `platform_persisted` is green but there is no
admission or reply, stop changing the desktop driver and investigate Feishu
event delivery, app identity/subscription, the WebSocket consumer, or Hermes.
After event-subscription, permission, or developer-console changes, republish
the Feishu app version; a connected WebSocket alone does not activate changes.

## Finish cleanly

- Leave no draft, changed clipboard, background PowerShell process, temporary
  listener, portproxy, scheduled task, token, or task-owned temp file.
- Keep evidence owner-only and limited to timestamps, hashes, deployed identity,
  assertion names, and pass/fail states.
- Record whether a product write was deliberately not confirmed.
- Call the E2E complete only when the required evidence level is named, expected
  reply facts pass, duplicate count is one, and cleanup is green.
