# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""The error policy: each EngineError code's sentence and button (src/errors.py), and where
Application.report() and the engine's `lost` send them (tests/test_app.py's app)."""

import unittest

from tests import ROOT  # noqa: F401  (registers src/ as the applemusic package)
from tests.test_app import AppTestCase

from applemusic.backend.errors import CODES, EngineError
from applemusic.errors import error_message


class ErrorMessageTest(unittest.TestCase):
    def test_the_table(self):
        self.assertEqual(error_message('no-browser'), (
            'Google Chrome is needed to play Apple Music', 'Preferences',
            'app.show-engine-preferences'))
        self.assertEqual(error_message('engine-down'), (
            'The playback engine is not running', 'Start', 'app.start-engine'))
        self.assertEqual(error_message('no-keyring'), (
            'The keyring is not available, so starting would lose the Apple Music sign-in',
            'Retry', 'app.start-engine'))
        self.assertEqual(error_message('not-signed-in'), (
            'Sign in to Apple Music again', 'Sign In', 'app.sign-in'))
        self.assertEqual(error_message('timeout'), (
            'Apple Music did not answer in time', None, None))
        self.assertEqual(error_message('api'), ('Apple Music could not do that', None, None))
        self.assertEqual(error_message('usage'), ('Something went wrong', None, None))
        self.assertEqual(error_message('anything else'), ('Something went wrong', None, None))

    def test_every_code_has_a_sentence_and_its_action_exists(self):
        from applemusic import main

        app = main.Application('0', 'io.github.jackicus.AppleMusic.ErrorsTest',
                               'io.github.jackicus.AppleMusic', 'default')
        for code in CODES:
            title, button, action = error_message(code)
            self.assertTrue(title, code)
            self.assertEqual(bool(button), bool(action), code)
            if action:
                self.assertIsNotNone(app.lookup_action(action.removeprefix('app.')), action)


class ReportTest(AppTestCase):
    def test_the_detail_stays_in_the_log(self):
        with self.assertLogs('applemusic.main', 'WARNING') as logged:
            self.app.report(EngineError('api', '500 <b>Internal</b> & more'))
        self.assertEqual(self.app.toasts, [('Apple Music could not do that', None, None)])
        self.assertIn('500 <b>Internal</b> & more', logged.output[0])

    def test_not_signed_in_opens_the_sign_in_when_signed_out(self):
        asked = []
        self.app.activate_action = lambda name, parameter=None: asked.append(name)
        with self.assertLogs('applemusic.main', 'WARNING'):
            self.app.report(EngineError('not-signed-in'))
        self.assertEqual((asked, self.app.toasts), (['sign-in'], []))
        self.app.settings.set_boolean('signed-in', True)  # the sign-in has expired
        with self.assertLogs('applemusic.main', 'WARNING'):
            self.app.report(EngineError('not-signed-in'))
        self.assertEqual(asked, ['sign-in'])
        self.assertEqual(self.app.toasts,
                         [('Sign in to Apple Music again', 'Sign In', 'app.sign-in')])

    def test_the_demo_has_no_engine_to_start(self):
        self.app.demo = True
        with self.assertLogs('applemusic.main', 'WARNING'):
            self.app.report(EngineError('engine-down'))
        self.assertEqual(self.app.toasts, [('Not available with the demo library', None, None)])


class LostTest(AppTestCase):
    def setUp(self):
        super().setUp()
        self.app.engine.connect('lost', self.app._on_engine_lost)

    def test_the_lost_engine_offers_a_restart(self):
        self.app.engine.emit('lost', 'the page crashed')
        self.assertEqual(self.app.toasts,
                         [('The playback engine stopped', 'Restart', 'app.start-engine')])

    def test_nothing_while_quitting_or_the_account_changes(self):
        self.app.signing_in = True
        self.app.engine.emit('lost', 'the Chrome window was closed')
        self.app.signing_in = False
        self.app.signing_out = True
        self.app.engine.emit('lost', 'stopped')
        self.app.signing_out = False
        self.app._quitting = object()  # the quit under way
        self.app.engine.emit('lost', 'stopped')
        self.assertEqual(self.app.toasts, [])


if __name__ == '__main__':
    unittest.main()
