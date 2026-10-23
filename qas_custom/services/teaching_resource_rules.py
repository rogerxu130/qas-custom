"""Small, framework-independent resource validation and expiry rules."""
import math
from datetime import datetime, time, timedelta
from pathlib import PurePosixPath
from zoneinfo import ZoneInfo

ZONE = ZoneInfo('Australia/Brisbane')
PART_SIZE = 32 * 1024 * 1024
MAX_SIZE = 10 * 1024**3
MAX_FILES = 100
TYPES = {'.mp4': ('video', 'video/mp4'), '.ppt': ('ppt', 'application/vnd.ms-powerpoint'), '.pptx': ('ppt', 'application/vnd.openxmlformats-officedocument.presentationml.presentation')}


def validate_file(filename, size):
    if not isinstance(filename, str) or not filename or len(filename) > 200 or any(c in filename for c in '/\\\r\n\x00'):
        raise ValueError('Choose a filename without folders or control characters (up to 200 characters).')
    if isinstance(size, bool):
        raise ValueError('Invalid file size.')
    try:
        number = int(size)
    except (ValueError, TypeError):
        raise ValueError('Invalid file size.') from None
    if str(size) != str(number) or not 0 < number <= MAX_SIZE:
        raise ValueError('Each file must be non-empty and no larger than 10 GiB.')
    kind = TYPES.get(PurePosixPath(filename).suffix.lower())
    if not kind:
        raise ValueError('Supported files: MP4, PPT and PPTX.')
    return number, *kind


def part_count(size):
    return math.ceil(int(size) / PART_SIZE)


def validate_parts(parts, size):
    expected = part_count(size)
    if len(parts) != expected:
        raise ValueError('Upload is incomplete. Resume the missing parts.')
    for index, part in enumerate(parts, 1):
        length = min(PART_SIZE, int(size) - (index - 1) * PART_SIZE)
        if part['PartNumber'] != index or part['Size'] != length:
            raise ValueError('Uploaded part size or sequence does not match this file.')


def link_lifetime(deadlines, direct=False, now=None):
    now = now or datetime.now(ZONE)
    if direct:
        return 900
    valid = []
    for value in deadlines:
        try:
            day = datetime.strptime(str(value), '%Y-%m-%d').date()
            valid.append(datetime.combine(day + timedelta(days=1), time.min, ZONE))
        except (ValueError, TypeError, OverflowError):
            continue
    if not valid:
        return 0
    return max(0, min(900, int((max(valid) - now).total_seconds())))


def normalize_tag(value):
    if not isinstance(value, str):
        raise ValueError('Tags must be text.')
    tag = ' '.join(value.strip(' ,，\t\r\n').split()).casefold()
    if not tag or len(tag) > 100 or ',' in tag or '，' in tag:
        raise ValueError('Add one tag at a time, up to 100 characters. Select or create each tag separately.')
    return tag
