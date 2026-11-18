import gc
try:
    import uasyncio as asyncio
except ImportError:
    import asyncio

from microdot import Microdot as BaseMicrodot
from microdot import mro
from microdot import NoCaseDict
from microdot import Request as BaseRequest
from microdot import Response as BaseResponse
from microdot import print_exception
from microdot import HTTPException
from microdot import MUTED_SOCKET_ERRORS


def _iscoroutine(coro):
    return hasattr(coro, 'send') and hasattr(coro, 'throw')


async def _wait_for(coro, timeout):
    return await asyncio.wait_for(coro, timeout)


class Request(BaseRequest):
    @staticmethod
    async def create(app, client_reader, client_writer, client_addr):
        # request line
        try:
            line = (await _wait_for(Request._safe_readline(client_reader), 5.0)).strip().decode()
            if not line:
                return None
            method, url, http_version = line.split()
            http_version = http_version.split('/', 1)[1]
        except Exception:
            return None

        # headers
        headers = NoCaseDict()
        content_length = 0
        while True:
            try:
                line = (await _wait_for(
                    Request._safe_readline(client_reader), 5.0)).strip().decode()
                if line == '':
                    break
                header, value = line.split(':', 1)
                value = value.strip()
                headers[header] = value
                if header.lower() == 'content-length':
                    content_length = int(value)
            except Exception:
                return None

        # body
        body = b''
        if content_length and content_length <= Request.max_body_length:
            try:
                body = await _wait_for(
                    client_reader.readexactly(content_length), 5.0)
            except Exception:
                return None
            stream = None
        else:
            body = b''
            stream = client_reader

        return Request(app, client_addr, method, url, http_version, headers,
                       body=body, stream=stream,
                       sock=(client_reader, client_writer))

    @staticmethod
    async def _safe_readline(stream):
        line = (await stream.readline())
        if len(line) > Request.max_readline:
            raise ValueError('line too long')
        return line


class Response(BaseResponse):
    async def write(self, stream):
        self.complete()
        write_failed = False

        try:
            if isinstance(self.body, str):
                self.body = self.body.encode('utf-8')

            if 'Content-Length' not in self.headers:
                if isinstance(self.body, (bytes, bytearray)):
                    self.headers['Content-Length'] = str(len(self.body))

            reason = self.reason if self.reason is not None else \
                ('OK' if self.status_code == 200 else 'N/A')
            lines = ['HTTP/1.0 {status_code} {reason}\r\n'.format(
                status_code=self.status_code, reason=reason)]
            for header, value in self.headers.items():
                values = value if isinstance(value, list) else [value]
                for value in values:
                    lines.append('{header}: {value}\r\n'.format(
                        header=header, value=value))
            if 'Connection' not in self.headers:
                lines.append('Connection: close\r\n')
            lines.append('\r\n')
            gc.collect()
            header_data = ''.join(lines).encode()
            lines = None

            async def _write(data):
                nonlocal write_failed
                if write_failed:
                    return False
                try:
                    if hasattr(stream, 'write'):
                        stream.write(data)
                        if hasattr(stream, 'drain'):
                            await _wait_for(stream.drain(), 8.0)
                    elif hasattr(stream, 'awrite'):
                        await _wait_for(stream.awrite(data), 8.0)
                    return True
                except Exception:
                    write_failed = True
                    return False

            if isinstance(self.body, (bytes, bytearray)):
                gc.collect()
                if len(self.body) <= 384:
                    await _write(header_data + self.body)
                elif await _write(header_data):
                    mv = memoryview(self.body)
                    for i in range(0, len(self.body), 512):
                        if not await _write(bytes(mv[i:i + 512])):
                            break
                        await asyncio.sleep_ms(1)
            elif hasattr(self.body, 'read'):
                if await _write(header_data):
                    gc.collect()
                    chunk_size = getattr(self, 'send_file_buffer_size', 512)
                    n_chunks = 0
                    while True:
                        chunk = self.body.read(chunk_size)
                        if not chunk:
                            break
                        if not await _write(chunk):
                            break
                        n_chunks += 1
                        if (n_chunks & 3) == 0:
                            gc.collect()
                        await asyncio.sleep_ms(1)
        except Exception as e:
            write_failed = True
            print_exception(e)
        finally:
            self._write_failed = write_failed
            if hasattr(self.body, 'close'):
                try:
                    self.body.close()
                except Exception:
                    pass
            self.body = None


class Microdot(BaseMicrodot):
    async def start_server(self, host='0.0.0.0', port=5000, debug=False,
                           ssl=None):
        self.debug = debug

        async def serve(reader, writer):
            try:
                await self.handle_request(reader, writer)
            except BaseException as exc:
                try:
                    print_exception(exc)
                except Exception:
                    pass

        try:
            self.server = await asyncio.start_server(serve, host, port, ssl=ssl)
        except TypeError:
            self.server = await asyncio.start_server(serve, host, port)

        while True:
            try:
                await self.server.wait_closed()
                break
            except AttributeError:
                await asyncio.sleep(0.1)

    def run(self, host='0.0.0.0', port=5000, debug=False, ssl=None):
        asyncio.run(self.start_server(host=host, port=port, debug=debug, ssl=ssl))

    def shutdown(self):
        if getattr(self, 'server', None):
            self.server.close()

    async def handle_request(self, reader, writer):
        req = None
        res = None
        acquired_lock = False
        try:
            peer = None
            try:
                peer = writer.get_extra_info('peername')
            except Exception:
                pass
            try:
                req = await Request.create(self, reader, writer, peer)
            except BaseException as exc:  # pragma: no cover
                print_exception(exc)

            if req:
                # Serialize dispatch + response streaming AFTER headers are read
                # so idle browser preconnect sockets cannot stall other requests.
                waited_ms = 0
                while getattr(self, '_busy', False) and waited_ms < 8000:
                    await asyncio.sleep_ms(20)
                    waited_ms += 20
                if not getattr(self, '_busy', False):
                    self._busy = True
                    acquired_lock = True
                res = await self.dispatch_request(req)
                if res != Response.already_handled:  # pragma: no branch
                    await res.write(writer)
        except BaseException as exc:
            pass
        finally:
            if req and res is not None and not getattr(res, '_write_failed', False):
                try:
                    await asyncio.sleep_ms(10)
                except Exception:
                    pass
            s = getattr(writer, 's', None) or getattr(reader, 's', None)
            if s:
                try:
                    import uasyncio.core as _core
                    ioq = getattr(_core, '_io_queue', None)
                    if ioq:
                        if id(s) in ioq.map:
                            sm = ioq.map[id(s)]
                            if sm[0] is not None:
                                sm[0].data = None
                            if sm[1] is not None:
                                sm[1].data = None
                            del ioq.map[id(s)]
                        try:
                            ioq.poller.unregister(s)
                        except Exception:
                            pass
                except Exception:
                    pass
                try:
                    s.close()
                except Exception:
                    pass
            else:
                try:
                    if hasattr(writer, 'close'):
                        writer.close()
                except Exception:
                    pass
            gc.collect()
            if acquired_lock:
                self._busy = False
        if self.debug and req:  # pragma: no cover
            try:
                print('{method} {path} {status_code}'.format(
                    method=req.method, path=req.path, status_code=res.status_code))
            except OSError:
                pass

    async def dispatch_request(self, req):
        if req:
            if req.path == '/favicon.ico':
                return Response('', 204)
            if req.content_length > req.max_content_length:
                if 413 in self.error_handlers:
                    res = await self._invoke_handler(
                        self.error_handlers[413], req)
                else:
                    res = 'Payload too large', 413
            else:
                f = self.find_route(req)
                try:
                    res = None
                    if callable(f):
                        for handler in self.before_request_handlers:
                            res = await self._invoke_handler(handler, req)
                            if res:
                                break
                        if res is None:
                            res = await self._invoke_handler(
                                f, req, **req.url_args)
                        if isinstance(res, tuple):
                            body = res[0]
                            if isinstance(res[1], int):
                                status_code = res[1]
                                headers = res[2] if len(res) > 2 else {}
                            else:
                                status_code = 200
                                headers = res[1]
                            res = Response(body, status_code, headers)
                        elif not isinstance(res, Response):
                            res = Response(res)
                        for handler in self.after_request_handlers:
                            res = await self._invoke_handler(
                                handler, req, res) or res
                        for handler in req.after_request_handlers:
                            res = await handler(req, res) or res
                    elif f in self.error_handlers:
                        res = await self._invoke_handler(
                            self.error_handlers[f], req)
                    else:
                        res = 'Not found', f
                except HTTPException as exc:
                    if exc.status_code in self.error_handlers:
                        res = self.error_handlers[exc.status_code](req)
                    else:
                        res = exc.reason, exc.status_code
                except BaseException as exc:
                    print_exception(exc)
                    exc_class = None
                    res = None
                    if exc.__class__ in self.error_handlers:
                        exc_class = exc.__class__
                    else:
                        for c in mro(exc.__class__)[1:]:
                            if c in self.error_handlers:
                                exc_class = c
                                break
                    if exc_class:
                        try:
                            res = await self._invoke_handler(
                                self.error_handlers[exc_class], req, exc)
                        except BaseException as exc2:  # pragma: no cover
                            print_exception(exc2)
                    if res is None:
                        if 500 in self.error_handlers:
                            res = await self._invoke_handler(
                                self.error_handlers[500], req)
                        else:
                            res = 'Internal server error', 500
        else:
            if 400 in self.error_handlers:
                res = await self._invoke_handler(self.error_handlers[400], req)
            else:
                res = 'Bad request', 400
        if isinstance(res, tuple):
            res = Response(*res)
        elif not isinstance(res, Response):
            res = Response(res)
        return res

    async def _invoke_handler(self, f_or_coro, *args, **kwargs):
        ret = f_or_coro(*args, **kwargs)
        if _iscoroutine(ret):
            ret = await ret
        return ret


abort = Microdot.abort
Response.already_handled = Response()
redirect = Response.redirect
send_file = Response.send_file
