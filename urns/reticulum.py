# µReticulum Core Engine
# Simplified for Pico W: JSON config, no RPC, async event loop

import os
import gc
import time
from . import const
from .log import log, set_loglevel, LOG_VERBOSE, LOG_DEBUG, LOG_ERROR, LOG_NOTICE, LOG_INFO
from .identity import Identity
from .transport import Transport


class Reticulum:
    # Protocol constants (also accessible via const module)
    MTU = const.MTU
    TRUNCATED_HASHLENGTH = const.TRUNCATED_HASHLENGTH
    HEADER_MAXSIZE = const.HEADER_MAXSIZE
    MDU = const.MDU
    DEFAULT_PER_HOP_TIMEOUT = const.DEFAULT_PER_HOP_TIMEOUT

    _instance = None
    _use_implicit_proof = True

    def __init__(self, config_path="/rns/config.json", loglevel=None):
        if loglevel is not None:
            set_loglevel(loglevel)

        # Prevent MicroPython split heap from growing into IDF heap.
        # 4096 triggers GC sooner, reducing fragmentation-driven IDF expansion.
        gc.collect()
        try:
            gc.threshold(4096)
        except Exception:
            pass

        Reticulum._instance = self

        self.is_connected_to_shared_instance = False
        self.config = {}
        self.interfaces = []

        # Derive storage directory from config path
        if "/" in config_path:
            self.storagepath = config_path[:config_path.rfind("/")]
        else:
            self.storagepath = "."

        # Ensure storage directory exists
        self._ensure_dir(self.storagepath)
        self._ensure_dir(self.storagepath + "/ratchets")

        # Load or create identity
        Identity.storagepath = self.storagepath
        self.identity = self._load_or_create_identity()

        # Load config
        self._load_config(config_path)

        # Load known destinations
        Identity.load_known_destinations()

        # Start transport
        Transport.start(self)

        log("µReticulum v0.1.0 started", LOG_NOTICE)
        log("Identity: " + self.identity.hexhash, LOG_INFO)
        try:
            log("Free memory: " + str(gc.mem_free()) + " bytes", LOG_VERBOSE)
        except AttributeError:
            pass

    def _load_or_create_identity(self):
        identity_path = self.storagepath + "/identity"
        if self._file_exists(identity_path):
            identity = Identity.from_file(identity_path)
            if identity:
                log("Loaded identity from storage", LOG_VERBOSE)
                return identity
        # Create new identity
        identity = Identity()
        identity.to_file(identity_path)
        log("Created new identity", LOG_VERBOSE)
        return identity

    def _load_config(self, config_path):
        try:
            import json
            if self._file_exists(config_path):
                with open(config_path, "r") as f:
                    self.config = json.load(f)
                log("Loaded configuration from " + config_path, LOG_VERBOSE)
            else:
                self.config = self._default_config()
                with open(config_path, "w") as f:
                    json.dump(self.config, f)
                log("Created default configuration", LOG_VERBOSE)
        except Exception as e:
            log("Config error, using defaults: " + str(e), LOG_ERROR)
            self.config = self._default_config()

    def _default_config(self):
        return {
            "loglevel": 3,
            "enable_transport": False,
            "interfaces": [
                {
                    "type": "UDPInterface",
                    "name": "WiFi UDP",
                    "enabled": True,
                    "listen_ip": "0.0.0.0",
                    "listen_port": 4242,
                    "forward_ip": "255.255.255.255",
                    "forward_port": 4242,
                }
            ],
        }

    def _file_exists(self, path):
        try:
            os.stat(path)
            return True
        except OSError:
            return False

    @staticmethod
    def _ensure_dir(path):
        try:
            os.mkdir(path)
        except OSError:
            pass

    def setup_interfaces(self):
        """Initialize network interfaces from config. Call after WiFi is connected."""
        gc.collect()
        for iface_config in self.config.get("interfaces", []):
            if not iface_config.get("enabled", True):
                continue
            itype = iface_config.get("type", "")
            try:
                if itype == "UDPInterface":
                    from .interfaces.udp import UDPInterface
                    iface = UDPInterface(iface_config)
                    self.interfaces.append(iface)
                    Transport.register_interface(iface)
                elif itype == "SerialInterface":
                    from .interfaces.serial import SerialInterface
                    iface = SerialInterface(iface_config)
                    self.interfaces.append(iface)
                    Transport.register_interface(iface)
                else:
                    log("Unknown interface type: " + itype, LOG_ERROR)
            except Exception as e:
                log("Failed to init interface " + itype + ": " + str(e), LOG_ERROR)

    async def run(self):
        """Main async event loop. Run with asyncio.run(reticulum.run())"""
        try:
            import uasyncio as asyncio
        except ImportError:
            import asyncio

        tasks = [
            asyncio.create_task(Transport.job_loop()),
        ]

        # Start interface poll loops
        for iface in self.interfaces:
            if hasattr(iface, 'poll_loop'):
                tasks.append(asyncio.create_task(iface.poll_loop()))

        log("Event loop running with " + str(len(tasks)) + " tasks", LOG_VERBOSE)
        await asyncio.gather(*tasks)

    def shutdown(self):
        """Clean shutdown - persist state and close interfaces"""
        log("Shutting down µReticulum", LOG_NOTICE)
        Transport.stop()
        Identity.persist_data()
        for iface in self.interfaces:
            if hasattr(iface, 'close'):
                try:
                    iface.close()
                except:
                    pass

    @staticmethod
    def get_instance():
        return Reticulum._instance

    @staticmethod
    def should_use_implicit_proof():
        return Reticulum._use_implicit_proof

    def get_first_hop_timeout(self, destination_hash):
        return Reticulum.DEFAULT_PER_HOP_TIMEOUT

    @staticmethod
    def exit_handler():
        if Reticulum._instance:
            Reticulum._instance.shutdown()
