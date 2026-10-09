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


def test_local_delete_does_not_publish_deletion_and_survives_cache_reset(tmp_path):
    server = FakeServer().start()
    proc = None
    try:
        uid = server.append('INBOX', message(41004), flags=())
        account = account_json(**server.account_kwargs())
        proc = MailsyncProcess(account, tmp_path, binary=BIN, env={'MAILBRIDGE_ARCHIVE': '1'})
        proc.start()
        proc.wait_for(lambda: list((proc.config_dir / 'mailbridge' / 'records').glob('*.json')),
                      60, what='retained copy')
        record = command(proc, 'list')['records'][0]
        trash = proc.db_folders()['Trash']
        proc.queue_task({'__cls': 'ChangeFolderTask', 'messageIds': [record['messageId']],
                         'folder': {'id': trash['id'], 'path': 'Trash', 'role': 'trash'}})
        def hidden():
            with proc.db() as db:
                row = db.execute('SELECT data FROM Message WHERE id = ?', (record['messageId'],)).fetchone()
                return row and json.loads(row['data']).get('mailbridgeHidden')
        proc.wait_for(hidden, 30, what='local deletion')
        assert command(proc, 'list')['records'][0]['folder'] == 'INBOX'
        command(proc, 'import', descriptor=record, state={'unread': False, 'starred': True, 'folder': 'INBOX'})
        assert hidden(), 'peer state must not restore locally deleted mail'
        server.expunge('INBOX', [uid])
        proc.stop()
        for name in ('edgehill.db', 'edgehill.db-wal', 'edgehill.db-shm'):
            (proc.config_dir / name).unlink(missing_ok=True)
        proc = MailsyncProcess(account, tmp_path, binary=BIN, env={'MAILBRIDGE_ARCHIVE': '1'})
        proc.start()
        proc.wait_for(lambda: command(proc, 'list')['records'], 30, what='archive rebuild')
        assert hidden()
        with proc.db() as db:
            retained = db.execute('SELECT Folder.path FROM MessageFolder JOIN Folder ON Folder.id = MessageFolder.folderId WHERE messageId = ? AND remoteUID = 0', (record['messageId'],)).fetchall()
            assert [r['path'] for r in retained] == ['Retained/Trash']
    finally:
        if proc: proc.stop()
        server.stop()


def test_pst_export_imports_complete_eml_without_uploading_it_to_imap(tmp_path):
    server = FakeServer().start()
    proc = None
    try:
        proc = MailsyncProcess(account_json(**server.account_kwargs()), tmp_path, binary=BIN, env={'MAILBRIDGE_ARCHIVE': '1'})
        proc.start(); proc.wait_quiescent(timeout=60)
        imports = proc.config_dir / 'mailbridge' / 'imports'
        imports.mkdir(parents=True, exist_ok=True)
        filename = '1234567890abcdef1234567890abcdef.eml'
        raw = message(41005, age_days=9000, attachment=('pst-report.bin', b'old PST attachment'))
        (imports / filename).write_bytes(raw)
        command(proc, 'import-file', file=filename, folder='Sent', role='sent', unread=False, starred=True)
        record = command(proc, 'list')['records'][0]
        assert record['role'] == 'sent' and not record['unread'] and record['starred']
        assert record['digest'] == hashlib.sha256(raw).hexdigest()
        assert not server.store.get('Sent').messages
        assert any(p.read_bytes() == b'old PST attachment' for p in (proc.config_dir / 'files').rglob('*') if p.is_file())
    finally:
        if proc: proc.stop()
        server.stop()


def test_encrypted_sync_runs_end_to_end_between_two_real_engines(tmp_path):
    import subprocess
    import threading
    import pytest
    script = Path(__file__).resolve().parents[2] / 'test' / 'mailbridge' / 'native-sync-integration.cjs'
    if not script.exists(): pytest.skip('Full-client integration runner is not present in the standalone engine checkout')
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    server = FakeServer().start()
    processes = {}
    httpd = None
    try:
        uid = server.append('INBOX', message(41006, attachment=('sync.bin', b'full native sync payload')), flags=())
        a = MailsyncProcess(account_json(**server.account_kwargs()), tmp_path / 'a', binary=BIN, env={'MAILBRIDGE_ARCHIVE': '1'})
        processes['a'] = a; a.start()
        a.wait_for(lambda: list((a.config_dir / 'mailbridge' / 'records').glob('*.json')), 60, what='permanent capture')
        server.expunge('INBOX', [uid]); a.wake(); a.wait_quiescent(timeout=60)
        b = MailsyncProcess(account_json(**server.account_kwargs(), account_id='c0ffee-integration-peer'), tmp_path / 'b', binary=BIN, env={'MAILBRIDGE_ARCHIVE': '1'})
        processes['b'] = b; b.start(); b.wait_quiescent(timeout=60)
        class Adapter(BaseHTTPRequestHandler):
            def do_POST(self):
                try:
                    packet = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    operation = packet.pop('operation')
                    result = command(processes[self.path.lstrip('/')], operation, **packet)
                    self.send_response(200)
                except Exception as exc:
                    result = {'error': str(exc)}; self.send_response(500)
                self.send_header('Content-Type', 'application/json'); self.end_headers()
                self.wfile.write(json.dumps(result).encode())
            def log_message(self, *_): pass
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), Adapter)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        script = Path(__file__).resolve().parents[2] / 'test' / 'mailbridge' / 'native-sync-integration.cjs'
        subprocess.run(['node', str(script), f'http://127.0.0.1:{httpd.server_port}', str(a.config_dir / 'mailbridge'), str(b.config_dir / 'mailbridge')], check=True, timeout=120)
        assert any(p.read_bytes() == b'full native sync payload' for p in (b.config_dir / 'files').rglob('*') if p.is_file())
    finally:
        if httpd: httpd.shutdown(); httpd.server_close()
        for proc in processes.values(): proc.stop()
        server.stop()


def test_unreadable_message_does_not_starve_older_mail_or_claim_complete_capture(tmp_path):
    import re
    server = FakeServer().start()
    proc = None
    try:
        server.append('INBOX', message(41100), flags=())
        for ident in range(41101, 41136): server.append('INBOX', message(ident, age_days=1000), flags=())
        failures = []
        def refuse_body(session, cmd, rest):
            rest = rest.decode() if isinstance(rest, bytes) else rest
            if cmd == 'UID FETCH' and re.match(r'^\s*1(?:\s|$)', rest, re.I) and 'BODY.PEEK[]' in rest.upper():
                failures.append(rest)
                session.reject_next = ('SERVERBUG', 'This body cannot currently be fetched')
        server.imap.add_hook('before_command', refuse_body)
        proc = MailsyncProcess(account_json(**server.account_kwargs()), tmp_path, binary=BIN, env={'MAILBRIDGE_ARCHIVE': '1'})
        proc.start()
        proc.wait_for(lambda: len(list((proc.config_dir / 'mailbridge' / 'records').glob('*.json'))) == 35,
                      60, what='older mail retained despite unreadable first message')
        proc.wait_quiescent(timeout=30)
        status = command(proc, 'list')
        assert status['unretained'] == 1
        assert status['mailSyncInitialized'] is True
        assert 1 <= len(failures) <= 4, 'failed body must back off instead of spinning'
    finally:
        if proc: proc.stop()
        server.stop()
