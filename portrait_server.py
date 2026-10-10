"""Local portrait configuration and PNG output. Python standard library only."""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import secrets
import struct
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / 'portrait-config.json'
DEFAULTS = {'background': 'background.png', 'advisor': 'advisor.png',
            'portrait_dir': 'output/portraits', 'advisor_dir': 'output/advisors'}
LOCK = threading.RLock()
DIALOG_LOCK = threading.Lock()
TOKEN = secrets.token_urlsafe(32)

def resolve_path(value):
    p = Path(value).expanduser()
    return (p if p.is_absolute() else ROOT / p).resolve()

def read_config():
    if not CONFIG.exists():
        return dict(DEFAULTS)
    data = json.loads(CONFIG.read_text(encoding='utf-8-sig'))
    return {**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}}

def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def png_size(data):
    if len(data) < 33 or data[:8] != b'\x89PNG\r\n\x1a\n' or data[12:16] != b'IHDR':
        raise ValueError('请选择有效的 PNG 图片。')
    return struct.unpack('>II', data[16:24])

def update_config(changes):
    with LOCK:
        cfg = read_config()
        for key, value in changes.items():
            if key not in DEFAULTS or not isinstance(value, str) or not value.strip():
                raise ValueError('配置项目或路径无效。')
            p = resolve_path(value.strip())
            if key in ('background', 'advisor'):
                w, h = png_size(p.read_bytes())
                if key == 'advisor' and (w < 712 or h < 937):
                    raise ValueError('小顾问模板至少需要 712 × 937 像素。')
            elif p.exists() and not p.is_dir():
                raise ValueError('输出位置必须是文件夹。')
            cfg[key] = value.strip()
        atomic_write(CONFIG, (json.dumps(cfg, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
        return cfg

def choose_path(key):
    if key not in DEFAULTS:
        raise ValueError('未知设置。')
    import tkinter as tk
    from tkinter import filedialog
    with DIALOG_LOCK:
        ui = tk.Tk()
        ui.withdraw()
        ui.attributes('-topmost', True)
        try:
            old = resolve_path(read_config()[key])
            initial = old.parent if key in ('background', 'advisor') else old
            if not initial.is_dir():
                initial = ROOT
            if key in ('background', 'advisor'):
                return filedialog.askopenfilename(parent=ui, title='选择 PNG 模板', initialdir=str(initial), filetypes=[('PNG 图片', '*.png')])
            return filedialog.askdirectory(parent=ui, title='选择输出文件夹', initialdir=str(initial))
        finally:
            ui.destroy()

def export_images(payload):
    stem = payload.get('name', '')
    if not isinstance(stem, str) or not re.fullmatch(r'[\w\-]+', stem) or len(stem) > 100:
        raise ValueError('文件名请使用文字、数字、下划线或短横线（最多 100 字符）。')
    if stem.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*(f'COM{i}' for i in range(1,10)),*(f'LPT{i}' for i in range(1,10))}:
        raise ValueError('这个文件名是 Windows 保留名称。')
    with LOCK:
        cfg = read_config()
        outputs = []
        for key, directory, suffix in [('portrait', 'portrait_dir', ''), ('advisor', 'advisor_dir', '_advisor')]:
            value = payload.get(key)
            if value is None:
                continue
            if not isinstance(value, str) or not value.startswith('data:image/png;base64,'):
                raise ValueError('输出图片格式不正确。')
            data = base64.b64decode(value.split(',', 1)[1], validate=True)
            dimensions = png_size(data)
            expected = (156,210) if key == 'portrait' else (64,64)
            if dimensions != expected:
                raise ValueError('预览尺寸与设置不一致，请重新加载模板后输出。')
            outputs.append((resolve_path(cfg[directory]) / (stem + suffix + '.png'), data))
        if not outputs:
            raise ValueError('请先生成成品预览。')
        for path, _ in outputs:
            if path.exists() and payload.get('overwrite') is not True:
                raise ValueError('文件已存在：' + str(path) + '。请换文件名，或勾选覆盖同名文件。')
        for path, data in outputs:
            atomic_write(path, data)
        return [str(path) for path, _ in outputs]

class Handler(BaseHTTPRequestHandler):
    def send_data(self, code, data, kind='application/json; charset=utf-8'):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(data)

    def allowed_host(self):
        return self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}'

    def do_GET(self):
        if not self.allowed_host():
            self.send_data(403, {'error': 'Host rejected'})
            return
        route = urlsplit(self.path).path
        try:
            if route in ('/', '/portrait-crop.html'):
                html = (ROOT / 'portrait-crop.html').read_text(encoding='utf-8').replace('__LOCAL_TOKEN__', TOKEN)
                self.send_data(200, html.encode('utf-8'), 'text/html; charset=utf-8')
            elif route == '/api/config':
                self.send_data(200, read_config())
            elif route in ('/api/template/background', '/api/template/advisor'):
                key = route.rsplit('/', 1)[1]
                data = resolve_path(read_config()[key]).read_bytes()
                png_size(data)
                self.send_data(200, data, 'image/png')
            else:
                self.send_data(404, {'error': 'Not found'})
        except Exception as e:
            self.send_data(400, {'error': str(e)})

    def do_POST(self):
        origin = f'http://127.0.0.1:{self.server.server_port}'
        if not self.allowed_host() or self.headers.get('Origin') != origin or self.headers.get('X-Local-Token') != TOKEN:
            self.send_data(403, {'error': 'Request rejected'})
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 20_000_000:
                raise ValueError('请求大小超出限制。')
            payload = json.loads(self.rfile.read(size))
            route = urlsplit(self.path).path
            if route == '/api/config':
                result = update_config(payload)
            elif route == '/api/choose':
                key = payload.get('key')
                selected = choose_path(key)
                result = update_config({key: selected}) if selected else read_config()
            elif route == '/api/export':
                result = {'paths': export_images(payload)}
            else:
                self.send_data(404, {'error': 'Not found'})
                return
            self.send_data(200, result)
        except Exception as e:
            self.send_data(400, {'error': str(e)})

    def log_message(self, *args):
        pass

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    if not CONFIG.exists():
        update_config({})
    else:
        read_config()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    url = f'http://127.0.0.1:{server.server_port}/'
    print('Portrait tool: ' + url, flush=True)
    print('Keep this window open. Ctrl+C stops the local service.', flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

if __name__ == '__main__':
    main()
