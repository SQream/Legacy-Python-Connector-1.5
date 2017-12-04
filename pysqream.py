from __future__ import absolute_import, division, print_function, unicode_literals

"""
Python 2.7/3.3+ connector for SQream DB

Tested on SQream protocols 4 and 5

Usage example:
    Import the `pysqream.py` file into your own codebase::

        import pysqream as sq
        import atexit
        from datetime import date, datetime


        sc = sq.Connector()
        atexit.register(sc.close)
        sc.connect(host='<ip>', database='<database>', user='<username>', password='<password>',
                    port=<server port>, clustered=<true/false - based on your installation>, timeout=<socket timeout>)
        qr = sc.query("SELECT x,y FROM t")
        print(sc.cols_names())
        print(sc.cols_types())
        print(sc.cols_to_rows())


.. _Python2/3 connector for SQream DB - GitHub:
https://github.com/SQream/sqream-connector-python
"""

import sys, socket, ssl, json, atexit, array
from struct import pack, unpack
from datetime import date, datetime
from time import gmtime
from itertools import groupby

from threading import Event
try: #Py2
    import thread
    from Queue import Queue, Empty, Full
except: # Py3
    import _thread as thread
    from queue import Queue, Empty, Full

try:
    #If numpy is installed, allows much faster conversion of datetime longs to python objects
    import numpy as np  
except:
    NP = False
else:
    from numpy import ma
    NP = True

# Defaults constants
PROTOCOL_VERSION = 4           # <==== Update to 5 here if using a new SQream version
DEFAULT_HOSTNAME = "127.0.0.1"
DEFAULT_SQREAM_PORT = 5000
SQL_SQREAM_PORT = 5100
DEFAULT_BUFFER_SIZE = 4096
DEFAULT_NETWORK_CHUNKSIZE = 10000
FLUSH_SIZE = 65536     # Default flush size for set() operations

VER = sys.version_info
MAJOR = VER[0]


column_codes_for_array =    {'ftBool': 'B',   
                            'ftUByte': 'B',
                             'ftShort': 'h',
                            'ftInt': 'i',  
                            'ftLong': 'q',
                            'ftFloat': 'f',                 
                            'ftDouble': 'd',
                            'ftDate': 'i',
                            'ftDateTime': 'q',
                            'ftVarchar': None,
                            'ftBlob':    None
                            }

sqream_typenames_to_codes = { 'BOOLEAN':  'ftBool', 
                            'TINYINT':  'ftUByte',
                            'SMALLINT': 'ftShort',
                            'INT':      'ftInt', 
                            'BIGINT':   'ftLong', 
                            'FLOAT':    'ftDouble',
                            'REAL':     'ftFloat',
                            'DATE':     'ftDate',
                            'DATETIME': 'ftDateTime',
                            'TIMESTAMP':'ftDateTime',
                            'VARCHAR':  'ftVarchar',
                            'NVARCHAR': 'ftBlob'
                            }    

"""
Conversion methods from SQream protocol to Python
"""


# 4b int to Date
def int_to_date(d):
    y = int((10000 * d + 14780) // 3652425)
    ddd = int(d - (y * 365 + y // 4 - y // 100 + y // 400))
    if (ddd < 0):
        y -= 1
        ddd = int(d - (y * 365 + y // 4 - y // 100 + y // 400))
    mi = int((52 + 100 * ddd) // 3060)
    yyyy = int((y + (mi + 2) // 12))
    mm = int(((mi + 2) % 12 + 1))
    dd = int((ddd - (mi * 306 + 5) // 10 + 1))
    return date(yyyy, mm, dd)


# 8b int to Timestamp
def long_to_datetime(dts):
    u = (dts >> 32)
    l = dts & 0xffffffff
    d = int_to_date(u)
    msec = int(l) % 1000
    l //= 1000
    sec = l % 60
    l //= 60
    min = l % 60
    l //= 60
    hour = int(l)
    return datetime(d.year, d.month, d.day, hour, min, sec, msec)


# Conversion helper to deal with dates
def conv_data_type(type, data):
    # Type conversions for unpack
    typeconversion = {"ftInt": "i",
                      "ftUByte": "B",
                      "ftShort": "h",
                      "ftLong": "q",
                      "ftFloat": "f",
                      "ftBool": "?",
                      "ftDouble": "d",
                      "ftDate": "i",
                      "ftDateTime": "q",
                      "ftVarchar": None
                      }

    if type == "ftDate":
        unpack_type = typeconversion[type]
        d = unpack(unpack_type, data)[0]
        return int_to_date(d)
    elif type == "ftDateTime":
        unpack_type = typeconversion[type]
        dt = unpack(unpack_type, data)[0]
        return long_to_datetime(dt)
    else:
        unpack_type = typeconversion[type]
        return unpack(unpack_type, data)[0]


# Class describing column metadata
class SqreamColumn(object):
    def __init__(self):
        self._type_name = None
        self._type_size = None
        self._column_name = None
        self._column_size = None
        self._isTrueVarChar = False
        self._nullable = False
        self._column_data = []

    def set_type_name(self, type_name):
        self._type_name = type_name

    def get_type_name(self):
        return self._type_name

    def set_type_size(self, type_size):
        self._type_size = type_size

    def get_type_size(self):
        return self._type_size

    def set_column_name(self, column_name):
        self._column_name = column_name

    def get_column_name(self):
        return self._column_name

    def set_column_size(self, column_size):
        self._column_size = column_size

    def get_column_size(self):
        return self._column_size

    def set_isTrueVarChar(self, isTrueVarChar):
        self._isTrueVarChar = isTrueVarChar

    def get_isTrueVarChar(self):
        return self._isTrueVarChar

    def set_nullable(self, nullable):
        self._nullable = nullable

    def get_nullable(self):
        return self._nullable

    def set_column_data(self, column_data):
        self._column_data = column_data

    def append_column_data(self, column_data):
        self._column_data += column_data

    def get_column_data(self):
        return self._column_data


# Connection object with sockets and ports and stuff

class SqreamConn(object):
    def __init__(self, username=None, password=None, database=None, host=None, port=None, clustered=False, timeout=15):
        self.get_nulls = self.get_nulls2 if MAJOR==2 else self.get_nulls3
        self._socket = None
        self._user = username
        self._password = password
        self._database = database
        self._host = host
        self._port = port
        self._clustered = clustered
        self._timeout = timeout

    HEADER_LEN = 10

    def set_socket(self, sock):
        assert isinstance(sock, (object, socket))
        self._socket = sock

    def set_user(self, username):
        self._user = username

    def set_password(self, password):
        self._password = password

    def set_database(self, database):
        self._database = database

    def set_host(self, host):
        self._host = host

    def set_port(self, port):
        self._port = port

    def set_clustered(self, clustered):
        self._clustered = clustered

    def open_socket(self):
        try:
            self.set_socket(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
            self._socket.settimeout(self._timeout)
        except socket.error as err:
            self.set_socket(None)
            raise RuntimeError("Error from SQream: " + str(err))
        except:
            raise RuntimeError("Other error")
    
    ''' SSL interlude : 
        ssl.wrap_socket(), ssl.get_server_certificate(addr, ssl_version=PROTOCOL_SSLv23, ca_certs=None)
    '''

    def cloak_socket(self, sock = None):
        ''' Wrap a socket to make it an SSL socket'''      
        try:
            self._socket = ssl.wrap_socket(sock or self._socket, ssl_version=ssl.PROTOCOL_TLSv1, ciphers="ADH-AES256-SHA")
        except:
            print ("Error wrapping socket")
    
    def close_socket(self):
        if self._socket:
            try:
                self._socket.close()
                self.set_socket(None)
            except(socket.error, AttributeError):
                pass

    def open_connection(self, ip=None, port=None):
        if ip is not None:
            tcp_ip = ip
        else:
            tcp_ip = DEFAULT_HOSTNAME
        self.set_host(tcp_ip)

        if port is not None:
            tcp_port = port
        else:
            tcp_port = DEFAULT_SQREAM_PORT
        self.set_port(tcp_port)

        try:
            self._socket.connect((tcp_ip, tcp_port))
        except socket.error as err:
            if self._socket:
                self.close_connection()
            raise RuntimeError("Couldn't connect to SQream server - " + str(err))
        except:
            print("Other error upon open connection")

    def close_connection(self):
        self.close_socket()

    def create_connection(self, ip, port):
        self.open_socket()
        self.open_connection(ip, port)

    @staticmethod
    def len2ind(lens):
        ind = []
        idx = 0
        for i in lens:
            idx += i
            ind.append(idx)
        return ind

    def bytes2val(self, col_type, column_data_row):
        if col_type != "ftVarchar":
            column_data_row = conv_data_type(col_type, column_data_row)
        else:
            column_data_row = column_data_row.replace(b'\x00', b'')
            column_data_row = column_data_row.rstrip()
        return column_data_row

    def readcolumnbytes(self, column_bytes):
        chunks = []
        bytes_rcvd = 0
        while bytes_rcvd < column_bytes:
            chunk = self.socket_recv(min(column_bytes - bytes_rcvd, DEFAULT_BUFFER_SIZE))
            if chunk == b'':
                raise RuntimeError("socket connection broken")
            chunks.append(chunk)
            bytes_rcvd += len(chunk)
        column_data = b''.join(chunks)
        return column_data

    @staticmethod
    def cmd2bytes(cmd_str, binary = False):
        ''' Packing command string to bytes and adding 10 bit header '''
        
        cmd_bytes_1 = bytearray([2])                 # Protocol version
        
        if not binary:
            cmd_bytes_2 = bytearray([1])             # Vote 1 for text
            cmd_bytes_4 = cmd_str.encode('ascii')
        else:
            cmd_bytes_2 = bytearray([2])             # 2 for binary
            cmd_bytes_4 = cmd_str
            
        cmd_bytes_3 = pack('q', len(cmd_bytes_4))
        cmd_bytes = cmd_bytes_1 + cmd_bytes_2 + cmd_bytes_3 + cmd_bytes_4
        
        return cmd_bytes

    def socket_recv(self, param):
        try:
            data_recv = self._socket.recv(param)
            # TCP says recv will only read 'up to' param bytes, so keep filling buffer
            remainder = param - len(data_recv)
            while remainder > 0:
                data_recv += self._socket.recv(remainder)
                remainder = param - len(data_recv)
            if b'{"error"' in data_recv:
                raise RuntimeError("Error from SQream: " + repr(data_recv))
        except socket.error as err:
            self.close_connection()
            self.set_socket(None)
            raise RuntimeError("Error from SQream: " + str(err))
        except RuntimeError as e:
            raise RuntimeError(e)
        except:
            raise RuntimeError("Other error while receiving from socket")
        return data_recv

    def exchange(self, cmd_str, close=False, binary = False):
        # If close=True, then do not expect to read anything back
        cmd_bytes = self.cmd2bytes(cmd_str, binary)
        try:
            self._socket.settimeout(None)
            self._socket.sendall(cmd_bytes)
        except socket.error as err:
            self.close_connection()
            self.set_socket(None)
            raise RuntimeError("Error from SQream: " + str(err))
        if close is False:
            data_recv = self.socket_recv(self.HEADER_LEN)
            ver_num = unpack('b', bytearray([data_recv[0]]))[0]
            if ver_num not in (4,5):   # Expecting 4 or 5  # if ver_num != PROTOCOL_VERSION: 
                raise RuntimeError(
                    "SQream protocol version mismatch. Expecting " + str(PROTOCOL_VERSION) + ", but got " + str(
                        ver_num) + ". Is this a newer/older SQream server?")
            val_len = unpack('q', data_recv[2:])[0]
            data_recv = self.socket_recv(val_len)
            return data_recv
        else:
            return

    def connect(self, database, username, password):
        if self._clustered is False:
            self.connect_unclustered(database, username, password)
        else:
            self.connect_clustered(database, username, password)

    def connect_clustered(self, database, username, password):
        read_len_raw = self.socket_recv(4)  # Read 4 bytes to find length of how much to read
        read_len = unpack('i', read_len_raw)[0]
        if read_len > 15 or read_len < 7:
            raise RuntimeError("Clustered connection requires a length of between 7 and 15, but I got " + str(
                read_len) + ". Perhaps this connection should be unclustered?")
        # Read the number of bytes, which is the IP in string format
        ip_addr = self.socket_recv(read_len)
        # Now read port
        port_raw = self.socket_recv(4)
        port = unpack('i', port_raw)[0]
        if port < 1000 or port > 65535:
            raise RuntimeError("Port out of bounds (1000 - 65535): " + str(port) + ".")
        self.close_connection()
        self.set_host(ip_addr)
        self.set_port(port)
        self.set_clustered(False)
        self.create_connection(ip_addr, port)
        self.connect_unclustered(database, username, password)

    def connect_unclustered(self, database, username, password):
        cmd_str = """{{"connectDatabase":"{0}","password":"{1}","username":"{2}"}}""".format(
            database.replace('"', '\\"')
            , password.replace('"', '\\"')
            , username.replace('"', '\\"'))
        self.exchange(cmd_str)
    #the "get_nulls" function are for python2/3 compatability for reading bytes
    def get_nulls2(self,column_data):
          return map(lambda c: unpack('b', bytes(c))[0], column_data)
          # or return [ord(c) for c in column_data]

    def get_nulls3(self,column_data):
       return [c for c in column_data]


    def execute(self, query_str):
        err = []
        query_data = list()
        
        if PROTOCOL_VERSION == 5:    # remove if clause once everyone's on 5
            # getStatementId is new for SQream protocol version 5
            cmd_str = '{"getStatementId" : "getStatementId"}'
            res_id = self.exchange(cmd_str)
            
        query_str = query_str.replace('\n', ' ').replace('\r', '')
        cmd_str = """{{"prepareStatement":"{0}","chunkSize":{1}}}""".format(query_str.replace('"', '\\"'),
                                                                            str(DEFAULT_NETWORK_CHUNKSIZE))
        
        # res1 is different between protocol 4 and 5 - ver. 4 returns Id, 5 doesn't
        res1 = self.exchange(cmd_str)

        cmd_str = '{"queryTypeOut" : "queryTypeOut"}'
        query_type_out = self.exchange(cmd_str)
        query_type_out = json.loads(query_type_out.decode("utf-8"))
        if not query_type_out["queryTypeNamed"]:
            cmd_str = '{"execute" : "execute"}'
            self.exchange(cmd_str)
            self.exchange('{"closeStatement":"closeStatement"}')
            return tuple(query_data), err

            pass
        else:
            for idx, col_type in enumerate(query_type_out['queryTypeNamed']):
                sq_col = SqreamColumn()
                sq_col.set_type_name(query_type_out['queryTypeNamed'][idx]['type'][0])
                sq_col.set_type_size(query_type_out['queryTypeNamed'][idx]['type'][1])
                sq_col.set_column_name(query_type_out['queryTypeNamed'][idx]['name'])
                sq_col.set_isTrueVarChar(query_type_out['queryTypeNamed'][idx]['isTrueVarChar'])
                sq_col.set_nullable(query_type_out['queryTypeNamed'][idx]['nullable'])
                query_data.append(sq_col)
            cmd_str = '{"execute" : "execute"}'
            self.exchange(cmd_str)
            # Keep reading while not connection closed
            while True:
                cmd_str = '{"fetch" : "fetch"}'
                fetch = self.exchange(cmd_str)
                fetch = json.loads(fetch.decode("utf-8"))
                rows_num = fetch["rows"]
                if rows_num == 0:
                    # No content to read
                    res2 = self.exchange('{"closeStatement":"closeStatement"}')
                    return tuple(query_data), err
                # Read to ignore header, which is irrelevant here
                data = self.socket_recv(self.HEADER_LEN)
                col_size = list()
                idx_first = 0
                idx_last = 1
                # Metadata store + how many columns to read ([val], [len,blob], [null,val], [null,len,blob])
                for col_data in query_data:
                    if col_data.get_isTrueVarChar():
                        idx_last += 1
                    if col_data.get_nullable():
                        idx_last += 1
                    col_data.set_column_size(fetch["colSzs"][idx_first:idx_last])
                    idx_first = idx_last
                    idx_last += 1

                    if col_data.get_isTrueVarChar() == False and col_data.get_nullable() == False:
                        column_data = self.readcolumnbytes(col_data.get_column_size()[0])  # , col_data.get_type_size())
                        column_data = [column_data[i:i + col_data.get_type_size()] for i in
                                       range(0, col_data.get_column_size()[0], col_data.get_type_size())]
                        column_data = list(map(lambda c: self.bytes2val(col_data.get_type_name(), c), column_data))

                    elif col_data.get_isTrueVarChar() == False and col_data.get_nullable() == True:
                        column_data = self.readcolumnbytes(col_data.get_column_size()[0])
                        is_null = self.get_nulls(column_data)
                        column_data = self.readcolumnbytes(col_data.get_column_size()[1])  # ,col_data.get_type_size(), None, is_null)
                        column_data = [column_data[i:i + col_data.get_type_size()] for i in
                                       range(0, col_data.get_column_size()[1], col_data.get_type_size())]
                        column_data = [self.bytes2val(col_data.get_type_name(),column_data[idx]) if elem == 0 else u"\\N" for idx, elem in enumerate(is_null)]

                    elif col_data.get_isTrueVarChar() == True and col_data.get_nullable() == False:
                        column_data = self.readcolumnbytes(col_data.get_column_size()[0])
                        column_data = [column_data[i:i + 4] for i in range(0, col_data.get_column_size()[0], 4)]
                        nvarchar_lens = map(lambda c: unpack('i', c)[0], column_data)
                        nvarchar_inds = self.len2ind(nvarchar_lens)
                        column_data = self.readcolumnbytes(col_data.get_column_size()[1])  # , None, nvarchar_inds[:-1])
                        column_data = [column_data[i:j] for i, j in
                                       zip([0] + nvarchar_inds[:-1], nvarchar_inds[:-1] + [None])]

                    elif col_data.get_isTrueVarChar() == True and col_data.get_nullable() == True:
                        column_data = self.readcolumnbytes(col_data.get_column_size()[0])
                        is_null = self.get_nulls(column_data)
                        column_data = self.readcolumnbytes(col_data.get_column_size()[1])
                        column_data = [column_data[i:i + 4] for i in range(0, col_data.get_column_size()[1], 4)]
                        nvarchar_lens = map(lambda c: unpack('i', c)[0], column_data)
                        nvarchar_inds = self.len2ind(nvarchar_lens)
                        column_data = self.readcolumnbytes(col_data.get_column_size()[2])
                        column_data = [column_data[i:j] if k == 0 else u"\\N" for i, j, k in
                                       zip([0] + nvarchar_inds[:-1], nvarchar_inds[:-1] + [None], is_null)]
                    else:
                        raise RuntimeError("Column data encountered malformed column during fetch")

                    col_data.append_column_data(column_data)


# This class should be used to create a connection
class Connector(object):
    def __init__(self):
        # Store the connection
        self._sc = None
        # Store the columns from the result
        self._cols = None
        self._query = None

    def connect(self, host='127.0.0.1', port=5000, database='master', user='sqream', password='sqream', clustered=False,
                timeout=15):
        # No connection yet, create a new one
        if self._sc is None:
            try:
                nsc = SqreamConn(clustered=clustered, timeout=timeout)
                nsc.create_connection(host, port)
                nsc.connect(database, user, password)
                self._sc = nsc
            except RuntimeError as e:
                raise RuntimeError(e)
            except:
                print("Unexpected error:", sys.exc_info()[0])
                raise
            return self._sc
        else:
            raise RuntimeError(
                'Connection already exists. You must close the current connection before creating a new one')

    def last_query(self):
        return self._query

    def last_cols(self):
        return self._cols

    def close(self):
        # Close existing connection, if it exists
        if self._sc is None:
            return
        else:
            self._sc.close_socket()
            self._sc = None

    def query(self, query=None):
        if query is None:
            raise RuntimeError("Query is empty")
        else:
            self._query = query
            try:
                rv = self._sc.execute(query)
                if rv is None:
                    return
                else:
                    # Unpack
                    columns, err = rv
                    if err:
                        raise RuntimeError(err)
                    else:
                        self._cols = columns
                        return self._cols
            except BaseException as e:
                raise RuntimeError("Unexpected error while running statement: " + str(e))

    def cols_data(self, cols=None):
        if cols == None:
            cols = self._cols
        if cols == None:
            raise RuntimeError("Last query did not return a result")
        return list(map(lambda c: c.get_column_data(), cols))

    def cols_names(self, cols=None):
        if cols == None:
            cols = self._cols
        if cols == None:
            raise RuntimeError("Last query did not return a result")
        return list(map(lambda c: c.get_column_name(), cols))

    def cols_types(self, cols=None):
        if cols == None:
            cols = self._cols
        if cols == None:
            raise RuntimeError("Last query did not return a result")
        return list(map(lambda c: c.get_type_name(), cols))

    def cols_nullable(self, cols=None):
        if cols == None:
            cols = self._cols
        if cols == None:
            raise RuntimeError("Last query did not return a result")
        return list(map(lambda c: c.get_nullable(), cols))

    def cols_to_rows(self, cols=None):
        # Transpose the columns into rows
        if cols == None:
            cols = self._cols
        if cols == None:
            raise RuntimeError("Last query did not return a result")
        cursor = self.cols_data(cols)
        return list(map(tuple, zip(*cursor)))


    def cols_to_rows2(self, cols=None):
        # Transpose the columns into rows
        if cols == None:
            cols = self._cols
        if cols == None:
            raise RuntimeError("Last query did not return a result")
        cursor = self.cols_data(cols)
        # cursor = list(map(lambda c: c.get_column_data(), cols))
        return zip(*(col.get_column_data() for col in cols))

    
# For backwards compatibility, remove eventually    
connector = Connector   
