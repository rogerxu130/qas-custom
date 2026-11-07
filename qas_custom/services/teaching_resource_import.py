"""Bounded course.xml normalization. No storage, network or Frappe access."""
import re
from xml.etree import ElementTree as ET

from qas_custom.services.teaching_resource_rules import MAX_FILES, normalize_tag, validate_file

MAX_XML_BYTES = 512 * 1024


def _text(node, name, maximum, required=False, language=None):
    matches = node.findall(name)
    if len(matches) > 1 and language:
        matches = [item for item in matches if item.get('lang') == language]
    if len(matches) > 1:
        raise ValueError(f'XML: ambiguous {name}. / XML 字段重复。')
    item = matches[0] if matches else None
    if item is not None and len(item):
        raise ValueError(f'XML: {name} must be plain text. / 字段不能包含嵌套标记。')
    value = (item.text or '').strip() if item is not None else ''
    if (required and not value) or len(value) > maximum:
        raise ValueError(f'XML: {name} must contain {1 if required else 0}–{maximum} characters. / 字段缺失或过长。')
    return value


def parse_course_xml(xml, local_files):
    if not isinstance(xml, str) or len(xml.encode('utf-8')) > MAX_XML_BYTES:
        raise ValueError('course.xml must be UTF-8 text, at most 512 KiB. / XML 文件过大。')
    if re.search(r'<!\s*(DOCTYPE|ENTITY)\b', xml, re.I):
        raise ValueError('XML DTD and entities are not supported. / XML 不允许 DTD 或实体声明。')
    try:
        root = ET.fromstring(xml)
    except (ET.ParseError, ValueError):
        raise ValueError('Invalid course.xml. / XML 格式错误。') from None
    if root.tag != 'course' or root.get('schema_version') != '1':
        raise ValueError('Use <course schema_version="1">. / 不支持此 XML 版本。')
    source_id = (root.get('id') or '').strip()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,139}', source_id):
        raise ValueError('XML course id must be 1–140 letters, digits, dots, colons, underscores or hyphens. / 课程 ID 无效。')
    allowed = {'series', 'sequence', 'title', 'description', 'admin_note', 'tags', 'files'}
    if any(child.tag not in allowed for child in root):
        raise ValueError('Unsupported course.xml field. / XML 含不支持的字段。')
    for name in ('series', 'sequence', 'tags', 'files'):
        if len(root.findall(name)) > 1:
            raise ValueError(f'XML: duplicate {name}. / XML 字段重复。')
    sequence = _text(root, 'sequence', 9)
    if sequence and (not sequence.isascii() or not sequence.isdigit() or int(sequence) < 1):
        raise ValueError('XML sequence must be a positive integer. / 课程序号须为正整数。')
    series = root.find('series')
    series_id = (series.get('id') or '').strip() if series is not None else ''
    if len(series_id) > 140:
        raise ValueError('XML series id is too long. / 系列 ID 过长。')
    tag_nodes = root.findall('./tags/tag')
    if len(tag_nodes) > 100 or any(len(tag) for tag in tag_nodes):
        raise ValueError('XML supports at most 100 plain-text tags. / 标签过多或格式错误。')
    tags = sorted({normalize_tag(tag.text or '') for tag in tag_nodes})
    nodes = root.findall('./files/file')
    if not 1 <= len(nodes) <= MAX_FILES:
        raise ValueError('XML must list 1–100 attachments. / XML 须列出 1–100 个附件。')
    files, names, orders = [], set(), set()
    for node in nodes:
        filename = _text(node, 'filename', 200, True)
        size, kind, _ = validate_file(filename, _text(node, 'bytes', 20, True))
        if node.get('type') != {'video': 'video', 'ppt': 'presentation'}[kind]:
            raise ValueError(f'{filename}: XML type does not match filename. / 文件类型不匹配。')
        order = node.get('order', '')
        if not re.fullmatch(r'[1-9][0-9]{0,5}', order):
            raise ValueError('XML file order must be a positive integer. / 附件顺序须为正整数。')
        order = int(order)
        if filename in names or order in orders:
            raise ValueError('XML contains duplicate filenames or file order. / 附件名称或顺序重复。')
        names.add(filename)
        orders.add(order)
        sha256 = _text(node, 'sha256', 64).lower()
        if sha256 and not re.fullmatch(r'[0-9a-f]{64}', sha256):
            raise ValueError(f'{filename}: invalid SHA256. / SHA256 格式错误。')
        files.append({'filename': filename, 'label': _text(node, 'display_name', 200, language='en') or filename,
                      'size': size, 'kind': kind, 'order': order, 'sha256': sha256})
    if not isinstance(local_files, list) or not 1 <= len(local_files) <= MAX_FILES:
        raise ValueError('Select 1–100 local attachments. / 本地附件数量无效。')
    actual = {}
    for item in local_files:
        if not isinstance(item, dict):
            raise ValueError('Invalid local attachment manifest. / 本地附件清单无效。')
        name = item.get('filename')
        size, _, _ = validate_file(name, item.get('size'))
        if name in actual:
            raise ValueError('Duplicate local filename. / 本地附件重名。')
        actual[name] = size
    if set(actual) != names:
        missing = sorted(names - set(actual))
        extra = sorted(set(actual) - names)
        raise ValueError('XML/local files differ. / XML 与文件夹不一致。 Missing / 缺少: ' + ', '.join(missing) + '; Extra / 未列入 XML: ' + ', '.join(extra))
    for item in files:
        if actual[item['filename']] != item['size']:
            raise ValueError(item['filename'] + ': size differs from XML. / 文件大小与 XML 不一致。')
    return {'source_id': source_id, 'series': {'id': series_id, 'title': _text(root, 'series', 200)},
            'sequence': int(sequence) if sequence else None,
            'title': _text(root, 'title', 200, True, 'en'),
            'summary': _text(root, 'description', 2000, language='en'),
            'admin_note': _text(root, 'admin_note', 2000, language='zh-CN'),
            'tags': tags, 'files': sorted(files, key=lambda item: item['order'])}
