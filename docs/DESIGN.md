# Design notes

## Goals
- Forward a Telegram chat to Discord reliably, including edits and deletions.
- Let a human *watch* what an automated trader would do before it is ever allowed to act.
- Make the safe behaviour the default and the risky behaviour a deliberate, visible switch.

## Key decisions

**1. Dry run is the default, and "off" is spelled one way.**
Only the exact value `OANDA_DRY_RUN=0` enables order placement. A typo, a blank value or a missing variable all leave the bot in dry run. In dry run the client returns fake order ids (`DRY-1`, …) so the rest of the pipeline (logging, tracking, reporting) is exercised exactly as in practice mode.

**2. No live endpoint exists in the code.**
The base URL is a single constant for OANDA's practice host. Making the system capable of live trading would require a code change and review, not a configuration mistake.

**3. All limits live in one file.**
`oanda_signals/rules.py` holds risk per signal, daily cap, max open orders, expiry, margin cap and message-age limit. They were fixed *before* the practice run began so they could not be tuned after seeing results; changing one is a deliberate, dated edit in version control.

**4. Pure logic is separated from I/O.**
`signal_parser.py` and `planner.py` take plain values and return plain values, so they are tested without any network. Network access is concentrated in `client.py` (OANDA), `forwarder.py` (Telegram, Discord) and `signal_bot_hook.py` (glue), and each is tested with fakes.

**5. Every decision is explainable.**
The bot never silently drops a message. Each one becomes a log line: `would_place`, `skip` (with reason), `duplicate`, etc. `docs/sample_decisions.jsonl` shows examples, including the kill switch.

**6. Fail closed, not open.**
- A failed start-up self-test forces dry run.
- A stale message (older than 10 min when first seen) is ignored.
- If the market has already moved beyond the entry, stop or target, the signal is skipped.
- A bot error is reported and swallowed so it can never stop the forwarder.

**7. Retries only where they are safe.**
Reads are retried a bounded number of times on transient statuses. Order-placing writes are not blindly retried, to avoid duplicate orders.

## Order of checks for each new signal
1. Parse (`signal_parser.py`) → valid signal, or skip with reason.
2. Duplicate? (same normalised signal already seen) → skip.
3. Kill switch file present? → skip.
4. Plan (`planner.py`) → entry / stop / target / units, or skip with reason (price already beyond levels, size too small, margin cap).
5. Daily risk cap and max-open checks (`bot.py`) → skip if exceeded.
6. `client.place_limit` → real practice order **or** fake `DRY-n` id in dry run.
7. Append the outcome to `decisions.jsonl`.

## What is deliberately not here
No live trading, no strategy optimisation, no performance claims, no real signals or chat identifiers, no credentials.
