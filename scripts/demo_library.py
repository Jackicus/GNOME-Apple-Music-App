#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""A made-up Apple Music library for demo and screenshots:
`demo_library.py [--cache DIR] [--albums N] [--playlists N] [--tracks N]`.

Writes library.json in the shape src/backend/README.md describes, the covers
and thumbnails it names (drawn here, at config's COVER_SIZE and THUMB_SIZE)
and the art/.sizes marker, into DIR (default build/demo). Every artist, album,
song, playlist, station and person in it is invented; only the curator labels
mimic Apple's own ("Apple Music Chill", "Apple Music Radio"). It came from the
GNOME Shell extension with the backend, and is this app's own now.
`--albums N` (N > 40) adds generated albums, by generated artists, to the 40
hand-written ones, for measuring big libraries: about 12 songs an album, so
2,500 albums make 30,000 songs, or `--tracks N` songs in all when given (the
generated albums are sized to reach it). Their song titles are all distinct, as
a real library's nearly are (a word and a noun, then a tail or a number where
that pair is taken), so sorting and collating them costs what it would.
`--playlists N` (N > 13) adds
generated playlists, each of 22 songs, at the top level of the folders. The
covers are drawn in parallel. The 40 hand-written albums and 13 playlists stay
as they are whatever the options: build/demo is byte-identical.
The last playlist is Favourite Songs, flagged by attributes.isFavourites.
Three playlist folders, one inside another, hold some of the playlists; the
rest are at the top level (`folders`, whose "root" entry lists the top level).
sections.videos holds six invented music videos with 16:9 artwork, as Apple's
is (the thumbnails 320 x 180). The other keys the app's sync adds
(sections.songs, artUrl) are optional and left out: the demo has no loose
songs and nothing to fetch.
Uses only Python stdlib and PyGObject / Cairo (no pip dependencies).
"""
import argparse
import concurrent.futures
import hashlib
import heapq
import importlib.util
import json
import math
import multiprocessing
import os
import pathlib
import random
import sys

import cairo
import gi
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import GdkPixbuf, GLib  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent

# The backend from the source tree, as tests/__init__.py loads it: src/ is laid
# out for installation and becomes the `applemusic` package only there.
if 'applemusic' not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        'applemusic', ROOT / 'src' / '__init__.py', submodule_search_locations=[str(ROOT / 'src')])
    _module = importlib.util.module_from_spec(_spec)
    sys.modules['applemusic'] = _module
    _spec.loader.exec_module(_module)

from applemusic.backend import config, normalize  # noqa: E402

# ---------------------------------------------------------------------------
# Visual styling and palettes
# ---------------------------------------------------------------------------

PALETTES = [
    ('#0d1b2a', '#1b4965', '#62b6cb'),  # Deep ocean
    ('#10002b', '#5a189a', '#e0aaff'),  # Violet nebula
    ('#1f2421', '#499f68', '#dce2aa'),  # Sage forest
    ('#2b0914', '#d90429', '#ffb4a2'),  # Crimson velvet
    ('#0a1128', '#0077b6', '#90e0ef'),  # Electric ice
    ('#1c1917', '#d97706', '#fef3c7'),  # Amber glow
    ('#240046', '#9d4edd', '#ff9e00'),  # Synthwave dusk
    ('#002830', '#0081a7', '#fdfcdc'),  # Nordic fjord
    ('#1a1423', '#5c3d75', '#eac435'),  # Midnight & gold
    ('#14213d', '#fca311', '#e5e5e5'),  # Navy & marigold
    ('#1b263b', '#e76f51', '#f4a261'),  # Sunset terrace
    ('#073b3a', '#0b6e4f', '#80ed99'),  # Emerald canopy
    ('#2e1f27', '#854d27', '#dd722a'),  # Terracotta autumn
    ('#212529', '#495057', '#f8f9fa'),  # Monochrome minimal
    ('#132a13', '#31572c', '#90a955'),  # Olive ridge
    ('#2b2d42', '#8d99ae', '#ef233c'),  # Slate & scarlet
    ('#180018', '#7209b7', '#4cc9f0'),  # Cyber violet
    ('#2c1b18', '#a75d5d', '#ffc3a0'),  # Dusty rose & coffee
    ('#0b132b', '#1c2541', '#5bc0be'),  # Midnight teal
    ('#1e1e24', '#444140', '#e54b4b'),  # Charcoal & coral
    ('#023047', '#219ebc', '#ffb703'),  # Mediterranean harbor
    ('#2b1e3a', '#a23b72', '#f18f01'),  # Twilight plum
    ('#1a202c', '#4a5568', '#a0aec0'),  # Modern slate
    ('#1d3557', '#457b9d', '#a8dadc'),  # Atlantic blues
    ('#3d0c11', '#d1495b', '#edae49'),  # Rich garnet
]

MOTIFS = [
    'sun_horizon',
    'concentric_rings',
    'geometric_facets',
    'soundwave_bars',
    'minimalist_arch',
    'retro_grid',
    'mountain_peaks',
    'halftone_matrix',
    'diagonal_stripes',
    'organic_blobs',
]


def hex_to_rgb(hex_code):
    """Convert hex string '#rrggbb' to (r, g, b) float tuple in 0..1."""
    h = hex_code.lstrip('#')
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def wrap_text(ctx, text, max_width, font_size):
    """Wrap text to fit within max_width using Cairo text extents."""
    ctx.set_font_size(font_size)
    words = text.split()
    lines = []
    current_line = []
    for word in words:
        trial = ' '.join(current_line + [word])
        extents = ctx.text_extents(trial)
        if extents.width <= max_width or not current_line:
            current_line.append(word)
        else:
            lines.append(' '.join(current_line))
            current_line = [word]
    if current_line:
        lines.append(' '.join(current_line))
    return lines


def draw_cover(out_path, title, subtitle, badge, palette, motif, is_artist=False,
               size=config.COVER_SIZE, wide=False):
    """Draw a size x size square artwork with Cairo and save as JPEG via GdkPixbuf.
    The drawing is laid out on a 512-unit square and scaled to `size`. `wide` draws a
    music video's 16:9 frame instead (size wide, on a 512 x 288 canvas): the motif in its
    middle square, which is what a square tile shows of it, under a play symbol."""
    dark_rgb = hex_to_rgb(palette[0])
    mid_rgb = hex_to_rgb(palette[1])
    light_rgb = hex_to_rgb(palette[2])

    width, height = (size, round(size * 9 / 16)) if wide else (size, size)
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    ctx = cairo.Context(surf)
    ctx.scale(size / 512, size / 512)

    # 1. Base gradient
    bg = cairo.LinearGradient(0, 0, 512, 288 if wide else 512)
    bg.add_color_stop_rgb(0.0, *dark_rgb)
    bg.add_color_stop_rgb(1.0, *mid_rgb)
    ctx.set_source(bg)
    ctx.paint()
    if wide:  # the square design, scaled into the middle 288 x 288
        ctx.save()
        ctx.translate(112, 0)
        ctx.scale(288 / 512, 288 / 512)

    # 2. Geometric motif
    if is_artist:
        # The artist tile crops this square art into the circle inscribed in
        # it (shape.js's `round` part: border-radius 9999px on a square box,
        # i.e. a circle of radius 256 centred on the image). Nothing that
        # matters — silhouette or text — may sit outside that circle, so the
        # whole portrait is clipped to a slightly smaller, safe circle and no
        # text is drawn here at all: the shelf/detail views already print the
        # artist's name as their own label below the round artwork.
        cx, cy, safe_r = 256, 256, 236

        ctx.save()
        ctx.arc(cx, cy, safe_r, 0, 2 * math.pi)
        ctx.clip()

        # Soft disc behind the silhouette
        ctx.arc(cx, cy, safe_r, 0, 2 * math.pi)
        ctx.set_source_rgba(light_rgb[0], light_rgb[1], light_rgb[2], 0.22)
        ctx.fill()

        ctx.set_line_width(3.0)
        ctx.arc(cx, cy, safe_r - 8, 0, 2 * math.pi)
        ctx.set_source_rgba(light_rgb[0], light_rgb[1], light_rgb[2], 0.8)
        ctx.stroke()

        # Centred head-and-shoulders silhouette, sized to stay inside safe_r
        # even after the clip (the clip is the real guarantee; this keeps the
        # unclipped shape close to it so nothing looks abruptly cut off).
        ctx.arc(cx, cy - 55, 62, 0, 2 * math.pi)
        ctx.set_source_rgba(light_rgb[0], light_rgb[1], light_rgb[2], 0.95)
        ctx.fill()

        ctx.arc(cx, cy + 210, 150, math.pi, 2 * math.pi)
        ctx.set_source_rgba(light_rgb[0], light_rgb[1], light_rgb[2], 0.85)
        ctx.fill()

        ctx.restore()

    elif motif == 'sun_horizon':
        # Sun disc
        ctx.arc(256, 195, 105, 0, 2 * math.pi)
        ctx.set_source_rgb(*light_rgb)
        ctx.fill()
        # Horizontal blinds
        ctx.set_source_rgb(*dark_rgb)
        for i in range(5):
            ctx.rectangle(90, 205 + i * 18, 332, 8)
            ctx.fill()

    elif motif == 'concentric_rings':
        ctx.set_line_width(2.5)
        for r in range(40, 220, 32):
            ctx.arc(256, 195, r, 0, 2 * math.pi)
            ctx.set_source_rgba(light_rgb[0], light_rgb[1], light_rgb[2], 0.35)
            ctx.stroke()
        ctx.arc(360, 140, 16, 0, 2 * math.pi)
        ctx.set_source_rgb(*light_rgb)
        ctx.fill()

    elif motif == 'geometric_facets':
        ctx.move_to(80, 280)
        ctx.line_to(256, 70)
        ctx.line_to(432, 280)
        ctx.close_path()
        ctx.set_source_rgba(light_rgb[0], light_rgb[1], light_rgb[2], 0.45)
        ctx.fill()

        ctx.move_to(160, 280)
        ctx.line_to(320, 100)
        ctx.line_to(400, 280)
        ctx.close_path()
        ctx.set_source_rgba(mid_rgb[0], mid_rgb[1], mid_rgb[2], 0.7)
        ctx.fill()

    elif motif == 'soundwave_bars':
        for i in range(16):
            h = 35 + math.sin(i * 0.45) * 85 + (i % 3) * 22
            x = 76 + i * 23
            ctx.rectangle(x, 200 - h / 2, 14, h)
            ctx.set_source_rgba(light_rgb[0], light_rgb[1], light_rgb[2], 0.75)
            ctx.fill()

    elif motif == 'minimalist_arch':
        ctx.arc(256, 155, 95, math.pi, 0)
        ctx.line_to(351, 290)
        ctx.line_to(161, 290)
        ctx.close_path()
        ctx.set_source_rgba(light_rgb[0], light_rgb[1], light_rgb[2], 0.55)
        ctx.fill()

        ctx.arc(256, 175, 60, math.pi, 0)
        ctx.line_to(316, 290)
        ctx.line_to(196, 290)
        ctx.close_path()
        ctx.set_source_rgba(dark_rgb[0], dark_rgb[1], dark_rgb[2], 0.8)
        ctx.fill()

    elif motif == 'retro_grid':
        ctx.arc(256, 140, 65, 0, 2 * math.pi)
        ctx.set_source_rgb(*light_rgb)
        ctx.fill()

        ctx.set_line_width(1.5)
        ctx.set_source_rgba(light_rgb[0], light_rgb[1], light_rgb[2], 0.45)
        for x in range(40, 490, 45):
            ctx.move_to(256, 170)
            ctx.line_to(x, 310)
            ctx.stroke()
        for y in [185, 210, 245, 290]:
            ctx.move_to(50, y)
            ctx.line_to(462, y)
            ctx.stroke()

    elif motif == 'mountain_peaks':
        ctx.move_to(40, 310)
        ctx.line_to(190, 130)
        ctx.line_to(340, 310)
        ctx.close_path()
        ctx.set_source_rgba(light_rgb[0], light_rgb[1], light_rgb[2], 0.5)
        ctx.fill()

        ctx.move_to(180, 310)
        ctx.line_to(330, 150)
        ctx.line_to(480, 310)
        ctx.close_path()
        ctx.set_source_rgba(mid_rgb[0], mid_rgb[1], mid_rgb[2], 0.85)
        ctx.fill()

        ctx.arc(390, 95, 25, 0, 2 * math.pi)
        ctx.set_source_rgba(light_rgb[0], light_rgb[1], light_rgb[2], 0.9)
        ctx.fill()

    elif motif == 'halftone_matrix':
        for gx in range(11):
            for gy in range(8):
                x = 66 + gx * 38
                y = 70 + gy * 30
                dist = math.hypot(x - 256, y - 175) / 200
                rad = max(2, 13 * (1 - min(1, dist)))
                ctx.arc(x, y, rad, 0, 2 * math.pi)
                ctx.set_source_rgba(light_rgb[0], light_rgb[1], light_rgb[2],
                                    0.7 * (1 - dist * 0.5))
                ctx.fill()

    elif motif == 'diagonal_stripes':
        ctx.set_line_width(20)
        for i in range(-4, 12):
            ctx.move_to(i * 55, 0)
            ctx.line_to(i * 55 + 200, 320)
            ctx.set_source_rgba(light_rgb[0], light_rgb[1], light_rgb[2],
                                0.35 if i % 2 == 0 else 0.15)
            ctx.stroke()

    elif motif == 'organic_blobs':
        ctx.arc(200, 175, 95, 0, 2 * math.pi)
        ctx.set_source_rgba(light_rgb[0], light_rgb[1], light_rgb[2], 0.4)
        ctx.fill()

        ctx.arc(310, 205, 85, 0, 2 * math.pi)
        ctx.set_source_rgba(mid_rgb[0], mid_rgb[1], mid_rgb[2], 0.65)
        ctx.fill()

    if wide:
        ctx.restore()
        # A play symbol over the middle: a video, not a cover. The tiles label it.
        ctx.arc(256, 144, 46, 0, 2 * math.pi)
        ctx.set_source_rgba(0, 0, 0, 0.45)
        ctx.fill()
        ctx.move_to(242, 120)
        ctx.line_to(242, 168)
        ctx.line_to(282, 144)
        ctx.close_path()
        ctx.set_source_rgba(1.0, 1.0, 1.0, 0.92)
        ctx.fill()

    # 3. Readability scrim + 4. Typography: skipped for artist portraits. That
    # text (title near the bottom-left corner, the badge near the top-right
    # one) sits well outside the inscribed circle the artist tile crops this
    # image to, so it would just be clipped away; the shelf/detail views
    # already show the artist's name as their own label under the artwork.
    if not is_artist and not wide:
        # 3. Readability scrim across bottom area
        scrim = cairo.LinearGradient(0, 250, 0, 512)
        scrim.add_color_stop_rgba(0.0, 0, 0, 0, 0.0)
        scrim.add_color_stop_rgba(0.4, 0, 0, 0, 0.45)
        scrim.add_color_stop_rgba(1.0, 0, 0, 0, 0.88)
        ctx.set_source(scrim)
        ctx.rectangle(0, 250, 512, 262)
        ctx.fill()

        # 4. Typography
        ctx.select_font_face('Sans', cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)

        # Compute optimal font size for title
        title_upper = title.upper()
        font_size = 32
        lines = wrap_text(ctx, title_upper, 440, font_size)
        if len(lines) > 2:
            font_size = 26
            lines = wrap_text(ctx, title_upper, 440, font_size)

        line_step = round(font_size * 1.15)
        y_start = 450 - (len(lines) - 1) * line_step - (28 if subtitle else 0)

        # Draw title
        ctx.set_font_size(font_size)
        ctx.set_source_rgba(1.0, 1.0, 1.0, 0.98)
        for idx, line in enumerate(lines):
            ctx.move_to(36, y_start + idx * line_step)
            ctx.show_text(line)

        # Draw subtitle / artist
        if subtitle:
            sub_y = y_start + len(lines) * line_step + 4
            ctx.select_font_face('Sans', cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
            ctx.set_font_size(20)
            ctx.set_source_rgba(1.0, 1.0, 1.0, 0.78)
            ctx.move_to(36, sub_y)
            ctx.show_text(subtitle)

        # Draw badge / year in upper right
        if badge:
            ctx.select_font_face('Sans', cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            ctx.set_font_size(14)
            ext = ctx.text_extents(badge)
            bx = 512 - 36 - ext.width - 16
            by = 36
            ctx.rectangle(bx, by, ext.width + 16, 26)
            ctx.set_source_rgba(0, 0, 0, 0.45)
            ctx.fill()

            ctx.set_source_rgba(1.0, 1.0, 1.0, 0.85)
            ctx.move_to(bx + 8, by + 18)
            ctx.show_text(badge)

    # 5. Save as JPEG via GdkPixbuf, straight from the surface's pixels
    # rather than through a PNG (which was most of a build's time). The
    # picture is opaque, so premultiplied or not is all one; cairo keeps it
    # as native-endian 32-bit xRGB words, a stride of exactly size * 4, and
    # GdkPixbuf wants R, G, B bytes.
    surf.flush()
    xrgb = bytes(surf.get_data())
    red, green, blue = (2, 1, 0) if sys.byteorder == 'little' else (1, 2, 3)
    rgb = bytearray(width * height * 3)
    rgb[0::3] = xrgb[red::4]
    rgb[1::3] = xrgb[green::4]
    rgb[2::3] = xrgb[blue::4]
    pixbuf = GdkPixbuf.Pixbuf.new_from_bytes(
        GLib.Bytes.new(bytes(rgb)), GdkPixbuf.Colorspace.RGB, False, 8, width, height, width * 3)
    pixbuf.savev(out_path, 'jpeg', ['quality'], ['85'])


# ---------------------------------------------------------------------------
# Data definitions: Artists, Albums, Playlists, Radio Stations
# ---------------------------------------------------------------------------

ARTISTS_DATA = [
    {
        'name': 'The Midnight Archipelago',
        'genre': 'Post-Rock',
        'bio': ('An instrumental post-rock collective exploring expansive dynamic shifts, '
                'tape-looped guitars, and oceanic textures.'),
    },
    {
        'name': 'Solaris Circuit',
        'genre': 'Synthwave',
        'bio': ('Blends analog polyphonic synthesizers with driving drum machine patterns, '
                'evoking neon-lit metropolitan nights.'),
    },
    {
        'name': 'Mara Lind & The Tide',
        'genre': 'Indie Folk',
        'bio': ('Weaves fingerpicked acoustic guitars, upright bass, and intimate vocal harmonies '
                'into evocative coastal landscapes.'),
    },
    {
        'name': 'Komorebi Quartet',
        'genre': 'Modern Classical',
        'bio': ('Combines prepared piano, cello, viola, and delicate field recordings, creating '
                'contemplative spaces exploring natural light.'),
    },
    {
        'name': 'Neon Boulevard',
        'genre': 'Dream Pop',
        'bio': ('Crafts lush, shimmering dream pop and synthpop with reverb-drenched guitars, '
                'sparkling arpeggios, and melancholic melodies.'),
    },
    {
        'name': 'Echoes of Orion',
        'genre': 'Ambient',
        'bio': ('Produces deep, immersive space ambient music utilizing modular synthesizer '
                'drones and resonant acoustic filters.'),
    },
    {
        'name': 'Velvet Horizon',
        'genre': 'Neo-Soul',
        'bio': ('Marries warm Rhodes electric pianos, silky vocal arrangements, and unhurried '
                'hip-hop-influenced grooves.'),
    },
    {
        'name': 'Dust & Radiance',
        'genre': 'Shoegaze',
        'bio': ('Creates dense walls of fuzzy distortion and soaring guitar glissandos, anchored '
                'by propulsive rhythm sections.'),
    },
    {
        'name': 'Sora Takahashi',
        'genre': 'Nu-Jazz',
        'bio': ('Tokyo-based keyboardist bridging modal acoustic jazz, complex syncopations, and '
                'modern UK broken beat production.'),
    },
    {
        'name': 'The Glass Observatory',
        'genre': 'Cinematic',
        'bio': ('Crafts grand, emotional cinematic narratives combining sweeping orchestral '
                'strings and subtle electronic undercurrents.'),
    },
    {
        'name': 'Cassette Memories',
        'genre': 'Lo-Fi Hip-Hop',
        'bio': ('Crafts nostalgic instrumental lo-fi beats saturated with warm vinyl crackle, '
                'dust-laden samples, and relaxed drums.'),
    },
    {
        'name': 'Aura & Frequency',
        'genre': 'Deep House',
        'bio': ('Explores the intersections of hypnotic deep house, rolling basslines, and '
                'melodic techno for peak-time dance floors.'),
    },
    {
        'name': 'Juniper Moon',
        'genre': 'Indie Pop',
        'bio': ('Celebrated for upbeat jangly guitars, brass accents, lyrical wit, and '
                'irresistible infectious hooks.'),
    },
    {
        'name': 'Paper Parachutes',
        'genre': 'Math Rock',
        'bio': ('Pairs intricate finger-tapping guitar work and interlocking odd-time polyrhythms '
                'with passionate, soaring choruses.'),
    },
    {
        'name': 'Subterranean Brass',
        'genre': 'Funk & Jazz',
        'bio': ('A high-octane 8-piece brass powerhouse fusing New Orleans street grooves, hard '
                'bop phrasing, and heavy funk backbeats.'),
    },
]

ALBUMS_DATA = [
    # 1. The Midnight Archipelago (3 albums)
    {
        'artist_idx': 0,
        'title': 'Signal from the Shallows',
        'year': 2018,
        'genre': 'Post-Rock',
        'summary': ('An atmospheric exploration of maritime isolation, anchored by reverberant '
                    'guitars and swelling cymbal washes.'),
        'discs': [
            [
                'Low Tide Warning', 'Beacon in the Fog', 'Submerged Currents',
                'Breakwater Echo', 'Distant Shoals', 'Tidal Drift',
                'The Shallows Wake', 'Anchor Line', 'Salt & Timber', 'Returning Tide',
            ],
        ],
    },
    {
        'artist_idx': 0,
        'title': 'Islands in Suspension',
        'year': 2021,
        'genre': 'Post-Rock',
        'summary': ('Expansive arrangements tracing the contours of remote archipelagos with '
                    'soaring crescendos and delicate acoustic interludes.'),
        'discs': [
            [
                'Archipelago Sunrise', 'Windward Passage', 'Suspension Bridge',
                'Isle of Glass', 'Mist Over Cape Hope', 'Granite Coast',
                "Seafarer's Compass", 'Quiet Estuary', 'Echoes on the Water',
                'Cove of Lanterns', 'Midnight Horizon', 'Drifting Home',
            ],
        ],
    },
    {
        'artist_idx': 0,
        'title': 'Cartography of Fog',
        'year': 2024,
        'genre': 'Post-Rock',
        'summary': ('A sweeping double-album journey across uncharted coastal sounds, moving from '
                    'meditative drones to thundering sonic squalls.'),
        'discs': [
            [
                "The Mapmaker's Ledger", 'Charted Coastline', 'Northbound Swell',
                'Lost Coordinates', 'Dense Maritime Air', 'Shoal Marker',
                'Sounding the Depth', 'First Anchorage',
            ],
            [
                'The Western Reach', 'Ghost Ship Relay', 'Barometer Falling',
                'Lighthouse Beam', 'Reef Navigation', 'Storm Petrels',
                'Compass Variation', 'Safe Harbor Lights',
            ],
        ],
    },

    # 2. Solaris Circuit (3 albums)
    {
        'artist_idx': 1,
        'title': 'Neon Velocity',
        'year': 2019,
        'genre': 'Synthwave',
        'summary': ('A nocturnal celebration of analog synthesizers, gated reverbs, and '
                    'high-speed highway escapades.'),
        'discs': [
            [
                'Ignition Sequence', 'Overdrive City', 'Chrome Highway',
                'Midnight Pursuit', 'Turbocharger', 'Gridlock Romance',
                'Analog Boulevard', 'Redline Horizon', 'Nightfall Accelerant',
                'Synthetic Pulse', 'Dawn Run',
            ],
        ],
    },
    {
        'artist_idx': 1,
        'title': 'Transmission Zero',
        'year': 2022,
        'genre': 'Synthwave',
        'summary': ('Dark cyberpunk motifs collide with cinematic arpeggios in a conceptual '
                    'narrative of underground radio dissidents.'),
        'discs': [
            [
                'Frequency Lock', 'Cybernetic Heart', 'Signal Decode',
                'Sublevel Terminal', 'Fiber Optic Sky', 'Rogue Satellite',
                'Quantum Static', 'Data Stream', 'Baud Rate 9600', 'End of Line',
            ],
        ],
    },
    {
        'artist_idx': 1,
        'title': 'Suborbital Drift',
        'year': 2025,
        'genre': 'Electronic',
        'summary': ('Weightless electronic rhythms and modular sequences inspired by low-Earth '
                    'orbit observations.'),
        'discs': [
            [
                'Atmosphere Exit', 'Zero G Velocity', 'Ion Engines',
                'Orbital Decay', 'Solar Wind', 'Apogee Burn',
                'Silent Thrusters', 'Dark Side Transit', 'Atmospheric Re-entry',
            ],
        ],
    },

    # 3. Mara Lind & The Tide (3 albums)
    {
        'artist_idx': 2,
        'title': 'Saltwater Hymns',
        'year': 2017,
        'genre': 'Indie Folk',
        'summary': ('Intimate fingerpicked acoustic ballads recorded in an empty wooden chapel by '
                    'the ocean.'),
        'discs': [
            [
                'Morning on the Pier', 'Tidepool Reflections', 'Dune Grass',
                "Fisherman's Daughter", 'Copper Kettle', 'Salt Air',
                'Woodsmoke & Sea', 'The Old Dinghy', 'Driftwood Fire', 'Lullaby for High Seas',
            ],
        ],
    },
    {
        'artist_idx': 2,
        'title': 'Canyon Fireflies',
        'year': 2020,
        'genre': 'Indie Folk',
        'summary': ('Earthy harmonies and porch-side storytelling capturing summer twilights in '
                    'the high desert.'),
        'discs': [
            [
                'Red Rock Valley', 'Firefly Glow', 'Dust on the Windshield',
                'Canyon Wall Whispers', 'Pinecone Lanterns', 'Riverbend Song',
                'Porch Swing Melody', 'Twilight Crickets', 'Hitching Post',
                'Cedar Smoke', 'Sleep Beneath the Stars',
            ],
        ],
    },
    {
        'artist_idx': 2,
        'title': 'The Northern Harbor',
        'year': 2023,
        'genre': 'Indie Folk',
        'summary': ('Rich chamber-folk arrangements with upright bass, fiddle, and poetic lyrics '
                    'of homecoming.'),
        'discs': [
            [
                'Harbor Bells', 'Ferry Crossing', 'Woolen Sweaters',
                'Gull Wing Flight', 'November Mist', 'Cobblestone Street',
                'Anchored in the Bay', 'Teahouse Window', 'Rowboat Solitude',
                'Cold Current', 'Winter Wharf', 'Homeward Voyage',
            ],
        ],
    },

    # 4. Komorebi Quartet (3 albums)
    {
        'artist_idx': 3,
        'title': 'Leaves in Still Water',
        'year': 2016,
        'genre': 'Modern Classical',
        'summary': ('Gentle prepared piano and string motifs mirroring the calm ripples of autumn '
                    'ponds.'),
        'discs': [
            [
                'First Ripple', 'Canopy Sunlight', 'Fallen Maple',
                'Silent Pond', 'Moss Garden', 'Raindrop Cadence',
                'Autumn Reverie', 'Bamboo Shadows', 'Evening Stillness',
            ],
        ],
    },
    {
        'artist_idx': 3,
        'title': 'Architecture of Silence',
        'year': 2021,
        'genre': 'Modern Classical',
        'summary': ('A profound two-part meditation recorded inside historic cathedral cloisters, '
                    'pairing resonance with deep pause.'),
        'discs': [
            [
                'Foundation Stone', 'The Empty Corridor', 'Arches of Dust',
                'Resonant Room', 'Vaulted Ceiling', 'Shaft of Light', 'Courtyard Rain',
            ],
            [
                'Pillar Shadows', 'Stairway in Marble', 'Acoustic Reflection',
                'The Cloister', 'Stone Bench', 'Belfry Breeze',
                'Quiet Nave', 'Final Echo',
            ],
        ],
    },
    {
        'artist_idx': 3,
        'title': 'Winter Light Studies',
        'year': 2024,
        'genre': 'Modern Classical',
        'summary': ('Sparse cello and viola duets capturing the fragile crystalline stillness of '
                    'northern winters.'),
        'discs': [
            [
                'Frost on Cedar', 'Pale Sunlight', 'Frozen Lake Etude',
                'Icicle Harmonics', 'Snowfall Nocturne', 'Winter Solstice',
                'Breath in Cold Air', 'Glacial Purity', 'Thawing Stream', 'Early Spring Whisper',
            ],
        ],
    },

    # 5. Neon Boulevard (3 albums)
    {
        'artist_idx': 4,
        'title': 'Midnight Cassette Club',
        'year': 2018,
        'genre': 'Dream Pop',
        'summary': ('Wistful dream-pop shimmering with vintage chorus pedals, tape flutter, and '
                    'romantic nostalgia.'),
        'discs': [
            [
                'Side A Track 1', 'Roller Disco', "Sunset Boulevard '88",
                'Starlight Diner', 'Lipstick Mirror', 'Prom Night Regrets',
                'Pastel Convertible', 'Tape Rewind', 'Neon Palms',
                'Late Call', 'Fade to Sunrise',
            ],
        ],
    },
    {
        'artist_idx': 4,
        'title': 'Electric Reverie',
        'year': 2021,
        'genre': 'Synthpop',
        'summary': ('Energetic hooks and sparkling synth arpeggios designed for midnight drives '
                    'and neon skylines.'),
        'discs': [
            [
                'Dream Sequence', 'Laser Dance', 'Prism Glow',
                'Memory Card', 'Velvet Highway', 'Synthesizer Heartbeat',
                'Mirage', 'Electric Blue', 'Midnight Kiss', 'Reverie Outro',
            ],
        ],
    },
    {
        'artist_idx': 4,
        'title': 'After Hours Echo',
        'year': 2024,
        'genre': 'Synthpop',
        'summary': ('Reflective downtempo synthpop capturing the quiet intimacy of empty city '
                    'streets at 3 AM.'),
        'discs': [
            [
                'City Lights Blurring', 'Last Call at the Lounge', 'Rainy Asphalt',
                'Subway Tile Reflection', 'Taxi Ride Reverie', 'Neon Umbrella',
                'Night Owl', '2 AM Espresso', 'Empty Dancefloor',
                'Corner Booth', 'Distant Sirens', 'Dawn Breaking Over Rooftops',
            ],
        ],
    },

    # 6. Echoes of Orion (3 albums)
    {
        'artist_idx': 5,
        'title': 'Stellar Cartography',
        'year': 2015,
        'genre': 'Ambient',
        'summary': ('Expansive analog drone compositions mapping distant celestial landmarks and '
                    'quiet cosmic voids.'),
        'discs': [
            [
                'Pillars of Creation', 'Horsehead Nebula', 'Lagrange Point 2',
                'Oort Cloud Passage', 'Kuiper Belt Drift', 'Cassini Gap',
                'Andromeda Approaching', 'Cosmic Horizon',
            ],
        ],
    },
    {
        'artist_idx': 5,
        'title': 'Voyager Suite',
        'year': 2019,
        'genre': 'Ambient',
        'summary': ('A grand conceptual double album tracing the solitary trajectory of robotic '
                    'explorers beyond our solar system.'),
        'discs': [
            [
                'Golden Record Intro', 'Jupiter Flyby', 'Great Red Spot',
                'Radiation Belts', 'Rings of Saturn', 'Enceladus Geysers',
                "Titan's Atmosphere", 'Heliosphere Boundary',
            ],
            [
                'Interstellar Medium', 'Pale Blue Dot', 'Signal Lag 19 Hours',
                'Dark Void Transit', 'Radioisotope Glow', 'Cosmic Dust Impacts',
                'Deep Space Antenna', 'Wandering the Galaxy', 'Infinite Silence',
            ],
        ],
    },
    {
        'artist_idx': 5,
        'title': 'Deep Cosmic Field',
        'year': 2023,
        'genre': 'Ambient',
        'summary': ('Sub-bass frequencies and slow-evolving harmonic filters evocative of cosmic '
                    'background radiation.'),
        'discs': [
            [
                'Vacuum Energy', 'Event Horizon', 'Singularity Pulse',
                'Gravitational Wave', 'Supercluster Web', 'Dark Matter Halo',
                'Cosmic Microwave Glow', 'Pulsar Beacon', 'Eternal Expansion',
            ],
        ],
    },

    # 7. Velvet Horizon (3 albums)
    {
        'artist_idx': 6,
        'title': 'Golden Hour Vibrations',
        'year': 2020,
        'genre': 'Neo-Soul',
        'summary': ('Warm Rhodes progressions, buttery vocal arrangements, and laid-back grooves '
                    'for sunset unwinding.'),
        'discs': [
            [
                'Honey Amber', 'Warm Breeze', 'Rooftop Sundown',
                'Smooth Operator', 'Silk Sheets', 'Golden Hour Glow',
                'Chai Tea & Chords', 'Unspoken Rhythm', 'Lazy Sunday Grooves',
                'Amber Skies', 'Dusk Embrace',
            ],
        ],
    },
    {
        'artist_idx': 6,
        'title': 'Midnight Bloom',
        'year': 2022,
        'genre': 'Neo-Soul',
        'summary': ('Sensual nighttime R&B and jazz-infused chord work detailing romance and city '
                    'nightlife.'),
        'discs': [
            [
                'Night Jasmine', 'Velvet Petals', 'Moonlit Patio',
                'Low Key Loving', 'Dim Lights & Wine', 'Late Night Text',
                'Heartstrings', 'Midnight Bloom', 'Slow Burn',
                'Soulful Cadence', 'Velvet Silhouette', 'After Dark',
            ],
        ],
    },
    {
        'artist_idx': 6,
        'title': 'Velvet Sessions Vol. 1',
        'year': 2025,
        'genre': 'Neo-Soul',
        'summary': ('Live-in-studio jams highlighting improvisational chemistry and unhurried '
                    'acoustic warmth.'),
        'discs': [
            [
                'Session Prelude', 'Rhodes in F Minor', 'Pocket Groove',
                'Muted Trumpet Soul', 'Bassline Serenade', 'Finger Snaps',
                'Vinyl Interlude', 'Vintage Warmth', 'Late Jam', 'Outro Toast',
            ],
        ],
    },

    # 8. Dust & Radiance (3 albums)
    {
        'artist_idx': 7,
        'title': 'Tremolo Summer',
        'year': 2017,
        'genre': 'Shoegaze',
        'summary': ('Glissando guitar washes, dizzying whammy bar vibrato, and buried vocals in a '
                    'haze of summer feedback.'),
        'discs': [
            [
                'Feedback Loop', 'Sun Drenched Fuzz', 'Tremolo Waves',
                'Blinding Glare', 'Reverb Haze', 'Silver Lake Walk',
                'Distortion Kiss', 'Pedalboard Dreams', "Summer's End Swell",
                'Overdrive Twilight',
            ],
        ],
    },
    {
        'artist_idx': 7,
        'title': 'Feedback Cathedral',
        'year': 2020,
        'genre': 'Shoegaze',
        'summary': ('Towering walls of sonic fuzz and harmonic resonance creating an overwhelming '
                    'yet sacred sonic sanctuary.'),
        'discs': [
            [
                'Nave of Noise', 'Echo Chamber', 'Stained Glass Shards',
                'Vault of Reverb', 'Sustained Note', 'Altar of Amps',
                'Sonic Sacrament', 'Cathedral Bells', 'Fuzz Choir',
                'Harmonic Resonator', 'Ascension',
            ],
        ],
    },
    {
        'artist_idx': 7,
        'title': 'Distortion in Bloom',
        'year': 2023,
        'genre': 'Shoegaze',
        'summary': ('A massive two-disc shoegaze opus tracing fragile acoustic melodies as they '
                    'disintegrate into euphoric distortion.'),
        'discs': [
            [
                'First Petal Feedback', 'Overdriven Stem', 'Wall of Sound Blossom',
                'Swirling Chorus', 'Decay Rate', 'Fuzz Meadow',
                'Grounded Wire', 'Static Garden', 'Greenhouse Drone',
            ],
            [
                'Noon Sun Glare', 'Tape Saturation', 'Reverb Spores',
                'Electric Vine', 'Wild Thistle Distortion', 'Blown Speaker Bloom',
                'Dusk Petal', 'Night-Blooming Jasmine Fuzz', 'Root System',
            ],
        ],
    },

    # 9. Sora Takahashi (3 albums)
    {
        'artist_idx': 8,
        'title': 'Tokyo Rain Reflections',
        'year': 2019,
        'genre': 'Nu-Jazz',
        'summary': ("Fluid piano lines and brushed syncopations capturing rainy evenings under "
                    "Shinjuku's neon signs."),
        'discs': [
            [
                'Shinjuku Crosswalk', 'Umbrella Drops', 'Neon in Puddles',
                'Yamanote Line Groove', 'Underpass Improvisation', 'Midnight Ramen Blues',
                'Alleyway Lanterns', 'Rainy Windowpane', 'Electric Piano Mist',
                'Last Train at Midnight',
            ],
        ],
    },
    {
        'artist_idx': 8,
        'title': 'Modal Drift',
        'year': 2022,
        'genre': 'Nu-Jazz',
        'summary': ('Complex modal jazz harmonies interwoven with lively broken-beat drumming and '
                    'upright bass flourishes.'),
        'discs': [
            [
                'Dorian Awakening', 'Pentatonic Cloud', 'Syncopated Pulse',
                'Rhodes Drift', 'Bass Solo in E', 'Brushed Snare',
                'Polytonal Glide', 'Fourth Interval', 'Floating Measure',
                'Modal Shift', 'Coda Reflections',
            ],
        ],
    },
    {
        'artist_idx': 8,
        'title': 'Syncopation City',
        'year': 2025,
        'genre': 'Nu-Jazz',
        'summary': ('A vibrant celebration of urban kinetic energy, shifting time signatures, and '
                    'sparkling Rhodes solos.'),
        'discs': [
            [
                'Rush Hour 8 AM', 'Cross-Rhythm Station', 'Broken Beat Espresso',
                'Hi-Hat Shuffle', 'Subway Echoes', '7/8 On the Expressway',
                'Pedestrian Syncopation', 'Rooftop Jam', 'Offbeat Romance',
                'Groove Laboratory', 'City Never Stops', 'Night Transit',
            ],
        ],
    },

    # 10. The Glass Observatory (3 albums)
    {
        'artist_idx': 9,
        'title': 'Constellations in Amber',
        'year': 2018,
        'genre': 'Cinematic',
        'summary': ('Orchestral strings and brass motifs evoking brass astrolabes and historic '
                    'hilltop stargazing domes.'),
        'discs': [
            [
                'Lens Calibration', 'Amber Skies', 'Telescopic Sweep',
                'The Dome Opens', 'Brass Gears', 'Focal Plane',
                'Starlight Preserved', 'Spectral Lines', 'Dawn Shutter',
            ],
        ],
    },
    {
        'artist_idx': 9,
        'title': 'The Permafrost Echo',
        'year': 2022,
        'genre': 'Cinematic',
        'summary': ('A chilling double-disc soundtrack for Arctic expeditions, balancing sub-zero '
                    'strings with thundering percussion.'),
        'discs': [
            [
                'Tundra Expedition', 'Ice Core Samples', 'Glacial Moraine',
                'The Frozen Valley', 'Blizzard Approaches', 'Aurora Borealis Choir',
                'Sub-Zero Pressure', 'First Thaw',
            ],
            [
                'Deep Crevasse', 'The Singing Ice', 'Frozen Compass',
                'Glacier Tongue', 'Mammoth Bones', 'Arctic Midnight Sun',
                'Permafrost Memory', 'Echoing Fjord',
            ],
        ],
    },
    {
        'artist_idx': 9,
        'title': 'Mirrors and Meteors',
        'year': 2025,
        'genre': 'Cinematic',
        'summary': ('Dynamic brass swells and pulsing electronics inspired by meteor showers and '
                    'optical astronomy.'),
        'discs': [
            [
                'Parabolic Mirror', 'Shooting Star Trace', 'Atmospheric Entry',
                'Meteor Shower Suite', 'Silver Coating', 'Night Sky Panorama',
                'Gravity Lens', 'Impact Crater Echo', 'Reflecting Pool',
                'Orbital Sweep',
            ],
        ],
    },

    # 11. Cassette Memories (2 albums)
    {
        'artist_idx': 10,
        'title': 'Warm Tape Hiss',
        'year': 2020,
        'genre': 'Lo-Fi Hip-Hop',
        'summary': ('Cozy beats saturated with vinyl crackle, dust-laden piano loops, and gentle '
                    'rainy day vibes.'),
        'discs': [
            [
                'Coffee Grinder Intro', 'Morning Sunlight', 'Vintage Vinyl Flip',
                'Rainy Day Study', 'Muffled Snare', 'Dusty Keys',
                'Tape Head Cleaning', 'Porch Stoop Beats', 'Subtle Headnod',
                'Cat on the Amplifier', 'Bonsai Tree', 'Midnight Chillout',
                'Crayon Drawings', 'Goodnight Tape',
            ],
        ],
    },
    {
        'artist_idx': 10,
        'title': 'Late Night Porch Sessions',
        'year': 2023,
        'genre': 'Lo-Fi Hip-Hop',
        'summary': ('Relaxed porch-side beatcraft weaving acoustic guitar licks with cricket '
                    'ambiance and gentle kicks.'),
        'discs': [
            [
                'Screen Door Slam', 'Crickets in Stereo', 'Gentle Strum',
                'Neighborhood Lamp', 'Cold Lemonade', 'Firefly Beat',
                'Vinyl Crackle Breeze', 'Faded Photograph', 'Late Summer Thoughts',
                'Distant Trains', 'Muted Horn', 'Twilight Chords', 'Moon Over the Yard',
            ],
        ],
    },

    # 12. Aura & Frequency (2 albums)
    {
        'artist_idx': 11,
        'title': 'Underground Reverberation',
        'year': 2019,
        'genre': 'Deep House',
        'summary': ('Hypnotic rolling basslines and warm dub chords crafted for intimate basement '
                    'sound systems.'),
        'discs': [
            [
                'Basement Entrance', 'Sub-Bass Pressure', '4 AM Warehouse',
                'Strobe Sequence', 'Hypnotic Loop', 'Filter Sweep',
                'Deep Resonance', 'Modular Acid', 'Peak Time', 'Tunnel Echo',
            ],
        ],
    },
    {
        'artist_idx': 11,
        'title': 'Resonance Chamber',
        'year': 2023,
        'genre': 'Melodic Techno',
        'summary': ('A two-disc exploration of cavernous industrial reverbs, driving kicks, and '
                    'ascending synth leads.'),
        'discs': [
            [
                'Chamber Acoustics', 'Kicking Low', 'Analog Hi-Hats',
                'Dark Matter Groove', 'Sonic Oscillation', 'Subterranean Sweep',
                'Pulse Modulation', 'Reverb Tail',
            ],
            [
                'Second Chamber', 'Echo Velocity', 'Industrial Percussion',
                'Hypnotic State', 'Midnight Frequency', 'Sine Wave Meditation',
                'Driving Rhythm', 'Dawn Release',
            ],
        ],
    },

    # 13. Juniper Moon (2 albums)
    {
        'artist_idx': 12,
        'title': 'Wildflower Gazette',
        'year': 2021,
        'genre': 'Indie Pop',
        'summary': ('Jangly acoustic guitars, whimsical lyricism, and chamber strings celebrating '
                    'springtime adventures.'),
        'discs': [
            [
                'Morning Gazette', 'Dandelion Wine', 'Bicycle Bell Song',
                'Picnic in the Park', 'Penny Loafers', 'Botanical Garden Waltz',
                'Lemon Drop Sun', 'Paper Airplane', 'Cottage Garden',
                'Chamber Strings', 'Sunday Stroll',
            ],
        ],
    },
    {
        'artist_idx': 12,
        'title': 'Paper Lantern Waltz',
        'year': 2024,
        'genre': 'Indie Pop',
        'summary': ('Delicate melodies and brass touches evoking evening lanterns swaying along '
                    'garden paths.'),
        'discs': [
            [
                'Festival Eve', 'Paper Lantern Glow', 'String of Lights',
                'Carousel Melody', 'Night Market Waltz', 'Origami Boat',
                'Moonlit Pathway', 'Gentle Clarinet', 'Last Lantern', 'Sleepy Town',
            ],
        ],
    },

    # 14. Paper Parachutes (2 albums)
    {
        'artist_idx': 13,
        'title': 'Odd Time Signatures',
        'year': 2019,
        'genre': 'Math Rock',
        'summary': ('Intricate finger-tapping patterns in 7/8 and 5/4 anchored by passionate '
                    'vocal melodies.'),
        'discs': [
            [
                'Count in 7/4', 'Finger Tap Intro', 'Polyrhythm Cafe',
                'Twinkly Guitars', 'Angular Chords', 'Off-Grid Breakdown',
                'Syncopated Heart', 'Math Class Blues', 'Pedal Shuffle',
                '5/8 Resolution', 'Final Measure',
            ],
        ],
    },
    {
        'artist_idx': 13,
        'title': 'Kinetic Geometry',
        'year': 2023,
        'genre': 'Math Rock',
        'summary': ('Sharp rhythmic shifts, interlocking guitar loops, and explosive dynamic '
                    'releases.'),
        'discs': [
            [
                'Tesseract', 'Sharp Angles', 'Interlocking Rhythms',
                'Fractal Breakdown', 'Kinetic Motion', 'Triangulation',
                'Perpendicular Lines', 'Velocity Vector', 'Harmonic Symmetry', 'Zero Point',
            ],
        ],
    },

    # 15. Subterranean Brass (2 albums)
    {
        'artist_idx': 14,
        'title': 'Low Frequency Grooves',
        'year': 2020,
        'genre': 'Funk & Jazz',
        'summary': ('Sousaphone basslines and infectious New Orleans second-line drumming with '
                    'hard-hitting funk energy.'),
        'discs': [
            [
                'Sousaphone Strut', 'Trombone Shout', 'Funk in the Alley',
                'Second Line Beat', 'Heavy Horn Section', 'Groove Machine',
                'Low End Rumble', 'Street Parade', 'Brass Breakdown',
                'Fat Bass Groove', 'Encore Funk',
            ],
        ],
    },
    {
        'artist_idx': 14,
        'title': 'The Basement Collective',
        'year': 2024,
        'genre': 'Contemporary Jazz',
        'summary': ('Fiery brass solo trades and infectious syncopated backbeats captured in an '
                    'intimate basement jam session.'),
        'discs': [
            [
                'Basement Jam Prelude', 'Tenor Sax Battle', 'Syncopated Snare',
                'Hot Pepper Horns', 'Bourbon Street Stomp', 'Midnight Brass Session',
                'Funky Footsteps', 'Muted Trumpet Jam', 'Collective Improv',
                'Big Brass Energy', 'Last Call Groove', 'Walk Home Stomp',
            ],
        ],
    },
]

PLAYLISTS_DATA = [
    {
        'title': 'Late Night Drift',
        'subtitle': 'Apple Music Chill',
        'genre': 'Downtempo',
        'summary': ('Low-tempo beats, atmospheric synths, and mellow melodies for late hours and '
                    'quiet contemplations.'),
        'filter_genres': ['Ambient', 'Lo-Fi Hip-Hop', 'Neo-Soul', 'Modern Classical'],
    },
    {
        'title': 'Analog Horizons',
        'subtitle': 'Apple Music Electronic',
        'genre': 'Electronic',
        'summary': ('Warm analog synthesizer compositions, arpeggiated sequences, and vintage '
                    'drum machines.'),
        'filter_genres': ['Synthwave', 'Electronic', 'Deep House', 'Melodic Techno'],
    },
    {
        'title': 'Indie Currents',
        'subtitle': 'Apple Music Indie',
        'genre': 'Alternative & Indie',
        'summary': ('The most compelling new sounds from independent bands, singer-songwriters, '
                    'and DIY studios.'),
        'filter_genres': ['Indie Folk', 'Indie Pop', 'Math Rock', 'Post-Rock'],
    },
    {
        'title': 'Deep Focus & Stillness',
        'subtitle': 'Apple Music Ambient',
        'genre': 'Ambient',
        'summary': ('Uncluttered ambient soundscapes and subtle drones designed to foster '
                    'concentration and flow.'),
        'filter_genres': ['Ambient', 'Modern Classical'],
    },
    {
        'title': 'Modern Jazz Underground',
        'subtitle': 'Apple Music Jazz',
        'genre': 'Jazz',
        'summary': ('Contemporary jazz explorations, complex rhythms, and forward-thinking '
                    'acoustic improvisations.'),
        'filter_genres': ['Nu-Jazz', 'Funk & Jazz', 'Contemporary Jazz'],
    },
    {
        'title': 'Sunset Drive',
        'subtitle': 'Curated by Robin Vale',
        'genre': 'Indie Pop',
        'summary': ('Golden hour indie anthems and euphoric hooks perfect for winding coastal '
                    'roads and open windows.'),
        'filter_genres': ['Dream Pop', 'Indie Pop', 'Indie Folk', 'Synthpop'],
    },
    {
        'title': 'Neon Expressway',
        'subtitle': 'Apple Music Synthwave',
        'genre': 'Synthwave',
        'summary': ('High-octane synthwave, retro electro, and driving basslines for night '
                    'driving under streetlights.'),
        'filter_genres': ['Synthwave', 'Synthpop', 'Electronic'],
    },
    {
        'title': 'Golden Hour Melodies',
        'subtitle': 'Apple Music Acoustic',
        'genre': 'Acoustic & Folk',
        'summary': ('Warm fingerstyle acoustics, gentle cello lines, and heartfelt vocal '
                    'performances.'),
        'filter_genres': ['Indie Folk', 'Modern Classical', 'Neo-Soul'],
    },
    {
        'title': 'Subterranean Bass',
        'subtitle': 'Apple Music Club',
        'genre': 'Deep House',
        'summary': ('Deep, rolling grooves and hypnotic club rhythms from subterranean '
                    'underground dancefloors.'),
        'filter_genres': ['Deep House', 'Melodic Techno', 'Electronic'],
    },
    {
        'title': 'Quiet Reflections',
        'subtitle': 'Apple Music Classical',
        'genre': 'Modern Classical',
        'summary': ('Gentle piano studies, minimalist string quartets, and contemplative '
                    'neo-classical works.'),
        'filter_genres': ['Modern Classical', 'Ambient', 'Cinematic'],
    },
    {
        'title': 'Soul & Reverie',
        'subtitle': 'Apple Music R&B',
        'genre': 'Neo-Soul',
        'summary': ('Lush chord progressions, velvet vocals, and head-nodding neo-soul grooves '
                    'for relaxed evenings.'),
        'filter_genres': ['Neo-Soul', 'Lo-Fi Hip-Hop', 'Nu-Jazz'],
    },
    {
        'title': 'Heavy Shoegaze & Echoes',
        'subtitle': 'Curated by Ellis Marsh',
        'genre': 'Shoegaze',
        'summary': ('Swirling fuzz pedals, feedback-drenched melodies, and ethereal '
                    'reverberations that envelop the senses.'),
        'filter_genres': ['Shoegaze', 'Post-Rock', 'Math Rock'],
    },
]

# The Favourite Songs playlist, the songs the listener loves. Apple keeps it as an ordinary
# library playlist with a flag of its own; here the flag is attributes.isFavourites
# (applemusic.library.FAVOURITES), which the app's sync maps Apple's onto. No genre or year, as
# Apple's has none.
FAVOURITES_DATA = {
    'title': 'Favourite Songs',
    'subtitle': '',  # a library playlist: no curator
    'summary': 'The songs you love, newest first.',
    'size': 36,
}

# The playlist folders, as Apple's sidebar nests them: a folder's playlists are numbers into
# PLAYLISTS_DATA (1 is l.pl001), its folders are titles here, and each lists its folders first,
# as Apple Music does. The playlists no folder holds, Favourite Songs among them, are at the top
# level, after the top-level folders, in the playlists' order.
FOLDERS_DATA = [
    {'title': 'Chill & Focus', 'folders': ['Jazz Nights'], 'playlists': [1, 4, 10]},
    {'title': 'Jazz Nights', 'folders': [], 'playlists': [5, 11]},
    {'title': 'On the Road', 'folders': [], 'playlists': [6, 7, 2]},
]

STATIONS_DATA = [
    {
        'title': 'Lighthouse One',
        'subtitle': 'Apple Music Radio',
        'genre': 'Various',
        'summary': ('The pulse of music culture with daily live broadcasts, exclusive artist '
                    'interviews, and global premieres.'),
    },
    {
        'title': 'Evergreen Hits Radio',
        'subtitle': 'Apple Music Radio',
        'genre': 'Pop & Rock',
        'summary': ("Celebrating the songs you know and love from the '80s, '90s, and 2000s with "
                    "passionate daily hosts."),
    },
    {
        'title': 'Slow Tide Radio',
        'subtitle': 'Apple Music Radio',
        'genre': 'Downtempo & Ambient',
        'summary': ('An uninterrupted stream of relaxed beats, mellow melodies, and soothing '
                    'acoustic textures.'),
    },
    {
        'title': 'Echoes in the Dark',
        'subtitle': 'Apple Music Radio',
        'genre': 'Electronic & Ambient',
        'summary': ('Atmospheric electronica, hypnotic modular synthesis, and dark ambient '
                    'soundscapes for nocturnal hours.'),
    },
    {
        'title': 'Pacific Highway Radio',
        'subtitle': 'Apple Music Radio',
        'genre': 'Indie & Alternative',
        'summary': ('Carefree indie melodies, breezy dream pop, and road-trip classics broadcast '
                    'straight from the coast.'),
    },
    {
        'title': 'The Soundstage',
        'subtitle': 'Apple Music Radio',
        'genre': 'Cinematic & Soundtracks',
        'summary': ('Sweeping orchestral film scores, modern classical compositions, and epic '
                    'cinematic themes.'),
    },
    {
        'title': 'Ambient Sleep Radio',
        'subtitle': 'Apple Music Radio',
        'genre': 'Ambient',
        'summary': ('Gentle sonic textures, continuous pink noise, and calming generative drones '
                    'designed for deep sleep.'),
    },
    {
        'title': 'Global Rhythm Pulse',
        'subtitle': 'Apple Music Radio',
        'genre': 'World & Nu-Jazz',
        'summary': ('Infectious polyrhythms, Afrobeat brass, Latin jazz grooves, and '
                    'cross-cultural beat experiments.'),
    },
]


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

# Music videos, each by one of the hand-written artists (an index into ARTISTS_DATA).
VIDEOS_DATA = [
    {'title': 'Lanterns over the Shallows', 'artist_idx': 0, 'year': 2021},
    {'title': 'Night Drive to Nowhere', 'artist_idx': 1, 'year': 2022},
    {'title': 'Harbour Lights (Live at the Pier)', 'artist_idx': 2, 'year': 2023},
    {'title': 'Paper Moons', 'artist_idx': 4, 'year': 2020},
    {'title': 'Slow Burn Sunday', 'artist_idx': 6, 'year': 2024},
    {'title': 'Tape Loop Afternoon', 'artist_idx': 10, 'year': 2025},
]


def build_folders(playlist_ids):
    """library.json's `folders` from FOLDERS_DATA: the "root" entry first, listing the top
    level, then each folder in FOLDERS_DATA's order. playlist_ids are the playlists' ids in
    section order; ids like l.fd001 are given in FOLDERS_DATA's order."""
    ids = {data['title']: f'l.fd{n:03d}' for n, data in enumerate(FOLDERS_DATA, 1)}
    parents = {child: ids[data['title']] for data in FOLDERS_DATA for child in data['folders']}
    folders = []
    held = set()
    for data in FOLDERS_DATA:
        children = [{'kind': 'folder', 'id': ids[title]} for title in data['folders']]
        children += [{'kind': 'playlist', 'id': playlist_ids[n - 1]} for n in data['playlists']]
        held.update(playlist_ids[n - 1] for n in data['playlists'])
        folders.append({
            'id': ids[data['title']],
            'title': data['title'],
            'parent': parents.get(data['title'], 'root'),
            'children': children,
        })
    top = [{'kind': 'folder', 'id': folder['id']}
           for folder in folders if folder['parent'] == 'root']
    top += [{'kind': 'playlist', 'id': pid} for pid in playlist_ids if pid not in held]
    return [{'id': 'root', 'title': 'Playlists', 'parent': None, 'children': top}] + folders


def slug(text):
    """Generate a clean URL-friendly slug."""
    return ''.join(c.lower() if c.isalnum() else '-' for c in text).strip('-')


def format_duration(ms):
    """Format milliseconds into 'M:SS' label."""
    total_seconds = round(ms / 1000)
    minutes = total_seconds // 60
    seconds = total_seconds % 60
    return f'{minutes}:{seconds:02d}'


# ---------------------------------------------------------------------------
# Generated albums for big libraries (--albums)
# ---------------------------------------------------------------------------

GEN_WORDS = [
    'Amber', 'Silver', 'Hollow', 'Quiet', 'Electric', 'Paper', 'Velvet', 'Distant',
    'Northern', 'Golden', 'Crystal', 'Scarlet', 'Faded', 'Wandering', 'Bright',
    'Tidal', 'Frozen', 'Hidden', 'Lunar', 'Copper', 'Glass', 'Slow', 'Pale',
    'Silent', 'Burning', 'Winter', 'Coastal', 'Borrowed', 'Open', 'Last',
]
GEN_NOUNS = [
    'Harbours', 'Satellites', 'Gardens', 'Rivers', 'Lanterns', 'Signals',
    'Horizons', 'Orchards', 'Tides', 'Mirrors', 'Valleys', 'Engines', 'Letters',
    'Islands', 'Wires', 'Meadows', 'Comets', 'Streets', 'Kites', 'Waves',
    'Embers', 'Canyons', 'Clouds', 'Parades', 'Circuits', 'Forests', 'Bridges',
    'Maps', 'Rooms', 'Stations',
]
# What a generated song title gets when its word and noun are taken: realistic
# endings first, a number when those run out.
GEN_TAILS = [
    'at Dawn', 'in Blue', '(Reprise)', 'for Two', 'Again', '(Live)', 'Revisited', 'on Tape',
    'at Night', 'in Motion', '(Interlude)', 'Pt. II', 'Undone', '(Acoustic)', 'in the Rain',
    'Overture', '(Demo)', 'After Hours', 'Redux', '(Edit)', 'by Morning', 'in Reverse',
    'Suite', '(Instrumental)', 'Once More', 'at the Edge', 'in Silver', '(Extended)',
    'Theme', 'Unfolding', 'from Afar', '(Alternate Take)', 'in Colour', 'Lullaby',
    '(Radio Mix)', 'Nocturne', 'Waltz', 'Sketch', '(Outro)', 'Coda',
]
GEN_FIRST = ['Ada', 'Bram', 'Cleo', 'Dario', 'Elin', 'Fenna', 'Gus', 'Hana', 'Ivo', 'Juno',
             'Kaito', 'Lior', 'Mina', 'Nils', 'Oona', 'Pim', 'Rhea', 'Sol', 'Tove', 'Wren']
GEN_LAST = ['Aldane', 'Brisk', 'Corran', 'Dunmore', 'Elsworth', 'Falkner', 'Greaves', 'Hollin',
            'Ivers', 'Jessop', 'Kettle', 'Lowry', 'Marchetti', 'Norland', 'Oakes', 'Pellow',
            'Quill', 'Rowan', 'Sable', 'Thorne']


def song_counts(album_count, track_count):
    """How many songs each of `album_count` generated albums gets so that, with the
    hand-written albums', the library holds `track_count` songs in all: 8 to 16 an album
    around the mean, the last few adjusted to land exactly. Seeded on its own."""
    if album_count <= 0:
        return []
    written = sum(len(disc) for album in ALBUMS_DATA for disc in album['discs'])
    wanted = track_count - written
    if wanted < album_count:
        raise ValueError(f'--tracks must be at least {written + album_count} for '
                         f'{album_count} generated albums')
    mean = round(wanted / album_count)
    rnd = random.Random(11)
    counts = [rnd.randint(max(1, mean - 4), mean + 4) for _ in range(album_count)]
    step = 1 if sum(counts) < wanted else -1
    position = 0
    while sum(counts) != wanted:
        if counts[position] + step >= 1:
            counts[position] += step
        position = (position + 1) % album_count
    return counts


def generated_albums(count, tracks=None):
    """(artists, albums) in ARTISTS_DATA's and ALBUMS_DATA's shapes: the hand-written
    ones, then enough invented ones to make `count` albums, by invented artists of one
    to six albums each, of 8 to 16 songs, or sized so the library holds `tracks` songs in
    all when that is given. Seeded, so a count always gives the same library."""
    artists = list(ARTISTS_DATA)
    albums = list(ALBUMS_DATA)
    rnd = random.Random(7)  # not the builder's: the hand-written part stays as it was
    genres = sorted({artist['genre'] for artist in ARTISTS_DATA})
    seen_artists = {artist['name'] for artist in artists}
    seen_titles = {album['title'] for album in albums}
    seen_songs = {song for album in albums for disc in album['discs'] for song in disc}
    tails = random.Random(17)  # its own, so the albums and artists drawn from rnd stay as they were
    counts = song_counts(count - len(albums), tracks) if tracks is not None else None
    remaining = 0

    def unique(make, seen):
        for _ in range(20):
            name = make()
            if name not in seen:
                break
        else:
            name = f'{make()} {len(seen) + 1}'
        seen.add(name)
        return name

    def song_title(base):
        """base, or base with a tail (or at last a number) that no song has yet."""
        title = base
        for _ in range(20):
            if title not in seen_songs:
                break
            title = f'{base} {tails.choice(GEN_TAILS)}'
        else:
            number = 2
            while f'{base} {number}' in seen_songs:
                number += 1
            title = f'{base} {number}'
        seen_songs.add(title)
        return title

    while len(albums) < count:
        if remaining == 0:
            remaining = rnd.randint(1, 6)
            name = unique(lambda: rnd.choice([
                f'The {rnd.choice(GEN_WORDS)} {rnd.choice(GEN_NOUNS)}',
                f'{rnd.choice(GEN_FIRST)} {rnd.choice(GEN_LAST)}',
                f'{rnd.choice(GEN_NOUNS)} & {rnd.choice(GEN_NOUNS)}',
            ]), seen_artists)
            genre = rnd.choice(genres)
            bio = f'An invented {genre.lower()} act, generated for big demo libraries.'
            artists.append({'name': name, 'genre': genre, 'bio': bio})
        remaining -= 1
        artist = artists[-1]
        title = unique(lambda: rnd.choice([
            f'{rnd.choice(GEN_WORDS)} {rnd.choice(GEN_NOUNS)}',
            f'{rnd.choice(GEN_NOUNS)} of {rnd.choice(GEN_WORDS)} {rnd.choice(GEN_NOUNS)}',
            f'The {rnd.choice(GEN_WORDS)} {rnd.choice(GEN_NOUNS)}',
        ]), seen_titles)
        song_count = counts[len(albums) - len(ALBUMS_DATA)] if counts else rnd.randint(8, 16)
        songs = [song_title(f'{rnd.choice(GEN_WORDS)} {rnd.choice(GEN_NOUNS)}')
                 for _ in range(song_count)]
        albums.append({
            'artist_idx': len(artists) - 1,
            'title': title,
            'year': rnd.randint(1968, 2026),
            'genre': artist['genre'],
            'summary': f"A generated {artist['genre'].lower()} album by {artist['name']}.",
            'discs': [songs],
        })
    return artists, albums


def generated_playlists(count):
    """PLAYLISTS_DATA's entries, then enough invented ones to make `count` playlists with
    Favourite Songs (which the builder adds last) counted in: curated by invented people or
    "Apple Music <genre>", each drawing on one to three genres. Seeded."""
    playlists = list(PLAYLISTS_DATA)
    rnd = random.Random(13)  # its own: the hand-written playlists' songs stay as they were
    genres = sorted({artist['genre'] for artist in ARTISTS_DATA})
    seen = {playlist['title'] for playlist in playlists}
    while len(playlists) + 1 < count:
        for _ in range(20):
            title = rnd.choice([
                f'{rnd.choice(GEN_WORDS)} {rnd.choice(GEN_NOUNS)} Mix',
                f'{rnd.choice(GEN_NOUNS)} for {rnd.choice(GEN_WORDS)} Days',
                f"{rnd.choice(GEN_FIRST)}'s {rnd.choice(GEN_NOUNS)}",
            ])
            if title not in seen:
                break
        else:
            title = f'{title} {len(seen) + 1}'
        seen.add(title)
        genre = rnd.choice(genres)
        curator = rnd.choice([f'Curated by {rnd.choice(GEN_FIRST)} {rnd.choice(GEN_LAST)}',
                              f'Apple Music {genre}'])
        playlists.append({
            'title': title,
            'subtitle': curator,
            'genre': genre,
            'summary': f'A generated {genre.lower()} playlist for big demo libraries.',
            'filter_genres': sorted(set([genre] + rnd.sample(genres, rnd.randint(0, 2)))),
        })
    return playlists


def _draw_one(job):
    out_path, kwargs, thumb_path, thumb_size = job
    draw_cover(out_path, **kwargs)
    pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(out_path, thumb_size, thumb_size, True)
    pixbuf.savev(thumb_path, 'jpeg', ['quality'], ['90'])


def draw_covers(covers, thumb_dir, thumb_size):
    """Draw each queued cover (draw_cover arguments) and its thumbnail under the same name in
    thumb_dir: in this process for the usual few dozen, in a pool of processes (forked:
    there are no threads here) for a big generated library."""
    jobs = [(out_path, kwargs,
             os.path.join(thumb_dir, os.path.basename(out_path)), thumb_size)
            for out_path, kwargs in covers]
    if len(jobs) < 200:
        for job in jobs:
            _draw_one(job)
        return
    context = multiprocessing.get_context('fork')
    with concurrent.futures.ProcessPoolExecutor(mp_context=context) as pool:
        for _ in pool.map(_draw_one, jobs, chunksize=16):
            pass


# ---------------------------------------------------------------------------
# Main library builder
# ---------------------------------------------------------------------------

def build_demo_library(out_dir, cover_size=config.COVER_SIZE, thumb_size=config.THUMB_SIZE,
                       album_count=None, playlist_count=None, track_count=None):
    """Generate library.json, cover_size artwork and the thumb_size thumbnails the
    tiles draw, and record both sizes in art/.sizes as a sync does. An album_count
    above the hand-written albums' adds generated ones (generated_albums), sized to
    hold track_count songs in all when given; a playlist_count above the hand-written
    playlists' (Favourite Songs counted) adds generated ones (generated_playlists)."""
    rnd = random.Random(42)
    artists_data, albums_data = generated_albums(album_count or len(ALBUMS_DATA), track_count)
    playlists_data = generated_playlists(playlist_count or len(PLAYLISTS_DATA) + 1)

    art_dir = os.path.join(out_dir, 'art')
    thumb_dir = os.path.join(out_dir, 'thumb')
    os.makedirs(art_dir, exist_ok=True)
    os.makedirs(thumb_dir, exist_ok=True)
    # Before drawing: a changed thumbnail size wipes thumb/.
    normalize.apply_art_sizes(out_dir, cover_size, thumb_size)

    # The covers are drawn together at the end (draw_covers), in parallel when
    # there are many; each draw_cover call below only queues one.
    covers = []

    def draw_cover(out_path, **kwargs):
        covers.append((out_path, kwargs))

    def thumb_for(art_path):
        """The thumbnail of a cover, under the same name in thumb/."""
        return os.path.join(thumb_dir, os.path.basename(art_path))

    catalog_id_base = 1724040000
    global_trk_counter = 1

    # 1. Build Albums and Tracks
    albums = []
    # Map artist_idx -> list of album objects for this artist
    artist_albums_map = {i: [] for i in range(len(artists_data))}
    all_tracks_catalog = []

    for alb_idx, alb_data in enumerate(albums_data, 1):
        artist_info = artists_data[alb_data['artist_idx']]
        artist_name = artist_info['name']
        album_id = f'l.alb{alb_idx:03d}'
        album_cat_id = str(catalog_id_base + alb_idx * 10)
        album_url = f"https://music.apple.com/us/album/{slug(alb_data['title'])}/{album_cat_id}"

        # Assign artwork palette & motif
        palette = PALETTES[alb_idx % len(PALETTES)]
        motif = MOTIFS[alb_idx % len(MOTIFS)]
        art_hash = hashlib.sha1(album_id.encode('utf-8')).hexdigest()
        art_path = os.path.join(art_dir, f'{art_hash}.jpg')
        draw_cover(
            out_path=art_path,
            title=alb_data['title'],
            subtitle=artist_name,
            badge=str(alb_data['year']),
            palette=palette,
            motif=motif,
            is_artist=False,
            size=cover_size,
        )
        thumb_path = thumb_for(art_path)

        # Build album tracks and groups
        album_groups = []
        album_all_tracks = []
        overall_index = 0

        for disc_idx, disc_track_names in enumerate(alb_data['discs'], 1):
            disc_entries = []
            group_name = f'Disc {disc_idx}' if len(alb_data['discs']) > 1 else 'Disc 1'

            for trk_num, trk_title in enumerate(disc_track_names, 1):
                # Duration between 140s and 380s
                dur_ms = rnd.randint(140, 380) * 1000 + rnd.randint(0, 999)
                is_explicit = rnd.random() < 0.12  # ~12% explicit

                trk_obj = {
                    'id': f'i.trk{global_trk_counter:04d}',
                    'catalogId': str(catalog_id_base + 50000 + global_trk_counter),
                    'title': trk_title,
                    'artist': artist_name,
                    'album': alb_data['title'],
                    'trackNumber': trk_num,
                    'discNumber': disc_idx,
                    'durationMs': dur_ms,
                    'durationLabel': format_duration(dur_ms),
                    'explicit': is_explicit,
                    'index': overall_index,
                    'thumb': None,
                    'type': 'library-songs',
                }
                global_trk_counter += 1
                overall_index += 1
                disc_entries.append(trk_obj)
                album_all_tracks.append(trk_obj)
                all_tracks_catalog.append((artist_info['genre'], trk_obj, thumb_path))

            album_groups.append({
                'name': group_name,
                'play': {'kind': 'album', 'id': album_id},
                'entries': disc_entries,
            })

        total_ms = sum(t['durationMs'] for t in album_all_tracks)
        album_explicit = any(t['explicit'] for t in album_all_tracks)

        album_item = {
            'id': album_id,
            'kind': 'album',
            'title': alb_data['title'],
            'subtitle': artist_name,
            'year': alb_data['year'],
            'genre': alb_data['genre'],
            'summary': alb_data['summary'],
            'art': art_path,
            'thumb': thumb_path,
            'artColor': palette[1],
            'trackCount': len(album_all_tracks),
            'durationMs': total_ms,
            'explicit': album_explicit,
            'catalogId': album_cat_id,
            'url': album_url,
            'play': {'kind': 'album', 'id': album_id},
            'groups': album_groups,
        }
        albums.append(album_item)
        artist_albums_map[alb_data['artist_idx']].append(album_item)

    # 2. Build Artists
    artists = []
    for art_idx, art_data in enumerate(artists_data, 1):
        artist_id = f'l.art{art_idx:03d}'
        artist_cat_id = str(catalog_id_base + 1000 + art_idx)
        artist_url = f"https://music.apple.com/us/artist/{slug(art_data['name'])}/{artist_cat_id}"

        # Assign artwork palette & motif
        palette = PALETTES[(art_idx * 3) % len(PALETTES)]
        art_hash = hashlib.sha1(artist_id.encode('utf-8')).hexdigest()
        art_path = os.path.join(art_dir, f'{art_hash}.jpg')
        draw_cover(
            out_path=art_path,
            title=art_data['name'],
            subtitle='Artist',
            badge='ARTIST',
            palette=palette,
            motif='',
            is_artist=True,
            size=cover_size,
        )

        artist_albs = artist_albums_map[art_idx - 1]
        artist_groups = []
        for alb in artist_albs:
            # Flatten tracks from album groups
            alb_tracks = []
            for g in alb['groups']:
                alb_tracks.extend(g['entries'])

            artist_groups.append({
                'name': alb['title'],
                'play': {'kind': 'album', 'id': alb['id']},
                'entries': alb_tracks,
            })

        artist_item = {
            'id': artist_id,
            'kind': 'artist',
            'title': art_data['name'],
            'subtitle': '',
            'year': None,
            'genre': art_data['genre'],
            'summary': art_data['bio'],
            'art': art_path,
            'thumb': thumb_for(art_path),
            'artColor': palette[1],
            'albumCount': len(artist_albs),
            'explicit': False,
            'catalogId': artist_cat_id,
            'url': artist_url,
            'play': {'kind': 'artist', 'id': artist_id},
            'groups': artist_groups,
        }
        artists.append(artist_item)

    # 3. Build Playlists
    # The catalog by genre, each list in catalog order with its position, so a playlist's
    # matching tracks are merged back into catalog order (as one pass over the catalog
    # would list them) without a pass over 40,000 tracks per playlist.
    by_genre = {}
    for position, (genre, trk, thumb) in enumerate(all_tracks_catalog):
        by_genre.setdefault(genre, []).append((position, (trk, thumb)))
    playlists = []
    for pl_idx, pl_data in enumerate(playlists_data, 1):
        playlist_id = f'l.pl{pl_idx:03d}'
        playlist_cat_id = str(catalog_id_base + 2000 + pl_idx)
        playlist_url = ('https://music.apple.com/us/playlist/'
                        f"{slug(pl_data['title'])}/{playlist_cat_id}")

        palette = PALETTES[(pl_idx * 5) % len(PALETTES)]
        motif = MOTIFS[(pl_idx * 2) % len(MOTIFS)]
        art_hash = hashlib.sha1(playlist_id.encode('utf-8')).hexdigest()
        art_path = os.path.join(art_dir, f'{art_hash}.jpg')
        draw_cover(
            out_path=art_path,
            title=pl_data['title'],
            subtitle=pl_data['subtitle'],
            badge='PLAYLIST',
            palette=palette,
            motif=motif,
            is_artist=False,
            size=cover_size,
        )

        # Pick candidate tracks matching playlist genre or collection
        matching_tracks = [pair for _position, pair in heapq.merge(
            *(by_genre.get(genre, []) for genre in dict.fromkeys(pl_data['filter_genres'])))]
        if len(matching_tracks) < 18:
            matching_tracks = [(t, thumb) for _, t, thumb in all_tracks_catalog]

        # Deterministic sample for this playlist
        selected_raw = rnd.sample(matching_tracks, min(len(matching_tracks), 22))

        playlist_tracks = []
        # A playlist's rows carry their own cover — the album's, here.
        for i, (raw_trk, raw_thumb) in enumerate(selected_raw):
            playlist_tracks.append({
                'id': f'i.plt{pl_idx:02d}_{i:03d}',
                'catalogId': raw_trk['catalogId'],
                'title': raw_trk['title'],
                'artist': raw_trk['artist'],
                'album': raw_trk['album'],
                'trackNumber': i + 1,
                'discNumber': 1,
                'durationMs': raw_trk['durationMs'],
                'durationLabel': raw_trk['durationLabel'],
                'explicit': raw_trk['explicit'],
                'index': i,
                'thumb': raw_thumb,
                'type': 'library-songs',
            })

        total_ms = sum(t['durationMs'] for t in playlist_tracks)
        playlist_explicit = any(t['explicit'] for t in playlist_tracks)

        playlist_item = {
            'id': playlist_id,
            'kind': 'playlist',
            'title': pl_data['title'],
            'subtitle': pl_data['subtitle'],
            'year': 2026,
            'genre': pl_data['genre'],
            'summary': pl_data['summary'],
            'art': art_path,
            'thumb': thumb_for(art_path),
            'artColor': palette[1],
            'trackCount': len(playlist_tracks),
            'durationMs': total_ms,
            'explicit': playlist_explicit,
            'catalogId': playlist_cat_id,
            'url': playlist_url,
            'play': {'kind': 'playlist', 'id': playlist_id},
            'groups': [
                {
                    'name': 'Tracks',
                    'play': {'kind': 'playlist', 'id': playlist_id},
                    'entries': playlist_tracks,
                }
            ],
        }
        playlists.append(playlist_item)

    # The Favourite Songs playlist, after the others so they stay as they were. Its own random
    # source, so nothing drawn from rnd afterwards changes either.
    favourites_id = f'l.pl{len(playlists) + 1:03d}'
    art_path = os.path.join(art_dir,
                            f"{hashlib.sha1(favourites_id.encode('utf-8')).hexdigest()}.jpg")
    draw_cover(
        out_path=art_path,
        title=FAVOURITES_DATA['title'],
        subtitle='Apple Music',  # the picture's words, as before
        badge='PLAYLIST',
        palette=('#2b0914', '#d90429', '#ffb4a2'),
        motif='concentric_rings',
        is_artist=False,
        size=cover_size,
    )
    loved = random.Random(7).sample(all_tracks_catalog,
                                    min(len(all_tracks_catalog), FAVOURITES_DATA['size']))
    favourite_tracks = []
    for i, (_genre, raw_trk, raw_thumb) in enumerate(loved):
        favourite_tracks.append({
            'id': f'i.fav_{i:03d}',
            'catalogId': raw_trk['catalogId'],
            'title': raw_trk['title'],
            'artist': raw_trk['artist'],
            'album': raw_trk['album'],
            'trackNumber': i + 1,
            'discNumber': 1,
            'durationMs': raw_trk['durationMs'],
            'durationLabel': raw_trk['durationLabel'],
            'explicit': raw_trk['explicit'],
            'index': i,
            'thumb': raw_thumb,
            'type': 'library-songs',
        })
    favourites_play = {'kind': 'playlist', 'id': favourites_id}
    playlists.append({
        'id': favourites_id,
        'kind': 'playlist',
        'title': FAVOURITES_DATA['title'],
        'subtitle': FAVOURITES_DATA['subtitle'],
        'year': None,
        'genre': None,
        'summary': FAVOURITES_DATA['summary'],
        'art': art_path,
        'thumb': thumb_for(art_path),
        'artColor': '#d90429',
        'trackCount': len(favourite_tracks),
        'durationMs': sum(t['durationMs'] for t in favourite_tracks),
        'explicit': any(t['explicit'] for t in favourite_tracks),
        'catalogId': None,
        'url': None,
        'play': favourites_play,
        'attributes': {'isFavourites': True},
        'groups': [{'name': 'Tracks', 'play': favourites_play, 'entries': favourite_tracks}],
    })

    # 4. Build Radio Stations
    radio_stations = []
    for st_idx, st_data in enumerate(STATIONS_DATA, 1):
        station_id = f'ra.st{st_idx:03d}'
        station_cat_id = str(catalog_id_base + 3000 + st_idx)
        station_url = ('https://music.apple.com/us/station/'
                       f"{slug(st_data['title'])}/{station_cat_id}")

        palette = PALETTES[(st_idx * 7) % len(PALETTES)]
        motif = MOTIFS[(st_idx * 3) % len(MOTIFS)]
        art_hash = hashlib.sha1(station_id.encode('utf-8')).hexdigest()
        art_path = os.path.join(art_dir, f'{art_hash}.jpg')
        draw_cover(
            out_path=art_path,
            title=st_data['title'],
            subtitle=st_data['subtitle'],
            badge='RADIO',
            palette=palette,
            motif=motif,
            is_artist=False,
            size=cover_size,
        )

        station_item = {
            'id': station_id,
            'kind': 'station',
            'title': st_data['title'],
            'subtitle': st_data['subtitle'],
            'year': None,
            'genre': st_data['genre'],
            'summary': st_data['summary'],
            'art': art_path,
            'thumb': thumb_for(art_path),
            'artColor': palette[1],
            'explicit': False,
            'catalogId': station_cat_id,
            'url': station_url,
            'play': {'kind': 'station', 'id': station_id},
            'groups': [],
        }
        radio_stations.append(station_item)

    # 5. Music videos: after the rest, with a random source of their own, so nothing above
    # changes. 16:9 artwork, and the Item shape the sync gives them (normalize_item).
    videos = []
    video_rnd = random.Random(23)
    for v_idx, v_data in enumerate(VIDEOS_DATA, 1):
        video_id = f'i.mv{v_idx:04d}'
        video_cat_id = str(catalog_id_base + 4000 + v_idx)
        artist_info = artists_data[v_data['artist_idx']]
        palette = PALETTES[(v_idx * 11) % len(PALETTES)]
        art_path = os.path.join(art_dir,
                                f"{hashlib.sha1(video_id.encode('utf-8')).hexdigest()}.jpg")
        draw_cover(
            out_path=art_path,
            title=v_data['title'],
            subtitle=artist_info['name'],
            badge=None,
            palette=palette,
            motif=MOTIFS[(v_idx * 7) % len(MOTIFS)],
            is_artist=False,
            size=cover_size,
            wide=True,
        )
        dur_ms = video_rnd.randint(180, 330) * 1000
        videos.append({
            'id': video_id,
            'kind': 'video',
            'title': v_data['title'],
            'subtitle': artist_info['name'],
            'year': v_data['year'],
            'genre': artist_info['genre'],
            'summary': None,
            'art': art_path,
            'thumb': thumb_for(art_path),
            'artColor': palette[1],
            'durationMs': dur_ms,
            'explicit': False,
            'catalogId': video_cat_id,
            'url': f"https://music.apple.com/us/music-video/{slug(v_data['title'])}/{video_cat_id}",
            'play': {'kind': 'musicVideo', 'id': video_id},
            'groups': [],
        })

    # 6. Build Shelves
    # Heavy Rotation: popular albums + top playlists + 1 radio station
    heavy_rotation_items = [
        albums[1],   # Islands in Suspension
        albums[3],   # Neon Velocity
        albums[6],   # Saltwater Hymns
        albums[9],   # Leaves in Still Water
        albums[18],  # Golden Hour Vibrations
        playlists[0],  # Late Night Drift
        playlists[5],  # Sunset Drive
        radio_stations[0],  # Lighthouse One
    ]

    # Recently Added: 8 newest albums
    newest_albums = sorted(albums, key=lambda a: (a['year'] or 0, a['id']), reverse=True)[:8]

    # Recently Played: 4 albums + 3 playlists + 1 radio station
    recently_played_items = [
        albums[4],   # Transmission Zero
        albums[12],  # Midnight Cassette Club
        albums[21],  # Tremolo Summer
        albums[24],  # Tokyo Rain Reflections
        playlists[1],  # Analog Horizons
        playlists[2],  # Indie Currents
        playlists[6],  # Neon Expressway
        radio_stations[2],  # Slow Tide Radio
    ]

    # Made for You: 4 playlists + 4 albums
    made_for_you_items = [
        playlists[3],  # Deep Focus & Stillness
        playlists[4],  # Modern Jazz Underground
        playlists[9],  # Quiet Reflections
        playlists[10],  # Soul & Reverie
        albums[15],  # Stellar Cartography
        albums[27],  # Constellations in Amber
        albums[30],  # Warm Tape Hiss
        albums[38],  # Low Frequency Grooves
    ]

    shelves = [
        {
            'key': 'heavy-rotation',
            'title': 'Heavy Rotation',
            'items': heavy_rotation_items,
        },
        {
            'key': 'recently-added',
            'title': 'Recently Added',
            'items': newest_albums,
        },
        {
            'key': 'recently-played',
            'title': 'Recently Played',
            'items': recently_played_items,
        },
        {
            'key': 'made-for-you',
            'title': 'Made for You',
            'items': made_for_you_items,
        },
    ]

    library = {
        'version': 2,  # sync.LIBRARY_VERSION
        'generated': '2026-09-25T12:00:00Z',
        'storefront': 'us',
        'sections': {
            'albums': albums,
            'artists': artists,
            'playlists': playlists,
            'radio': radio_stations,
            'videos': videos,
        },
        'shelves': shelves,
        'folders': build_folders([playlist['id'] for playlist in playlists]),
    }

    draw_covers(covers, thumb_dir, thumb_size)

    out_file = os.path.join(out_dir, 'library.json')
    with open(out_file, 'w', encoding='utf-8') as f:
        json.dump(library, f, indent=2)

    print(
        f'demo library: {len(albums)} albums, {len(artists)} artists, '
        f'{len(playlists)} playlists, {len(radio_stations)} radio stations, '
        f'{len(videos)} music videos in {out_dir}'
    )


# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description='Generate an invented Apple Music library for demos, screenshots and tests.'
    )
    parser.add_argument(
        '--cache',
        default=str(ROOT / 'build' / 'demo'),
        help='Cache directory to write library.json, art/ and thumb/ into (default: build/demo)',
    )
    parser.add_argument(
        '--albums',
        type=int,
        default=len(ALBUMS_DATA),
        help=f'How many albums (default and minimum {len(ALBUMS_DATA)}); more are generated, '
             'by generated artists, for measuring big libraries',
    )
    parser.add_argument(
        '--playlists',
        type=int,
        default=len(PLAYLISTS_DATA) + 1,
        help=f'How many playlists, Favourite Songs counted (default and minimum '
             f'{len(PLAYLISTS_DATA) + 1}); more are generated, at the top level',
    )
    parser.add_argument(
        '--tracks',
        type=int,
        help='How many songs in all (default: 8 to 16 an album); the generated albums are '
             'sized to reach it',
    )
    args = parser.parse_args()
    if args.albums < len(ALBUMS_DATA):
        parser.error(f'--albums must be at least {len(ALBUMS_DATA)}')
    if args.playlists < len(PLAYLISTS_DATA) + 1:
        parser.error(f'--playlists must be at least {len(PLAYLISTS_DATA) + 1}')
    if args.tracks is not None and args.albums == len(ALBUMS_DATA):
        parser.error('--tracks needs --albums above the hand-written ones')
    return os.path.abspath(args.cache), args.albums, args.playlists, args.tracks


def main():
    out_dir, album_count, playlist_count, track_count = parse_args()
    try:
        build_demo_library(out_dir, album_count=album_count, playlist_count=playlist_count,
                           track_count=track_count)
    except ValueError as error:
        sys.exit(f'demo_library: {error}')


if __name__ == '__main__':
    main()
