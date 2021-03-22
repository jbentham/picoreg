# PicoReg: PyQT5 display of RP2040 registers using SWD
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
# v0.30 JPB 22/3/21 Set I/O pins as inputs on close

import sys, re, importlib, os.path
from PyQt5 import QtCore, QtGui, QtXml, QtWidgets

VERSION = "PicoReg v0.30"

SVD_FNAME     = "rp2040.svd"
SWD_IMPORT    = "picoreg_gpio"
CORE_NAMES    = "Core 0", "Core 1"
KEY_ELEMENTS  = ["peripheral", "register", "field"]
DISP_MSEC     = 200
STATUS_MSEC   = 100
WIN_SIZE      = 640, 400
CLR_DISP      = "\x1a"
DESC_EOL      = re.compile(r"(\\n)+\n*\s*")
BITRANGE      = re.compile("(\d+):(\d+)")
COL_LABELS    = 'Peripheral', 'Base', 'Offset', 'Bits', 'Value'
TREE_FONT     = QtGui.QFont("Courier", 11)
TEXT_FONT     = QtGui.QFont("Courier", 10)
TREE_STYLE    = ("QTreeView{selection-color:#FF0000;} " +
                 "QTreeView{selection-background-color:#FFEEEE;} ")
PERIPH_COL, BASE_COL, OSET_COL = 0, 1, 2
BITS_COL,   VAL_COL,  DESC_COL = 3, 4, 5

# Import SWD driver module
def import_module(name):
    mod = None
    try:
        mod = importlib.import_module(name)
    except ModuleNotFoundError as error:
        print("Error importing %s: %s" % (name, error))
    return mod

# Handler for XML document
class XmlHandler(QtXml.QXmlDefaultHandler):
    def __init__(self, root):
        QtXml.QXmlDefaultHandler.__init__(self)
        self.root = root
        self.elem = None
        self.txt = self.err = ""
        self.elements = {}
        self.texts = []
        self.reg_count = self.field_count = 0

    # Start of an XML document
    def startDocument(self):
        print("Loading %s... " % SVD_FNAME)
        return True

    # End of an XML document
    def endDocument(self):
        print("Found %u fields in %u registers" % (self.field_count, self.reg_count))
        return True

    # Start of an XML element
    def startElement(self, namespace, name, qname, attributes):
        # Push previous element text onto stack
        self.texts.append(self.txt)
        self.txt = ""
        # If a key element..
        if name in KEY_ELEMENTS:
            # If derived from another branch, copy child elements
            if attributes.length() and attributes.index("derivedFrom") >= 0:
                dervstr = attributes.value(attributes.index("derivedFrom"))
                if dervstr in self.elements:
                    derv = self.elements[dervstr]
                    self.elem = QtWidgets.QTreeWidgetItem(self.root)
                    children = derv.clone().takeChildren()
                    self.elem.addChildren(children)
            # Add key element to tree
            else:
                self.elem = QtWidgets.QTreeWidgetItem(self.root if self.elem is None else self.elem)
        return True

    # End of an XML element
    def endElement(self, namespace, name, qname):
        if self.elem is not None:
            # If this is a peripheral, count registers
            if name=='peripheral':
                self.reg_count += self.elem.childCount()
            # If this is a register, count fields
            if name=='register':
                self.field_count += self.elem.childCount()
            # Keep key elements at same level in tree
            if name in KEY_ELEMENTS:
                self.elem = self.elem.parent()
            # Handle additional elements, not in tree
            # Set peripheral/register name, save it for lookup
            elif name == 'name':
                self.elem.setText(PERIPH_COL, self.txt)
                self.elements[self.txt] = self.elem
            # Set peripheral base address
            elif name == "baseAddress":
                self.elem.setText(BASE_COL, self.txt)
            # Calculate register address from base & offset
            elif name == "addressOffset":
                self.elem.setText(OSET_COL, "0x%04x" % int(self.txt, 16))
            # Display bit range
            elif name == "bitRange":
                self.elem.setText(BITS_COL, self.txt)
            # Get description
            elif name == "description":
                self.elem.setText(DESC_COL, self.txt)
        # Restore previous element text
        self.txt = self.texts.pop()
        return True

    # Handle characters within XML element
    def characters(self, text):
        self.txt += text
        return True

    # Handle parsing error
    def fatalError(self, ex):
        print("\nParse error in line %d, column %d: %s" % (
              ex.lineNumber(), ex.columnNumber(), ex.message()))
        return False
    def errorString(self):
        return self.err

# Widget to display tree derived from XML file
class TreeWidget(QtWidgets.QWidget):
    def __init__(self, fname):
        QtWidgets.QWidget.__init__(self)
        self.item = None
        layout = QtWidgets.QVBoxLayout(self)
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setColumnCount(VAL_COL + 1)
        self.tree.setHeaderLabels([*COL_LABELS])
        self.tree.header().setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(0, QtCore.Qt.AscendingOrder)
        self.tree.setRootIsDecorated(True)
        self.tree.setFont(TREE_FONT)
        self.tree.setStyleSheet(TREE_STYLE)
        layout.addWidget(self.tree)
        handler = XmlHandler(self.tree)
        reader = QtXml.QXmlSimpleReader()
        reader.setContentHandler(handler)
        reader.setErrorHandler(handler)
        f = QtCore.QFile(fname)
        source = QtXml.QXmlInputSource(f)
        reader.parse(source)

    # Peripheral/register/field selection has changed (mouse or kbd)
    # Print name and description
    def itemSelectionChanged(self):
        self.item = self.tree.selectedItems()[0] if self.tree.selectedItems() else None
        print(CLR_DISP, end="")
        if self.item:
            name = self.item_name(self.item)
            addr = self.item_address(self.item)
            bits = self.item_bits(self.item)
            desc = self.item_description(self.item)
            if name:
                print(name, end="")
                print(((" 0x%08x" % addr) if addr else ""), end="")
                print((" [%u:%u]" % bits) if bits is not None else "")
                print(("\n%s" % desc) if desc is not None else "")

    # Return name of selected item
    def item_name(self, item):
        s = ""
        if item:
            it = item
            while it:
                s = it.text(PERIPH_COL) + ("." if s else "") + s
                it = it.parent()
        return s

    # Return address of selected item
    def item_address(self, item):
        addr = None
        if item and item.text(BITS_COL):
            item = item.parent()
        if item and item.text(OSET_COL):
            addr = int(item.text(OSET_COL), 16)
            item = item.parent()
        if item and item.text(BASE_COL):
            base = int(item.text(BASE_COL), 16)
            addr = base if addr is None else base + addr
        return addr

    # Return bit range
    def item_bits(self, item):
        bits = None
        if item.text(BITS_COL):
            vals = BITRANGE.search(item.text(BITS_COL))
            bits = int(vals.group(1)), int(vals.group(2))
        return bits

    # Return description of item
    def item_description(self, item):
        return re.sub(DESC_EOL, "\n", item.text(DESC_COL)) if item else ""

    # Set value field of item
    def item_value_display(self, item, value):
        s = ""
        if value is not None:
            bits = self.item_bits(item)
            bits = bits if bits is not None else [31,0]
            nbits = (bits[0] - bits[1] + 1)
            val = (value >> bits[1]) & ((1 << nbits) - 1)
            s = ((" %u    " % val) if nbits<=3 else
                 (" 0x%1x"  % val) if nbits<=4 else
                 (" 0x%02x" % val) if nbits<=8 else
                 (" 0x%04x" % val) if nbits<=16 else
                 (" 0x%08x" % val))
        item.setText(VAL_COL, s)

    # Set bits fields of item
    def item_bits_display(self, item, value):
        if item and value is not None and item.childCount():
            for n in range(0, item.childCount()):
                self.item_value_display(item.child(n), value)

# Main window
class MainWindow(QtWidgets.QMainWindow):
    text_update = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
    # Create main window resources
        QtWidgets.QMainWindow.__init__(self, parent)
        self.running = False
        self.swdrv = self.conn = self.tries = None
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.do_timeout)
        self.central = QtWidgets.QWidget(self)
        self.textbox = QtWidgets.QTextEdit(self.central)
        self.textbox.setFont(TEXT_FONT)
    # Redirect print() to text box
        self.text_update.connect(self.append_text)
        sys.stdout = self
        print(VERSION)
    # Create layout
        vlayout = QtWidgets.QVBoxLayout()
        self.widget = TreeWidget(SVD_FNAME)
        vlayout.addWidget(self.widget)
        btn_layout = QtWidgets.QHBoxLayout()
        self.core_sel = QtWidgets.QComboBox(self)
        self.core_sel.addItems(CORE_NAMES)
        self.verbose_cbox = QtWidgets.QCheckBox("Verbose", self)
        self.verbose_cbox.stateChanged.connect(self.do_verbose)
        self.conn_btn = QtWidgets.QPushButton('Connect', self)
        self.conn_btn.pressed.connect(self.do_conn)
        self.single_btn = QtWidgets.QPushButton('Single', self)
        self.single_btn.pressed.connect(self.do_single)
        self.run_btn = QtWidgets.QPushButton('Run', self)
        self.run_btn.pressed.connect(self.do_run)
        self.exit_btn = QtWidgets.QPushButton('Exit', self)
        self.exit_btn.pressed.connect(self.close)
        btn_layout.addWidget(self.core_sel)
        btn_layout.addWidget(self.verbose_cbox)
        btn_layout.addWidget(self.conn_btn)
        btn_layout.addWidget(self.single_btn)
        btn_layout.addWidget(self.run_btn)
        btn_layout.addWidget(self.exit_btn)
        vlayout.addLayout(btn_layout)
        vlayout.addWidget(self.textbox)
        self.central.setLayout(vlayout)
        self.setCentralWidget(self.central)
        self.status = self.statusBar()
        self.widget.tree.itemSelectionChanged.connect(self.widget.itemSelectionChanged)
        self.do_conn(dis = True)

    # Establish SWD connection
    def connect(self):
        if self.conn:
            self.do_verbose()
            self.conn.set_core(self.core_sel.currentIndex())
            self.conn.conn_func_retry(None)
        return False if not self.conn else self.conn.connected

    # Get single value from CPU register or memory, given address
    def get_value(self, addr):
        return self.conn.conn_func_retry(self.conn.peek, addr)

    # Stop SWD connection
    def disconnect(self):
        if self.conn and self.conn.connected:
            self.conn.disconnect()
        print("Disconnected")

    # Connect/disconnect button
    def do_conn(self, dis=False):
        # Import & initialise SWD module if not already imported
        if self.swdrv is None and not dis:
            self.swdrv = import_module(SWD_IMPORT)
            if self.swdrv:
                self.conn = self.swdrv.SwdConnection()
                self.conn.open()
        # Toggle connect/disconnect
        if self.conn:
            if self.conn.connected:
                self.do_run(stop=True)
                self.disconnect()
            elif not dis:
                self.connect()
        # Enable/disable buttons
        connected = self.conn is not None and self.conn.connected
        self.conn_btn.setText("Disconnect" if connected else "Connect")
        self.single_btn.setEnabled(connected)
        self.run_btn.setEnabled(connected)
        self.core_sel.setEnabled(not connected)

    # Do single read cycle
    def do_single(self):
        if self.running:
            self.do_run(stop = True)
        self.do_read()

    # Do read cycle
    def do_read(self):
        if self.widget.item is not None:
            # Get address in CPU memory space
            addr = self.widget.item_address(self.widget.item)
            if addr is not None:
                # Get value, disconnect if error
                val = self.get_value(addr)
                if val is None:
                    print("SWD link failure")
                    self.do_run(stop = True)
                    self.do_conn(dis = True)
                else:
                    # Display register value, and bit field values
                    item = self.widget.item
                    if item.text(BITS_COL):
                        item = item.parent()
                    if item.text(OSET_COL):
                        self.widget.item_value_display(item, val)
                        self.widget.item_bits_display(item, val)
            self.status.showMessage("Reading...", STATUS_MSEC)

    # Start/stop data transfers
    def do_run(self, stop=False):
        if self.running:
            self.timer.stop()
            self.running = False
        elif not stop and self.conn and self.conn.connected:
            self.timer.start(DISP_MSEC)
            self.running = True
        self.run_btn.setText("Stop" if self.running else "Run")

    # Timer timeout
    def do_timeout(self):
        self.do_read()

    # Verbose checkbox state has changed
    def do_verbose(self):
        if self.conn and self.conn.msg:
            self.conn.msg.verbose = self.verbose_cbox.isChecked()

    # Handle sys.stdout.write: update text display
    def write(self, text):
        self.text_update.emit(str(text))
    def flush(self):
        pass

    # Append to text display (for print function)
    def append_text(self, text):
        if text.startswith(CLR_DISP):
            self.textbox.setText("")
            text = text[1:]
        cur = self.textbox.textCursor()     # Move cursor to end of text
        cur.movePosition(QtGui.QTextCursor.End)
        s = str(text)
        while s:
            head,sep,s = s.partition("\n")  # Split line at LF
            cur.insertText(head)            # Insert text at cursor
            if sep:                         # New line if LF
                cur.insertBlock()
        self.textbox.setTextCursor(cur)     # Update visible cursor

    # Window is closing
    def closeEvent(self, event):
        self.do_run(stop=True)
        self.disconnect()
        if self.conn:
            self.conn.close()

if __name__ == '__main__':
    if not os.path.isfile(SVD_FNAME):
        print("Can't find %s" % SVD_FNAME)
    else:
        print("Loading %s... " % SVD_FNAME)
        app = QtWidgets.QApplication(sys.argv)
        win = MainWindow()
        win.resize(*WIN_SIZE)
        win.show()
        win.setWindowTitle(VERSION)
        sys.exit(app.exec_())
# EOF
