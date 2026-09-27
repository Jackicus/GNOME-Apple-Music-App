project(
  'apple-music',
  version: '0.1.0',
  meson_version: '>= 1.1.0',
  license: 'GPL-2.0-or-later',
  default_options: ['warning_level=2', 'werror=false'],
)

i18n = import('i18n')
gnome = import('gnome')
python = import('python')

base_id = 'io.github.jackicus.AppleMusic'
resource_path = '/io/github/jackicus/AppleMusic'

if get_option('profile') == 'development'
  application_id = base_id + '.Devel'
  git_rev = run_command('git', 'rev-parse', '--short', 'HEAD', check: false).stdout().strip()
  version = meson.project_version() + (git_rev == '' ? '-devel' : '-' + git_rev)
else
  application_id = base_id
  version = meson.project_version()
endif

dependency('gtk4', version: '>= 4.20')
dependency('libadwaita-1', version: '>= 1.9')
dependency('pygobject-3.0', version: '>= 3.50')

pkgdatadir = get_option('prefix') / get_option('datadir') / meson.project_name()
moduledir = pkgdatadir / 'applemusic'

subdir('data')
subdir('src')
subdir('po')

gnome.post_install(
  glib_compile_schemas: true,
  gtk_update_icon_cache: true,
  update_desktop_database: true,
)
