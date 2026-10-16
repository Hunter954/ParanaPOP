#!/usr/bin/env python3
"""Prepare an isolated Parana Pop copy. Never changes production DNS or Railway."""
import json, os, pathlib, secrets, subprocess, time, urllib.parse

BASE = pathlib.Path("/root/paranapop-migration")
BASE.mkdir(mode=0o700, exist_ok=True)
os.chmod(BASE, 0o700)
os.umask(0o077)
REPO = "/opt/portais/paranapop"
RAILWAY = "/root/.railway/bin/railway"
PROJECT = "77143e2e-b85c-4767-ac34-eb233d1f2b46"
ENVIRONMENT = "73e6401a-c776-4e09-978a-142cd7be0a9a"
WEB = "842915dc-e262-4c9b-86b2-a0056030ae82"
DB = "3274d912-ca6f-44ce-8475-707d440b90d6"
os.environ.update(RAILWAY_CALLER="skill:use-railway@1.3.5",
                  RAILWAY_AGENT_SESSION="paranapop-vps-20261016")

def run(args, *, stdin=None, timeout=600):
    p = subprocess.run(args, cwd=REPO, stdin=stdin, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, timeout=timeout)
    if p.returncode:
        # Do not print errors that might contain connection credentials.
        raise RuntimeError("Command failed: " + args[0] + " " + args[1])
    return p.stdout.decode()

def save_json(name, data):
    (BASE / name).write_text(json.dumps(data))
    os.chmod(BASE / name, 0o600)

def envfile(name, values):
    for k, v in values.items():
        if "\n" in str(v) or "\r" in str(v):
            raise RuntimeError("Multiline variable needs separate handling: " + k)
    path = BASE / name
    path.write_text("".join(str(k)+"="+str(v)+"\n" for k,v in values.items()))
    os.chmod(path, 0o600)
    return str(path)

def present(name):
    return subprocess.run(["docker","container","inspect",name],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0

def main():
    print("1/7 Checking authenticated Railway CLI", flush=True)
    run([RAILWAY, "whoami", "--json"])
    run([RAILWAY, "link", "--project", PROJECT, "--environment", ENVIRONMENT])
    print("2/7 Exporting configuration privately on VPS", flush=True)
    web = json.loads(run([RAILWAY, "variable", "list", "--service", WEB,
                          "--environment", ENVIRONMENT, "--json"]))
    db = json.loads(run([RAILWAY, "variable", "list", "--service", DB,
                         "--environment", ENVIRONMENT, "--json"]))
    if not isinstance(web, dict) or not isinstance(db, dict):
        raise RuntimeError("Unexpected configuration format")
    if not web.get("SECRET_KEY") or not web.get("R2_ACCESS_KEY_ID"):
        raise RuntimeError("Required web settings were not exported")
    save_json("source-web.json", web)
    save_json("source-db.json", db)
    url = urllib.parse.urlparse(db["DATABASE_PUBLIC_URL"])
    source_env = envfile("source-pg.env", {
        "PGHOST":url.hostname, "PGPORT":url.port or 5432,
        "PGUSER":urllib.parse.unquote(url.username or ""),
        "PGPASSWORD":urllib.parse.unquote(url.password or ""),
        "PGDATABASE":url.path.lstrip("/"), "PGSSLMODE":"require",
    })
    dump = BASE / "initial.dump"
    print("3/7 Creating consistent PostgreSQL 18 backup", flush=True)
    if not dump.exists():
        run(["docker","run","--rm","--network","host","--env-file",source_env,
             "-v",str(BASE)+":/backup","postgres:18","pg_dump",
             "--format=custom","--file=/backup/initial.dump"], timeout=1800)
    if dump.stat().st_size < 1000:
        raise RuntimeError("Backup is unexpectedly small")
    print("Backup bytes:", dump.stat().st_size, flush=True)
    print("4/7 Preparing private PostgreSQL 18", flush=True)
    network = "paranapop-private"
    subprocess.run(["docker","network","create",network],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for volume in ("paranapop_postgres18", "paranapop_uploads"):
        run(["docker","volume","create",volume])
    secret_path = BASE / "target-password"
    if not secret_path.exists():
        secret_path.write_text(secrets.token_hex(32))
    password = secret_path.read_text().strip()
    target_env = envfile("target-pg.env", {
        "POSTGRES_USER":"paranapop", "POSTGRES_DB":"paranapop",
        "POSTGRES_PASSWORD":password,
    })
    if not present("paranapop-db"):
        run(["docker","run","-d","--name","paranapop-db","--network",network,
             "--restart","unless-stopped","--memory","1g","--cpus","2",
             "--env-file",target_env,
             "-v","paranapop_postgres18:/var/lib/postgresql","postgres:18"])
    for _ in range(60):
        try:
            run(["docker","exec","paranapop-db","pg_isready",
                 "-U","paranapop","-d","paranapop"])
            break
        except RuntimeError:
            time.sleep(2)
    else:
        raise RuntimeError("Target PostgreSQL did not become ready")
    print("5/7 Restoring backup into isolated database", flush=True)
    restored = BASE / "restored.ok"
    if not restored.exists():
        with dump.open("rb") as f:
            run(["docker","exec","-i","paranapop-db","pg_restore",
                 "-U","paranapop","-d","paranapop","--no-owner",
                 "--no-privileges","--exit-on-error"], stdin=f, timeout=1800)
        restored.write_text("ok")
    print("6/7 Starting private application copy", flush=True)
    target_web = {k:v for k,v in web.items() if not k.startswith("RAILWAY_")}
    target_web.update(
        DATABASE_URL="postgresql+psycopg2://paranapop:"+password+"@paranapop-db:5432/paranapop",
        PORT="8000", MEDIA_ROOT="/data/uploads", R2_LOCAL_DIR="/data/uploads",
        AUTO_SYNC_INTERVAL="0", MEDIA_INDEX_ON_START="0", DB_BOOTSTRAP_REQUIRED="1",
    )
    web_env = envfile("target-web.env", target_web)
    if not present("paranapop-preview"):
        run(["docker","run","--rm","--user","0","--network","none",
             "-v","paranapop_uploads:/data","paranapop:vps-cfc9932",
             "sh","-c","mkdir -p /data/uploads && chown -R 10001:10001 /data"])
        run(["docker","run","-d","--name","paranapop-preview","--network",network,
             "--restart","unless-stopped","--memory","768m","--cpus","1.5",
             "--env-file",web_env,"-v","paranapop_uploads:/data","paranapop:vps-cfc9932"])
    print("7/7 Checking health, home and restored data", flush=True)
    check = """import json,urllib.request
results={}
for path in ['/health','/']:
    with urllib.request.urlopen('http://127.0.0.1:8000'+path,timeout=30) as r:
        results[path]=r.status
print(json.dumps(results))"""
    for _ in range(40):
        try:
            checks = json.loads(run(["docker","exec","paranapop-preview",
                                     "python","-c",check], timeout=90))
            break
        except RuntimeError:
            time.sleep(3)
    else:
        raise RuntimeError("Application health check failed")
    count = run(["docker","exec","paranapop-db","psql","-U","paranapop",
                 "-d","paranapop","-At","-c","SELECT count(*) FROM post;"]).strip()
    result = {"status":"private-copy-ready", "http":checks,
              "post_count":int(count), "backup_bytes":dump.stat().st_size,
              "r2_configured":bool(web.get("R2_BUCKET")), "production_dns_changed":False}
    save_json("result.json",result)
    print(json.dumps(result),flush=True)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("STOPPED:",type(e).__name__,str(e) if isinstance(e,RuntimeError) else
              "Inspect locally without exposing secrets",flush=True)
        raise SystemExit(1)
