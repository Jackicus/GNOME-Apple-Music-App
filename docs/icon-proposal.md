# An original app icon: a proposal

A proposal for the owner to decide on; nothing here is installed or wired in.

## Why

The icon in `data/icons/` is a red rounded square with a white double note, which is very close
to Apple Music's own logo, and the display name is exactly "Apple Music". For an app that is not
Apple's, that invites confusion and a trademark complaint (a store or a host may take the app
down for it), and it is the first thing a reviewer of the project notices. The README and the
metainfo already say that the app is not affiliated with Apple; an icon of its own would say it
too.

The name is a separate question. The app ID (`io.github.jackicus.AppleMusic`) is harder to
change than the display name: installed settings, the MPRIS name and the metainfo follow it.
A display name that describes rather than borrows (for example "Music Player for Apple Music"
or a name of its own with "for Apple Music" in the summary) would sit better beside a new icon.

## The concept

![The proposed icon at 128 px](icon-proposal.svg)

`docs/icon-proposal.svg`, a draft at the GNOME app icon size (128 × 128):

- **A record half out of its sleeve.** A physical object, as the GNOME HIG asks of app icons,
  and one that reads as "music" without Apple's note or colours. The sleeve is in front, so the
  silhouette is a rectangle with a circle behind it: distinct from a plain rounded square at
  any size.
- **Base and side.** Both objects stand on the HIG's darker side (the sleeve's lower edge, the
  record's rim below its face), so the icon sits among GNOME's own.
- **GNOME palette colours only.** The sleeve in Purple 1 to 2 (`#dc8add` to `#c061cb`) with a
  Purple 3 to 4 side (`#9141ac` to `#813d9c`); the record in Dark 3 and 4 (`#3d3846`,
  `#241f31`) over a Dark 5 rim; the label in Orange 1 and 4 (`#ffbe6f`, `#e66100`). No Apple
  red, no gradient from pink to red.
- **A level meter on the sleeve.** Five white bars, rounded as GNOME's symbolic icons are: a
  hint of playback, and a shape that survives at 32 px.
- **The symbolic icon** (`-symbolic.svg`, 16 px) would keep only the silhouette: the sleeve's
  outline with the three middle bars, and the record's arc behind it, drawn in the 2 px stroke
  weight the bundled symbolic icons use.

## What changing it would take

- Replace `data/icons/io.github.jackicus.AppleMusic.svg` and its `-symbolic` twin (the file
  names stay: they follow the app ID). A designer's pass on the draft first: the shapes are
  placeholders for proportions and colours.
- A striped variant for the `.Devel` build, as GNOME's development builds have, which waited
  for this decision.
- Check it in the app grid, the About dialog, GNOME Shell's media controls and the Software
  app's listing, in both colour schemes and at 32, 64 and 128 px.
- The metainfo's brand colours (`#ffc2cb` light, `#3d0b17` dark) follow Apple Music's red; a
  purple icon would take purple ones (Purple 1 `#dc8add` light, `#2b1a33` dark, say), and the
  accent colour in `src/style.css` could follow.
