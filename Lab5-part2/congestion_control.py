import enum
import logging
import queue
import struct
import threading
import datetime
import matplotlib.pyplot as plt
import random

class PacketType(enum.IntEnum):
    DATA = ord('D')
    ACK = ord('A')
    SYN = ord('S')

class Packet:
    _PACK_FORMAT = '!BI'
    _HEADER_SIZE = struct.calcsize(_PACK_FORMAT)
    MAX_DATA_SIZE = 1400

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
        header = struct.pack(Packet._PACK_FORMAT, self._type.value, self._seq_num)
        return header + self._data
       
    @classmethod
    def from_bytes(cls, raw):
        header = struct.unpack(Packet._PACK_FORMAT, raw[:Packet._HEADER_SIZE])
        type = PacketType(header[0])
        seq_num = header[1]
        data = raw[Packet._HEADER_SIZE:]
        return Packet(type, seq_num, data)

    def __str__(self):
        return "{} {}".format(self._type.name, self._seq_num)

class Sender:
    _BUF_SIZE = 5000
    DUPLICATE_ACK_THRESHOLD = 3

    def __init__(self, ll_endpoint, use_slow_start=False, use_fast_retransmit=False):
        self._ll_endpoint = ll_endpoint
        self._rtt = 2 * (ll_endpoint.transmit_delay + ll_endpoint.propagation_delay)

        self._last_ack_recv = -1
        self._last_seq_sent = -1
        self._last_seq_written = 0
        self._buf = [None] * Sender._BUF_SIZE
        self._buf_slot = threading.Semaphore(Sender._BUF_SIZE)

        self._use_slow_start = use_slow_start
        self._use_fast_retransmit = use_fast_retransmit
        self._cwnd = 1 if use_slow_start else 1
        self._dup_acks = 0

        self._plotter = CwndPlotter()

        self._shutdown = False
        self._recv_thread = threading.Thread(target=self._recv)
        self._recv_thread.start()

        packet = Packet(PacketType.SYN, 0)
        self._buf_slot.acquire()
        self._buf[0] = {"packet" : packet, "send_time" : None}
        self._timer = None
        self._transmit(0)

    def _transmit(self, seq_num):
        slot = seq_num % Sender._BUF_SIZE
        packet = self._buf[slot]["packet"]
        self._ll_endpoint.send(packet.to_bytes())
        send_time = datetime.datetime.now()

        if (self._last_seq_sent < seq_num):
            self._last_seq_sent = seq_num

        if self._buf[slot]["send_time"] is None:
            logging.info("Transmit: {}".format(packet))
            self._buf[slot]["send_time"] = send_time
        else:
            logging.info("Retransmit: {}".format(packet))
            self._buf[slot]["send_time"] = 0

        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(2 * self._rtt, self._timeout)
        self._timer.start()

    def send(self, data):
        for i in range(0, len(data), Packet.MAX_DATA_SIZE):
            self._send(data[i:i+Packet.MAX_DATA_SIZE])

    def _send(self, data):
        self._buf_slot.acquire()
        self._last_seq_written += 1
        packet = Packet(PacketType.DATA, self._last_seq_written , data)
        slot = packet.seq_num % Sender._BUF_SIZE
        self._buf[slot] = {"packet" : packet, "send_time" : None}

        if (self._last_seq_sent - self._last_ack_recv < int(self._cwnd)):
            self._transmit(packet.seq_num)
        
    def _timeout(self):
        self._cwnd = max(1, self._cwnd // 2)
        logging.info("CWND: {}".format(self._cwnd))
        self._plotter.update_cwnd(self._cwnd)

        for seq_num in range(self._last_ack_recv + 1, self._last_seq_sent + 1):
            slot = seq_num % Sender._BUF_SIZE
            self._buf[slot]["send_time"] = None

        oldest_unacknowledged = self._last_ack_recv + 1
        logging.info("Resending after timeout")
        self._transmit(oldest_unacknowledged)

    def _recv(self):
            while (not self._shutdown) or (self._last_ack_recv < self._last_seq_sent):
                raw = self._ll_endpoint.recv()
                if raw is None:
                    continue
                packet = Packet.from_bytes(raw)
                recv_time = datetime.datetime.now()
                logging.info("Received ACK: {}".format(packet))

                if (packet.seq_num <= self._last_ack_recv):
                    continue

                while (self._last_ack_recv < packet.seq_num):
                    self._last_ack_recv += 1
                    slot = self._last_ack_recv % Sender._BUF_SIZE

                    send_time = self._buf[slot]["send_time"]
                    if (send_time != None and send_time != 0):
                        elapsed = recv_time - send_time
                        self._rtt = self._rtt * 0.9 + elapsed.total_seconds() * 0.1
                        logging.info("Updated RTT estimate: {}".format(self._rtt))

                    self._buf[slot] = None
                    self._buf_slot.release()

                if (self._last_seq_sent < self._last_ack_recv):
                    self._last_seq_sent = self._last_ack_recv

                if (self._timer != None and self._last_ack_recv == self._last_seq_sent):
                    self._timer.cancel()
                    self._timer = None

                if self._use_fast_retransmit:
                    logging.info("Packet seq: {}".format(packet.seq_num))
                    logging.info("Last seq sent: {}".format(self._last_seq_sent))
                    logging.info("Last ack received: {}".format(self._last_ack_recv))
                    if packet.seq_num == self._last_ack_recv:
                        self._dup_acks += 1
                        logging.info("Duplicate ACK count: {}".format(self._dup_acks))
                        if self._dup_acks == Sender.DUPLICATE_ACK_THRESHOLD:
                            logging.debug("Fast retransmit triggered")
                            ssthresh = max(1, self._cwnd // 2)  # New ssthresh
                            self._cwnd = ssthresh + 3  # New cwnd for retransmission
                            self._transmit(self._last_ack_recv + 1)  # Retransmit lost packet
                            self._dup_acks = 0
                            continue
                        else:
                            logging.info("Assuming packet loss")
                            self._cwnd += 1  # Assume packet loss
                elif self._use_slow_start:
                    logging.info("Slow start is Enabled!")
                    self._cwnd *= 2
                else:
                    logging.info("AIMD is Enabled!")
                    self._cwnd = self._cwnd + 1 / self._cwnd  

                logging.debug("CWND: {}".format(self._cwnd))
                self._plotter.update_cwnd(self._cwnd)

                while  ((self._last_seq_sent < self._last_seq_written) and
                        (self._last_seq_sent - self._last_ack_recv < int(self._cwnd))):
                    self._transmit(self._last_seq_sent + 1)
            logging.info("Transmission finished.")

class Receiver:
    _BUF_SIZE = 1000

    def __init__(self, ll_endpoint, loss_probability=0):
        self._ll_endpoint = ll_endpoint

        self._last_ack_sent = -1
        self._max_seq_recv = -1
        self._recv_window = [None] * Receiver._BUF_SIZE

        self._ready_data = queue.Queue()

        self._recv_thread = threading.Thread(target=self._recv)
        self._recv_thread.daemon = True
        self._recv_thread.start()

        self._dup_ack_count = 0

    def recv(self):
        return self._ready_data.get()

    def _recv(self):
            slow_start = True  
            while True:
                raw = self._ll_endpoint.recv()
                if raw is None:
                    continue
                packet = Packet.from_bytes(raw)
                logging.info("Received: {}".format(packet))

                if packet.seq_num <= self._last_ack_sent:
                    ack = Packet(PacketType.ACK, self._last_ack_sent)
                    self._ll_endpoint.send(ack.to_bytes())
                    logging.info("Sent: {}".format(ack))
                    continue

                slot = packet.seq_num % Receiver._BUF_SIZE
                self._recv_window[slot] = packet.data
                if packet.seq_num > self._max_seq_recv:
                    self._max_seq_recv = packet.seq_num

                ack_num = self._last_ack_sent
                while ack_num < self._max_seq_recv:
                    next_slot = (ack_num + 1) % Receiver._BUF_SIZE
                    data = self._recv_window[next_slot]

                    if data is None:
                        break

                    ack_num += 1
                    self._ready_data.put(data)
                    self._recv_window[next_slot] = None

                self._last_ack_sent = ack_num
                ack = Packet(PacketType.ACK, self._last_ack_sent)
                self._ll_endpoint.send(ack.to_bytes())
                logging.info("Sent: {}".format(ack))

                if slow_start:
                    logging.info("Entering slow start. Congestion window size: {}".format(ack_num))
                else:
                    logging.info("Congestion avoidance phase. Congestion window size: {}".format(ack_num))

                if slow_start and ack_num >= 10:  
                    slow_start = False
                    logging.debug("Exiting slow start. Congestion window size: {}".format(ack_num))

                if ack_num > self._last_ack_sent + 1:
                    self._dup_ack_count += 1
                    logging.debug("Duplicate ACK received. Count: {}".format(self._dup_ack_count))
                    if self._dup_ack_count >= Sender.DUPLICATE_ACK_THRESHOLD:
                        logging.debug("Fast retransmit triggered")
                        self._ll_endpoint.notify_fast_retransmit()
                        self._last_ack_sent = ack_num - 1
                        self._dup_ack_count = 0
                        self._max_seq_recv = ack_num - 1
            logging.info("Reception finished.")


class CwndPlotter:
    def __init__(self):
        self._start_time = datetime.datetime.now()
        self._times = [0]
        self._cwnd_values = [1]

    def update_cwnd(self, cwnd):
        elapsed_time = (datetime.datetime.now() - self._start_time).total_seconds()
        self._times.append(elapsed_time)
        self._cwnd_values.append(cwnd)
        plt.plot(self._times, self._cwnd_values, 'b-')
        plt.xlabel('Time (s)')
        plt.ylabel('Congestion Window Size')
        plt.title('Congestion Window Evolution')
        plt.savefig('cwnd.png')

