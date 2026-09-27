blueprint_compiler = find_program('blueprint-compiler', required: false)
if not blueprint_compiler.found()
  # Fall back to the wrap. Its configured wrapper can't run uninstalled
  # (it can't find its own module), so run the source tree's script instead.
  subproject('blueprint-compiler')
  blueprint_compiler = find_program(
    meson.project_source_root() / 'subprojects' / 'blueprint-compiler' / 'blueprint-compiler.py',
  )
endif

blueprints = custom_target(
  'blueprints',
  input: files(
    'pages/artist.blp',
    'pages/detail.blp',
    'pages/grid.blp',
    'pages/home.blp',
    'pages/radio.blp',
    'pages/songs.blp',
    'player_bar.blp',
    'widgets/cover.blp',
    'widgets/hero_tile.blp',
    'widgets/shelf.blp',
    'widgets/song_title.blp',
    'widgets/tile.blp',
    'widgets/track_row.blp',
    'window.blp',
  ),
  output: '.',
  command: [
    blueprint_compiler,
    'batch-compile',
    '@OUTPUT@',
    '@CURRENT_SOURCE_DIR@',
    '@INPUT@',
  ],
)

gnome.compile_resources(
  'applemusic',
  'applemusic.gresource.xml',
  gresource_bundle: true,
  install: true,
  install_dir: pkgdatadir,
  dependencies: blueprints,
)

configure_file(
  input: 'apple-music.in',
  output: 'apple-music',
  configuration: {
    'PYTHON': python.find_installation('python3').full_path(),
    'VERSION': version,
    'APP_ID': application_id,
    'BASE_ID': base_id,
    'PROFILE': get_option('profile'),
    # Where --demo looks: scripts/demo_library.py's default output.
    'DEMO_DIR': meson.project_source_root() / 'build' / 'demo',
    'localedir': get_option('prefix') / get_option('localedir'),
    'pkgdatadir': pkgdatadir,
  },
  install: true,
  install_dir: get_option('bindir'),
  install_mode: 'r-xr-xr-x',
)

install_data(
  '__init__.py',
  'library.py',
  'main.py',
  'player_bar.py',
  'sections.py',
  'sidebar.py',
  'window.py',
  install_dir: moduledir,
)

install_data(
  'pages/__init__.py',
  'pages/artist.py',
  'pages/detail.py',
  'pages/grid.py',
  'pages/home.py',
  'pages/radio.py',
  'pages/songs.py',
  install_dir: moduledir / 'pages',
)

install_data(
  'widgets/__init__.py',
  'widgets/artwork.py',
  'widgets/cover.py',
  'widgets/hero_tile.py',
  'widgets/shelf.py',
  'widgets/song_title.py',
  'widgets/tile.py',
  'widgets/track_row.py',
  install_dir: moduledir / 'widgets',
)

# The backend package. am.py stays out: it is a reference for porting, never run or imported.
install_data(
  'backend/__init__.py',
  'backend/bridge.js',
  'backend/cdp.py',
  'backend/config.py',
  'backend/sync.py',
  install_dir: moduledir / 'backend',
)
