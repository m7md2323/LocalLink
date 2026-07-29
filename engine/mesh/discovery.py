"""Peer discovery engine for the LocalLink mesh network. """

import logging
import socket
from zeroconf import ServiceBrowser, ServiceInfo, Zeroconf

SERVICE_TYPE = "_locallink._tcp.local."
DEFAULT_PORT = 5000
PEER_ID_KEY = "peer_id"
PUBLIC_KEY_KEY = "pubkey"
IP_DISCOVERY_TARGET = "8.8.8.8"

logger = logging.getLogger(__name__)

# Listener: receives callbacks from the zeroconf library when peers
# appear, vanish, or update. Runs on zeroconf's internal thread.


class LocalLinkListener:
    """Zeroconf listener that fires user-supplied callbacks on peer events. """
   

    def __init__(self, on_peer_found=None, on_peer_lost=None, local_peer_id=None):
        """Store the user's callbacks and our own peer_id for self-filtering."""
        
        self._on_peer_found = on_peer_found
        self._on_peer_lost = on_peer_lost
        self._local_peer_id = local_peer_id

    def add_service(self, zeroconf, service_type, name):
        """Called when a new peer appears on the network. """
        
        try:
            data = self._fetch_peer_data(zeroconf, service_type, name)
            if data is None:
                return
            
            if data["peer_id"] == self._local_peer_id:
                return
            
            if self._on_peer_found:
                self._on_peer_found(data)
                
        except Exception:
            # If something goes wrong while adding the peer, we will keep going and log the error.
            logger.exception("Error handling add_service for %s", name)

    def remove_service(self, zeroconf, service_type, name):
        """Called when a peer's mDNS announcement expires. """
       
        try:
            peer_id = self._parse_peer_id_from_name(name)
            if peer_id and peer_id != self._local_peer_id and self._on_peer_lost:
                self._on_peer_lost(peer_id)
        except Exception:
            logger.exception("Error handling remove_service for %s", name)

    def update_service(self, zeroconf, service_type, name):
        """Called when an existing peer changes its announcement. """
        
        self.add_service(zeroconf, service_type, name)

    #####--Private Helpers--#####

    def _fetch_peer_data(self, zeroconf, service_type, name):
        """Resolve a service name to a peer-data dict.

        Returns None if the service can't be resolved (it might have
        disappeared between the callback and our lookup) or if the
        announcement is missing required fields.
        """
        info = zeroconf.get_service_info(service_type, name)
        if info is None:
            logger.debug("get_service_info returned None for %s", name)
            return None
        return self._extract_peer_data(info)

    def _extract_peer_data(self, info):
        """Turn a ServiceInfo into a dict suitable for save_peer(). """
        
        properties = info.properties

        raw_peer_id = properties.get(PEER_ID_KEY.encode("utf-8"))
        raw_public_key = properties.get(PUBLIC_KEY_KEY.encode("utf-8"))

        if not raw_peer_id:
            return None

        peer_id = raw_peer_id.decode("utf-8") if isinstance(raw_peer_id, bytes) else raw_peer_id
        public_key = ""
        if raw_public_key:
            public_key = raw_public_key.decode("utf-8") if isinstance(raw_public_key, bytes) else raw_public_key

        # info.addresses is a list of raw address bytes. For a local
        # mesh we expect one IPv4 entry. socket.inet_ntoa turns the
        # 4 raw bytes back into "192.168.1.5".
        if info.addresses:
            # Take the first address. For a single-NIC machine on a
            # local network this is the only one.
            ip_address = socket.inet_ntoa(info.addresses[0])
        else:
            ip_address = "127.0.0.1"

        return {
            "peer_id": peer_id,
            "public_key": public_key,
            "ip_address": ip_address,
            "port": info.port,
            "is_online": True,
        }

    def _parse_peer_id_from_name(self, name):
        """Pull the peer_id back out of a service name string. """
        return name.split(".")[0]


# Discovery — manages the zeroconf engine

class Discovery:
    """Background mDNS engine that advertises us and finds other peers."""

    def __init__(
        self,
        peer_id,
        public_key,
        port=DEFAULT_PORT,
        host=None,
        on_peer_found=None,
        on_peer_lost=None,
    ):
        """Store configuration. Does not touch the network. """
        
        self.peer_id = peer_id
        self.public_key = public_key
        self.port = port
        self.host = host

        self._on_peer_found = on_peer_found
        self._on_peer_lost = on_peer_lost

        # These start as None and are populated by start().
        self._zeroconf = None
        self._service_info = None
        self._browser = None
        self._listener = None
        self._running = False

    def start(self):
        """Open the zeroconf engine, register our service, start browsing."""
        
        if self._running:
            logger.debug("Discovery already running, ignoring start()")
            return False

        try:
            # Figure out our local IP.
            if self.host is None:
                self.host = self._get_local_ip()

            # Open the zeroconf engine.
            self._zeroconf = Zeroconf()

            # Build the announcement and register it.
            self._service_info = self._build_service_info()
            self._register_service()

            # Create the listener and start the browser.
            self._listener = LocalLinkListener(
                on_peer_found=self._on_peer_found,
                on_peer_lost=self._on_peer_lost,
                local_peer_id=self.peer_id,
            )
            self._start_browser()

            self._running = True
            logger.info(
                "Discovery started: peer_id=%s at %s:%s",
                self.peer_id, self.host, self.port,
            )
            return True

        except Exception:
            # If anything in the chain above fails, roll back.
            logger.exception("Failed to start discovery, cleaning up")
            self._cleanup()
            raise

    def stop(self):
        """Unregister our service and close the zeroconf engine.

        Idempotent: calling stop() while not running is a no-op.
        """
        if not self._running:
            return
        self._cleanup()
        logger.info("Discovery stopped: peer_id=%s", self.peer_id)

    @property
    def is_running(self):
        """True if the engine is currently active."""
        return self._running

    ######--Private Heplers--#####.
    def _cleanup(self):
        """Tear down all zeroconf state, swallowing exceptions per step. """
       
        try:
            if self._zeroconf is not None and self._service_info is not None:
                self._zeroconf.unregister_service(self._service_info)
        except Exception:
            logger.exception("Error unregistering service")

        # Drop the browser and listener references. 
        
        self._browser = None
        self._listener = None
        self._service_info = None

        try:
            if self._zeroconf is not None:
                self._zeroconf.close()
        except Exception:
            logger.exception("Error closing zeroconf engine")

        self._zeroconf = None
        self._running = False

    def _get_local_ip(self):
        """Discover this machine's local IP using the UDP socket trick."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect((IP_DISCOVERY_TARGET, 80))
            ip = sock.getsockname()[0]
            return ip
        except OSError:

            logger.warning("Could not determine local IP, falling back to 127.0.0.1")
            return "127.0.0.1"
        finally:
            sock.close()

    def _build_service_info(self):
        """Build the ServiceInfo that represents our announcement.

        The name format is "<peer_id>.<service_type>" — the unique
        instance name followed by the service type. zeroconf requires
        this exact structure.
        """
        # The instance name is what other browsers will see and pass back to us in add_service.
        service_name = f"{self.peer_id}.{SERVICE_TYPE}"

        # The address must be raw network bytes (4 bytes for IPv4).
        # inet_aton turns "192.168.1.5" into b'\xc0\xa8\x01\x05'.
        address_bytes = socket.inet_aton(self.host)

        # TXT record properties. zeroconf accepts a dict of strings
        # and encodes them into the DNS TXT record format for us.
        properties = {
            PEER_ID_KEY: self.peer_id,
            PUBLIC_KEY_KEY: self.public_key,
        }

        return ServiceInfo(
            SERVICE_TYPE,
            service_name,
            addresses=[address_bytes],
            port=self.port,
            properties=properties,
        )

    def _register_service(self):
        """Send our announcement onto the network."""
        self._zeroconf.register_service(self._service_info)

    def _unregister_service(self):
        """Retract our announcement (good citizen on shutdown)."""
        self._zeroconf.unregister_service(self._service_info)

    def _start_browser(self):
        """Create the ServiceBrowser that listens for remote peers."""
        self._browser = ServiceBrowser(
            self._zeroconf,
            SERVICE_TYPE,
            self._listener,
        )
