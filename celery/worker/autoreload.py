# -*- coding: utf-8 -*-
"""
    celery.worker.autoreload
    ~~~~~~~~~~~~~~~~~~~~~~~~

    This module implements automatic module reloading
"""
from __future__ import absolute_import
from __future__ import with_statement

import errno
import hashlib
import os
import select
import sys
import time

from collections import defaultdict

from .. import current_app
from ..abstract import StartStopComponent
from ..utils.threads import bgThread, Event

try:
    import pyinotify
    _ProcessEvent = pyinotify.ProcessEvent
except ImportError:
    pyinotify = None        # noqa
    _ProcessEvent = object  # noqa


class WorkerComponent(StartStopComponent):
    name = "worker.autoreloader"
    requires = ("pool", )

    def __init__(self, w, autoreload=None, **kwargs):
        self.enabled = w.autoreload = autoreload
        w.autoreloader = None

    def create(self, w):
        w.autoreloader = self.instantiate(w.autoreloader_cls,
                                          modules=w.autoreload,
                                          logger=w.logger)
        return w.autoreloader


def file_hash(filename, algorithm="md5"):
    hobj = hashlib.new(algorithm)
    with open(filename, "rb") as f:
        for chunk in iter(lambda: f.read(2 ** 20), ''):
            hobj.update(chunk)
    return hobj.digest()


class BaseMonitor(object):

    def __init__(self, files, on_change=None, shutdown_event=None,
            interval=0.5):
        self.files = files
        self.interval = interval
        self._on_change = on_change
        self.modify_times = defaultdict(int)
        self.shutdown_event = shutdown_event or Event()

    def start(self):
        raise NotImplementedError("Subclass responsibility")

    def stop(self):
        pass

    def on_change(self, modified):
        if self._on_change:
            return self._on_change(modified)


class StatMonitor(BaseMonitor):
    """File change monitor based on the ``stat`` system call."""

    def _mtimes(self):
        return ((f, self._mtime(f)) for f in self.files)

    def _maybe_modified(self, f, mt):
        return mt is not None and self.modify_times[f] != mt

    def start(self):
        while not self.shutdown_event.is_set():
            modified = dict((f, mt) for f, mt in self._mtimes()
                                if self._maybe_modified(f, mt))
            if modified:
                self.on_change(modified.keys())
                self.modify_times.update(modified)
            time.sleep(self.interval)

    @staticmethod
    def _mtime(path):
        try:
            return os.stat(path).st_mtime
        except Exception:
            pass


class KQueueMonitor(BaseMonitor):
    """File change monitor based on BSD kernel event notifications"""

    def __init__(self, *args, **kwargs):
        assert hasattr(select, "kqueue")
        super(KQueueMonitor, self).__init__(*args, **kwargs)
        self.filemap = dict((f, None) for f in self.files)

    def start(self):
        self._kq = select.kqueue()
        kevents = []
        for f in self.filemap:
            self.filemap[f] = fd = os.open(f, os.O_RDONLY)

            ev = select.kevent(fd,
                    filter=select.KQ_FILTER_VNODE,
                    flags=select.KQ_EV_ADD |
                            select.KQ_EV_ENABLE |
                            select.KQ_EV_CLEAR,
                    fflags=select.KQ_NOTE_WRITE |
                            select.KQ_NOTE_EXTEND)
            kevents.append(ev)

        events = self._kq.control(kevents, 0)
        while not self.shutdown_event.is_set():
            events = self._kq.control(kevents, 1)
            fds = [e.ident for e in events]
            modified = [k for k, v in self.filemap.iteritems()
                                        if v in fds]
            self.on_change(modified)

    def stop(self):
        self._kq.close()
        for fd in filter(None, self.filemap.values()):
            try:
                os.close(fd)
            except OSError, exc:
                if exc != errno.EBADF:
                    raise
            self.filemap[fd] = None
        self.filemap.clear()


class InotifyMonitor(_ProcessEvent):
    """File change monitor based on Linux kernel `inotify` subsystem"""

    def __init__(self, modules, on_change=None, **kwargs):
        assert pyinotify
        self._modules = modules
        self._on_change = on_change

    def start(self):
        try:
            self._wm = pyinotify.WatchManager()
            self._notifier = pyinotify.Notifier(self._wm)
            for m in self._modules:
                self._wm.add_watch(m, pyinotify.IN_MODIFY)
            self._notifier.loop()
        finally:
            self.close()

    def close(self):
        self._notifier.stop()
        self._wm.close()

    def process_IN_MODIFY(self, event):
        self.on_change(event.pathname)

    def on_change(self, modified):
        if self._on_change:
            return self._on_change(modified)


# kqueue monitor not working properly at this time.
#if hasattr(select, "kqueue"):
#    Monitor = KQueueMonitor
if sys.platform.startswith("linux") and pyinotify:
    Monitor = InotifyMonitor
else:
    Monitor = StatMonitor


class Autoreloader(bgThread):
    """Tracks changes in modules and fires reload commands"""
    Monitor = Monitor

    def __init__(self, modules, monitor_cls=None, logger=None, **kwargs):
        super(Autoreloader, self).__init__()
        self.daemon = True
        self.logger = logger
        files = [sys.modules[m].__file__ for m in modules]
        self.Monitor = monitor_cls or self.Monitor
        self._monitor = self.Monitor(files, self.on_change,
                shutdown_event=self._is_shutdown, **kwargs)
        self._hashes = dict([(f, file_hash(f)) for f in files])

    def body(self):
        try:
            self._monitor.start()
        except OSError, exc:
            if exc.errno not in (errno.EINTR, errno.EAGAIN):
                raise

    def _maybe_modified(self, f):
        digest = file_hash(f)
        if digest != self._hashes[f]:
            self._hashes[f] = digest
            return True
        return False

    def on_change(self, files):
        modified = [f for f in files if self._maybe_modified(f)]
        if modified:
            self.logger.info("Detected modified modules: %s" % (
                    map(self._module_name, modified), ))
            self._reload(map(self._module_name, modified))

    def _reload(self, modules):
        current_app.control.broadcast("pool_restart",
                arguments={"imports": modules, "reload_modules": True})

    def stop(self):
        self._monitor.stop()

    @staticmethod
    def _module_name(path):
        return os.path.splitext(os.path.basename(path))[0]
