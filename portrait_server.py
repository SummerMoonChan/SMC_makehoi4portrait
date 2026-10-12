"""Local portrait configuration and PNG output. Python standard library only."""
import argparse
import base64
import json
import os
from pathlib import Path, PureWindowsPath
import re
import secrets
import struct
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from gfx_writer import validate_gfx_name, gfx_path, rewrite_gfx, read_gfx, encode_gfx

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / 'portrait-config.json'
DEFAULTS = {'background': 'background.png', 'advisor': 'advisor.png', 'mod_root': '',
            'portrait_pattern': r'gfx\leaders\TAG', 'advisor_pattern': r'gfx\interface\ideas', 'gfx_file': 'SMC_portraits.gfx'}
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

def validate_pattern(value):
    pattern = PureWindowsPath(value)
    if not value.strip() or pattern.is_absolute() or pattern.drive or pattern.root or '..' in pattern.parts:
        raise ValueError('输出路径必须是模组内的相对路径，不能包含 .. 或盘符。')
    if any(re.search(r'[:*?"<>|]', part) or part.endswith(('.', ' ')) for part in pattern.parts):
        raise ValueError('输出路径含有文件夹禁用字符。')
    return pattern


def output_directory(cfg, key, tag):
    if not cfg.get('mod_root'):
        raise ValueError('请先选择并保存钢四模组文件夹。')
    mod = resolve_path(cfg['mod_root'])
    if not mod.is_dir():
        raise ValueError('模组文件夹不存在，请重新选择。')
    parts = validate_pattern(cfg[key].replace('TAG', tag)).parts
    directory = mod.joinpath(*parts).resolve()
    if not directory.is_relative_to(mod):
        raise ValueError('输出位置必须位于所选模组内。')
    return directory


def update_config(changes):
    with LOCK:
        cfg = read_config()
        for key, value in changes.items():
            if key not in DEFAULTS or not isinstance(value, str):
                raise ValueError('配置项目或路径无效。')
            value = value.strip()
            if key == 'mod_root':
                if value and not resolve_path(value).is_dir():
                    raise ValueError('模组文件夹不存在，请选择有效目录。')
            elif key == 'gfx_file':
                validate_gfx_name(value)
            elif key in ('portrait_pattern', 'advisor_pattern'):
                validate_pattern(value)
            elif key in ('background', 'advisor'):
                if not value:
                    raise ValueError('模板路径不能为空。')
                w, h = png_size(resolve_path(value).read_bytes())
                if key == 'advisor' and (w < 712 or h < 937):
                    raise ValueError('小顾问模板至少需要 712 × 937 像素。')
            cfg[key] = value
        atomic_write(CONFIG, (json.dumps(cfg, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
        return cfg

def choose_path(key):
    if key not in ('background', 'advisor', 'mod_root'):
        raise ValueError('未知设置。')
    import tkinter as tk
    from tkinter import filedialog
    with DIALOG_LOCK:
        ui = tk.Tk()
        ui.withdraw()
        ui.attributes('-topmost', True)
        try:
            old = resolve_path(read_config()[key] or str(ROOT))
            initial = old.parent if key in ('background', 'advisor') else old
            if not initial.is_dir():
                initial = ROOT
            if key in ('background', 'advisor'):
                return filedialog.askopenfilename(parent=ui, title='选择 PNG 模板', initialdir=str(initial), filetypes=[('PNG 图片', '*.png')])
            return filedialog.askdirectory(parent=ui, title='选择输出文件夹', initialdir=str(initial))
        finally:
            ui.destroy()

def output_stem(payload):
    tag = payload.get('tag', '')
    person = payload.get('person', '')
    if not isinstance(tag, str) or not re.fullmatch(r'[A-Z0-9]{3}', tag):
        raise ValueError('TAG 必须恰好为三个大写字母或数字。')
    if (not isinstance(person, str) or not person or len(person) > 80
            or re.search(r'[\s\\/:*?"<>|]', person) or person.endswith('.')):
        raise ValueError('人物名字不能为空、不能含空格或文件名禁用字符，最多 80 字符，结尾不能是句点。')
    return tag + '_' + person


def export_images(payload):
    stem = output_stem(payload)
    with LOCK:
        cfg = read_config()
        if not cfg.get('mod_root'):
            raise ValueError('请先选择并保存钢四模组文件夹。')
        outputs = []
        for key, directory, suffix in [('portrait', 'portrait_pattern', ''), ('advisor', 'advisor_pattern', '_small')]:
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
            outputs.append((key, output_directory(cfg, directory, payload['tag']) / (stem + suffix + '.png'), data))
        if not outputs:
            raise ValueError('请先生成成品预览。')
        mod = resolve_path(cfg['mod_root'])
        target = gfx_path(mod, cfg['gfx_file'])
        eligible = [(key, path) for key, path, _ in outputs if not path.exists() or payload.get('overwrite') is True]
        original, gfx_text = read_gfx(target) if eligible else (None, '')
        def entry(key, path):
            suffix = '_small' if key == 'advisor' else ''
            return ('GFX_portrait_' + stem + suffix, path.relative_to(mod).as_posix())
        # Validate syntax before writing any images.
        if eligible:
            rewrite_gfx(gfx_text, [entry(key, path) for key, path in eligible])
        results = []
        for key, path, data in outputs:
            exists = path.exists()
            if exists and payload.get('overwrite') is not True:
                results.append({'kind': key, 'path': str(path), 'status': 'exists', 'message': '文件已存在，未覆盖'})
                continue
            try:
                atomic_write(path, data)
                results.append({'kind': key, 'path': str(path), 'status': 'overwritten' if exists else 'saved'})
            except OSError as error:
                results.append({'kind': key, 'path': str(path), 'status': 'error', 'message': str(error)})
        successful = [r for r in results if r['status'] in ('saved', 'overwritten')]
        interface_result = {'path': str(target), 'status': 'skipped', 'message': '没有成功保存的图片，未修改 Interface。'}
        if successful:
            try:
                updated = rewrite_gfx(gfx_text, [entry(r['kind'], Path(r['path'])) for r in successful])
                encoded = encode_gfx(updated, original)
                current = target.read_bytes() if target.exists() else None
                if current != original:
                    raise ValueError('GFX 在处理期间被其他程序修改，未覆盖；请重新保存。')
                if encoded != original:
                    atomic_write(target, encoded)
                interface_result = {'path': str(target), 'status': ('saved' if original is None else 'overwritten') if encoded != original else 'unchanged',
                                    'message': '已创建 GFX' if original is None else '已更新 GFX' if encoded != original else 'GFX 条目已是最新，无需修改'}
            except (OSError, ValueError) as error:
                interface_result = {'path': str(target), 'status': 'error', 'message': str(error)}
        return {'results': results, 'interface': interface_result,
                'paths': [r['path'] for r in results if r['status'] in ('saved', 'overwritten')],
                'overwritten': [r['path'] for r in results if r['status'] == 'overwritten']}


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
                result = {'path': selected}
            elif route == '/api/export':
                result = export_images(payload)
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
