"""Log rotation must not turn a healthy engine scenario into a harness failure."""
import builtins
import threading
from pathlib import Path

from harness.mailsync import MailsyncProcess


def test_rotation_between_stat_and_open_retries_without_losing_position(tmp_path, monkeypatch):
    process = object.__new__(MailsyncProcess)
    process.config_dir = tmp_path
    process.account = {'id': 'fixture'}
    process._log_lock = threading.Lock()
    process._log_pos = 0
    received = []
    process._ingest_log = received.append
    log = process.log_path
    log.write_bytes(b'a complete line\n')
    original = builtins.open
    attempts = 0
    def rotated_open(path, *args, **kwargs):
        nonlocal attempts
        if path == log:
            attempts += 1
            if attempts == 1:
                raise FileNotFoundError('logger is rotating')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(builtins, 'open', rotated_open)
    process._drain_log()
    assert process._log_pos == 0
    assert not received
    process._drain_log()
    assert received == [b'a complete line']
    assert process._log_pos == len(b'a complete line\n')
