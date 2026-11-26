"""Check collector delivery coverage separately from invoiceable quantities."""
from .store import one, rows

SCHEMA = '''
CREATE TABLE IF NOT EXISTS inspections(id TEXT PRIMARY KEY, cluster TEXT NOT NULL,
   start INTEGER NOT NULL, end INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS inspection_packets(inspection TEXT NOT NULL, position INTEGER NOT NULL,
   sample TEXT NOT NULL, PRIMARY KEY(inspection, position));
'''
TABLES = ('inspections', 'inspection_packets')


def gaps(start, end, positions):
    missing = []
    for position in positions:
        if start < position:
            missing.append([start, position])
        start = position+1
    if start < end:
        missing.append([start, end])
    return missing


def handle(db, cmd):
    op = cmd['op']
    if op == 'assign':
        if not 0 <= cmd['start'] < cmd['end'] or not cmd['cluster']:
            return {'decision': 'invalid_inspection'}
        db.execute('INSERT INTO inspections VALUES(?,?,?,?)', (cmd['id'], cmd['cluster'], cmd['start'], cmd['end']))
        return {'decision': 'inspection_assigned'}
    batch = one(db, 'SELECT * FROM inspections WHERE id=?', (cmd['inspection'],))
    if not batch:
        return {'decision': 'unknown_inspection'}
    if op == 'observe':
        sample = one(db, 'SELECT id,cluster FROM samples WHERE id=?', (cmd['sample'],))
        if not sample or sample['cluster'] != batch['cluster'] or not batch['start'] <= cmd['position'] < batch['end']:
            return {'decision': 'inspection_evidence_denied'}
        key = batch['id'], cmd['position']
        old = one(db, 'SELECT sample FROM inspection_packets WHERE inspection=? AND position=?', key)
        if old:
            return {'decision': 'already_recorded' if old['sample'] == sample['id'] else 'position_conflict'}
        db.execute('INSERT INTO inspection_packets VALUES(?,?,?)', (*key, sample['id']))
        return {'decision': 'position_recorded'}
    if op == 'inspect':
        positions = rows(db, 'SELECT position FROM inspection_packets WHERE inspection=? ORDER BY position', (batch['id'],))
        missing = gaps(batch['start'], batch['end'], (r['position'] for r in positions))
        return {'decision': 'collection_incomplete' if missing else 'collection_complete', 'gaps': missing}
    return {'decision': 'unknown_operation'}
