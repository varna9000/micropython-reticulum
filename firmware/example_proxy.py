"""
µReticulum Proxy: USB Serial <-> LoRa Chat Bridge
==================================================
Turns the RP2040 into a transparent serial<->Reticulum proxy.

On your laptop, open a terminal program (screen / minicom / PuTTY / Tera
Term) on the RP2040's USB CDC port. Anything you type is sent as an LXMF
message over the configured interface (E32 LoRa in your case). Anything
received from a peer is printed back to the same terminal.

Commands (lines starting with /):
  /help            show this help
  /peers           list known peers
  /to <hex>        set current chat target by hex-hash prefix
  /me              show this node's LXMF address
  /name            show this node's display name
  /announce        broadcast my identity now
  /quit            shutdown and return to the MicroPython REPL

Anything that does NOT start with / is sent as a chat message to the
current target peer.

Auto-targeting:
  * If DEFAULT_PEER below is set to a peer's hex hash, that peer is
    auto-selected once it announces or we receive a message from it.
  * Otherwise, the first peer we see (announce OR incoming message)
    becomes the current target.
  * Any incoming message updates the current target to its sender, so
    replying is always automatic.

Setup:
  1. Edit config.py: set NODE_NAME, configure E32 interface.
     IMPORTANT: set DEBUG = 0 in config.py so the urns library does not
     print log noise onto your chat terminal.
  2. Copy urns/, config.py, example_proxy.py to the device.
  3. From the REPL: import example_proxy
  4. Connect from your laptop with a serial terminal. RP2040 USB CDC
     ignores the baud rate, so any value works (115200 is conventional).
"""

from config import NODE_NAME, CONFIG

import gc
gc.collect()


# ---- Proxy options ----------------------------------------------------

# Hex hash of a specific peer to auto-target. Leave as None to auto-target
# whichever peer is seen first.
DEFAULT_PEER = None

# Set to True if you want the local terminal to echo your sent messages
# back so you can see your own outbound traffic in the log.
SHOW_OUTGOING = True

# When True, the proxy announces itself once a second after boot and then
# every 2 minutes. Each announce blocks the event loop for ~5-7s of pure
# Python crypto on rp2040, during which input is unresponsive. Set to
# False to start silent — you can trigger announces manually with the
# /announce command at any time.
AUTO_ANNOUNCE = False

# When True, prints a heartbeat from the chat loop every few seconds so
# you can confirm the loop is being scheduled by the asyncio event loop.
HEARTBEAT = False


# ---- Internal state ---------------------------------------------------

_current_peer = None      # bytes: destination hash of current chat target
_peer_names = {}          # dest_hash bytes -> display name string


def _short(h):
    return h.hex()[:8] if h else "????????"


def _set_peer(dest_hash, announce_change=True):
    global _current_peer
    if _current_peer == dest_hash:
        return
    _current_peer = dest_hash
    if announce_change:
        name = _peer_names.get(dest_hash, "?")
        print("[target -> " + name + "/" + _short(dest_hash) + "]")


async def _send(router, dest_hash, body):
    """Send an LXMF message asynchronously."""
    import uasyncio as asyncio
    await asyncio.sleep(0)
    try:
        msg = router.send_message(dest_hash, body)
        if not msg:
            print("[err: unknown identity " + _short(dest_hash) + "]")
        elif SHOW_OUTGOING:
            print("[me -> " + _short(dest_hash) + "] " + body)
    except Exception as e:
        print("[err: send failed: " + str(e) + "]")
    gc.collect()


# ---- Serial chat loop -------------------------------------------------

async def serial_chat_loop(router):
    """Poll USB CDC stdin, dispatch /commands or send chat lines."""
    import sys
    import select
    import uasyncio as asyncio
    from urns.identity import Identity

    poller = select.poll()
    poller.register(sys.stdin, select.POLLIN)
    buf = ""

    print("")
    print("uReticulum chat proxy ready. Type /help for commands.")
    print("My address: " + router.delivery_destination.hexhash)
    if not AUTO_ANNOUNCE:
        print("(auto-announce disabled — type /announce to broadcast)")
    print("")

    while True:
        try:
            if poller.poll(0):
                ch = sys.stdin.read(1)
                if ch in ("\n", "\r"):
                    line = buf.strip()
                    buf = ""
                    if not line:
                        pass

                    elif line == "/help":
                        print("commands:")
                        print("  /peers          list known peers")
                        print("  /to <hex>       set chat target by hash prefix")
                        print("  /me             show my LXMF address")
                        print("  /name           show my display name")
                        print("  /announce       broadcast my identity now")
                        print("  /quit           exit to REPL")
                        print("  <anything else> send as chat to current target")

                    elif line == "/me":
                        print("my address: " + router.delivery_destination.hexhash)

                    elif line == "/name":
                        print("my name: " + NODE_NAME)

                    elif line == "/peers":
                        if not Identity.known_destinations:
                            print("[no peers known yet]")
                        else:
                            for dh in Identity.known_destinations:
                                name = _peer_names.get(dh, "?")
                                marker = " *" if dh == _current_peer else ""
                                print("  " + _short(dh) + "  " + name + marker)

                    elif line.startswith("/to "):
                        pfx = line[4:].strip().lower()
                        found = None
                        for dh in Identity.known_destinations:
                            if dh.hex().startswith(pfx):
                                found = dh
                                break
                        if found:
                            _set_peer(found)
                        else:
                            print("[no peer matches '" + pfx + "']")

                    elif line == "/announce":
                        print("[announcing...]")
                        try:
                            router.announce()
                            print("[announced]")
                        except Exception as e:
                            print("[err: " + str(e) + "]")
                        gc.collect()

                    elif line == "/quit":
                        raise KeyboardInterrupt

                    elif line.startswith("/"):
                        print("[unknown command: " + line + "]")

                    else:
                        if _current_peer is None:
                            print("[no target — /peers then /to <prefix>]")
                        else:
                            asyncio.create_task(
                                _send(router, _current_peer, line))
                else:
                    buf += ch

        except KeyboardInterrupt:
            raise
        except Exception as e:
            print("[err: " + str(e) + "]")

        await asyncio.sleep(0.05)


# ---- Reticulum / LXMF setup -------------------------------------------

def setup_node(rns):
    from urns.lxmf import LXMRouter
    router = LXMRouter(identity=rns.identity)
    dest = router.register_delivery_identity(rns.identity,
                                             display_name=NODE_NAME)

    def on_message(message):
        sender = message.source_hash
        content = message.content_as_string() or "(binary)"
        name = _peer_names.get(sender, _short(sender))
        verified = "" if message.signature_validated else " [UNVERIFIED]"
        print("<" + name + ">" + verified + " " + content)

        # Auto-target sender so a reply just works
        if _current_peer != sender:
            _set_peer(sender, announce_change=True)
        gc.collect()

    router.register_delivery_callback(on_message)

    def on_announce(destination_hash, display_name):
        if display_name:
            _peer_names[destination_hash] = display_name
        # Acquire target on first announce
        if _current_peer is None:
            if DEFAULT_PEER:
                try:
                    if destination_hash == bytes.fromhex(DEFAULT_PEER):
                        _set_peer(destination_hash)
                except Exception:
                    pass
            else:
                _set_peer(destination_hash)
        else:
            # Just note the new peer quietly
            print("[peer: " + (display_name or "?") +
                  " " + _short(destination_hash) + "]")

    router.register_announce_callback(on_announce)
    return dest, router


# ---- Main -------------------------------------------------------------

def main():
    import uasyncio as asyncio
    from urns import Reticulum
    from urns.log import LOG_NONE

    # Force quiet operation regardless of what config.py says
    quiet_config = dict(CONFIG)
    quiet_config["loglevel"] = 0

    rns = Reticulum(loglevel=LOG_NONE)
    rns.config = quiet_config

    dest, router = setup_node(rns)
    rns.setup_interfaces()
    gc.collect()

    async def initial_announce():
        await asyncio.sleep(2)
        print("[auto-announcing... ~2s]")
        try:
            router.announce()
            print("[announced]")
        except Exception as e:
            print("[announce err: " + str(e) + "]")
        gc.collect()

    async def reannounce_loop():
        while True:
            await asyncio.sleep(120)
            print("[re-announcing...]")
            try:
                router.announce()
            except Exception:
                pass
            gc.collect()

    async def run_all():
        from urns.transport import Transport

        # Create all tasks independently — no gather(), so one dying
        # doesn't kill the rest
        asyncio.create_task(Transport.job_loop())
        asyncio.create_task(serial_chat_loop(router))

        for iface in rns.interfaces:
            if hasattr(iface, 'poll_loop'):
                asyncio.create_task(iface.poll_loop())

        if AUTO_ANNOUNCE:
            asyncio.create_task(initial_announce())
            asyncio.create_task(reannounce_loop())

        n = len(Transport.interfaces)
        print("[tasks started, " + str(n) + " interface(s)]")

        # Keep the event loop alive forever
        while True:
            await asyncio.sleep(10)

    try:
        asyncio.run(run_all())
    except KeyboardInterrupt:
        rns.shutdown()
        print("[shutdown]")


main()
