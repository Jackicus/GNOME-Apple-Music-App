# An older blueprint-compiler on PATH (as some distributions ship) falls back to the wrap's.
blueprint_compiler = find_program('blueprint-compiler', version: '>= 0.22', required: false)
if not blueprint_compiler.found()
  # Fall back to the wrap. Its configured wrapper can't run uninstalled
  # (it can't find its own module), so run the source tree's script instead.
  subproject('blueprint-compiler')
  blueprint_compiler = find_program(
    meson.project_source_root() / 'subprojects' / 'blueprint-compiler' / 'blueprint-compiler.py',
  )
endif

# One target per .blp, each naming its .ui, so an edited .blp is recompiled: a single target
# with the directory as its output looked up to date whenever anything else had been written
# there. The .ui files land flat in the build directory, which the gresource lists them from.
blueprints = []
foreach blueprint : files(
    'pages/artist.blp',
    'pages/detail.blp',
    'pages/grid.blp',
    'pages/home.blp',
    'pages/radio.blp',
    'pages/search.blp',
    'pages/shelves.blp',
    'pages/songs.blp',
    'dialogs/preferences.blp',
    'dialogs/signin.blp',
    'player_bar.blp',
    'widgets/category_tile.blp',
    'widgets/cover.blp',
    'widgets/hero_tile.blp',
    'widgets/now_playing.blp',
    'widgets/shelf.blp',
    'widgets/song_title.blp',
    'widgets/tile.blp',
    'widgets/track_row.blp',
    'window.blp',
  )
  blueprints += custom_target(
    input: blueprint,
    output: '@BASENAME@.ui',
    command: [blueprint_compiler, 'compile', '--output', '@OUTPUT@', '@INPUT@'],
  )
endforeach

gnome.compile_resources(
  'applemusic',
  'applemusic.gresource.xml',
  gresource_bundle: true,
  install: true,
  install_dir: pkgdatadir,
  dependencies: blueprints,
)

python3 = python.find_installation('python3')

# Where --demo reads its invented library when APPLE_MUSIC_CACHE is unset. The development
# profile points at the source tree's build/demo (scripts/demo_library.py's default output,
# which scripts/demo.sh fills); a release build names no source path, and --demo there reads
# build/demo under the directory it is started from.
demo_dir = ''
if get_option('profile') == 'development'
  demo_dir = meson.project_source_root() / 'build' / 'demo'
endif

configure_file(
  input: 'apple-music.in',
  output: 'apple-music',
  configuration: {
    'PYTHON': python3.full_path(),
    'VERSION': version,
    'APP_ID': application_id,
    'BASE_ID': base_id,
    'PROFILE': get_option('profile'),
    'DEMO_DIR': demo_dir,
    'localedir': get_option('prefix') / get_option('localedir'),
    'pkgdatadir': pkgdatadir,
  },
  install: true,
  install_dir: get_option('bindir'),
  install_mode: 'r-xr-xr-x',
)

install_data(
  '__init__.py',
  'actions.py',
  'engine.py',
  'i18n.py',
  'library.py',
  'lyrics.py',
  'main.py',
  'mpris.py',
  'player.py',
  'player_bar.py',
  'sections.py',
  'shortcuts.py',
  'sidebar.py',
  'sync.py',
  'window.py',
  install_dir: moduledir,
)

install_data(
  'pages/__init__.py',
  'pages/artist.py',
  'pages/detail.py',
  'pages/grid.py',
  'pages/home.py',
  'pages/made_for_you.py',
  'pages/new.py',
  'pages/radio.py',
  'pages/search.py',
  'pages/shelves.py',
  'pages/songs.py',
  install_dir: moduledir / 'pages',
)

install_data(
  'dialogs/__init__.py',
  'dialogs/preferences.py',
  'dialogs/signin.py',
  install_dir: moduledir / 'dialogs',
)

install_data(
  'widgets/__init__.py',
  'widgets/artwork.py',
  'widgets/category_tile.py',
  'widgets/context_menu.py',
  'widgets/cover.py',
  'widgets/hero_tile.py',
  'widgets/lyrics.py',
  'widgets/now_playing.py',
  'widgets/queue.py',
  'widgets/shelf.py',
  'widgets/song_title.py',
  'widgets/tile.py',
  'widgets/track_row.py',
  'widgets/transport.py',
  'widgets/util.py',
  install_dir: moduledir / 'widgets',
)

# The backend package.
install_data(
  'backend/__init__.py',
  'backend/bridge.js',
  'backend/cdp.py',
  'backend/chrome.py',
  'backend/client.py',
  'backend/config.py',
  'backend/errors.py',
  'backend/normalize.py',
  'backend/store.py',
  install_dir: moduledir / 'backend',
)

# Byte-compile the modules once they are installed (after every install_data above): they live
# outside site-packages, so Meson's python.bytecompile never reaches them, and a system install
# without bytecode imports twice as slowly (phase 19: 144 ms against 77 for applemusic.main).
bytecompile = get_option('python.bytecompile')
if bytecompile >= 0
  meson.add_install_script(
    python3,
    meson.project_source_root() / 'build-aux' / 'meson' / 'compile-python.py',
    get_option('datadir') / meson.project_name() / 'applemusic',
    bytecompile.to_string(),
    install_tag: 'runtime',
  )
endif
