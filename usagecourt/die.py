"""An offline memory array with one-time programmable row and column substitutions."""
import json

from .store import one

SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_dies(id TEXT PRIMARY KEY, spec TEXT NOT NULL,
    image TEXT NOT NULL, revision INTEGER NOT NULL);
"""


def get(db, name):
    row = one(db, 'SELECT * FROM memory_dies WHERE id=?', (name,))
    if row:
        for field in ('spec', 'image'):
            row[field] = json.loads(row[field])
    return row


def address(image, logical):
    r, c = logical
    return (image['rows'].get(str(r),r),image['columns'].get(str(c),c))


def validate_axis(mapping, original, spares):
    return (isinstance(mapping,dict)
            and all(k in {str(i) for i in original} and type(v) is int and v in spares
                    for k,v in mapping.items())
            and len(set(mapping.values())) == len(mapping))


def valid_spec(spec):
    def axis(normal, spares):
        return (isinstance(normal,list) and isinstance(spares,list) and bool(normal)
                and all(type(n) is int and n>=0 for n in normal+spares)
                and len(normal+spares)==len(set(normal+spares)))

    def coord(value, rows, cols):
        return (isinstance(value,list) and len(value)==2 and all(type(v) is int for v in value)
                and value[0] in rows and value[1] in cols)

    try:
        nr,sr,nc,sc = (spec[k] for k in ('rows','spare_rows','columns','spare_columns'))
        return (axis(nr,sr) and axis(nc,sc)
                and all(coord(cell,nr+sr,nc+sc) for cell in spec['stuck_zero'])
                and all(len(pair)==2 and pair[0]!=pair[1]
                        and all(coord(cell,nr+sr,nc+sc) for cell in pair)
                        for pair in spec['couplings']))
    except (AttributeError, KeyError, TypeError):
        return False


def handle(db, command):
    op = command['op']
    name = command['die']
    row = get(db,name)
    if op == 'install':
        if row or not valid_spec(command['spec']):
            return {'decision':'invalid_die'}
        db.execute('INSERT INTO memory_dies VALUES(?,?,?,0)',
                   (name,json.dumps(command['spec'],sort_keys=True),'{"columns":{},"rows":{}}'))
        return {'decision':'installed','revision':0}
    if not row:
        return {'decision':'unknown_die'}
    if op == 'read':
        return {'decision':'read','revision':row['revision'],'image':row['image']}
    if op != 'burn':
        return {'decision':'unknown_operation'}
    image = command['image']
    spec = row['spec']
    if (not isinstance(image,dict) or set(image)!={'rows','columns'}
            or not validate_axis(image['rows'],spec['rows'],spec['spare_rows'])
            or not validate_axis(image['columns'],spec['columns'],spec['spare_columns'])):
        return {'decision':'invalid_image'}
    for axis in ('rows','columns'):
        if any(image[axis].get(origin)!=target for origin,target in row['image'][axis].items()):
            return {'decision':'fuse_cannot_change'}
    revision = row['revision'] + int(image != row['image'])
    db.execute('UPDATE memory_dies SET image=?,revision=? WHERE id=?',
               (json.dumps(image,sort_keys=True),revision,name))
    return {'decision':'burned','revision':revision,'image':image}
