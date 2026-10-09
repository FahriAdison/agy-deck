"""AGY Web v3 - real PTY terminal sessions.

Each websocket gets its own login shell on a real pseudo-terminal, so
interactive programs (vim, nano, ssh, htop, sudo prompts) actually work.
Output streams back as raw bytes (ANSI intact); the frontend (xterm.js)
renders it.
"""
import array
import errno
import fcntl
import os
import pty
import queue
import select
import signal
import struct
import termios
import threading

from . import config


class PtySession:
    def __init__(self, out_queue, cols=100, rows=32):
        self.q = out_queue
        self.cols = cols
        self.rows = rows
        self._stop = threading.Event()
        pid, fd = pty.fork()
        if pid == 0:
            # child: the shell
            try:
                os.environ.setdefault("TERM", "xterm-256color")
                os.chdir(config.WORKSPACE_DIR)
                os.execvp("bash", ["bash", "--login"])
            except Exception:
                os._exit(1)
        self.pid = pid
        self.fd = fd
        self._set_winsize(cols, rows)
        self.reader = threading.Thread(target=self._read_loop, daemon=True,
                                       name="pty-reader")
        self.reader.start()

    def _set_winsize(self, cols, rows):
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
        except Exception:
            pass

    def resize(self, cols, rows):
        self.cols, self.rows = cols, rows
        self._set_winsize(cols, rows)

    def write(self, data: bytes):
        try:
            os.write(self.fd, data)
        except OSError:
            pass

    def _read_loop(self):
        try:
            while not self._stop.is_set():
                r, _, _ = select.select([self.fd], [], [], 0.2)
                if not r:
                    continue
                try:
                    data = os.read(self.fd, 65536)
                except OSError as e:
                    if e.errno == errno.EIO:
                        break  # child exited
                    continue
                if not data:
                    break
                self.q.put({"t": "term_output", "data": data})
        finally:
            self.q.put({"t": "term_exit"})

    def alive(self):
        try:
            os.kill(self.pid, 0)
            return True
        except OSError:
            return False

    def close(self):
        self._stop.set()
        try:
            os.kill(self.pid, signal.SIGHUP)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
