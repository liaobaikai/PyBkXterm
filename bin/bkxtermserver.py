# -*- coding: utf-8 -*-
import sys
import json
import os
import platform
import signal
import socket
import struct
import time
import selectors
import traceback
import logging
from base64 import b64encode
from hashlib import sha1
from queue import Queue
from socketserver import BaseRequestHandler
from socketserver import TCPServer
from threading import Thread

import paramiko

if hasattr(os, "fork"):
    from socketserver import ForkingTCPServer

    _TCPServer = ForkingTCPServer
else:
    from socketserver import ThreadingTCPServer

    _TCPServer = ThreadingTCPServer

__author__ = 'lbk <baikai.liao@qq.com>'
__version__ = '0.2'
__status__ = "production"
__date__ = "2019-11-18"

server_base_path = os.path.dirname(os.path.dirname(os.path.abspath(sys.argv[0])))
shutdown_bind_address = "localhost"
shutdown_command = b"\x1b[^shutdown\x1b\\"
check_health_command = b'\x1b[^Hello\x1b\\'
reply_health_command = b'\x1b[^Hi\x1b\\'
HANDSHAKE_RESPONSE_HEADER = \
    'HTTP/1.1 101 Switching Protocols\r\n' \
    'Connection: Upgrade\r\n' \
    'Upgrade: websocket\r\n' \
    'Sec-WebSocket-Accept: {0}\r\n' \
    'WebSocket-Protocal: chat\r\n' \
    '\r\n'

'''
+-+-+-+-+-------+-+-------------+-------------------------------+
 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+-+-+-+-+-------+-+-------------+-------------------------------+
|F|R|R|R| opcode|M| Payload len |    Extended payload length    |
|I|S|S|S|  (4)  |A|     (7)     |             (16/64)           |
|N|V|V|V|       |S|             |   (if payload len==126/127)   |
| |1|2|3|       |K|             |                               |
+-+-+-+-+-------+-+-------------+ - - - - - - - - - - - - - - - +
|     Extended payload length continued, if payload len == 127  |
+ - - - - - - - - - - - - - - - +-------------------------------+
|                     Payload Data continued ...                |
+---------------------------------------------------------------+
'''
FIN = 0x80
OPCODE = 0x0f
MASKED = 0x80
PAYLOAD_LEN = 0x7f
PAYLOAD_LEN_EXT16 = 0x7e
PAYLOAD_LEN_EXT64 = 0x7f

OPCODE_TEXT = 0x01
CLOSE_CONN = 0x8


# 获取根目录
def get_server_base():
    return server_base_path


# 获取字典的值
def get_value(maps, name, default=None):
    if name not in maps:
        return default
    else:
        return maps[name]


# 获取字典的值
def get_int_value(maps, name, default=0):
    return int(get_value(maps, name, default))


# 获取当前时间
def get_now():
    return time.asctime(time.localtime(time.time()))


# 读取Properties配置文件
class Properties:

    def __init__(self, file=None, separator="=", ignore_case=False):
        self.properties = {}
        self.lines = None
        self.separator = separator
        self.ignore_case = ignore_case

        if file is not None:
            with open(file) as f:
                self.lines = f.readlines()

    def load(self, lines=None):
        if lines is not None:
            self.lines = lines.split('\n')
        chunk = ""
        for line in self.lines:
            if len(line) == 0 or line[0] == '#':
                continue
            if line[-1] == "\r" or line[-1] == "\n":
                line = line[0:-1]
                if len(line) == 0:
                    continue
            if line[-1] == '\\':
                chunk += line[0:-1].strip()
                continue
            if len(chunk) > 0:
                # 处理多行的情况
                line = chunk + line.strip()
                chunk = ""

            ss = line.strip().split(self.separator, 1)
            v1 = '' if len(ss) == 1 else ss[1]
            k1 = ss[0].lower() if self.ignore_case is True else ss[0]
            self.properties[k1.strip()] = v1.strip()
        return self.properties


params = {}
for param in sys.argv[1:]:
    try:
        k2, v2 = param.split("=", 2)
        params[k2] = v2
    except ValueError:
        params[param] = ""

# 读取配置文件
config_key = "-D.config.file"
has_config_file = True
if get_value(params, "-D.config.file") is None:
    params[config_key] = os.path.join(get_server_base(), 'x', 'bkserver.properties')
    print('INFO: Loading default config file...', params[config_key])

if os.path.exists(params[config_key]) is False:
    print('WARN: Config file is missing...')
    has_config_file = False

if has_config_file is True:
    properties = Properties(params[config_key])
    configs = properties.load()
else:
    # 配置文件不存在！
    configs = {}

# 处理参数
port = get_int_value(configs, 'port', 8899)
bind_address = get_value(configs, 'bind_address', "0.0.0.0")
shutdown_port = get_int_value(configs, 'shutdown_port', 8898)
socket_buffer_size = get_int_value(configs, 'socket_buffer_size', 8192)
channel_buffer_size = get_int_value(configs, 'channel_buffer_size', 131072)
queue_blocking = True if get_int_value(configs, 'queue_blocking', 0) == 1 else False


# 获取临时目录
def get_tmpdir():
    if "tmpdir" in configs:
        tmpdir = configs["tmpdir"]
    else:
        tmpdir = "${server_base}/temp"

    tmpdir = tmpdir.replace("${server_base}", get_server_base())

    if os.path.exists(tmpdir) is True:
        if os.path.isdir(tmpdir) is False:
            raise TypeError('(%s) is not a directory. Please delete it first!')
    else:
        # 目录不存在
        os.makedirs(tmpdir)
    return tmpdir


# 获取日志文件
def get_log_file(param_name, default_file_name):
    if param_name in configs:
        log_file = configs[param_name]
    else:
        log_file = "${server_base}/logs/%s" % default_file_name

    log_file = log_file.replace("${server_base}", get_server_base())

    logs = os.path.dirname(log_file)

    if os.path.exists(logs) is False:
        # 目录不存在
        os.makedirs(logs)
        open(log_file, 'w').close()
    else:
        if os.path.isfile(logs) is True:
            raise TypeError('(%s) is not a file. Please delete it first!' % logs)
        else:
            if os.path.exists(log_file) is False:
                open(log_file, 'w').close()

    return log_file


# pid文件
def get_pid_file():
    if "pid" in configs:
        pid = configs["pid"]
    else:
        pid = "${server_base}/temp/bk-server.pid"
    return pid.replace("${server_base}", get_server_base())


# 获取错误日志文件
def get_error_log():
    return get_log_file('error_log', 'error.log')


# 获取访问日志文件
def get_access_log():
    return get_log_file('access_log', 'access.log')


pid_file = get_pid_file()
access_log = get_access_log()
error_log = get_error_log()

log_date_fmt = '%Y-%m-%d %H:%M:%S'
log_file_mode = 'w+'
log_level = 'NOTSET'
log_format = '%(asctime)s - %(module)s.%(funcName)s[line:%(lineno)d] - %(levelname)s: %(message)s'

log_levels = {
    'NOTSET': logging.NOTSET,
    'DEBUG': logging.DEBUG,
    'INFO': logging.INFO,
    'WARN': logging.WARN,
    'WARNING': logging.WARNING,
    'ERROR': logging.ERROR,
    'FATAL': logging.FATAL,
    'CRITICAL': logging.CRITICAL
}

logging.basicConfig(level=log_levels[log_level], format=log_format)
error_logger = logging.getLogger(__name__)
access_logger = logging.getLogger(__name__)
info_logger = logging.getLogger(__name__)


# 获取数据解密私钥
# with open('rsa_private_key.pem', 'r') as fp:
#     rsa_private_key = fp.read()


# 打印基本信息
def print_base_info():
    print('Using python base:       ', sys.prefix)
    print('Using python executable: ', sys.executable)
    print('Using server base:       ', get_server_base())
    print('Using server tmpdir:     ', get_tmpdir())


# 将bytes数据转字符串
def decode_utf8(data, flag='ignore', replace_str=''):
    try:
        return data.decode(encoding='utf-8')
    except UnicodeDecodeError:
        result = ''
        last_except = False
        for r in iter(data):
            try:
                result += (bytes([r]).decode(encoding='utf-8'))
                last_except = False
            except UnicodeDecodeError as ude:
                access_logger.warning(ude)
                if last_except is False:
                    if flag == 'ignore':
                        result += ''
                    elif flag == 'replace':
                        result += replace_str
                    last_except = True

        return result


# 创建守护进程
def daemon(pid_file):
    # 创建子进程，退出父进程
    pid = os.fork()
    if pid:
        sys.exit(0)
    os.chdir('/')
    os.umask(0)
    os.setsid()
    # 创建子进程的子进程（孙进程），退出子进程
    pid2 = os.fork()
    if pid2:
        sys.exit(0)
    # 此时，孙进程已经是守护进程

    if pid_file:
        with open(pid_file, 'w+') as f:
            f.write(str(os.getpid()))


# 启动服务器
def startup():
    # 打印相关信息
    print_base_info()
    # 初始化服务器
    server = BkXtermServer((bind_address, port), WebSocketRequestHandler)
    # 查看端口是否被占用
    server.server_activate()
    # 不是Windows系统的话，切换到守护进程
    # if hasattr(os, "fork"):
    #    daemon(get_pid_file())
    # 启动服务器
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('shutdown server...')

        def shutdown_server():
            server.sd_server.shutdown()
            server.shutdown()

        Thread(target=shutdown_server).start()

    try:
        os.remove(get_pid_file())
    except FileNotFoundError:
        pass


# 关闭服务器
def kill():
    if os.path.exists(get_pid_file()) is False:
        print("PID file not exists!")
        return
    with open(get_pid_file()) as f:
        pid = f.read()
    try:
        os.kill(int(pid), signal.SIGTERM)
    except ProcessLookupError as ple:
        print(ple)
    finally:
        try:
            os.remove(get_pid_file())
        except OSError:
            pass


# 创建一个客户端，与服务器进行通讯，发送关闭服务器的指令，让服务器执行shutdown()。
def shutdown():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.connect((shutdown_bind_address, shutdown_port))
            sock.sendall(shutdown_command)
        except ConnectionRefusedError:
            print_base_info()
            print('ERROR: Could not contact [%s:%s]. Server may not be running.'
                  % (shutdown_bind_address, shutdown_port))
            print(traceback.format_exc())


# 检查主服务器的健康状态
def check_server():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.connect((shutdown_bind_address, shutdown_port))
            sock.sendall(check_health_command)
            if sock.recv(64) == reply_health_command:
                print('Server running...')
        except ConnectionRefusedError:
            print('Server not running!')


# 打包消息
# 参考：https://www.cnblogs.com/ssyfj/p/9245150.html
def pack_message(message):
    # 参考 websocket-server模块(pip3 install websocket-server)
    """
    :param message: str
    :return: bytes
    """
    if isinstance(message, bytes):
        try:
            message = message.decode('utf-8')
        except UnicodeDecodeError:
            print('Can\'t send message, message is not valid UTF-8!')
            return
    elif isinstance(message, str):
        pass
    else:
        message = str(message)

    header = bytearray()
    header.append(FIN | OPCODE_TEXT)

    payload = message.encode(encoding='utf-8')
    payload_length = len(payload)
    if payload_length <= 125:
        header.append(payload_length)
    elif 126 <= payload_length <= 65535:
        header.append(PAYLOAD_LEN_EXT16)
        header.extend(struct.pack('>H', payload_length))
    elif payload_length < 18446744073709551616:
        header.append(PAYLOAD_LEN_EXT64)
        header.extend(struct.pack('>Q', payload_length))
    else:
        raise Exception("Message is too big. Consider breaking it into chunks.")

    return header + payload


# web socket 连接处理器
class WebSocketRequestHandler(BaseRequestHandler):

    def __init__(self, request, client_address, server):
        self.keep_alive = True
        self.ssh_client = None
        self.sftp_client = None
        self.handshake_done = False  # 是否握手成功
        self.selector = selectors.DefaultSelector()
        # 默认注册请求读取事件
        self.selector.register(request, selectors.EVENT_READ)
        self.reg_selector = None
        self.heartbeat_time = None
        self.read_pos = 0  # 读取到那个位置
        self.queue = Queue()  # 消息队列，主要用于ssh服务器返回的数据缓冲。
        super().__init__(request, client_address, server)

    def reg_read(self):
        self.selector.modify(self.request, selectors.EVENT_READ)

    def reg_send(self):
        self.selector.modify(self.request, selectors.EVENT_WRITE)

    # 和客户端握手
    # 参考：https://www.cnblogs.com/ssyfj/p/9245150.html
    def handshake(self, data):
        """
        :param data: bytes
        :return:
        """
        print(data)
        # 从请求的数据中获取 Sec-WebSocket-Key, Upgrade

        try:
            request_header = str(data, encoding='ascii')
        except UnicodeDecodeError:
            access_logger.error('WS] handshake fail of decode error!')
            self.keep_alive = False
            return

        maps = Properties(separator=':', ignore_case=True).load(request_header)
        if get_value(maps, 'upgrade') is None:
            access_logger.error('WS] Client tried to connect but was missing upgrade!')
            self.keep_alive = False
            return
        sw_key = get_value(maps, 'sec-websocket-key')
        if sw_key is None:
            access_logger.error('WS] Client tried to connect but was missing a key!')
            self.keep_alive = False
            return
        hash_value = sha1(sw_key.encode() + b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11')
        resp_key = b64encode(hash_value.digest()).strip().decode('ascii')
        resp_body = HANDSHAKE_RESPONSE_HEADER.format(resp_key).encode()
        self.handshake_done = self.request.send(resp_body)
        if self.handshake_done > 0:
            access_logger.info('WS] handshake success...')
            self.server.new_client(self)

    # 读取客户端传过来的数据
    def read_message(self):
        try:
            message = self.request.recv(socket_buffer_size)

            self.read_pos = 0

            if self.handshake_done is False:
                self.handshake(message)
            else:
                # 解析文本
                # 在读下一个字符，看看有没有客户端两次传入，一次解析的。
                while self.read_bytes(message, 1):
                    self.read_pos -= 1
                    decoded = self.unpack_message(message)
                    self.handle_decoded(decoded)
                # print('read next....')

        except (ConnectionAbortedError, ConnectionResetError, TimeoutError) as es:
            # [Errno 54] Connection reset by peer
            print('read_message: ', es)
            self.shutdown_request()

    # 消息解包
    # struct.pack struct.unpack
    # https://blog.csdn.net/qq_30638831/article/details/80421019
    # 参考：https://www.cnblogs.com/ssyfj/p/9245150.html
    def unpack_message(self, message):
        """
        :param message: bytes
        :return: str
        """
        b1, b2 = self.read_bytes(message, 2)
        # fin = b1 & FIN
        opcode = b1 & OPCODE
        masked = b2 & MASKED
        payload_length = b2 & PAYLOAD_LEN

        if not b1:
            access_logger.error('WS] Client closed connection.')
            self.keep_alive = False
            return
        if opcode == CLOSE_CONN:
            access_logger.error('WS] Client closed connection.')
            self.keep_alive = False
            return
        if not masked:
            access_logger.error('WS] Client must always be masked.')
            self.keep_alive = False
            return

        if payload_length == 126:
            # (132,)
            payload_length = struct.unpack('>H', self.read_bytes(message, 2))[0]
        elif payload_length == 127:
            # (132,)
            payload_length = struct.unpack('>Q', self.read_bytes(message, 8))[0]

        masks = self.read_bytes(message, 4)

        decoded = bytearray()
        for c in self.read_bytes(message, payload_length):
            c ^= masks[len(decoded) % 4]
            decoded.append(c)
        return str(decoded, encoding='utf-8')

    # 向客户端写发送数据
    # 从消息队列中获取数据(self.queue)
    def send_message(self):
        message = b''
        while not self.queue.empty():
            message += self.queue.get_nowait()
        # 从selection中获取数据
        # selection_key = self.selector.get_key(self.request)
        # message = selection_key.data

        presentation = decode_utf8(message, flag='replace', replace_str='?')
        payload = pack_message(presentation)
        try:
            self.request.send(payload)
        except BrokenPipeError as bpe:
            # 发送失败，可能客户端已经异常断开。
            print('BrokenPipeError: ', bpe)
            # 取消ssh-chan注册
            try:
                self.selector.unregister(self.ssh_client.chan)
            except KeyError:
                # 当read_channel_message取消注册后，再次取消注册会抛出错误
                pass
            finally:
                self.shutdown_request()

        self.heartbeat_time = time.time()
        # 将事件类型改成selectors.EVENT_READ
        # 切换到读取的功能
        self.reg_read()

    # 响应连接断开信息
    def resp_closed_message(self, flag):
        try:
            chan = self.ssh_client.chan  # type: paramiko.Channel
            self.selector.unregister(chan)
        except KeyError:
            pass
        self.queue.put((flag + '连接已断开。按回车键重新连接...\r\n').encode('utf-8'), block=queue_blocking)
        self.reg_send()

    # 从ssh通道中获取数据
    def packet_read_wait(self):
        chan = self.ssh_client.chan  # type: paramiko.Channel

        data = None
        if chan.recv_stderr_ready():
            print('recv_stderr_ready: ', chan.recv_stderr(4096))

        elif chan.recv_ready():
            try:
                data = chan.recv(channel_buffer_size)
            except socket.timeout as st:
                print('read_channel_message', st)
        elif chan.recv_exit_status():
            print('recv_exit_status: ', chan.recv_exit_status())
            # 取消注册选择器，防止循环输出。
            self.resp_closed_message('\x1b^exit\x07')
            chan.close()
        elif chan.closed is True:
            # 取消注册选择器，防止循环输出。
            self.resp_closed_message('\x1b^close\x07')

        if data is not None and len(data) > 0:
            access_logger.debug(data)

            self.queue.put(data, block=queue_blocking)
            # 将事件类型改成selectors.EVENT_WRITE
            # 切换到写入的功能
            # self.selector.modify(self.request, selectors.EVENT_WRITE, data=data)
            self.reg_send()

    # 向通道发送数据
    def packet_write_wait(self, cmd):
        """
        :param cmd: str
        """
        chan = self.ssh_client.chan  # type: paramiko.Channel
        if chan.closed is True:
            # 取消注册ssh通道读取事件
            try:
                self.selector.unregister(chan)
            except KeyError:
                # 当read_channel_message取消注册后，再次取消注册会抛出错误
                pass

            self.queue.put('正在重新连接...\r\n\r\n'.encode('utf-8'), block=queue_blocking)
            self.reg_send()

            # 重新创建终端。
            self.ssh_client.new_terminal_shell()
            # 重新注册ssh通道读取事件
            self.selector.register(self.ssh_client.chan, selectors.EVENT_READ)
            return

        print('chan.send_ready(): ', chan.send_ready())
        if chan.send_ready():
            access_logger.debug(cmd)
            chan.send(cmd)
            self.heartbeat_time = time.time()
        else:

            ssh = self.ssh_client  # type: SSHClient
            self.queue.put('packet_write_wait: Connection to {} port {}: Broken pipe'.format(
                get_value(ssh.args, 'hostname'), get_value(ssh.args, 'port')))

    # shutdown请求
    def shutdown_request(self):
        access_logger.info('SD_SERVER] shutdown request...')
        self.keep_alive = False
        try:
            ssh = self.ssh_client.ssh  # type: paramiko.SSHClient
            chan = self.ssh_client.chan  # type: paramiko.Channel
            chan.shutdown(socket.SHUT_RDWR)
            chan.close()
            ssh.close()
        except AttributeError:
            pass

        self.selector.close()
        server = self.server  # type: BkXtermServer
        server.shutdown_client(self)

    # 处理请求
    def handle(self):

        while self.keep_alive:

            selection_keys = self.selector.select()

            for key, events in selection_keys:

                if key.fileobj == self.request:
                    if events == selectors.EVENT_READ:
                        # 从客户端中读取
                        self.read_message()
                    elif events == selectors.EVENT_WRITE:
                        # 发送数据给客户端
                        self.send_message()
                elif key.fileobj == self.ssh_client.chan:
                    if events == selectors.EVENT_READ:
                        # 从ssh通道读取数据
                        self.packet_read_wait()

    # 读取字节
    def read_bytes(self, data, num):
        """
        :param data: bytes
        :param num: int
        :return: bytes
        """
        bs = data[self.read_pos:num + self.read_pos]
        self.read_pos += num
        return bs

    # 处理解码后的文本
    def handle_decoded(self, decoded):
        """
        :param decoded: str
        """
        if decoded is None:
            return
        # print('handle_decoded: ', decoded)
        try:
            print('decoded', decoded)

            # 通过私钥解密数据
            # try:
            #     source = rsa.decrypt(decoded.encode(), decode_pri_key).decode()
            # except rsa.DecryptionError as de:
            #     print(de)
            #     return

            user_data = json.loads(decoded)

            if self.ssh_client is None:
                self.connect_terminal(user_data)
                return
            if 'size' in user_data:
                access_logger.debug('size:', user_data)

                size = get_value(user_data, 'size')
                width = get_int_value(size, 'w', 80)
                height = get_int_value(size, 'h', 24)
                access_logger.debug('size: w{},h{}'.format(width, height))

                self.ssh_client.chan.resize_pty(width=width, height=height)
            if 'cmd' in user_data:
                cmd = get_value(user_data, 'cmd')
                if cmd:
                    self.packet_write_wait(cmd)

            if 'sftp' in user_data:
                """
                    {
                        chdir: '',
                        chmod: '',
                        chown: '',
                        file: '',
                        get: '',
                        listdir:
                    }
                """
                pass

        except ValueError:
            # 非JSON数据
            # 判断是否为心跳数据
            pass

    # 连接到终端
    def connect_terminal(self, message):

        """
        :param message: dict
        """
        target = get_value(message, 'target')
        access_logger.debug(self.request.getpeername())
        access_logger.debug(message)

        if target is None:
            access_logger.error('无效的主机信息！！！')
        else:
            # 连接终端
            size = get_value(message, 'size')
            if size is None:
                size = {}

            self.ssh_client = SSHClient({
                'hostname': get_value(target, 'hostname'),
                'port': get_int_value(target, 'port'),
                'username': get_value(target, 'username'),
                'password': get_value(target, 'password'),
                'width': get_int_value(size, 'w', 80),
                'height': get_int_value(size, 'h', 24),
                'term': get_value(message, 'term', 'vt100')
            })

            # 连接失败？
            msg = self.ssh_client.connect()  # type: dict
            if msg is None:
                # 发送版本及加密方式等的信息
                self.queue.put(json.dumps(self.ssh_client.get_ssh_info()).encode(), block=queue_blocking)
                self.reg_send()
            else:
                # 连接错误信息
                self.queue.put(json.dumps(msg).encode(), block=queue_blocking)
                self.reg_send()
                return

            self.ssh_client.new_terminal_shell()
            # 开始心跳的时间
            self.heartbeat_time = time.time()
            # 注册ssh通道读取事件
            self.selector.register(self.ssh_client.chan, selectors.EVENT_READ)

    # 请求结束
    # 释放资源
    def finish(self):
        self.shutdown_request()


# shutdown请求处理器
class SDRequestHandler(BaseRequestHandler):

    # 做出关闭的动作
    def shutdown(self):
        self.server.shutdown()
        # 发出shutdown的命令
        http = self.server.http  # type: BkXtermServer
        http.shutdown()

    def handle(self):
        data = self.request.recv(64)
        if data == shutdown_command:
            # 执行shutdown需要另外开一个进程，不然会造成死锁。
            # 参考：https://blog.csdn.net/candcplusplus/article/details/51794411
            #      结束serve_forever()
            Thread(target=self.shutdown).start()
        elif data == check_health_command:
            # 健康检查，响应客户端
            self.request.send(reply_health_command)


# shutdown服务器
class SDServer(TCPServer):
    def __init__(self, server, server_address, handler):
        self.http = server
        super().__init__(server_address, handler)


# web socket 服务器
# Unix... Mix-in class to handle each request in a new process.
# Windows... Mix-in class to handle each request in a new thread.
class BkXtermServer(_TCPServer):
    allow_reuse_address = True

    def __init__(self, server_address, handler):
        # 连接的客户端
        self.clients = []
        # 连接数量
        self.id_counter = 0
        self.sd_server = SDServer(self, (shutdown_bind_address, shutdown_port), SDRequestHandler)
        super().__init__(server_address, handler)

    def new_client(self, handler):
        self.id_counter += 1
        client = {
            'id': self.id_counter,
            'handler': handler,
            'address': handler.client_address
        }
        self.clients.append(client)

    def shutdown_client(self, shutdown_handler):
        self.id_counter -= 1
        for client in self.clients:
            handler = client['handler']  # type: WebSocketRequestHandler
            if handler == shutdown_handler:
                self.clients.remove(client)
                break
        print(self.id_counter)
        print(self.clients)

    def server_activate(self):
        super().server_activate()
        self.sd_server.server_activate()

    def run_sd_server(self):
        self.sd_server.serve_forever(0.5)

    def shutdown(self):
        for client in self.clients:
            handler = client['handler']  # type: WebSocketRequestHandler
            handler.shutdown_request()
        super().shutdown()

    def serve_forever(self, poll_interval=0.5):
        Thread(target=self.run_sd_server).start()
        print('Server started.')
        super().serve_forever(poll_interval)


# 检查SSH的版本号
def check_banner(version):
    seg = version.split("-", 2)
    if len(seg) < 3:
        raise ValueError("Invalid SSH banner")
    version = seg[1]
    client = seg[2]
    if version != "1.99" and version != "2.0":
        msg = "Incompatible version ({} instead of 2.0)"
        raise ValueError(msg.format(version))
    return version, client


class SSHClient:

    # heartbeat 心跳时间(s)
    def __init__(self, args=None, heartbeat=30):
        paramiko.util.log_to_file('paramiko.log')
        self.ssh = paramiko.SSHClient()
        self.ssh.load_system_host_keys()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy)
        self.heartbeat = heartbeat
        self.chan = None
        self.args = args

    # 连接终端
    def connect(self):

        error_msg = None
        try:
            self.ssh.connect(
                hostname=get_value(self.args, 'hostname'),
                port=get_value(self.args, 'port'),
                username=get_value(self.args, 'username'),
                password=get_value(self.args, 'password')
            )

            transport = self.ssh.get_transport()  # type: paramiko.Transport

            # rsa_key = paramiko.RSAKey.generate(2048)

            # print(rsa_key.key)

            # transport.add_server_key(rsa_key)
            # print(transport.get_server_key())

            # stdin, stdout, stderr = self.ssh.exec_command('export LANG=zh_CN.UTF-8')
            # stdin, stdout, stderr = self.ssh.exec_command('date', environment={'LANG': 'zh_CN.UTF-8'})
            #
            # print(stdout.read())

        except paramiko.BadHostKeyException:
            error_msg = {'status': 'fail',
                         'e': 'BadHostKey',
                         'zh_msg': '无法验证服务器的主机密钥',
                         'en_msg': 'the server\'s host key could not be verified'}
        except paramiko.AuthenticationException:
            error_msg = {'status': 'fail',
                         'e': 'Authentication',
                         'zh_msg': '身份验证失败',
                         'en_msg': 'authentication failed'}
        except paramiko.SSHException:
            error_msg = {'status': 'fail',
                         'e': 'SSH',
                         'zh_msg': '连接或建立会话时出现其他错误',
                         'en_msg': 'there was any other error connecting or establishing an SSH session'}
        except socket.error:
            error_msg = {'e': 'socket.error',
                         'zh_msg': '连接时发生套接字错误',
                         'en_msg': 'socket error occurred while connecting'}

        return error_msg

    # 获取ssh通道
    def new_terminal_shell(self):

        if self.ssh.get_transport().is_active() is False:
            # session不活跃
            # 需要重新连接
            self.connect()

        self.chan = self.ssh.invoke_shell(get_value(self.args, 'term'),
                                          get_value(self.args, 'width'),
                                          get_value(self.args, 'height'))  # type: paramiko.Channel
        # 如果设置了env
        # self.chan.send('export LANG=zh_CN.UTF-8\r')

        self.chan.setblocking(True)
        self.chan.send_exit_status(0)

    # 创建终端
    def new_terminal(self):
        self.connect()
        self.new_terminal_shell()

    # 获取SSH版本号
    def get_ssh_info(self):
        # 获取版本号
        transport = self.ssh.get_transport()  # type: paramiko.Transport
        print(transport.get_banner())

        remote_version, remote_client = check_banner(transport.remote_version)

        return {
            'status': 'success',
            'version': remote_version,
            'cipher': transport.remote_cipher
        }


if __name__ == '__main__':

    if len(sys.argv) > 1:
        commands = sys.argv[-1]
        if commands == 'start':
            startup()
        elif commands == 'run':
            startup()
        elif commands == 'stop':
            shutdown()
        elif commands == 'stop-force':
            kill()
        elif commands == 'version':
            print_base_info()
            try:
                fs = platform.platform().split('-')
                os_name = fs[0]
                os_version = fs[1]
                architecture = fs[2]
            except ValueError as e:
                os_name = platform.platform(aliased=True, terse=True)
                os_version = platform.version()
                architecture = platform.architecture()[0]

            print('Server version: ', __version__)
            print('OS Name:        ', os_name)
            print('OS Version:     ', os_version)
            print('Architecture:   ', architecture)
            print('Python build:   ', platform.python_build()[0])
        elif commands == 'status':
            print_base_info()
            print('Using server pid file:   ', get_pid_file())
            if os.path.exists(get_pid_file()):
                with open(get_pid_file()) as pf:
                    print('Using PID:               ', pf.read())
            #
            print('Using server bind:       ', bind_address)
            print('Using server port:       ', port)
            print('Using server sd port:    ', shutdown_port)
            check_server()
        else:
            print('Usage: bkxterm.sh ( commands ... )')
            print('commands:')
            print('  run:        Start Server in the current window')
            print('  start:      Start Server in a separate window')
            print('  stop:       Stop Server, waiting up to 5 seconds for the process to end')
            print('  status:     View running Server status')
            print('  version:    What version of server are you running?')
    else:
        startup()
