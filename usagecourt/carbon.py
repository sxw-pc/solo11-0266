"""Produce simulated cluster emissions declarations from sealed meter cases."""
from .store import one, rows

SCHEMA = '''
CREATE TABLE IF NOT EXISTS carbon_factors(
  id TEXT PRIMARY KEY, cluster TEXT NOT NULL, start INTEGER NOT NULL,
  end INTEGER NOT NULL, grams_per_unit INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS carbon_declarations(
  id TEXT PRIMARY KEY, meter_case TEXT NOT NULL UNIQUE,
  grams INTEGER NOT NULL, factor TEXT NOT NULL, status TEXT NOT NULL);
'''
TABLES = ('carbon_factors', 'carbon_declarations')


def handle(db, command):
    op = command['op']
    if op == 'factor':
        start, end = command['start'], command['end']
        if start >= end or command['grams_per_unit'] < 0:
            return {'decision': 'invalid_factor'}
        clash = one(db, 'SELECT id FROM carbon_factors WHERE cluster=? AND start<? AND end>?',
                    (command['cluster'], end, start))
        if clash:
            return {'decision': 'factor_overlap', 'factor': clash['id']}
        db.execute('INSERT INTO carbon_factors VALUES(?,?,?,?,?)',
                   (command['id'], command['cluster'], start, end, command['grams_per_unit']))
        return {'decision': 'factor_registered'}
    if op == 'declare':
        case = one(db, '''SELECT m.*, s.cluster FROM meter_cases m JOIN samples s
            ON s.id=m.sample WHERE m.id=? AND m.tenant=? AND m.status='sealed' ''',
                   (command['case'], command['tenant']))
        if not case:
            return {'decision': 'unsealed_case'}
        prior = one(db, 'SELECT id FROM carbon_declarations WHERE meter_case=?', (case['id'],))
        if prior:
            return {'decision': 'already_declared', 'declaration': prior['id']}
        factor = one(db, 'SELECT * FROM carbon_factors WHERE cluster=? AND start<=? AND end>=?',
                     (case['cluster'], case['start'], case['ends']))
        if not factor:
            return {'decision': 'factor_gap'}
        grams = case['quantity'] * factor['grams_per_unit']
        db.execute('INSERT INTO carbon_declarations VALUES(?,?,?,?,?)',
                   (command['id'], case['id'], grams, factor['id'], 'declared'))
        return {'decision': 'declared', 'grams': grams, 'factor': factor['id']}
    if op == 'inspect':
        declarations = rows(db, '''SELECT d.*, CASE WHEN m.status='sealed' THEN d.status ELSE 'review' END AS effective
            FROM carbon_declarations d JOIN meter_cases m ON m.id=d.meter_case
            WHERE m.tenant=? ORDER BY d.id''', (command['tenant'],))
        return {'decision': 'carbon_statement', 'declarations': declarations}
    return {'decision': 'unknown_operation'}
