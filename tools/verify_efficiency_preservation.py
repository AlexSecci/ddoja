"""Compare every original production DB value against the saved goal baseline."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3

ROOT=Path(__file__).resolve().parents[1]


def audit(output):
    pointer=json.loads((ROOT/'data/experiments/five-area-improvements/current.json').read_text())
    baseline=Path(pointer['baseline'])/'pipeline.sqlite'
    with sqlite3.connect(baseline.as_uri()+'?mode=ro',uri=True) as old_db,sqlite3.connect((ROOT/'data/pipeline.sqlite').as_uri()+'?mode=ro',uri=True) as current_db:
        old_db.execute('BEGIN');current_db.execute('BEGIN');tables=[]
        for (name,) in old_db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
            quoted='"'+name.replace('"','""')+'"'
            try:
                old=old_db.execute('SELECT rowid,* FROM '+quoted+' ORDER BY rowid').fetchall()
                current=current_db.execute('SELECT rowid,* FROM '+quoted+' ORDER BY rowid').fetchall()
            except sqlite3.OperationalError:
                old=old_db.execute('SELECT * FROM '+quoted).fetchall();current=current_db.execute('SELECT * FROM '+quoted).fetchall()
            tables.append({'table':name,'baseline_rows':len(old),'current_rows':len(current),'all_original_values_equal':old==current,
                'baseline_sha256':hashlib.sha256(repr(old).encode()).hexdigest(),'current_sha256':hashlib.sha256(repr(current).encode()).hexdigest()})
        result={'all_tables_unchanged':all(t['all_original_values_equal'] for t in tables),'tables':tables,
                'database_quick_check':current_db.execute('PRAGMA quick_check').fetchone()[0],
                'active_workers':current_db.execute('SELECT count(*) FROM agent_workers WHERE turn_active=1').fetchone()[0],
                'campaigns':[{'company':r[0],'status':r[1],'stop_requested':r[2]} for r in current_db.execute('SELECT company_id,status,stop_requested FROM pipelines')]}
    output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(result,indent=2),encoding='utf-8')
    if not result['all_tables_unchanged'] or result['database_quick_check']!='ok' or result['active_workers']:
        raise RuntimeError('Production preservation/idle audit did not pass; inspect the report without resuming work.')
    return {'preserved_tables':len(tables),'all_original_values_equal':True,'active_workers':0,'database_quick_check':'ok'}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args();print(json.dumps(audit(args.output)))
