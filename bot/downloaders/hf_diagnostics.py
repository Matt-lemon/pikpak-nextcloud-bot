"""Allowlisted aria2 diagnostics: no arbitrary messages or signed URL paths."""
import re
from urllib.parse import urlparse

LABELS = {2: 'timeout', 3: 'not_found', 4: 'not_found', 5: 'too_slow',
          6: 'network', 8: 'resume_unsupported', 9: 'disk_full',
          10: 'piece_length_changed', 11: 'duplicate_file', 13: 'file_exists',
          14: 'rename_failed', 15: 'open_failed', 16: 'create_failed',
          17: 'file_io', 18: 'mkdir_failed', 19: 'dns', 22: 'http_header',
          23: 'redirect_limit', 24: 'authorization', 29: 'server_busy',
          32: 'checksum_failed'}
KINDS = {'expired', 'transient', 'range', 'disk_space', 'file_io', 'download', 'aria2', 'removed', 'changed'}


def number(value):
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def safe_origin(uri):
    try:
        parsed = urlparse(uri)
        host = parsed.hostname or ''
        if parsed.scheme == 'https' and (host == 'huggingface.co' or
                host in {'cdn-lfs.huggingface.co', 'cdn-lfs.hf.co', 'cas-bridge.xethub.hf.co', 'cas-server.xethub.hf.co'}):
            return 'https://' + host
    except (TypeError, ValueError):
        pass
    return None


def clean_diagnostic(value):
    """Revalidate even stored/input diagnostics, preventing journal injection."""
    if not isinstance(value, dict):
        return {}
    result = {k: number(value[k]) for k in ('errorCode', 'completedLength', 'totalLength', 'downloadSpeed') if k in value}
    status = number(value.get('httpStatus'))
    if 100 <= status <= 599:
        result['httpStatus'] = status
    gid = value.get('gid', '')
    if isinstance(gid, str) and re.fullmatch('[a-fA-F0-9]{16}', gid):
        result['gid'] = gid
    if value.get('kind') in KINDS:
        result['kind'] = value['kind']
    if value.get('status') in {'active', 'waiting', 'paused', 'error', 'complete', 'removed', 'unavailable'}:
        result['status'] = value['status']
    for key in ('effectiveOrigin', 'usedOrigin'):
        origin = safe_origin(value.get(key))
        if origin:
            result[key] = origin
    # The message is a controlled explanation of the observed code/status.
    result['errorMessage'] = LABELS.get(result.get('errorCode'), 'unclassified')
    if 'httpStatus' in result:
        result['errorMessage'] += ': HTTP ' + str(result['httpStatus'])
    return result


def diagnose(info):
    code = number(info.get('errorCode'))
    message = str(info.get('errorMessage', ''))
    # Examine untrusted text only in memory; never log it verbatim.
    match = re.search(r'(?:status\s*[=:]\s*|HTTP(?:/\d(?:\.\d)?)?\s+)([1-5]\d{2})\b', message, re.I)
    status = int(match[1]) if match else None
    if code == 9 or re.search(r'no space left|disk full|errno\s*[=:]\s*28\b', message, re.I):
        kind = 'disk_space'
    elif code in {8, 10, 32} or re.search(r'content-range|range.*(?:mismatch|invalid|not satisf)|size.*(?:mismatch|changed)', message, re.I) or status == 416:
        kind = 'range'
    elif code == 24 or status in {401, 403, 410} or re.search(r'(?:signature|signed url|request).*(?:expired|expiry)', message, re.I):
        kind = 'expired'
    elif code in {2, 5, 6, 19, 29} or status in {408, 429} or (status and status >= 500) or re.search(r'connection (?:reset|closed)|unexpected EOF|timed out|timeout', message, re.I):
        kind = 'transient'
    elif code in {13, 14, 15, 16, 17, 18}:
        kind = 'file_io'
    else:
        kind = 'download'
    origins = [safe_origin(u.get('uri')) for f in info.get('files', []) for u in f.get('uris', []) if u.get('status') == 'used']
    result = dict(info, kind=kind, httpStatus=status, usedOrigin=next((o for o in origins if o), None))
    return clean_diagnostic(result)
