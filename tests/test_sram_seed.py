import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from usagecourt.store import connect, replay, run

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures/sram_install.json'


class SramSeedTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = str(Path(tmp.name) / 'die.sqlite')
        self.db = connect(self.path)
        self.addCleanup(self.db.close)
        self.install = json.loads(FIXTURE.read_text())[0]
        self.assertEqual(run(self.db,'die',self.install)['decision'],'installed')
        self.number = 0

    def event(self, subsystem, **data):
        self.number += 1
        return run(self.db, subsystem, dict(event_id=f'die-{self.number}',die='D17',**data))

    def burn(self, rows, columns):
        return self.event('die',op='burn',image={'rows':rows,'columns':columns})

    def measure(self, pattern, rows=None):
        return self.event('patterns',op='test',pattern=pattern,rows=[0,1,2] if rows is None else rows)

    def test_static_repair_passes_alone_but_reintroduces_coupling(self):
        self.assertEqual(self.burn({'0':3},{'1':3,'2':4})['decision'],'burned')
        self.assertEqual(self.measure('isolated')['decision'],'pattern_passed')
        bad = self.measure('shared')
        self.assertEqual(bad['failures'],[{'kind':'coupled_read','aggressor':[2,1],
                                          'cell':[1,1],'physical':[1,3]}])
        self.assertTrue(replay(self.path)['verified'])

    def test_shared_read_becomes_clean_under_different_fuse_image(self):
        before = self.measure('shared')
        self.assertEqual(before['revision'],0)
        self.assertEqual(len(before['failures']),3)
        self.assertEqual(self.burn({'2':3},{'0':3,'1':4})['revision'],1)
        passed = self.measure('shared')
        self.assertEqual((passed['revision'],passed['decision'],passed['cells']), (1,'pattern_passed',9))
        self.assertEqual(self.burn({'0':3},{'1':3,'2':4})['decision'],'fuse_cannot_change')
        self.assertTrue(replay(self.path)['verified'])

    def test_one_free_spare_supports_six_cells_with_fixed_address_rows(self):
        small = self.burn({'0':3},{'1':3})
        self.assertEqual(small['image']['columns'], {'1':3})
        checked = self.measure('shared',[0,1])
        self.assertEqual((checked['decision'],checked['cells']),('pattern_passed',6))
        self.assertEqual(self.measure('shared')['decision'],'pattern_failed')
