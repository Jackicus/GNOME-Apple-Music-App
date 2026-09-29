# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""Google Chrome as the engine tests have it: a process the Engine spawns with its DevTools pipe
on descriptors 3 and 4, relaying that pipe to the test's fake browser over a Unix socket.

The test process answers CDP on the socket's other end (tests/test_engine.py), while this
process lives and dies as Chrome would: the Engine's signals reach it, its exit closes the
pipe, and the pipe closing (the Engine's end let go) ends it, as it ends Chrome. Not a test
module; run by the fake `google-chrome-stable` the tests write, with Chrome's arguments,
which it ignores but reports. The environment says the rest:

    FAKE_CHROME_SOCKET  the Unix socket to connect to (the first line sent is JSON:
                        {"pid", "argv"}; NUL-framed CDP both ways after it)
    FAKE_CHROME_LOG     a file each event is appended to as "<event> <pid>": start, term
                        (SIGTERM received), eof (the pipe closed), exit
    FAKE_CHROME_MODE    '' (as Chrome), 'stubborn' (SIGTERM and the pipe's end ignored:
                        only SIGKILL ends it), or 'exit:N' (exit at once with status N)
"""

import json
import os
import select
import signal
import socket
import sys
import time

PIPE_IN, PIPE_OUT = 3, 4  # what Chrome reads commands from, and writes answers to


def log(event):
    path = os.environ.get('FAKE_CHROME_LOG')
    if path:
        with open(path, 'a', encoding='utf-8') as file:
            file.write(f'{event} {os.getpid()}\n')


def write_all(fd, data):
    while data:
        data = data[os.write(fd, data):]


def main():
    mode = os.environ.get('FAKE_CHROME_MODE', '')
    if mode.startswith('exit:'):
        log('exit')
        sys.exit(int(mode[len('exit:'):]))
    stubborn = mode == 'stubborn'

    def on_term(_signum, _frame):
        log('term')
        if not stubborn:
            os._exit(0)
    signal.signal(signal.SIGTERM, on_term)
    log('start')

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(os.environ['FAKE_CHROME_SOCKET'])
    hello = json.dumps({'pid': os.getpid(), 'argv': sys.argv[1:]}) + '\n'
    sock.sendall(hello.encode())
    sources = [PIPE_IN, sock]
    while sources:
        try:
            ready, _, _ = select.select(sources, [], [])
        except InterruptedError:
            continue
        if PIPE_IN in ready:
            data = os.read(PIPE_IN, 65536)
            if data:
                sock.sendall(data)
            else:
                log('eof')
                sources.remove(PIPE_IN)
                if not stubborn:
                    break  # the Engine let go of the pipe: Chrome closes
        if sock in ready:
            data = sock.recv(65536)
            if data:
                write_all(PIPE_OUT, data)
            else:
                break  # the fake browser hung up: this Chrome is gone
    if stubborn:
        while True:  # wedged: only SIGKILL ends it
            time.sleep(60)
    log('exit')


if __name__ == '__main__':
    main()
