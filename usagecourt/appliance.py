"""Record measured traits of edge appliances received by a facility."""
from .store import one

SCHEMA = """
CREATE TABLE IF NOT EXISTS appliances(
    serial TEXT PRIMARY KEY, running_watts INTEGER NOT NULL,
    clearance_mm INTEGER NOT NULL, shock_g INTEGER NOT NULL
);
"""
TABLES = ('appliances',)


def observed(db, serial):
    return one(db, 'SELECT * FROM appliances WHERE serial=?', (serial,))


def handle(db, command):
    if command['op'] != 'record':
        return {'decision': 'unknown_operation'}
    serial = command['serial']
    values = tuple(command[key] for key in ('running_watts', 'clearance_mm', 'shock_g'))
    if not isinstance(serial, str) or not serial.strip() or any(type(v) is not int or v < 0 for v in values):
        return {'decision': 'invalid_appliance_observation'}
    db.execute("""
        INSERT INTO appliances(serial,running_watts,clearance_mm,shock_g) VALUES(?,?,?,?)
        ON CONFLICT(serial) DO UPDATE SET running_watts=excluded.running_watts,
            clearance_mm=excluded.clearance_mm,shock_g=excluded.shock_g
    """, (serial, *values))
    return {'decision': 'appliance_observed'}
