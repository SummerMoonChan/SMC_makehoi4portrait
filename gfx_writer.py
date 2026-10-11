"""Preserve GFX text while updating exact names and reverse-scanning TAG anchors."""
import re
from pathlib import Path


def validate_gfx_name(name):
    if (not isinstance(name, str) or not name or len(name) > 120
            or re.search(r'[\\/:*?"<>|\r\n]', name) or not name.lower().endswith('.gfx')
            or name != name.strip()):
        raise ValueError('Interface 请只填写 .gfx 文件名，不能包含目录；固定写入模组的 interface 文件夹。')
    return name


def gfx_path(mod, name):
    path = (mod / 'interface' / validate_gfx_name(name)).resolve()
    if not path.is_relative_to(mod):
        raise ValueError('Interface 文件必须位于所选模组内。')
    return path


def tokenize(text):
    pattern = re.compile(r'#[^\r\n]*|"(?:\\.|[^"\\])*"|[{}=]|[^\s{}="#]+')
    tokens, position = [], 0
    for match in pattern.finditer(text):
        if text[position:match.start()].strip():
            raise ValueError('GFX 存在无法识别的引号或字符，未写入文件。')
        position = match.end()
        if not match.group().startswith('#'):
            tokens.append((match.group(), match.start(), match.end()))
    if text[position:].strip():
        raise ValueError('GFX 存在无法识别的引号或字符，未写入文件。')
    pairs, stack = {}, []
    for i, (value, _, _) in enumerate(tokens):
        if value == '{':
            stack.append(i)
        elif value == '}':
            if not stack:
                raise ValueError('GFX 花括号不匹配，未写入文件。')
            start = stack.pop()
            pairs[start] = i
    if stack:
        raise ValueError('GFX 花括号不匹配，未写入文件。')
    return tokens, pairs


def indentation(text, position):
    start = text.rfind('\n', 0, position) + 1
    prefix = text[start:position]
    return prefix if not prefix.strip() else '\t'


def parse_gfx(text):
    tokens, pairs = tokenize(text)
    containers, sprites = [], []
    for i in range(len(tokens) - 2):
        if [t[0] for t in tokens[i:i+3]] != ['spriteTypes', '=', '{']:
            continue
        opening, closing = i + 2, pairs[i + 2]
        container = {'open': tokens[opening][2], 'close': tokens[closing][1]}
        containers.append(container)
        j = opening + 1
        while j < closing:
            if j + 2 < closing and [t[0] for t in tokens[j:j+3]] == ['spriteType', '=', '{']:
                end = pairs[j+2]
                fields, k = {}, j + 3
                while k < end:
                    if k + 2 < end and tokens[k+1][0] == '=' and tokens[k+2][0].startswith('"'):
                        fields[tokens[k][0]] = (tokens[k+2][0][1:-1], tokens[k+2][1], tokens[k+2][2])
                    if tokens[k][0] == '{':
                        k = pairs[k] + 1
                    else:
                        k += 1
                sprites.append({'start': tokens[j][1], 'end': tokens[end][2],
                                'close': tokens[end][1], 'fields': fields, 'container': container})
                j = end + 1
            elif tokens[j][0] == '{':
                j = pairs[j] + 1
            else:
                j += 1
    if not containers:
        raise ValueError('目标 GFX 中未找到 spriteTypes = {，未写入文件。')
    return containers, sprites


def quote(value):
    return '"' + value.replace('\\', '/').replace('"', '\\"') + '"'


def rewrite_gfx(text, entries):
    """entries: ordered (complete GFX name, mod-relative PNG path) pairs."""
    containers, sprites = parse_gfx(text)
    newline = '\r\n' if '\r\n' in text else '\n'
    edits, fresh = [], []
    for name, texture in entries:
        matching = [s for s in sprites if s['fields'].get('name', ('',))[0] == name]
        if not matching:
            fresh.append((name, texture))
            continue
        for sprite in matching:
            field = sprite['fields'].get('texturefile')
            if field:
                if field[0] != texture:
                    edits.append((field[1], field[2], quote(texture)))
            else:
                indent = indentation(text, sprite['start']) + '\t'
                line_start = text.rfind('\n', 0, sprite['close']) + 1
                position = line_start if not text[line_start:sprite['close']].strip() else sprite['close']
                edits.append((position, position, indent + 'texturefile = ' + quote(texture) + newline))
    if fresh:
        tag = re.fullmatch(r'GFX_portrait_([A-Z0-9]{3})_.+', fresh[0][0]).group(1)
        anchor = None
        for sprite in reversed(sprites):
            name = sprite['fields'].get('name', ('',))[0]
            match = re.fullmatch(r'GFX_portrait_([A-Z0-9]{3})_.+', name)
            if match and match.group(1) <= tag:
                anchor = sprite
                break
        position = anchor['end'] if anchor else containers[0]['open']
        if anchor:
            line_end = text.find('\n', position)
            line_end = len(text) if line_end < 0 else line_end
            tail = text[position:line_end].strip()
            if not tail or tail.startswith('#'):
                position = line_end - 1 if line_end > position and text[line_end-1] == '\r' else line_end
        first = next((s for s in sprites if s['container'] == containers[0]), None)
        indent = indentation(text, (anchor or first)['start']) if anchor or first else '\t'
        blocks = []
        for name, texture in fresh:
            blocks.append(indent + 'spriteType = {' + newline + indent + '\tname = ' + quote(name)
                          + newline + indent + '\ttexturefile = ' + quote(texture) + newline + indent + '}')
        insertion = newline + newline.join(blocks)
        if position < len(text) and text[position] not in '\r\n':
            insertion += newline
        edits.append((position, position, insertion))
    for start, end, value in sorted(edits, key=lambda edit: (edit[0], edit[1]), reverse=True):
        text = text[:start] + value + text[end:]
    return text


def read_gfx(path):
    raw = path.read_bytes() if path.exists() else None
    try:
        text = raw.decode('utf-8-sig') if raw is not None else '# Portraits prepared by 647\nspriteTypes = {\n}\n'
    except UnicodeDecodeError as error:
        raise ValueError('GFX 不是 UTF-8 编码，未修改文件；请先转换成 UTF-8。') from error
    return raw, text


def encode_gfx(text, original):
    return (b'\xef\xbb\xbf' if original and original.startswith(b'\xef\xbb\xbf') else b'') + text.encode('utf-8')
