import json
import multiprocessing

from .tasks import QueueConnection
from .config import Config
from .tools import TimeoutFunction
from .store import Store


class IPPool(Store):

    def __init__(self):
        # Use Redis db=4 for ip's
        super(IPPool, self).__init__(db=4)
        # Reserve the gateway ip: 172.17.42.1
        self[(42, 1)] = True


class IPPoolServer(object):

    def __init__(self):
        self.ips = IPPool()
        self.start()

    def act(self, message, responder):
        msg = json.loads(message)
        tag = msg['tag']
        ip = tuple(msg['ip'])
        if tag == 'remove':
            del self.ips[ip]
        if tag == 'reserve':
            if ip in self.ips:
                responder(json.dumps({'result': 'in use'}))
            else:
                self.ips[ip] = True
                responder(json.dumps({'result': 'ok'}))

    def _run(self):
        conn = QueueConnection(Config.get('queue', 'host'),
                               Config.getint('queue', 'port'),
                               'ip_pool')
        conn.consume_forever(self.act, exclusive=True)

    def start(self):
        self.proc = multiprocessing.Process(
            target=self._run, name='ip_pool_server')
        self.proc.start()


class IPPoolClient(object):

    def connect(self):
        return QueueConnection(Config.get('queue', 'host'),
                               Config.getint('queue', 'port'),
                               'ip_pool')

    def reserve(self, ip):
        conn = self.connect()
        conn.send(json.dumps({'tag': 'reserve',
                              'ip': ip}))
        g = conn.receive()
        f = TimeoutFunction(lambda: json.loads(next(g)), 1)
        try:
            r = f()
            return r['result'] == 'ok'
        finally:
            g.close()
            conn.connection.close()

    def remove(self, ip):
        conn = self.connect()
        conn.send(json.dumps({'tag': 'remove', 'ip': ip}))
