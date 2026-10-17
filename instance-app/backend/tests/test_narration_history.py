import tempfile
import unittest
from pathlib import Path

from backend.host.local.store import LocalStore
from backend.host.services import AppServices


class NarrationHistoryTests(unittest.TestCase):
    def test_saved_versions_round_trip_through_sqlite(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStore(Path(directory) / 'business.sqlite3')
            project = store.create_project('test-workspace', 'Test')
            app = AppServices.__new__(AppServices)
            saved = []
            for text in ['first version', 'regenerated version']:
                row = store.add_narration(
                    project_id=project['id'], title='Narration',
                    segments=[{'node_id': 'root-a', 'title': 'Root',
                               'text': text, 'duration_seconds': 5}],
                    provider='fake', model='offline',
                )
                saved.append(app.narration_public(row))
            loaded = [app.narration_public(row)
                      for row in store.list_narrations(project['id'])]
            self.assertEqual(len(loaded), 2)
            self.assertEqual({item['id'] for item in loaded},
                             {item['id'] for item in saved})
            self.assertEqual({item['segments'][0]['text'] for item in loaded},
                             {'first version', 'regenerated version'})
            self.assertTrue(all(item['segments'][0]['node_id'] == 'root-a'
                                for item in loaded))

    def test_in_memory_response_stays_readable(self):
        app = AppServices.__new__(AppServices)
        item = app.narration_public({
            'id': 'n', 'title': 'Narration', 'provider': 'fake', 'model': 'offline',
            'segments': [{'node_id': 'a', 'text': 'Explanation'}],
            'created_at': '2026-10-17T19:00:00Z',
        })
        self.assertEqual(item['segments'][0]['text'], 'Explanation')


if __name__ == '__main__':
    unittest.main()
