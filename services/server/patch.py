"""Temporary same-length patch for supermemory-server 0.0.9; delete it with 0.0.10.

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
assert len(patched) == len(data)
open(sys.argv[1], "wb").write(patched)
print(f"patched snapshot dirty tracking ({len(OLD)} bytes)")
