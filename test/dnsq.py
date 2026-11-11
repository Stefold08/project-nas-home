"""Piccolo client DNS per le prove: manda una domanda da un IP sorgente a scelta."""
import random, socket, struct, sys

def build(name, qtype=1):
    tid = random.randint(0, 65535)
    head = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
    q = b"".join(bytes([len(p)]) + p.encode() for p in name.strip(".").split(".")) + b"\0"
    return tid, head + q + struct.pack(">HH", qtype, 1)

def ask(name, src="127.0.0.1", server=("127.0.0.1", 5353), qtype=1, timeout=2.0):
    tid, pkt = build(name, qtype)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((src, 0)); s.settimeout(timeout)
    s.sendto(pkt, server)
    data, _ = s.recvfrom(4096)
    s.close()
    rcode = data[3] & 0x0F
    ancount = struct.unpack(">H", data[6:8])[0]
    return {"rcode": rcode, "answers": ancount}

if __name__ == "__main__":
    src, name = sys.argv[1], sys.argv[2]
    qtype = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    print(ask(name, src, qtype=qtype))
