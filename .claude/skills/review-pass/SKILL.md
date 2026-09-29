---
name: review-pass
description: Review a set of changes (a commit range, a branch, or the working tree) against this project's rules, fix the clear-cut findings and list the rest. Use when asked to review, audit or check changes before a merge or a release.
argument-hint: "[commit range, branch, or nothing for this branch against main]"
---

Review pass over: $ARGUMENTS
(Nothing given: this branch's changes against `main`, or the working tree if there are none.)

1. Read CLAUDE.md, then the `.claude/rules/` file for every area the diff touches. Start from
   `git diff --stat`, then read each changed file whole, not only its hunks.
2. Look for:
   - blocking work on the main loop: `time.sleep`, synchronous sockets or HTTP, `json.load` of
     the library, image decoding, synchronous Gio file calls, outside `asyncio.to_thread`;
   - widgets touched from a thread;
   - a droppable widget (page, shelf, dialog, row) connecting a child's signal to its own bound
     method or declaring `=> $handler()` in its `.blp`, instead of `widgets.util.connect_weak`;
     a new droppable class missing from tests/test_page_lifetime.py;
   - a box of widgets where a list model belongs; costly binds (`_()` in bind, a wrapping
     label, artwork asked in bind, a row that changes size);
   - user-visible strings without `_()`; files missing from `po/POTFILES.in`,
     `src/meson.build` or the gresource;
   - an exception that can reach the user as a traceback instead of a toast; an engine command
     that skips `await self._ready()`, or a caller that checks `engine.state` where the command
     would wait for a start in progress;
   - Chrome or MusicKit reached other than through `app.engine`; anything polling MusicKit;
   - custom CSS where a libadwaita style class exists, CSS outside `src/style.css`, hard-coded
     colours;
   - accessibility: icon-only buttons without a tooltip, recycled rows without an accessible
     label, decorative images not `presentation`, keys missing from `src/shortcuts.py` or
     `scripts/a11y_check.py`;
   - real account data anywhere: code, fixtures, docs, logs, screenshots;
   - comments or docs (CLAUDE.md, `.claude/rules/`, `docs/`) that the change made untrue, and
     comments that narrate history instead of describing the code.
3. Fix what is clear-cut, one logical fix per commit, each with
   `scripts/headless.sh scripts/check.sh` green. For anything visible, check it with the
   `screenshots` skill.
4. Report: what you fixed (commits), what needs a decision (file:line, the options and their
   cost), and what you could not verify.
