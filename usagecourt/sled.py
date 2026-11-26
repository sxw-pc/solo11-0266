"""Bench measurements for detachable edge-rack trays."""
from .store import one

SCHEMA = """
CREATE TABLE IF NOT EXISTS sled_checks(serial TEXT PRIMARY KEY, grams INTEGER NOT NULL,
    offset_mm INTEGER NOT NULL, connector TEXT NOT NULL, residue_mg INTEGER NOT NULL);
"""
TABLES = ('sled_checks',)


def current(db, serial):
    return one(db, 'SELECT * FROM sled_checks WHERE serial=?', (serial,))


def handle(db, command):
    if command['op'] != 'measure':
        return {'decision': 'unknown_operation'}
    serial, grams, offset, connector, residue = (command[k] for k in (
        'serial', 'grams', 'offset_mm', 'connector', 'residue_mg'))
    if not serial or not connector or type(grams) is not int or grams <= 0 or type(offset) is not int or type(residue) is not int or residue < 0:
        return {'decision': 'invalid_sled_check'}
    db.execute("""
        INSERT INTO sled_checks VALUES(?,?,?,?,?) ON CONFLICT(serial) DO UPDATE SET
        grams=excluded.grams,offset_mm=excluded.offset_mm,connector=excluded.connector,
        residue_mg=excluded.residue_mg
    """, (serial, grams, offset, connector, residue))
    return {'decision': 'sled_measured'}
