#!/usr/bin/env python3
"""Read-only copy of Railway media into the isolated VPS preview volume."""
import base64, json, os, pathlib, subprocess, sys
os.umask(0o077)
BASE=pathlib.Path("/root/paranapop-migration")
RAILWAY="/root/.railway/bin/railway"
os.environ.update(RAILWAY_CALLER="skill:use-railway@1.3.5",RAILWAY_AGENT_SESSION="paranapop-vps-20261016")
args=[RAILWAY,"ssh","--project","77143e2e-b85c-4767-ac34-eb233d1f2b46",
      "--environment","73e6401a-c776-4e09-978a-142cd7be0a9a",
      "--service","842915dc-e262-4c9b-86b2-a0056030ae82","--","bash","-o","pipefail","-c",
      "echo VPS_MEDIA_START; tar -C /data -czf - . | base64 -w 76; echo VPS_MEDIA_END"]
print("Copying media directly from Railway to VPS",flush=True)
with (BASE/"media-transfer.b64").open("wb") as out, (BASE/"media-transfer.stderr").open("wb") as err:
    p=subprocess.run(args,stdout=out,stderr=err,stdin=subprocess.DEVNULL,timeout=3600,cwd="/opt/portais/paranapop")
if p.returncode:
    print("Media transfer failed; details kept privately on VPS",flush=True)
    sys.exit(1)
started=ended=False
archive=BASE/"media.tar.gz"
with (BASE/"media-transfer.b64").open("rb") as inp, archive.open("wb") as out:
    for line in inp:
        line=line.strip()
        if b"VPS_MEDIA_START" in line:
            started=True
            continue
        if b"VPS_MEDIA_END" in line:
            ended=True
            break
        if started and line:
            out.write(base64.b64decode(line,validate=True))
if not started or not ended or archive.stat().st_size<20:
    raise SystemExit("Incomplete media transfer")
# Validate every archive member before extracting.
import tarfile
with tarfile.open(archive,"r:gz") as tar:
    count=0
    for member in tar:
        path=pathlib.PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts or member.issym() or member.islnk():
            raise SystemExit("Unsafe member in media archive")
        count+=int(member.isfile())
cmd=["docker","run","--rm","--user","0","--network","none",
     "-v","paranapop_uploads:/data","-v",str(BASE)+":/backup:ro",
     "paranapop:vps-cfc9932","sh","-c",
     "tar -xzf /backup/media.tar.gz -C /data && chown -R 10001:10001 /data"]
subprocess.run(cmd,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
result={"local_files_copied":count,"archive_bytes":archive.stat().st_size}
(BASE/"media-result.json").write_text(json.dumps(result))
print(json.dumps(result),flush=True)
