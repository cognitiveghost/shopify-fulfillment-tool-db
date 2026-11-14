"""Worker: a started worker delivers even when its caller kept no reference."""

import gc
import threading

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication

from gui.worker import Worker


def test_an_unheld_worker_still_delivers_its_result_and_finished(qtbot):
    release = threading.Event()
    got = []
    pool = QThreadPool()

    worker = Worker(lambda: release.wait(5) and "done")
    worker.signals.result.connect(got.append)
    worker.signals.finished.connect(lambda: got.append("finished"))
    pool.start(worker)
    # The caller drops it while it runs: CI caught "Signal source has been
    # deleted" from exactly this.
    del worker
    gc.collect()
    release.set()

    assert pool.waitForDone(5000)
    qtbot.waitUntil(lambda: "finished" in got, timeout=2000)
    assert got == ["done", "finished"]


def test_a_finished_worker_is_let_go():
    import gui.worker as module

    worker = Worker(lambda: None)
    worker.run()  # same thread: finished is delivered directly
    QApplication.processEvents()
    assert id(worker) not in module._alive
