#!/usr/bin/env python3
"""Stage the website against its existing live DB; never changes production DNS."""
import json, os, pathlib, subprocess, time, urllib.parse

B = pathlib.Path('/root/paranapop-migration')
D = pathlib.Path('/etc/dokploy/traefik/dynamic')
IMAGE = 'paranapop:vps-cfc9932'
APP = 'paranapop-live'
BOT = 'https://paranapop-whatsapp-production.up.railway.app'
os.umask(0o077)

def run(args, timeout=600):
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if p.returncode:
        raise RuntimeError('Failed command: ' + args[0] + ' ' + args[1])
    return p.stdout.decode()

def write(path, data):
    path.write_text(data)
    path.chmod(0o600)

def atomic_json(path, obj):
    tmp = path.with_suffix('.tmp')
    write(tmp, json.dumps(obj))
    tmp.replace(path)

def main():
    web = json.loads((B/'source-web.json').read_text())
    database = json.loads((B/'source-db.json').read_text())
    url = urllib.parse.urlsplit(database['DATABASE_PUBLIC_URL'])
    params = dict(urllib.parse.parse_qsl(url.query))
    params.update(sslmode='require', application_name='paranapop-vps-web')
    dburl = urllib.parse.urlunsplit(url._replace(query=urllib.parse.urlencode(params)))
    if dburl.startswith('postgres://'):
        dburl = dburl.replace('postgres://', 'postgresql+psycopg2://', 1)
    elif dburl.startswith('postgresql://'):
        dburl = dburl.replace('postgresql://', 'postgresql+psycopg2://', 1)
    env = {k:v for k,v in web.items() if not k.startswith('RAILWAY_')}
    env.update(DATABASE_URL=dburl, PORT='8000', MEDIA_ROOT='/data/uploads',
               R2_LOCAL_DIR='/data/uploads', AUTO_SYNC_INTERVAL='0',
               MEDIA_INDEX_ON_START='0', DB_BOOTSTRAP_REQUIRED='1',
               FORWARDED_ALLOW_IPS='*', SITE_URL='https://paranapop.com.br')
    for key,value in env.items():
        if '\n' in str(value) or '\r' in str(value):
            raise RuntimeError('Multiline setting requires handling: '+key)
    envpath = B/'live-web.env'
    write(envpath, ''.join(str(k)+'='+str(v)+'\n' for k,v in env.items()))
    # Only query the live DB here. Do not restore the stale staging backup into it.
    checkdb = '''import os,json
from sqlalchemy import create_engine,text
e=create_engine(os.environ['DATABASE_URL'])
with e.connect() as c:
 print(json.dumps({'post_count':c.execute(text('SELECT count(*) FROM post')).scalar(),
 'ssl':c.execute(text('SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()')).scalar(),
 'whatsapp_service_url':c.execute(text("SELECT value FROM site_setting WHERE key='whatsapp_service_url'")).scalar()}))
'''
    dbstate = json.loads(run(['docker','run','--rm','--env-file',str(envpath),
                             IMAGE,'python','-c',checkdb]))
    if not dbstate['ssl'] or not dbstate['post_count']:
        raise RuntimeError('Live DB validation failed')
    # The existing bot URL is already public in this setup. Stop if that changes.
    boturl = (dbstate.pop('whatsapp_service_url') or '').rstrip('/')
    if boturl != BOT:
        raise RuntimeError('WhatsApp callback address needs review before cutover')
    present = subprocess.run(['docker','inspect',APP],stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL).returncode == 0
    if not present:
        run(['docker','run','-d','--name',APP,'--network','paranapop-private',
             '--restart','unless-stopped','--memory','768m','--cpus','1.5',
             '--env-file',str(envpath),'-v','paranapop_uploads:/data',IMAGE])
    subprocess.run(['docker','network','connect','dokploy-network',APP],
                   stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    check = '''import json,urllib.request,urllib.error
out={}
for path in ['/health','/','/admin/login','/admin/api/whatsapp-bot/publish']:
 req=urllib.request.Request('http://127.0.0.1:8000'+path,headers={'Host':'paranapop.com.br','X-Forwarded-Proto':'https'},method='GET')
 try:
  with urllib.request.urlopen(req,timeout=60) as r: out[path]=r.status
 except urllib.error.HTTPError as e: out[path]=e.code
print(json.dumps(out))
'''
    for _ in range(80):
        try:
            http = json.loads(run(['docker','exec',APP,'python','-c',check],timeout=180))
            if http['/health']==200 and http['/']==200:
                break
        except RuntimeError:
            pass
        time.sleep(3)
    else:
        raise RuntimeError('Live web health failed')
    # Keep the existing preview read-only and unindexed while using current data.
    preview_path=D/'paranapop-preview.yml'
    preview=json.loads(preview_path.read_text())
    for service in preview['http']['services'].values():
        for server in service.get('loadBalancer',{}).get('servers',[]):
            server['url']='http://'+APP+':8000'
    atomic_json(preview_path,preview)
    rule='Host(`paranapop.com.br`) || Host(`www.paranapop.com.br`)'
    config={'http':{'routers':{
        'paranapop-official-http':{'rule':rule,'entryPoints':['web'],
            'middlewares':['paranapop-official-https'],'service':'paranapop-official'},
        'paranapop-official-https':{'rule':rule,'entryPoints':['websecure'],
            'service':'paranapop-official','tls':{}}},
        'middlewares':{'paranapop-official-https':{'redirectScheme':{'scheme':'https','permanent':False}}},
        'services':{'paranapop-official':{'loadBalancer':{'servers':[{'url':'http://'+APP+':8000'}]}}}}}
    # No certificate resolver until DNS validation installs a valid certificate.
    atomic_json(D/'paranapop-official.yml',config)
    hooks=B/'tls-hooks'
    hooks.mkdir(mode=0o700,exist_ok=True)
    write(hooks/'auth.py', AUTH_HOOK)
    write(hooks/'deploy.py', DEPLOY_HOOK)
    (hooks/'auth.py').chmod(0o700)
    (hooks/'deploy.py').chmod(0o700)
    result={'phase':'live-db-and-domain-routing-ready', 'database':'existing Railway PostgreSQL',
            'database_ssl':dbstate['ssl'], 'live_post_count':dbstate['post_count'],
            'http':http, 'bot_still_on_railway':True, 'dns_changed':False,
            'certificate':'pending DNS TXT validation'}
    atomic_json(B/'cutover-result.json',result)
    print(json.dumps(result),flush=True)
    subprocess.run(['docker','pull','certbot/certbot:latest'],check=True,
                   stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    existing=subprocess.run(['docker','inspect','paranapop-certbot'],
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0
    if not existing:
        run(['docker','run','-d','--name','paranapop-certbot',
             '-v',str(B/'letsencrypt')+':/etc/letsencrypt',
             '-v',str(B/'tls-state')+':/state',
             '-v',str(hooks)+':/ops:ro','-v',str(D)+':/dynamic',
             'certbot/certbot:latest','certonly','--non-interactive','--manual',
             '--preferred-challenges','dns','--manual-auth-hook','/ops/auth.py',
             '--deploy-hook','/ops/deploy.py','--agree-tos','--register-unsafely-without-email',
             '--cert-name','paranapop.com.br','-d','paranapop.com.br','-d','www.paranapop.com.br'])
    print('Certificate request started; read tls-state/challenges.json for public TXT records.',flush=True)

AUTH_HOOK = '''#!/usr/bin/env python3
import os,json,pathlib,time,urllib.request,urllib.parse
p=pathlib.Path('/state/challenges.json')
p.parent.mkdir(parents=True,exist_ok=True)
records=json.loads(p.read_text()) if p.exists() else []
domain=os.environ.get('CERTBOT_IDENTIFIER') or os.environ['CERTBOT_DOMAIN']
records=[r for r in records if r['name']!='_acme-challenge.'+domain]
records.append({'type':'TXT','name':'_acme-challenge.'+domain,'value':os.environ['CERTBOT_VALIDATION']})
p.write_text(json.dumps(records)); p.chmod(0o600)
if int(os.environ['CERTBOT_REMAINING_CHALLENGES'])!=0:
 raise SystemExit(0)
pathlib.Path('/state/phase').write_text('awaiting-dns-txt')
def visible(r,base):
 q=urllib.parse.urlencode({'name':r['name'],'type':'TXT'})
 req=urllib.request.Request(base+q,headers={'Accept':'application/dns-json'})
 with urllib.request.urlopen(req,timeout=15) as response: data=json.load(response)
 return any(r['value']==answer.get('data','').strip('"') for answer in data.get('Answer',[]))
for attempt in range(720):
 try:
  if all(visible(r,'https://cloudflare-dns.com/dns-query?') and visible(r,'https://dns.google/resolve?') for r in records):
   time.sleep(30)
   pathlib.Path('/state/phase').write_text('validating-certificate')
   raise SystemExit(0)
 except Exception: pass
 time.sleep(20)
raise SystemExit('DNS TXT validation timed out')
'''

DEPLOY_HOOK = '''#!/usr/bin/env python3
import os,pathlib,shutil,json
d=pathlib.Path('/dynamic')
certs=d/'certs'/'paranapop'; certs.mkdir(parents=True,exist_ok=True)
source=pathlib.Path(os.environ['RENEWED_LINEAGE'])
for f in ['fullchain.pem','privkey.pem']:
 dest=certs/f; shutil.copyfile(source/f,dest); dest.chmod(0o600)
hostpath='/etc/dokploy/traefik/dynamic/certs/paranapop/'
p=d/'paranapop-certificate.yml'
tmp=p.with_suffix('.tmp')
tmp.write_text(json.dumps({'tls':{'certificates':[{'certFile':hostpath+'fullchain.pem','keyFile':hostpath+'privkey.pem'}]}}))
tmp.chmod(0o600); tmp.replace(p)
pathlib.Path('/state/phase').write_text('certificate-installed')
'''

if __name__=='__main__':
    try: main()
    except Exception as error:
        print('STOPPED:',str(error) if isinstance(error,RuntimeError) else type(error).__name__,flush=True)
        raise SystemExit(1)
