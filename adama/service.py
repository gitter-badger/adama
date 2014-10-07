import glob
import itertools
import json
import multiprocessing
import os
import re
import ssl
import socket
import threading
import urlparse

from enum import Enum
from flask import request, Response
from flask.ext import restful
import jinja2
import requests
import ijson
import yaml

from .api import APIException, RegisterException, ok
from .config import Config
from .docker import docker_output, start_container, tail_logs
from .firewall import Firewall
from .tools import (location_of, identifier, service_iden,
                    adapter_iden, interleave)
from .tasks import Producer
from .service_store import service_store


LANGUAGES = {
    'python': ('py', 'pip install {package}'),
    'ruby': ('rb', 'gem install {package}'),
    'javascript': ('js', 'npm install -g {package}'),
    'lua': ('lua', None),
    'java': ('jar', None)
}

EXTENSIONS = {
    '.py': 'python',
    '.js': 'javascript',
    '.rb': 'ruby',
    '.jar': 'java',
    '.lua': 'lua'
}

# Timeout to wait for output of containers at start up, before
# declaring them dead
TIMEOUT = 3  # second

# Timout to wait while stopping workers
STOP_TIMEOUT = 5

HERE = location_of(__file__)


class WorkerState(Enum):
    started = 1
    error = 2


class Service(object):

    METADATA_DEFAULT = ''
    PARAMS = [
        # parameter, mandatory?, default
        ('name', True),
        ('namespace', True),
        ('type', True),
        ('code_dir', True),
        ('version', False, '0.1'),
        ('url', False, 'http://localhost'),
        ('whitelist', False, []),
        ('description', False, ''),
        ('requirements', False, []),
        ('notify', False, ''),
        ('json_path', False, ''),
        ('main_module', False, 'main'),
        ('metadata', False, METADATA_DEFAULT)
    ]

    def __init__(self, **kwargs):
        """Initialize service.

        First get metadata from ``code_dir`` and then use ``kwargs``.

        """
        code_dir = kwargs['code_dir']
        self.metadata = kwargs.get('metadata', self.METADATA_DEFAULT)
        self.__dict__.update(get_metadata_from(
            os.path.join(code_dir, self.metadata)))
        self.__dict__.update(kwargs)
        self.validate_args()

        self.iden = identifier(self.namespace,
                               self.name,
                               self.version)
        self.adapter_name = adapter_iden(self.name, self.version)
        self.whitelist.append(urlparse.urlparse(self.url).hostname)
        self.whitelist = list(set(self.whitelist))
        self.validate_whitelist()

        self.main_module_path = self.find_main_module()
        self.language = self.detect_language(self.main_module_path)
        self.state = None
        self.workers = []
        self.firewall = None

    def validate_args(self):
        for param in self.PARAMS:
            try:
                getattr(self, param[0])
            except AttributeError:
                if param[1]:
                    raise
                setattr(self, param[0], param[2])
        if not valid_image_name(self.name):
            raise APIException("'{}' is not a valid service name.\n"
                               "Allowed characters: [a-z0-9_.-]"
                               .format(self.name))

    def to_json(self):
        obj = {key[0]: getattr(self, key[0]) for key in self.PARAMS}
        obj['language'] = self.language
        obj['workers'] = self.workers
        return obj

    def make_image(self):
        """Make a docker image for this service."""

        self.firewall = Firewall(self.whitelist)
        render_template(
            os.path.dirname(self.main_module),
            os.path.basename(self.main_module_path),
            self.language,
            self.requirements,
            into=self.code_dir)
        self.build()

    def find_main_module(self):
        """Find the path to the ``main_module``."""

        directory, basename = os.path.split(self.main_module)
        module, _ = os.path.splitext(basename)

        found = glob.glob(os.path.join(self.code_dir, directory, module+'.*'))
        if not found:
            raise APIException('module not found: {}'
                               .format(self.main_module), 400)

        return found[0]

    def detect_language(self, module):
        _, ext = os.path.splitext(module)
        try:
            return EXTENSIONS[ext]
        except KeyError:
            raise APIException('unknown extension {0}'.format(ext), 400)

    def validate_whitelist(self):
        """Make sure ip's and domain name can be resolved."""

        for addr in self.whitelist:
            try:
                socket.gethostbyname_ex(addr)
            except:
                raise APIException(
                    "'{}' does not look like an ip or domain name"
                    .format(addr), 400)

    def build(self):
        prev_cwd = os.getcwd()
        os.chdir(self.code_dir)
        try:
            output = docker_output('build', '-t', self.iden, '.')
            print(output)
        finally:
            os.chdir(prev_cwd)

    def start_workers(self, n=None):
        if self.language is None:
            raise APIException('language of adapter not detected yet', 500)
        if n is None:
            n = Config.getint(
                'workers', '{}_instances'.format(self.language))
        self.workers = [self.start_worker() for _ in range(n)]

    def start_worker(self):
        worker, iface, ip = start_container(
            self.iden,          # image name
            '--queue-host',
            Config.get('queue', 'host'),
            '--queue-port',
            Config.get('queue', 'port'),
            '--queue-name',
            self.iden,
            '--adapter-type',
            self.type)
        self.firewall.register(worker, iface)
        return worker

    def stop_workers(self):
        threads = []
        for worker in self.workers:
            threads.append(self.async_stop_worker(worker))
        for thread in threads:
            thread.join(STOP_TIMEOUT)

    def async_stop_worker(self, worker):
        self.firewall.unregister(worker)
        thread = threading.Thread(target=docker_output,
                                  args=('kill', worker))
        thread.start()
        return thread

    def check_health(self):
        """Check that all workers started ok."""

        q = multiprocessing.Queue()

        def log(worker, q):
            producer = tail_logs(worker, timeout=TIMEOUT)
            v = (check(producer), worker)
            q.put(v)

        # Ask for logs from workers. We do it in processes so we can
        # poll for a little while the workers in parallel.
        ts = []
        for worker in self.workers:
            t = multiprocessing.Process(target=log, args=(worker, q),
                                        name='Worker log {}'.format(worker))
            t.start()
            ts.append(t)
        # wait for all processes (the timeout guarantees they'll finish)
        for t in ts:
            t.join()

        logs = []
        while not q.empty():
            state, worker = q.get()
            if state == WorkerState.error:
                logs.append(docker_output('logs', worker))
                self.firewall.unregister(worker)

        if logs:
            raise RegisterException(len(self.workers), logs)

    def exec_worker(self, endpoint, args, request):
        """Process a request through the worker."""

        meth = getattr(self, 'exec_worker_{}'.format(self.type))
        return meth(endpoint, args, request)

    def exec_worker_query(self, endpoint, args, request):
        """Send ``args`` to ``queue`` in QueryWorker model."""

        queue = self.iden
        args['endpoint'] = endpoint
        client = Producer(queue_host=Config.get('queue', 'host'),
                          queue_port=Config.getint('queue', 'port'),
                          queue_name=queue)
        client.send(args)
        gen = itertools.imap(json.dumps, client.receive())
        return Response(result_generator(gen, lambda: client.metadata),
                        mimetype='application/json')

    def exec_worker_map_filter(self, endpoint, args, request):
        """Forward request and process response.

        Forward the request to the third party service, and map the
        response through the ``process`` user function.

        """
        if endpoint != 'search':
            raise APIException("service of type 'map_filter' does "
                               "not support /list")

        if is_https(self.url) and request.method == 'GET':
            method = tls1_get
        else:
            method = getattr(requests, request.method.lower())
        response = method(self.url,
                          params=request.args,
                          stream=True)
        if response.ok:
            path = '.'.join(filter(None, [self.json_path, 'item']))
            results = ijson.items(FileLikeWrapper(response), path)

            return Response(
                result_generator(process_by_client(self, results),
                                 lambda: {}),
                mimetype='application/json')
        else:
            raise APIException('response from external service: {}'
                               .format(response))


class ServiceQueryResource(restful.Resource):

    def get(self, namespace, service):
        args = self.validate_get()
        try:
            iden = service_iden(namespace, service)
            srv = service_store[iden]['service']
        except KeyError:
            raise APIException('service not found: {}'
                               .format(service_iden(namespace, service)),
                               404)

        return srv.exec_worker('search', args, request)

    def validate_get(self):
        # no defined query language yet
        # accept everything
        return {key: val[0] for key, val in dict(request.args).items()}


class ServiceListResource(restful.Resource):

    def get(self, namespace, service):
        args = self.validate_get()
        try:
            iden = service_iden(namespace, service)
            srv = service_store[iden]['service']
        except KeyError:
            raise APIException('service not found: {}'
                               .format(service_iden(namespace, service)),
                               404)

        return srv.exec_worker('list', args, request)

    def validate_get(self):
        return dict(request.args)


class ServiceResource(restful.Resource):

    def get(self, namespace, service):
        full_name = service_iden(namespace, service)
        try:
            srv = service_store[full_name]
            if srv['slot'] == 'ready':
                return ok({
                    'result': {
                        'service': srv['service'].to_json()
                    }
                })
            else:
                return ok({
                    'result': srv
                })
        except KeyError:
            raise APIException(
                "service not found: {}/{}"
                .format(namespace, service),
                404)

    def post(self, namespace, service):
        pass

    def delete(self, namespace, service):
        name = service_iden(namespace, service)
        try:
            srv = service_store[name]['service']
            try:
                srv.stop_workers()
                # TODO: need to clean up containers here too
            except Exception:
                # ignore any error while stopping and removing workers
                pass
            del service_store[name]
        except KeyError:
            pass
        return ok({})


class FileLikeWrapper(object):

    def __init__(self, response):
        self.response = response
        self.it = self.response.iter_content(chunk_size=1)
        self.src = itertools.chain.from_iterable(self.it)

    def read(self, n=512):
        return ''.join(itertools.islice(self.src, None, n))


def process_by_client(service, results):
    """Process results through a ProcessWorker.

    Return a generator which produces JSON objects (as strings).

    """

    client = Producer(
        queue_host=Config.get('queue', 'host'),
        queue_port=Config.getint('queue', 'port'),
        queue_name=service.iden)
    for result in results:
        client.send(result)
        for obj in client.receive():
            yield json.dumps(obj)
            if 'error' in obj:
                # abort as soon as there is an error
                return


def result_generator(results, metadata):
    """Construct JSON response from ``results``.

    ``results`` is a generator that produces JSON objects, and
    ``metadata`` is a function that returns extra information.

    The reason for metadata being a function is to be able to collect
    information after the ``results`` generator has been exhausted
    (for example: timing).

    """
    yield '{"result": [\n'
    for line in interleave(['\n, '], results):
        yield line
    yield '\n],\n'
    yield '"metadata": {0},\n'.format(json.dumps(metadata()))
    yield '"status": "success"}\n'


def is_https(url):
    return urlparse.urlparse(url).scheme == 'https'


class TLSv1Adapter(requests.adapters.HTTPAdapter):
    """Adapter to support TLSv1 in requests."""

    def init_poolmanager(self, connections, maxsize, block=False):
        self.poolmanager = requests.packages.urllib3.poolmanager.PoolManager(
            num_pools=connections,
            maxsize=maxsize,
            block=block,
            ssl_version=ssl.PROTOCOL_TLSv1)


def tls1_get(*args, **kwargs):
    session = requests.Session()
    session.mount('https://', TLSv1Adapter())
    kwargs['verify'] = False
    return session.get(*args, **kwargs)


def check(producer):
    """Check status of a container.

    Look at the output of the worker in the container.

    """
    state = WorkerState.error
    for line in producer:
        if line.startswith('*** WORKER ERROR'):
            return WorkerState.error
        if line.startswith('*** WORKER STARTED'):
            state = WorkerState.started
    return state


def render_template(main_module_path, main_module_name,
                    language, requirements, into):
    """Create Dockerfile for ``language``.

    Consider a list of ``requirements``, and write the Dockerfile in
    directory ``into``.

    """

    dockerfile_template = jinja2.Template(
        open(os.path.join(HERE, 'containers/Dockerfile.adapter')).read())
    requirement_cmds = (
        'RUN ' + requirements_installer(language, requirements)
        if requirements else '')

    dockerfile = dockerfile_template.render(
        main_module_path=main_module_path,
        main_module_name=main_module_name,
        language=language,
        requirement_cmds=requirement_cmds)
    with open(os.path.join(into, 'Dockerfile'), 'w') as f:
        f.write(dockerfile)


def requirements_installer(language, requirements):
    """Return the command to install requirements.

    ``requirements`` is a list of packages to be installed in the
    environment for ``language``.

    """
    _, installer = LANGUAGES[language]
    return installer.format(package=' '.join(requirements))


def get_metadata_from(directory):
    """Return a dictionary from the metadata file in ``directory``.

    Try to read the file ``metadata.yml`` or ``metadata.json`` at the root
    of the directory.

    Return an empty dict if no file is found.

    """
    for filename in ['metadata.yml', 'metadata.json']:
        try:
            f = open(os.path.join(directory, filename))
            break
        except IOError:
            pass
    else:
        # tried all files, give up and return empty
        return {}
    return yaml.load(f.read())


def valid_image_name(name):
    return re.search(r'[^a-z0-9-_.]', name) is None
