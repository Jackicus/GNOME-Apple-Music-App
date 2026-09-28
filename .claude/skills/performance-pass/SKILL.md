---
name: performance-pass
description: Measure and improve startup time, page switches, scrolling smoothness or memory on a large invented library, with numbers before and after. Use when something feels slow, stutters or uses too much memory, or to check the performance targets before a release.
argument-hint: "[page or feature]"
---

Target: $ARGUMENTS

1. Read CLAUDE.md, `.claude/rules/performance.md`, and `.claude/rules/library.md` if the model
   is involved.
2. Make the big library if build/demo-big is missing:
   `scripts/demo_library.py --cache build/demo-big --albums 3000 --playlists 300 --tracks 40000`.
   Install the current build (`meson install -C build`, or run `scripts/demo.sh` once).
3. Measure before changing anything, with the measured window visible on screen:
   - `scripts/bench.py --runs 5`: startup marks, page switches with their longest frame, RSS,
     and the pages step (pages opened and closed, and how many stay alive);
     `--profile KEY` for a cProfile of one page's first switch;
   - `APPLE_MUSIC_CACHE=build/demo-big scripts/scroll_test.py --page KEY` (`--page songs
     --distance 40000`, `--sidebar`, `--speed 20000` for a fling);
   - `python3 -X importtime` over the startup imports.
   Write down the numbers and the target they miss.
4. Fix the biggest cost first, one change at a time, measuring after each: work moved off the
   main loop or split with `yield_to_frames()`; sorting and filtering through expressions;
   cheaper binds; artwork asked after the frame and cancelled when recycled; batched splices;
   imports deferred; pages and shelves freed when dropped.
5. Runs vary by 100 ms or more on a desktop with other load: compare medians, and treat a
   difference inside the noise as none.
6. Put before and after in the commit message, and in `docs/notes.md` when the finding will
   matter beyond this change. Run `scripts/check.sh`, check touched pages with the
   `screenshots` skill, and commit.
