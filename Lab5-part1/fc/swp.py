import enum
import logging
import llp
import queue
import struct
import threading
import random

class SWPType(enum.IntEnum):
    DATA = ord('D')
    ACK = ord('A')

class SWPPacket:
    _PACK_FORMAT = '!BI'
    _HEADER_SIZE = struct.calcsize(_PACK_FORMAT)
    MAX_DATA_SIZE = 1400 # Leaves plenty of space for IP + UDP + SWP header 

    def __init__(self, type, seq_num, data=b''):
        self._type = type
        self._seq_num = seq_num
        self._data = data

    @property
    def type(self):
        return self._type

    @property
    def seq_num(self):
        return self._seq_num
    
    @property
    def data(self):
        return self._data

    def to_bytes(self):
        header = struct.pack(SWPPacket._PACK_FORMAT, self._type.value, 
                self._seq_num)
        return header + self._data
       
    @classmethod
    def from_bytes(cls, raw):
        header = struct.unpack(SWPPacket._PACK_FORMAT,
                raw[:SWPPacket._HEADER_SIZE])
        type = SWPType(header[0])
        seq_num = header[1]
        data = raw[SWPPacket._HEADER_SIZE:]
        return SWPPacket(type, seq_num, data)

    def __str__(self):
        return "%s %d %s" % (self._type.name, self._seq_num, repr(self._data))

class LLPEndpoint:
    def __init__(self, local_address=None, remote_address=None, 
            loss_probability=0):
        self._local_address = local_address
        self._remote_address = remote_address
        self._loss_probability = loss_probability

        self._socket = socket.socket(type=socket.SOCK_DGRAM)
        if self._local_address is not None:
            self._socket.bind(self._local_address)
        if self._remote_address is not None:
            self._socket.connect(self._remote_address)
            self._local_address = self._socket.getsockname()

        self._shutdown = False

    def send(self, raw_bytes):
        logging.debug('LLP sent: %s' % raw_bytes)
        return self._socket.send(raw_bytes)

    def recv(self, max_size=4096):
        dropped = True
        while dropped:
            if self._remote_address is None:
                try:
                    (raw_bytes, address) = self._socket.recvfrom(max_size)
                except OSError:
                    return None
                self._remote_address = address
                self._socket.connect(self._remote_address)
            else:
                try:
                    raw_bytes = self._socket.recv(max_size)
                except OSError:
                    return None

            if len(raw_bytes) == 0:
                return None

            if random.random() >= self._loss_probability:
                dropped = False
            else:
                logging.debug('LLP dropped: %s' % raw_bytes)
        
        logging.debug('LLP received: %s' % raw_bytes)
        return raw_bytes

    def shutdown(self):
        if (not self._shutdown):
            self._socket.shutdown(socket.SHUT_RDWR)
            self._socket.close()
            self._shutdown = True


class SWPSender:
    _SEND_WINDOW_SIZE = 5
    _TIMEOUT = 1

    def __init__(self, remote_address, loss_probability=0):
        self._llp_endpoint = llp.LLPEndpoint(remote_address=remote_address,
                loss_probability=loss_probability)

        # Start receive thread
        self._recv_thread = threading.Thread(target=self._recv)
        self._recv_thread.start()

        self._send_window = {}  # Dictionary to keep track of sent packets and their timers
        self._send_window_lock = threading.Lock()
        self._next_seq_num = 0
        self._ack_received = threading.Event()

    def send(self, data):
        for i in range(0, len(data), SWPPacket.MAX_DATA_SIZE):
            self._send(data[i:i+SWPPacket.MAX_DATA_SIZE])

    def _send(self, data):
        with self._send_window_lock:
            if len(self._send_window) >= self._SEND_WINDOW_SIZE:
                logging.debug("Send window full. Waiting for acknowledgment.")
                self._ack_received.wait()

            seq_num = self._next_seq_num
            self._next_seq_num += 1

            packet = SWPPacket(SWPType.DATA, seq_num, data)
            self._llp_endpoint.send(packet.to_bytes())

            # Start a timer for retransmission
            timer = threading.Timer(self._TIMEOUT, self._retransmit, args=[seq_num])
            timer.start()

            self._send_window[seq_num] = (packet, timer)

    def _retransmit(self, seq_num):
        with self._send_window_lock:
            if seq_num in self._send_window:
                logging.debug("Retransmitting packet with sequence number %d" % seq_num)
                packet, timer = self._send_window[seq_num]
                self._llp_endpoint.send(packet.to_bytes())
                # Restart the timer
                timer = threading.Timer(self._TIMEOUT, self._retransmit, args=[seq_num])
                timer.start()
                self._send_window[seq_num] = (packet, timer)

    def _recv(self):
        while True:
            # Receive SWP packet
            raw = self._llp_endpoint.recv()
            if raw is None:
                continue
            packet = SWPPacket.from_bytes(raw)
            logging.debug("Received: %s" % packet)

            if packet.type == SWPType.ACK:
                with self._send_window_lock:
                    seq_num = packet.seq_num
                    if seq_num in self._send_window:
                        packet, timer = self._send_window.pop(seq_num)
                        timer.cancel()
                        self._ack_received.set()
                    else:
                        logging.debug("Received acknowledgment for unknown sequence number %d" % seq_num)


class SWPReceiver:
    _RECV_WINDOW_SIZE = 5

    def __init__(self, local_address, loss_probability=0):
        self._llp_endpoint = llp.LLPEndpoint(local_address=local_address, 
                loss_probability=loss_probability)

        # Received data waiting for application to consume
        self._ready_data = queue.Queue()

        # Start receive thread
        self._recv_thread = threading.Thread(target=self._recv)
        self._recv_thread.start()
        
        self._expected_seq_num = 0
        self._recv_lock = threading.Lock()

    def recv(self):
        return self._ready_data.get()

    def _recv(self):
        while True:
            # Receive data packet
            raw = self._llp_endpoint.recv()
            packet = SWPPacket.from_bytes(raw)
            logging.debug("Received: %s" % packet)
            
            if packet.type == SWPType.DATA:
                with self._recv_lock:
                    seq_num = packet.seq_num
                    if seq_num == self._expected_seq_num:
                        self._ready_data.put(packet.data)
                        self._expected_seq_num += 1
                        self._send_ack(seq_num)
                    elif seq_num > self._expected_seq_num:
                        logging.debug("Out of order packet received. Expected sequence number: %d, Received: %d" % (self._expected_seq_num, seq_num))
                    else:
                        self._send_ack(seq_num)
            else:
                logging.debug("Received non-data packet. Ignored.")

    def _send_ack(self, seq_num):
        ack_packet = SWPPacket(SWPType.ACK, seq_num)
        self._llp_endpoint.send(ack_packet.to_bytes())
