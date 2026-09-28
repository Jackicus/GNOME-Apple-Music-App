---
name: screenshots
description: Take and check screenshots of the real app window on the invented demo library - to verify a visible change, to attach to a pull request, or to retake the metainfo screenshots in data/screenshots/. Use whenever a change is visible or screenshots are asked for.
argument-hint: "[page key or item, e.g. albums, album:first, now-playing]"
---

What to shoot: $ARGUMENTS

1. The shots come from the installed build, so install the current code first: run
   `scripts/demo.sh` once (it builds, installs into build/install, makes build/demo if it is
   missing, and starts the app; close it) or `meson install -C build`.
2. Always pass `--demo`: without it the script reads the real cache. Write into build/, which
   git ignores:
   - `scripts/screenshot.py build/<name>.png --demo --page KEY`: a root page (a destination key
     from `src/sections.py`, `playlist:ID` or `folder:ID`);
   - `--open album:first` (or `artist`, `playlist`, `station`, `video`; `folder:ID` with a real
     id): a pushed page over `--page`;
   - `--now-playing` (`lyrics` or `queue`) with `--page albums`: an invented item playing, the
     sheet open;
   - `--search TERM`, `--context-menu`, `--expand first`, `--signed-in [NAME]`,
     `--preferences [general|engine]`, `--dialog about|shortcuts`.
   Dark is the default; add `--light`. Sizes: the default, `--size 400x700`, and
   `--size 360x640`, the narrowest supported.
3. Look at every PNG with the Read tool. Check that the change is there; that nothing is
   clipped, overlapping, or ellipsized where it should fit; both schemes; focus and selection.
   A harmless at-spi warning in the script's output is expected.
4. Shots of work in progress stay in build/. Never commit or share a shot taken without
   `--demo`.
5. Retaking the metainfo screenshots (`data/screenshots/`, 1100x760 at scale 1, dark and
   light): `home` (`--page home`), `albums` (`--page albums`), `album` (`--page albums --open
   album:first`) and `now-playing` (`--page albums --now-playing`), each once plain and once
   with `--light`, saved as `<name>-dark.png` and `<name>-light.png`. Keep the names: installed
   metainfo files link them by URL. Shrink them losslessly if an optimiser such as oxipng is at
   hand, look at each one (demo data only), and check that none carries a text chunk:
   `grep -c -a -E 'tEXt|iTXt|zTXt' data/screenshots/*.png` prints 0 for each.
