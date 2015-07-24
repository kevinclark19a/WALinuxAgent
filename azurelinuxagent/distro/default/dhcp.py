# Windows Azure Linux Agent
#
# Copyright 2014 Microsoft Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Requires Python 2.4+ and Openssl 1.0+
import os
import socket
import array
import time
import azurelinuxagent.logger as logger
from azurelinuxagent.utils.osutil import OSUTIL
from azurelinuxagent.exception import AgentNetworkError
import azurelinuxagent.utils.fileutil as fileutil
import azurelinuxagent.utils.shellutil as shellutil
from azurelinuxagent.utils.textutil import *

WIRE_SERVER_ADDR_FILE_NAME="WireServer"

class DhcpHandler(object):
    def __init__(self):
        self.endpoint = None
        self.gateway = None
        self.routes = None

    def wait_for_network(self):
        ipv4 = OSUTIL.get_ip4_addr()
        while ipv4 == '' or ipv4 == '0.0.0.0':
            logger.info("Waiting for network.")
            time.sleep(10)
            OSUTIL.start_network()
            ipv4 = OSUTIL.get_ip4_addr()

    def probe(self):
        logger.info("Send dhcp request")
        self.wait_for_network()
        mac_addr = OSUTIL.get_mac_addr()
        req = build_dhcp_request(mac_addr)
        resp = send_dhcp_request(req)
        endpoint, gateway, routes = parse_dhcp_resp(resp)
        self.endpoint = endpoint
        logger.info("Wire server endpoint:{0}", endpoint)
        logger.info("Gateway:{0}", gateway)
        logger.info("Routes:{0}", routes)
        if endpoint is not None:
            path = os.path.join(OSUTIL.get_lib_dir(), WIRE_SERVER_ADDR_FILE_NAME)
            fileutil.write_file(path, endpoint)
        self.gateway = gateway
        self.routes = routes
        self.conf_routes()

    def get_endpoint(self):
        return self.endpoint

    def conf_routes(self):
        logger.info("Configure routes")
        #Add default gateway
        if self.gateway is not None:
            OSUTIL.route_add(0 , 0, self.gateway)
        if self.routes is not None:
            for route in self.routes:
                OSUTIL.route_add(route[0], route[1], route[2])

def validate_dhcp_resp(request, response):
    bytes_recv = len(response)
    if bytes_recv < 0xF6:
        logger.error("HandleDhcpResponse: Too few bytes received:{0}",
                     str(bytes_recv))
        return False

    logger.verb("BytesReceived:{0}", hex(bytes_recv))
    logger.verb("DHCP response:{0}", hex_dump(response, bytes_recv))

    # check transactionId, cookie, MAC address cookie should never mismatch
    # transactionId and MAC address may mismatch if we see a response
    # meant from another machine
    if not compare_bytes(request, response, 0xEC, 4):
        logger.verb("Cookie not match:\nsend={0},\nreceive={1}",
                       hex_dump3(request, 0xEC, 4),
                       hex_dump3(response, 0xEC, 4))
        raise AgentNetworkError("Cookie in dhcp respones "
                                "doesn't match the request")

    if not compare_bytes(request, response, 4, 4):
        logger.verb("TransactionID not match:\nsend={0},\nreceive={1}",
                       hex_dump3(request, 4, 4),
                       hex_dump3(response, 4, 4))
        raise AgentNetworkError("TransactionID in dhcp respones "
                                "doesn't match the request")

    if not compare_bytes(request, response, 0x1C, 6):
        logger.verb("Mac Address not match:\nsend={0},\nreceive={1}",
                       hex_dump3(request, 0x1C, 6),
                       hex_dump3(response, 0x1C, 6))
        raise AgentNetworkError("Mac Addr in dhcp respones "
                                "doesn't match the request")

def parse_route(response, option, i, length, bytes_recv):
    # http://msdn.microsoft.com/en-us/library/cc227282%28PROT.10%29.aspx
    logger.verb("Routes at offset: {0} with length:{1}",
                   hex(i),
                   hex(length))
    routes = []
    if length < 5:
        logger.error("Data too small for option:{0}", str(option))
    j = i + 2
    while j < (i + length + 2):
        mask_len_bits = str_to_ord(response[j])
        mask_len_bytes = (((mask_len_bits + 7) & ~7) >> 3)
        mask = 0xFFFFFFFF & (0xFFFFFFFF << (32 - mask_len_bits))
        j += 1
        net = unpack_big_endian(response, j, mask_len_bytes)
        net <<= (32 - mask_len_bytes * 8)
        net &= mask
        j += mask_len_bytes
        gateway = unpack_big_endian(response, j, 4)
        j += 4
        routes.append((net, mask, gateway))
    if j != (i + length + 2):
        logger.error("Unable to parse routes")
    return routes

def parse_ip_addr(response, option, i, length, bytes_recv):
    if i + 5 < bytes_recv:
        if length != 4:
            logger.error("Endpoint or Default Gateway not 4 bytes")
            return None
        addr = unpack_big_endian(response, i + 2, 4)
        ip_addr = int_to_ip4_addr(addr)
        return ip_addr
    else:
        logger.error("Data too small for option:{0}", str(option))
    return None

def parse_dhcp_resp(response):
    """
    Parse DHCP response:
    Returns endpoint server or None on error.
    """
    logger.verb("parse Dhcp Response")
    bytes_recv = len(response)
    endpoint = None
    gateway = None
    routes = None

    # Walk all the returned options, parsing out what we need, ignoring the
    # others. We need the custom option 245 to find the the endpoint we talk to,
    # as well as, to handle some Linux DHCP client incompatibilities,
    # options 3 for default gateway and 249 for routes. And 255 is end.

    i = 0xF0 # offset to first option
    while i < bytes_recv:
        option = str_to_ord(response[i])
        length = 0
        if (i + 1) < bytes_recv:
            length = str_to_ord(response[i + 1])
        logger.verb("DHCP option {0} at offset:{1} with length:{2}",
                       hex(option),
                       hex(i),
                       hex(length))
        if option == 255:
            logger.verb("DHCP packet ended at offset:{0}", hex(i))
            break
        elif option == 249:
            routes = parse_route(response, option, i, length, bytes_recv)
        elif option == 3:
            gateway = parse_ip_addr(response, option, i, length, bytes_recv)
            logger.verb("Default gateway:{0}, at {1}",
                           gateway,
                           hex(i))
        elif option == 245:
            endpoint = parse_ip_addr(response, option, i, length, bytes_recv)
            logger.verb("Azure wire protocol endpoint:{0}, at {1}",
                           gateway,
                           hex(i))
        else:
            logger.verb("Skipping DHCP option:{0} at {1} with length {2}",
                           hex(option),
                           hex(i),
                           hex(length))
        i += length + 2
    return endpoint, gateway, routes


def allow_dhcp_broadcast(func):
    """
    Temporary allow broadcase for dhcp. Remove the route when done.
    """
    def wrapper(*args, **kwargs):
        missing_default_route = OSUTIL.is_missing_default_route()
        ifname = OSUTIL.get_if_name()
        if missing_default_route:
            OSUTIL.set_route_for_dhcp_broadcast(ifname)
        result = func(*args, **kwargs)
        if missing_default_route:
            OSUTIL.remove_route_for_dhcp_broadcast(ifname)
        return result
    return wrapper

def disable_dhcp_service(func):
    """
    In some distros, dhcp service needs to be shutdown before agent probe
    endpoint through dhcp.
    """
    def wrapper(*args, **kwargs):
        if OSUTIL.is_dhcp_enabled():
            OSUTIL.stop_dhcp_service()
            result = func(*args, **kwargs)
            OSUTIL.start_dhcp_service()
            return result
        else:
            return func(*args, **kwargs)
    return wrapper


@allow_dhcp_broadcast
@disable_dhcp_service
def send_dhcp_request(request):
    __waiting_duration__ = [0, 10, 30, 60, 60]
    sock = None
    for duration in __waiting_duration__:
        try:
            OSUTIL.allow_dhcp_broadcast()
            response = socket_send(request)
            validate_dhcp_resp(request, response)
            return response
        except AgentNetworkError as e:
            logger.error("Failed to send DHCP request: {0}", e)
            return None
        finally:
            if sock:
                sock.close()
        time.sleep(duration)

def socket_send(request):
    sock = socket.socket(socket.AF_INET,
                         socket.SOCK_DGRAM,
                         socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", 68))
    sock.sendto(request, ("<broadcast>", 67))
    sock.settimeout(10)
    logger.verb("Send DHCP request: Setting socket.timeout=10, "
                   "entering recv")
    response = sock.recv(1024)
    return response

def build_dhcp_request(mac_addr):
    """
    Build DHCP request string.
    """
    #
    # typedef struct _DHCP {
    #     UINT8   Opcode;                    /* op:    BOOTREQUEST or BOOTREPLY */
    #     UINT8   HardwareAddressType;       /* htype: ethernet */
    #     UINT8   HardwareAddressLength;     /* hlen:  6 (48 bit mac address) */
    #     UINT8   Hops;                      /* hops:  0 */
    #     UINT8   TransactionID[4];          /* xid:   random */
    #     UINT8   Seconds[2];                /* secs:  0 */
    #     UINT8   Flags[2];                  /* flags: 0 or 0x8000 for broadcast */
    #     UINT8   ClientIpAddress[4];        /* ciaddr: 0 */
    #     UINT8   YourIpAddress[4];          /* yiaddr: 0 */
    #     UINT8   ServerIpAddress[4];        /* siaddr: 0 */
    #     UINT8   RelayAgentIpAddress[4];    /* giaddr: 0 */
    #     UINT8   ClientHardwareAddress[16]; /* chaddr: 6 byte eth MAC address */
    #     UINT8   ServerName[64];            /* sname:  0 */
    #     UINT8   BootFileName[128];         /* file:   0  */
    #     UINT8   MagicCookie[4];            /*   99  130   83   99 */
    #                                        /* 0x63 0x82 0x53 0x63 */
    #     /* options -- hard code ours */
    #
    #     UINT8 MessageTypeCode;              /* 53 */
    #     UINT8 MessageTypeLength;            /* 1 */
    #     UINT8 MessageType;                  /* 1 for DISCOVER */
    #     UINT8 End;                          /* 255 */
    # } DHCP;
    #

    # tuple of 244 zeros
    # (struct.pack_into would be good here, but requires Python 2.5)
    request = [0] * 244

    trans_id = gen_trans_id()

    # Opcode = 1
    # HardwareAddressType = 1 (ethernet/MAC)
    # HardwareAddressLength = 6 (ethernet/MAC/48 bits)
    for a in range(0, 3):
        request[a] = [1, 1, 6][a]

    # fill in transaction id (random number to ensure response matches request)
    for a in range(0, 4):
        request[4 + a] = str_to_ord(trans_id[a])

    logger.verb("BuildDhcpRequest: transactionId:%s,%04X" % (
                   hex_dump2(trans_id),
                   unpack_big_endian(request, 4, 4)))

    # fill in ClientHardwareAddress
    for a in range(0, 6):
        request[0x1C + a] = str_to_ord(mac_addr[a])

    # DHCP Magic Cookie: 99, 130, 83, 99
    # MessageTypeCode = 53 DHCP Message Type
    # MessageTypeLength = 1
    # MessageType = DHCPDISCOVER
    # End = 255 DHCP_END
    for a in range(0, 8):
        request[0xEC + a] = [99, 130, 83, 99, 53, 1, 1, 255][a]
    return array.array("B", request)

def gen_trans_id():
    return os.urandom(4)
