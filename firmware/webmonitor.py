"""
µReticulum — minimal HTTP monitor for a transport node
======================================================
A plain-HTTP dashboard on the LAN (NOT Reticulum) so you can point a browser at
the node and watch its routing tables, relay counters, known paths and live log
update in real time. Useful for operating a transport router.

  GET /      -> HTML dashboard (auto-refreshes every 2 s via /api)
  GET /api   -> JSON snapshot of the node's state

Usage (from the router example, inside the asyncio loop):
  import uasyncio as asyncio, webmonitor
  asyncio.create_task(webmonitor.serve(node_name="my-router", port=80))

Then open  http://<node-ip>/  from any device on the same network.
"""
import gc
import uasyncio as asyncio
from urns import Transport, const
from urns.log import get_log_ring, log, LOG_NOTICE, LOG_ERROR

_NODE = "uRNS"
_BATTERY_FN = None      # set by serve(); returns battery volts (float) or None

_PAGE = """<!DOCTYPE html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>uRNS Transport Router</title><style>
body{font-family:ui-monospace,monospace;background:#0c0e0c;color:#b8f5b8;margin:0;padding:1em}
h1{font-size:1.1em;color:#7fff7f;margin:.2em 0}h3{color:#6fce6f;margin:.8em 0 .3em;font-size:.95em}
.box{background:#121512;border:1px solid #243024;border-radius:6px;padding:.6em .8em;margin:.4em 0}
table{border-collapse:collapse;width:100%;font-size:.85em}td,th{padding:2px 8px;text-align:left;border-bottom:1px solid #1e271e}
th{color:#6fce6f}b{color:#7fff7f}.dim{color:#5a6e5a}
#log{background:#000;height:42vh;overflow:auto;white-space:pre-wrap;font-size:.8em;line-height:1.35em}
.note{color:#9fe89f}.err{color:#ff6b6b}.warn{color:#ffc14d}.crit{color:#ff6b6b}
.pill{display:inline-block;background:#1c241c;border:1px solid #2c3a2c;border-radius:10px;padding:1px 8px;margin:1px}
.on{color:#7fff7f}.off{color:#ff6b6b}
</style></head><body>
<h1>µReticulum Transport Router &mdash; <span id=node class=dim>...</span></h1>
<div class=box id=info>connecting...</div>
<h3>Tables &amp; relay counters</h3><div class=box id=tables></div>
<h3>Known paths</h3><div class=box id=paths></div>
<h3>Live log <span class=dim>(newest at bottom)</span></h3><div class=box id=log></div>
<script>
function esc(s){return (''+s).replace(/&/g,'&amp;').replace(/</g,'&lt;')}
async function tick(){
 try{
  const d=await (await fetch('/api',{cache:'no-store'})).json();
  node.textContent=d.node+'  '+(d.transport_id||'').slice(0,16);
  info.innerHTML='<span class=pill>transport '+(d.transport_enabled?'<span class=on>ON</span>':'<span class=off>OFF</span>')+'</span>'
    +'<span class=pill>free '+((d.free_mem/1024)|0)+' KB</span>'
    +(d.battery?'<span class=pill>batt <b>'+d.battery.v.toFixed(2)+' V</b> ~'+d.battery.pct+'%</span>':'')
    +d.interfaces.map(i=>'<span class=pill>'+esc(i.name)+' '+(i.online?'<span class=on>&#10003;</span>':'<span class=off>&#10007;</span>')+'</span>').join('');
  const t=d.tables,r=d.relayed;
  tables.innerHTML='<span class=pill>paths '+t.paths+'</span><span class=pill>reachable '+t.reachable+'</span>'
   +'<span class=pill>links '+t.links+'</span><span class=pill>reverse '+t.reverse+'</span>'
   +'<span class=pill>cache '+t.cache+'</span><span class=pill>queued '+t.queued+'</span><br>'
   +'<b>RELAYED</b> <span class=pill>announces '+r.announces+'</span><span class=pill>data '+r.data+'</span>'
   +'<span class=pill>links '+r.links+'</span><span class=pill>proofs '+r.proofs+'</span>';
  paths.innerHTML=d.paths.length?('<table><tr><th>destination</th><th>hops</th><th>via</th><th>interface</th></tr>'
   +d.paths.map(p=>'<tr><td>'+p.dest.slice(0,20)+'</td><td>'+p.hops+'</td><td>'+p.via+'</td><td>'+esc(p.iface)+'</td></tr>').join('')+'</table>'):'<span class=dim>(none yet)</span>';
  const atBottom=log.scrollTop+log.clientHeight>=log.scrollHeight-30;
  log.innerHTML=d.log.map(l=>{const c=l.indexOf('[ERR')>0||l.indexOf('[CRIT')>0?'err':l.indexOf('[WARN')>0?'warn':'note';return '<span class='+c+'>'+esc(l)+'</span>';}).join('\\n');
  if(atBottom)log.scrollTop=log.scrollHeight;
 }catch(e){info.innerHTML='<span class=off>disconnected — retrying...</span>';}
 setTimeout(tick,2000);
}
tick();
</script></body></html>""".encode()


def _lipo_pct(v):
    """Rough single-cell LiPo state-of-charge (%) from resting voltage — approximate."""
    pts = ((3.30, 0), (3.60, 10), (3.70, 25), (3.80, 50),
           (3.90, 65), (4.00, 80), (4.10, 92), (4.20, 100))
    if v <= pts[0][0]:
        return 0
    if v >= pts[-1][0]:
        return 100
    for (v0, p0), (v1, p1) in zip(pts, pts[1:]):
        if v < v1:
            return int(p0 + (p1 - p0) * (v - v0) / (v1 - v0))
    return 100


def _snapshot():
    T = Transport
    paths = []
    for dest, e in list(T.path_table.items())[:48]:
        try:
            rif = e[const.IDX_PT_RECV_IF]
            paths.append({
                "dest": dest.hex(),
                "hops": e[const.IDX_PT_HOPS],
                "via": e[const.IDX_PT_NEXT_HOP].hex()[:8],
                "iface": str(rif) if rif is not None else "?",
            })
        except Exception:
            pass
    batt = None
    if _BATTERY_FN:
        try:
            v = _BATTERY_FN()
            if v is not None:
                batt = {"v": round(v, 2), "pct": _lipo_pct(v)}
        except Exception:
            pass
    return {
        "node": _NODE,
        "transport_id": T.identity.hash.hex() if T.identity else None,
        "transport_enabled": bool(T.transport_enabled),
        "free_mem": gc.mem_free(),
        "interfaces": [{"name": i.name, "online": bool(i.online)} for i in T.interfaces],
        "tables": {
            "paths": len(T.path_table), "reachable": len(T.reachable_destinations),
            "links": len(T.link_table), "reverse": len(T.reverse_table),
            "cache": len(T.packet_cache), "queued": len(T.announce_table),
        },
        "relayed": {
            "announces": T.relayed_announces, "data": T.relayed_data,
            "links": T.relayed_links, "proofs": T.relayed_proofs,
        },
        "paths": paths,
        "battery": batt,
        "log": list(get_log_ring()),
    }


async def _handle(reader, writer):
    try:
        req = await reader.readline()
        # Drain the rest of the request headers.
        while True:
            h = await reader.readline()
            if not h or h == b"\r\n":
                break
        path = b"/"
        try:
            path = req.split(b" ")[1]
        except Exception:
            pass

        if path.startswith(b"/api"):
            import json
            body = json.dumps(_snapshot()).encode()
            ctype = b"application/json"
        else:
            body = _PAGE
            ctype = b"text/html; charset=utf-8"

        writer.write(b"HTTP/1.0 200 OK\r\nContent-Type: " + ctype +
                     b"\r\nConnection: close\r\nCache-Control: no-store\r\n"
                     b"Access-Control-Allow-Origin: *\r\n\r\n")
        writer.write(body)
        await writer.drain()
    except Exception:
        try:
            writer.write(b"HTTP/1.0 500 Internal Error\r\nConnection: close\r\n\r\n")
            await writer.drain()
        except Exception:
            pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
    gc.collect()


async def serve(node_name="uRNS", host="0.0.0.0", port=80, battery_fn=None):
    """Start the HTTP monitor and keep its task alive. Run as an asyncio task.

    battery_fn: optional callable returning battery volts (float) or None; when
    given, the value is shown as a gauge in the dashboard header."""
    global _NODE, _BATTERY_FN
    _NODE = node_name
    _BATTERY_FN = battery_fn
    try:
        await asyncio.start_server(_handle, host, port)
        log("Web monitor listening on http://<node-ip>:" + str(port) + "/  (LAN, plain HTTP)", LOG_NOTICE)
    except Exception as e:
        log("Web monitor failed to start: " + str(e), LOG_ERROR)
        return
    while True:
        await asyncio.sleep(3600)
