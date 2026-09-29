# TODO

What is left before the first release, as of 2026-09-29. The app is feature-complete for 0.9.0:
all 20 build phases and the 13 work packages of the pre-release audit are merged, CI is green,
and every audit issue is closed except the ones below, which need the maintainer. The audit's
plan, results and live-verification summary are on the tracking issue
[#3](https://github.com/Jackicus/GNOME-Apple-Music-App/issues/3).

## 1. Sign in again (2 minutes)

The development build's Chrome profile lost its Apple Music sign-in during the automated live
check (Chrome was started where it couldn't reach the desktop keyring; the guard in
[#176](https://github.com/Jackicus/GNOME-Apple-Music-App/issues/176) now prevents that).

- [ ] `scripts/run.sh`, click **Sign In**, sign in in the Chrome window that opens.
- [ ] If you plan to use a system install (`meson setup _build --prefix=/usr`, see README),
      sign that one in too: the release build has its own Chrome profile, cache and sign-in,
      separate from the development build's.

## 2. Account checks (after signing in)

The checks that need a signed-in account couldn't run automatically. They're listed in
[#154](https://github.com/Jackicus/GNOME-Apple-Music-App/issues/154). Either tick them off while
using the app, or start a Claude Code session in the repo and ask it to run the live account
checks: the `live-engine-check` skill and #154's comments have the steps, starting with a live
confirmation of the keyring guard.

- [ ] Keyring guard confirmed live (#154, last comment).
- [ ] A full library sync, library playback (including a disc-2 track), lyrics timing.
- [ ] Favourite then un-favourite one song; big artists and playlists show everything.
- [ ] Search in Apple Music mode, a category, New and Made for You.

## 3. Checks only you can do

Also in #154. These need real input, your eyes, or change the account:

- [ ] GNOME Shell's media card: name, icon, artwork, no blink between songs, media keys.
- [ ] Keyboard in a real window: Down across sections in the narrow sidebar; the sidebar's
      context menu on a folder closes cleanly.
- [ ] Drag a track onto a sidebar playlist (and hold it over a folder to open it); Add to
      Playlist; Add to Library.
- [ ] Sign Out end to end (the Apple session is revoked); sign out during a sync; quit during
      sign-out; Clear Cache in Preferences.

## 4. Decisions

Nine issues labelled `needs-decision`. Seven already have a working default that is easy to flip:
if you're happy with it, close the issue. My recommendation is in bold.

- [ ] [#145](https://github.com/Jackicus/GNOME-Apple-Music-App/issues/145) **The icon and name
      resemble Apple Music's (trademark risk).** The icon is close to Apple's logo and the
      display name is exactly "Apple Music". Options: keep both; a new original icon
      (`docs/icon-proposal.md` has a concept and a draft SVG); or a new icon and a distinct
      name. **Replace the icon before any public listing, and settle the name before tagging:
      nobody uses the app yet, so even the app ID could change now at no cost to anyone.**
- [ ] [#146](https://github.com/Jackicus/GNOME-Apple-Music-App/issues/146) **When to tag v0.9.0
      and publish to the AUR.** **After sections 1–3 pass and #145 is settled.** Steps are in
      section 5.
- [ ] [#147](https://github.com/Jackicus/GNOME-Apple-Music-App/issues/147) **Revoke the Apple
      session on sign-out even when the engine is stopped** (it starts Chrome headless for up
      to 20 s to revoke, then wipes). Alternative: skip revocation when stopped (instant, but
      the token stays valid at Apple until it expires). **Keep it: a sign-out should really
      sign out.**
- [ ] [#148](https://github.com/Jackicus/GNOME-Apple-Music-App/issues/148) **Music videos play as
      audio** in the headless engine. Alternative: a toast with Open in Browser. **Keep it for
      0.9.0 (it's listed as a limitation); an "Open in Browser" menu item can come later.**
- [ ] [#149](https://github.com/Jackicus/GNOME-Apple-Music-App/issues/149) **Space presses a
      focused button; Now Playing is Ctrl+Shift+N** (Ctrl+N is the HIG's "New"). **Keep it:
      it's what GTK and the HIG expect, and it fixes buttons for screen reader users.** Already
      in the release notes.
- [ ] [#150](https://github.com/Jackicus/GNOME-Apple-Music-App/issues/150) **MPRIS Stop pauses
      and rewinds** instead of ending the session (the Shell keeps the player; Play starts the
      song again). **Keep it: it's what the MPRIS spec asks.**
- [ ] [#151](https://github.com/Jackicus/GNOME-Apple-Music-App/issues/151) **The development
      build keeps its own cache and sign-in.** Alternative: share the release build's (starts
      with the library, but the two overwrite each other). **Keep it.**
- [ ] [#152](https://github.com/Jackicus/GNOME-Apple-Music-App/issues/152) **library.json
      version 2 forces one full sync after the upgrade** (needed for the multi-disc fix).
      **Keep it.**
- [ ] [#153](https://github.com/Jackicus/GNOME-Apple-Music-App/issues/153) **A lazy Songs model
      after 0.9** (about 21 MB less memory on a large library; changes the Songs and Search
      models). **Leave it for after 0.9.0, and only if memory matters on a real library.**

## 5. Release (when #146 says go)

From `docs/release.md`:

- [ ] Update the `<release version="0.9.0" date="…">` notes in
      `data/io.github.jackicus.AppleMusic.metainfo.xml.in` and pin the screenshot URLs to the tag.
- [ ] `git tag -a v0.9.0 -m 'Apple Music for GNOME 0.9.0' && git push origin v0.9.0`
- [ ] In `build-aux/aur/`: set the `# Maintainer:` line's contact to the one you want public,
      `updpkgsums && makepkg --printsrcinfo > .SRCINFO`, `makepkg -si` to try it, then publish
      to the AUR.
- [ ] Optional: a GitHub release with `meson dist -C build`'s tarball.

## 6. Later, optional

- [ ] Build and run the Flatpak in GNOME Builder (the manifest in `build-aux/flatpak/` has never
      been built: no GNOME 50 runtime on the development machine).
- [ ] [#169](https://github.com/Jackicus/GNOME-Apple-Music-App/issues/169): real key presses in
      the keyboard check through mutter's RemoteDesktop API.
- [ ] [#180](https://github.com/Jackicus/GNOME-Apple-Music-App/issues/180): `tests.test_tiles`
      crashes GTK when run on its own (it passes in the full suite and in CI).
- [ ] Translations: the strings are ready (`po/`), no languages yet.

## Working on it

Every change goes through a pull request with CI. `CLAUDE.md` and `.claude/` hold the rules for
Claude Code sessions (area rules load as files are touched; skills: `fix-bug`, `hig-polish`,
`performance-pass`, `review-pass`, `screenshots`, `live-engine-check`). Run GUI scripts through
`scripts/headless.sh` so no window opens on the desktop. Delete this file once it's all done.
