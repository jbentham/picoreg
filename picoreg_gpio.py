# PicoReg: low-level driver to display RP2040 registers using SWD
#
# For detailed description, see https://iosoft.blog/picoreg
#
# Copyright (c) 2020 Jeremy P Bentham
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

import time, sys, signal, RPi.GPIO as GPIO
from ctypes import Structure, c_uint

swd_hdr =    [("Start",  c_uint, 1), ("APnDP", c_uint, 1),
             ("RnW",    c_uint, 1), ("Addr",  c_uint, 2),
             ("Parity", c_uint, 1), ("Stop",  c_uint, 1),
             ("Park",   c_uint, 1)]

swd_ack =    [("ack",  c_uint, 3)]

swd_val32p = [("value",  c_uint, 32), ("parity", c_uint, 1)]

swd_turn =   [("turn", c_uint, 1)]

NUM_TRIES       = 3     # Number of tries if error
SWD_DP          = 0     # AP/DP flag bits
SWD_AP          = 1
SWD_WR          = 0     # RD/WR flag bits
SWD_RD          = 1
DP_CORE0        = 0x01002927    # ID to select core 0 and 1
DP_CORE1        = 0x11002927
SWD_ACK_OK      = 1     # SWD Ack values
SWD_ACK_WAIT    = 2
SWD_ACK_ERROR   = 4

CLK_PIN         = 20    # Clock and data BCM pin numbers
DAT_PIN         = 21

test_addr       = 0xd0000004 # Address to test (GPIO input)

# Calculate parity of 32-bit integer
def parity32(i):
    i = i - ((i >> 1) & 0x55555555)
    i = (i & 0x33333333) + ((i >> 2) & 0x33333333)
    i = (((i + (i >> 4)) & 0x0F0F0F0F) * 0x01010101) >> 24
    return i & 1

# SWD hardware driver
class GpioDrv(object):
    def __init__(self, dev=None):
        self.dev = dev
        self.txdata = ""
        self.rxdata = ""

    # Open driver
    def open(self):
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(CLK_PIN, GPIO.OUT)
        GPIO.setup(DAT_PIN, GPIO.IN)
        return True

    # Close driver
    def close(self):
        GPIO.setup(CLK_PIN, GPIO.IN)
        GPIO.setup(DAT_PIN, GPIO.IN)
        return True

    # Add some bits to transmit buffer, dummy bits if a read cycle
    def send_bits(self, data, nbits, recv):
        n = 0
        while n < nbits:
            self.txdata += "." if recv else "1" if data & (1 << n) else "0"
            n += 1

    # Add some bytes to transmit buffer, dummy bits if a read cycle
    def send_bytes(self, data, recv):
        for b in data:
            self.send_bits(b, 8, recv)

    # Get some bits into receive buffer
    def recv_bits(self, nbits):
        val = None
        if nbits <= len(self.rxdata):
            val = n = 0
            while n < nbits:
                if self.rxdata[n] == "1":
                    val |= 1 << n
                n += 1
            self.rxdata = self.rxdata[nbits:]
        return val

    # Send bits from transmit buffer, get any receive bits
    def xfer(self):
        self.rxdata = lastbit = ""
        for bit in self.txdata:
            if bit == ".":
                self.rxdata += ("1" if GPIO.input(DAT_PIN) else "0")
                if bit != lastbit:
                    GPIO.setup(DAT_PIN, GPIO.IN)
                GPIO.output(CLK_PIN, 1)
            else:
                if lastbit == "" or lastbit == ".":
                    GPIO.setup(DAT_PIN, GPIO.OUT)
                GPIO.output(DAT_PIN, 1 if bit=="1" else 0)
                GPIO.output(CLK_PIN, 1)
            GPIO.output(CLK_PIN, 0)
            lastbit = bit
        self.txdata = ""
        return self.rxdata

# Class for a register with multiple bit-fields
class BitReg(object):
    def __init__(self, fields, recv=True):
        self.fields, self.recv = fields, recv
        class Struct(Structure):
            _fields_ = fields
        self.vals = Struct()
        self.nbits = sum([f[2] for f in self.fields])

    # Transmit the bit values
    def send_vals(self, drv):
        for field in self.fields:
            val, n = getattr(self.vals, field[0]), field[2]
            drv.send_bits(val, n, self.recv)

    # Receive the bit values
    def recv_vals(self, drv):
        for field in self.fields:
            if self.recv:
                val = drv.recv_bits(field[2])
                setattr(self.vals, field[0], val)

    # Return string with field values
    def field_vals(self):
        s = ""
        for field in self.fields:
            val = getattr(self.vals, field[0])
            s += " %s=" % field[0]
            s += ("%u" % val) if field[2]<8 else ("0x%x" % val)
        return s

# SWD message: header, ack and 32-bit value
class SwdMsg(object):
    def __init__(self, drv):
        self.drv = drv
        self.regs = []
        self.ok = self.verbose = False
        self.hdr = BitReg(swd_hdr, False)
        self.turn1 = BitReg(swd_turn, True)
        self.ack = BitReg(swd_ack, True)
        self.turn2 = BitReg(swd_turn, True)
        self.val32p = BitReg(swd_val32p, False)
        self.hdr.vals.Start = 1
        self.hdr.vals.Stop = 0
        self.hdr.vals.Park = 1

    # Verbose print of message values
    def verbose_print(self, label=""):
        if self.verbose:
            print("%s  %s" % (self.msg_vals(), label))

    # Leave dormant state: see IHI 0031F page B5-137
    def arm_wakeup_msg(self):
        self.drv.send_bytes([0xff], False)
        self.drv.send_bytes([0x92,0xF3,0x09,0x62,0x95,0x2D,0x85,0x86,
            0xE9,0xAF,0xDD,0xE3,0xA2,0x0E,0xBC,0x19], False)
        self.drv.send_bits(0, 4, False)     # Idle bits
        self.drv.send_bytes([0x1a], False)  # Activate SW-DP
        return self

    # SWD reset; at least 50 high bits then at least 2 low bits
    def swd_reset_msg(self):
        self.drv.send_bytes([0xff,0xff,0xff,0xff,0xff,0xff,0xff,0x00], False)
        if self.verbose:
            print("SWD reset")
        return self

    # Do an SWD read cycle
    def read(self, ap, addr, label=""):
        ret = self.set(ap, SWD_RD, addr).send_vals().xfer().recv_vals()
        self.verbose_print(label)
        return ret

    # Do an SWD write cycle
    def write(self, ap, addr, val, label=""):
        ret = self.set(ap, SWD_WR, addr, val).send_vals().xfer().recv_vals()
        self.verbose_print(label)
        return ret

    # Set header for a read or write cycle, with data if write
    def set(self, ap, rd, addr, val=0):
        self.hdr.vals.APnDP = ap
        self.hdr.vals.RnW = rd
        self.hdr.vals.Addr = addr >> 2
        self.hdr.vals.Parity = (ap ^ rd ^ (addr>>2) ^ (addr>>3)) & 1
        self.val32p.vals.value = val;
        self.val32p.vals.parity = parity32(val)
        self.regs = [self.hdr, self.turn1, self.ack]
        self.regs += [self.val32p, self.turn2] if rd else [self.turn2, self.val32p]
        self.val32p.recv = rd
        return self

    # Send values to transmit buffer
    def send_vals(self):
        for reg in self.regs:
            reg.send_vals(self.drv)
        return self

    # Get values from receive buffer
    def recv_vals(self):
        for reg in self.regs:
            reg.recv_vals(self.drv)
        return self.check()

    # Transfer values to & from target
    def xfer(self):
        self.drv.xfer()
        return self

    # Check ack-value, and parity if a read cycle
    def check(self):
        self.ok = (self.ack.vals.ack == 1 and
                  (self.hdr.vals.RnW == 0 or
                   parity32(self.val32p.vals.value) == self.val32p.vals.parity))
        return self

    # Return string with header and data values
    def field_vals(self):
        return (self.hdr.field_vals() + self.ack.field_vals() +
                self.val32p.field_vals() +
                (" OK" if self.check() else " ERROR"))

    # Return string with key message values
    def msg_vals(self):
        return ("%s %s %X 0x%08x %s" % (
            "Rd" if self.hdr.vals.RnW else "Wr",
            "AP" if self.hdr.vals.APnDP else "DP",
            self.hdr.vals.Addr << 2, self.val32p.vals.value,
            "OK" if self.ok else "Ack %u" % self.ack.vals.ack))

# SWD connection class
class SwdConnection(object):
    def __init__(self):
        self.drv = self.msg = None
        self.connected = False
        self.dp = DP_CORE0

    # Open the hardware driver
    def open(self):
        self.drv = GpioDrv()
        if self.drv.open():
            self.msg = SwdMsg(self.drv)
            return True
        return False

    # Close the hardware driver
    def close(self):
        self.drv.close()
        self.drv = None

    # Enable or disable verbose messages
    def verbose(self, enable):
        self.msg.verbose = enable

    # Set core number (0 or 1)
    def set_core(self, core):
        self.dp = DP_CORE1 if core else DP_CORE0

    # Connect to a device, given the Debug Port
    # CPU doesn't acknowledge transmission, so assume OK
    def connect(self):
        print("SWD connection restart")
        self.msg.arm_wakeup_msg().swd_reset_msg().xfer()
        self.msg.write(SWD_DP, 0xc, self.dp)
        return True

    # Disconnect from device
    def disconnect(self):
        self.msg.swd_reset_msg().xfer()
        self.connected = False

    # Get DP ID reg value (0x0bc12477 for RP2040)
    def get_dpidr(self):
        self.msg.read(SWD_DP, 0, "Read DPIDR")
        return self.msg.val32p.vals.value if self.msg.ok else None

    # Power up the interface, return AHB ID reg value
    def power_up(self):
        self.msg.write(SWD_DP, 0,   0x1e, "Clear error bits")
        self.msg.write(SWD_DP, 8,   0,    "Set AP and DP bank 0")
        self.msg.write(SWD_DP, 4,   0x50000001, "Power up")
        self.msg.read (SWD_DP, 4,         "Read status")
        self.msg.write(SWD_DP, 8,   0xf0, "Set AP bank F, DP bank 0")
        self.msg.read (SWD_AP, 0xc,       "Read AP addr 0xFC")
        self.msg.read (SWD_DP, 0xc,       "Read AP result (AHB3-AP IDR)")
        idr = self.msg.val32p.vals.value if self.msg.ok else None
        self.msg.write(SWD_AP, 0,   0xA2000012, "Auto-increment word values")
        self.msg.write(SWD_DP, 8,   0,    "Set AP and DP bank 0")
        return idr if self.msg.ok else None

    # Read a 32-bit location in the CPU's memory
    def peek(self, addr):
        self.msg.write(SWD_AP, 4, addr,  "Set AP address")
        self.msg.read (SWD_AP, 0xc,      "AP read cycle")
        self.msg.read (SWD_DP, 0xc,      "DP read result")
        return self.msg.val32p.vals.value if self.msg.ok else None

    # Add a retry loop around connection and user function
    # Return value from user function; if no function, just connect
    def conn_func_retry(self, func, *args):
        val = None
        tries = NUM_TRIES
        while tries>0 and val is None:
            if not self.connected and self.connect():
                self.connected = True
                dpidr = self.get_dpidr()
                if dpidr is None:
                    print("Can't read CPU ID")
                    self.connected = False
                else:
                    print("DPIDR 0x%08x" % dpidr)
                    apidr = self.power_up()
                    if apidr is None:
                        print("Can't connect to AP")
                        self.connected = False
                    else:
                        tries = NUM_TRIES
            if not self.connected:
                tries -= 1
            elif not func:
                break
            else:
                val = func(*args)
                if val is None:
                    self.connected = False
                    tries -= 1
                else:
                    tries = NUM_TRIES
        return val

    # Handle ctrl-C
    def sigint_handler(self, *args):
        print("Terminating")
        self.disconnect()
        self.close()
        sys.exit(0)

if __name__ == "__main__":
    conn = SwdConnection()
    conn.open()
    tries = NUM_TRIES
    repeat = False
    val = None
    args = sys.argv[1:]
    while args:
        if args[0] == "-r":
            repeat = True
        if args[0] == "-v":
            conn.verbose(True)
        else:
            try:
                test_addr = int(args[0], 16)
            except:
                pass
        args.pop(0)
    signal.signal(signal.SIGINT, conn.sigint_handler)

    while True:
        val = conn.conn_func_retry(conn.peek, test_addr)
        if val is not None:
            print("0x%08x: 0x%08x" % (test_addr, val))
        if val is None or not repeat:
            break
        time.sleep(0.1)
    conn.disconnect()

# EOF
