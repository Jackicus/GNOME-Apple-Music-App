---
name: fix-bug
description: Reproduce, find the cause of, and fix a bug in the app, with a regression test. Use when the user reports something broken (a crash, a wrong state, a UI glitch, a sync, sign-in or playback problem) or pastes a traceback or log.
argument-hint: "[what happens; what should happen; steps; logs]"
---

Bug report: $ARGUMENTS

1. Read CLAUDE.md and the `.claude/rules/` file for the area the bug is in. If the report does
   not say what should happen or how to get there, ask one question, or state your assumption
   and go on.
2. Reproduce it in the cheapest place that shows it:
   - a unit test with stand-ins (the model, backend, sync, Player, MPRIS, actions and shortcuts
     all have one to copy from in `tests/`);
   - a widget test through `tests/gtk.py`'s `@requires_gtk`;
   - the demo library: `scripts/headless.sh scripts/demo.sh --debug`, or the `screenshots`
     skill (`scripts/headless.sh scripts/screenshot.py --demo …`) to see a
     state;
   - the real engine only when the bug needs Apple's answers or playback, and only after asking:
     follow the `live-engine-check` skill.
3. Find the cause, not the symptom: name the line and why it is wrong. Then look for the same
   pattern elsewhere (the artwork logic has several copies; so do the engine-status and
   command-runner code paths).
4. Write the failing test, fix the code, watch the test pass. A visible fix gets screenshots
   before and after (dark and `--light`; `--size 360x640` if the layout adapts).
5. Run `scripts/headless.sh scripts/check.sh`, fix any line in CLAUDE.md, `.claude/rules/` or
   `docs/` the fix made wrong, and commit with a message that names the cause.
