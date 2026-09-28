## Summary

<!-- What changes, and why. For a fix, the user-visible effect. -->

## Linked issues

<!-- One line per issue this PR resolves, so GitHub closes them on merge: -->
Closes #

## Checklist

- [ ] `scripts/check.sh` passes locally (and CI is green).
- [ ] UI changes: screenshots attached (`scripts/screenshot.py --demo build/shot.png --page KEY`, dark and `--light`, and `--size 360x640`, the narrowest supported width, for anything adaptive), taken from the demo library.
- [ ] No real account data anywhere: code, tests, fixtures, docs, logs or screenshots.
- [ ] Every new user-visible string goes through `_()` (or `ngettext`/`C_`), and new files with strings are listed in `po/POTFILES.in`.
- [ ] Backend or model changes come with a unit test in `tests/`.
