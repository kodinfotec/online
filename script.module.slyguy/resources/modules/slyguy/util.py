import os
import hashlib
import shutil
import platform
import base64
import struct
import codecs
import json
import io
import gzip
import re
import threading
import socket
from contextlib import closing

from kodi_six import xbmc, xbmcgui, xbmcaddon
from six.moves import queue
from six.moves.urllib.parse import urlparse, urlunparse
from six import PY2

from .language import _
from .log import log
from .exceptions import Error
from .constants import WIDEVINE_UUID, WIDEVINE_PSSH, DEFAULT_WORKERS, ADDON_PROFILE, CHUNK_SIZE
from .session import requests

def fix_url(url):
    parse = urlparse(url)
    parse = parse._replace(path=re.sub('/{2,}','/',parse.path))
    return urlunparse(parse)

def url_sub(url):
    file_path = os.path.join(ADDON_PROFILE, 'url_subs.txt')
    if not os.path.exists(file_path):
        return url

    try:
        with open(file_path, 'r') as f:
            while True:
                pattern = f.readline()
                if not pattern: # end of file
                    break

                pattern = pattern.rstrip()
                if not pattern: # blank line
                    continue

                replace = f.readline().rstrip()
                if not replace: # no replace after pattern
                    continue

                _url = re.sub(pattern, replace, url)
                if _url != url:
                    log.debug('URL sub match: {} > {}'.format(url, _url))
                    url = _url
                    break

    except Exception as e:
        log.debug('failed to parse urls.txt')
        log.exception(e)

    return url

def check_port(port=0, default=False):
    try:
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
            s.bind(('127.0.0.1', port))
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            return s.getsockname()[1]
    except:
        return default

def kodi_db(name):
    options = []
    db_dir = xbmc.translatePath('special://database')

    for file in os.listdir(db_dir):
        db_path = os.path.join(db_dir, file)

        result = re.match('{}([0-9]+)\.db'.format(name.lower()), file.lower())
        if result:
            options.append([db_path, int(result.group(1))])

    options = sorted(options, key=lambda x: x[1], reverse=True)

    if options:
        return options[0][0]
    else:
        return None

def async_tasks(tasks, workers=DEFAULT_WORKERS, raise_on_error=True):
    def worker():
        while not task_queue.empty():
            task, index = task_queue.get_nowait()
            try:
                resp_queue.put([task(), index])
            except Exception as e:
                resp_queue.put([e, index])
            finally:
                task_queue.task_done()

    task_queue = queue.Queue()
    resp_queue = queue.Queue()

    for i in range(len(tasks)):
        task_queue.put([tasks[i], i])

    threads = []
    num_workers = min(workers, len(tasks))
    log.debug('Starting {} workers'.format(num_workers))
    for i in range(num_workers):
        thread = threading.Thread(target=worker)
        thread.daemon = True
        thread.start()
        threads.append(thread)

    results = []
    exception = None
    for i in range(len(tasks)):
        result = resp_queue.get()

        if raise_on_error and isinstance(result[0], Exception):
            with task_queue.mutex:
                task_queue.queue.clear()

            exception = result[0]
            break

        results.append(result)

    for thread in threads:
        thread.join()

    if exception:
        raise exception

    return [x[0] for x in sorted(results, key=lambda x: x[1])]

def get_addon(addon_id, required=False, install=True):
    try:
        try: return xbmcaddon.Addon(addon_id)
        except: pass

        if install:
            xbmc.executebuiltin('InstallAddon({})'.format(addon_id), True)

        kodi_rpc('Addons.SetAddonEnabled', {'addonid': addon_id, 'enabled': True}, raise_on_error=True)

        return xbmcaddon.Addon(addon_id)
    except:
        if required:
            raise Error(_(_.ADDON_REQUIRED, addon_id=addon_id))
        else:
            return None

def require_country(required=None, _raise=False):
    if not required:
        return ''

    required = required.upper()
    country  = user_country()
    if country and country != required:
        msg = _(_.GEO_COUNTRY_ERROR, required=required, current=country)
        if _raise:
            raise Error(msg)
        else:
            return msg

    return ''

def user_country():
    try:
        country = requests.get('http://ip-api.com/json/?fields=countryCode').json()['countryCode'].upper()
        log.debug('fetched user country: {}'.format(country))
        return country
    except:
        log.debug('Unable to get users country')
        return ''

def gdrivedl(url, dst_path):
    if 'drive.google.com' not in url.lower():
        raise Error('Not a gdrive url')

    ID_PATTERNS = [
        re.compile('/file/d/([0-9A-Za-z_-]{10,})(?:/|$)', re.IGNORECASE),
        re.compile('id=([0-9A-Za-z_-]{10,})(?:&|$)', re.IGNORECASE),
        re.compile('([0-9A-Za-z_-]{10,})', re.IGNORECASE)
    ]
    FILE_URL = 'https://docs.google.com/uc?export=download&id={id}&confirm={confirm}'
    CONFIRM_PATTERN = re.compile("download_warning[0-9A-Za-z_-]+=([0-9A-Za-z_-]+);", re.IGNORECASE)
    FILENAME_PATTERN = re.compile('attachment;filename="(.*?)"', re.IGNORECASE)

    id = None
    for pattern in ID_PATTERNS:
        match = pattern.search(url)
        if match:
            id = match.group(1)
            break

    if not id:
        raise Error('No file ID find in gdrive url')

    session = requests.session()
    resp = session.get(FILE_URL.format(id=id, confirm=''), stream=True)
    if not resp.ok:
        raise Error('Gdrive url no longer exists')

    if 'ServiceLogin' in resp.url:
        raise Error('Gdrive url does not have link sharing enabled')

    cookies = resp.headers.get('Set-Cookie') or ''
    if 'download_warning' in cookies:
        confirm = CONFIRM_PATTERN.search(cookies)
        resp = session.get(FILE_URL.format(id=id, confirm=confirm.group(1)), stream=True)

    filename = FILENAME_PATTERN.search(resp.headers.get('content-disposition')).group(1)
    dst_path = dst_path if os.path.isabs(dst_path) else os.path.join(dst_path, filename)

    resp.raise_for_status()
    with open(dst_path, 'wb') as f:
        for chunk in resp.iter_content(CHUNK_SIZE):
            f.write(chunk)

    return filename

def FileIO(file_name, method, chunksize=CHUNK_SIZE):
    if xbmc.getCondVisibility('System.Platform.Android'):
        file_obj = io.FileIO(file_name, method)
        if method.startswith('r'):
            return io.BufferedReader(file_obj, buffer_size=chunksize)
        else:
            return io.BufferedWriter(file_obj, buffer_size=chunksize)
    else:
        return open(file_name, method, chunksize)

def gzip_extract(in_path, chunksize=CHUNK_SIZE, raise_error=True):
    log.debug('Gzip Extracting: {}'.format(in_path))
    out_path = in_path + '_extract'

    try:
        with FileIO(out_path, 'wb') as f_out:
            with FileIO(in_path, 'rb') as in_obj:
                with gzip.GzipFile(fileobj=in_obj) as f_in:
                    shutil.copyfileobj(f_in, f_out, length=chunksize)
    except Exception as e:
        remove_file(out_path)
        if raise_error:
            raise
        log.exception(e)
        return False
    else:
        remove_file(in_path)
        shutil.move(out_path, in_path)
        return True

def xz_extract(in_path, chunksize=CHUNK_SIZE, raise_error=True):
    if PY2:
        raise Error(_.XZ_ERROR)

    import lzma

    log.debug('Gzip Extracting: {}'.format(in_path))
    out_path = in_path + '_extract'

    try:
        with FileIO(out_path, 'wb') as f_out:
            with FileIO(in_path, 'rb') as in_obj:
                with lzma.LZMAFile(filename=in_obj) as f_in:
                    shutil.copyfileobj(f_in, f_out, length=chunksize)
    except Exception as e:
        remove_file(out_path)
        if raise_error:
            raise
        log.exception(e)
        return False
    else:
        remove_file(in_path)
        shutil.move(out_path, in_path)
        return True

def load_json(filepath, encoding='utf8', raise_error=True):
    try:
        with codecs.open(filepath, 'r', encoding='utf8') as f:
            return json.load(f)
    except:
        if raise_error:
            raise
        else:
            return False

def save_json(filepath, data, raise_error=True, pretty=False, **kwargs):
    _kwargs = {'ensure_ascii': False}

    if pretty:
        _kwargs['indent'] = 4
        _kwargs['sort_keys'] = True
        _kwargs['separators'] = (',', ': ')

    if PY2:
        _kwargs['encoding'] = 'utf8'

    _kwargs.update(kwargs)

    try:
        with codecs.open(filepath, 'w', encoding='utf8') as f:
            f.write(json.dumps(data, **_kwargs))

        return True
    except:
        if raise_error:
            raise
        else:
            return False

def jwt_data(token):
    b64_string = token.split('.')[1]
    b64_string += "=" * ((4 - len(b64_string) % 4) % 4) #fix padding
    return json.loads(base64.b64decode(b64_string))

def set_kodi_string(key, value=''):
    xbmcgui.Window(10000).setProperty(key, u"{}".format(value))

def get_kodi_string(key, default=''):
    value = xbmcgui.Window(10000).getProperty(key)
    return value or default

def get_kodi_setting(key, default=None):
    data = kodi_rpc('Settings.GetSettingValue', {'setting': key})
    return data.get('value', default)

def set_kodi_setting(key, value):
    return kodi_rpc('Settings.SetSettingValue', {'setting': key, 'value': value})

def kodi_rpc(method, params=None, raise_on_error=False):
    try:
        payload = {'jsonrpc':'2.0', 'id':1}
        payload.update({'method': method})
        if params:
            payload['params'] = params

        data = json.loads(xbmc.executeJSONRPC(json.dumps(payload)))
        if 'error' in data:
            raise Exception('Kodi RPC "{} {}" returned Error: "{}"'.format(method, params or '', data['error'].get('message')))

        return data['result']
    except Exception as e:
        if raise_on_error:
            raise
        else:
            return {}

def remove_file(file_path):
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
    except:
        return False
    else:
        return True

def hash_6(value, default=None, length=6):
    if not value:
        return default

    h = hashlib.md5(u'{}'.format(value).encode('utf8'))
    return base64.b64encode(h.digest()).decode('utf8')[:length]

def md5sum(filepath):
    if not os.path.exists(filepath):
        return None

    return hashlib.md5(open(filepath,'rb').read()).hexdigest()

## to find BCOV-POLICY. Open below url
## account_id / player_id / videoid can be found by right clicking player and selecting Player Information
## https://players.brightcove.net/{account_id}/{player_id}_default/index.html?videoId={videoid}
## then seach all files for policyKey

def process_brightcove(data):
    if type(data) != dict:
        try:
            if data[0]['error_code'].upper() == 'CLIENT_GEO' or data[0].get('client_geo','').lower() == 'anonymizing_proxy':
                raise Error(_.GEO_ERROR)
            else:
                msg = data[0].get('message', data[0]['error_code'])
        except KeyError:
            msg = _.NO_ERROR_MSG

        raise Error(_(_.NO_BRIGHTCOVE_SRC, error=msg))

    sources = []

    for source in data.get('sources', []):
        if not source.get('src'):
            continue

        # HLS
        if source.get('type') == 'application/x-mpegURL' and 'key_systems' not in source:
            sources.append({'source': source, 'type': 'hls', 'order_1': 1, 'order_2': int(source.get('ext_x_version', 0))})

        # MP4
        elif source.get('container') == 'MP4' and 'key_systems' not in source:
            sources.append({'source': source, 'type': 'mp4', 'order_1': 2, 'order_2': int(source.get('avg_bitrate', 0))})

        # Widevine
        elif source.get('type') == 'application/dash+xml' and 'com.widevine.alpha' in source.get('key_systems', ''):
            sources.append({'source': source, 'type': 'widevine', 'order_1': 3, 'order_2': 0})

        elif source.get('type') == 'application/vnd.apple.mpegurl' and 'key_systems' not in source:
            sources.append({'source': source, 'type': 'hls', 'order_1': 1, 'order_2': 0})

    if not sources:
        raise Error(_.NO_BRIGHTCOVE_SRC)

    sources = sorted(sources, key = lambda x: (x['order_1'], -x['order_2']))
    source = sources[0]

    from . import plugin, inputstream

    if source['type'] == 'mp4':
        return plugin.Item(
            path = source['source']['src'],
            art = False,
        )
    elif source['type'] == 'hls':
        return plugin.Item(
            path = source['source']['src'],
            inputstream = inputstream.HLS(live=False, force=False),
            art = False,
        )
    elif source['type'] == 'widevine':
        return plugin.Item(
            path = source['source']['src'],
            inputstream = inputstream.Widevine(license_key=source['source']['key_systems']['com.widevine.alpha']['license_url']),
            art = False,
        )
    else:
        raise Error(_.NO_BRIGHTCOVE_SRC)

def get_system_arch():
    if xbmc.getCondVisibility('System.Platform.Android'):
        system = 'Android'
    elif xbmc.getCondVisibility('System.Platform.UWP') or '4n2hpmxwrvr6p' in xbmc.translatePath('special://xbmc/'):
        system = 'UWP'
    elif xbmc.getCondVisibility('System.Platform.Windows'):
        system = 'Windows'
    elif xbmc.getCondVisibility('System.Platform.IOS'):
        system = 'IOS'
    elif xbmc.getCondVisibility('System.Platform.Darwin'):
        system = 'Darwin'
    elif xbmc.getCondVisibility('System.Platform.Linux') or xbmc.getCondVisibility('System.Platform.Linux.RaspberryPi'):
        system = 'Linux'
    else:
        system = platform.system()

    if system == 'Windows':
        arch = platform.architecture()[0]
    else:
        try:
            arch = platform.machine()
        except:
            arch = ''

    if 'aarch64' in arch or 'arm64' in arch:
        #64bit kernel with 32bit userland
        if (struct.calcsize("P") * 8) == 32:
            arch = 'armv7'
        else:
            arch = 'arm64'

    elif 'arm' in arch:
        if 'v6' in arch:
            arch = 'armv6'
        else:
            arch = 'armv7'

    elif arch == 'i686':
        arch = 'i386'

    log.debug('System: {}, Arch: {}'.format(system, arch))

    return system, arch

def strip_namespaces(tree):
    for el in tree.iter():
        tag = el.tag
        if tag and isinstance(tag, str) and tag[0] == '{':
            el.tag = tag.partition('}')[2]
        attrib = el.attrib
        if attrib:
            for name, value in list(attrib.items()):
                if name and isinstance(name, str) and name[0] == '{':
                    del attrib[name]
                    attrib[name.partition('}')[2]] = value

def cenc_init(data=None, uuid=None, kids=None):
    data = data or bytearray()
    uuid = uuid or WIDEVINE_UUID
    kids = kids or []

    length = len(data) + 32

    if kids:
        #each kid is 16 bytes (+ 4 for kid count)
        length += (len(kids) * 16) + 4

    init_data = bytearray(length)
    pos = 0

    # length (4 bytes)
    r_uint32 = struct.pack(">I", length)
    init_data[pos:pos+len(r_uint32)] = r_uint32
    pos += len(r_uint32)

    # pssh (4 bytes)
    init_data[pos:pos+len(r_uint32)] = WIDEVINE_PSSH
    pos += len(WIDEVINE_PSSH)

    # version (1 if kids else 0)
    r_uint32 = struct.pack("<I", 1 if kids else 0)
    init_data[pos:pos+len(r_uint32)] = r_uint32
    pos += len(r_uint32)

    # uuid (16 bytes)
    init_data[pos:pos+len(uuid)] = uuid
    pos += len(uuid)

    if kids:
        # kid count (4 bytes)
        r_uint32 = struct.pack(">I", len(kids))
        init_data[pos:pos+len(r_uint32)] = r_uint32
        pos += len(r_uint32)

        for kid in kids:
            # each kid (16 bytes)
            init_data[pos:pos+len(uuid)] = kid
            pos += len(kid)

    # length of data (4 bytes)
    r_uint32 = struct.pack(">I", len(data))
    init_data[pos:pos+len(r_uint32)] = r_uint32
    pos += len(r_uint32)

    # data (X bytes)
    init_data[pos:pos+len(data)] = data
    pos += len(data)

    return base64.b64encode(init_data).decode('utf8')

def parse_cenc_init(b64string):
    init_data = bytearray(base64.b64decode(b64string))
    pos = 0

    # length (4 bytes)
    r_uint32 = init_data[pos:pos+4]
    length, = struct.unpack(">I", r_uint32)
    pos += 4

    # pssh (4 bytes)
    r_uint32 = init_data[pos:pos+4]
    pssh, = struct.unpack(">I", r_uint32)
    pos += 4

    # version (4 bytes) (1 if kids else 0)
    r_uint32 = init_data[pos:pos+4]
    version, = struct.unpack("<I", r_uint32)
    pos += 4

    # uuid (16 bytes)
    uuid = init_data[pos:pos+16]
    pos += 16

    kids = []
    if version == 1:
        # kid count (4 bytes)
        r_uint32 = init_data[pos:pos+4]
        num_kids, = struct.unpack(">I", r_uint32)
        pos += 4

        for i in range(num_kids):
            # each kid (16 bytes)
            kids.append(init_data[pos:pos+16])
            pos += 16

    # length of data (4 bytes)
    r_uint32 = init_data[pos:pos+4]
    data_length, = struct.unpack(">I", r_uint32)
    pos += 4

    # data
    data = init_data[pos:pos+data_length]
    pos += data_length

    return uuid, version, data, kids

def cenc_version1to0(cenc):
    uuid, version, data, kids = parse_cenc_init(cenc)

    if version != 1 or not data or uuid != WIDEVINE_UUID:
        return cenc

    return cenc_init(data)

def pthms_to_seconds(duration):
    if not duration:
        return None

    keys = [['H', 3600], ['M', 60], ['S', 1]]

    seconds = 0
    duration = duration.lstrip('PT')
    for key in keys:
        if key[0] in duration:
            count, duration = duration.split(key[0])
            seconds += float(count) * key[1]

    return int(seconds)