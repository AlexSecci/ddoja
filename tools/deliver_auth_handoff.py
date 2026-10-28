"""Deliver one current authorized email token without logging its value."""
import argparse
import getpass
import json
from pathlib import Path
import sys
import warnings
import httpx

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.store import Store
from app.auth_handoffs import deliver,outcome


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker',required=True)
    parser.add_argument('--message-id')
    parser.add_argument('--outcome',choices=['no_fresh_mail','ambiguous_mail','broker_unavailable'])
    args=parser.parse_args();store=Store(ROOT)
    request=json.loads((ROOT/'data/auth-handoffs'/(args.worker+'.request.json')).read_text())
    if args.outcome:
        outcome(store,args.worker,request['request_id'],args.outcome)
        result={'outcome':args.outcome,'worker_id':args.worker,'request_id':request['request_id']}
    else:
        if not args.message_id:parser.error('--message-id is required for token delivery.')
        warnings.simplefilter('error',getpass.GetPassWarning)
        code=getpass.getpass('Current email token (hidden): ').strip()
        result=deliver(store,args.worker,args.message_id,code)
    # The database transition is durable even if the dashboard is restarting.
    try:
        with httpx.Client(base_url='http://127.0.0.1:8765',timeout=3,trust_env=False) as client:
            token=client.get('/api/session').json()['token']
            response=client.post('/api/auth-handoffs/'+args.worker+'/delivered',
                json={'request_id':result['request_id']},headers={'x-pipeline-token':token})
            response.raise_for_status()
    except (httpx.HTTPError,ValueError,KeyError):result['scheduler_notice']='Delivery saved; dashboard wake notification unavailable.'
    print(json.dumps(result))


if __name__=='__main__':main()
