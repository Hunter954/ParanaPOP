#!/usr/bin/env python3
"""Expose a read-only preview; never repoint production DNS."""
import json, pathlib, subprocess, time, urllib.request, ssl
host="paranapop-vps.70.38.101.106.sslip.io"
def run(args):
    p=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    if p.returncode:
        raise RuntimeError("Command failed: "+args[0]+" "+args[1])
    return p.stdout.decode()
n=json.loads(run(["docker","inspect","paranapop-preview"]))[0]["NetworkSettings"]["Networks"]
if "dokploy-network" not in n:
    run(["docker","network","connect","dokploy-network","paranapop-preview"])
root=pathlib.Path("/etc/dokploy/traefik")
static=root/"traefik.yml"
text=static.read_text()
if "email: test@localhost.com" in text:
    static.with_suffix(".pre-preview.yml").write_text(text)
    static.write_text(text.replace("email: test@localhost.com","email: ''"))
    run(["docker","restart","dokploy-traefik"])
rule="Host(`"+host+"`) && (Method(`GET`) || Method(`HEAD`)) && !PathPrefix(`/admin`)"
config={"http":{
 "routers":{
  "paranapop-preview-tls":{"rule":rule,"entryPoints":["websecure"],
   "service":"paranapop-preview","middlewares":["preview-noindex"],"tls":{"certResolver":"letsencrypt"}},
  "paranapop-preview-http":{"rule":rule,"entryPoints":["web"],
   "service":"paranapop-preview","middlewares":["preview-https","preview-noindex"]}},
 "services":{"paranapop-preview":{"loadBalancer":{"servers":[{"url":"http://paranapop-preview:8000"}]}}},
 "middlewares":{
  "preview-noindex":{"headers":{"customResponseHeaders":{"X-Robots-Tag":"noindex, nofollow, noarchive"}}},
  "preview-https":{"redirectScheme":{"scheme":"https","permanent":False}}}
}}
(root/"dynamic"/"paranapop-preview.yml").write_text(json.dumps(config))
print("Read-only preview configured:",host,flush=True)
result=None
for _ in range(40):
    try:
        with urllib.request.urlopen("https://"+host+"/health",timeout=10) as r:
            result={"preview_url":"https://"+host+"/","health_status":r.status,
                    "noindex":r.headers.get("X-Robots-Tag"),"tls_verified":True}
        break
    except Exception:
        time.sleep(3)
if result is None:
    raise SystemExit("Preview configured, but public TLS validation is pending")
pathlib.Path("/root/paranapop-migration/preview-result.json").write_text(json.dumps(result))
print(json.dumps(result),flush=True)
