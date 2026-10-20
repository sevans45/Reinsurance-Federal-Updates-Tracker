from contextlib import closing
import base64
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch
import reinsurance_tracker as t
from restore_state import restore

INFO = {'type': 'service_account', 'client_email': 'test@example.invalid',
        'private_key': 'dummy-private-key', 'token_uri': 'https://oauth2.googleapis.com/token'}


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.db = str(Path(self.temp.name) / 'state.db')
        self.settings = patch.multiple(t, DB_FILE=self.db, SHEETS_ENABLED=True,
                                      SHEETS_SPREADSHEET_ID='test', GOOGLE_CREDENTIALS_JSON=json.dumps(INFO))
        self.settings.start()
        self.addCleanup(self.settings.stop)
        self.conn = t.init_database()
        self.addCleanup(self.conn.close)
        t.add_update(self.conn, 'one', 'test', '=untrusted()', 'https://example.invalid/one', '2026-10-17', ['insurance'])
        self.sheet = MagicMock()
        self.sheet.col_values.return_value = []
        self.gs = patch.object(t, 'gspread')
        self.creds = patch.object(t, 'Credentials')
        gs = self.gs.start()
        self.creds.start()
        self.addCleanup(self.gs.stop)
        self.addCleanup(self.creds.stop)
        gs.authorize.return_value.open_by_key.return_value.worksheet.return_value = self.sheet

    def test_credentials_raw_base64_bom_and_wrapped(self):
        raw = json.dumps(INFO)
        for value in (raw, '\ufeff'+raw, base64.b64encode(raw.encode()).decode(),
                      '\n'.join(base64.b64encode(raw.encode()).decode()[i:i+20] for i in range(0, len(base64.b64encode(raw.encode())), 20))):
            self.assertEqual(t.load_google_credentials(value), INFO)

    def test_invalid_credentials_are_sanitized(self):
        for value in ('', 'not-json-secret', '////', '{}', '[]'):
            with self.assertRaises(ValueError) as err:
                t.load_google_credentials(value)
            self.assertNotIn('not-json-secret', str(err.exception))

    def test_export_failure_remains_pending(self):
        self.sheet.append_rows.side_effect = Exception('sensitive-response')
        with self.assertRaises(RuntimeError) as err:
            t.append_to_google_sheet(t.pending_sheet_updates(self.conn), self.conn)
        self.assertNotIn('sensitive-response', str(err.exception))
        self.assertEqual(len(t.pending_sheet_updates(self.conn)), 1)

    def test_success_uses_raw_and_clears_pending(self):
        t.append_to_google_sheet(t.pending_sheet_updates(self.conn), self.conn)
        self.assertEqual(t.pending_sheet_updates(self.conn), [])
        self.assertEqual(self.sheet.append_rows.call_args.kwargs['value_input_option'], 'RAW')

    def test_retry_reconciles_existing_url_after_uncertain_write(self):
        self.sheet.col_values.return_value = ['URL', 'https://example.invalid/one']
        t.append_to_google_sheet(t.pending_sheet_updates(self.conn), self.conn)
        self.sheet.append_rows.assert_not_called()
        self.assertEqual(t.pending_sheet_updates(self.conn), [])

    def test_partial_batches_keep_unconfirmed_items_pending(self):
        for number in range(1, 201):
            t.add_update(self.conn, str(number), 'test', 'title', f'https://example.invalid/{number}', '2026-10-17', [])
        self.sheet.append_rows.side_effect = [None, Exception('timeout')]
        with self.assertRaises(RuntimeError):
            t.append_to_google_sheet(t.pending_sheet_updates(self.conn), self.conn)
        self.assertEqual(len(t.pending_sheet_updates(self.conn)), 101)

    def test_disabled_export_stays_pending(self):
        with patch.object(t, 'SHEETS_ENABLED', False):
            t.append_to_google_sheet(t.pending_sheet_updates(self.conn), self.conn)
        self.assertEqual(len(t.pending_sheet_updates(self.conn)), 1)

    def test_legacy_database_migration_and_duplicate_tracking(self):
        legacy = str(Path(self.temp.name) / 'legacy.db')
        with closing(sqlite3.connect(legacy)) as c:
            c.execute('CREATE TABLE updates(id TEXT PRIMARY KEY, source TEXT, title TEXT, url TEXT, date_published TEXT, date_added TEXT, keywords TEXT)')
            c.execute("INSERT INTO updates VALUES ('old','source','title','https://old','2026-10-17','2026-10-17','insurance')")
            c.commit()
        with patch.object(t, 'DB_FILE', legacy):
            c = t.init_database()
            try:
                self.assertTrue(t.is_duplicate(c, 'old'))
                self.assertEqual(len(t.pending_sheet_updates(c)), 1)
            finally:
                c.close()

    def test_restore_preserves_database_and_rejects_bad_zip(self):
        archive = Path(self.temp.name) / 'state.zip'
        dest = Path(self.temp.name) / 'restored.db'
        with zipfile.ZipFile(archive, 'w') as z:
            z.write(self.db, 'reinsurance_updates.db')
        restore(archive, dest)
        with closing(sqlite3.connect(dest)) as c:
            self.assertTrue(t.is_duplicate(c, 'one'))
        original = dest.read_bytes()
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('../reinsurance_updates.db', 'bad')
        with self.assertRaises(ValueError):
            restore(archive, dest)
        self.assertEqual(dest.read_bytes(), original)

    def test_main_fails_export_even_with_no_new_updates(self):
        with patch.object(t, 'fetch_congressional_bills', return_value=[]), patch.object(t, 'fetch_federal_register', return_value=[]), patch.object(t, 'fetch_rss_feeds', return_value=[]), patch.object(t, 'append_to_google_sheet', side_effect=RuntimeError('failed')):
            with self.assertRaises(RuntimeError):
                t.main()


if __name__ == '__main__':
    unittest.main()
