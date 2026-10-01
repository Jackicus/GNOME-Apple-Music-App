# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

# The development build is "Music Sleeve (Development)" in the desktop file and the metainfo,
# as GNOME's development builds are, so it is told from a release install beside it. The
# Flatpak manifest adds no suffix of its own.
name_suffix = get_option('profile') == 'development' ? ' (Development)' : ''
data_configuration = {'APP_ID': application_id, 'NAME_SUFFIX': name_suffix}

desktop_file = i18n.merge_file(
  input: configure_file(
    input: base_id + '.desktop.in',
    output: application_id + '.desktop.in',
    configuration: data_configuration,
  ),
  output: application_id + '.desktop',
  type: 'desktop',
  po_dir: '../po',
  install: true,
  install_dir: get_option('datadir') / 'applications',
)

metainfo_file = i18n.merge_file(
  input: configure_file(
    input: base_id + '.metainfo.xml.in',
    output: application_id + '.metainfo.xml.in',
    configuration: data_configuration,
  ),
  output: application_id + '.metainfo.xml',
  po_dir: '../po',
  install: true,
  install_dir: get_option('datadir') / 'metainfo',
)

install_data(
  base_id + '.gschema.xml',
  install_dir: get_option('datadir') / 'glib-2.0' / 'schemas',
)

# The development build gets the striped icon, installed under its own .Devel name.
app_icon = get_option('profile') == 'development' ? base_id + '-devel.svg' : base_id + '.svg'
install_data(
  'icons' / app_icon,
  rename: application_id + '.svg',
  install_dir: get_option('datadir') / 'icons' / 'hicolor' / 'scalable' / 'apps',
)
install_data(
  'icons' / base_id + '-symbolic.svg',
  rename: application_id + '-symbolic.svg',
  install_dir: get_option('datadir') / 'icons' / 'hicolor' / 'symbolic' / 'apps',
)

desktop_file_validate = find_program('desktop-file-validate', required: false)
if desktop_file_validate.found()
  test('validate-desktop', desktop_file_validate, args: [desktop_file])
endif

appstreamcli = find_program('appstreamcli', required: false)
if appstreamcli.found()
  test('validate-metainfo', appstreamcli,
    args: ['validate', '--no-net', '--explain', metainfo_file])
endif

glib_compile_schemas = find_program('glib-compile-schemas', required: false)
if glib_compile_schemas.found()
  test('validate-schema', glib_compile_schemas,
    args: ['--strict', '--dry-run', meson.current_source_dir()])
endif
