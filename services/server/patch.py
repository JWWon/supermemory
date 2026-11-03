"""Temporary same-length patch for supermemory-server 0.0.9; delete it with 0.0.10.

Before any upgrade, start once with SM_SNAPSHOT=1 and stop it, so `data` holds the current store
in the stock encrypted format (see the disk-backed store section below).

0.0.9 re-dumps the whole encrypted store (about 800 MB here) after any 10-second
window in which a non-read statement ran (supermemoryai/supermemory#1413). Its
every-minute ingestion coordinator inserts and deletes a lease row in "cache",
and every authenticated request updates apikey.last_request, so the store was
rewritten every minute (about 1.2 TB a day). This exempts only those disposable
writes from the dirty flag. Every other write still snapshots, transactions
still always count, and shutdown snapshots unconditionally.
"""
import hashlib
import re
import sys

BINARY_SHA256 = "b789be16cd7192b3c08e882396c0a94ba330ea467d5a7fb1b39ac3684abf5374"
OLD = (b'var pm0=/^\\s*(select|show|explain)\\b/i;function Qv8(V,q){let $=V.query.bind(V);'
       b'V.query=(...v)=>{if(!pm0.test(String(v[0])))q();return $(...v)};let X=V.exec.bind(V);'
       b'V.exec=(...v)=>{if(!pm0.test(String(v[0])))q();return X(...v)};let W=V.transaction.bind(V);'
       b'V.transaction=(...v)=>{return q(),W(...v)}}')
NEW = (b'var pm0=/^\\s*(select|show|explain)\\b|^(insert into|delete from) "cache"|'
       b'^update "apikey" set "(last_request|request_count)"/i;function Qv8(V,q){'
       b'for(let k of["query","exec","transaction"]){let f=V[k].bind(V);'
       b'V[k]=(...v)=>(k=="transaction"||!pm0.test(v[0])?q():0,f(...v))}}')

# Self-check: the embedded pattern is plain enough that Python's re reads it like JavaScript.
not_dirty = re.compile(NEW.split(b"var pm0=/", 1)[1].split(b"/i;", 1)[0].decode(), re.I)
for sql, exempt in [
    ('select * from "document" where "document"."id" = $1', True),
    ('insert into "cache" ("key", "value", "expires_at") values ($1, $2, $3)', True),
    ('delete from "cache" where ("cache"."key" = $1)', True),
    ('update "apikey" set "last_request" = $1 where "apikey"."id" = $2', True),
    ('update "apikey" set "request_count" = $1, "last_request" = $2 where "apikey"."id" = $3', True),
    ('update "apikey" set "enabled" = $1 where "apikey"."id" = $2', False),
    ('insert into "apikey" ("id", "key") values ($1, $2)', False),
    ('insert into "document" ("content") values (\'mentions "cache"\')', False),
    ('insert into "cache_other" ("key") values ($1)', False),
    ('delete from "memory_entry" where "memory_entry"."id" = $1', False),
]:
    assert bool(not_dirty.search(sql)) == exempt, sql

data = open(sys.argv[1], "rb").read()
if hashlib.sha256(data).hexdigest() != BINARY_SHA256:
    sys.exit("unexpected supermemory-server binary: this patch only fits 0.0.9")
if data.count(OLD) != 1 or len(NEW) > len(OLD):
    sys.exit("patch target not found exactly once")
patched = data.replace(OLD, NEW[:-1] + b" " * (len(OLD) - len(NEW)) + b"}")

# Snapshot interval 10 s -> 300 s, the workaround reported in supermemoryai/supermemory#1826.
# Each dirty tick dumps, gzips and encrypts the whole store in memory, so at about 1.4 GB
# back-to-back ticks during ingestion pushed the server past its memory limit. A crash can
# now lose up to 5 minutes of writes; a normal stop still writes a final snapshot.
INTERVAL_OLD = b'Om0="supermemory-pglite-v1",ov8=1e4;'
INTERVAL_NEW = b'Om0="supermemory-pglite-v1",ov8=3e5;'
if patched.count(INTERVAL_OLD) != 1:
    sys.exit("snapshot interval target not found exactly once")
patched = patched.replace(INTERVAL_OLD, INTERVAL_NEW)

# Boot SIGKILLs the PID in the previous run's runtime/rivet/engine.pid without checking it is the
# engine. A new container restarts PIDs and thread ids low, so after any crash that number is often
# the server itself: exit 137, no OOM, and the file survives to do it again on the next boot. A new
# PID namespace cannot hold an orphan engine, and the file is overwritten once the engine starts.
REAP_OLD = b'if(!Number.isInteger($)||$<=1)return;try{process.kill($,"SIGKILL")'
REAP_NEW = b'return;' + b" " * 30 + b'try{process.kill($,"SIGKILL")'
assert len(REAP_OLD) == len(REAP_NEW)
if patched.count(REAP_OLD) != 1:
    sys.exit("engine reaper target not found exactly once")
patched = patched.replace(REAP_OLD, REAP_NEW)

# Disk-backed store. Stock 0.0.9 keeps the whole Postgres data directory in memory and dumps, gzips
# and encrypts all of it on every dirty tick, so boot and snapshot peaks run about 7x the store and hit
# the VM's ceiling at 1.65 GB. A plain path makes PGlite use the NodeFS class already in the bundle.
# The first boot loads the encrypted `data` into <data>/pg once; later boots open <data>/pg directly.
# SM_SNAPSHOT=1 restores the snapshot timer, which writes `data` on stop: the way back to stock.
# Single-user Postgres has no checkpointer, hence the CHECKPOINT timer.
OLD_LOAD = (
    b'if(await Ev8(q)){let O=await Dv8(q),p;try{p=lP(Om0,Tm0,O)}catch{throw Error(tv8(q,V))}'
    b'$=new Blob([p]),G4(`[storage] Loaded snapshot (${(O.length/1024).toFixed(1)} KB ciphertext '
    b'\\u2192 ${(p.length/1024).toFixed(1)} KB plaintext)`)}'
    b'else G4("[storage] No snapshot found \\u2014 fresh start.");'
)
NEW_LOAD = (
    b'if(await Ev8(q)&&!await Ev8(V+"/pg/PG_VERSION")){let O=await Dv8(q),p;'
    b'try{p=lP(Om0,Tm0,O)}catch{throw Error(tv8(q,V))}$=new Blob([p])}'
)
OLD_BOOT = (
    b'async function iJ8(){G4(`[db] Mode: encrypted local storage (${H2})`);let V=await Bv8(H2),'
    b'q=V.loadBlob?await YX8(m54(jX8(),"supermemory-preflight-")):void 0,$;try{'
    b'let{wasmModule:X,fsBundle:W,vectorExtension:v}=await ZJ8(q);$=new BY("memory://",{wasmModule:X,'
    b'fsBundle:W,extensions:{vector:v},...V.loadBlob?{loadDataDir:V.loadBlob}:{}}),await $.waitReady;'
    b'let z=await Bz8($,H2,V.loadBlob!==null);if(z)a$(`Verified original-store backup: ${z}`);'
    b'return await $.exec("CREATE EXTENSION IF NOT EXISTS vector"),L94($,UR),'
    b'G4("[migrate] Applying embedded migrations..."),await Mv8($),C00=V.install($),$}catch(X)'
)
NEW_BOOT = (
    b'async function iJ8(){let V=await Bv8(H2),'
    b'q=V.loadBlob?await YX8(m54(jX8(),"supermemory-preflight-")):void 0,$;try{'
    b'let{wasmModule:X,fsBundle:W,vectorExtension:v}=await ZJ8(q);$=new BY(H2+"/pg",{wasmModule:X,'
    b'fsBundle:W,extensions:{vector:v},...V.loadBlob?{loadDataDir:V.loadBlob}:{}}),await $.waitReady;'
    b'await Bz8($,H2,V.loadBlob!==null);'
    b'return await $.exec("CREATE EXTENSION IF NOT EXISTS vector"),L94($,UR),'
    b'await Mv8($),C00=process.env.SM_SNAPSHOT?V.install($):'
    b'(setInterval(()=>$.exec("CHECKPOINT").catch(console.error),6e5).unref(),'
    b'{stop:()=>$.exec("CHECKPOINT")}),$}catch(X)'
)


def pad(new: bytes, old: bytes, keep_tail: bytes = b"") -> bytes:
    """Fill with spaces before keep_tail so the binary keeps its length; JavaScript ignores them."""
    if len(new) > len(old) or not new.endswith(keep_tail):
        sys.exit("disk-store replacement does not fit")
    return new[: len(new) - len(keep_tail)] + b" " * (len(old) - len(new)) + keep_tail


for old_bytes, new_bytes in [(OLD_LOAD, pad(NEW_LOAD, OLD_LOAD)), (OLD_BOOT, pad(NEW_BOOT, OLD_BOOT, b"$}catch(X)"))]:
    if patched.count(old_bytes) != 1:
        sys.exit(f"disk-store target not found exactly once: {old_bytes[:40]!r}")
    patched = patched.replace(old_bytes, new_bytes)
# Same-length labels, so boot output no longer claims encryption.
patched = patched.replace(b'"encrypted local storage"', b'"disk-backed local store"')
patched = patched.replace(b'database:`encrypted local storage (', b'database:`disk-backed local store (')

assert len(patched) == len(data)
open(sys.argv[1], "wb").write(patched)
print(f"patched snapshot dirty tracking ({len(OLD)} bytes), snapshot interval (300 s), engine reaper, disk-backed store")
