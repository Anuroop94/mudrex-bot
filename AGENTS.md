# Live-trading agent contract

These rules apply to every coding agent, including Claude and Codex.

1. Create/verify `STOP` before editing, testing, reviewing, or restarting runtime code. Never remove `STOP`, enable live trading, start a scheduler, or place an exchange order. Only the owner may resume.
2. `trade_policy.py` is the authoritative owner-policy contract. Do not reinterpret its caps, cycle, bracket, notification, or trade-count rules in prompts.
3. Never read, print, copy, modify, or commit `.env`, API secrets, Telegram identifiers, runtime databases, logs, heartbeat files, or live state files.
4. Tests may use only `FakeMudrex`/localhost and must set `MUDREX_TEST_MODE=1`. Test files may explicitly unlock an in-process migration constant, but no environment variable may bypass a production safety gate. A test must never contact the live Mudrex or Telegram APIs.
5. Keep one task per change. Execution-safety fixes, strategy research, dashboard work, and production operations require separate commits and reviews.
6. Do not activate `AUTONOMOUS_HEDGE_READY` until all of these are complete: LONG and SHORT bracket orders are side-aware; every entry has verified stop-loss and take-profit; 24-hour set accounting, approval above three, and Rs500 P&L stops are enforced; Telegram delivery is journaled/retried; fake-exchange and adversarial tests pass.
7. Learning may rank or veto candidate trades. It may never raise leverage, widen loss limits, remove a stop/target, force a minimum trade count, edit its own policy, or deploy its own code.
8. Before handoff, show the owner the diff, test results, unresolved assumptions, and exact manual resume steps. Do not commit unless the owner asks.
9. When callable-agent orchestration is available, route routine discovery, test triage, and documentation to the lowest-cost capable model. Reserve high-reasoning models for live-trading safety, architecture, and unresolved failures. Claude, Freebuff, and FreeLLMAPI may be used only through an installed, auditable CLI/MCP/API bridge; never invent availability or expose credentials.
