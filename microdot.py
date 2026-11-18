try:
    from sys import print_exception as _pe
    def print_exception(exc):
        try: _pe(exc)
        except Exception: pass
except ImportError:
    def print_exception(exc): pass

try:
    import ujson as json
except ImportError:
    import json

MUTED_SOCKET_ERRORS = [5, 32, 54, 104, 113, 128]

def urldecode_str(s):
    s = s.replace('+', ' ')
    parts = s.split('%')
    if len(parts) == 1:
        return s
    res = [parts[0]]
    for item in parts[1:]:
        if item == '':
            res.append('%')
        else:
            res.append(chr(int(item[:2], 16)) + item[2:])
    return ''.join(res)

class NoCaseDict(dict):
    def __init__(self, d=None):
        super().__init__(d or {})
        self.keymap = {k.lower(): k for k in self.keys() if k.lower() != k}
    def __setitem__(self, key, value):
        kl = key.lower()
        key = self.keymap.get(kl, key)
        if kl != key:
            self.keymap[kl] = key
        super().__setitem__(key, value)
    def __getitem__(self, key):
        return super().__getitem__(self.keymap.get(key.lower(), key.lower()))
    def __contains__(self, key):
        return self.keymap.get(key.lower(), key.lower()) in self.keys()
    def get(self, key, default=None):
        return super().get(self.keymap.get(key.lower(), key.lower()), default)

def mro(cls):
    if hasattr(cls, 'mro'):
        return cls.__mro__
    def _mro(c):
        res = [c]
        for b in c.__bases__:
            res += _mro(b)
        return res
    lst = _mro(cls)
    pruned = []
    for i in range(len(lst)):
        b = lst.pop(0)
        if b not in lst:
            pruned.append(b)
    return pruned

class Request():
    max_content_length = 16384
    max_body_length = 16384
    max_readline = 2048

    def __init__(self, app, client_addr, method, url, http_version, headers,
                 body=None, stream=None, sock=None):
        self.app = app
        self.client_addr = client_addr
        self.method = method
        self.url = url
        self.path = url
        self.query_string = None
        self.args = {}
        self.headers = headers
        self.content_length = 0
        self.content_type = None

        if '?' in self.path:
            self.path, self.query_string = self.path.split('?', 1)
            for pair in self.query_string.split('&'):
                if '=' in pair:
                    k, v = pair.split('=', 1)
                    self.args[urldecode_str(k)] = urldecode_str(v)

        if 'Content-Length' in self.headers:
            try: self.content_length = int(self.headers['Content-Length'])
            except ValueError: pass
        if 'Content-Type' in self.headers:
            self.content_type = self.headers['Content-Type']

        self._body = body
        self.body_used = False
        self._stream = stream
        self.sock = sock
        self._json = None
        self.after_request_handlers = []

    @property
    def body(self):
        return self._body

    @property
    def json(self):
        if self._json is None and self._body:
            try:
                self._json = json.loads(self._body.decode('utf-8'))
            except Exception:
                self._json = None
        return self._json

class Response():
    @classmethod
    def redirect(cls, location, status_code=302):
        return cls(status_code=status_code, headers={'Location': location})
    types_map = {
        'html': 'text/html',
        'css': 'text/css',
        'js': 'application/javascript',
        'json': 'application/json',
        'txt': 'text/plain',
        'png': 'image/png',
        'jpg': 'image/jpeg',
    }
    send_file_buffer_size = 512

    def __init__(self, body='', status_code=200, headers=None, reason=None):
        self.status_code = status_code
        self.headers = NoCaseDict(headers)
        self.reason = reason
        self.body = body

    def complete(self):
        if 'Content-Type' not in self.headers:
            self.headers['Content-Type'] = 'text/plain'

    @classmethod
    def send_file(cls, filename, status_code=200, content_type=None):
        import uos
        if not content_type:
            ext = filename.split('.')[-1]
            content_type = cls.types_map.get(ext, 'application/octet-stream')
        headers = {'Content-Type': content_type}
        gz = filename + '.gz'
        try:
            st = uos.stat(gz)
            f = open(gz, 'rb')
            headers['Content-Encoding'] = 'gzip'
            headers['Content-Length'] = str(st[6])
            return cls(body=f, status_code=status_code, headers=headers)
        except Exception:
            pass
        f = open(filename, 'rb')
        try:
            headers['Content-Length'] = str(uos.stat(filename)[6])
        except Exception:
            pass
        return cls(body=f, status_code=status_code, headers=headers)

class URLPattern():
    def __init__(self, url_pattern):
        self.url_pattern = url_pattern
        self.segs = url_pattern.lstrip('/').split('/') if '<' in url_pattern else None

    def match(self, path):
        if self.segs is None:
            return {} if path == self.url_pattern else None
        parts = path.lstrip('/').split('/')
        if len(parts) != len(self.segs):
            return None
        args = {}
        for s, p in zip(self.segs, parts):
            if s and s[0] == '<':
                inner = s[1:-1]
                if ':' in inner:
                    t, n = inner.rsplit(':', 1)
                    try:
                        args[n] = int(p) if t == 'int' else p
                    except ValueError:
                        return None
                else:
                    args[inner] = p
            elif s != p:
                return None
        return args

class HTTPException(Exception):
    def __init__(self, status_code, reason=None):
        self.status_code = status_code
        self.reason = reason or str(status_code) + ' error'

class Microdot():
    @staticmethod
    def abort(status_code, reason=None):
        raise HTTPException(status_code, reason=reason)
    def __init__(self):
        self.url_map = []
        self.before_request_handlers = []
        self.after_request_handlers = []
        self.error_handlers = {}
        self.debug = False

    def route(self, url_pattern, methods=None):
        def decorated(f):
            self.url_map.append((methods or ['GET'], URLPattern(url_pattern), f))
            return f
        return decorated

    def before_request(self, f):
        self.before_request_handlers.append(f)
        return f

    def after_request(self, f):
        self.after_request_handlers.append(f)
        return f

    def find_route(self, req):
        f = 404
        for route_methods, route_pattern, route_handler in self.url_map:
            args = route_pattern.match(req.path)
            if args is not None:
                req.url_args = args
                if req.method in route_methods or req.method == 'OPTIONS':
                    return route_handler
                f = 405
        return f

    def _invoke_handler(self, f, *args, **kwargs):
        ret = f(*args, **kwargs)
        return ret

Response.already_handled = Response()
send_file = Response.send_file
