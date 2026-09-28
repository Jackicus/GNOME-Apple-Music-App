---
name: hig-polish
description: GNOME HIG and polish pass on a page, dialog or widget - screenshots in both colour schemes and at narrow sizes, compared with the GNOME HIG and libadwaita conventions, then fixed. Use when asked to polish something, make it look native or GNOME-like, tidy spacing or states, or check it against the HIG.
argument-hint: "[page key, dialog or widget]"
---

Target: $ARGUMENTS

1. Read CLAUDE.md, `.claude/rules/ui.md` and `.claude/rules/gtk-notes.md`.
2. Shoot the target with the `screenshots` skill: the default size in dark and `--light`, then
   `--size 400x700` and `--size 360x640`, plus the states that matter for it (empty, loading,
   signed out, `--signed-in`, `--now-playing`, `--search`, `--context-menu`, `--preferences`,
   `--dialog`).
3. Look at every PNG and compare it with the GNOME HIG (https://developer.gnome.org/hig/) and
   with how GNOME's own apps (Music, Nautilus, Settings) handle the same thing:
   - header bars: root pages carry a `title-1` in the content and no header title; pushed
     pages show their title in the header bar;
   - spacing in multiples of 6 px, content aligned to one margin;
   - boxed lists, status pages with a title, a description and a button that helps, spinners
     while loading;
   - tooltips on icon-only buttons, mnemonics in dialogs, ellipsizing rather than clipping,
     nothing wider than 360 px;
   - both colour schemes and high contrast, no hard-coded colours;
   - the Apple Music web player's arrangement of the same page (shelves, hero cards, grids;
     "Reference layout" in `docs/history/build-plan.md`), where it does not fight the HIG.
4. Prefer libadwaita widgets and style classes; remove CSS that a style class covers.
5. Fix, shoot again and look again. If focus or keys changed, run `scripts/a11y_check.py`
   (also `--size 360x640`). Run `scripts/check.sh` and commit; describe the before and after in
   the message (the shots stay in build/).
