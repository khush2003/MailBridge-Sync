"""Real engine retention and peer import, independent of the IMAP cache."""
import hashlib
import json
import shutil
from pathlib import Path
from harness.mailsync import MailsyncProcess, account_json
from harness.servers.fake import FakeServer
from harness.mailgen import message

import os
BIN = Path(os.environ.get('MAILSYNC_BIN', Path(__file__).resolve().parents[1] / 'build' / 'mailsync'))


def command(proc, operation, **args):
    ident = f'mb-{len(proc.state.deltas)}'
    proc.send({'type': 'mailbridge', 'id': ident, 'operation': operation, **args})
    def answer():
        return next((m for d in proc.state.deltas if d.model_class == 'MailBridgeResult'
                     for m in d.models if m.get('id') == ident), None)
    proc.wait_for(lambda: answer() is not None, 20, what=f'archive {operation}')
    result = answer()
    assert 'error' not in result, result
    return result['result']


def test_old_mail_and_attachments_survive_expunge_and_import(tmp_path):
    server = FakeServer().start()
    a = b = None
    try:
        raw = message(41001, age_days=9000, attachment=('old-report.bin', b'old binary\x00payload'))
        uid = server.append('INBOX', raw, flags=())
        a = MailsyncProcess(account_json(**server.account_kwargs()), tmp_path / 'a', binary=BIN,
                            env={'MAILBRIDGE_ARCHIVE': '1'})
        a.start()
        def retained():
            return list((a.config_dir / 'mailbridge' / 'records').glob('*.json'))
        a.wait_for(lambda: len(retained()) == 1, 60, what='full old-message capture')
        records = command(a, 'list')['records']
        assert len(records) == 1
        record = records[0]
        assert record['digest'] == hashlib.sha256(raw).hexdigest()
        assert record['unread'] is True
        server.expunge('INBOX', [uid])
        a.send({'type': 'wake-workers'})
        a.wait_quiescent(timeout=60)
        assert command(a, 'list')['records'][0]['key'] == record['key']
        # A fresh PC with a different native account id must recover complete mail after server cleanup.
        b = MailsyncProcess(account_json(**server.account_kwargs(), account_id='c0ffee-second-pc'),
                            tmp_path / 'b', binary=BIN, env={'MAILBRIDGE_ARCHIVE': '1'})
        b.start()
        b.wait_quiescent(timeout=60)
        blobs = b.config_dir / 'mailbridge' / 'blobs'
        blobs.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(a.config_dir / 'mailbridge' / 'blobs' / f"{record['digest']}.eml",
                        blobs / f"{record['digest']}.eml")
        imported = command(b, 'import', descriptor=record, state={'unread': False, 'starred': True})
        second = command(b, 'list')['records'][0]
        assert second['key'] == record['key']
        assert second['starred'] and not second['unread']
        import sqlite3
        with sqlite3.connect(b.db_path) as db:
            assert db.execute('SELECT COUNT(*) FROM File').fetchone()[0] == 1
            body = db.execute('SELECT value FROM MessageBody WHERE id = ?', (imported['messageId'],)).fetchone()[0]
            assert 'body of message 41001' in body
        assert any(p.read_bytes() == b'old binary\x00payload' for p in (b.config_dir / 'files').rglob('*') if p.is_file())
    finally:
        if a: a.stop()
        if b: b.stop()
        server.stop()



def test_cache_rebuild_preserves_read_flag_and_folder_without_cloud(tmp_path):
    server = FakeServer().start()
    proc = None
    try:
        raw = message(41002)
        uid = server.append('INBOX', raw, flags=())
        account = account_json(**server.account_kwargs())
        proc = MailsyncProcess(account, tmp_path, binary=BIN, env={'MAILBRIDGE_ARCHIVE': '1'})
        proc.start()
        proc.wait_for(lambda: len(list((proc.config_dir / 'mailbridge' / 'records').glob('*.json'))) == 1,
                      60, what='retained message')
        record = command(proc, 'list')['records'][0]
        command(proc, 'import', descriptor=record, state={'unread': False, 'starred': True, 'folder': 'Projects'})
        server.expunge('INBOX', [uid])
        proc.stop()
        for name in ('edgehill.db', 'edgehill.db-wal', 'edgehill.db-shm'):
            (proc.config_dir / name).unlink(missing_ok=True)
        proc = MailsyncProcess(account, tmp_path, binary=BIN, env={'MAILBRIDGE_ARCHIVE': '1'})
        proc.start()
        proc.wait_for(lambda: command(proc, 'list')['records'], 30, what='recovered archive')
        record = command(proc, 'list')['records'][0]
        assert not record['unread'] and record['starred']
        assert record['folder'] == 'Projects'
    finally:
        if proc: proc.stop()
        server.stop()


def test_peer_flags_are_written_to_imap_and_server_deletes_still_retain(tmp_path):
    server = FakeServer().start()
    proc = None
    try:
        raw = message(41003)
        uid = server.append('INBOX', raw, flags=())
        proc = MailsyncProcess(account_json(**server.account_kwargs()), tmp_path, binary=BIN,
                               env={'MAILBRIDGE_ARCHIVE': '1'})
        proc.start()
        proc.wait_for(lambda: len(list((proc.config_dir / 'mailbridge' / 'records').glob('*.json'))) == 1,
                      60, what='capture')
        record = command(proc, 'list')['records'][0]
        command(proc, 'import', descriptor=record, state={'unread': False, 'starred': True})
        proc.wait_for(lambda: '\\Seen' in server.store.get('INBOX').by_uid(uid).flags and
                              '\\Flagged' in server.store.get('INBOX').by_uid(uid).flags,
                      30, what='peer flags on IMAP')
        server.expunge('INBOX', [uid]); proc.wake()
        proc.wait_quiescent(timeout=60)
        record = command(proc, 'list')['records'][0]
        assert not record['unread'] and record['starred']
    finally:
        if proc: proc.stop()
        server.stop()


def test_smtp_success_is_retained_before_sent_folder_append(tmp_path):
    server = FakeServer(smtp=True).start()
    proc = None
    try:
        account = account_json(**server.account_kwargs())
        proc = MailsyncProcess(account, tmp_path, binary=BIN, env={'MAILBRIDGE_ARCHIVE': '1'})
        proc.start(); proc.wait_quiescent(timeout=60)
        # Any append is refused after SMTP succeeds: the local Sent archive must still exist.
        def refuse_append(session, cmd, rest):
            if cmd == 'APPEND': session.reject_next = ('OVERQUOTA', 'Company mailbox is full')
        server.imap.add_hook('before_command', refuse_append)
        proc.queue_task({'__cls': 'SendDraftTask', 'draft': {
            'id': 'draft-mailbridge-test', 'aid': account['id'], 'v': 0,
            'hMsgId': 'mailbridge-sent@example.test', 'subject': 'Sent despite full mailbox',
            'body': '<p>Retain this outgoing message</p>', 'plaintext': False,
            'from': [{'name': 'Test', 'email': 'test@example.test'}],
            'to': [{'name': 'Colleague', 'email': 'colleague@example.test'}], 'cc': [], 'bcc': [],
            'replyTo': [], 'files': [], 'rthMsgId': '', 'fwdMsgId': '',
        }, 'perRecipientBodies': None})
        proc.wait_for(lambda: len(server.sent_messages()) == 1, 30, what='SMTP acceptance')
        proc.wait_for(lambda: list((proc.config_dir / 'mailbridge' / 'records').glob('*.json')),
                      30, what='local sent copy before IMAP append')
        record = command(proc, 'list')['records'][0]
        assert record['role'] == 'sent'
        assert b'Retain this outgoing message' in (proc.config_dir / 'mailbridge' / 'blobs' / f"{record['digest']}.eml").read_bytes()
        proc.wait_for_log('Could not place a message into the Sent folder', timeout=30)
        assert len(server.sent_messages()) == 1
    finally:
        if proc: proc.stop()
        server.stop()
