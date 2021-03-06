import re
from hashlib import md5

from swift.common.utils import split_path
from swift.common.utils import readconf

ACCESS_READABLE = 0x1
ACCESS_WRITABLE = 0x1 << 1
ACCESS_RANDOM = 0x1 << 2
ACCESS_NETWORK = 0x1 << 3
ACCESS_CDR = 0x1 << 4
ACCESS_CHECKPOINT = 0x1 << 5

DEVICE_MAP = {
    'stdin': ACCESS_READABLE,
    'stdout': ACCESS_WRITABLE,
    'stderr': ACCESS_WRITABLE,
    'input': ACCESS_RANDOM | ACCESS_READABLE,
    'output': ACCESS_RANDOM | ACCESS_WRITABLE,
    'debug': ACCESS_NETWORK,
    'image': ACCESS_CDR,
    'db': ACCESS_CHECKPOINT,
    'script': ACCESS_RANDOM | ACCESS_READABLE,
}

TAR_MIMES = ['application/x-tar', 'application/x-gtar', 'application/x-ustar',
             'application/x-gzip']
CLUSTER_CONFIG_FILENAME = 'boot/cluster.map'
NODE_CONFIG_FILENAME = 'boot/system.map'
STREAM_CACHE_SIZE = 128 * 1024

DEFAULT_EXE_SYSTEM_MAP = r'''
    [{
        "name": "executable",
        "exec": {
            "path": "{.object_path}",
            "args": "{.args}"
        },
        "file_list": [
            {
                "device": "stdout",
                "content_type": "{.content_type=text/plain}"
            }
        ]
    }]
    '''

POST_TEXT_ACCOUNT_SYSTEM_MAP = r'''
    [{
        "name": "script",
        "exec": {
            "path": "{.exe_path}",
            "args": "{.args}script"
        },
        "file_list": [
            {
                "device": "stdout",
                "content_type": "text/plain"
            }
        ]
    }]
'''

POST_TEXT_OBJECT_SYSTEM_MAP = r'''
    [{
        "name": "script",
        "exec": {
            "path": "{.exe_path}",
            "args": "{.args}script"
        },
        "file_list": [
            {
                "device": "stdin",
                "path": {.object_path}
            },
            {
                "device": "stdout",
                "content_type": "text/plain"
            }
        ]
    }]
'''

ACCOUNT_HOME_PATH = ['.', '~']

MD5HASH_LENGTH = len(md5('').hexdigest())
REPORT_LENGTH = 6
REPORT_VALIDATOR = 0
REPORT_DAEMON = 1
REPORT_RETCODE = 2
REPORT_ETAG = 3
REPORT_CDR = 4
REPORT_STATUS = 5

RE_ILLEGAL = u'([\u0000-\u0008\u000b-\u000c\u000e-\u001f\ufffe-\uffff])' + \
             u'|' + \
             u'([%s-%s][^%s-%s])|([^%s-%s][%s-%s])|([%s-%s]$)|(^[%s-%s])' % \
             (unichr(0xd800), unichr(0xdbff), unichr(0xdc00), unichr(0xdfff),
              unichr(0xd800), unichr(0xdbff), unichr(0xdc00), unichr(0xdfff),
              unichr(0xd800), unichr(0xdbff), unichr(0xdc00), unichr(0xdfff),)

TIMEOUT_GRACE = 0.5


def merge_headers(final, mergeable, new):
    key_list = mergeable.keys()
    for key in key_list:
        mergeable[key] = new.get(key, mergeable[key])
        if not final.get(key):
            final[key] = str(mergeable[key])
        else:
            final[key] += ',' + str(mergeable[key])
    for key in new.keys():
        if key not in key_list:
            final[key] = new[key]


def has_control_chars(line):
    if line:
        if re.search(RE_ILLEGAL, line):
            return True
        if re.search(r"[\x01-\x1F\x7F]", line):
            return True
    return False


def can_run_as_daemon(node_conf, daemon_conf):
    if node_conf.exe != daemon_conf.exe:
        return False
    if not node_conf.channels:
        return False
    if len(node_conf.channels) != len(daemon_conf.channels):
        return False
    if node_conf.connect or node_conf.bind:
        return False
    channels = sorted(node_conf.channels, key=lambda ch: ch.device)
    for n, d in zip(channels, daemon_conf.channels):
        if n.device not in d.device:
            return False
    return True


def expand_account_path(account_name, path):
    if path.account in ACCOUNT_HOME_PATH:
        return SwiftPath.init(account_name,
                              path.container,
                              path.obj)
    return path


class ObjPath:

    def __init__(self, url, path):
        self.url = url
        self.path = path

    def __eq__(self, other):
        if not isinstance(other, ObjPath):
            return False
        if self.url == other.url:
            return True
        return False

    def __ne__(self, other):
        if not isinstance(other, ObjPath):
            return True
        if self.url != other.url:
            return True
        return False


class SwiftPath(ObjPath):

    def __init__(self, url):
        (_junk, path) = url.split('swift:/')
        ObjPath.__init__(self, url, path)
        (account, container, obj) = split_path(path, 1, 3, True)
        self.account = account
        self.container = container
        self.obj = obj

    @classmethod
    def init(cls, account, container, obj):
        if not account:
            return None
        return cls('swift://' +
                   '/'.join(filter(None,
                                   (account, container, obj))))


class ImagePath(ObjPath):

    def __init__(self, url):
        (_junk, path) = url.split('file://')
        ObjPath.__init__(self, url, path)
        parts = path.split(':', 1)
        if len(parts) > 1:
            self.image = parts[0]
            self.path = parts[1]
        else:
            self.image = 'image'


class ZvmPath(ObjPath):

    def __init__(self, url):
        (_junk, path) = url.split('zvm://')
        ObjPath.__init__(self, url, path)
        (host, device) = path.split(':', 1)
        self.host = host
        if device.startswith('/dev/'):
            self.device = device
        else:
            self.device = '/dev/%s' % device


class CachePath(ObjPath):

    def __init__(self, url):
        (_junk, path) = url.split('cache:/')
        ObjPath.__init__(self, url, path)
        (etag, account, container, obj) = split_path(path, 1, 4, True)
        self.etag = etag
        self.account = account
        self.container = container
        self.obj = obj
        self.path = '/%s/%s/%s' % (account, container, obj)


class NetPath(ObjPath):

    def __init__(self, url):
        (proto, path) = url.split('://')
        ObjPath.__init__(self, url, '%s:%s' % (proto, path))


def parse_location(url):
    if not url:
        return None
    if url.startswith('swift://'):
        return SwiftPath(url)
    elif url.startswith('file://'):
        return ImagePath(url)
    elif url.startswith('zvm://'):
        return ZvmPath(url)
    elif url.startswith('cache://'):
        return CachePath(url)
    elif url.startswith('tcp://') or url.startswith('udp://'):
        return NetPath(url)
    return None


def is_swift_path(location):
    if isinstance(location, SwiftPath):
        return True
    return False


def is_zvm_path(location):
    if isinstance(location, ZvmPath):
        return True
    return False


def is_image_path(location):
    if isinstance(location, ImagePath):
        return True
    return False


def is_cache_path(location):
    if isinstance(location, CachePath):
        return True
    return False


class ZvmChannel(object):
    def __init__(self, device, access, path=None,
                 content_type=None, meta_data=None,
                 mode=None, removable='no', mountpoint='/', min_size=0):
        self.device = device
        self.access = access
        self.path = path
        self.content_type = content_type
        self.meta = meta_data if meta_data else {}
        self.mode = mode
        self.removable = removable
        self.mountpoint = mountpoint
        self.min_size = min_size


def load_server_conf(conf, sections):
    server_conf_file = conf.get('__file__', None)
    if server_conf_file:
        server_conf = readconf(server_conf_file)
        for sect in sections:
            if server_conf.get(sect, None):
                conf.update(server_conf[sect])
