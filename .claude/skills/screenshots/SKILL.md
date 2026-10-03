---
name: screenshots
description: Take and check screenshots of the real app window on the invented demo library - to verify a visible change, to attach to a pull request, or to retake the metainfo screenshots in data/screenshots/. Use whenever a change is visible or screenshots are asked for.
argument-hint: "[page key or item, e.g. albums, album:first, now-playing]"
---

What to shoot: $ARGUMENTS

1. The shots come from the installed build, so install the current code first:
   `meson install -C build` (after `scripts/headless.sh scripts/check.sh`, which configures
   and compiles build/).
2. Always run the script through `scripts/headless.sh` (a private, invisible display: no window
   on the desktop) and always pass `--demo` (without it the script reads the real cache). Write
   into build/, which git ignores:
   - `scripts/headless.sh scripts/screenshot.py build/<name>.png --demo --page KEY`: a root
     page (a destination key from `src/sections.py`, `playlist:ID`, `folder:ID` or
     `folder:first`);
   - `--open KIND:first` (or `KIND:ID`; KIND `album`, `artist`, `playlist`, `folder`,
     `station` or `video`): a pushed page over `--page`;
   - `--playing`: an invented item playing (the first album's first track), the player bar in
     its playing state; `--now-playing [lyrics|queue]`: the same with the Now Playing sheet
     open on that tab;
   - `--signed-in [NAME]`: the account button signed in (an invented NAME: its avatar's colour
     follows the name); `--banner sign-in|expired`: the sign-in banner;
   - `--sidebar` (the narrow layout: the sidebar rather than the page), `--expand first`,
     `--search TERM`, `--context-menu`, `--preferences [general|engine]`,
     `--dialog about|shortcuts`; `--scroll PX` for the rest of a page taller than the
     window (an artist's: `--open artist:first --scroll 900`);
   - the playlists' menus and dialogs: `--more-options` (the page's More Options menu),
     `--sidebar-menu playlist:first|folder:first|all-playlists`, and
     `--dialog new-playlist|rename|delete` for the playlist or folder the page shows.
   Dark is the default; add `--light`. Sizes: the default (1100x760), `--size 400x700`, and
   `--size 360x640`, the narrowest supported. The script waits for the artwork to decode
   before it shoots.
3. Look at every PNG with the Read tool. Check that the change is there; that nothing is
   clipped, overlapping, or ellipsized where it should fit; both schemes; focus and selection;
   covers rather than placeholders (retake a shot that shows placeholders). A harmless at-spi
   warning in the script's output is expected.
4. Shots of work in progress stay in build/. Never commit or share a shot taken without
   `--demo`.
5. Retaking the metainfo screenshots (`data/screenshots/`, 1100x760 at scale 1), each as
   `<name>-dark.png` and, with `--light`, `<name>-light.png`, all with
   `--signed-in 'Alex Rivera'` (invented):
   - `home`: `--page home --playing`;
   - `albums`: `--page albums --playing`;
   - `album`: `--page albums --open album:first --playing` (the album playing);
   - `now-playing`: `--page albums --now-playing`.
   Keep the names: installed metainfo files link them by URL. Shrink them losslessly with
   `oxipng --opt 4 --strip safe data/screenshots/*.png`,
   look at each one (demo data only), and check that none carries a text chunk:
   `grep -c -a -E 'tEXt|iTXt|zTXt' data/screenshots/*.png` prints 0 for each. Update the
   captions in the metainfo if what a shot shows changed.
