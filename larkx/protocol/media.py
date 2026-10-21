import os
import re
import zlib
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

import requests
from loguru import logger
from .auth import LarkAuth
from ..config import load_config
from .region import get_region


def message_resource_url(msg_id: str, key: str, chat_id: str, host: str=None) -> str:
    """host 为所属区域的文件主机(LarkAuth.region.file_host),不传时取 LARKX_REGION。"""
    host = host or get_region(load_config()['region']).file_host
    return f'{host}/download/messages/{msg_id}/keys/{key}?chat_id={chat_id}'


def extract_resource_key(msg_type: int, content_data: dict) -> str:
    if not content_data:
        return ''
    if msg_type == 5:
        v2 = content_data.get('imageV2') or {}
        if v2.get('imageKey'):
            return v2['imageKey']
        img = (content_data.get('image') or {}).get('origin') or {}
        return img.get('key') or (content_data.get('image') or {}).get('imageKey', '')
    if msg_type in (3, 15, 7, 10):
        return content_data.get('key', '')
    return ''


def download(auth: LarkAuth, url: str, out_path: str=None, max_bytes: int=200 * 1024 * 1024):
    with requests.get(url, cookies=auth.cookies, timeout=60, stream=True) as r:
        r.raise_for_status()
        content_type = r.headers.get('content-type', '')
        if not out_path:
            buf = b''
            for chunk in r.iter_content(1 << 16):
                buf += chunk
                if len(buf) > max_bytes:
                    raise ValueError('文件过大')
            return (buf, content_type)
        n = 0
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with open(out_path, 'wb') as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
                n += len(chunk)
                if n > max_bytes:
                    raise ValueError('文件过大')
        return (out_path, content_type, n)


def open_stream(auth: LarkAuth, url: str, chunk: int=1 << 16):
    """打开一个资源, 返回 (逐块读取的迭代器, 总字节数或 0)。读完或中途放弃都会关掉连接。"""
    r = requests.get(url, cookies=auth.cookies, timeout=60, stream=True)
    try:
        r.raise_for_status()
    except Exception:
        r.close()
        raise
    # 压缩传输时 requests 会边读边解压, 原始长度对不上解压后的字节数, 就不报长度
    length = 0 if r.headers.get('content-encoding') else int(r.headers.get('content-length') or 0)

    def chunks():
        try:
            yield from r.iter_content(chunk)
        finally:
            r.close()
    return chunks(), length


# ---- 上传 ----
# 接口都在文件主机上, 带登录 cookie, 请求和响应是 lark.pan 里的 protobuf(实测得出):
#   图片: POST /api/image, multipart 表单字段 image → 响应字段 1 是图片 key(img_ 开头)
#   文件: POST /upload/create(CreateFileUploadRequest) → uploadId, 它就是文件 key(file_ 开头)
#         POST /upload/part?upload_id=&part_id=, 请求体是这一块的原始字节 → UploadFilePartResponse(块号、crc32)
#         POST /upload/complete(CompleteFileUploadRequest, 带每块的 crc32) → code 为 0 即上传完成
# 只是把内容存到飞书, 不发消息; 发消息另走 PutMessageRequest。
UPLOAD_PART_SIZE = 4 * 1024 * 1024
UPLOAD_WORKERS = 4
FILE_TYPE_FILE = 1  # CompleteFileUploadRequest.FileType.FILE
UPLOAD_KEY = re.compile(r'[\w\-.]{1,200}')


class UploadError(Exception):
    """上传到飞书失败。"""


def _upload_headers(auth: LarkAuth, content_type: str = '') -> dict:
    headers = {'origin': auth.region.web_origin, 'referer': auth.region.web_origin + '/',
               'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36'}
    if content_type:
        headers['content-type'] = content_type
    return headers


def _pan():
    from .proto.gateway import _lark_all
    return _lark_all().pan


def _post(auth: LarkAuth, path: str, what: str, **kwargs) -> bytes:
    try:
        r = requests.post(auth.region.file_host + path, cookies=auth.cookies, timeout=kwargs.pop('timeout', 60), **kwargs)
    except requests.RequestException as e:
        raise UploadError(f'{what}失败: {e}') from e
    if r.status_code != 200:
        raise UploadError(f'{what}失败: HTTP {r.status_code} {r.text[:200]}')
    return r.content


def _parse(cls, content: bytes, what: str):
    msg = cls()
    try:
        msg.ParseFromString(content)
    except Exception as e:
        raise UploadError(f'{what}后解析响应失败: {e}') from e
    return msg


def upload_image(auth: LarkAuth, data: bytes, name: str = 'image.png', mime: str = 'image/png') -> str:
    """上传一张图片, 返回图片 key(发图片消息用)。"""
    content = _post(auth, '/api/image', '上传图片', files={'image': (name or 'image.png', data, mime or 'application/octet-stream')},
                    headers=_upload_headers(auth))
    resp = _parse(_pan().PutFileResponse, content, '上传图片')
    if not UPLOAD_KEY.fullmatch(resp.key or ''):
        raise UploadError('上传图片失败: 响应里没有图片 key')
    return resp.key


def upload_file(auth: LarkAuth, fileobj, size: int, name: str, mime: str = '', progress=None,
                part_size: int = UPLOAD_PART_SIZE, workers: int = UPLOAD_WORKERS) -> str:
    """分块上传一个文件, 返回文件 key(发文件消息用)。fileobj 是打开的二进制文件, 从当前位置读 size 字节;
    progress(已上传字节, 总字节) 每传完一块调用一次。几块同时传(单个连接实测只有 1MB/s 左右)。"""
    if size <= 0:
        raise UploadError('不能上传空文件')
    pan = _pan()
    create = pan.CreateFileUploadRequest(fileType=FILE_TYPE_FILE, fileName=name, fileSize=int(size))
    content = _post(auth, '/upload/create', '创建上传', data=create.SerializeToString(),
                    headers=_upload_headers(auth, 'application/x-protobuf'))
    created = _parse(pan.CreateFileUploadResponse, content, '创建上传')
    upload_id = created.uploadId
    if not UPLOAD_KEY.fullmatch(upload_id or ''):
        raise UploadError('创建上传失败: 响应里没有 uploadId')

    def send_part(part_id: int, chunk: bytes) -> int:
        content = _post(auth, '/upload/part', f'上传第 {part_id} 块', params={'upload_id': upload_id, 'part_id': part_id},
                        data=chunk, headers=_upload_headers(auth, 'application/octet-stream'), timeout=120)
        part = _parse(pan.UploadFilePartResponse, content, f'上传第 {part_id} 块')
        crc = zlib.crc32(chunk)
        if part.partId != part_id or part.crc32 != crc:
            raise UploadError(f'上传第 {part_id} 块失败: 服务端校验和不一致')
        return crc

    crcs, done, read = {}, 0, 0
    with ThreadPoolExecutor(max(1, workers)) as pool:
        running = {}
        part_id = 0
        while read < size or running:
            # 同时在传的不超过 workers 块, 内存里最多这么多块
            while read < size and len(running) < max(1, workers):
                chunk = fileobj.read(min(part_size, size - read))
                if not chunk:
                    raise UploadError('读取待上传的文件失败: 内容比声明的短')
                part_id += 1
                read += len(chunk)
                running[pool.submit(send_part, part_id, chunk)] = (part_id, len(chunk))
            finished, _ = wait(running, return_when=FIRST_COMPLETED)
            for fut in finished:
                pid, n = running.pop(fut)
                crcs[pid] = fut.result()
                done += n
                if progress:
                    progress(done, size)
    complete = pan.CompleteFileUploadRequest(uploadId=upload_id, fileType=FILE_TYPE_FILE, fileName=name, mime=mime or '')
    for k in sorted(crcs):
        complete.partToCrc32[k] = crcs[k]
    content = _post(auth, '/upload/complete', '完成上传', data=complete.SerializeToString(),
                    headers=_upload_headers(auth, 'application/x-protobuf'))
    result = _parse(pan.CompleteFileUploadResponse, content, '完成上传')
    if result.code != 0:
        raise UploadError(f'完成上传失败: code={result.code}')
    return upload_id


def download_message_resource(auth: LarkAuth, msg: dict, out_path: str=None):
    key = extract_resource_key(msg.get('msg_type', 0), msg.get('content_data') or {})
    if not key:
        raise ValueError('该消息没有可下载的资源')
    url = message_resource_url(msg['msg_id'], key, msg['chat_id'], host=auth.region.file_host)
    logger.info(f'下载资源: {url}')
    return download(auth, url, out_path)
